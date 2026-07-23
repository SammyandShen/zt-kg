#!/usr/bin/env python3
"""
classify_tags.py — 用 claude CLI（无头模式，走订阅）让 LLM 复核标签七类分型。

范围：tag_meta.json 中全部 candidate + active 条目（retired 不动；
gen_tag_meta.OVERRIDES 里人工拍板的名字受保护不改）。每批 ~70 个标签，
附上下文（次数/家数/代表股/原始涨停原因样例）。
active 条目决定热力频道归属，改判门槛提高（置信度 ≥0.8），且结束时单独列明细。

结果处理：
- 置信度 ≥0.6 且类型合法 → 写回 tag_meta.type（status 保持 candidate，不进热度，
  只改善个股页/筛选页分区），并标 "llm" 字段记录复核模型
- 父节点建议不直接改 taxonomy（树是人工治理的），汇总存
  data/llm_parent_suggestions.json 供审后采纳
- 台账 data/llm_review.json 记录全部原始判定，断点续传靠它；--force 重判

用法：
  python3 scripts/classify_tags.py --limit 140   # 试跑2批看质量
  python3 scripts/classify_tags.py               # 全量（跑完自动 build_site 提示）
"""

import argparse
import json
import re
import subprocess
import sys

import common
from gen_tag_meta import OVERRIDES
from summarize_news import find_claude

META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
TAXONOMY_PATH = common.REPO_ROOT / "data" / "taxonomy.json"
LEDGER_PATH = common.REPO_ROOT / "data" / "llm_review.json"
PARENT_SUG_PATH = common.REPO_ROOT / "data" / "llm_parent_suggestions.json"

MODEL = "claude-sonnet-5"
TIMEOUT_SEC = 600
TYPES = {"sector", "product", "theme", "catalyst", "attribute", "event", "unknown"}

PROMPT_HEAD = """你是A股涨停原因标签的分类专家。把每个标签分到七类之一：
- sector: 稳定的大行业/产业骨架（如 半导体、医药医疗、汽车、金融）
- product: 稳定的细分行业、产品、技术、零部件或业务线（如 PCB、光模块、氦气、PEEK材料）
- theme: 可形成资金共振的市场题材（如 人形机器人、CPO、算力租赁、低空经济）
- catalyst: 触发涨停的事件类型，跨公司可复用（如 中报预增、定增获批、产品涨价、中标）
- attribute: 公司相对稳定的属性/身份（如 央企、山东国资、次新股、华为供应商、苹果供应商）
- event: 公司级、时点化的一次性事件（如 拟收购欧康诺、紫金矿业入主、拟10亿投建基地）
- unknown: 信息不足，无法判断

判断要点：
- sector 只给足以长期充当上层骨架的大产业，禁止把单个产品、材料、零部件判成 sector
- 产品名/细分行业名默认 product；只有形成跨公司的阶段性交易叙事时才判 theme
- 同一方向若兼有稳定业务和交易叙事，按标签措辞区分：XX供应商/客户→attribute，
  XX产业链/XX概念→theme；不得仅凭一次合作把公司永久归入某产业
- "XX运价""XX价格"类价格指标 → theme；"XX涨价" → catalyst
- 带具体公司名/具体金额的一次性动作 → event；"XX系""客户XX" → attribute
- 每个标签给 c=置信度(0~1)，拿不准就低置信度并 t=unknown，禁止硬猜

父节点：仅当类型为 sector/product/theme/catalyst 且能明确挂到下面"可选父节点"之一时填 p，否则 p 为空串。
可选父节点（只能从中选，不得自造）：
{NODES}

只输出一个严格 JSON 数组，元素形如 {"n":"标签名","t":"theme","p":"半导体","c":0.9}，
覆盖清单中每个标签，不要输出任何其他文字。

标签清单（含上下文）：
"""


def tag_context(conn, name: str) -> str:
    row = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT e.code) FROM event_concepts ec
        JOIN limit_up_events e ON e.id=ec.event_id
        JOIN concepts c ON c.id=ec.concept_id WHERE c.name=?""", (name,)).fetchone()
    total, stocks = row or (0, 0)
    names = [r[0] for r in conn.execute("""
        SELECT e.name FROM event_concepts ec
        JOIN limit_up_events e ON e.id=ec.event_id
        JOIN concepts c ON c.id=ec.concept_id WHERE c.name=?
        GROUP BY e.code ORDER BY COUNT(*) DESC LIMIT 3""", (name,))]
    reasons = [r[0] for r in conn.execute("""
        SELECT DISTINCT e.reason_type FROM event_concepts ec
        JOIN limit_up_events e ON e.id=ec.event_id
        JOIN concepts c ON c.id=ec.concept_id WHERE c.name=? LIMIT 2""", (name,))]
    return (f"{name} | {total}次/{stocks}股 | 代表股:{','.join(names) or '—'}"
            f" | 原因例:{' ; '.join((r or '')[:40] for r in reasons)}")


def parse_json_arr(txt: str) -> list:
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        raise ValueError(f"输出中未找到JSON数组: {txt[:200]}")
    return json.loads(m.group(0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前N个（试跑）")
    ap.add_argument("--batch", type=int, default=70)
    ap.add_argument("--force", action="store_true", help="已判过的也重判")
    ap.add_argument("--conf", type=float, default=0.6, help="写回tag_meta的置信度门槛")
    args = ap.parse_args()

    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    note = meta.pop("$note", "")
    tax = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    tax.pop("$note", None)
    nodes = sorted(set(tax) | {c for v in tax.values() for c in v})

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8")) if LEDGER_PATH.exists() else {}

    todo = [k for k, v in meta.items()
            if v.get("status") in ("candidate", "active") and k not in OVERRIDES
            and (args.force or k not in ledger)]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("无待复核标签（台账已覆盖，--force 可重判）")
        return 0
    print(f"待复核 {len(todo)} 条，每批 {args.batch}，模型 {MODEL}")

    conn = common.open_db()
    claude_bin = find_claude()
    head = PROMPT_HEAD.replace("{NODES}", "、".join(nodes))

    def save_all():
        out = {"$note": note}
        for k in sorted(meta, key=lambda x: (meta[x]["status"] != "active", meta[x]["type"], x)):
            out[k] = meta[k]
        META_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    n_applied = n_changed = n_lowconf = n_err = 0
    act_changes = []           # active 条目的改判明细（影响热力频道，需人工过目）
    for bi in range(0, len(todo), args.batch):
        batch = todo[bi:bi + args.batch]
        lines = [f"{i + 1}. {tag_context(conn, nm)}" for i, nm in enumerate(batch)]
        try:
            r = subprocess.run([claude_bin, "-p", "--model", MODEL],
                               input=head + "\n".join(lines),
                               capture_output=True, text=True, timeout=TIMEOUT_SEC)
            if r.returncode != 0:
                raise RuntimeError(f"claude 退出码 {r.returncode}: {r.stderr[:200]}")
            items = parse_json_arr(r.stdout)
        except Exception as e:
            print(f"❌ 批 {bi // args.batch + 1}: {e}", file=sys.stderr)
            n_err += 1
            continue

        valid = set(batch)
        for it in items:
            nm = str(it.get("n", "")).strip()
            t = str(it.get("t", "")).strip()
            p = str(it.get("p", "")).strip()
            try:
                c = float(it.get("c", 0))
            except (TypeError, ValueError):
                c = 0.0
            if nm not in valid or t not in TYPES:
                continue
            if p not in nodes:
                p = ""
            ledger[nm] = {"t": t, "p": p, "c": round(c, 2), "model": MODEL}
            is_active = meta[nm].get("status") == "active"
            need = max(args.conf, 0.8) if is_active else args.conf
            if c >= need and t != "unknown":
                if meta[nm]["type"] != t:
                    n_changed += 1
                    if is_active:
                        act_changes.append((nm, meta[nm]["type"], t, c))
                meta[nm]["type"] = t
                meta[nm]["llm"] = MODEL
                n_applied += 1
            else:
                n_lowconf += 1
        save_all()
        print(f"✅ 批 {bi // args.batch + 1}/{(len(todo) - 1) // args.batch + 1}"
              f"（累计 写回{n_applied} 改判{n_changed} 低置信{n_lowconf}）")

    # 父节点建议汇总（不自动改 taxonomy）
    sugg = {nm: v["p"] for nm, v in ledger.items() if v.get("p")}
    PARENT_SUG_PATH.write_text(json.dumps(sugg, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
    print(f"\n完成：写回 {n_applied}（其中改判 {n_changed}）、低置信保留 {n_lowconf}、"
          f"失败批 {n_err}；父节点建议 {len(sugg)} 条 → {PARENT_SUG_PATH.name}")
    if act_changes:
        print(f"⚠️ active 条目改判 {len(act_changes)} 条（影响热力频道归属，请过目）：")
        for nm, old, new, c in act_changes:
            print(f"   {nm}: {old} → {new} (conf={c:.2f})")
    print("下一步：python3 scripts/build_site.py 导出生效")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
