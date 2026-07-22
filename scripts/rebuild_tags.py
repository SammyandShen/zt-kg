#!/usr/bin/env python3
"""
rebuild_tags.py — aliases.json 变更后，从原始 reason_type 全量重建概念派生表。

流程：校验 aliases.json → 清空 event_concepts/concept_aliases → 重灌别名 →
      遍历全部事件重新拆分映射 → 清孤儿概念 → 打印变更摘要。
安全：limit_up_events.reason_type 是原始真源，本脚本只动派生表，可无限次重跑。
"""

import sys

import common


def main() -> int:
    alias_map = common.load_aliases()  # 内部已校验别名冲突
    conn = common.open_db()

    before = dict(conn.execute(
        "SELECT c.name, COUNT(*) FROM concepts c "
        "JOIN event_concepts ec ON ec.concept_id=c.id GROUP BY c.id"))

    with conn:
        conn.execute("DELETE FROM event_concepts")
        conn.execute("DELETE FROM concept_aliases")

        cache: dict[str, int] = {}
        # 重灌别名表
        for alias, canon in alias_map.items():
            cid = common.get_or_create_concept(conn, canon, cache)
            conn.execute("INSERT OR REPLACE INTO concept_aliases(alias, concept_id) VALUES(?,?)",
                         (alias, cid))
        # 全量重建事件-概念关系
        n_events = 0
        for eid, reason in conn.execute(
                "SELECT id, reason_type FROM limit_up_events").fetchall():
            for tag in common.split_tags(reason):
                canon = alias_map.get(tag, tag)
                cid = common.get_or_create_concept(conn, canon, cache)
                conn.execute(
                    "INSERT OR IGNORE INTO event_concepts(event_id, concept_id, raw_tag) "
                    "VALUES(?,?,?)", (eid, cid, tag))
            n_events += 1
        # 清孤儿概念
        orphans = conn.execute(
            "SELECT id, name FROM concepts WHERE id NOT IN "
            "(SELECT DISTINCT concept_id FROM event_concepts) AND id NOT IN "
            "(SELECT DISTINCT concept_id FROM concept_aliases)").fetchall()
        for oid, _ in orphans:
            conn.execute("DELETE FROM concepts WHERE id=?", (oid,))

    after = dict(conn.execute(
        "SELECT c.name, COUNT(*) FROM concepts c "
        "JOIN event_concepts ec ON ec.concept_id=c.id GROUP BY c.id"))

    print(f"重建完成：{n_events} 个事件，概念数 {len(before)} → {len(after)}，"
          f"清理孤儿概念 {len(orphans)} 个")
    merged = {k: (before.get(k, 0), v) for k, v in after.items()
              if v != before.get(k, 0)}
    if merged:
        print("计数变化的概念（多为合并所致）：")
        for name, (b, a) in sorted(merged.items(), key=lambda x: -x[1][1])[:20]:
            print(f"  {name}: {b} → {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
