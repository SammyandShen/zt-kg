#!/usr/bin/env python3
"""fetch_em_boards.py — 热点雷达信号源 S1：东财概念板块涨幅榜。

东财自建概念词表与同花顺涨停原因完全独立，是"市场在炒什么"的第二观测口。
每日收盘后抓全量概念板块（clist API 免鉴权），涨幅榜前 board_top_n 且
上涨家数 ≥ board_min_up 的板块写 theme_signals(source='em_board')。

只做信号留痕，不写归因/热力；提名收敛在 discover_hotspots.py。
signal_date 取库内最新交易日（本脚本在 fetch_daily 之后跑）；若与自然日
不一致（节假日跑班次），板块行情本来就是最后一个交易日的收盘态，口径自洽。

用法：python3 scripts/fetch_em_boards.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import date

import common

# push2 主域偶发拒连，delay 域实测稳定（收盘后数据无延迟差异）
HOSTS = ["push2delay.eastmoney.com", "82.push2.eastmoney.com",
         "push2.eastmoney.com"]
FIELDS = "f3,f12,f14,f104,f105,f128"   # 涨幅/板块代码/名称/上涨家数/下跌家数/领涨股
PAGE_SIZE = 100
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}


def fetch_boards() -> list[dict]:
    """全量概念板块，按涨幅降序。任一 host 成功即返回。"""
    last_err: Exception | None = None
    for host in HOSTS:
        try:
            rows: list[dict] = []
            page = 1
            while True:
                qs = urllib.parse.urlencode({
                    "pn": page, "pz": PAGE_SIZE, "po": 1, "np": 1, "fltt": 2,
                    "fid": "f3", "fs": "m:90+t:3", "fields": FIELDS,
                })
                req = urllib.request.Request(
                    f"https://{host}/api/qt/clist/get?{qs}", headers=HEADERS)
                payload = json.loads(
                    urllib.request.urlopen(req, timeout=20).read())
                data = payload.get("data") or {}
                diff = data.get("diff") or []
                rows.extend(diff)
                total = data.get("total") or 0
                if page * PAGE_SIZE >= total or not diff:
                    return rows
                page += 1
                common.polite_sleep(0.3)
        except Exception as e:                      # noqa: BLE001 换 host 重试
            last_err = e
            continue
    raise RuntimeError(f"东财概念板块榜全部 host 失败：{last_err}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="库内最新交易日≠今日时仍写入（盘中手动跑=以当前盘面"
                         "近似该日收盘态；班次内不需要此参数）")
    args = ap.parse_args()

    conn = common.open_db()
    cfg_path = common.REPO_ROOT / "data" / "semantic_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")).get("discovery", {})
    top_n = cfg.get("board_top_n", 20)
    min_up = cfg.get("board_min_up", 10)

    row = conn.execute(
        "SELECT MAX(trade_date) FROM limit_up_events WHERE pool='zt'").fetchone()
    signal_date = row[0] if row else None
    if not signal_date:
        print("库内无交易日，跳过")
        return 0
    if signal_date != date.today().isoformat():
        # 盘中手动跑会把今日盘中数据错记到旧交易日名下；班次在 fetch_daily
        # 之后跑（signal_date=今日）不会走到这里。周末/节假日跑班次时板块
        # 行情停在上一收盘态，跳过也不丢数据（该日已在收盘当天记过）。
        if not args.force:
            print(f"⏭️ 库内最新交易日 {signal_date} ≠ 今日，跳过"
                  f"（防盘中数据错记；确需写入用 --force）")
            return 0
        print(f"⚠️ --force：以当前盘面近似 {signal_date} 收盘态记账")

    boards = fetch_boards()
    if not boards:
        print("⚠️ 东财榜返回空，跳过")
        return 0
    now = common.now_iso()
    picked = 0
    for rank, b in enumerate(boards[:top_n], 1):
        name = str(b.get("f14") or "").strip()
        up = b.get("f104") or 0
        if not name or up < min_up:
            continue
        strength = round((top_n + 1 - rank) / top_n, 3)
        detail = json.dumps({
            "rank": rank, "chg": b.get("f3"), "up": up,
            "down": b.get("f105"), "lead": b.get("f128"),
        }, ensure_ascii=False)
        if not args.dry_run:
            conn.execute(
                "INSERT INTO theme_signals(signal_date,source,term,strength,"
                "detail,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(signal_date,source,term) DO UPDATE SET "
                "strength=excluded.strength, detail=excluded.detail",
                (signal_date, "em_board", name, strength, detail, now))
        picked += 1
    if not args.dry_run:
        conn.commit()
    print(f"✅ 东财概念榜 {len(boards)} 个板块，{signal_date} 入选信号 {picked} 条"
          f"（前{top_n}名且上涨≥{min_up}家）"
          + ("（dry-run 未写库）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
