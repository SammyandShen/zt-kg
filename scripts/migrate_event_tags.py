#!/usr/bin/env python3
"""
migrate_event_tags.py — event 类标签出词典（一次性迁移，可重复跑）。

设计依据（三层体系）：event 是"某公司某天发生了什么"，是记录不是词汇——
公告类事件由 corporate_events 承载，原始涨停原因永存 limit_up_events.reason_type
并在个股页展示。因此词典中 type=event 的标签一律 retire：
- tag_meta: status='retired'（保留条目防 gen_tag_meta --all 重新登记）
- taxonomy: 从所有父节点的子列表中移除；若自身是父键则整键删除
- OVERRIDES 中显式声明为 event 的（如"券商看好"）同样 retire

运行前检测 classify_tags 防写冲突。用法：--dry-run 预览。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import common

META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
TAX_PATH = common.REPO_ROOT / "data" / "taxonomy.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if subprocess.run(["pgrep", "-f", "classify_tags.py"],
                      capture_output=True).returncode == 0:
        print("⚠️ classify_tags.py 正在运行，跳过迁移（避免写冲突）")
        return 0

    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    note = meta.pop("$note", None)
    tax = json.loads(TAX_PATH.read_text(encoding="utf-8"))
    tax_note = tax.pop("$note", None)

    event_tags = {nm for nm, m in meta.items()
                  if m.get("type") == "event" and m.get("status") != "retired"}
    n_retire = len(event_tags)
    for nm in event_tags:
        meta[nm]["status"] = "retired"

    n_edge = n_parent = 0
    for parent in list(tax):
        if parent in event_tags:
            del tax[parent]
            n_parent += 1
            continue
        before = len(tax[parent])
        tax[parent] = [c for c in tax[parent] if c not in event_tags]
        n_edge += before - len(tax[parent])

    print(f"event 出词典：retire {n_retire} 个标签，"
          f"移除树边 {n_edge} 条 / 父键 {n_parent} 个")
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
    out_tax.update(tax)
    TAX_PATH.write_text(json.dumps(out_tax, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print("✅ 已写盘；请跑 audit_tags.py + rebuild_semantic_layer.py + build_site.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
