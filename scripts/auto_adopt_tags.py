#!/usr/bin/env python3
"""
auto_adopt_tags.py — 标签分型/挂树的自动采纳工序（零人工干预原则）。

做两件事（都幂等）：
1. 高置信转正：candidate 标签且 LLM 复核 conf≥0.8 且类型≠unknown → status=active。
2. 父建议自动挂树：llm_parent_suggestions.json 过四道闸后写入 taxonomy.json——
   ①父节点存在（tag_meta 或 taxonomy 内）②频道一致（sector/product/theme 同属
   题材频道；catalyst 只挂 catalyst；attribute/event 不挂）③无环 ④conf≥0.6。

bad case 由用户反馈后用 OVERRIDES（gen_tag_meta.py）或手工修 taxonomy 覆盖；
OVERRIDES 内的标签本工序永不改动。运行前检测 classify_tags 是否在跑（避免写冲突）。

用法：python3 scripts/auto_adopt_tags.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import common
from gen_tag_meta import OVERRIDES

META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
TAX_PATH = common.REPO_ROOT / "data" / "taxonomy.json"
LEDGER_PATH = common.REPO_ROOT / "data" / "llm_review.json"
SUGG_PATH = common.REPO_ROOT / "data" / "llm_parent_suggestions.json"

HEAT_BUCKET = {"sector": "theme", "product": "theme", "theme": "theme",
               "catalyst": "catalyst"}


def classify_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "classify_tags.py"], capture_output=True)
    return r.returncode == 0


def ancestors(tax: dict, node: str) -> set:
    """node 的全部祖先（防环加入挂树前检查）。"""
    parents = {}
    for p, kids in tax.items():
        for k in kids:
            parents.setdefault(k, []).append(p)
    out, stack = set(), [node]
    while stack:
        n = stack.pop()
        for p in parents.get(n, []):
            if p not in out:
                out.add(p)
                stack.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if classify_running():
        print("⚠️ classify_tags.py 正在运行，跳过本次自动采纳（避免写冲突）")
        return 0

    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    note = meta.pop("$note", None)
    tax = json.loads(TAX_PATH.read_text(encoding="utf-8"))
    tax_note = tax.pop("$note", None)
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8")) \
        if LEDGER_PATH.exists() else {}
    suggs = json.loads(SUGG_PATH.read_text(encoding="utf-8")) \
        if SUGG_PATH.exists() else {}

    tax_nodes = set(tax) | {c for kids in tax.values() for c in kids}
    type_of = lambda nm: (meta.get(nm) or {}).get("type", "unknown")

    # 1) 高置信转正
    n_active = 0
    for nm, m in meta.items():
        if nm in OVERRIDES or m.get("status") != "candidate":
            continue
        led = ledger.get(nm) or {}
        if float(led.get("c", 0)) >= 0.8 and led.get("t") not in (None, "unknown") \
                and m.get("type") == led.get("t"):
            m["status"] = "active"
            n_active += 1

    # 2) 父建议四道闸挂树
    n_edge = 0
    skip = {"no_parent": 0, "channel": 0, "cycle": 0, "lowconf": 0, "dup": 0}
    for nm, led in ledger.items():
        parent = (suggs.get(nm) or led.get("p") or "").strip()
        if not parent or nm in OVERRIDES:
            continue
        if float(led.get("c", 0)) < 0.6:
            skip["lowconf"] += 1
            continue
        child_bucket = HEAT_BUCKET.get(type_of(nm))
        parent_bucket = HEAT_BUCKET.get(type_of(parent))
        if parent not in tax_nodes and parent not in meta:
            skip["no_parent"] += 1
            continue
        if not child_bucket or not parent_bucket or child_bucket != parent_bucket:
            skip["channel"] += 1
            continue
        if nm in tax.get(parent, []):
            skip["dup"] += 1
            continue
        if nm == parent or parent in {nm} | set(tax.get(nm, [])) \
                or nm in ancestors(tax, parent):
            skip["cycle"] += 1
            continue
        tax.setdefault(parent, []).append(nm)
        tax_nodes.add(nm)
        tax_nodes.add(parent)
        n_edge += 1

    print(f"自动采纳：转正 {n_active}，挂树 {n_edge}；"
          f"跳过 父不存在{skip['no_parent']}/频道不符{skip['channel']}/"
          f"防环{skip['cycle']}/低置信{skip['lowconf']}/已存在{skip['dup']}")
    if args.dry_run:
        print("（dry-run，未写盘）")
        return 0

    out_meta = {"$note": note} if note else {}
    for k in sorted(meta, key=lambda x: (meta[x]["status"] != "active",
                                         meta[x]["type"], x)):
        out_meta[k] = meta[k]
    META_PATH.write_text(json.dumps(out_meta, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    out_tax = {"$note": tax_note} if tax_note else {}
    out_tax.update({k: tax[k] for k in tax})
    TAX_PATH.write_text(json.dumps(out_tax, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print("✅ 已写盘 tag_meta.json / taxonomy.json；请跑 audit_tags.py + build_site.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
