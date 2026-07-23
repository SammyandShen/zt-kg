#!/usr/bin/env python3
"""标签体系质量门禁：只读检查，不修改数据库或配置。"""

import json
import sys
from collections import Counter

import common
import gen_tag_meta


META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
TAXONOMY_PATH = common.REPO_ROOT / "data" / "taxonomy.json"
VALID_TYPES = {"sector", "theme", "catalyst", "attribute", "event", "unknown"}
VALID_STATUS = {"active", "candidate", "retired"}


def bucket(tag_type: str) -> str:
    if tag_type in {"sector", "theme"}:
        return "theme"
    return tag_type


def find_cycle(taxonomy: dict[str, list[str]]) -> list[str] | None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: list[str]) -> list[str] | None:
        if node in visiting:
            return path[path.index(node):] + [node]
        if node in visited:
            return None
        visiting.add(node)
        path.append(node)
        for child in taxonomy.get(node, []):
            cycle = visit(child, path)
            if cycle:
                return cycle
        path.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in taxonomy:
        cycle = visit(node, [])
        if cycle:
            return cycle
    return None


def main() -> int:
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    meta.pop("$note", None)
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    taxonomy.pop("$note", None)
    alias_raw = json.loads(common.ALIASES_PATH.read_text(encoding="utf-8"))
    alias_map = common.load_aliases()
    expansions = common.load_expansions(alias_map)
    conn = common.open_db()
    concepts = {row[0] for row in conn.execute("SELECT name FROM concepts")}

    errors: list[str] = []
    warnings: list[str] = []
    invalid_meta = sorted(
        name for name, value in meta.items()
        if value.get("type") not in VALID_TYPES or value.get("status") not in VALID_STATUS
    )
    if invalid_meta:
        errors.append("tag_meta 非法枚举：" + "、".join(invalid_meta[:20]))

    missing_meta = sorted(concepts - set(meta))
    if missing_meta:
        errors.append("数据库概念缺少 tag_meta：" + "、".join(missing_meta[:20]))
    retired_concepts = sorted(
        name for name in concepts
        if meta.get(name, {}).get("status") == "retired"
    )
    if retired_concepts:
        errors.append("派生表仍含 retired 概念：" + "、".join(retired_concepts[:20]))

    cycle = find_cycle(taxonomy)
    if cycle:
        errors.append("taxonomy 存在环：" + " → ".join(cycle))

    nodes = set(taxonomy)
    nodes.update(child for children in taxonomy.values() for child in children)
    missing_node_meta = sorted(nodes - set(meta))
    if missing_node_meta:
        errors.append("taxonomy 节点缺少 tag_meta：" + "、".join(missing_node_meta[:20]))
    non_active_nodes = sorted(
        name for name in nodes
        if name in meta and meta[name].get("status") != "active"
    )
    if non_active_nodes:
        errors.append("taxonomy 含非 active 节点：" + "、".join(non_active_nodes[:20]))

    cross_channel = sorted(
        (parent, child)
        for parent, children in taxonomy.items()
        for child in children
        if parent in meta and child in meta
        and bucket(meta[parent]["type"]) != bucket(meta[child]["type"])
    )
    if cross_channel:
        errors.append("taxonomy 跨频道父子：" +
                      "、".join(f"{p}→{c}" for p, c in cross_channel[:20]))

    active_heat = {
        name for name, value in meta.items()
        if value.get("status") == "active"
        and value.get("type") in {"sector", "theme", "catalyst"}
    }
    missing_from_tree = sorted(active_heat - nodes)
    if missing_from_tree:
        errors.append("active 热力标签未入 taxonomy：" + "、".join(missing_from_tree[:20]))

    bad_expansion_sources = sorted(
        source for source in expansions
        if meta.get(source, {}).get("status") != "retired"
    )
    if bad_expansion_sources:
        errors.append("展开源未 retired：" + "、".join(bad_expansion_sources[:20]))
    bad_expansion_targets = sorted(
        target for targets in expansions.values() for target in targets
        if target not in meta
        or meta[target].get("status") != "active"
        or meta[target].get("type") in {"event", "unknown"}
    )
    if bad_expansion_targets:
        errors.append("展开目标未正式定型：" + "、".join(sorted(set(bad_expansion_targets))[:20]))

    alias_sources = {
        alias
        for canonical, aliases in alias_raw.items()
        if not canonical.startswith("$")
        for alias in aliases
    }
    bad_alias_sources = sorted(
        alias for alias in alias_sources
        if alias in meta and meta[alias].get("status") != "retired"
    )
    if bad_alias_sources:
        errors.append("别名源未 retired：" + "、".join(bad_alias_sources[:20]))

    override_drift = sorted(
        name for name, tag_type in gen_tag_meta.OVERRIDES.items()
        if name in meta and meta[name].get("status") != "retired"
        and meta[name].get("type") != tag_type
    )
    if override_drift:
        errors.append("OVERRIDES 与 tag_meta 漂移：" + "、".join(override_drift[:20]))

    reviewed = Counter((value["status"], value["type"]) for value in meta.values())
    print(f"数据库概念 {len(concepts)}；tag_meta {len(meta)}；taxonomy 节点 {len(nodes)}")
    for (status, tag_type), count in sorted(reviewed.items()):
        print(f"  {status:<10} {tag_type:<10} {count}")
    if warnings:
        print("\n警告：")
        for warning in warnings:
            print("  - " + warning)
    if errors:
        print("\n❌ 标签质量门禁未通过：")
        for error in errors:
            print("  - " + error)
        return 1
    print("\n✅ 标签质量门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
