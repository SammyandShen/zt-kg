#!/usr/bin/env python3
"""discover_news_terms.py — 热点雷达 S3+S4：新闻标题 / 财经快讯 RSS 提词。

S3 news_terms：库内近2交易日涨停关联新闻标题（规则去噪后）。
S4 flash_rss：外部财经快讯 RSS（华尔街见闻/界面/36氪快讯/钛媒体，
  toutiao-agent 2026-06 实测可用清单；单源失败跳过不阻断）。

两路标题合并成一次 claude CLI(sonnet) 调用，提取"市场炒作题材短语"写
theme_signals；台账 data/hotspot_ledger.json 按 (交易日) 防重，改提示词后
删对应日期条目即可重跑。零人工干预：提词只产信号，转正走分型链。

保险丝：短语 ≤8 字；命中股票名/已退休标签丢弃；LLM 不可用直接跳过（不规则兜底
——标题分词质量差，宁可当天无信号）。

用法：python3 scripts/discover_news_terms.py [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request

import common
from summarize_news import find_claude

LEDGER_PATH = common.REPO_ROOT / "data" / "hotspot_ledger.json"
MODEL = "claude-sonnet-5"
TIMEOUT_SEC = 240

RSS_SOURCES = [
    ("华尔街见闻", "https://dedicated.wallstreetcn.com/rss.xml"),
    ("界面新闻", "https://a.jiemian.com/index.php?m=article&a=rss"),
    ("36氪快讯", "https://36kr.com/feed-newsflash"),
    ("钛媒体", "https://www.tmtpost.com/feed"),
]
# 库内新闻噪声模板（龙虎榜/公告类标题不含题材信息）
NOISE_RE = re.compile(
    r"龙虎榜|异常波动|异动公告|风险提示|澄清|停牌|复牌|连板|涨停收盘|"
    r"天\d+板|收盘价|融资余额|大宗交易|限售|解禁|回购进展|质押")

PROMPT = """你是A股盘面观察员。从下面的新闻/快讯标题中提取当下市场正在交易或
即将发酵的【题材短语】。

规则：
- 只要"炒作对象"：产业方向、产品技术、事件驱动的概念（如 固态电池、算力租赁、
  减肥药、低空经济）。
- 不要：公司名/股票名、指数与大盘描述、业绩/回购/增减持等公司行为词、
  形容词短语、宏观政策泛词（如 降准）除非它直接对应可交易题材。
- 每个短语 ≤8 个汉字，用市场惯用叫法；同义合并；最多 12 个。
- evidence 摘一条命中标题原文（截断可）。

只输出一个 JSON 对象，不要任何其他文字：
{"terms":[{"term":"...","evidence":"标题原文","hot":1到5的热度直觉}]}

===== 标题清单 =====
"""


def gather_news_titles(conn, dates: list[str]) -> list[str]:
    recent = dates[-2:]
    ph = ",".join("?" for _ in recent)
    titles = []
    for (t,) in conn.execute(
            f"SELECT DISTINCT title FROM news WHERE trade_date IN ({ph})",
            recent):
        if t and not NOISE_RE.search(t):
            titles.append(t.strip())
    return titles[:120]


def gather_rss_titles() -> list[str]:
    out: list[str] = []
    for name, url in RSS_SOURCES:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            raw = urllib.request.urlopen(req, timeout=15).read()
            text = raw.decode("utf-8", errors="ignore")
            titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                                text, re.S)
            got = 0
            for t in titles[1:]:               # 首个 <title> 是频道名
                t = re.sub(r"\s+", " ", t).strip()
                if t and len(t) >= 8 and not NOISE_RE.search(t):
                    out.append(t)
                    got += 1
                if got >= 25:
                    break
            print(f"  RSS {name}: {got} 条")
        except Exception as e:                  # noqa: BLE001 单源失败不阻断
            print(f"  ⚠️ RSS {name} 失败：{type(e).__name__}")
        common.polite_sleep(0.3)
    return out


def call_llm(claude_bin: str, payload: str) -> dict:
    r = subprocess.run([claude_bin, "-p", "--model", MODEL],
                       input=PROMPT + payload,
                       capture_output=True, text=True, timeout=TIMEOUT_SEC)
    if r.returncode != 0:
        raise RuntimeError(f"claude 退出码 {r.returncode}: {r.stderr[:200]}")
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise ValueError(f"LLM 输出无 JSON: {r.stdout[:200]}")
    return json.loads(m.group(0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略台账重跑当日")
    args = ap.parse_args()

    conn = common.open_db()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up_events "
        "WHERE pool='zt' ORDER BY trade_date")]
    if not dates:
        print("库内无交易日，跳过")
        return 0
    day = dates[-1]

    ledger = (json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
              if LEDGER_PATH.exists() else {})
    ledger.setdefault("news_terms", {})
    if day in ledger["news_terms"] and not args.force:
        print(f"⏭️ {day} 已提词（台账防重；--force 重跑）")
        return 0

    news_titles = gather_news_titles(conn, dates)
    rss_titles = gather_rss_titles()
    if not news_titles and not rss_titles:
        print("两路标题均空，跳过")
        return 0

    stock_names = {r[0] for r in conn.execute("SELECT name FROM stocks")}
    drops = common.load_drop_tags()
    try:
        claude_bin = find_claude()
        payload = json.dumps({
            "库内涨停关联新闻": news_titles,
            "财经快讯": rss_titles[:80],
        }, ensure_ascii=False, indent=1)
        result = call_llm(claude_bin, payload)
    except Exception as e:                      # noqa: BLE001
        print(f"❌ LLM 提词失败（跳过，不阻断班次）：{e}", file=sys.stderr)
        return 0

    news_set = set(news_titles)
    now = common.now_iso()
    n = 0
    accepted: list[dict] = []
    for item in result.get("terms", []):
        term = str(item.get("term") or "").strip()
        if not term or len(term) > 8 or term in stock_names or term in drops:
            continue
        hot = min(5, max(1, int(item.get("hot") or 1)))
        source = ("news_terms" if str(item.get("evidence") or "") in news_set
                  else "flash_rss")
        strength = round(0.3 + hot * 0.1, 2)     # 0.4-0.8：热度直觉映射
        detail = json.dumps(
            {"evidence": str(item.get("evidence") or "")[:80], "hot": hot},
            ensure_ascii=False)
        if not args.dry_run:
            conn.execute(
                "INSERT INTO theme_signals(signal_date,source,term,strength,"
                "detail,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(signal_date,source,term) DO UPDATE SET "
                "strength=excluded.strength, detail=excluded.detail",
                (day, source, term, strength, detail, now))
        accepted.append({"term": term, "source": source, "hot": hot})
        n += 1
    if not args.dry_run:
        conn.commit()
        ledger["news_terms"][day] = {"at": now, "terms": accepted}
        LEDGER_PATH.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    print(f"✅ 提词 {n} 条（标题：库内{len(news_titles)}+快讯{len(rss_titles)}）："
          + "、".join(a["term"] for a in accepted)
          + ("（dry-run 未写库）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
