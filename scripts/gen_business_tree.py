#!/usr/bin/env python3
"""业务节点自动挂树：孤立 product 节点 → 产业父节点（零人工干预）。

背景（2026-08-03 审计）：年报候选晋升按名建独立 product 节点后没有归树工序，
2392/2416 节点孤立、92% 产品仅属一家公司，"哪些公司做同一产品/属于哪个产业"
答不上来。本脚本分批把孤立 product 节点喂 claude CLI（sonnet），归入产业父节点
（优先复用已有产业，没有合适的则提出简洁新产业名），台账 data/business_tree.json
防重、断点续传；拿不准记 null 留孤儿（--force 重问）。
rebuild_semantic_layer 每次以 source='auto_tree' 重放台账为 business_concept_edges，
bad case 直接改台账里的 parent 或删条目重问。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common

TREE_PATH = Path(__file__).parent.parent / "data" / "business_tree.json"
MODEL = "claude-sonnet-5"
TIMEOUT_SEC = 300
BATCH = 60

# 提示词里给的种子产业池：库内已有产业 + 常用A股大产业。模型优先复用，
# 实在没有贴合的才提新名；提过的新产业进台账后对后续批次即"已有"。
SEED_SECTORS = [
    "半导体", "消费电子", "电子元器件", "计算机软件", "通信设备",
    "汽车产业链", "新能源", "电力设备", "机械设备", "军工装备",
    "机器人产业", "低空经济与航天", "医药医疗", "化工新材料",
    "有色金属", "钢铁煤炭", "油气产业", "农林牧渔", "食品饮料",
    "大消费", "纺织服装", "轻工制造", "建筑建材", "房地产",
    "金融服务", "交通物流", "环保公用", "传媒互联网", "数字基础设施",
    "工业数字化", "工业设备零部件", "工程建设服务", "电子信息工程服务",
]

PROMPT_HEAD = """你在维护A股公司业务图谱的产业层级。下面是一批"产品/业务"节点名\
（来自上市公司年报的主营表述），请把每个归入一个"大产业"父节点。

规则：
1. 优先从【产业池】里选最贴合的一个；池里实在没有贴合的，才提出一个新产业名\
（2-8个汉字、通用大产业口径，如"半导体"，不要生僻组合词）。
2. 产业=稳定的大行业；不要把产品归入题材词（如"AI概念"）或属性词（如"国企"）。
3. 拿不准的填 null，宁可留空不错挂。
4. 只输出一个 JSON 对象：{"产品名": "产业名或null", ...}，不要多余文字。

【产业池】
%s

【待归类节点】
%s
"""


def find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for path in (
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.claude/local/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ):
        if os.path.exists(path):
            return path
    raise FileNotFoundError("找不到 claude CLI")


def load_tree() -> dict:
    if TREE_PATH.exists():
        return json.loads(TREE_PATH.read_text(encoding="utf-8"))
    return {
        "$note": "业务节点→产业父节点台账（gen_business_tree.py 生成，LLM判决留痕；"
                 "rebuild 以 source='auto_tree' 重放为 business_concept_edges）。"
                 "parent=null 表示拿不准留孤儿（--force 重问）；bad case 直接改 parent "
                 "或删条目次日重问。",
        "assignments": {},
    }


def save_tree(tree: dict) -> None:
    TREE_PATH.write_text(
        json.dumps(tree, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def orphan_products(conn) -> list[str]:
    return [r[0] for r in conn.execute("""
        SELECT b.name FROM business_concepts b
        WHERE b.concept_type='product' AND b.status='active'
          AND b.id NOT IN (SELECT child_id FROM business_concept_edges)
        ORDER BY (SELECT COUNT(DISTINCT f.code) FROM stock_business_facts f
                  WHERE f.business_concept_id=b.id AND f.status='verified') DESC,
                 b.name
    """)]


def call_llm(claude_bin: str, sectors: list[str], names: list[str]) -> dict:
    payload = PROMPT_HEAD % ("、".join(sectors), "\n".join(names))
    r = subprocess.run([claude_bin, "-p", "--model", MODEL],
                       input=payload, capture_output=True, text=True,
                       timeout=TIMEOUT_SEC)
    if r.returncode != 0:
        raise RuntimeError(f"claude 退出码 {r.returncode}: {r.stderr[:200]}")
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise ValueError(f"LLM 输出无 JSON: {r.stdout[:200]}")
    return json.loads(m.group(0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="本次最多处理的孤立节点数（每批 %d 一次 LLM 调用）" % BATCH)
    ap.add_argument("--force", action="store_true",
                    help="对台账里 parent=null 的节点重问")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = common.open_db()
    tree = load_tree()
    assigned = tree["assignments"]
    day = common.now_iso()[:10]

    known_sectors = list(dict.fromkeys(
        [r[0] for r in conn.execute(
            "SELECT name FROM business_concepts WHERE concept_type='sector' "
            "AND status='active'")]
        + [v["parent"] for v in assigned.values() if v.get("parent")]
        + SEED_SECTORS))

    pending = [n for n in orphan_products(conn)
               if n not in assigned
               or (args.force and assigned[n].get("parent") is None)]
    if not pending:
        print("✅ 无待挂树的孤立业务节点")
        return 0
    pending = pending[:args.limit]
    print(f"待挂树 {len(pending)} 个（台账已有 {len(assigned)} 条）")
    if args.dry_run:
        print("\n".join(pending[:20]))
        return 0

    claude_bin = find_claude()
    n_ok = n_null = 0
    for i in range(0, len(pending), BATCH):
        batch = pending[i:i + BATCH]
        try:
            result = call_llm(claude_bin, known_sectors, batch)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired,
                json.JSONDecodeError) as exc:
            print(f"⚠️ 批次失败（明日自动重试）：{exc}", file=sys.stderr)
            continue
        for name in batch:
            parent = result.get(name)
            if isinstance(parent, str):
                parent = parent.strip() or None
            if parent is not None and (
                    not isinstance(parent, str) or parent == name
                    or not (2 <= len(parent) <= 8)
                    or not re.fullmatch(r"[一-鿿A-Za-z0-9]+", parent)):
                print(f"  ⤷ 越界父节点已置空：{name} → {parent!r}")
                parent = None
            assigned[name] = {"parent": parent, "decided_by": "llm-sonnet",
                              "decided_at": day}
            if parent:
                n_ok += 1
                if parent not in known_sectors:
                    known_sectors.append(parent)
            else:
                n_null += 1
        save_tree(tree)                      # 每批落盘，断点续传
        print(f"  批次 {i // BATCH + 1}：挂树 {n_ok} / 留空 {n_null}（累计）")

    new_sectors = sorted({v["parent"] for v in assigned.values() if v.get("parent")}
                         - set(SEED_SECTORS))
    print(f"✅ 挂树完成：新增判决 {n_ok + n_null}（成功 {n_ok}，留空 {n_null}）；"
          f"产业清单 {len(new_sectors)} 个非种子产业")
    print("   跑 rebuild_semantic_layer.py 重放生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
