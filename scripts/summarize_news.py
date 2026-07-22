#!/usr/bin/env python3
"""
summarize_news.py — 用 claude CLI（无头模式，走本机订阅、无API账单）为每只涨停股
生成一句话归因，综合 reason_type + 关联新闻标题/摘要，存 briefs 表。

设计：
- 每个交易日打包成一次 claude 调用（~80只/次），要求输出严格 JSON {code: 一句话}
- 幂等与成熟度（与 fetch_news 对齐）：brief 生成时间早于「涨停日+2天」的视为
  不成熟（当时新闻窗口未关闭，盘中/当日材料不全），会随 --days 覆盖自动重算；
  之后生成的为终态跳过。--force 无视规则全量重写
- claude 不在 PATH 时按常见安装位置兜底（launchd 环境）

用法：
  python3 scripts/summarize_news.py             # 最新交易日
  python3 scripts/summarize_news.py --days 3
  python3 scripts/summarize_news.py --date 2026-07-21 --force
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

import common

MODEL = "claude-haiku-4-5-20251001"
TIMEOUT_SEC = 600

PROMPT_HEAD = """你是A股涨停复盘助手。下面是某交易日的涨停股清单，每只股给出了
同花顺标注的涨停原因短语，以及关联新闻的标题和摘要（可能为空或有噪音）。

请为每只股写一句话涨停归因（30字以内），要求：
- 必须是有主谓结构的自然语句，说清什么事件/逻辑驱动了涨停，禁止用"+"号串标签
- 原因短语只是标签，你要做的是把它翻译成人话，新闻里有具体事件时优先引用事件
- 新闻与涨停无关时（榜单文、基金持仓文），依据标签写保守概括
- 信息不足就保守表述，禁止编造数字或事件；不要重复股票名
- 反例（禁止）："人形机器人+精密传动+超跌反弹"
- 正例："人形机器人核心传动件供应商，超跌后资金回流带动反弹"
- 正例："公告中标国网13亿订单，特高压逻辑发酵"
- 只输出一个JSON对象，键为6位股票代码，值为一句话，不要输出其他任何文字

数据：
"""


def find_claude() -> str:
    p = shutil.which("claude")
    if p:
        return p
    for cand in (os.path.expanduser("~/.local/bin/claude"),
                 os.path.expanduser("~/.claude/local/claude"),
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError("找不到 claude CLI")


def parse_json_obj(txt: str) -> dict:
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError(f"输出中未找到JSON: {txt[:200]}")
    return json.loads(m.group(0))


def summarize_date(conn, claude_bin: str, d: str, force: bool) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT e.code, e.name, e.reason_type FROM limit_up_events e "
        "LEFT JOIN briefs b ON b.code=e.code AND b.trade_date=e.trade_date "
        "WHERE e.trade_date=?"
        + ("" if force else " AND (b.code IS NULL "
                            "  OR b.created_at < datetime(e.trade_date, '+2 day'))")
        + " ORDER BY e.code", (d,)).fetchall()
    if not rows:
        return 0, 0

    lines = []
    for code, name, reason in rows:
        news = conn.execute(
            "SELECT title, snippet FROM news WHERE code=? AND trade_date=? "
            "ORDER BY (CASE WHEN title LIKE '%'||?||'%' THEN 0 ELSE 1 END), "
            "pub_time DESC LIMIT 3",
            (code, d, name)).fetchall()
        item = f"{code} {name} | 原因: {reason or '无'}"
        for t, sn in news:
            item += f"\n  新闻: {t} — {(sn or '')[:80]}"
        lines.append(item)

    prompt = PROMPT_HEAD + f"交易日: {d}\n\n" + "\n".join(lines)
    r = subprocess.run([claude_bin, "-p", "--model", MODEL],
                       input=prompt, capture_output=True, text=True,
                       timeout=TIMEOUT_SEC)
    if r.returncode != 0:
        raise RuntimeError(f"claude 退出码 {r.returncode}: {r.stderr[:300]}")
    briefs = parse_json_obj(r.stdout)

    valid_codes = {c for c, _, _ in rows}
    n = 0
    with conn:
        for code, brief in briefs.items():
            code = str(code).strip()
            if code in valid_codes and isinstance(brief, str) and brief.strip():
                conn.execute(
                    "INSERT OR REPLACE INTO briefs(code, trade_date, brief, model, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (code, d, brief.strip()[:60], MODEL, common.now_iso()))
                n += 1
    return len(rows), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--date")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = common.open_db()
    claude_bin = find_claude()
    dates = [args.date] if args.date else [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up_events "
        "ORDER BY trade_date DESC LIMIT ?", (args.days,))]

    total = 0
    for d in dates:
        try:
            n_stock, n_ok = summarize_date(conn, claude_bin, d, args.force)
            total += n_ok
            if n_stock:
                print(f"✅ {d}: {n_stock} 只待总结，写入 {n_ok} 条")
            else:
                print(f"ℹ️ {d}: 无待总结（已全覆盖）")
        except Exception as e:
            print(f"❌ {d}: {e}", file=sys.stderr)
    return 0 if total or not dates else 1


if __name__ == "__main__":
    sys.exit(main())
