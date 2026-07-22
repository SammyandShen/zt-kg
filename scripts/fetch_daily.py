#!/usr/bin/env python3
"""
fetch_daily.py — 每日增量：抓当日涨停池入库（launchd 收盘后调用，可安全重跑）。

- 空响应（节假日/周末）→ 记 empty，exit 0
- 失败重试3次（5s/15s/30s）后 exit 1（不留脏数据，fetch_log 记 error）
"""

import sys
import time
from datetime import date

import common


def main() -> int:
    today = date.today()
    ds_api, ds_iso = today.strftime("%Y%m%d"), today.isoformat()
    conn = common.open_db()

    err = None
    rows = stats = None
    for backoff in (0, 5, 15, 30):
        if backoff:
            time.sleep(backoff)
        try:
            rows, stats = common.fetch_pool(ds_api)
            err = None
            break
        except Exception as e:
            err = e
    if err is not None:
        common.log_fetch(conn, ds_iso, "error", error_msg=str(err)[:500])
        print(f"❌ {ds_iso} 抓取失败: {err}", file=sys.stderr)
        return 1
    if not rows:
        common.log_fetch(conn, ds_iso, "empty", record_count=0)
        print(f"ℹ️ {ds_iso} 无涨停数据（非交易日）")
        return 0

    common.upsert_events(conn, ds_iso, rows)
    common.upsert_day_stats(conn, ds_iso, stats)
    common.log_fetch(conn, ds_iso, "ok", record_count=len(rows))
    print(f"✅ {ds_iso}: {len(rows)} 只涨停入库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
