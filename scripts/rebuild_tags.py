#!/usr/bin/env python3
"""
rebuild_tags.py — aliases.json / tag_expansions.json 变更后，从原始 reason_type
全量重建概念派生表。

流程：校验两份配置 → 清空 event_concepts/concept_aliases → 重灌别名 →
      遍历全部事件走 normalize_tags（拆分→展开→别名）→ 清孤儿概念 → 打印变更摘要。
--dry-run：只在内存里预演，打印将发生的变化（展开命中、计数增减、新增/消失概念），
           不写数据库。改完配置先 dry-run 过目再实跑。
安全：limit_up_events.reason_type 是原始真源，本脚本只动派生表，可无限次重跑。
"""

import argparse
import sys
from collections import Counter

import common


def current_counts(conn) -> dict[str, int]:
    return dict(conn.execute(
        "SELECT c.name, COUNT(*) FROM concepts c "
        "JOIN event_concepts ec ON ec.concept_id=c.id GROUP BY c.id"))


def simulate(conn, alias_map, expansions):
    """内存预演：返回 (重建后各概念计数, 各展开键命中事件数)。"""
    after: Counter = Counter()
    exp_hits: Counter = Counter()
    for (reason,) in conn.execute("SELECT reason_type FROM limit_up_events"):
        pairs = common.normalize_tags(reason, alias_map, expansions)
        for canon, _ in pairs:
            after[canon] += 1
        for tag in common.split_tags(reason):
            if tag in expansions:
                exp_hits[tag] += 1
    return after, exp_hits


def print_diff(before: dict[str, int], after: dict[str, int],
               exp_hits: Counter, expansions: dict) -> None:
    if exp_hits:
        print(f"展开命中 {len(exp_hits)}/{len(expansions)} 个键：")
        for tag, n in exp_hits.most_common():
            print(f"  {tag} → {' + '.join(expansions[tag])}  （{n} 个事件）")
    unused = sorted(set(expansions) - set(exp_hits))
    if unused:
        print(f"⚠️ {len(unused)} 个展开键在库中无事件（留着等数据，或检查写法）：{'、'.join(unused)}")

    gone = sorted((k for k in before if k not in after), key=lambda k: -before[k])
    born = sorted((k for k in after if k not in before), key=lambda k: -after[k])
    changed = {k: (before[k], after[k]) for k in after
               if k in before and after[k] != before[k]}
    if gone:
        print(f"将消失的概念 {len(gone)} 个：" +
              "、".join(f"{k}({before[k]})" for k in gone[:30]) +
              (" …" if len(gone) > 30 else ""))
    if born:
        print(f"将新增的概念 {len(born)} 个：" +
              "、".join(f"{k}({after[k]})" for k in born[:30]) +
              (" …" if len(born) > 30 else ""))
    if changed:
        print("计数变化的概念：")
        for name, (b, a) in sorted(changed.items(), key=lambda x: -abs(x[1][1] - x[1][0]))[:30]:
            print(f"  {name}: {b} → {a}")
    if not (gone or born or changed):
        print("配置与当前派生表一致，无变化")


def main() -> int:
    ap = argparse.ArgumentParser(description="重建概念派生表")
    ap.add_argument("--dry-run", action="store_true", help="只预演打印变化，不写库")
    args = ap.parse_args()

    alias_map = common.load_aliases()            # 内部已校验别名冲突
    expansions = common.load_expansions(alias_map)  # 校验链式/与别名重叠
    conn = common.open_db()
    before = current_counts(conn)

    if args.dry_run:
        after, exp_hits = simulate(conn, alias_map, expansions)
        print("== dry-run 预演（未写库） ==")
        print_diff(before, dict(after), exp_hits, expansions)
        return 0

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
            for canon, tag in common.normalize_tags(reason, alias_map, expansions):
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

    after = current_counts(conn)
    _, exp_hits = simulate(conn, alias_map, expansions)
    print(f"重建完成：{n_events} 个事件，概念数 {len(before)} → {len(after)}，"
          f"清理孤儿概念 {len(orphans)} 个")
    print_diff(before, after, exp_hits, expansions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
