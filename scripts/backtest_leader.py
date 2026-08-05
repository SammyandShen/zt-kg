#!/usr/bin/env python3
"""backtest_leader.py — 龙头评分体系回测（只读，不进每日班次）。

验证问题：按当日榜（as-of，无未来函数）在收盘跟随 rank1/2/3，次日开盘
相对该轮当日全体成员的超额（spread）是否为正、是否单调、是否优于
现行基线（当日最高板+首封最早）。附换龙次数体检与权重敏感性扫描。

spread_k(ep,D) = rank_k 当日事件的 open_pct − 该轮当日全体成员 open_pct 均值
（resting 席位当日无事件，天然不参与——与实盘"断板股不打板跟随"一致）。

用法：python3 scripts/backtest_leader.py [--scan]
      --scan：六维权重逐项 ±0.10（其余按比例归一）重算榜单看 spread1 变化，
              在事务内重算、算完回滚，库内数据零变更。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict

import common
from rebuild_semantic_layer import derive_leader_board, load_config


def member_matrix(conn):
    """(episode_id, trade_date) -> {code: (open_pct, lb, first_time)}"""
    out = defaultdict(dict)
    for ep, d, code, lb, ft, pct in conn.execute("""
        SELECT l.episode_id, e.trade_date, e.code, COALESCE(e.lb_count,1),
               e.first_time, o.open_pct
        FROM event_theme_links l
        JOIN limit_up_events e ON e.id=l.event_id
        LEFT JOIN event_next_open o ON o.event_id=e.id
        WHERE l.episode_id IS NOT NULL AND l.status!='rejected'
          AND e.pool='zt'"""):
        out[(ep, d)][code] = (pct, lb, ft)
    return out


def spreads_from_board(conn, matrix):
    board = defaultdict(dict)
    for ep, d, code, rank in conn.execute(
            "SELECT episode_id, trade_date, code, rank FROM episode_leader_daily"):
        board[(ep, d)][rank] = code
    spreads = {1: [], 2: [], 3: []}
    for key, m in matrix.items():
        pcts = [v[0] for v in m.values() if v[0] is not None]
        if len(pcts) < 2:
            continue
        avg = sum(pcts) / len(pcts)
        for r in (1, 2, 3):
            code = board.get(key, {}).get(r)
            if code and code in m and m[code][0] is not None:
                spreads[r].append(m[code][0] - avg)
    return spreads


def baseline_spreads(matrix):
    """现行旧口径基线：当日最高板（≥2，平手取首封最早）。"""
    out = []
    for _, m in matrix.items():
        pcts = [v[0] for v in m.values() if v[0] is not None]
        if len(pcts) < 2:
            continue
        avg = sum(pcts) / len(pcts)
        cand = [(code, v) for code, v in m.items() if v[1] >= 2]
        if not cand:
            continue
        _, v = sorted(cand, key=lambda x: (-x[1][1], x[1][2] or 10 ** 12))[0]
        if v[0] is not None:
            out.append(v[0] - avg)
    return out


def stat_line(name, xs):
    if not xs:
        return f"  {name}: 无样本"
    mean = statistics.fmean(xs)
    win = sum(1 for x in xs if x > 0) / len(xs)
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    t = mean / (sd / len(xs) ** 0.5) if sd else float("inf")
    return (f"  {name}: 均值 {mean:+.2f}pct · 胜率 {win:.0%} · n={len(xs)}"
            f" · t≈{t:.1f}")


def churn_report(conn):
    changes = defaultdict(int)
    prev = {}
    for ep, d, code in conn.execute(
            "SELECT episode_id, trade_date, code FROM episode_leader_daily "
            "WHERE rank=1 ORDER BY episode_id, trade_date"):
        if ep in prev and prev[ep] != code:
            changes[ep] += 1
        prev[ep] = code
    dist = defaultdict(int)
    for ep in prev:
        dist[changes[ep]] += 1
    total = len(prev)
    heavy = sum(v for k, v in dist.items() if k > 4)
    return (f"换龙体检：{total} 轮，换龙次数分布 "
            + " ".join(f"{k}次×{dist[k]}" for k in sorted(dist))
            + f"；>4次占比 {heavy / total:.0%}（过高说明 swap_margin 偏小）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="权重敏感性扫描")
    args = ap.parse_args()

    conn = common.open_db()
    matrix = member_matrix(conn)
    print("=== 龙头评分回测（次日开盘超额 vs 轮内当日均值）===")
    spreads = spreads_from_board(conn, matrix)
    for r in (1, 2, 3):
        print(stat_line(f"rank{r}", spreads[r]))
    print(stat_line("基线（旧口径最高板）", baseline_spreads(matrix)))
    print(churn_report(conn))

    if args.scan:
        cfg = load_config()
        w0 = dict(cfg["leader"]["weights"])
        print("\n=== 权重敏感性（±0.10 归一重算，事务内回滚）===")
        for dim in w0:
            for delta in (0.10, -0.10):
                w = dict(w0)
                w[dim] = max(0.0, w[dim] + delta)
                s = sum(w.values())
                w = {k: v / s for k, v in w.items()}
                cfg["leader"]["weights"] = w
                conn.execute("BEGIN")
                try:
                    derive_leader_board(conn, cfg)
                    sp = spreads_from_board(conn, matrix)[1]
                finally:
                    conn.rollback()
                mean = statistics.fmean(sp) if sp else 0.0
                win = (sum(1 for x in sp if x > 0) / len(sp)) if sp else 0.0
                print(f"  {dim}{delta:+.2f}: rank1 均值 {mean:+.2f}pct"
                      f" · 胜率 {win:.0%} · n={len(sp)}")
        cfg["leader"]["weights"] = w0
    return 0


if __name__ == "__main__":
    sys.exit(main())
