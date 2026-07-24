#!/usr/bin/env python3
"""
fetch_company_reports.py — 从巨潮资讯公开公告接口抓取最新年度报告并提取文本。

只建立官方证据缓存，不直接生成正式主营标签。PDF/文本放在 data/report_cache/
（已 gitignore），元数据写 company_reports。

用法：
  python3 scripts/fetch_company_reports.py --days 1 --limit 10
  python3 scripts/fetch_company_reports.py --codes 002173,600962
  python3 scripts/fetch_company_reports.py --codes 002173 --refresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

import common

STOCK_LIST_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_ROOT = "https://static.cninfo.com.cn/"
CACHE_DIR = common.REPO_ROOT / "data" / "report_cache"
STOCK_CACHE = CACHE_DIR / "cninfo_stock.json"
USER_AGENT = "Mozilla/5.0 (compatible; zt-kg/1.0; public-disclosure-research)"


def request_bytes(url: str, data: bytes | None = None,
                  headers: dict | None = None, retries: int = 3) -> bytes:
    merged = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.cninfo.com.cn/",
    }
    merged.update(headers or {})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=merged)
            with urllib.request.urlopen(req, timeout=35) as resp:
                return resp.read()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def load_stock_index(refresh: bool = False) -> dict[str, dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stale = (
        not STOCK_CACHE.exists()
        or time.time() - STOCK_CACHE.stat().st_mtime > 7 * 86400
    )
    if refresh or stale:
        STOCK_CACHE.write_bytes(request_bytes(STOCK_LIST_URL))
    raw = json.loads(STOCK_CACHE.read_text(encoding="utf-8"))
    return {
        row["code"]: row
        for row in raw.get("stockList", [])
        if row.get("category") == "A股"
    }


def report_year_from_title(title: str) -> int | None:
    match = re.search(r"(20\d{2})年年度报告", title)
    return int(match.group(1)) if match else None


def is_full_annual_report(title: str) -> bool:
    if "年度报告" not in title:
        return False
    return not any(word in title for word in (
        "摘要", "英文", "取消", "问询", "审核", "说明会", "更正公告",
    ))


def query_latest_report(code: str, org_id: str) -> dict | None:
    is_sh = code.startswith("6")
    current_year = date.today().year
    form = {
        "pageNum": "1",
        "pageSize": "30",
        "column": "sse" if is_sh else "szse",
        "tabName": "fulltext",
        "plate": "sh" if is_sh else "sz",
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": "category_ndbg_szsh",
        "trade": "",
        "seDate": f"{current_year - 3}-01-01~{current_year}-12-31",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    payload = urllib.parse.urlencode(form).encode()
    body = request_bytes(
        QUERY_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    raw = json.loads(body.decode("utf-8"))
    candidates = [
        item for item in raw.get("announcements") or []
        if is_full_annual_report(item.get("announcementTitle") or "")
        and report_year_from_title(item.get("announcementTitle") or "")
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            report_year_from_title(item.get("announcementTitle") or "") or 0,
            item.get("announcementTime") or 0,
        ),
        reverse=True,
    )
    return candidates[0]


def extract_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    chunks = []
    for page_num, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            chunks.append(f"\n===== PDF第{page_num}页 =====\n{text}")
    return "".join(chunks)


def select_codes(conn, args) -> list[str]:
    if args.codes:
        return list(dict.fromkeys(re.findall(r"\d{6}", args.codes)))
    dates = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM limit_up_events "
            "ORDER BY trade_date DESC LIMIT ?", (args.days,)
        )
    ]
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    return [
        row[0] for row in conn.execute(
            f"SELECT DISTINCT code FROM limit_up_events "
            f"WHERE pool='zt' AND trade_date IN ({placeholders}) ORDER BY code",
            dates,
        )
    ]


def save_report(conn, code: str, item: dict, refresh: bool) -> tuple[int, str]:
    title = item["announcementTitle"]
    year = report_year_from_title(title)
    assert year is not None
    existing = conn.execute(
        "SELECT status,text_path FROM company_reports "
        "WHERE code=? AND report_year=?", (code, year)
    ).fetchone()
    if existing and existing[0] == "extracted" and not refresh:
        text_path = common.REPO_ROOT / (existing[1] or "")
        if text_path.exists():
            return year, "cached"

    report_dir = CACHE_DIR / code
    report_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = report_dir / f"{year}.pdf"
    text_path = report_dir / f"{year}.txt"
    url = urllib.parse.urljoin(STATIC_ROOT, item["adjunctUrl"])
    try:
        pdf_bytes = request_bytes(url)
        pdf_path.write_bytes(pdf_bytes)
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        text = extract_pdf_text(pdf_path)
        text_path.write_text(text, encoding="utf-8")
        status, error = "extracted", None
    except Exception as exc:
        digest = None
        status, error = "error", str(exc)[:500]

    timestamp_ms = item.get("announcementTime")
    published = (
        datetime.fromtimestamp(timestamp_ms / 1000).date().isoformat()
        if timestamp_ms else None
    )
    now = common.now_iso()
    conn.execute(
        """
        INSERT INTO company_reports(
          code,report_year,title,url,published_at,announcement_id,sha256,
          pdf_path,text_path,status,error_message,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code,report_year) DO UPDATE SET
          title=excluded.title,url=excluded.url,published_at=excluded.published_at,
          announcement_id=excluded.announcement_id,sha256=excluded.sha256,
          pdf_path=excluded.pdf_path,text_path=excluded.text_path,
          status=excluded.status,error_message=excluded.error_message,
          updated_at=excluded.updated_at
        """,
        (
            code, year, title, url, published, item.get("announcementId"), digest,
            str(pdf_path.relative_to(common.REPO_ROOT)),
            str(text_path.relative_to(common.REPO_ROOT)),
            status, error, now, now,
        ),
    )
    conn.commit()
    if error:
        raise RuntimeError(error)
    return year, "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1,
                        help="未指定codes时，处理最近N个交易日的涨停股")
    parser.add_argument("--codes", help="逗号分隔的股票代码")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    conn = common.open_db()
    codes = select_codes(conn, args)
    if args.limit:
        codes = codes[:args.limit]
    stock_index = load_stock_index(args.refresh)
    ok = cached = errors = 0
    for index, code in enumerate(codes, 1):
        stock = stock_index.get(code)
        if not stock:
            print(f"⚠️ {code}: 巨潮证券列表无记录")
            errors += 1
            continue
        try:
            item = query_latest_report(code, stock["orgId"])
            if not item:
                print(f"⚠️ {code}: 未找到年度报告")
                errors += 1
                continue
            year, result = save_report(conn, code, item, args.refresh)
            cached += result == "cached"
            ok += result == "downloaded"
            print(f"✅ [{index}/{len(codes)}] {code}: {year}年报 {result}")
        except Exception as exc:
            errors += 1
            print(f"❌ [{index}/{len(codes)}] {code}: {exc}", file=sys.stderr)
        if index < len(codes):
            time.sleep(0.35)
    print(f"完成：下载/提取 {ok}，缓存命中 {cached}，失败 {errors}")
    return 1 if errors and not (ok or cached) else 0


if __name__ == "__main__":
    sys.exit(main())
