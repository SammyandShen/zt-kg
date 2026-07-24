#!/usr/bin/env python3
"""
fetch_event_announcements.py — 为近期涨停事件补充巨潮资讯官方公告标题证据。

只抓取涨停日前2天至后2天内、标题命中交易催化关键词的公告。公告先作为
event_evidence 的“上下文证据”保存；只有标题明确点名具体题材时，后续语义重建
才会把它绑定到 event_theme_evidence。脚本不会自动核实题材。

用法：
  python3 scripts/fetch_event_announcements.py --days 2
  python3 scripts/fetch_event_announcements.py --date 2026-07-23
  python3 scripts/fetch_event_announcements.py --codes 002173,600962 --days 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

import common
from fetch_company_reports import (
    QUERY_URL,
    STATIC_ROOT,
    load_stock_index,
    request_bytes,
)

CATALYST_KEYWORDS = (
    "收购", "并购", "重组", "购买资产", "资产置换", "控制权", "股权转让",
    "中标", "合同", "订单", "战略合作", "框架协议", "签署协议",
    "业绩预告", "业绩快报", "预增", "扭亏", "同比增长",
    "获得批复", "注册证", "获准", "取得许可", "通过认证",
    "投产", "扩产", "建设项目", "研发进展", "临床试验", "专利",
    "产品价格", "调价", "涨价", "参股公司", "对外投资",
)
NOISE_KEYWORDS = (
    "年度报告", "半年度报告", "季度报告", "社会责任报告", "审计报告",
    "股东大会", "董事会决议", "监事会决议", "独立董事", "法律意见书",
    "权益分派", "分红派息", "募集资金存放", "投资者关系活动记录表",
    "签字注册会计师",
)
THIRD_PARTY_MARKERS = (
    "会计师事务所", "律师事务所", "证券股份有限公司关于", "资产评估有限公司",
    "独立财务顾问", "专项核查意见", "法律意见书", "承诺函",
)
MAX_PER_EVENT = 5
SOURCE_PRIORITY = {"ths": 0, "wencai": 1, "kpl": 2}


def clean_title(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\s+", " ", value).strip()


def relevant_title(title: str) -> bool:
    return (
        any(keyword in title for keyword in CATALYST_KEYWORDS)
        and not any(keyword in title for keyword in NOISE_KEYWORDS)
    )


def item_published_date(item: dict) -> str:
    timestamp_ms = item.get("announcementTime")
    return (
        datetime.fromtimestamp(timestamp_ms / 1000).date().isoformat()
        if timestamp_ms else ""
    )


def announcement_rank(item: dict, issuer_name: str) -> tuple:
    title = item["_clean_title"]
    score = 0
    if issuer_name and title.startswith(issuer_name):
        score += 12
    if "恢复审核" in title or "获得批复" in title or "获准" in title:
        score += 8
    if "收购" in title or "购买资产" in title or "重大资产重组" in title:
        score += 5
    if "中标" in title or "合同" in title or "业绩预告" in title:
        score += 4
    if "回复" in title or "修订说明" in title:
        score -= 3
    if any(marker in title for marker in THIRD_PARTY_MARKERS):
        score -= 12
    return score, item.get("announcementTime") or 0


def query_announcements(code: str, org_id: str, issuer_name: str,
                        start: str, end: str) -> list[dict]:
    is_sh = code.startswith("6")
    form = {
        "pageNum": "1",
        "pageSize": "100",
        "column": "sse" if is_sh else "szse",
        "tabName": "fulltext",
        "plate": "sh" if is_sh else "sz",
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "time",
        "sortType": "desc",
        "isHLtitle": "true",
    }
    body = request_bytes(
        QUERY_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    raw = json.loads(body.decode("utf-8"))
    rows = raw.get("announcements") or []
    for row in rows:
        row["_clean_title"] = clean_title(row.get("announcementTitle") or "")
    rows = [
        row for row in rows
        if start <= item_published_date(row) <= end
        and relevant_title(row["_clean_title"])
    ]
    rows.sort(key=lambda row: announcement_rank(row, issuer_name), reverse=True)
    return rows[:MAX_PER_EVENT]


def select_events(conn, args) -> list[tuple]:
    if args.date:
        dates = [args.date]
    else:
        dates = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT trade_date FROM limit_up_events "
                "WHERE pool='zt' ORDER BY trade_date DESC LIMIT ?",
                (max(1, args.days),),
            )
        ]
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    params: list[str] = list(dates)
    code_filter = ""
    if args.codes:
        codes = list(dict.fromkeys(re.findall(r"\d{6}", args.codes)))
        if not codes:
            return []
        code_filter = f" AND code IN ({','.join('?' for _ in codes)})"
        params.extend(codes)
    rows = conn.execute(
        f"""
        SELECT id,trade_date,code,name,source
        FROM limit_up_events
        WHERE pool='zt' AND trade_date IN ({placeholders}){code_filter}
        ORDER BY trade_date,code
        """,
        params,
    ).fetchall()
    preferred: dict[tuple[str, str], tuple] = {}
    for event_id, trade_date, code, name, source in rows:
        key = trade_date, code
        current = preferred.get(key)
        if current is None or SOURCE_PRIORITY.get(source, 99) < current[0]:
            preferred[key] = (
                SOURCE_PRIORITY.get(source, 99),
                (event_id, trade_date, code, name),
            )
    return [value[1] for value in preferred.values()]


def save_evidence(conn, event: tuple, item: dict) -> bool:
    event_id, _trade_date, code, name = event
    announcement_id = str(item.get("announcementId") or "")
    if not announcement_id:
        return False
    title = item["_clean_title"]
    published = item_published_date(item) or None
    url = urllib.parse.urljoin(STATIC_ROOT, item.get("adjunctUrl") or "")
    key = f"cninfo_announcement:{announcement_id}"
    now = common.now_iso()
    conn.execute(
        """
        INSERT INTO evidence_items(
          evidence_key,evidence_type,source_name,title,url,published_at,
          subject_code,subject_name,subject_status,claim,reliability,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(evidence_key) DO UPDATE SET
          title=excluded.title,url=excluded.url,published_at=excluded.published_at,
          subject_code=excluded.subject_code,subject_name=excluded.subject_name,
          subject_status=excluded.subject_status,claim=excluded.claim,
          reliability=excluded.reliability
        """,
        (
            key, "announcement", "巨潮资讯", title, url, published,
            code, name, "direct", title, 0.95, now,
        ),
    )
    evidence_id = conn.execute(
        "SELECT id FROM evidence_items WHERE evidence_key=?", (key,)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO event_evidence(event_id,evidence_id,relevance_status,note)
        VALUES(?,?,?,?)
        ON CONFLICT(event_id,evidence_id) DO UPDATE SET
          relevance_status=excluded.relevance_status,note=excluded.note
        """,
        (
            event_id, evidence_id, "context",
            "官方公告标题命中催化关键词；需进一步判断是否解释本次涨停题材",
        ),
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--date")
    parser.add_argument("--codes", help="逗号分隔的股票代码")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    conn = common.open_db()
    events = select_events(conn, args)
    if args.limit:
        events = events[:args.limit]
    if not events:
        print("无待处理涨停事件")
        return 0
    stocks = load_stock_index()
    saved = errors = 0
    for index, event in enumerate(events, 1):
        _event_id, trade_date, code, _name = event
        stock = stocks.get(code)
        if not stock:
            print(f"⚠️ {code}: 巨潮证券列表无记录")
            errors += 1
            continue
        center = datetime.strptime(trade_date, "%Y-%m-%d").date()
        start = (center - timedelta(days=2)).isoformat()
        end = (center + timedelta(days=2)).isoformat()
        try:
            conn.execute(
                """
                DELETE FROM event_evidence
                WHERE event_id=? AND evidence_id IN (
                  SELECT id FROM evidence_items
                  WHERE evidence_key LIKE 'cninfo_announcement:%'
                )
                """,
                (_event_id,),
            )
            rows = query_announcements(
                code, stock["orgId"], stock.get("zwjc") or _name, start, end
            )
            for item in rows:
                saved += save_evidence(conn, event, item)
            conn.commit()
            if rows:
                print(f"✅ [{index}/{len(events)}] {code} {trade_date}: {len(rows)}条相关公告")
        except Exception as exc:
            conn.rollback()
            errors += 1
            print(f"❌ [{index}/{len(events)}] {code} {trade_date}: {exc}", file=sys.stderr)
        if index < len(events):
            time.sleep(0.25)
    conn.execute("""
        DELETE FROM evidence_items
        WHERE evidence_key LIKE 'cninfo_announcement:%'
          AND NOT EXISTS (
            SELECT 1 FROM event_evidence ee WHERE ee.evidence_id=evidence_items.id
          )
          AND NOT EXISTS (
            SELECT 1 FROM event_theme_evidence ete
            WHERE ete.evidence_id=evidence_items.id
          )
    """)
    conn.commit()
    print(f"完成：事件 {len(events)}，写入关联 {saved}，失败 {errors}")
    return 1 if errors == len(events) else 0


if __name__ == "__main__":
    sys.exit(main())
