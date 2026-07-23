#!/usr/bin/env python3
"""
backfill.py — 一次性回补同花顺涨停池历史数据（约1年滚动窗口内）。

- 从今天倒推 --span 个自然日（默认370），跳过周六日；节假日靠空响应记 empty
- 断点续传：fetch_log 中 status in (ok, empty) 的日期直接跳过，重跑天然幂等
- 限速：每请求 sleep 1.5~2.5s；单日失败重试3次(5s/15s/30s)后记 error 继续
- 越过滚动窗口边界（DateOutOfRangeError）即终止，打印实际覆盖起点

用法：
  python3 scripts/backfill.py --dry-run --days 3     # 只抓不落库，验证解析
  python3 scripts/backfill.py                        # 全量回补
"""

import argparse
import sys
import time
from datetime import date, timedelta

import common


def dry_run(n_days: int) -> int:
    """抓最近 n 个自然日（含周末以观察空响应），打印解析结果，不落库。"""
    d = date.today()
    shown = 0
    while shown < n_days:
        ds = d.strftime("%Y%m%d")
        try:
            rows, stats = common.fetch_pool(ds)
        except common.DateOutOfRangeError as e:
            print(f"{ds}: 超出窗口 {e}")
            return 0
        print(f"\n=== {ds}: {len(rows)} 条 | 市场统计 {stats} ===")
        for r in rows[:5]:
            lb = common.decode_high_days(r.get("high_days_value"))
            print(f"  {r.get('code')} {r.get('name')} [{common.board_of(str(r.get('code')))}]"
                  f" {r.get('high_days')}(连板数={lb}) {r.get('limit_up_type')}"
                  f" 炸板{r.get('open_num')}次 | {r.get('reason_type')}"
                  f" → 标签{common.split_tags(r.get('reason_type'))}")
        shown += 1
        d -= timedelta(days=1)
        common.polite_sleep()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--span", type=int, default=370, help="从今天倒推的自然日数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=3, help="dry-run 抓几天")
    args = ap.parse_args()

    if args.dry_run:
        return dry_run(args.days)

    conn = common.open_db()
    alias_map = common.load_aliases()
    done = {(r[0], r[1]) for r in conn.execute(
        "SELECT trade_date, source FROM fetch_log "
        "WHERE source IN ('ths','ths:touch') AND status IN ('ok','empty')")}

    # 每个目标 = (日期, 池)：涨停池 source='ths'，炸板池 source='ths:touch'
    targets = []
    d = date.today()
    for _ in range(args.span):
        if d.weekday() < 5:
            for pool, src in (("zt", "ths"), ("touch", "ths:touch")):
                if (d.isoformat(), src) not in done:
                    targets.append((d, pool, src))
        d -= timedelta(days=1)
    print(f"待抓取 {len(targets)} 个(日期,池)（已完成 {len(done)} 个，跳过）")

    n_ok = n_empty = n_err = 0
    hit_boundary = None
    for i, (day, pool, src) in enumerate(targets):
        ds_api = day.strftime("%Y%m%d")
        ds_iso = day.isoformat()
        rows = stats = None
        err = None
        for attempt, backoff in enumerate((0, 5, 15, 30)):
            if backoff:
                time.sleep(backoff)
            try:
                rows, stats = common.fetch_pool(ds_api, pool=pool)
                err = None
                break
            except common.DateOutOfRangeError:
                hit_boundary = ds_iso
                break
            except Exception as e:
                err = e
        if hit_boundary:
            common.log_fetch(conn, ds_iso, "out_of_range", source=src)
            print(f"⛔ {ds_iso} 越过滚动窗口边界，终止回补")
            break
        if err is not None:
            common.log_fetch(conn, ds_iso, "error", error_msg=str(err)[:500], source=src)
            print(f"❌ {ds_iso}[{pool}]: {err}")
            n_err += 1
        elif not rows:
            common.log_fetch(conn, ds_iso, "empty", record_count=0, source=src)
            n_empty += 1
        else:
            common.upsert_events(conn, ds_iso, rows, alias_map, pool=pool)
            if pool == "zt":
                common.upsert_day_stats(conn, ds_iso, stats)
            common.log_fetch(conn, ds_iso, "ok", record_count=len(rows), source=src)
            n_ok += 1
            print(f"✅ {ds_iso}[{pool}]: {len(rows)} 条  [{i + 1}/{len(targets)}]")
        common.polite_sleep()

    # 收尾摘要
    print("\n===== 回补摘要 =====")
    print(f"ok={n_ok}  empty(节假日)={n_empty}  error={n_err}"
          + (f"  边界={hit_boundary}" if hit_boundary else ""))
    total, days = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT trade_date) FROM limit_up_events WHERE pool='zt'").fetchone()
    n_touch = conn.execute(
        "SELECT COUNT(*) FROM limit_up_events WHERE pool='touch'").fetchone()[0]
    lo, hi = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM limit_up_events").fetchone()
    print(f"库内涨停 {total} 条 + 炸板 {n_touch} 条，覆盖 {days} 个交易日（{lo} ~ {hi}），"
          f"日均涨停 {total / days:.1f} 只" if days else "库内无数据")
    print("\nTop20 概念：")
    for name, cnt in conn.execute(
            "SELECT c.name, COUNT(*) n FROM event_concepts ec JOIN concepts c ON c.id=ec.concept_id "
            "GROUP BY c.id ORDER BY n DESC LIMIT 20"):
        print(f"  {name}: {cnt}")
    fails = [r[0] for r in conn.execute(
        "SELECT trade_date FROM fetch_log WHERE status='error' ORDER BY trade_date")]
    if fails:
        print(f"\n⚠️ 失败日期（重跑本脚本自动补）: {', '.join(fails)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
