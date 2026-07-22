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
CST = timezone(timedelta(hours=8))


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
               e.open_num, e.order_amount, e.currency_value, e.first_time, e.reason_type
        FROM limit_up_events e JOIN chosen ch ON ch.id=e.id
        ORDER BY e.trade_date, e.lb_count DESC, e.first_time"""):
        (eid, d, code, lb, hd, lt, opens, amt, mcap, ft, reason) = row
        events[d].append([
            code, lb, hd, lt, opens,
            round(amt / 1e4) if amt else None,        # 封单(万)
            round(mcap / 1e8, 1) if mcap else None,   # 流通市值(亿)
            hhmm(ft), reason, ec_map.get(eid, []),
        ])

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

    known_names = {v[0] for v in concepts.values()}
    data = {
        "generated_at": common.now_iso(),
        "dates": dates,
        "day_stats": day_stats,
        "concepts": concepts,
        "aliases": aliases,
        "stocks": stocks,
        "events": events,
        "taxonomy": load_taxonomy(known_names),
        "news": news,
        "briefs": briefs,
    }
    js = ("// 由 scripts/build_site.py 生成，禁止手改\n"
          "// event 字段: [code, 连板数, high_days, 涨停类型, 炸板次数, 封单万, 流通市值亿, 首封HH:MM, 原始原因, [概念id]]\n"
          "const ZTKG_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(js, encoding="utf-8")
    size_mb = OUT_PATH.stat().st_size / 1e6
    print(f"💾 docs/data.js: {len(dates)} 天 / {sum(len(v) for v in events.values())} 事件 / "
          f"{len(concepts)} 概念 / {size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
