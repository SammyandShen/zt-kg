#!/usr/bin/env python3
"""fetch_next_open.py — 涨停股次日开盘价增量抓取（情绪趋势"隔日开盘溢价"）。

对 zt 池（收盘封住）每个涨停事件，记录次一交易日开盘价相对事件日收盘价
（=涨停价）的涨幅，落表 event_next_open。数据源：东方财富历史K线接口
（push2his，免鉴权），前复权 fqt=1——相邻两根K线的比值不受除权除息影响。

增量设计：只处理还缺记录、且事件日早于库内最新交易日的事件（最新日的
次日尚未开盘，明天自然补上）；按股票一次抓全窗口K线批量结算该股全部
欠账，每股一个请求。停牌顺延到复牌首根K线。

用法：python3 scripts/fetch_next_open.py [--limit N只] [--dry-run]
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


def fetch_kline(code: str, beg_yyyymmdd: str) -> list[tuple[str, float, float]]:
    """返回 [(date, open, close)]，前复权。市场前缀猜错时自动换一边重试。"""
    guess = "1" if code.startswith("6") else "0"
    last_exc = None
    for market in (guess, "0" if guess == "1" else "1"):
        qs = urllib.parse.urlencode({
            "secid": f"{market}.{code}", "klt": "101", "fqt": "1",
            "beg": beg_yyyymmdd, "end": "20500101",
            "fields1": "f1,f2,f3", "fields2": "f51,f52,f53",
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
                d, o, c = row.split(",")[:3]
                out.append((d, float(o), float(c)))
            return out
    if last_exc is not None:          # 两个前缀都没拿到响应才算失败
        raise last_exc
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少只股票（0=全部）")
    ap.add_argument("--dry-run", action="store_true")
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
    codes = list(pending)
    if args.limit:
        codes = codes[:args.limit]
    n_events = sum(len(pending[c]) for c in codes)
    print(f"待补次日开盘：{len(codes)} 只股票 / {n_events} 个事件")
    if args.dry_run or not codes:
        return 0

    now = common.now_iso()
    n_done = n_wait = n_fail = 0
    for i, code in enumerate(codes, 1):
        beg = min(d for _, d in pending[code]).replace("-", "")
        try:
            bars = fetch_kline(code, beg)
        except Exception as exc:
            n_fail += 1
            print(f"⚠️ {code} K线抓取失败（明日重试）：{exc}", file=sys.stderr)
            continue
        by_date = {d: (o, c) for d, o, c in bars}
        dates = [d for d, _, _ in bars]
        rows = []
        for eid, d in pending[code]:
            if d not in by_date:
                n_wait += 1           # K线里没有事件日（极端：当日停牌口径差异），跳过
                continue
            nxt = next((x for x in dates if x > d), None)
            if nxt is None:
                n_wait += 1           # 事件后尚无K线（长停牌），复牌后自动补
                continue
            close = by_date[d][1]
            nopen = by_date[nxt][0]
            if close <= 0:
                n_wait += 1
                continue
            rows.append((eid, nxt, close, nopen,
                         round((nopen / close - 1) * 100, 3), now))
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO event_next_open VALUES (?,?,?,?,?,?)", rows)
            conn.commit()
            n_done += len(rows)
        if i % 200 == 0:
            print(f"  进度 {i}/{len(codes)}（已补 {n_done}）")
        time.sleep(SLEEP_BASE + random.random() * 0.15)

    print(f"✅ 次日开盘补齐 {n_done} 个事件（待复牌/无K线 {n_wait}，失败 {n_fail} 只）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
