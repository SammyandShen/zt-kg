#!/usr/bin/env python3
"""
judge_attributions.py — 核实漏斗三队列的自动判决工序（用户授权代行人工判决）。

三个口的判决方式：
- ①活跃轮次主炒归因、②新轮次成轮/否轮：claude CLI(sonnet) 依据证据判决，
  **保守规则**——verified 需要可解释的公司-题材关联且当日叙事主导；rejected 需要
  明确反证；证据不足一律 leave（留在候选区，10日无响应自然过期）。
- ③龙头认定：纯机械规则，不用 LLM——开放轮次内当前连板最高者，平局取首封最早。

判决写入 data/facts_overrides.json（decided_by=llm/rule 留痕，与人工判决可区分），
由 rebuild_semantic_layer.py 重放生效。已判过的键自动跳过（幂等）。

用法：
  python3 scripts/judge_attributions.py --dry-run   # 只打印拟判决
  python3 scripts/judge_attributions.py             # 判决并写 overrides
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict

import common
from summarize_news import find_claude

OVERRIDES_PATH = common.REPO_ROOT / "data" / "facts_overrides.json"
MODEL = "claude-sonnet-5"
TIMEOUT_SEC = 300

PROMPT_HEAD = """你是A股涨停归因审核员。对下面的候选归因和候选题材轮次做判决。

判决规则（必须保守）：
- 归因 verdict=verified 需同时满足：①公司与题材的关联可解释（业务/公告/新闻点名，
  不能只有同花顺原因标签）②当日该股确实以此叙事为主导。
- 归因 verdict=rejected 仅当有明确反证：公司澄清、明显蹭概念、或证据显示当日
  主导叙事是另一个题材。
- 轮次 verdict=verified 需能写出具体共同催化（什么事件、哪天、什么来源级别）；
  写不出就 leave。verdict=rejected 仅当成分股之间明显没有共同叙事（纯标签巧合）。
- 证据不足、拿不准 → 一律 verdict=leave（不写理由也可）。宁可 leave 不可错判。

只输出一个 JSON 对象，不要任何其他文字：
{"links":[{"code":"...","trade_date":"...","theme":"...","verdict":"verified|rejected|leave","note":"一句话理由"}],
 "episodes":[{"theme":"...","start_date":"...","verdict":"verified|rejected|leave","catalyst":"共同催化一句话(verified时必填)","note":"..."}]}

===== 待判决数据 =====
"""


def load_overrides() -> dict:
    if OVERRIDES_PATH.exists():
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    else:
        raw = {}
    raw.setdefault("link_verdicts", [])
    raw.setdefault("episode_verdicts", [])
    raw.setdefault("leaders", [])
    return raw


def judged_keys(overrides: dict) -> tuple[set, set, set]:
    links = {(v["code"], v["trade_date"], v["theme"])
             for v in overrides["link_verdicts"]}
    eps = {(v["theme"], v["start_date"]) for v in overrides["episode_verdicts"]}
    leaders = {(v["theme"], v["start_date"]) for v in overrides["leaders"]}
    return links, eps, leaders


def open_episodes(conn) -> list[tuple]:
    return conn.execute("""
        SELECT ep.id, ep.concept_id, c.name, ep.start_date, ep.end_date, ep.phase
        FROM theme_episodes ep JOIN concepts c ON c.id=ep.concept_id
        WHERE ep.status='provisional'""").fetchall()


def evidence_snippets(conn, code: str, d: str, limit: int = 3) -> list[str]:
    out = []
    for title, source in conn.execute(
            "SELECT title,source FROM news WHERE code=? AND trade_date=? "
            "ORDER BY pub_time DESC LIMIT ?", (code, d, limit)):
        out.append(f"新闻[{source}]{title}")
    for (title,) in conn.execute("""
        SELECT e.title FROM event_evidence ee
        JOIN evidence_items e ON e.id=ee.evidence_id
        JOIN limit_up_events ev ON ev.id=ee.event_id
        WHERE ev.code=? AND ev.trade_date=? AND e.evidence_type='announcement'
        LIMIT 2""", (code, d)):
        out.append(f"公告:{title}")
    return out


def build_link_queue(conn, dates: list[str], judged: set) -> list[dict]:
    """①活跃轮次主炒归因：近2交易日、属于开放轮次的候选。"""
    open_ep_ids = {row[0] for row in open_episodes(conn)}
    if not open_ep_ids or len(dates) < 1:
        return []
    recent = dates[-2:]
    rows = conn.execute(f"""
        SELECT e.code, e.trade_date, e.name, e.reason_type, c.name,
               l.confidence, l.basis_kind, l.episode_id, e.lb_count
        FROM event_theme_links l
        JOIN limit_up_events e ON e.id=l.event_id
        JOIN concepts c ON c.id=l.concept_id
        WHERE l.status='candidate' AND l.episode_id IN
              ({','.join('?' * len(open_ep_ids))})
          AND e.trade_date IN ({','.join('?' * len(recent))})
        ORDER BY e.trade_date DESC, l.confidence DESC
    """, [*open_ep_ids, *recent]).fetchall()
    queue = []
    for code, d, name, reason, theme, conf, basis, ep_id, lb in rows:
        if (code, d, theme) in judged:
            continue
        review = conn.execute("""
            SELECT verdict, score FROM attribution_reviews r
            JOIN event_theme_links l ON l.event_id=r.event_id AND l.concept_id=r.concept_id
            JOIN limit_up_events e ON e.id=r.event_id
            JOIN concepts c ON c.id=r.concept_id
            WHERE e.code=? AND e.trade_date=? AND c.name=?
            ORDER BY r.stage DESC LIMIT 1""", (code, d, theme)).fetchone()
        queue.append({
            "code": code, "trade_date": d, "stock": name, "theme": theme,
            "reason": reason or "", "lb": lb or 1,
            "basis": basis or "无", "conf": round(conf, 2),
            "review": f"{review[0]}({review[1]:.2f})" if review else "无",
            "evidence": evidence_snippets(conn, code, d),
        })
    return queue[:30]


def build_episode_queue(conn, dates: list[str], judged: set) -> list[dict]:
    """②新轮次提案：近3交易日启动的开放轮次。"""
    if len(dates) < 3:
        return []
    d3 = dates[-3]
    queue = []
    for ep_id, cid, theme, start, _end, phase in open_episodes(conn):
        if start < d3 or (theme, start) in judged:
            continue
        members = conn.execute("""
            SELECT e.code, e.name, e.trade_date, e.lb_count, e.reason_type
            FROM event_theme_links l JOIN limit_up_events e ON e.id=l.event_id
            WHERE l.episode_id=? ORDER BY e.trade_date, e.lb_count DESC""",
            (ep_id,)).fetchall()
        ev_titles = []
        for code, _n, d, _lb, _r in members[:6]:
            ev_titles += evidence_snippets(conn, code, d, 2)
        queue.append({
            "theme": theme, "start_date": start, "phase": phase,
            "members": [f"{n}({c}){lb or 1}板 原因:{(r or '')[:40]}"
                        for c, n, d, lb, r in members[:8]],
            "evidence": ev_titles[:8],
        })
    return queue[:10]


def rule_leaders(conn, dates: list[str], judged: set) -> list[dict]:
    """③龙头认定：机械规则——开放轮次内当前最高板，平局取首封最早。"""
    out = []
    for ep_id, cid, theme, start, _end, _phase in open_episodes(conn):
        if (theme, start) in judged:
            continue
        has_leader = conn.execute(
            "SELECT 1 FROM event_theme_links WHERE episode_id=? "
            "AND market_role='leader' LIMIT 1", (ep_id,)).fetchone()
        if has_leader:
            continue
        row = conn.execute("""
            SELECT e.code, e.name, MAX(e.lb_count), MIN(e.first_time)
            FROM event_theme_links l JOIN limit_up_events e ON e.id=l.event_id
            WHERE l.episode_id=? GROUP BY e.code
            ORDER BY MAX(e.lb_count) DESC, MIN(e.first_time) ASC LIMIT 1""",
            (ep_id,)).fetchone()
        if row and (row[2] or 0) >= 2:      # 首板龙不认，至少2板才有辨识度
            out.append({"theme": theme, "start_date": start, "code": row[0],
                        "stock": row[1], "lb": row[2]})
    return out


def call_llm(claude_bin: str, payload: str) -> dict:
    r = subprocess.run([claude_bin, "-p", "--model", MODEL],
                       input=PROMPT_HEAD + payload,
                       capture_output=True, text=True, timeout=TIMEOUT_SEC)
    if r.returncode != 0:
        raise RuntimeError(f"claude 退出码 {r.returncode}: {r.stderr[:200]}")
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise ValueError(f"LLM 输出无 JSON: {r.stdout[:200]}")
    return json.loads(m.group(0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = common.open_db()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up_events ORDER BY trade_date")]
    overrides = load_overrides()
    jl, je, jld = judged_keys(overrides)

    q1 = build_link_queue(conn, dates, jl)
    q2 = build_episode_queue(conn, dates, je)
    q3 = rule_leaders(conn, dates, jld)
    if not q1 and not q2 and not q3:
        print("✅ 三队列均空，无需判决")
        return 0
    print(f"队列：归因 {len(q1)} / 新轮次 {len(q2)} / 龙头 {len(q3)}")

    day = common.now_iso()[:10]
    n_links = n_eps = 0
    if q1 or q2:
        payload = json.dumps({"归因候选": q1, "轮次候选": q2},
                             ensure_ascii=False, indent=1)
        claude_bin = find_claude()
        try:
            result = call_llm(claude_bin, payload)
        except Exception as e:
            print(f"❌ LLM 判决失败（跳过，不阻断）：{e}", file=sys.stderr)
            result = {"links": [], "episodes": []}
        valid_l = {(x["code"], x["trade_date"], x["theme"]) for x in q1}
        for v in result.get("links", []):
            key = (v.get("code"), v.get("trade_date"), v.get("theme"))
            if key not in valid_l or v.get("verdict") not in ("verified", "rejected"):
                continue
            overrides["link_verdicts"].append({
                "code": key[0], "trade_date": key[1], "theme": key[2],
                "verdict": v["verdict"], "note": (v.get("note") or "")[:120],
                "decided_by": "llm-sonnet", "decided_at": day})
            n_links += 1
        valid_e = {(x["theme"], x["start_date"]) for x in q2}
        for v in result.get("episodes", []):
            key = (v.get("theme"), v.get("start_date"))
            if key not in valid_e or v.get("verdict") not in ("verified", "rejected"):
                continue
            if v["verdict"] == "verified" and not (v.get("catalyst") or "").strip():
                continue                    # verified 必须给得出共同催化
            row = {"theme": key[0], "start_date": key[1], "verdict": v["verdict"],
                   "note": (v.get("note") or "")[:120],
                   "decided_by": "llm-sonnet", "decided_at": day}
            if v.get("catalyst"):
                row["catalyst"] = v["catalyst"][:120]
            overrides["episode_verdicts"].append(row)
            n_eps += 1

    for x in q3:
        overrides["leaders"].append({
            "theme": x["theme"], "start_date": x["start_date"], "code": x["code"],
            "note": f"规则认定：当前{x['lb']}板最高（{x['stock']}）",
            "decided_by": "rule", "decided_at": day})

    if args.dry_run:
        print(json.dumps({"拟新增判决": {
            "links": n_links, "episodes": n_eps, "leaders": len(q3)}},
            ensure_ascii=False))
        return 0
    OVERRIDES_PATH.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    print(f"✅ 判决落账：归因 {n_links}（leave 不落账），轮次 {n_eps}，"
          f"龙头 {len(q3)}；跑 rebuild_semantic_layer.py 重放生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
