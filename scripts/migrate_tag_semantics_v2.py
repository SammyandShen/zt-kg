#!/usr/bin/env python3
"""产业/题材语义修复迁移。

一次性、幂等地完成：
1. 引入 product（稳定细分行业/产品/业务线），让 sector 只保留大产业骨架；
2. 修正已确认的产业/题材错分与供应链题材；
3. 合并严格同义标签、拆解“风电光伏”复合标签；
4. 修正 taxonomy 中已确认的错误父子关系，并登记虚拟分组。

本脚本只改 JSON 配置，不直接修改 SQLite；运行后必须先
rebuild_tags.py --dry-run，再实跑 rebuild_tags.py。
"""

from __future__ import annotations

import json
from pathlib import Path

import common


DATA_DIR = common.REPO_ROOT / "data"
META_PATH = DATA_DIR / "tag_meta.json"
TAXONOMY_PATH = DATA_DIR / "taxonomy.json"
ALIASES_PATH = DATA_DIR / "aliases.json"
EXPANSIONS_PATH = DATA_DIR / "tag_expansions.json"
LLM_REVIEW_PATH = DATA_DIR / "llm_review.json"
PARENT_SUG_PATH = DATA_DIR / "llm_parent_suggestions.json"


# sector 只保留可稳定充当产业骨架的大类；细分行业、产品和业务线统一归 product。
SECTOR_NAMES = {
    "交通运输", "物流", "轨道交通",
    "军工航天", "军工", "航空装备", "船舶制造",
    "医药医疗", "医药", "中药", "化学制药", "医疗器械",
    "医药流通", "医药零售", "动物保健",
    "半导体", "半导体设备", "半导体材料", "半导体封装",
    "半导体分销", "晶圆代工", "集成电路",
    "周期资源", "化工", "化工新材料", "天然气", "水泥",
    "油气开采", "油气装备", "煤化工", "煤炭", "电力",
    "石油化工", "石油炼化", "林业", "造纸",
    "地产基建", "房地产", "地产开发", "工程机械", "环保",
    "大消费", "农业食品", "出版", "教育", "旅游景区",
    "消费电子", "游戏", "零售", "生猪养殖",
    "新能源", "新能源发电", "储能", "光伏", "风电",
    "锂电池", "核电", "电网设备",
    "机器人",
    "汽车", "新能源汽车", "汽车零部件", "汽车电子",
    "金融", "证券", "保险", "多元金融", "期货",
    "算力与AI基建", "数据中心", "光通信",
    "机械设备", "电子元件", "显示与光电子", "工业气体",
}

# 已确认属于市场交易叙事/技术路线，而不是稳定大产业骨架。
THEME_NAMES = {
    "存储芯片", "创新药", "先进封装", "工业母机", "盐湖提锂",
    "超级电容", "培育钻石", "绿色电力", "氢能", "碳化硅",
    "玻璃基板", "行星滚柱丝杠", "光刻胶", "HJT电池",
    "第三代半导体",
    # 企业生态类：用“产业链/概念”表示市场共振；供应商/客户/合作仍保留 attribute。
    "AMD产业链", "SK海力士产业链", "华为产业链", "奇瑞产业链",
    "小米产业链", "特斯拉产业链", "英伟达产业链", "苹果产业链",
    "宇树科技概念", "字节概念", "成飞概念", "拼多多概念",
    "比亚迪概念", "鸿蒙生态",
}

# 虚拟节点只承担聚合/导航，不是假装成一条原始涨停标签。
VIRTUAL_NODES = {
    "业绩与分红": "catalyst",
    "供需与经营催化": "catalyst",
    "重组股权事件": "catalyst",
    "风险化解": "catalyst",
    "算力与AI基建": "sector",
    "周期资源": "sector",
    "地产基建": "sector",
    "军工航天": "sector",
    "机械设备": "sector",
    "电子元件": "sector",
    "显示与光电子": "sector",
    "工业气体": "sector",
    "通用设备": "product",
    "工业零部件": "product",
}

STRICT_ALIASES = {
    "碳化硅": ["SiC"],
    "先进封装": ["半导体先进封装"],
    "风电设备": ["风电装备"],
    "HJT电池": ["异质结"],
    "算力PCB": ["AI算力PCB"],
    "光伏": ["光伏概念"],
    "半导体": ["芯片概念"],
    "智谱AI": ["智谱AI概念"],
    "鸿蒙生态": ["鸿蒙概念"],
}

THEME_PARENTS = {
    "AMD产业链": ["AI算力"],
    "SK海力士产业链": ["存储芯片"],
    "华为产业链": ["国产替代"],
    "奇瑞产业链": ["汽车"],
    "小米产业链": ["小米汽车"],
    "特斯拉产业链": ["汽车", "新能源汽车"],
    "英伟达产业链": ["AI算力"],
    "苹果产业链": ["消费电子"],
    "宇树科技概念": ["人形机器人"],
    "字节概念": ["AI应用"],
    "成飞概念": ["军工航天"],
    "拼多多概念": ["跨境电商"],
    "比亚迪概念": ["新能源汽车"],
    "鸿蒙生态": ["AI应用", "国产替代"],
}


def load(path: Path) -> tuple[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.pop("$note", ""), data


def dump(
    path: Path,
    note: str,
    data: dict,
    *,
    sort_values: bool = False,
    key_order=None,
) -> None:
    out = {"$note": note}
    for key in sorted(data, key=key_order):
        value = data[key]
        if sort_values and isinstance(value, list):
            value = sorted(dict.fromkeys(value))
        out[key] = value
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def add_edge(taxonomy: dict[str, list[str]], parent: str, child: str) -> None:
    taxonomy.setdefault(parent, [])
    if parent != child and child not in taxonomy[parent]:
        taxonomy[parent].append(child)


def remove_edge(taxonomy: dict[str, list[str]], parent: str, child: str) -> None:
    if parent in taxonomy:
        taxonomy[parent] = [name for name in taxonomy[parent] if name != child]


def merge_node(taxonomy: dict[str, list[str]], source: str, target: str) -> None:
    """把别名源节点的父子关系转移到规范名，然后移除源节点。"""
    for parent, children in taxonomy.items():
        if source not in children:
            continue
        taxonomy[parent] = [target if name == source else name for name in children]
    for child in taxonomy.pop(source, []):
        add_edge(taxonomy, target, child)


def retire(meta: dict, source: str, canonical: str | None = None) -> None:
    entry = meta.setdefault(source, {"type": "product", "status": "retired"})
    if canonical and canonical in meta:
        entry["type"] = meta[canonical]["type"]
    entry["status"] = "retired"
    entry.pop("virtual", None)


def main() -> int:
    meta_note, meta = load(META_PATH)
    tax_note, taxonomy = load(TAXONOMY_PATH)
    alias_note, aliases = load(ALIASES_PATH)
    expansion_note, expansions = load(EXPANSIONS_PATH)

    before_types: dict[str, int] = {}
    for value in meta.values():
        before_types[value["type"]] = before_types.get(value["type"], 0) + 1

    # 1) 类型语义：sector 收窄，原细分行业/产品进入 product，明确题材再覆盖。
    for name, value in meta.items():
        if value.get("type") == "sector" and name not in SECTOR_NAMES:
            value["type"] = "product"
    for name in SECTOR_NAMES:
        if name in meta and meta[name].get("status") != "retired":
            meta[name]["type"] = "sector"
    for name in THEME_NAMES:
        if name in meta and meta[name].get("status") != "retired":
            meta[name]["type"] = "theme"
    for name, value in meta.items():
        if value.get("status") == "retired" or value.get("type") in {"event", "catalyst"}:
            continue
        if (name.endswith(("产业链", "概念", "生态"))
                and "全产业链" not in name):
            value["type"] = "theme"

    for name, tag_type in VIRTUAL_NODES.items():
        entry = meta.setdefault(name, {})
        entry.update({"type": tag_type, "status": "active", "virtual": True})

    # 2) 严格同义词：配置别名、迁移树关系、退役源标签。
    for canonical, sources in STRICT_ALIASES.items():
        aliases.setdefault(canonical, [])
        for source in sources:
            if source not in aliases[canonical]:
                aliases[canonical].append(source)
            merge_node(taxonomy, source, canonical)
            retire(meta, source, canonical)

    # 3) 并列复合标签必须拆开，不作为独立题材继续统计。
    expansions["风电光伏"] = ["风电", "光伏"]
    for child in taxonomy.pop("风电光伏", []):
        add_edge(taxonomy, "风电", child)
    for parent in list(taxonomy):
        remove_edge(taxonomy, parent, "风电光伏")
    retire(meta, "风电光伏")

    # 4) 修复已确认的错误父子关系。
    remove_edge(taxonomy, "半导体设备", "工业母机")
    add_edge(taxonomy, "机械设备", "工业母机")
    for child in ("3D打印", "激光", "空分设备", "轻型输送带"):
        remove_edge(taxonomy, "工业母机", child)
    add_edge(taxonomy, "机械设备", "3D打印")
    add_edge(taxonomy, "机械设备", "激光")
    add_edge(taxonomy, "机械设备", "通用设备")
    add_edge(taxonomy, "机械设备", "工业零部件")
    add_edge(taxonomy, "机械设备", "工程机械")
    add_edge(taxonomy, "通用设备", "空分设备")
    add_edge(taxonomy, "工业零部件", "轻型输送带")

    remove_edge(taxonomy, "商业航天", "氦气")
    add_edge(taxonomy, "工业气体", "氦气")
    add_edge(taxonomy, "工业气体", "电子特气")

    for child in ("MLCC", "薄膜电容", "铝电解电容"):
        remove_edge(taxonomy, "半导体", child)
        add_edge(taxonomy, "电子元件", child)
    add_edge(taxonomy, "电子元件", "电子陶瓷")
    if "电子陶瓷" in meta:
        meta["电子陶瓷"]["type"] = "product"
        meta["电子陶瓷"]["status"] = "active"

    for child in ("显示面板", "光学光电子", "玻璃基板"):
        remove_edge(taxonomy, "半导体", child)
        add_edge(taxonomy, "显示与光电子", child)

    remove_edge(taxonomy, "机器人", "PEEK材料")
    add_edge(taxonomy, "化工新材料", "PEEK材料")

    # 企业生态中，“产业链/概念”进入题材树；供应商、客户和合作标签仍是属性。
    for theme, parents in THEME_PARENTS.items():
        if theme not in meta:
            continue
        meta[theme]["type"] = "theme"
        meta[theme]["status"] = "active"
        for parent in parents:
            add_edge(taxonomy, parent, theme)

    # 规范名经过别名迁移后再确保类型正确。
    for name in THEME_NAMES:
        if name in meta and meta[name].get("status") != "retired":
            meta[name]["type"] = "theme"

    # 清理重复、自环和空白名称。
    for parent, children in list(taxonomy.items()):
        taxonomy[parent] = sorted({child for child in children if child and child != parent})

    # 5) 旧 LLM 台账同步到新七类语义，避免治理台“一键采纳”把 product 改回 sector。
    if LLM_REVIEW_PATH.exists():
        ledger = json.loads(LLM_REVIEW_PATH.read_text(encoding="utf-8"))
        alias_to_canonical = {
            source: canonical
            for canonical, sources in aliases.items()
            for source in sources
        }
        valid_nodes = set(taxonomy) | {
            child for children in taxonomy.values() for child in children
        }
        for name, value in ledger.items():
            if not isinstance(value, dict):
                continue
            if (name in THEME_NAMES
                    or (name.endswith(("产业链", "概念", "生态"))
                        and "全产业链" not in name
                        and meta.get(name, {}).get("type") not in {"event", "catalyst"})):
                value["t"] = "theme"
            elif value.get("t") == "sector" and name not in SECTOR_NAMES:
                value["t"] = "product"
            parent = value.get("p", "")
            if parent in alias_to_canonical:
                parent = alias_to_canonical[parent]
            elif parent == "风电光伏":
                parent = "风电"
            if (parent not in valid_nodes
                    or meta.get(parent, {}).get("status") != "active"):
                parent = ""
            value["p"] = parent
        LLM_REVIEW_PATH.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        suggestions = {
            name: value["p"]
            for name, value in ledger.items()
            if isinstance(value, dict) and value.get("p")
        }
        PARENT_SUG_PATH.write_text(
            json.dumps(suggestions, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )

    meta_note = (
        "标签类型注册表。type: sector/product/theme/catalyst/attribute/event/unknown；"
        "sector=稳定大产业骨架，product=稳定细分行业/产品/技术/业务线，"
        "theme=可形成资金共振的交易叙事；status: active/candidate/retired。"
        "sector|product|theme 进入题材热力，catalyst 进入催化热力，其余不进。"
        "virtual=true 表示仅用于聚合导航、不对应原始涨停标签。"
        "2026-07-23按产业/题材边界完成第二轮存量复核。"
    )
    tax_note = (
        "正式 taxonomy 仅含 active 节点；父子必须同频道。"
        "sector/product/theme 共用题材频道，catalyst 单独频道；"
        "virtual 节点仅作聚合。产业是稳定骨架，题材是交易叙事，"
        "通用产品不得仅因一次共现挂进某个题材。"
    )

    dump(
        META_PATH,
        meta_note,
        meta,
        key_order=lambda name: (
            meta[name].get("status") != "active",
            meta[name].get("type", "unknown"),
            name,
        ),
    )
    dump(TAXONOMY_PATH, tax_note, taxonomy, sort_values=True)
    dump(ALIASES_PATH, alias_note, aliases, sort_values=True)
    dump(EXPANSIONS_PATH, expansion_note, expansions)

    after_types: dict[str, int] = {}
    for value in meta.values():
        after_types[value["type"]] = after_types.get(value["type"], 0) + 1
    print("迁移完成（仅 JSON 配置，尚未重建数据库）")
    print("类型迁移前：", " ".join(f"{k}={v}" for k, v in sorted(before_types.items())))
    print("类型迁移后：", " ".join(f"{k}={v}" for k, v in sorted(after_types.items())))
    print(f"taxonomy 节点：{len(set(taxonomy) | {c for xs in taxonomy.values() for c in xs})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
