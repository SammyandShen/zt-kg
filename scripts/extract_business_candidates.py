#!/usr/bin/env python3
"""
extract_business_candidates.py — 从官方年报文本生成公司业务事实候选。

模型输出只写 business_fact_candidates(status=candidate)；conf≥0.7 的核心/重要
关系由次日 rebuild_semantic_layer 自动晋升，其余留作观察证据（零人工干预）。

用法：
  python3 scripts/extract_business_candidates.py --codes 002173,600962
  python3 scripts/extract_business_candidates.py --limit 10
  python3 scripts/extract_business_candidates.py --codes 002173 --force
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import common

MODEL = "claude-sonnet-5"
TIMEOUT_SEC = 600
SECTION_KEYWORDS = (
    "公司主要业务", "主要业务", "主营业务", "营业收入构成", "分行业",
    "分产品", "主要产品", "核心业务", "经营模式", "收入和成本",
)
VALID_FACT_TYPES = {
    "sector", "subindustry", "product", "service", "technology", "growth",
}
VALID_RELATIONS = {
    "core", "secondary", "research", "holding", "supply_chain",
    "planned_acquisition",
}
VALID_MATURITY = {
    "core_revenue", "commercialized", "early_revenue", "research",
    "holding", "proposed", "unknown",
}
# LLM 常见的同义/越界写法归一到枚举，避免整条候选被拒（拒绝=白烧一次调用）
MATURITY_ALIASES = {
    "commercial": "commercialized", "operational": "commercialized",
    "mature": "core_revenue", "core": "core_revenue",
    "secondary": "commercialized", "early": "early_revenue",
    "rnd": "research", "r&d": "research", "developing": "research",
    "planned": "proposed", "pending": "proposed",
}
RELATION_ALIASES = {
    "non_core": "secondary", "noncore": "secondary", "minor": "secondary",
    "main": "core", "primary": "core",
    "investment": "holding", "equity": "holding", "participation": "holding",
    "acquisition": "planned_acquisition",
    "supplier": "supply_chain", "customer": "supply_chain",
    "upstream": "supply_chain", "downstream": "supply_chain",
}
FACT_TYPE_ALIASES = {
    "industry": "sector", "business": "product", "segment": "subindustry",
    "tech": "technology", "material": "product", "equipment": "product",
    # LLM 偶发把关系维度写进 fact_type：归到最通用的 product
    "supply_chain": "product", "core": "product",
}

PROMPT = """你是上市公司业务事实审校员。请只根据下面的年度报告原文，提取公司客观业务事实。

要求：
- 只提取公司本体或明确控股子公司的现有业务；联营/参股必须标 holding
- 不得把市场概念、行业热点、客户所在行业、一次性事件写成公司主营
- 拟收购且尚未完成必须标 planned_acquisition + proposed
- 核心收入业务标 core + core_revenue；已有产品但非核心收入可标 secondary
- 研发阶段标 research + research，不能写成已商业化
- tag_name 使用简洁稳定的行业/产品/服务名，不带“龙头、概念、涨价、受益”等叙事
- summary 说明业务边界；claim 必须是原文中能直接支持该事实的短句或数据，最多120字
- 信息不足宁可少报。confidence范围0~1
- 只输出 JSON 数组，不要Markdown：
[{{"tag_name":"","fact_type":"product","relation_type":"core",
  "maturity":"core_revenue","confidence":0.9,"summary":"","claim":""}}]

股票：{code} {name}
报告：{title}

年报摘录：
{excerpt}
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


def focused_excerpt(text: str, max_chars: int = 24000) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if any(keyword in line for keyword in SECTION_KEYWORDS):
            selected.update(range(max(0, index - 8), min(len(lines), index + 65)))
    if not selected:
        return "\n".join(lines[:800])[:max_chars]
    chunks = []
    previous = -2
    for index in sorted(selected):
        if index > previous + 1:
            chunks.append("\n--- 摘录分隔 ---\n")
        chunks.append(lines[index])
        previous = index
    return "\n".join(chunks)[:max_chars]


def parse_json_array(output: str) -> list[dict]:
    match = re.search(r"\[.*\]", output, re.S)
    if not match:
        raise ValueError(f"模型输出中没有JSON数组：{output[:300]}")
    rows = json.loads(match.group(0))
    if not isinstance(rows, list):
        raise ValueError("模型输出不是数组")
    return rows


def validate_candidate(raw: dict) -> dict:
    tag = str(raw.get("tag_name") or "").strip()
    fact_type = str(raw.get("fact_type") or "").strip()
    relation = str(raw.get("relation_type") or "").strip()
    maturity = str(raw.get("maturity") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    claim = str(raw.get("claim") or "").strip()
    confidence = float(raw.get("confidence", 0))
    if not tag or not summary or not claim:
        raise ValueError("候选缺少 tag_name/summary/claim")
    fact_type = FACT_TYPE_ALIASES.get(fact_type, fact_type)
    if fact_type not in VALID_FACT_TYPES:
        raise ValueError(f"{tag}: 非法 fact_type={fact_type}")
    relation = RELATION_ALIASES.get(relation, relation)
    if relation not in VALID_RELATIONS:
        raise ValueError(f"{tag}: 非法 relation_type={relation}")
    maturity = MATURITY_ALIASES.get(maturity, maturity)
    if maturity not in VALID_MATURITY:
        maturity = "unknown"        # 归一失败降级 unknown，不整条拒绝
    if relation == "planned_acquisition" and maturity != "proposed":
        raise ValueError(f"{tag}: 拟收购必须是 proposed")
    return {
        "tag_name": tag[:60],
        "fact_type": fact_type,
        "relation_type": relation,
        "maturity": maturity,
        "confidence": max(0, min(1, confidence)),
        "summary": summary[:500],
        "claim": claim[:500],
    }


def heuristic_candidates(text: str) -> list[dict]:
    """无模型时的保守兜底：只识别年报中明确的主营/主导产品句式。"""
    compact = re.sub(r"\s+", "", text)
    rules = [
        (
            re.compile(r"公司(?:是)?一家以提供(.{2,30}?)为主营业务"),
            "service", "core", "core_revenue", 0.72,
        ),
        (
            re.compile(r"公司主营业务为(.{2,40}?)[，。；]"),
            "product", "core", "core_revenue", 0.7,
        ),
        (
            re.compile(r"公司的主营业务为(.{2,40}?)[，。；]"),
            "product", "core", "core_revenue", 0.7,
        ),
        (
            re.compile(r"公司主营(.{2,35}?)的采选和销售(?:，|。|；|截至)"),
            "product", "core", "core_revenue", 0.7,
        ),
        (
            re.compile(
                r"公司主要业务未发生变化，经营范围主要是(.{2,80}?)[，。；]"
            ),
            "product", "core", "core_revenue", 0.72,
        ),
        (
            re.compile(r"(?:公司)?主导产品为(.{2,35}?)[，。；]"),
            "product", "core", "commercialized", 0.68,
        ),
        (
            re.compile(r"公司(?:的)?主要产品为(.{2,35}?)[，。；]"),
            "product", "core", "commercialized", 0.68,
        ),
        (
            re.compile(
                r"公司(?:是)?专业从事(.{2,35}?)(?:的)?"
                r"(?:研发、生产及销售|研发、生产和销售|研发、生产、销售)"
            ),
            "product", "core", "core_revenue", 0.72,
        ),
        (
            re.compile(r"公司主营\s*(.{2,15}?)\s*业务为"),
            "service", "core", "core_revenue", 0.7,
        ),
        (
            re.compile(r"公司(数据中心)业务涵盖"),
            "service", "core", "core_revenue", 0.7,
        ),
        (
            re.compile(r"公司(医药研发与制造)业务涵盖"),
            "service", "core", "core_revenue", 0.7,
        ),
        (
            re.compile(r"公司深耕“(.{2,40}?)”三大核心业务"),
            "service", "core", "core_revenue", 0.72,
        ),
        (
            re.compile(
                r"公司是一家以.{2,40}?为核心技术.{0,100}?"
                r"致力成为领先的(.{2,30}?)解决方案提供商"
            ),
            "service", "core", "core_revenue", 0.68,
        ),
        (
            re.compile(r"公司确立了双主业的发展模式：(.{2,20}?)经营性主业"),
            "service", "core", "core_revenue", 0.68,
        ),
    ]
    found: dict[tuple[str, str], dict] = {}
    for pattern, fact_type, relation, maturity, confidence in rules:
        for match in pattern.finditer(compact):
            raw_tag = match.group(1)
            tag = re.sub(
                r"(的)?(?:生产和销售|生产、销售|生产销售|研发和销售|"
                r"研发、生产及销售|研发、生产和销售|研发、生产、销售|"
                r"采选和销售)$", "", raw_tag
            )
            tag = tag.replace("（浆）", "").replace("(浆)", "").strip("，。、；：")
            if (
                tag in {"业务", "主营", "产品", "服务"}
                or any(bad in tag for bad in (
                    "业务数据", "统计口径", "毛利率", "盈利能力",
                    "相关的业务", "履约义务", "同业竞争", "主营业务均",
                    "业务为", "主营",
                ))
            ):
                continue
            if not 2 <= len(tag) <= 30:
                continue
            candidate_fact_type = "service" if "服务" in tag else fact_type
            start = max(0, match.start() - 16)
            end = min(len(compact), match.end() + 70)
            claim = compact[start:end]
            summary = (
                f"年报明确披露公司主营/主导业务为“{tag}”；"
                "该条由确定性句式规则生成，需人工复核业务边界和收入重要性。"
            )
            key = (tag, relation)
            candidate = {
                "tag_name": tag,
                "fact_type": candidate_fact_type,
                "relation_type": relation,
                "maturity": maturity,
                "confidence": confidence,
                "summary": summary,
                "claim": claim,
            }
            if key not in found or candidate["confidence"] > found[key]["confidence"]:
                found[key] = candidate
    return list(found.values())


def select_reports(conn, args) -> list[tuple]:
    where = ["r.status='extracted'", "r.text_path IS NOT NULL"]
    params: list = []
    if args.codes:
        codes = list(dict.fromkeys(re.findall(r"\d{6}", args.codes)))
        where.append("r.code IN (" + ",".join("?" for _ in codes) + ")")
        params.extend(codes)
    if not args.force:
        where.append("""
          NOT EXISTS (
            SELECT 1 FROM business_fact_candidates b
            WHERE b.code=r.code AND b.report_year=r.report_year
          )
        """)
    # 近5交易日涨停过的股票优先出队：每日限量下先补当下正在异动的公司
    sql = f"""
        SELECT r.code,s.name,r.report_year,r.title,r.url,r.published_at,r.text_path
        FROM company_reports r JOIN stocks s ON s.code=r.code
        WHERE {' AND '.join(where)}
        ORDER BY (r.code IN (
            SELECT DISTINCT code FROM limit_up_events
            WHERE trade_date >= (
              SELECT MIN(d) FROM (SELECT DISTINCT trade_date AS d
                FROM limit_up_events ORDER BY trade_date DESC LIMIT 5)
            )
        )) DESC, r.report_year DESC, r.code
    """
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    return conn.execute(sql, params).fetchall()


def store_candidates(conn, report: tuple, candidates: list[dict],
                     extractor: str, force: bool) -> int:
    code, name, year, title, url, published, text_rel = report
    if force:
        conn.execute(
            "DELETE FROM business_fact_candidates "
            "WHERE code=? AND report_year=? AND status='candidate'",
            (code, year),
        )
    now = common.now_iso()
    for row in candidates:
        evidence_key = (
            f"cninfo_report:{code}:{year}:"
            + re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", row["tag_name"])[:50]
        )
        common_evidence = {
            "evidence_key": evidence_key,
            "evidence_type": "report",
            "source_name": "巨潮资讯",
            "title": title,
            "url": url,
            "published_at": published,
            "subject_status": "direct",
            "claim": row["claim"],
            "reliability": 0.9,
        }
        # 复用语义层的幂等证据写入规则，避免两套证据格式漂移。
        from rebuild_semantic_layer import upsert_evidence
        upsert_evidence(conn, common_evidence, code=code, name=name)
        conn.execute(
            """
            INSERT INTO business_fact_candidates(
              code,report_year,tag_name,fact_type,relation_type,maturity,status,
              confidence,summary,evidence_key,extractor,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'candidate',?,?,?,?,?,?)
            ON CONFLICT(code,report_year,tag_name,relation_type) DO UPDATE SET
              fact_type=excluded.fact_type,maturity=excluded.maturity,
              confidence=excluded.confidence,summary=excluded.summary,
              evidence_key=excluded.evidence_key,extractor=excluded.extractor,
              updated_at=excluded.updated_at
            """,
            (
                code, year, row["tag_name"], row["fact_type"],
                row["relation_type"], row["maturity"], row["confidence"],
                row["summary"], evidence_key, extractor, now, now,
            ),
        )
    conn.commit()
    return len(candidates)


def extract_with_claude(conn, claude_bin: str, report: tuple, force: bool) -> int:
    code, name, year, title, url, published, text_rel = report
    text_path = common.REPO_ROOT / text_rel
    excerpt = focused_excerpt(text_path.read_text(encoding="utf-8", errors="ignore"))
    prompt = PROMPT.format(code=code, name=name, title=title, excerpt=excerpt)
    result = subprocess.run(
        [claude_bin, "-p", "--model", MODEL],
        input=prompt, capture_output=True, text=True, timeout=TIMEOUT_SEC,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise RuntimeError(detail or f"claude退出码{result.returncode}")
    # 单条候选越界只丢该条：拒掉整只=白烧一次完整LLM调用
    candidates = []
    for row in parse_json_array(result.stdout):
        try:
            candidates.append(validate_candidate(row))
        except (ValueError, TypeError) as exc:
            print(f"  ⤷ 跳过越界候选：{exc}")
    return store_candidates(conn, report, candidates, MODEL, force)


def extract_with_heuristic(conn, report: tuple, force: bool) -> int:
    text_path = common.REPO_ROOT / report[6]
    rows = [
        validate_candidate(row)
        for row in heuristic_candidates(
            text_path.read_text(encoding="utf-8", errors="ignore")
        )
    ]
    return store_candidates(conn, report, rows, "heuristic-v1", force)


def another_instance_running() -> bool:
    r = subprocess.run(
        ["pgrep", "-f", "extract_business_candidates.py"], capture_output=True,
        text=True,
    )
    pids = [p for p in r.stdout.split() if p.strip() and int(p) != os.getpid()]
    return bool(pids)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--extractor", choices=("auto", "claude", "heuristic"), default="auto",
        help="auto会在Claude未登录/不可用时回退到确定性规则",
    )
    args = parser.parse_args()
    if another_instance_running():
        print("ℹ️ 已有另一个抽取实例在运行，本次跳过（避免重复烧调用/写库竞争）")
        return 0
    conn = common.open_db()
    reports = select_reports(conn, args)
    if not reports:
        print("ℹ️ 没有待提取年报")
        return 0
    claude_bin = None
    if args.extractor in ("auto", "claude"):
        try:
            claude_bin = find_claude()
        except FileNotFoundError:
            if args.extractor == "claude":
                raise
    use_claude = claude_bin is not None and args.extractor != "heuristic"
    errors = 0
    consecutive_fail = 0
    for index, report in enumerate(reports, 1):
        try:
            if use_claude:
                # 年报文本抽取失败（扫描版PDF等）会导致每天白烧一次调用：
                # 近空文本直接标 no_text 永久出队，等 bad case 或更好来源
                text_path = common.REPO_ROOT / report[6]
                if len(text_path.read_text(encoding="utf-8",
                                           errors="ignore").strip()) < 1000:
                    conn.execute(
                        "UPDATE company_reports SET status='no_text' "
                        "WHERE code=? AND report_year=?", (report[0], report[2]))
                    conn.commit()
                    print(f"⏭️ [{index}/{len(reports)}] {report[0]}: "
                          "年报文本为空/近空（扫描版？），标记 no_text 出队")
                    continue
                # 瞬时错误按只重试；只有连续多只都失败才判定 Claude 当前不可用。
                # 失败股票留在待抽队列（不写规则空行占位），明日自动重试。
                # ValueError=模型输出JSON格式错，输出是随机的，重问通常就好
                count = None
                last_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        count = extract_with_claude(
                            conn, claude_bin, report, args.force
                        )
                        extractor = "Claude"
                        consecutive_fail = 0
                        break
                    except (RuntimeError, ValueError) as exc:
                        last_exc = exc
                        if attempt < 2:
                            time.sleep(20 * (attempt + 1))
                if count is None:
                    errors += 1
                    consecutive_fail += 1
                    print(f"❌ [{index}/{len(reports)}] {report[0]}: "
                          f"Claude 3次重试均失败（留待明日）：{last_exc}",
                          file=sys.stderr)
                    if consecutive_fail >= 3:
                        if args.extractor == "claude":
                            raise RuntimeError(f"连续{consecutive_fail}只失败：{last_exc}")
                        print("⚠️ 连续3只失败，判定Claude当前不可用，"
                              "终止本批（剩余留在队列明日重试）")
                        break
                    continue
            else:
                count = extract_with_heuristic(conn, report, args.force)
                extractor = "规则"
            print(
                f"✅ [{index}/{len(reports)}] {report[0]} {report[2]}年："
                f"{count}条候选（{extractor}）"
            )
        except Exception as exc:
            errors += 1
            print(f"❌ [{index}/{len(reports)}] {report[0]}: {exc}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
