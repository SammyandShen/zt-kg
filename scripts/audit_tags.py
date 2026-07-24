#!/usr/bin/env python3
"""标签体系质量门禁：只读检查，不修改数据库或配置。"""

import json
import sys
from collections import Counter

import common
import gen_tag_meta


META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
TAXONOMY_PATH = common.REPO_ROOT / "data" / "taxonomy.json"
VALID_TYPES = {"sector", "product", "theme", "catalyst", "attribute", "event", "unknown"}
VALID_STATUS = {"active", "candidate", "retired"}
DISALLOWED_TAXONOMY_EDGES = {
    ("半导体设备", "工业母机"),
    ("工业母机", "轻型输送带"),
    ("工业母机", "空分设备"),
    ("商业航天", "氦气"),
    ("半导体", "MLCC"),
    ("半导体", "薄膜电容"),
    ("半导体", "铝电解电容"),
}


def bucket(tag_type: str) -> str:
    if tag_type in {"sector", "product", "theme"}:
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
        or ("virtual" in value and not isinstance(value["virtual"], bool))
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

    disallowed_edges = sorted(
        (parent, child)
        for parent, children in taxonomy.items()
        for child in children
        if (parent, child) in DISALLOWED_TAXONOMY_EDGES
    )
    if disallowed_edges:
        errors.append("taxonomy 含已确认错误父子：" +
                      "、".join(f"{p}→{c}" for p, c in disallowed_edges))

    inverted_sector_edges = sorted(
        (parent, child)
        for parent, children in taxonomy.items()
        for child in children
        if parent in meta and child in meta
        and meta[parent].get("type") == "theme"
        and meta[child].get("type") == "sector"
    )
    if inverted_sector_edges:
        errors.append("题材节点下直接挂大产业（应改为 product/theme 或调整方向）：" +
                      "、".join(f"{p}→{c}" for p, c in inverted_sector_edges[:20]))

    broad_sector_drift = sorted(
        name for name, value in meta.items()
        if value.get("status") != "retired"
        and value.get("type") == "sector"
        and name not in gen_tag_meta.SECTOR_NAMES
    )
    if broad_sector_drift:
        errors.append("sector 混入未登记的大产业（细分产品应为 product）：" +
                      "、".join(broad_sector_drift[:20]))

    narrative_suffix_drift = sorted(
        name for name, value in meta.items()
        if value.get("status") != "retired"
        and value.get("type") not in {"theme", "event", "catalyst"}
        and name.endswith(("产业链", "概念", "生态"))
        and "全产业链" not in name
    )
    if narrative_suffix_drift:
        errors.append("产业链/概念/生态叙事未归 theme：" +
                      "、".join(narrative_suffix_drift[:20]))

    active_heat = {
        name for name, value in meta.items()
        if value.get("status") == "active"
        and value.get("type") in {"sector", "product", "theme", "catalyst"}
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

    # 语义层门禁：供应商原因自动生成的记录不能升级为已核实；已核实业务事实和
    # 人工涨停归因必须有可追溯证据；拟收购不能伪装成核心主营。
    missing_business_evidence = conn.execute("""
        SELECT f.code||':'||f.tag_name
        FROM stock_business_facts f
        LEFT JOIN business_fact_evidence be ON be.fact_id=f.id
        WHERE f.status='verified'
        GROUP BY f.id HAVING COUNT(be.evidence_id)=0
    """).fetchall()
    if missing_business_evidence:
        errors.append("已核实业务事实缺少证据：" +
                      "、".join(row[0] for row in missing_business_evidence[:20]))

    missing_attribution_evidence = conn.execute("""
        SELECT e.code||':'||e.trade_date||':'||c.name
        FROM event_theme_links l
        JOIN limit_up_events e ON e.id=l.event_id
        JOIN concepts c ON c.id=l.concept_id
        LEFT JOIN event_theme_evidence ete
          ON ete.event_id=l.event_id AND ete.concept_id=l.concept_id
        WHERE l.source='manual' AND l.status='verified'
        GROUP BY l.event_id,l.concept_id HAVING COUNT(ete.evidence_id)=0
    """).fetchall()
    if missing_attribution_evidence:
        errors.append("已核实单次涨停归因缺少支持证据：" +
                      "、".join(row[0] for row in missing_attribution_evidence[:20]))

    derived_verified = conn.execute(
        "SELECT COUNT(*) FROM event_theme_links "
        "WHERE source='derived' AND status='verified'"
    ).fetchone()[0]
    if derived_verified:
        errors.append(f"自动原因标签被错误升级为 verified：{derived_verified} 条")

    invalid_reviews = conn.execute("""
        SELECT e.code||':'||e.trade_date||':'||c.name
        FROM attribution_reviews r
        JOIN event_theme_links l
          ON l.event_id=r.event_id AND l.concept_id=r.concept_id
        JOIN limit_up_events e ON e.id=r.event_id
        JOIN concepts c ON c.id=r.concept_id
        WHERE r.stage NOT IN (0,1,2)
           OR r.verdict NOT IN ('supporting','weak','insufficient')
           OR r.score<0 OR r.score>1
           OR r.retained_rate<0 OR r.retained_rate>1
           OR r.mature!=(r.stage=2)
           OR r.source!='deterministic-v1'
           OR l.status!='candidate'
    """).fetchall()
    if invalid_reviews:
        errors.append("T+归因复核记录违反旁证边界：" +
                      "、".join(row[0] for row in invalid_reviews[:20]))

    acquisition_as_core = conn.execute("""
        SELECT code||':'||tag_name FROM stock_business_facts
        WHERE relation_type='planned_acquisition'
          AND maturity IN ('core_revenue','commercialized')
    """).fetchall()
    if acquisition_as_core:
        errors.append("拟收购被错误标成现有主营/商业化：" +
                      "、".join(row[0] for row in acquisition_as_core[:20]))

    invalid_theme_mappings = conn.execute("""
        SELECT c.name||'→'||m.business_tag_name
        FROM theme_business_mappings m
        JOIN concepts c ON c.id=m.concept_id
        LEFT JOIN stock_business_facts f
          ON f.tag_name=m.business_tag_name
         AND f.status NOT IN ('rejected','expired')
        WHERE m.status!='rejected'
        GROUP BY m.concept_id,m.business_tag_name
        HAVING COUNT(f.id)=0
    """).fetchall()
    if invalid_theme_mappings:
        errors.append("题材业务映射找不到有效业务事实：" +
                      "、".join(row[0] for row in invalid_theme_mappings[:20]))

    meta_type_by_name = {
        name: value.get("type") for name, value in meta.items()
    }
    mapping_theme_names = [
        row[0] for row in conn.execute("""
            SELECT DISTINCT c.name FROM theme_business_mappings m
            JOIN concepts c ON c.id=m.concept_id
            WHERE m.status!='rejected'
        """)
    ]
    bad_mapping_theme_types = sorted(
        name for name in mapping_theme_names
        if meta_type_by_name.get(name) != "theme"
    )
    if bad_mapping_theme_types:
        errors.append("题材业务映射左侧不是 theme：" +
                      "、".join(bad_mapping_theme_types[:20]))

    event_theme_names = [
        row[0] for row in conn.execute("""
            SELECT DISTINCT c.name FROM event_theme_links l
            JOIN concepts c ON c.id=l.concept_id
            WHERE l.status!='rejected'
        """)
    ]
    bad_event_theme_types = sorted(
        name for name in event_theme_names
        if meta.get(name, {}).get("type") != "theme"
        or meta.get(name, {}).get("status") != "active"
    )
    if bad_event_theme_types:
        errors.append("单次涨停题材关系引用了非 active/theme 标签：" +
                      "、".join(bad_event_theme_types[:20]))

    reviewed = Counter((value["status"], value["type"]) for value in meta.values())
    print(f"数据库概念 {len(concepts)}；tag_meta {len(meta)}；taxonomy 节点 {len(nodes)}")
    print(f"  virtual    {sum(bool(value.get('virtual')) for value in meta.values())}")
    for (status, tag_type), count in sorted(reviewed.items()):
        print(f"  {status:<10} {tag_type:<10} {count}")
    semantic_counts = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM stock_business_facts),
          (SELECT COUNT(*) FROM event_theme_links),
          (SELECT COUNT(*) FROM event_theme_links WHERE status='verified'),
          (SELECT COUNT(*) FROM theme_episodes),
          (SELECT COUNT(*) FROM theme_business_mappings WHERE status!='rejected'),
          (SELECT COUNT(*) FROM evidence_items),
          (SELECT COUNT(*) FROM business_fact_candidates WHERE status='candidate'),
          (SELECT COUNT(*) FROM attribution_reviews)
    """).fetchone()
    print("语义层：业务事实 {}；单次题材关系 {}（已核实 {}）；题材轮次 {}；"
          "题材业务映射 {}；证据 {}；年报业务候选 {}；T+复核 {}".format(
        *semantic_counts))
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
