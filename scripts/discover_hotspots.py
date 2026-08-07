#!/usr/bin/env python3
"""discover_hotspots.py — 热点雷达：S2 业务共振信号 + 提名收敛。

两个职责（晚班在第二次 rebuild 之后、build_site 之前跑）：
1. S2 业务图谱共振：最新交易日 zt 池里 ≥resonance_min 家公司共享同一 active
   业务节点（verified 事实，含祖先传递闭包），且该节点同名概念当日归因家数
   不足共振家数×resonance_attr_ratio——即"多家同业务公司齐涨停但同花顺标签
   没写它"，写 theme_signals(source='biz_resonance')。
2. 提名收敛：theme_signals 全量重算 hotspot_nominations（纯派生）。
   词先过归一化管线对齐词典（exact/alias/none）；门槛=≥2源 或 连续≥2日 或
   单信号强度≥nomination_strength；超 radar_expire_days 无新信号 dismissed。

红线：提名永不写 event_theme_links / theme_episodes / 正式热力；
新词转正只能走 gen_tag_meta → classify_tags → auto_adopt 既有分型链。

用法：python3 scripts/discover_hotspots.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

import common


def load_discovery_cfg() -> dict:
    cfg_path = common.REPO_ROOT / "data" / "semantic_config.json"
    return json.loads(cfg_path.read_text(encoding="utf-8")).get("discovery", {})


def business_ancestors(conn) -> dict[int, set[int]]:
    """business_concept_edges 传递闭包：node -> {自身+全部祖先}。"""
    parents: dict[int, set[int]] = defaultdict(set)
    for pid, cid in conn.execute(
            "SELECT parent_id, child_id FROM business_concept_edges"):
        parents[cid].add(pid)
    closure: dict[int, set[int]] = {}

    def climb(node: int, stack: frozenset = frozenset()) -> set[int]:
        if node in closure:
            return closure[node]
        if node in stack:                       # 防御环
            return {node}
        out = {node}
        for p in parents.get(node, ()):
            out |= climb(p, stack | {node})
        closure[node] = out
        return out

    for node in list(parents):
        climb(node)
    return closure


def derive_resonance_signals(conn, cfg: dict, latest: str) -> int:
    """S2：当日涨停股的业务节点聚合 vs 该节点同名概念的当日归因覆盖。

    意外度分母（2026-08-07 首日实测校准）：大根产业（电子元器件/化工新材料，
    基数百只级）每天都能凑够绝对家数，共振必须同时过"占节点基数比例"阈——
    3/3 的焦炭是信号，11/97 的电子元器件是背景噪声。
    """
    res_min = cfg.get("resonance_min", 3)
    attr_ratio = cfg.get("resonance_attr_ratio", 0.5)
    univ_ratio = cfg.get("resonance_univ_ratio", 0.15)
    zt_codes = {r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM limit_up_events "
        "WHERE trade_date=? AND pool='zt'", (latest,))}
    if not zt_codes:
        return 0
    closure = business_ancestors(conn)
    node_names = dict(conn.execute(
        "SELECT id, name FROM business_concepts WHERE status='active'"))

    node_codes: dict[int, set[str]] = defaultdict(set)
    universe: dict[int, set[str]] = defaultdict(set)   # 节点全库覆盖股数（基数）
    for code, bcid in conn.execute(
            "SELECT code, business_concept_id FROM stock_business_facts "
            "WHERE status='verified' AND business_concept_id IS NOT NULL"):
        for node in closure.get(bcid, {bcid}):
            if node in node_names:
                universe[node].add(code)
                if code in zt_codes:
                    node_codes[node].add(code)

    # 该概念名当日归因家数（同名弱连接：概念空间与业务空间仅按名对照）
    attr_counts: dict[str, int] = dict(conn.execute(
        """SELECT c.name, COUNT(DISTINCT e.code)
           FROM event_theme_links l
           JOIN limit_up_events e ON e.id=l.event_id
           JOIN concepts c ON c.id=l.concept_id
           WHERE e.trade_date=? AND l.status!='rejected'
           GROUP BY c.name""", (latest,)))

    now = common.now_iso()
    n = 0
    for node, codes in node_codes.items():
        base = len(universe[node]) or 1
        ratio = len(codes) / base
        if len(codes) < res_min or ratio < univ_ratio:
            continue
        name = node_names[node]
        if attr_counts.get(name, 0) >= len(codes) * attr_ratio:
            continue                       # 同花顺标签已覆盖，不重复报
        strength = round(min(1.0, ratio), 3)
        detail = json.dumps({
            "codes": sorted(codes)[:12], "n": len(codes), "base": base,
            "attr_n": attr_counts.get(name, 0),
        }, ensure_ascii=False)
        conn.execute(
            "INSERT INTO theme_signals(signal_date,source,term,strength,"
            "detail,created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(signal_date,source,term) DO UPDATE SET "
            "strength=excluded.strength, detail=excluded.detail",
            (latest, "biz_resonance", name, strength, detail, now))
        n += 1
    return n


def rebuild_nominations(conn, cfg: dict, dates: list[str]) -> dict[str, int]:
    """theme_signals → hotspot_nominations 全量重算（幂等）。"""
    alias_map = common.load_aliases()
    concept_names = {r[0] for r in conn.execute("SELECT name FROM concepts")}
    min_sources = cfg.get("nomination_min_sources", 2)
    min_days = cfg.get("nomination_min_days", 2)
    min_strength = cfg.get("nomination_strength", 0.55)
    expire = cfg.get("radar_expire_days", 5)
    fresh_cut = dates[-min(expire, len(dates))]

    grouped: dict[str, dict] = {}
    for d, source, term, strength in conn.execute(
            "SELECT signal_date, source, term, strength FROM theme_signals"):
        canon = alias_map.get(term, term)
        g = grouped.setdefault(canon, {
            "dates": set(), "sources": set(), "sum": 0.0, "max": 0.0,
            "raw_terms": set()})
        g["dates"].add(d)
        g["sources"].add(source)
        g["sum"] += strength
        g["max"] = max(g["max"], strength)
        g["raw_terms"].add(term)

    conn.execute("DELETE FROM hotspot_nominations")
    counts = {"radar": 0, "adopted": 0, "dismissed": 0}
    for canon, g in grouped.items():
        if not (len(g["sources"]) >= min_sources
                or len(g["dates"]) >= min_days
                or g["max"] >= min_strength):
            continue                               # 未达提名门槛，保持纯信号
        if canon in concept_names:
            match_kind = ("exact" if canon in g["raw_terms"] else "alias")
            matched = canon
        else:
            match_kind, matched = "none", None
        last = max(g["dates"])
        if last < fresh_cut:
            status = "dismissed"
        elif matched:
            status = "adopted"                     # 已在词典：外源印证展示
        else:
            status = "radar"                       # 新词：待分型
        conn.execute(
            "INSERT INTO hotspot_nominations(term,first_date,last_date,n_days,"
            "n_sources,strength_sum,match_kind,matched_name,status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (canon, min(g["dates"]), last, len(g["dates"]),
             len(g["sources"]), round(g["sum"], 3), match_kind, matched, status))
        counts[status] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = common.open_db()
    cfg = load_discovery_cfg()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up_events "
        "WHERE pool='zt' ORDER BY trade_date")]
    if not dates:
        print("库内无交易日，跳过")
        return 0

    conn.execute("BEGIN")
    try:
        n_res = derive_resonance_signals(conn, cfg, dates[-1])
        counts = rebuild_nominations(conn, cfg, dates)
        if args.dry_run:
            conn.rollback()
            mode = "dry-run（已回滚）"
        else:
            conn.commit()
            mode = "已写入"
        print(f"✅ 热点雷达{mode}：{dates[-1]} 业务共振信号 {n_res} 条；"
              f"提名 radar {counts['radar']} / 已在词典 {counts['adopted']} / "
              f"退场 {counts['dismissed']}")
        return 0
    except Exception:
        conn.rollback()
        raise


if __name__ == "__main__":
    sys.exit(main())
