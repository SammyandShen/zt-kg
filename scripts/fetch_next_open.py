#!/usr/bin/env python3
"""fetch_next_open.py — 涨停股行情增量抓取：次日开盘价 + 日K线落库。

两个职责共用一次东财K线请求（push2his，免鉴权，前复权 fqt=1——相邻K线
比值不受除权除息影响）：
① event_next_open：zt 池每个涨停事件的次一交易日开盘价 vs 事件日收盘价
   （=涨停价），情绪趋势"隔日开盘溢价"指标用。停牌顺延到复牌首根K线。
② daily_kline：整段K线 upsert 落库（龙头评分的断板质量/反包分项用）。
   前复权值会随新除权漂移，每次整段覆盖即自动校正。

抓取集合 = 有欠账事件的股票 ∪ 非closed轮次成员（保证 resting 龙头断板日
K线可得）。事件日为最新交易日的欠账留待明日（次日尚未开盘）。

用法：python3 scripts/fetch_next_open.py [--limit N只] [--dry-run]
      python3 scripts/fetch_next_open.py --refresh-bars   # 全量zt池股票K线回补
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.parse
import urllib.request

import common

KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TIMEOUT_SEC = 15
SLEEP_BASE = 0.2


def fetch_kline(code: str, beg_yyyymmdd: str) -> list[tuple]:
    """返回 [(date, open, close, high, low, volume)]，前复权。
    市场前缀猜错时自动换一边重试。"""
    guess = "1" if code.startswith("6") else "0"
    last_exc = None
    for market in (guess, "0" if guess == "1" else "1"):
        qs = urllib.parse.urlencode({
            "secid": f"{market}.{code}", "klt": "101", "fqt": "1",
            "beg": beg_yyyymmdd, "end": "20500101",
            "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56",  # date,open,close,high,low,volume
        })
        req = urllib.request.Request(f"{KLINE_API}?{qs}",
                                     headers={"User-Agent": common.HEADERS["User-Agent"]})
        try:
            payload = json.loads(
                urllib.request.urlopen(req, timeout=TIMEOUT_SEC).read())
        except Exception as exc:      # 错前缀可能被直接断连；换另一边再试
            last_exc = exc
            continue
        last_exc = None
        klines = (payload.get("data") or {}).get("klines") or []
        if klines:
            out = []
            for row in klines:
                p = row.split(",")
                out.append((p[0], float(p[1]), float(p[2]),
                            float(p[3]), float(p[4]), float(p[5])))
            return out
    if last_exc is not None:          # 两个前缀都没拿到响应才算失败
        raise last_exc
    return []


def save_bars(conn, code: str, bars: list[tuple]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO daily_kline VALUES (?,?,?,?,?,?,?)",
        [(code, d, o, c, h, lo, v) for d, o, c, h, lo, v in bars])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少只股票（0=全部）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-bars", action="store_true",
                    help="忽略欠账，为全部zt池股票整段回补日K线")
    args = ap.parse_args()

    conn = common.open_db()
    conn.execute("PRAGMA busy_timeout=60000")   # 与其他工序并发时等锁而不是报错

    pending: dict[str, list[tuple[int, str]]] = {}
    for eid, code, d in conn.execute("""
        SELECT e.id, e.code, e.trade_date FROM limit_up_events e
        LEFT JOIN event_next_open o ON o.event_id = e.id
        WHERE e.pool='zt' AND o.event_id IS NULL
          AND e.trade_date < (SELECT MAX(trade_date) FROM limit_up_events)
        ORDER BY e.code, e.trade_date"""):
        pending.setdefault(code, []).append((eid, d))

    # K线刷新集合：欠账股 ∪ 非closed轮次成员（断板 resting 股当日无新事件，
    # 但龙头评分需要它的断板日K线）
    if args.refresh_bars:
        bar_only = {c for (c,) in conn.execute(
            "SELECT DISTINCT code FROM limit_up_events WHERE pool='zt'")}
    else:
        bar_only = {c for (c,) in conn.execute("""
            SELECT DISTINCT e.code FROM event_theme_links l
            JOIN theme_episodes te ON te.id = l.episode_id
            JOIN limit_up_events e ON e.id = l.event_id
            WHERE te.status IN ('provisional','verified')
              AND l.status != 'rejected'""")}
    bar_only -= set(pending)
    # 已回补过整段K线的股票，日常只需增量尾段；用库内该股最后一根K线做起点
    bar_start = {c: (r[0] or None) for c, r in
                 ((c, conn.execute(
                     "SELECT MAX(trade_date) FROM daily_kline WHERE code=?",
                     (c,)).fetchone()) for c in bar_only)}

    codes = list(pending) + sorted(bar_only)
    if args.limit:
        codes = codes[:args.limit]
    n_events = sum(len(pending.get(c, [])) for c in codes)
    print(f"待补次日开盘：{len(pending)} 只 / {n_events} 个事件；"
          f"K线刷新：{len(bar_only)} 只")
    if args.dry_run or not codes:
        return 0

    now = common.now_iso()
    n_done = n_wait = n_fail = n_bars = 0
    for i, code in enumerate(codes, 1):
        if code in pending:
            beg = min(d for _, d in pending[code]).replace("-", "")
        elif args.refresh_bars or not bar_start.get(code):
            beg = "20250701"                     # 数据窗口起点之前
        else:
            beg = bar_start[code].replace("-", "")
        try:
            bars = fetch_kline(code, beg)
        except Exception as exc:
            n_fail += 1
            print(f"⚠️ {code} K线抓取失败（明日重试）：{exc}", file=sys.stderr)
            continue
        if bars:
            save_bars(conn, code, bars)
            n_bars += len(bars)
        by_date = {b[0]: b for b in bars}
        dates = [b[0] for b in bars]
        rows = []
        for eid, d in pending.get(code, []):
            if d not in by_date:
                n_wait += 1           # K线里没有事件日（极端：当日停牌口径差异），跳过
                continue
            nxt = next((x for x in dates if x > d), None)
            if nxt is None:
                n_wait += 1           # 事件后尚无K线（长停牌），复牌后自动补
                continue
            close = by_date[d][2]
            nopen = by_date[nxt][1]
            if close <= 0:
                n_wait += 1
                continue
            rows.append((eid, nxt, close, nopen,
                         round((nopen / close - 1) * 100, 3), now))
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO event_next_open VALUES (?,?,?,?,?,?)", rows)
            n_done += len(rows)
        conn.commit()
        if i % 200 == 0:
            print(f"  进度 {i}/{len(codes)}（事件已补 {n_done}，K线 {n_bars} 根）")
        time.sleep(SLEEP_BASE + random.random() * 0.15)

    print(f"✅ 次日开盘补齐 {n_done} 个事件（待复牌/无K线 {n_wait}，失败 {n_fail} 只）；"
          f"日K线落库 {n_bars} 根")
    return 0


if __name__ == "__main__":
    sys.exit(main())
