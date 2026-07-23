#!/usr/bin/env python3
"""
gen_tag_meta.py — 生成/增量更新 data/tag_meta.json 草稿。

标签七类模型：sector(大产业) / product(稳定细分行业、产品、技术或业务线) /
theme(市场题材) / catalyst(事件催化) / attribute(公司属性) /
event(公司级一次性事件) / unknown(未判断)。
频道由类型直接决定（不设单独字段）：sector|product|theme→题材热度，
catalyst→催化热度，其余不进热度。

规则：
- taxonomy 节点 → status=active：催化根子树=catalyst，明确大产业根=sector，
  其他稳定产品/业务默认 product，市场叙事由 OVERRIDES 明确为 theme
- 非树内但满足审核门槛（单日共振≥3股 或 累计≥3股且≥2日）→ status=candidate，
  类型为关键词推断的建议值
- --all：全库所有概念都登记（长尾也处理）——模式命中给建议类型，
  无命中默认 product；status 一律 candidate
- 已存在条目一律不动（人工审核结果优先），只新增
"""

import argparse
import json
import re
import sys

import common

META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
TAXONOMY_PATH = common.REPO_ROOT / "data" / "taxonomy.json"

CATALYST_ROOTS = {
    "业绩与分红", "重组股权事件", "供需与经营催化",
    "产品与资质获批", "超跌反弹", "风险化解",
}
ATTRIBUTE_ROOTS = {"国资背景"}

SECTOR_NAMES = {
    "交通运输", "物流", "轨道交通", "军工航天", "军工", "航空装备",
    "船舶制造", "医药医疗", "医药", "中药", "化学制药", "医疗器械",
    "医药流通", "医药零售", "动物保健", "半导体", "半导体设备",
    "半导体材料", "半导体封装", "半导体分销", "晶圆代工", "集成电路",
    "周期资源", "化工", "化工新材料", "天然气", "水泥", "油气开采",
    "油气装备", "煤化工", "煤炭", "电力", "石油化工", "石油炼化",
    "林业", "造纸", "地产基建", "房地产", "地产开发", "工程机械",
    "环保", "大消费", "农业食品", "出版", "教育", "旅游景区",
    "消费电子", "游戏", "零售", "生猪养殖", "新能源", "新能源发电",
    "储能", "光伏", "风电", "锂电池", "核电", "电网设备", "机器人",
    "汽车", "新能源汽车", "汽车零部件", "汽车电子", "金融", "证券",
    "保险", "多元金融", "期货", "算力与AI基建", "数据中心", "光通信",
    "机械设备", "电子元件", "显示与光电子", "工业气体",
}

# 关键词推断（子节点/候选标签用；OVERRIDES 优先）
CATALYST_PAT = re.compile(
    r"预增|扭亏|减亏|亏损|业绩|分红|送转|股息|回购|增持|减持|定增|转债|激励|"
    r"涨价|大单|订单|获批|募资|扩产|强赎|中标|产能|解禁|权益分派|摘帽|复牌")
EVENT_PAT = re.compile(
    r"收购|入主|参股|持股|投建|投资|转型|变更|转让|上市|解锁|澄清|终止|成立|"
    r"合作|中签|借壳|剥离|出售")
ATTRIBUTE_PAT = re.compile(
    r"国资|央企|国企|次新|ST|龙头$|供应商|供应链|供货|^客户|^供应|系$|背景$|概念股$"
)
THEME_PAT = re.compile(r"产业链$|概念$|生态$")

OVERRIDES = {
    "国企改革": "theme",        # 市场题材，非属性（评审纠错点）
    "华为昇腾": "theme", "小米汽车": "theme",
    # “产业链/概念”表示市场共振；“供应商/客户/合作”仍属于稳定属性。
    "AMD产业链": "theme", "SK海力士产业链": "theme",
    "华为产业链": "theme", "奇瑞产业链": "theme", "小米产业链": "theme",
    "特斯拉产业链": "theme", "英伟达产业链": "theme", "苹果产业链": "theme",
    "宇树科技概念": "theme", "字节概念": "theme", "成飞概念": "theme",
    "拼多多概念": "theme", "比亚迪概念": "theme", "鸿蒙生态": "theme",
    "华为合作": "attribute",
    "重组胶原蛋白": "theme",     # "重组"是生物技术，防误判
    "海外业务": "attribute", "出口": "attribute",
    "海南自贸港": "theme", "粤港澳大湾区": "theme",
    "国产替代": "theme", "3D打印": "theme",
    "存储芯片": "theme", "创新药": "theme", "先进封装": "theme",
    "工业母机": "theme", "盐湖提锂": "theme", "超级电容": "theme",
    "培育钻石": "theme", "绿色电力": "theme", "氢能": "theme",
    "碳化硅": "theme", "玻璃基板": "theme", "行星滚柱丝杠": "theme",
    "光刻胶": "theme", "HJT电池": "theme", "第三代半导体": "theme",
    "次新股": "attribute",
    "海外产能": "attribute", "全球化产能": "attribute",
    "券商看好": "event", "券商推荐": "event",
    "中标": "catalyst",
    "H股上市": "catalyst",  # 涨停语境=公告拟发H股，非公司属性（LLM复核曾误判attribute）
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


def infer_type(name: str, default: str = "product") -> str:
    """事件动词优先于属性（"江西国资拟入主"是事件不是属性），再催化、再属性。"""
    if name in OVERRIDES:
        return OVERRIDES[name]
    if EVENT_PAT.search(name):
        return "event"
    if CATALYST_PAT.search(name):
        return "catalyst"
    if ATTRIBUTE_PAT.search(name):
        return "attribute"
    if THEME_PAT.search(name) and "全产业链" not in name:
        return "theme"
    return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="全库所有概念都登记（长尾无模式命中的归 product/candidate）")
    args = ap.parse_args()

    tax = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    tax.pop("$note", None)
    children = {c for v in tax.values() for c in v}
    roots = [p for p in tax if p not in children]

    meta: dict = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    note = meta.pop("$note", None) or (
        "标签类型注册表。type: sector/product/theme/catalyst/attribute/event/unknown；"
        "status: active(已审核,可进热度)/candidate(待审核,不进热度)/retired。"
        "频道由类型决定：sector|product|theme→题材热度，catalyst→催化热度，其余不进。"
        "由 gen_tag_meta.py 生成草稿（已有条目不会被覆盖），人工修订后即为事实源。")

    # OVERRIDES 是人工拍板规则，不仅保护新条目，也纠正已存在条目的类型漂移；
    # retired 的展开/别名源标签不复活。
    corrected = 0
    for name, tag_type in OVERRIDES.items():
        if (name in meta and meta[name].get("status") != "retired"
                and meta[name].get("type") != tag_type):
            meta[name]["type"] = tag_type
            corrected += 1

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
            elif n in SECTOR_NAMES:
                t = "sector"
            else:
                t = infer_type(n, default="product")
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

    # 3) --all：剩余全部长尾概念（模式命中给类型，无命中=product）
    added["all"] = 0
    if args.all:
        for (name,) in conn.execute("SELECT name FROM concepts ORDER BY name"):
            if name in meta:
                continue
            meta[name] = {"type": infer_type(name, default="product"),
                          "status": "candidate"}
            added["all"] += 1

    out = {"$note": note}
    for k in sorted(meta, key=lambda x: (meta[x]["status"] != "active", meta[x]["type"], x)):
        out[k] = meta[k]
    META_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")

    stats: dict = {}
    for v in meta.values():
        stats[(v["status"], v["type"])] = stats.get((v["status"], v["type"]), 0) + 1
    print(f"tag_meta.json: 共 {len(meta)} 条（本次新增 树内{added['tree']} / 候选{added['cand']}"
          f" / 长尾{added.get('all', 0)}；人工规则纠偏{corrected}）")
    for (st_, t), n in sorted(stats.items()):
        print(f"  {st_:<10} {t:<10} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
