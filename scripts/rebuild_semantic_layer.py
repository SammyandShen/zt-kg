#!/usr/bin/env python3
"""
rebuild_semantic_layer.py — 重建“公司业务事实 + 单次涨停题材 + 题材轮次 + 证据”派生层。

边界：
- limit_up_events.reason_type / event_concepts 仍是供应商原始线索，永不在这里改写。
- 自动生成的 event_theme_links 只标 candidate，不能冒充已核实涨停原因。
- verified 业务事实与人工归因来自版本化 JSON；重建时保留人工记录、覆盖自动候选。
- 自动题材轮次只依据已存在的候选关系与盘面宽度，状态一律 provisional/closed，
  共同催化未核实前不会升级为 verified。

用法：
  python3 scripts/rebuild_semantic_layer.py --dry-run
  python3 scripts/rebuild_semantic_layer.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import common

TAG_META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
BUSINESS_FACTS_PATH = common.REPO_ROOT / "data" / "business_facts.json"
ATTRIBUTIONS_PATH = common.REPO_ROOT / "data" / "event_attributions.json"
THEME_BUSINESS_PATH = common.REPO_ROOT / "data" / "theme_business_mappings.json"
SOURCE_PRIORITY = {"ths": 0, "wencai": 1, "kpl": 2}


def load_json_list(path: Path, key: str) -> list[dict]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"{path.name}.{key} 必须是数组")
    return rows


def load_tag_meta() -> dict[str, dict]:
    if not TAG_META_PATH.exists():
        return {}
    raw = json.loads(TAG_META_PATH.read_text(encoding="utf-8"))
    raw.pop("$note", None)
    return raw


def preferred_events(conn) -> list[tuple]:
    """与 build_site.py 相同的来源优先级，每个(日,股)只取一条封板记录。"""
    best: dict[tuple[str, str], tuple[int, tuple]] = {}
    rows = conn.execute(
        "SELECT id, trade_date, code, name, reason_type, source "
        "FROM limit_up_events WHERE pool='zt' ORDER BY trade_date, code"
    ).fetchall()
    for row in rows:
        eid, d, code, _name, _reason, source = row
        rank = SOURCE_PRIORITY.get(source, 99)
        key = (d, code)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, row)
    return [value[1] for value in best.values()]


def upsert_evidence(conn, raw: dict, code: str | None = None,
                    name: str | None = None) -> int:
    key = str(raw.get("evidence_key") or "").strip()
    claim = str(raw.get("claim") or "").strip()
    if not key or not claim:
        raise ValueError("证据必须包含 evidence_key 和 claim")
    now = common.now_iso()
    conn.execute(
        """
        INSERT INTO evidence_items(
          evidence_key,evidence_type,source_name,title,url,published_at,
          subject_code,subject_name,subject_status,claim,reliability,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(evidence_key) DO UPDATE SET
          evidence_type=excluded.evidence_type,
          source_name=excluded.source_name,
          title=excluded.title,
          url=excluded.url,
          published_at=excluded.published_at,
          subject_code=excluded.subject_code,
          subject_name=excluded.subject_name,
          subject_status=excluded.subject_status,
          claim=excluded.claim,
          reliability=excluded.reliability
        """,
        (
            key, raw.get("evidence_type", "unknown"), raw.get("source_name"),
            raw.get("title"), raw.get("url"), raw.get("published_at"),
            raw.get("subject_code") or code, raw.get("subject_name") or name,
            raw.get("subject_status", "unknown"), claim,
            float(raw.get("reliability", 0)), now,
        ),
    )
    return conn.execute(
        "SELECT id FROM evidence_items WHERE evidence_key=?", (key,)
    ).fetchone()[0]


def import_business_facts(conn, stock_names: dict[str, str]) -> int:
    rows = load_json_list(BUSINESS_FACTS_PATH, "facts")
    seen_keys: set[tuple[str, str, str, str]] = set()
    now = common.now_iso()
    imported = 0
    for raw in rows:
        code = str(raw.get("code") or "").strip()
        tag = str(raw.get("tag_name") or "").strip()
        relation = str(raw.get("relation_type") or "").strip()
        valid_from = str(raw.get("valid_from") or "")
        if code not in stock_names:
            raise ValueError(f"business_facts 未知股票代码：{code}")
        if not tag or not relation:
            raise ValueError(f"business_facts {code} 缺少 tag_name/relation_type")
        key = (code, tag, relation, valid_from)
        if key in seen_keys:
            raise ValueError(f"business_facts 重复：{key}")
        seen_keys.add(key)
        conn.execute(
            """
            INSERT INTO stock_business_facts(
              code,tag_name,fact_type,relation_type,maturity,status,confidence,
              summary,valid_from,valid_to,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code,tag_name,relation_type,valid_from) DO UPDATE SET
              fact_type=excluded.fact_type,
              maturity=excluded.maturity,
              status=excluded.status,
              confidence=excluded.confidence,
              summary=excluded.summary,
              valid_to=excluded.valid_to,
              source=excluded.source,
              updated_at=excluded.updated_at
            """,
            (
                code, tag, raw.get("fact_type", "product"), relation,
                raw.get("maturity", "unknown"), raw.get("status", "candidate"),
                float(raw.get("confidence", 0)), raw.get("summary"), valid_from,
                raw.get("valid_to"), "manual", now, now,
            ),
        )
        fact_id = conn.execute(
            "SELECT id FROM stock_business_facts "
            "WHERE code=? AND tag_name=? AND relation_type=? AND valid_from=?",
            key,
        ).fetchone()[0]
        conn.execute("DELETE FROM business_fact_evidence WHERE fact_id=?", (fact_id,))
        for evidence in raw.get("evidence", []):
            evidence_id = upsert_evidence(
                conn, evidence, code=code, name=stock_names[code]
            )
            conn.execute(
                "INSERT OR IGNORE INTO business_fact_evidence VALUES(?,?)",
                (fact_id, evidence_id),
            )
        imported += 1

    # JSON 是人工事实真源：已从文件删除的 manual 记录同步移除，自动记录不动。
    manual_rows = conn.execute(
        "SELECT id,code,tag_name,relation_type,COALESCE(valid_from,'') "
        "FROM stock_business_facts WHERE source='manual'"
    ).fetchall()
    for fact_id, code, tag, relation, valid_from in manual_rows:
        if (code, tag, relation, valid_from) not in seen_keys:
            conn.execute("DELETE FROM stock_business_facts WHERE id=?", (fact_id,))
    return imported


def import_event_evidence(conn, events: list[tuple]) -> int:
    """导入原始原因、已抓新闻和LLM摘要，但都不自动证明某个题材。"""
    n = 0
    for eid, d, code, name, reason, _source in events:
        if reason:
            evidence_id = upsert_evidence(
                conn,
                {
                    "evidence_key": f"ths_reason:{eid}",
                    "evidence_type": "ths_reason",
                    "source_name": "同花顺",
                    "title": f"{d} 涨停原因原始标签",
                    "published_at": d,
                    "subject_status": "direct",
                    "claim": reason,
                    "reliability": 0.45,
                },
                code,
                name,
            )
            conn.execute(
                "INSERT OR REPLACE INTO event_evidence VALUES(?,?,?,?)",
                (eid, evidence_id, "unknown", "供应商候选线索，不等于已核实原因"),
            )
            n += 1

        for news_id, title, url, source, pub, snippet in conn.execute(
            "SELECT id,title,url,source,pub_time,snippet FROM news "
            "WHERE code=? AND trade_date=?",
            (code, d),
        ):
            direct = name in (title or "") or code in (title or "")
            evidence_id = upsert_evidence(
                conn,
                {
                    "evidence_key": f"news:{news_id}",
                    "evidence_type": "news",
                    "source_name": source,
                    "title": title,
                    "url": url,
                    "published_at": pub,
                    "subject_status": "direct" if direct else "unknown",
                    "claim": (snippet or title or "")[:500],
                    "reliability": 0.65 if direct else 0.4,
                },
                code,
                name,
            )
            conn.execute(
                "INSERT OR REPLACE INTO event_evidence VALUES(?,?,?,?)",
                (
                    eid, evidence_id, "unknown",
                    "标题点名股票" if direct else "尚未确认新闻主体与股票关系",
                ),
            )
            n += 1

        brief = conn.execute(
            "SELECT brief,model,created_at FROM briefs WHERE code=? AND trade_date=?",
            (code, d),
        ).fetchone()
        if brief:
            text, model, created = brief
            evidence_id = upsert_evidence(
                conn,
                {
                    "evidence_key": f"brief:{code}:{d}",
                    "evidence_type": "llm_summary",
                    "source_name": model,
                    "title": f"{d} LLM一句话归因",
                    "published_at": created,
                    "subject_status": "direct",
                    "claim": text,
                    "reliability": 0.25,
                },
                code,
                name,
            )
            conn.execute(
                "INSERT OR REPLACE INTO event_evidence VALUES(?,?,?,?)",
                (eid, evidence_id, "context", "模型摘要仅作辅助阅读，不作为核实证据"),
            )
            n += 1
    return n


def import_theme_business_mappings(conn, tag_meta: dict[str, dict]) -> int:
    """导入题材→业务标签映射；它生成候选池，不生成单次涨停归因。"""
    rows = load_json_list(THEME_BUSINESS_PATH, "mappings")
    seen: set[tuple[int, str]] = set()
    now = common.now_iso()
    imported = 0
    available_business_tags = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT tag_name FROM stock_business_facts "
            "WHERE status NOT IN ('rejected','expired')"
        )
    }
    for raw in rows:
        theme = str(raw.get("theme") or "").strip()
        business_tag = str(raw.get("business_tag_name") or "").strip()
        if not theme or not business_tag:
            raise ValueError("theme_business_mappings 缺少 theme/business_tag_name")
        meta = tag_meta.get(theme) or {}
        if meta.get("type") != "theme" or meta.get("status") != "active":
            raise ValueError(
                f"theme_business_mappings 的题材必须是 active/theme：{theme}"
            )
        if business_tag not in available_business_tags:
            raise ValueError(
                f"theme_business_mappings 找不到有效公司业务事实：{business_tag}"
            )
        cid = common.get_or_create_concept(conn, theme, {})
        key = (cid, business_tag)
        if key in seen:
            raise ValueError(f"theme_business_mappings 重复：{theme} → {business_tag}")
        seen.add(key)
        conn.execute(
            """
            INSERT INTO theme_business_mappings(
              concept_id,business_tag_name,mapping_type,status,confidence,
              rationale,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(concept_id,business_tag_name) DO UPDATE SET
              mapping_type=excluded.mapping_type,
              status=excluded.status,
              confidence=excluded.confidence,
              rationale=excluded.rationale,
              source=excluded.source,
              updated_at=excluded.updated_at
            """,
            (
                cid, business_tag, raw.get("mapping_type", "exact"),
                raw.get("status", "candidate"), float(raw.get("confidence", 0)),
                raw.get("rationale"), "manual", now, now,
            ),
        )
        imported += 1

    manual_rows = conn.execute(
        "SELECT concept_id,business_tag_name FROM theme_business_mappings "
        "WHERE source='manual'"
    ).fetchall()
    for cid, business_tag in manual_rows:
        if (cid, business_tag) not in seen:
            conn.execute(
                "DELETE FROM theme_business_mappings "
                "WHERE concept_id=? AND business_tag_name=?",
                (cid, business_tag),
            )
    return imported


def derive_candidate_theme_links(conn, events: list[tuple],
                                 tag_meta: dict[str, dict]) -> int:
    """只从 active/theme 原因标签生成低置信候选，不生成公司业务事实。"""
    active_themes = {
        name for name, meta in tag_meta.items()
        if meta.get("type") == "theme" and meta.get("status") == "active"
    }
    if not active_themes:
        return 0
    event_ids = {row[0] for row in events}
    event_info = {row[0]: row for row in events}
    rows = conn.execute(
        "SELECT ec.event_id,ec.concept_id,c.name "
        "FROM event_concepts ec JOIN concepts c ON c.id=ec.concept_id"
    ).fetchall()
    eligible = [
        row for row in rows if row[0] in event_ids and row[2] in active_themes
    ]
    breadth: Counter[tuple[str, int]] = Counter()
    for eid, cid, _name in eligible:
        breadth[(event_info[eid][1], cid)] += 1

    now = common.now_iso()
    for eid, cid, name in eligible:
        d, code, stock_name = (
            event_info[eid][1], event_info[eid][2], event_info[eid][3]
        )
        same_day = breadth[(d, cid)]
        confidence = min(0.55, 0.35 + min(3, max(0, same_day - 1)) * 0.05)
        brief = conn.execute(
            "SELECT brief FROM briefs WHERE code=? AND trade_date=?", (code, d)
        ).fetchone()
        if brief and name in brief[0]:
            confidence = min(0.55, confidence + 0.05)
        matched_news: list[int] = []
        for news_id, title, snippet in conn.execute(
            "SELECT id,title,snippet FROM news WHERE code=? AND trade_date=?",
            (code, d),
        ):
            haystack = f"{title or ''}\n{snippet or ''}"
            # 只有新闻同时点名题材，且标题或正文点名股票时，才可作为这条
            # “某次涨停×某题材”的旁证。它仍只提高候选置信度，不自动核实。
            direct_stock = stock_name in haystack or code in haystack
            if name in haystack and direct_stock:
                evidence = conn.execute(
                    "SELECT id FROM evidence_items WHERE evidence_key=?",
                    (f"news:{news_id}",),
                ).fetchone()
                if evidence:
                    matched_news.append(evidence[0])
        if matched_news:
            confidence = min(0.7, confidence + min(0.15, 0.08 * len(matched_news)))
        conn.execute(
            """
            INSERT OR IGNORE INTO event_theme_links(
              event_id,concept_id,theme_role,relation_type,status,confidence,
              rationale,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                eid, cid, "candidate", "unverified", "candidate", confidence,
                "原始涨停原因包含已审核交易题材；尚未核实共同催化和公司关联。",
                "derived", now, now,
            ),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO event_theme_evidence VALUES(?,?,?)",
            [(eid, cid, evidence_id) for evidence_id in matched_news],
        )
    return len(eligible)


def find_preferred_event(conn, code: str, trade_date: str) -> tuple | None:
    rows = conn.execute(
        "SELECT id,trade_date,code,name,reason_type,source FROM limit_up_events "
        "WHERE code=? AND trade_date=? AND pool='zt'",
        (code, trade_date),
    ).fetchall()
    if not rows:
        return None
    return min(rows, key=lambda row: SOURCE_PRIORITY.get(row[5], 99))


def import_manual_attributions(conn, stock_names: dict[str, str]) -> int:
    rows = load_json_list(ATTRIBUTIONS_PATH, "attributions")
    seen: set[tuple[int, int]] = set()
    now = common.now_iso()
    n = 0
    for raw in rows:
        code = str(raw.get("code") or "").strip()
        d = str(raw.get("trade_date") or "").strip()
        theme = str(raw.get("theme") or "").strip()
        event = find_preferred_event(conn, code, d)
        if not event:
            raise ValueError(f"event_attributions 找不到封板事件：{code} {d}")
        if not theme:
            raise ValueError(f"event_attributions {code} {d} 缺少 theme")
        eid = event[0]
        cid = common.get_or_create_concept(conn, theme, {})
        key = (eid, cid)
        if key in seen:
            raise ValueError(f"event_attributions 重复：{code} {d} {theme}")
        seen.add(key)
        conn.execute(
            """
            INSERT INTO event_theme_links(
              event_id,concept_id,theme_role,relation_type,market_role,status,
              confidence,rationale,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id,concept_id) DO UPDATE SET
              theme_role=excluded.theme_role,
              relation_type=excluded.relation_type,
              market_role=excluded.market_role,
              status=excluded.status,
              confidence=excluded.confidence,
              rationale=excluded.rationale,
              source=excluded.source,
              updated_at=excluded.updated_at
            """,
            (
                eid, cid, raw.get("theme_role", "primary"),
                raw.get("relation_type", "unverified"), raw.get("market_role"),
                raw.get("status", "candidate"), float(raw.get("confidence", 0)),
                raw.get("rationale"), "manual", now, now,
            ),
        )
        # 证据必须绑定到“某次涨停 × 某个题材”。先清掉该归因上一次导入的
        # 绑定，避免旧证据在人工配置变更后残留。
        conn.execute(
            "DELETE FROM event_theme_evidence WHERE event_id=? AND concept_id=?",
            (eid, cid),
        )
        for evidence in raw.get("evidence", []):
            evidence_id = upsert_evidence(
                conn, evidence, code=code, name=stock_names.get(code)
            )
            conn.execute(
                "INSERT OR REPLACE INTO event_evidence VALUES(?,?,?,?)",
                (eid, evidence_id, "context", f"“{theme}”归因的上下文证据"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO event_theme_evidence VALUES(?,?,?)",
                (eid, cid, evidence_id),
            )
        n += 1

    # JSON 是人工归因真源：改名或删除后，旧 manual 关系必须同步移除，
    # 否则同一涨停会同时残留新旧题材。
    manual_rows = conn.execute(
        "SELECT event_id,concept_id FROM event_theme_links WHERE source='manual'"
    ).fetchall()
    for eid, cid in manual_rows:
        if (eid, cid) not in seen:
            conn.execute(
                "DELETE FROM event_theme_links WHERE event_id=? AND concept_id=?",
                (eid, cid),
            )
    return n


def split_activity_groups(rows: list[tuple], date_index: dict[str, int]) -> list[list[tuple]]:
    groups: list[list[tuple]] = []
    current: list[tuple] = []
    last_index: int | None = None
    for row in sorted(rows, key=lambda item: (item[1], item[2])):
        idx = date_index[row[1]]
        if current and last_index is not None and idx - last_index > 2:
            groups.append(current)
            current = []
        current.append(row)
        last_index = idx
    if current:
        groups.append(current)
    return groups


def derive_episodes(conn, events: list[tuple]) -> int:
    event_info = {row[0]: row for row in events}
    all_dates = sorted({row[1] for row in events})
    if not all_dates:
        return 0
    date_index = {d: i for i, d in enumerate(all_dates)}
    latest_date = all_dates[-1]
    links_by_concept: dict[int, list[tuple]] = defaultdict(list)
    for eid, cid, status, source in conn.execute(
        "SELECT event_id,concept_id,status,source FROM event_theme_links "
        "WHERE status!='rejected'"
    ):
        if eid in event_info:
            links_by_concept[cid].append(
                (eid, event_info[eid][1], event_info[eid][2], status, source)
            )

    now = common.now_iso()
    n = 0
    for cid, rows in links_by_concept.items():
        for group in split_activity_groups(rows, date_index):
            codes = {row[2] for row in group}
            daily = Counter(row[1] for row in group)
            if len(codes) < 2 or max(daily.values()) < 2:
                continue
            start, end = min(daily), max(daily)
            current = daily[end]
            peak = max(daily.values())
            active_dates = sorted(daily)
            if end != latest_date:
                phase, status = "recession", "closed"
            elif len(active_dates) <= 2:
                phase, status = "startup", "provisional"
            elif current >= 4 and current == peak:
                phase, status = "climax", "provisional"
            elif len(active_dates) >= 2 and current > daily[active_dates[-2]]:
                phase, status = "fermentation", "provisional"
            elif current < peak:
                phase, status = "divergence", "provisional"
            else:
                phase, status = "fermentation", "provisional"
            has_verified = any(row[3] == "verified" for row in group)
            confidence = min(
                0.75,
                0.38 + min(5, len(codes)) * 0.025
                + min(5, peak) * 0.025 + (0.1 if has_verified else 0),
            )
            conn.execute(
                """
                INSERT INTO theme_episodes(
                  concept_id,start_date,end_date,phase,status,catalyst_summary,
                  confidence,source,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(concept_id,start_date) DO UPDATE SET
                  end_date=excluded.end_date,
                  phase=excluded.phase,
                  status=excluded.status,
                  catalyst_summary=excluded.catalyst_summary,
                  confidence=excluded.confidence,
                  source=excluded.source,
                  updated_at=excluded.updated_at
                """,
                (
                    cid, start, end, phase, status,
                    "待核实共同催化；当前轮次仅由候选题材关系和多股盘面响应生成。",
                    confidence, "derived", now, now,
                ),
            )
            episode_id = conn.execute(
                "SELECT id FROM theme_episodes WHERE concept_id=? AND start_date=?",
                (cid, start),
            ).fetchone()[0]
            event_ids = [row[0] for row in group]
            conn.executemany(
                "UPDATE event_theme_links SET episode_id=?,updated_at=? "
                "WHERE event_id=? AND concept_id=?",
                [(episode_id, now, eid, cid) for eid in event_ids],
            )
            placeholders = ",".join("?" for _ in event_ids)
            evidence_ids = conn.execute(
                f"SELECT DISTINCT evidence_id FROM event_theme_evidence "
                f"WHERE concept_id=? AND event_id IN ({placeholders})",
                [cid, *event_ids],
            ).fetchall()
            conn.executemany(
                "INSERT OR IGNORE INTO theme_episode_evidence VALUES(?,?)",
                [(episode_id, evidence_id) for (evidence_id,) in evidence_ids],
            )
            n += 1
    return n


def audit(conn) -> dict[str, int]:
    return {
        "business_facts": conn.execute(
            "SELECT COUNT(*) FROM stock_business_facts"
        ).fetchone()[0],
        "verified_business_facts": conn.execute(
            "SELECT COUNT(*) FROM stock_business_facts WHERE status='verified'"
        ).fetchone()[0],
        "theme_links": conn.execute(
            "SELECT COUNT(*) FROM event_theme_links"
        ).fetchone()[0],
        "verified_theme_links": conn.execute(
            "SELECT COUNT(*) FROM event_theme_links WHERE status='verified'"
        ).fetchone()[0],
        "episodes": conn.execute(
            "SELECT COUNT(*) FROM theme_episodes"
        ).fetchone()[0],
        "theme_business_mappings": conn.execute(
            "SELECT COUNT(*) FROM theme_business_mappings WHERE status!='rejected'"
        ).fetchone()[0],
        "evidence": conn.execute(
            "SELECT COUNT(*) FROM evidence_items"
        ).fetchone()[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="预演并回滚数据行变更")
    args = parser.parse_args()

    conn = common.open_db()
    tag_meta = load_tag_meta()
    stock_names = dict(conn.execute("SELECT code,name FROM stocks"))
    events = preferred_events(conn)

    conn.execute("BEGIN")
    try:
        # 只删除可重建的自动派生记录；人工业务事实和人工归因保留并由 JSON 同步。
        conn.execute("DELETE FROM event_theme_links WHERE source='derived'")
        conn.execute("DELETE FROM theme_episodes WHERE source='derived'")
        n_facts = import_business_facts(conn, stock_names)
        n_mappings = import_theme_business_mappings(conn, tag_meta)
        n_evidence = import_event_evidence(conn, events)
        n_candidates = derive_candidate_theme_links(conn, events, tag_meta)
        n_manual = import_manual_attributions(conn, stock_names)
        n_episodes = derive_episodes(conn, events)
        counts = audit(conn)
        if args.dry_run:
            conn.rollback()
            mode = "dry-run（已回滚）"
        else:
            conn.commit()
            mode = "已写入"
        print(
            f"✅ 语义层{mode}：人工业务事实 {n_facts}，自动题材候选 {n_candidates}，"
            f"人工涨停归因 {n_manual}，题材业务映射 {n_mappings}，"
            f"题材轮次 {n_episodes}，导入事件证据 {n_evidence}"
        )
        print(
            "   当前汇总：" + "；".join(f"{key}={value}" for key, value in counts.items())
        )
        return 0
    except Exception:
        conn.rollback()
        raise


if __name__ == "__main__":
    sys.exit(main())
