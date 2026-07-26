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
TIMEOUT_SEC = 480

PROMPT = """你是A股题材研究员。下面给出「交易题材」列表和「公司业务标签」列表
（业务标签来自年报核实的公司在营业务）。

任务：找出所有"业务标签是该题材的直接承载"的映射。规则（必须保守）：
- exact：业务本身就是题材交易的对象（脑电采集设备→脑机接口）
- upstream：业务是题材核心环节的直接上游材料/设备（电极材料→脑机接口）
- downstream：业务是题材的直接下游应用
- 只映射直接关系；"沾边""可能受益"一律不输出。宁缺毋滥。
- 同一业务标签可映射到多个题材，反之亦然。

只输出 JSON 数组，不要其他文字：
[{"theme":"题材名","business_tag_name":"业务标签名","mapping_type":"exact|upstream|downstream","confidence":0.0到1.0,"rationale":"一句话"}]

交易题材：%s

业务标签：%s
"""


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def active_themes() -> list[str]:
    meta = load_json(META_PATH, {})
    meta.pop("$note", None)
    return sorted(nm for nm, m in meta.items()
                  if m.get("type") == "theme" and m.get("status") == "active")


def business_tags(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT tag_name FROM stock_business_facts "
        "WHERE status NOT IN ('rejected','expired')"))


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
    themes = active_themes()
    tags = business_tags(conn)
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
    if new_tags:
        results += call_llm(claude_bin, themes, new_tags)
    if new_themes and old_tags:
        results += call_llm(claude_bin, new_themes, old_tags)

    doc = load_json(MAPPING_PATH, {"mappings": []})
    existing = {(m.get("theme"), m.get("business_tag_name"))
                for m in doc["mappings"]}
    theme_set, tag_set = set(themes), set(tags)
    day = common.now_iso()[:10]
    added = skipped = 0
    for it in results:
        theme = str(it.get("theme") or "").strip()
        tag = str(it.get("business_tag_name") or "").strip()
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
    ledger["judged_tags"] = tags
    ledger["judged_themes"] = themes
    LEDGER_PATH.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("✅ 已写盘；跑 rebuild_semantic_layer.py + build_site.py 生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
