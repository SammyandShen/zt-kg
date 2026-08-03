#!/usr/bin/env python3
"""
fetch_interactive_qa.py — 从深交所互动易(irm.cninfo.com.cn)抓公司已回复的问答，
用保守规则抽取"供应链/客户配套"关系，补第③层（上下游映射股）的数据源。

设计约束（零人工干预原则下的保险丝）：
- 只看公司自己的回复(attachedContent)，提问内容一律不作为证据
- 只认词典内已存在的 product/theme/sector 标签（不发明新词）
- 回复句必须同时含标签和供应动词，含否定词的句子整句丢弃
- 产出只写 business_fact_candidates(relation=supply_chain, conf=0.55)：
  低于 0.7 晋升线，由 rebuild 以 status='candidate' 落入事实表→仅进③层
  观察名单与题材候选池，永不进产业热力、不冒充主营
- 覆盖边界：互动易主要覆盖深市；沪市 e互动 是另一接口，暂不接

用法：
  python3 scripts/fetch_interactive_qa.py --days 2 --limit 40
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

import common

API = "http://irm.cninfo.com.cn/newircs/index/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")
NEGATION = re.compile(
    r"不涉及|没有|暂无|暂未|尚未|不存在|未向|未与|未有|不构成|无直接")
SENT_SPLIT = re.compile(r"[。；！？\n]")


def directional_patterns(tag: str) -> list[re.Pattern]:
    """方向性供应句式：必须能读出"谁向谁供什么"，纯共现不算。"""
    t = re.escape(tag)
    return [
        # 公司产品/方案 供应|应用于 → 标签领域
        re.compile(rf"(产品|方案|设备|材料|器件|组件|模组|服务)"
                   rf"[^。；]{{0,20}}(供应|供货|应用于|已用于|批量交付|送样|中标)"
                   rf"[^。；]{{0,15}}{t}"),
        # 为|向 标签客户 供应|配套
        re.compile(rf"[为向][^。；]{{0,12}}{t}[^。；]{{0,15}}"
                   rf"(供应|供货|提供|配套|交付|批量)"),
        # 标签领域/产业链 的客户|订单|出货
        re.compile(rf"{t}(领域|行业|产业|产业链)[^。；]{{0,10}}"
                   rf"(客户|订单|批量|出货|供应|应用)"),
    ]


def load_supply_tags() -> list[str]:
    """词典内 active 的 product/theme/sector 标签（2-8字），作为唯一可认对象。"""
    meta = json.loads(
        (common.REPO_ROOT / "data" / "tag_meta.json").read_text(encoding="utf-8"))
    return [nm for nm, m in meta.items()
            if not nm.startswith("$") and isinstance(m, dict)
            and m.get("status") == "active"
            and m.get("type") in ("product", "theme", "sector")
            and 2 <= len(nm) <= 8]


def fetch_qa(name: str) -> list[dict]:
    body = urllib.parse.urlencode(
        {"pageNo": 1, "pageSize": 30, "keyWord": name}).encode()
    req = urllib.request.Request(
        API, data=body, headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8")).get("results", [])


def extract_relations(reply: str, tags: list[str]) -> list[tuple[str, str]]:
    """回复文本 → [(标签, 依据句)]。只认方向性供应句式，含否定整句丢弃。"""
    out = []
    for sent in SENT_SPLIT.split(reply):
        sent = sent.strip()
        if not sent or len(sent) > 200 or NEGATION.search(sent):
            continue
        for tag in tags:
            if tag in sent and any(p.search(sent)
                                   for p in directional_patterns(tag)):
                out.append((tag, sent))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    conn = common.open_db()
    tags = load_supply_tags()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up_events "
        "ORDER BY trade_date DESC LIMIT ?", (args.days,))]
    if not dates:
        print("ℹ️ 库内无交易日")
        return 0
    stocks = conn.execute(
        "SELECT DISTINCT e.code, s.name FROM limit_up_events e "
        "JOIN stocks s ON s.code=e.code "
        f"WHERE e.trade_date IN ({','.join('?' * len(dates))}) AND e.pool='zt' "
        "AND e.code NOT LIKE '6%' "        # 互动易只覆盖深市
        "ORDER BY e.code LIMIT ?", (*dates, args.limit)).fetchall()
    from rebuild_semantic_layer import upsert_evidence
    now = common.now_iso()
    n_new = errors = 0
    for code, name in stocks:
        try:
            results = fetch_qa(name)
        except Exception as exc:
            errors += 1
            print(f"❌ {code} {name}: {exc}", file=sys.stderr)
            time.sleep(1)
            continue
        found = 0
        for qa in results:
            if str(qa.get("stockCode")) != code:
                continue
            # attachedContent 非空 = 公司已回复（qaStatus 字段含义不稳定，不作依据）
            reply = (qa.get("attachedContent") or "").strip()
            if not reply:
                continue
            pub = qa.get("attachedPubDate") or qa.get("pubDate") or 0
            pub_iso = time.strftime("%Y-%m-%d", time.localtime(int(pub) / 1000)) \
                if pub else ""
            year = int(pub_iso[:4]) if pub_iso else 0
            for tag, sent in extract_relations(reply, tags)[:5 - found]:
                evidence_key = f"irm_qa:{code}:{qa.get('indexId')}:{tag}"
                upsert_evidence(conn, {
                    "evidence_key": evidence_key,
                    "evidence_type": "qa",
                    "source_name": "互动易",
                    "title": f"{name}互动易回复（{pub_iso}）",
                    "url": "https://irm.cninfo.com.cn/",
                    "published_at": pub_iso,
                    "subject_status": "direct",
                    "claim": sent[:400],
                    "reliability": 0.7,
                }, code=code, name=name)
                cur = conn.execute(
                    """
                    INSERT INTO business_fact_candidates(
                      code,report_year,tag_name,fact_type,relation_type,maturity,
                      status,confidence,summary,evidence_key,extractor,
                      created_at,updated_at
                    ) VALUES(?,?,?,?,'supply_chain','unknown','candidate',
                             0.55,?,?,?,?,?)
                    ON CONFLICT(code,report_year,tag_name,relation_type)
                    DO NOTHING
                    """,
                    (code, year, tag, "product",
                     f"互动易回复确认与{tag}存在供应/客户关系"[:300],
                     evidence_key, "irm-rule-v1", now, now))
                if cur.rowcount:
                    n_new += 1
                    found += 1
            if found >= 5:
                break
        conn.commit()
        time.sleep(1.2)
    print(f"✅ 互动易供应链抽取：{len(stocks)} 只深市涨停股，"
          f"新增 supply_chain 候选 {n_new} 条"
          + (f"，失败 {errors} 只" if errors else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
