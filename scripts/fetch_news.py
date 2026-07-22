#!/usr/bin/env python3
"""
fetch_news.py — 为涨停事件关联新闻（东财搜索接口，按股票名逐只查询）。

关联规则：取发布时间在 [涨停日-2天, 涨停日+1天] 窗口内的新闻，标题含股票名的
优先，每次查询最多存3条（URL去重累积）。

幂等与成熟度：news_log 记录上次抓取时间。若上次抓取早于「涨停日+2天」，说明
新闻窗口尚未关闭（当晚/次日的解读稿可能还没发），会自动重查补入增量；
窗口关闭后抓过的即为终态，永久跳过。因此 run-daily 用 --days 2 即可让
昨日记录在今天自动补齐晚间新闻。

用法：
  python3 scripts/fetch_news.py               # 最新交易日
  python3 scripts/fetch_news.py --days 5      # 最近5个交易日
  python3 scripts/fetch_news.py --date 2026-07-21
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

import common

SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"
MAX_PER_STOCK = 3
SLEEP_SEC = 0.35
# 榜单/基金持仓类汇总文章对个股归因意义不大，标题命中即降权
NOISE_WORDS = ("龙虎榜", "营业部", "板块复盘", "涨停复盘", "收评", "午评",
               "附股", "短线走稳", "站上", "净值增长率", "季度利润", "重仓股",
               "融资余额", "大宗交易", "解禁", "只股", "指数", "ETF")


def search_news(keyword: str) -> list[dict]:
    param = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": 1, "pageSize": 30,
                                       "preTag": "", "postTag": ""}},
    }
    url = SEARCH_API + "?cb=cb&param=" + urllib.parse.quote(
        json.dumps(param, ensure_ascii=False))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    txt = urllib.request.urlopen(req, timeout=15).read().decode()
    payload = json.loads(txt[txt.index("(") + 1:txt.rindex(")")])
    return (payload.get("result") or {}).get("cmsArticleWebOld") or []


def pick_news(arts: list[dict], name: str, trade_date: str) -> list[dict]:
    d = date.fromisoformat(trade_date)
    lo, hi = (d - timedelta(days=2)).isoformat(), (d + timedelta(days=1)).isoformat()
    cands = []
    for a in arts:
        pub = (a.get("date") or "")[:10]
        if not (lo <= pub <= hi):
            continue
        title = (a.get("title") or "").strip()
        if not title or not a.get("url"):
            continue
        score = (3 if name in title else 0) \
            - (2 if any(w in title for w in NOISE_WORDS) else 0) \
            + (1 if (a.get("content") or "").strip().startswith(name) else 0)
        cands.append((score, a))
    # 分数高优先，同分新的优先（稳定排序两段式）
    cands.sort(key=lambda x: x[1].get("date", ""), reverse=True)
    cands.sort(key=lambda x: x[0], reverse=True)
    # 有"标题点名"的优质新闻时，剔除榜单类噪音
    if cands and cands[0][0] >= 3:
        cands = [c for c in cands if c[0] >= 1]
    return [a for _, a in cands[:MAX_PER_STOCK]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--date")
    args = ap.parse_args()

    conn = common.open_db()
    if args.date:
        dates = [args.date]
    else:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM limit_up_events "
            "ORDER BY trade_date DESC LIMIT ?", (args.days,))]

    todo = []
    for d in dates:
        for code, name in conn.execute(
                "SELECT DISTINCT e.code, e.name FROM limit_up_events e "
                "LEFT JOIN news_log l ON l.code=e.code AND l.trade_date=e.trade_date "
                "WHERE e.trade_date=? AND (l.code IS NULL "
                "  OR l.fetched_at < datetime(e.trade_date, '+2 day')) "
                "ORDER BY e.code", (d,)):
            todo.append((d, code, name))
    if not todo:
        print("无待抓取（全部已有 news_log 记录）")
        return 0
    print(f"待关联 {len(todo)} 个 涨停×个股（{dates[-1]} ~ {dates[0]}）")

    n_ok = n_err = n_news = 0
    for i, (d, code, name) in enumerate(todo, 1):
        try:
            arts = search_news(name)
            picked = pick_news(arts, name, d)
            with conn:
                for a in picked:
                    conn.execute(
                        "INSERT OR IGNORE INTO news(code, trade_date, title, url, "
                        "source, pub_time, snippet, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                        (code, d, a["title"].strip(), a["url"],
                         a.get("mediaName"), a.get("date"),
                         (a.get("content") or "").strip()[:200], common.now_iso()))
                conn.execute(
                    "INSERT OR REPLACE INTO news_log(code, trade_date, fetched_at, n_found) "
                    "VALUES(?,?,?,?)", (code, d, common.now_iso(), len(picked)))
            n_ok += 1
            n_news += len(picked)
        except Exception as e:
            n_err += 1
            if n_err <= 3:
                print(f"  ⚠️ {name}({code}) {d}: {e}", file=sys.stderr)
        if i % 25 == 0:
            print(f"  [{i}/{len(todo)}] 已存 {n_news} 条新闻")
        time.sleep(SLEEP_SEC)

    print(f"完成：查询 {n_ok} 只（失败{n_err}），入库新闻 {n_news} 条")
    return 0 if n_err < len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
