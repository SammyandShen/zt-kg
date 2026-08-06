#!/usr/bin/env python3
"""next_day_gap.py — 涨停次日开盘行为统计（最近一年，沪深主板+创业板）。

样本：limit_up_events(pool='zt') 中沪主板(600/601/603/605)、深主板(000/001/002/003)、
      创业板(300/301) 的涨停事件；排除 ST/*ST/退市整理/S股，排除次日停牌（次日无K线）。
基准：涨停日收盘价（=涨停价，前复权，取自 daily_kline）。

按「涨停日 D」聚合，统计其成分股在 D+1 的：
  ① 高开比例 open2 > close1            ② 高开股的平均高开幅度
  ③ 低开比例 open2 < close1            ④ 低开股的平均低开幅度
  ⑤ 全天不及前收比例 high2 < close1    ⑥ 该子集的平均收盘亏损（close2/close1-1）

输出：next_day_gap_daily.csv（按天）、next_day_gap_events.csv（逐事件明细）
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ztkg.db"
OUT = Path(__file__).resolve().parent

BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")

SQL = """
WITH cal AS (SELECT DISTINCT trade_date d FROM daily_kline),
ev AS (
    SELECT e.id, e.code, e.name, e.trade_date, e.high_days_value, e.lb_count
    FROM limit_up_events e
    WHERE e.pool = 'zt'
      AND substr(e.code, 1, 3) IN {prefixes}
      AND e.name NOT LIKE '%ST%'
      AND e.name NOT LIKE '%退%'
      AND e.name NOT LIKE 'S%'
),
nx AS (
    SELECT ev.*, (SELECT min(d) FROM cal WHERE d > ev.trade_date) AS next_date
    FROM ev
)
SELECT nx.trade_date, nx.next_date, nx.code, nx.name, nx.lb_count,
       k1.close AS close1,
       k2.open AS open2, k2.high AS high2, k2.low AS low2, k2.close AS close2
FROM nx
JOIN daily_kline k1 ON k1.code = nx.code AND k1.trade_date = nx.trade_date
JOIN daily_kline k2 ON k2.code = nx.code AND k2.trade_date = nx.next_date
WHERE k1.close > 0
ORDER BY nx.trade_date, nx.code
"""


def board_of(code: str) -> str:
    p = code[:3]
    if p in ("600", "601", "603", "605"):
        return "沪主板"
    if p in ("000", "001", "002", "003"):
        return "深主板"
    return "创业板"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL.format(prefixes=str(BOARD_PREFIX))).fetchall()
    conn.close()

    events = []
    for r in rows:
        c1 = r["close1"]
        events.append({
            "date": r["trade_date"], "next_date": r["next_date"],
            "code": r["code"], "name": r["name"], "board": board_of(r["code"]),
            "lb_count": r["lb_count"] or 1,
            "close1": c1,
            "open_pct": (r["open2"] / c1 - 1) * 100,
            "high_pct": (r["high2"] / c1 - 1) * 100,
            "low_pct": (r["low2"] / c1 - 1) * 100,
            "close_pct": (r["close2"] / c1 - 1) * 100,
        })

    # 逐事件明细
    with open(OUT / "next_day_gap_events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(events[0].keys()))
        w.writeheader()
        w.writerows(events)

    # 按涨停日聚合
    by_day: dict[str, list] = {}
    for e in events:
        by_day.setdefault(e["date"], []).append(e)

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    daily = []
    for d in sorted(by_day):
        g = by_day[d]
        n = len(g)
        up = [e for e in g if e["open_pct"] > 0]
        dn = [e for e in g if e["open_pct"] < 0]
        flat = n - len(up) - len(dn)
        under = [e for e in g if e["high_pct"] < 0]          # 全天最高价 < 前收
        daily.append({
            "date": d, "next_date": g[0]["next_date"], "n": n,
            "gap_up_ratio": len(up) / n * 100,
            "gap_up_mean": mean([e["open_pct"] for e in up]),
            "gap_dn_ratio": len(dn) / n * 100,
            "gap_dn_mean": mean([e["open_pct"] for e in dn]),
            "flat_ratio": flat / n * 100,
            "open_mean_all": mean([e["open_pct"] for e in g]),
            "under_ratio": len(under) / n * 100,
            "under_close_mean": mean([e["close_pct"] for e in under]),
            "under_high_mean": mean([e["high_pct"] for e in under]),
            "close_mean_all": mean([e["close_pct"] for e in g]),
        })

    with open(OUT / "next_day_gap_daily.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(daily[0].keys()))
        w.writeheader()
        w.writerows(daily)

    # 全样本汇总
    n = len(events)
    up = [e for e in events if e["open_pct"] > 0]
    dn = [e for e in events if e["open_pct"] < 0]
    under = [e for e in events if e["high_pct"] < 0]
    print(f"样本区间: {daily[0]['date']} ~ {daily[-1]['date']}  交易日 {len(daily)}  事件 {n}")
    print(f"高开: {len(up)/n*100:.2f}%  均值 {mean([e['open_pct'] for e in up]):+.2f}%")
    print(f"低开: {len(dn)/n*100:.2f}%  均值 {mean([e['open_pct'] for e in dn]):+.2f}%")
    print(f"平开: {(n-len(up)-len(dn))/n*100:.2f}%")
    print(f"次日开盘均值(全样本): {mean([e['open_pct'] for e in events]):+.2f}%")
    print(f"全天不及前收: {len(under)/n*100:.2f}%  收盘亏损均值 {mean([e['close_pct'] for e in under]):+.2f}%"
          f"  最高价距前收均值 {mean([e['high_pct'] for e in under]):+.2f}%")
    print(f"次日收盘均值(全样本): {mean([e['close_pct'] for e in events]):+.2f}%")
    for b in ("沪主板", "深主板", "创业板"):
        gb = [e for e in events if e["board"] == b]
        gu = [e for e in gb if e["open_pct"] > 0]
        gd = [e for e in gb if e["open_pct"] < 0]
        gun = [e for e in gb if e["high_pct"] < 0]
        print(f"  [{b}] n={len(gb)} 高开{len(gu)/len(gb)*100:.1f}%({mean([e['open_pct'] for e in gu]):+.2f}%) "
              f"低开{len(gd)/len(gb)*100:.1f}%({mean([e['open_pct'] for e in gd]):+.2f}%) "
              f"全天不及前收{len(gun)/len(gb)*100:.1f}%({mean([e['close_pct'] for e in gun]):+.2f}%)")


if __name__ == "__main__":
    main()
