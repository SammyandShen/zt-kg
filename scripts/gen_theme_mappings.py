#!/usr/bin/env python3
"""
gen_theme_mappings.py — 题材→业务标签映射的自动生成工序（零人工干预）。

映射回答"这个交易题材由哪些客观业务承载"，是 watch 池（有业务未异动的
补涨候选）反查和 link_basis 业务边匹配的依据。

增量设计：台账 data/llm_mapping_ledger.json 记录已判过的业务标签集/题材集，
每次只对新增部分调 sonnet（新业务标签×全部题材 + 新题材×存量标签），
保守规则=只输出直接承载关系，拿不准不映射。生成条目 status=candidate、
source=llm 留痕；人工条目（source=manual）永不改动。

用法：python3 scripts/gen_theme_mappings.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

import common
from summarize_news import find_claude

MAPPING_PATH = common.REPO_ROOT / "data" / "theme_business_mappings.json"
LEDGER_PATH = common.REPO_ROOT / "data" / "llm_mapping_ledger.json"
META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
MODEL = "claude-sonnet-5"
TIMEOUT_SEC = 600
BATCH = 60             # 新目标×全部题材按批调用；238节点一口气调会超时
MAX_CALLS = 12         # 单次运行 LLM 调用预算：约 15-20 分钟，积压靠台账逐日消化

PROMPT = """你是A股题材研究员。下面给出「交易题材」列表和「公司业务标签」列表
（业务标签来自年报核实的公司在营业务；其中带【节点】前缀的是三层业务图谱的
产业/细分聚合节点——映射到它会自动覆盖其下全部产品的公司，粒度合适时优先选它，
而不是逐个映射叶子产品）。

任务：找出所有"业务标签是该题材的直接承载"的映射。规则（必须保守）：
- exact：业务本身就是题材交易的对象（脑电采集设备→脑机接口）
- upstream：业务是题材核心环节的直接上游材料/设备（电极材料→脑机接口）
- downstream：业务是题材的直接下游应用
- 只映射直接关系；"沾边""可能受益"一律不输出。宁缺毋滥。
- 聚合节点必须整体贴合才映射（题材只对应其中一两个产品时，映射叶子不映射节点）。
- 同一业务标签可映射到多个题材，反之亦然。输出节点时不要带【节点】前缀。

只输出 JSON 数组，不要其他文字：
[{"theme":"题材名","business_tag_name":"业务标签名","mapping_type":"exact|upstream|downstream","confidence":0.0到1.0,"rationale":"一句话"}]

交易题材：%s

业务标签：%s
"""


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def active_themes(conn) -> list[str]:
    """映射左侧词表（2026-08-07 扩三型）：active theme 全部 + active 的
    sector/product 中**立过题材轮次**的——被市场实际炒过的产业/产品概念才需要
    反查公司池；587 个产品标签全量进词表会产生几十次低质大 prompt 调用。
    新标签成轮后自动进入词表，增量收敛。
    """
    meta = load_json(META_PATH, {})
    meta.pop("$note", None)
    episode_names = {r[0] for r in conn.execute(
        "SELECT DISTINCT c.name FROM theme_episodes ep "
        "JOIN concepts c ON c.id=ep.concept_id")}
    out = []
    for nm, m in meta.items():
        if m.get("status") != "active":
            continue
        t = m.get("type")
        if t == "theme" or (t in ("sector", "product") and nm in episode_names):
            out.append(nm)
    return sorted(out)


def business_tags(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT tag_name FROM stock_business_facts "
        "WHERE status NOT IN ('rejected','expired')"))


def graph_sector_nodes(conn) -> list[str]:
    """三层图谱的产业/细分节点（有后代产品事实的才算有效映射目标）。"""
    return sorted(r[0] for r in conn.execute("""
        SELECT DISTINCT p.name FROM business_concepts p
        JOIN business_concept_edges e ON e.parent_id=p.id
        WHERE p.concept_type='sector' AND p.status='active'
    """))


def call_llm(claude_bin: str, themes: list[str], tags: list[str]) -> list[dict]:
    if not themes or not tags:
        return []
    prompt = PROMPT % ("、".join(themes), "、".join(tags))
    r = subprocess.run([claude_bin, "-p", "--model", MODEL], input=prompt,
                       capture_output=True, text=True, timeout=TIMEOUT_SEC)
    if r.returncode != 0:
        raise RuntimeError(f"claude 退出码 {r.returncode}: {r.stderr[:200]}")
    m = re.search(r"\[.*\]", r.stdout, re.S)
    return json.loads(m.group(0)) if m else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = common.open_db()
    themes = active_themes(conn)
    node_set = set(graph_sector_nodes(conn))
    tags = sorted(set(business_tags(conn)) | node_set)   # 叶子标签 + 图谱聚合节点
    mark = lambda ts: [("【节点】" + t) if t in node_set else t for t in ts]
    ledger = load_json(LEDGER_PATH, {"judged_tags": [], "judged_themes": []})
    new_tags = sorted(set(tags) - set(ledger["judged_tags"]))
    new_themes = sorted(set(themes) - set(ledger["judged_themes"]))
    old_tags = sorted(set(tags) & set(ledger["judged_tags"]))
    old_themes = sorted(set(themes) & set(ledger["judged_themes"]))
    if not new_tags and not new_themes:
        print("✅ 无新增业务标签/题材，映射无需更新")
        return 0
    print(f"增量：新业务标签 {len(new_tags)} × 全部题材 {len(themes)}；"
          f"新题材 {len(new_themes)} × 存量标签 {len(old_tags)}")

    claude_bin = find_claude()
    results: list[dict] = []
    judged_tags_ok = set(ledger["judged_tags"]) & set(tags)
    judged_themes_ok = set(ledger["judged_themes"]) & set(themes)
    # 新题材处理（2026-08-07 重构）：不再走"题材块×标签块"二维批（60题材×200
    # 标签的 prompt 实测超时，且单题材块需 17 次调用全成才记台账，预算 12 之下
    # 永不收敛）。改为把存量标签重新入队——标签批通道按块记台账断点续传、
    # 262题材×60标签形状已验证不超时；代价是老题材陪跑一轮（一次性），
    # 换来收敛性保证。sweep 期间又出新题材会再次触发重置，同样收敛。
    if new_themes and old_tags:
        print(f"ℹ️ 新题材 {len(new_themes)} 个：存量标签重新入队全量对判"
              f"（{len(old_tags)} 个，分批断点续传）")
        new_tags = sorted(set(new_tags) | set(old_tags))
        judged_tags_ok -= set(old_tags)
        judged_themes_ok.update(new_themes)
    calls = 0
    for i in range(0, len(new_tags), BATCH):
        if calls >= MAX_CALLS:
            print(f"ℹ️ 调用预算 {MAX_CALLS} 用尽，剩余标签明日续跑")
            break
        chunk = new_tags[i:i + BATCH]
        try:
            results += call_llm(claude_bin, themes, mark(chunk))
            calls += 1
            judged_tags_ok.update(chunk)
        except Exception as exc:
            calls += 1
            print(f"⚠️ 标签批次失败（下次重试）：{exc}", file=sys.stderr)

    doc = load_json(MAPPING_PATH, {"mappings": []})
    existing = {(m.get("theme"), m.get("business_tag_name"))
                for m in doc["mappings"]}
    theme_set, tag_set = set(themes), set(tags)
    day = common.now_iso()[:10]
    added = skipped = 0
    for it in results:
        theme = str(it.get("theme") or "").strip()
        tag = (str(it.get("business_tag_name") or "").strip()
               .removeprefix("【节点】"))          # 提示词要求不带前缀，防御性剥离
        mtype = it.get("mapping_type")
        try:
            conf = float(it.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        # 复刻 import 校验，避免 rebuild 时整体报错回滚
        if (theme not in theme_set or tag not in tag_set
                or mtype not in ("exact", "upstream", "downstream")
                or conf < 0.6 or (theme, tag) in existing):
            skipped += 1
            continue
        doc["mappings"].append({
            "theme": theme, "business_tag_name": tag, "mapping_type": mtype,
            "status": "candidate", "confidence": round(conf, 2),
            "rationale": f"[llm {day}] {(it.get('rationale') or '')[:100]}",
        })
        existing.add((theme, tag))
        added += 1

    print(f"映射：新增 {added}，过滤 {skipped}（校验不过/低置信/已存在）")
    if args.dry_run:
        print("（dry-run，未写盘）")
        return 0
    MAPPING_PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    # 只把成功批次记入台账，失败批次明日自动重试
    ledger["judged_tags"] = sorted(judged_tags_ok)
    ledger["judged_themes"] = sorted(judged_themes_ok)
    LEDGER_PATH.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("✅ 已写盘；跑 rebuild_semantic_layer.py + build_site.py 生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
