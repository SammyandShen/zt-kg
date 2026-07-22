#!/usr/bin/env python3
"""
gen_tag_meta.py — 生成/增量更新 data/tag_meta.json 草稿。

标签六类模型：sector(大产业) / theme(市场题材) / catalyst(事件催化) /
attribute(公司属性) / event(公司级一次性事件) / unknown(未判断)。
频道由类型直接决定（不设单独字段）：sector|theme→题材热度，
catalyst→催化热度，其余不进热度。

规则：
- taxonomy 节点 → status=active：业绩与分红/重组股权事件子树=catalyst，
  国资背景子树=attribute，其余根=sector、子节点按关键词判 event/catalyst，
  默认 theme；OVERRIDES 拥有最终解释权
- 非树内但满足审核门槛（单日共振≥3股 或 累计≥3股且≥2日）→ status=candidate，
  类型为关键词推断的建议值
- 已存在条目一律不动（人工审核结果优先），只新增
"""

import json
import re
import sys

import common

META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
TAXONOMY_PATH = common.REPO_ROOT / "data" / "taxonomy.json"

CATALYST_ROOTS = {"业绩与分红", "重组股权事件"}
ATTRIBUTE_ROOTS = {"国资背景"}

# 关键词推断（子节点/候选标签用；OVERRIDES 优先）
CATALYST_PAT = re.compile(
    r"预增|扭亏|减亏|亏损|业绩|分红|送转|股息|回购|增持|减持|定增|转债|激励|"
    r"涨价|大单|订单|获批|募资|扩产|强赎|中标|产能|解禁|权益分派|摘帽|复牌")
EVENT_PAT = re.compile(
    r"收购|入主|参股|持股|投建|投资|转型|变更|转让|上市|解锁|澄清|终止|成立|"
    r"合作|中签|借壳|剥离|出售")
ATTRIBUTE_PAT = re.compile(r"国资$|^央企$|^国企$|次新|^ST|龙头$|^客户|^供应")

OVERRIDES = {
    "国企改革": "theme",        # 市场题材，非属性（评审纠错点）
    "华为昇腾": "theme", "SK海力士": "theme", "小米汽车": "theme",
    "华为合作": "unknown",      # 待逐条审核后再定
    "重组胶原蛋白": "theme",     # "重组"是生物技术，防误判
    "海外业务": "attribute", "出口": "theme",
    "海南自贸区": "theme", "3D打印": "theme",
    "券商看好": "event", "券商推荐": "event",
    "产品涨价": "catalyst", "超跌反弹": "catalyst",
}


def subtree(tax: dict, root: str) -> set:
    out, stack = set(), [root]
    while stack:
        n = stack.pop()
        if n in out:
            continue
        out.add(n)
        stack += tax.get(n, [])
    return out


def infer_type(name: str) -> str:
    if name in OVERRIDES:
        return OVERRIDES[name]
    if ATTRIBUTE_PAT.search(name):
        return "attribute"
    if CATALYST_PAT.search(name):
        return "catalyst"
    if EVENT_PAT.search(name):
        return "event"
    return "theme"


def main() -> int:
    tax = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    tax.pop("$note", None)
    children = {c for v in tax.values() for c in v}
    roots = [p for p in tax if p not in children]

    meta: dict = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    note = meta.pop("$note", None) or (
        "标签类型注册表。type: sector/theme/catalyst/attribute/event/unknown；"
        "status: active(已审核,可进热度)/candidate(待审核,不进热度)/retired。"
        "频道由类型决定：sector|theme→题材热度，catalyst→催化热度，其余不进。"
        "由 gen_tag_meta.py 生成草稿（已有条目不会被覆盖），人工修订后即为事实源。")

    cat_nodes = set().union(*[subtree(tax, r) for r in CATALYST_ROOTS])
    attr_nodes = set().union(*[subtree(tax, r) for r in ATTRIBUTE_ROOTS])

    added = {"tree": 0, "cand": 0}
    # 1) taxonomy 节点 → active
    for r in roots:
        for n in subtree(tax, r):
            if n in meta:
                continue
            if n in OVERRIDES:
                t = OVERRIDES[n]
            elif n in attr_nodes:
                t = "attribute"
            elif n in cat_nodes:
                t = "catalyst"
            elif n in roots:
                t = "sector" if n not in ("国企改革",) else "theme"
            else:
                t = infer_type(n)
            meta[n] = {"type": t, "status": "active"}
            added["tree"] += 1

    # 2) 达标候选（非树内）→ candidate
    conn = common.open_db()
    rows = conn.execute("""
        WITH s AS (SELECT ec.concept_id cid, e.trade_date d, COUNT(DISTINCT e.code) ds
                   FROM event_concepts ec JOIN limit_up_events e ON e.id=ec.event_id
                   GROUP BY cid, d),
        agg AS (SELECT cid, MAX(ds) mx, COUNT(*) days FROM s GROUP BY cid),
        st AS (SELECT ec.concept_id cid, COUNT(DISTINCT e.code) stocks
               FROM event_concepts ec JOIN limit_up_events e ON e.id=ec.event_id GROUP BY cid)
        SELECT c.name FROM agg JOIN st USING(cid) JOIN concepts c ON c.id=agg.cid
        WHERE agg.mx>=3 OR (st.stocks>=3 AND agg.days>=2)""").fetchall()
    for (name,) in rows:
        if name in meta:
            continue
        meta[name] = {"type": infer_type(name), "status": "candidate"}
        added["cand"] += 1

    out = {"$note": note}
    for k in sorted(meta, key=lambda x: (meta[x]["status"] != "active", meta[x]["type"], x)):
        out[k] = meta[k]
    META_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")

    stats: dict = {}
    for v in meta.values():
        stats[(v["status"], v["type"])] = stats.get((v["status"], v["type"]), 0) + 1
    print(f"tag_meta.json: 共 {len(meta)} 条（本次新增 树内{added['tree']} / 候选{added['cand']}）")
    for (st_, t), n in sorted(stats.items()):
        print(f"  {st_:<10} {t:<10} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
