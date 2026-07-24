#!/usr/bin/env python3
"""
build_site.py — 从 SQLite 导出 docs/data.js（网页数据，脚本生成禁手改）。

导出结构（紧凑数组，字段顺序见 EVENT_FIELDS 注释）：
  const ZTKG_DATA = {
    generated_at, dates: [...],
    day_stats: { date: [num, history_num, rate, open_num] },
    concepts:  { id: [name, total_runs, active_days] },
    aliases:   { alias: concept_id },
    stocks:    { code: name },
    events:    { date: [[code, lb_count, high_days, limit_up_type, open_num,
                         order_amount_wan, currency_value_yi, "HH:MM", reason, [concept_ids]], ...] }
  };
二期3年数据量大时：按年拆 data-YYYY.js 多文件（前端合并同一全局对象），本脚本预留 --split-year。
"""

import json
import sys
from datetime import datetime, timezone, timedelta

import common

OUT_PATH = common.REPO_ROOT / "docs" / "data.js"
TAXONOMY_PATH = common.REPO_ROOT / "data" / "taxonomy.json"
TAG_META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
LLM_REVIEW_PATH = common.REPO_ROOT / "data" / "llm_review.json"
CST = timezone(timedelta(hours=8))


def load_tag_meta() -> dict:
    """{标签名: [type, status, virtual]}，未登记的标签按 unknown/candidate 处理。"""
    if not TAG_META_PATH.exists():
        return {}
    meta = json.loads(TAG_META_PATH.read_text(encoding="utf-8"))
    meta.pop("$note", None)
    return {k: [v.get("type", "unknown"), v.get("status", "candidate"),
                bool(v.get("virtual", False))]
            for k, v in meta.items()}


def load_llm_sugg(tag_meta: dict) -> dict:
    """治理台用：仍是 candidate 的标签附 LLM 复核建议 {name: [type, conf, parent]}。"""
    if not LLM_REVIEW_PATH.exists():
        return {}
    led = json.loads(LLM_REVIEW_PATH.read_text(encoding="utf-8"))
    return {k: [v.get("t", "unknown"), v.get("c", 0), v.get("p", "")]
            for k, v in led.items()
            if (tag_meta.get(k) or ["", ""])[1] == "candidate"}


def load_taxonomy(known_names: set[str]) -> dict:
    """读 taxonomy.json，剔除 $note；报告解析不到概念的叶子名（父可为虚拟分组）。"""
    if not TAXONOMY_PATH.exists():
        return {}
    tax = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    tax.pop("$note", None)
    parents = set(tax)
    missing = sorted({child for kids in tax.values() for child in kids
                      if child not in known_names and child not in parents})
    if missing:
        print(f"⚠️ taxonomy 中 {len(missing)} 个子标签暂无涨停记录（保留，等数据）：\n"
              f"   {'、'.join(missing[:20])}{' …' if len(missing) > 20 else ''}")
    return tax


def validate_taxonomy(taxonomy: dict, tag_meta: dict) -> None:
    """正式 taxonomy 只能包含 active 节点，且父子必须在同一展示频道。"""
    nodes = set(taxonomy)
    nodes.update(child for kids in taxonomy.values() for child in kids)
    missing = sorted(n for n in nodes if n not in tag_meta)
    non_active = sorted(n for n in nodes
                        if n in tag_meta and tag_meta[n][1] != "active")

    def bucket(name: str) -> str | None:
        tag_type = (tag_meta.get(name) or ["unknown"])[0]
        if tag_type in ("sector", "product", "theme"):
            return "题材"
        return {
            "catalyst": "催化",
            "attribute": "属性",
            "event": "事件",
        }.get(tag_type)

    cross_channel = sorted(
        (parent, child)
        for parent, children in taxonomy.items()
        for child in children
        if bucket(parent) != bucket(child)
    )
    errors = []
    inverted_sector_edges = sorted(
        (parent, child)
        for parent, children in taxonomy.items()
        for child in children
        if (tag_meta.get(parent) or ["unknown"])[0] == "theme"
        and (tag_meta.get(child) or ["unknown"])[0] == "sector"
    )
    disallowed_edges = {
        ("半导体设备", "工业母机"),
        ("工业母机", "轻型输送带"),
        ("工业母机", "空分设备"),
        ("商业航天", "氦气"),
        ("半导体", "MLCC"),
        ("半导体", "薄膜电容"),
        ("半导体", "铝电解电容"),
    }
    found_disallowed = sorted(
        (parent, child)
        for parent, children in taxonomy.items()
        for child in children
        if (parent, child) in disallowed_edges
    )
    if missing:
        errors.append("缺少 tag_meta：" + "、".join(missing[:20]))
    if non_active:
        errors.append("非 active 节点：" + "、".join(non_active[:20]))
    if cross_channel:
        errors.append("跨频道父子：" +
                      "、".join(f"{p}→{c}" for p, c in cross_channel[:20]))
    if inverted_sector_edges:
        errors.append("题材下直接挂大产业：" +
                      "、".join(f"{p}→{c}" for p, c in inverted_sector_edges[:20]))
    if found_disallowed:
        errors.append("已确认错误父子：" +
                      "、".join(f"{p}→{c}" for p, c in found_disallowed))
    if errors:
        raise ValueError("taxonomy 质量校验失败；" + "；".join(errors))


def hhmm(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=CST).strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return None


# 多源优先级：同一(交易日,代码)存在多个 source 记录时只导出优先级最高的一条，
# 防止未来接入问财/开盘啦后网页端双计（数值越小优先级越高）
SOURCE_PRIORITY = {"ths": 0, "wencai": 1, "kpl": 2}


def main() -> int:
    conn = common.open_db()

    # 选主记录集合 chosen（临时表），后续所有事件/概念统计都只基于它
    best: dict[tuple, tuple] = {}
    for eid, d, code, src in conn.execute(
            "SELECT id, trade_date, code, source FROM limit_up_events"):
        p = SOURCE_PRIORITY.get(src, 99)
        k = (d, code)
        if k not in best or p < best[k][0]:
            best[k] = (p, eid)
    conn.execute("CREATE TEMP TABLE chosen(id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO chosen VALUES(?)",
                     [(v[1],) for v in best.values()])
    n_dup = conn.execute("SELECT COUNT(*) FROM limit_up_events").fetchone()[0] - len(best)
    if n_dup:
        print(f"ℹ️ 多源去重：{n_dup} 条低优先级来源记录未导出")

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT e.trade_date FROM limit_up_events e "
        "JOIN chosen c ON c.id=e.id ORDER BY e.trade_date")]

    day_stats = {d: [n, h, round(r, 4) if r is not None else None, o]
                 for d, n, h, r, o in conn.execute(
                     "SELECT trade_date, num, history_num, rate, open_num FROM day_stats")}

    concepts = {}
    for cid, name, total, days in conn.execute("""
        SELECT c.id, c.name, COUNT(*), COUNT(DISTINCT e.trade_date)
        FROM concepts c JOIN event_concepts ec ON ec.concept_id=c.id
        JOIN limit_up_events e ON e.id=ec.event_id
        JOIN chosen ch ON ch.id=e.id GROUP BY c.id"""):
        concepts[cid] = [name, total, days]

    aliases = {}
    for alias, cid in conn.execute("SELECT alias, concept_id FROM concept_aliases"):
        if cid in concepts and alias != concepts[cid][0]:
            aliases[alias] = cid

    stocks = dict(conn.execute("SELECT code, name FROM stocks"))

    ec_map: dict[int, list[int]] = {}
    for eid, cid in conn.execute(
            "SELECT event_id, concept_id FROM event_concepts "
            "WHERE event_id IN (SELECT id FROM chosen)"):
        ec_map.setdefault(eid, []).append(cid)

    events: dict[str, list] = {d: [] for d in dates}
    for row in conn.execute("""
        SELECT e.id, e.trade_date, e.code, e.lb_count, e.high_days, e.limit_up_type,
               e.open_num, e.order_amount, e.currency_value, e.first_time, e.reason_type,
               e.pool
        FROM limit_up_events e JOIN chosen ch ON ch.id=e.id
        ORDER BY e.trade_date, e.pool DESC, e.lb_count DESC, e.first_time"""):
        (eid, d, code, lb, hd, lt, opens, amt, mcap, ft, reason, pool) = row
        ev = [
            code, lb, hd, lt, opens,
            round(amt / 1e4) if amt else None,        # 封单(万)
            round(mcap / 1e8, 1) if mcap else None,   # 流通市值(亿)
            hhmm(ft), reason, ec_map.get(eid, []),
        ]
        if pool == "touch":
            ev.append(1)  # 索引10：触及涨停未封住（炸板池），缺省=封住
        events[d].append(ev)

    # 新闻：只导出最近60个交易日（控制 data.js 体积），键 "code|date"
    news: dict[str, list] = {}
    if dates:
        cutoff = dates[-min(60, len(dates))]
        for code, d, title, url, source, pub in conn.execute(
                "SELECT code, trade_date, title, url, source, pub_time FROM news "
                "WHERE trade_date>=? ORDER BY code, trade_date", (cutoff,)):
            news.setdefault(f"{code}|{d}", []).append(
                [title, url, source, (pub or "")[:16]])

    briefs: dict[str, str] = {}
    if dates:
        cutoff = dates[-min(60, len(dates))]
        for code, d, brief in conn.execute(
                "SELECT code, trade_date, brief FROM briefs WHERE trade_date>=?", (cutoff,)):
            briefs[f"{code}|{d}"] = brief

    # 语义层：公司客观业务事实、单次涨停题材、题材轮次和可追溯证据。
    # 自动题材关系保持 candidate/provisional；网页必须显式展示状态，不能把它
    # 当作已核实主营或已确认涨停原因。
    business_facts: dict[str, list] = {}
    used_evidence_ids: set[int] = set()
    for row in conn.execute("""
        SELECT id,code,tag_name,fact_type,relation_type,maturity,status,confidence,
               summary,valid_from,valid_to
        FROM stock_business_facts
        WHERE status!='rejected'
        ORDER BY code,(relation_type='core') DESC,confidence DESC,tag_name
    """):
        (fact_id, code, tag, fact_type, relation, maturity, status, confidence,
         summary, valid_from, valid_to) = row
        evidence_ids = [r[0] for r in conn.execute(
            "SELECT evidence_id FROM business_fact_evidence WHERE fact_id=?",
            (fact_id,))]
        used_evidence_ids.update(evidence_ids)
        business_facts.setdefault(code, []).append([
            tag, fact_type, relation, maturity, status, round(confidence, 2),
            summary, valid_from, valid_to, evidence_ids,
        ])

    event_themes: dict[str, list] = {}
    if dates:
        cutoff = dates[-min(60, len(dates))]
        for row in conn.execute("""
            SELECT e.id,e.code,e.trade_date,l.concept_id,l.theme_role,l.relation_type,
                   l.market_role,l.status,l.confidence,l.rationale,l.episode_id,l.source
            FROM event_theme_links l
            JOIN limit_up_events e ON e.id=l.event_id
            JOIN chosen ch ON ch.id=e.id
            WHERE e.trade_date>=? AND l.status!='rejected'
            ORDER BY e.trade_date,e.code,
                     (l.theme_role='primary') DESC,l.confidence DESC
        """, (cutoff,)):
            (eid, code, d, cid, role, relation, market_role, status, confidence,
             rationale, episode_id, source) = row
            evidence_ids = [r[0] for r in conn.execute(
                "SELECT evidence_id FROM event_theme_evidence "
                "WHERE event_id=? AND concept_id=?", (eid, cid))]
            used_evidence_ids.update(evidence_ids)
            event_themes.setdefault(f"{code}|{d}", []).append([
                cid, role, relation, market_role, status, round(confidence, 2),
                rationale, episode_id, source, evidence_ids,
            ])

    theme_episodes: dict[int, list] = {}
    for row in conn.execute("""
        SELECT ep.id,ep.concept_id,ep.start_date,ep.end_date,ep.phase,ep.status,
               ep.catalyst_summary,ep.confidence,
               COUNT(DISTINCT e.code)
        FROM theme_episodes ep
        LEFT JOIN event_theme_links l ON l.episode_id=ep.id AND l.status!='rejected'
        LEFT JOIN limit_up_events e ON e.id=l.event_id
        GROUP BY ep.id
        ORDER BY ep.start_date
    """):
        (episode_id, cid, start, end, phase, status, catalyst, confidence,
         stock_count) = row
        evidence_ids = [r[0] for r in conn.execute(
            "SELECT evidence_id FROM theme_episode_evidence WHERE episode_id=?",
            (episode_id,))]
        used_evidence_ids.update(evidence_ids)
        theme_episodes[episode_id] = [
            cid, start, end, phase, status, catalyst, round(confidence, 2),
            stock_count, evidence_ids,
        ]

    # 题材反查业务候选池：来自显式题材→业务标签映射，再连接有证据的公司事实。
    # 这里只是可研究的公司全集，不能自动算作本轮题材成分股。
    theme_business_candidates: dict[int, list] = {}
    for row in conn.execute("""
        SELECT m.concept_id,f.id,f.code,f.tag_name,f.relation_type,f.maturity,
               f.status,f.confidence,f.summary,m.mapping_type,m.status,
               m.confidence,m.rationale
        FROM theme_business_mappings m
        JOIN stock_business_facts f ON f.tag_name=m.business_tag_name
        WHERE m.status!='rejected' AND f.status NOT IN ('rejected','expired')
        ORDER BY m.concept_id,(f.relation_type='core') DESC,
                 f.confidence DESC,f.code
    """):
        (cid, fact_id, code, business_tag, relation, maturity, fact_status,
         fact_confidence, summary, mapping_type, mapping_status,
         mapping_confidence, rationale) = row
        evidence_ids = [r[0] for r in conn.execute(
            "SELECT evidence_id FROM business_fact_evidence WHERE fact_id=?",
            (fact_id,))]
        used_evidence_ids.update(evidence_ids)
        theme_business_candidates.setdefault(cid, []).append([
            code, business_tag, relation, maturity, fact_status,
            round(fact_confidence, 2), summary, mapping_type, mapping_status,
            round(mapping_confidence, 2), rationale, evidence_ids,
        ])

    semantic_evidence: dict[int, list] = {}
    if used_evidence_ids:
        placeholders = ",".join("?" for _ in used_evidence_ids)
        for row in conn.execute(f"""
            SELECT id,evidence_type,source_name,title,url,published_at,
                   subject_status,claim,reliability
            FROM evidence_items WHERE id IN ({placeholders})
        """, sorted(used_evidence_ids)):
            (evidence_id, kind, source, title, url, published, subject_status,
             claim, reliability) = row
            semantic_evidence[evidence_id] = [
                kind, source, title, url, published, subject_status, claim,
                round(reliability, 2),
            ]

    known_names = {v[0] for v in concepts.values()}
    tag_meta = load_tag_meta()
    taxonomy = load_taxonomy(known_names)
    validate_taxonomy(taxonomy, tag_meta)

    data = {
        "generated_at": common.now_iso(),
        "dates": dates,
        "day_stats": day_stats,
        "concepts": concepts,
        "aliases": aliases,
        "stocks": stocks,
        "events": events,
        "taxonomy": taxonomy,
        "tag_meta": tag_meta,
        "llm_sugg": load_llm_sugg(tag_meta),
        "news": news,
        "briefs": briefs,
        "business_facts": business_facts,
        "event_themes": event_themes,
        "theme_episodes": theme_episodes,
        "theme_business_candidates": theme_business_candidates,
        "semantic_evidence": semantic_evidence,
    }
    js = ("// 由 scripts/build_site.py 生成，禁止手改\n"
          "// event 字段: [code, 连板数, high_days, 涨停类型, 炸板次数, 封单万, 流通市值亿, 首封HH:MM, 原始原因, [概念id], touch?]  最后一位=1表示触及涨停未封住\n"
          "const ZTKG_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(js, encoding="utf-8")
    size_mb = OUT_PATH.stat().st_size / 1e6
    print(f"💾 docs/data.js: {len(dates)} 天 / {sum(len(v) for v in events.values())} 事件 / "
          f"{len(concepts)} 概念 / {size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
