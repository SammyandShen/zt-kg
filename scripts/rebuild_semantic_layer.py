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
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import common

TAG_META_PATH = common.REPO_ROOT / "data" / "tag_meta.json"
BUSINESS_FACTS_PATH = common.REPO_ROOT / "data" / "business_facts.json"
ATTRIBUTIONS_PATH = common.REPO_ROOT / "data" / "event_attributions.json"
THEME_BUSINESS_PATH = common.REPO_ROOT / "data" / "theme_business_mappings.json"
CONFIG_PATH = common.REPO_ROOT / "data" / "semantic_config.json"
OVERRIDES_PATH = common.REPO_ROOT / "data" / "facts_overrides.json"
BUSINESS_TREE_PATH = common.REPO_ROOT / "data" / "business_tree.json"
SOURCE_PRIORITY = {"ths": 0, "wencai": 1, "kpl": 2}

DEFAULT_CONFIG = {
    "episode": {"min_codes": 3, "min_same_day": 3, "first_seal_span_min": 90,
                "gap_days": 5, "overlap_warn": 0.5},
    "candidate_expire_days": 10,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = {"episode": {**DEFAULT_CONFIG["episode"]},
           "candidate_expire_days": raw.get(
               "candidate_expire_days", DEFAULT_CONFIG["candidate_expire_days"])}
    for key, value in (raw.get("episode") or {}).items():
        if not key.endswith("_note"):
            cfg["episode"][key] = value
    return cfg


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
        "SELECT id, trade_date, code, name, reason_type, source, first_time "
        "FROM limit_up_events WHERE pool='zt' ORDER BY trade_date, code"
    ).fetchall()
    for row in rows:
        eid, d, code, _name, _reason, source, _ft = row
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


def upsert_business_path(conn, raw: dict, tag: str, now: str) -> int:
    """导入公司业务层级，并返回事实指向的末级业务概念 id。"""
    path = raw.get("business_path")
    if not isinstance(path, list) or not path:
        raise ValueError(f"business_facts {tag} 缺少 business_path")
    nodes: list[int] = []
    for i, node in enumerate(path):
        if not isinstance(node, dict):
            raise ValueError(f"business_facts {tag}.business_path[{i}] 必须是对象")
        name = str(node.get("name") or "").strip()
        concept_type = str(node.get("type") or "").strip()
        if not name or concept_type not in {"sector", "product"}:
            raise ValueError(
                f"business_facts {tag}.business_path[{i}] 需要 name，"
                "type 只能是 sector/product"
            )
        if i == 0 and concept_type != "sector":
            raise ValueError(f"business_facts {tag}.business_path 必须从 sector 开始")
        conn.execute(
            """
            INSERT INTO business_concepts(
              name,concept_type,status,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(name,concept_type) DO UPDATE SET
              status='active',source='manual',updated_at=excluded.updated_at
            """,
            (name, concept_type, "active", "manual", now, now),
        )
        concept_id = conn.execute(
            "SELECT id FROM business_concepts WHERE name=? AND concept_type=?",
            (name, concept_type),
        ).fetchone()[0]
        nodes.append(concept_id)
    if str(path[-1].get("name") or "").strip() != tag:
        raise ValueError(
            f"business_facts {tag}.business_path 末级必须与 tag_name 相同"
        )
    for parent_id, child_id in zip(nodes, nodes[1:]):
        if parent_id == child_id:
            raise ValueError(f"business_facts {tag}.business_path 出现自环")
        conn.execute(
            """
            INSERT INTO business_concept_edges(
              parent_id,child_id,source,created_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(parent_id,child_id) DO UPDATE SET source='manual'
            """,
            (parent_id, child_id, "manual", now),
        )
    return nodes[-1]


def import_business_facts(conn, stock_names: dict[str, str]) -> int:
    rows = load_json_list(BUSINESS_FACTS_PATH, "facts")
    seen_keys: set[tuple[str, str, str, str]] = set()
    now = common.now_iso()
    imported = 0
    conn.execute("DELETE FROM business_concept_edges WHERE source='manual'")
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
        business_concept_id = upsert_business_path(conn, raw, tag, now)
        conn.execute(
            """
            INSERT INTO stock_business_facts(
              code,business_concept_id,tag_name,fact_type,relation_type,maturity,status,confidence,
              summary,valid_from,valid_to,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code,tag_name,relation_type,valid_from) DO UPDATE SET
              business_concept_id=excluded.business_concept_id,
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
                code, business_concept_id, tag, raw.get("fact_type", "product"), relation,
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
    # 候选所用的同一条年报证据已经进入人工事实时，标记为 accepted，
    # 避免治理台重复要求复核；其他同义或更细候选仍保持 candidate。
    conn.execute("""
        UPDATE business_fact_candidates
        SET status='accepted',updated_at=?
        WHERE status='candidate'
          AND evidence_key IN (
            SELECT DISTINCT e.evidence_key
            FROM business_fact_evidence be
            JOIN evidence_items e ON e.id=be.evidence_id
            JOIN stock_business_facts f ON f.id=be.fact_id
            WHERE f.source='manual' AND f.status='verified'
          )
    """, (now,))
    conn.execute(
        """
        DELETE FROM business_concepts
        WHERE source='manual'
          AND id NOT IN (
            SELECT business_concept_id FROM stock_business_facts
            WHERE business_concept_id IS NOT NULL
          )
          AND id NOT IN (SELECT parent_id FROM business_concept_edges)
          AND id NOT IN (SELECT child_id FROM business_concept_edges)
        """
    )
    return imported


def promote_business_candidates(conn) -> int:
    """年报业务候选自动晋升（零人工干预原则，2026-07-25 用户定）。

    年报是公司主营的权威来源：conf≥0.7 且 relation 为 core/secondary 的候选
    自动晋升为 verified 业务事实（source='auto_report' 留痕，与人工 manual 区分；
    bad case 由用户反馈后在 business_facts.json 覆盖或 rejected）。
    业务概念按名字匹配既有节点，没有则建独立 product 节点（挂树交给后续治理）。
    """
    now = common.now_iso()
    n = 0
    for (cand_id, code, year, tag, fact_type, relation, maturity, confidence,
         revenue_share, summary, evidence_key) in conn.execute(
            "SELECT id,code,report_year,tag_name,fact_type,relation_type,"
            "maturity,confidence,revenue_share,summary,evidence_key "
            "FROM business_fact_candidates "
            "WHERE status='candidate' AND confidence>=0.7 "
            "AND relation_type IN ('core','secondary')").fetchall():
        concept = conn.execute(
            "SELECT id FROM business_concepts WHERE name=? "
            "AND concept_type='product' LIMIT 1", (tag,)).fetchone()
        if concept:
            concept_id = concept[0]
        else:
            # 事实末级必须挂 product 节点（门禁强制）；标签名撞上 sector 节点
            # （如细分"印制电路板"）时建同名 product 并挂到该 sector 下
            conn.execute(
                "INSERT INTO business_concepts(name,concept_type,status,source,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (tag, "product", "active", "auto_report", now, now))
            concept_id = conn.execute(
                "SELECT id FROM business_concepts WHERE name=? AND concept_type='product'",
                (tag,)).fetchone()[0]
            sector = conn.execute(
                "SELECT id FROM business_concepts WHERE name=? "
                "AND concept_type='sector' AND status='active'", (tag,)).fetchone()
            if sector:
                conn.execute(
                    "INSERT OR IGNORE INTO business_concept_edges VALUES(?,?,?,?)",
                    (sector[0], concept_id, "auto_report", now))
        conn.execute(
            """
            INSERT INTO stock_business_facts(
              code,business_concept_id,tag_name,fact_type,relation_type,maturity,
              status,confidence,revenue_share,summary,valid_from,valid_to,source,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
            ON CONFLICT(code,tag_name,relation_type,valid_from) DO UPDATE SET
              business_concept_id=excluded.business_concept_id,
              maturity=excluded.maturity,
              confidence=excluded.confidence,
              revenue_share=COALESCE(excluded.revenue_share,
                                     stock_business_facts.revenue_share),
              summary=excluded.summary,
              updated_at=excluded.updated_at
            """,
            (code, concept_id, tag, fact_type, relation, maturity, "verified",
             confidence, revenue_share,
             f"[{year}年报自动晋升] {summary or ''}"[:300], "",
             "auto_report", now, now))
        fact_id = conn.execute(
            "SELECT id FROM stock_business_facts WHERE code=? AND tag_name=? "
            "AND relation_type=? AND valid_from=''",
            (code, tag, relation)).fetchone()[0]
        ev = conn.execute("SELECT id FROM evidence_items WHERE evidence_key=?",
                          (evidence_key,)).fetchone()
        if ev:
            conn.execute("INSERT OR IGNORE INTO business_fact_evidence VALUES(?,?)",
                         (fact_id, ev[0]))
        conn.execute(
            "UPDATE business_fact_candidates SET status='accepted',updated_at=? "
            "WHERE id=?", (now, cand_id))
        n += 1

    # 互动易供应链候选：以 status='candidate' 落入事实域——只进③层观察名单
    # 与题材候选池（导出过滤只排 rejected/expired），不进产业热力、不冒充主营。
    for (cand_id, code, tag, confidence, summary, evidence_key) in conn.execute(
            "SELECT id,code,tag_name,confidence,summary,evidence_key "
            "FROM business_fact_candidates "
            "WHERE status='candidate' AND relation_type='supply_chain' "
            "AND (extractor LIKE 'irm%' OR extractor LIKE 'sse%')").fetchall():
        concept = conn.execute(
            "SELECT id FROM business_concepts WHERE name=? "
            "ORDER BY concept_type='product' DESC LIMIT 1", (tag,)).fetchone()
        if concept and conn.execute(
                "SELECT concept_type FROM business_concepts WHERE id=?",
                (concept[0],)).fetchone()[0] == "product":
            concept_id = concept[0]
        else:
            # 词典无同名 product 节点（可能只有同名 sector，如产业池"半导体"）：
            # 建 product 节点——门禁要求事实末级必须挂同名 product
            conn.execute(
                "INSERT INTO business_concepts(name,concept_type,status,source,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name,concept_type) DO NOTHING",
                (tag, "product", "active", "auto_qa", now, now))
            concept_id = conn.execute(
                "SELECT id FROM business_concepts WHERE name=? AND concept_type='product'",
                (tag,)).fetchone()[0]
        conn.execute(
            """
            INSERT INTO stock_business_facts(
              code,business_concept_id,tag_name,fact_type,relation_type,maturity,
              status,confidence,summary,valid_from,valid_to,source,
              created_at,updated_at
            ) VALUES(?,?,?,'product','supply_chain','unknown','candidate',?,?,'',
                     NULL,'auto_qa',?,?)
            ON CONFLICT(code,tag_name,relation_type,valid_from) DO UPDATE SET
              confidence=MAX(excluded.confidence, stock_business_facts.confidence),
              business_concept_id=COALESCE(stock_business_facts.business_concept_id,
                                           excluded.business_concept_id),
              updated_at=excluded.updated_at
            """,
            (code, concept_id, tag, confidence, summary or "", now, now))
        fact_id = conn.execute(
            "SELECT id FROM stock_business_facts WHERE code=? AND tag_name=? "
            "AND relation_type='supply_chain' AND valid_from=''",
            (code, tag)).fetchone()[0]
        ev = conn.execute("SELECT id FROM evidence_items WHERE evidence_key=?",
                          (evidence_key,)).fetchone()
        if ev:
            conn.execute("INSERT OR IGNORE INTO business_fact_evidence VALUES(?,?)",
                         (fact_id, ev[0]))
        conn.execute(
            "UPDATE business_fact_candidates SET status='accepted',updated_at=? "
            "WHERE id=?", (now, cand_id))
        n += 1
    return n


def import_event_evidence(conn, events: list[tuple]) -> int:
    """导入原始原因、已抓新闻和LLM摘要，但都不自动证明某个题材。"""
    n = 0
    for eid, d, code, name, reason, _source, _ft in events:
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


def ensure_fact_nodes(conn) -> int:
    """门禁不变量兜底：任何有效业务事实必须挂同名 product 节点。

    历史来源漏建节点（如 auto_qa 首批 NULL）或新增来源遗漏时自动补齐并回填
    business_concept_id，不让发布线卡在单条脏数据上。
    """
    now = common.now_iso()
    n = 0
    for fact_id, tag in conn.execute(
            "SELECT f.id, f.tag_name FROM stock_business_facts f "
            "LEFT JOIN business_concepts bc ON bc.id=f.business_concept_id "
            "WHERE f.status NOT IN ('rejected','expired') "
            "AND (f.business_concept_id IS NULL "
            "     OR bc.concept_type!='product')").fetchall():
        # 撞名 sector（细分聚类后出现，如"印制电路板"）时同样重挂到同名 product
        conn.execute(
            "INSERT INTO business_concepts(name,concept_type,status,source,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(name,concept_type) DO NOTHING",
            (tag, "product", "active", "auto_repair", now, now))
        pid = conn.execute(
            "SELECT id FROM business_concepts WHERE name=? AND concept_type='product'",
            (tag,)).fetchone()[0]
        conn.execute(
            "UPDATE stock_business_facts SET business_concept_id=?, updated_at=? "
            "WHERE id=?", (pid, now, fact_id))
        sector = conn.execute(
            "SELECT id FROM business_concepts WHERE name=? "
            "AND concept_type='sector' AND status='active'", (tag,)).fetchone()
        if sector:
            conn.execute(
                "INSERT OR IGNORE INTO business_concept_edges VALUES(?,?,?,?)",
                (sector[0], pid, "auto_repair", now))
        n += 1
    return n


def import_business_tree(conn) -> int:
    """重放 LLM 挂树台账（business_tree.json → source='auto_tree' 层级边）。

    三层结构：groups 段=产业→细分（sector→sector 边，仅一级——细分的 parent
    不得又是别的细分）；assignments 段=产品→产业或细分。产品节点按名匹配
    （不存在的跳过，等节点出现后下次重放挂上）；父节点不存在则建 sector 节点
    （source='auto_tree'）。已有 manual 边的子节点不动——人工 business_path
    优先于自动挂树。
    """
    if not BUSINESS_TREE_PATH.exists():
        return 0
    tree = json.loads(BUSINESS_TREE_PATH.read_text(encoding="utf-8"))
    assignments = tree.get("assignments", {})
    groups = tree.get("groups", {})
    now = common.now_iso()
    conn.execute("DELETE FROM business_concept_edges WHERE source='auto_tree'")
    n = 0

    def sector_node(name: str) -> int:
        conn.execute(
            "INSERT INTO business_concepts(name,concept_type,status,source,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(name,concept_type) DO NOTHING",
            (name, "sector", "active", "auto_tree", now, now))
        return conn.execute(
            "SELECT id FROM business_concepts WHERE name=? AND concept_type='sector'",
            (name,)).fetchone()[0]

    for sub, v in groups.items():
        parent_name = v.get("parent")
        if (not parent_name or parent_name == sub
                or parent_name in groups):      # 中间层只允许一级
            print(f"⚠️ business_tree groups 越界（跳过）：{sub}→{parent_name}")
            continue
        conn.execute(
            "INSERT OR IGNORE INTO business_concept_edges"
            "(parent_id,child_id,source,created_at) VALUES(?,?,?,?)",
            (sector_node(parent_name), sector_node(sub), "auto_tree", now))
        n += 1
    for child_name, v in assignments.items():
        parent_name = v.get("parent")
        if not parent_name or parent_name == child_name:
            continue
        child = conn.execute(
            "SELECT id FROM business_concepts WHERE name=? "
            "AND concept_type='product' AND status='active'",
            (child_name,)).fetchone()
        if not child:
            continue
        if conn.execute(
                "SELECT 1 FROM business_concept_edges WHERE child_id=? LIMIT 1",
                (child[0],)).fetchone():
            continue                        # 人工路径已挂，不覆盖
        conn.execute(
            "INSERT INTO business_concepts(name,concept_type,status,source,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(name,concept_type) DO NOTHING",
            (parent_name, "sector", "active", "auto_tree", now, now))
        parent_id = conn.execute(
            "SELECT id FROM business_concepts WHERE name=? AND concept_type='sector'",
            (parent_name,)).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO business_concept_edges"
            "(parent_id,child_id,source,created_at) VALUES(?,?,?,?)",
            (parent_id, child[0], "auto_tree", now))
        n += 1
    return n


def graph_descendant_fact_tags(conn, node_name: str) -> list[str]:
    """业务图谱 sector 节点（产业/细分）→ 全部后代 product 的有效事实标签名。"""
    root = conn.execute(
        "SELECT id FROM business_concepts WHERE name=? AND concept_type='sector' "
        "AND status='active'", (node_name,)).fetchone()
    if not root:
        return []
    seen_ids = {root[0]}
    frontier = [root[0]]
    while frontier:
        nxt = [r[0] for r in conn.execute(
            f"SELECT child_id FROM business_concept_edges WHERE parent_id IN "
            f"({','.join('?' * len(frontier))})", frontier) if r[0] not in seen_ids]
        seen_ids.update(nxt)
        frontier = nxt
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT f.tag_name FROM stock_business_facts f "
        f"JOIN business_concepts b ON b.id=f.business_concept_id "
        f"WHERE b.id IN ({','.join('?' * len(seen_ids))}) "
        f"AND b.concept_type='product' "
        f"AND f.status NOT IN ('rejected','expired')", list(seen_ids))]


def import_theme_business_mappings(conn, tag_meta: dict[str, dict]) -> int:
    """导入题材→业务标签映射；它生成候选池，不生成单次涨停归因。

    图谱化（2026-08-04 阶段一）：映射目标可以是三层业务图谱的 sector 节点
    （产业/细分）——导入时展开为其后代产品的事实标签行（source='graph'，
    每次重建重生成），下游一切按 tag_name 连接的消费端零改动受益；
    sector 级原始行不入库（门禁要求映射行必须挂到有事实的叶子标签）。
    直接叶子映射优先：展开行 DO NOTHING 不覆盖同键的直接映射。
    """
    rows = load_json_list(THEME_BUSINESS_PATH, "mappings")
    seen: set[tuple[int, str]] = set()
    now = common.now_iso()
    imported = 0
    conn.execute("DELETE FROM theme_business_mappings WHERE source='graph'")
    available_business_tags = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT tag_name FROM stock_business_facts "
            "WHERE status NOT IN ('rejected','expired')"
        )
    }
    graph_rows: list[tuple] = []
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
            expanded = graph_descendant_fact_tags(conn, business_tag)
            if not expanded:
                raise ValueError(
                    f"theme_business_mappings 找不到有效公司业务事实或"
                    f"可展开的图谱节点：{business_tag}"
                )
            cid = common.get_or_create_concept(conn, theme, {})
            for leaf in expanded:
                graph_rows.append((
                    cid, leaf, raw.get("mapping_type", "exact"),
                    raw.get("status", "candidate"),
                    float(raw.get("confidence", 0)),
                    f"[图谱展开自 {business_tag}] {raw.get('rationale') or ''}"[:200],
                ))
            continue
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
    # 图谱展开行最后插：直接叶子映射（manual）优先，同键 DO NOTHING 不覆盖
    for cid, leaf, mtype, status, conf, rationale in graph_rows:
        cur = conn.execute(
            """
            INSERT INTO theme_business_mappings(
              concept_id,business_tag_name,mapping_type,status,confidence,
              rationale,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'graph',?,?)
            ON CONFLICT(concept_id,business_tag_name) DO NOTHING
            """,
            (cid, leaf, mtype, status, conf, rationale, now, now))
        imported += cur.rowcount
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

    # link_basis 凭据预载：题材→业务标签映射 + 各股有效业务事实。
    # 凭据回答"这家公司凭什么和这个题材有关"；无凭据的归因只能算盘面联想。
    theme_biz_tags: dict[int, set[str]] = defaultdict(set)
    for cid, biz_tag in conn.execute(
            "SELECT concept_id,business_tag_name FROM theme_business_mappings "
            "WHERE status!='rejected'"):
        theme_biz_tags[cid].add(biz_tag)
    facts_by_code: dict[str, list[tuple]] = defaultdict(list)
    for fid, code, tag, status, vfrom, vto in conn.execute(
            "SELECT id,code,tag_name,status,valid_from,valid_to "
            "FROM stock_business_facts WHERE status NOT IN ('rejected','expired')"):
        facts_by_code[code].append((fid, tag, status, vfrom or "", vto or ""))

    def find_basis(code: str, cid: int, theme: str, d: str,
                   announcement_ids: list[int]):
        """业务边优先（verified 优先），其次官方公告；都没有 → None。"""
        want = theme_biz_tags.get(cid, set()) | {theme}
        hits = [f for f in facts_by_code.get(code, ())
                if f[1] in want and f[3] <= d and (not f[4] or f[4] >= d)]
        if hits:
            best = min(hits, key=lambda f: (f[2] != "verified",))
            return "business_fact", best[0]
        if announcement_ids:
            return "announcement", announcement_ids[0]
        return None, None

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
        matched_evidence: list[int] = []
        matched_announcements: list[int] = []
        for evidence_id, evidence_type, title, claim, subject_status in conn.execute(
            """
            SELECT ee.evidence_id,e.evidence_type,e.title,e.claim,e.subject_status
            FROM event_evidence ee
            JOIN evidence_items e ON e.id=ee.evidence_id
            WHERE ee.event_id=? AND e.evidence_type IN ('news','announcement')
            """,
            (eid,),
        ):
            haystack = f"{title or ''}\n{claim or ''}"
            # 新闻需要同时点名题材与股票；官方公告已由证券代码精确查询，只需标题/
            # 原文明确点名题材。两者仍只作为旁证，不自动核实。
            direct_stock = (
                subject_status == "direct"
                and (
                    evidence_type == "announcement"
                    or stock_name in haystack
                    or code in haystack
                )
            )
            if name in haystack and direct_stock:
                matched_evidence.append(evidence_id)
                if evidence_type == "announcement":
                    matched_announcements.append(evidence_id)
        if matched_evidence:
            confidence = min(
                0.7, confidence + min(0.15, 0.08 * len(matched_evidence))
            )
        # link_basis：无业务边/公司事件凭据的归因只能算盘面联想，置信度封顶。
        basis_kind, basis_id = find_basis(code, cid, name, d, matched_announcements)
        if basis_kind == "business_fact":
            confidence = min(0.7, confidence + 0.05)
            rationale = "凭据=公司业务边；共同催化仍待核实。"
        elif basis_kind == "announcement":
            rationale = "凭据=官方公告点名题材；共同催化仍待核实。"
        else:
            confidence = min(confidence, 0.45)
            rationale = "仅盘面联想：无业务边/公司事件凭据，置信度封顶0.45。"
        conn.execute(
            """
            INSERT OR IGNORE INTO event_theme_links(
              event_id,concept_id,theme_role,relation_type,status,confidence,
              rationale,source,basis_kind,basis_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                eid, cid, "candidate", "unverified", "candidate", confidence,
                rationale, "derived", basis_kind, basis_id, now, now,
            ),
        )
        # T0 快照只在首次出现时写入（INSERT OR IGNORE），重建不覆盖——
        # 回测"按当晚归因跟随"必须用这里的点值。
        conn.execute(
            "INSERT OR IGNORE INTO attribution_snapshots VALUES(?,?,?,?)",
            (eid, cid, json.dumps({
                "theme_role": "candidate", "status": "candidate",
                "confidence": round(confidence, 3), "source": "derived",
                "basis": basis_kind,
            }, ensure_ascii=False), now),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO event_theme_evidence VALUES(?,?,?)",
            [(eid, cid, evidence_id) for evidence_id in matched_evidence],
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


def split_activity_groups(rows: list[tuple], date_index: dict[str, int],
                          gap_days: int, sustain_min: int,
                          decay_ratio: float) -> list[list[tuple]]:
    """按活跃日+动量衰减切分轮次（2026-08-03 修正）。

    活跃日 = 当日该题材 ≥sustain_min 只涨停。断轮时钟只认"够格的"活跃日：
    广度必须 ≥ max(sustain_min, ceil(本浪峰值×decay_ratio))——浪越高，
    尾部涓流（相对峰值衰减后的零星2-3只）越撑不住时钟，浪自然断开。
    旧规则任意1只涨停都刷新时钟，常青题材被粘成378天巨轮（人形机器人/
    固态电池/国企改革），启动点与阶段全部失真。断浪后峰值清零，下一浪从
    sustain_min 重新起步（新浪成立仍需过 min_codes+触发日双门槛）。
    轮次跨度=[首个够格日, 末个够格日]，跨度内的单股/涓流日随轮；
    跨度外的零散涨停不归组（属个股事件，走候选过期通道）。
    """
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        by_day[row[1]].append(row)
    day_breadth = {d: len({r[2] for r in rs}) for d, rs in by_day.items()}
    sustain_days = sorted(d for d, n in day_breadth.items() if n >= sustain_min)
    day_groups: list[list[str]] = []
    span: list[str] = []
    peak = 0
    for d in sustain_days:
        if span and date_index[d] - date_index[span[-1]] > gap_days:
            day_groups.append(span)
            span, peak = [], 0
        if day_breadth[d] >= max(sustain_min, math.ceil(peak * decay_ratio)):
            span.append(d)
            peak = max(peak, day_breadth[d])
    if span:
        day_groups.append(span)
    out: list[list[tuple]] = []
    for days in day_groups:
        lo, hi = days[0], days[-1]
        out.append([r for d in sorted(by_day) if lo <= d <= hi
                    for r in sorted(by_day[d], key=lambda item: item[2])])
    return out


def hhmm_minutes(ts) -> int | None:
    """first_time 时间戳 → 当日分钟数（本地时区按数据源约定）。"""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone, timedelta
        t = datetime.fromtimestamp(int(ts), tz=timezone(timedelta(hours=8)))
        return t.hour * 60 + t.minute
    except (ValueError, OSError, OverflowError):
        return None


def trigger_day_of(group: list[tuple], event_info: dict, cfg_ep: dict) -> str | None:
    """立轮触发日：首个同日家数≥min_same_day 且首封极差≤span 的交易日。

    缺首封时间的事件不参与极差判定；当日有效时间样本<2 时只按家数判定。
    """
    daily_events: dict[str, list[int]] = defaultdict(list)
    for row in group:
        daily_events[row[1]].append(row[0])
    for d in sorted(daily_events):
        eids = daily_events[d]
        if len({event_info[e][2] for e in eids}) < cfg_ep["min_same_day"]:
            continue
        minutes = [m for m in (hhmm_minutes(event_info[e][6]) for e in eids)
                   if m is not None]
        if len(minutes) >= 2 and max(minutes) - min(minutes) > cfg_ep["first_seal_span_min"]:
            continue
        return d
    return None


def derive_episodes(conn, events: list[tuple], cfg: dict) -> tuple[int, list[str]]:
    cfg_ep = cfg["episode"]
    event_info = {row[0]: row for row in events}
    all_dates = sorted({row[1] for row in events})
    if not all_dates:
        return 0, []
    date_index = {d: i for i, d in enumerate(all_dates)}
    latest_date = all_dates[-1]
    latest_idx = date_index[latest_date]
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
    open_members: dict[int, set[str]] = {}      # 开放轮次成员，用于重叠告警
    open_names: dict[int, str] = {}
    warnings: list[str] = []
    sustain_min = cfg_ep.get("sustain_min_members", 2)
    decay_ratio = cfg_ep.get("wave_decay_ratio", 0.25)
    for cid, rows in links_by_concept.items():
        for group in split_activity_groups(rows, date_index, cfg_ep["gap_days"],
                                           sustain_min, decay_ratio):
            codes = {row[2] for row in group}
            daily = Counter(row[1] for row in group)
            # 硬阈值立轮：整轮股票数 + 触发日（同日家数&首封极差）双门槛；
            # 不达标不立轮——单股/两股的公告驱动属于个股事件，不强行造题材。
            if len(codes) < cfg_ep["min_codes"]:
                continue
            trigger = trigger_day_of(group, event_info, cfg_ep)
            if trigger is None:
                continue
            start, end = min(daily), max(daily)
            current = daily[end]
            peak = max(daily.values())
            active_dates = sorted(daily)
            idle = latest_idx - date_index[end]   # 距最新交易日的静默天数
            if idle > cfg_ep["gap_days"]:
                phase, status = "recession", "closed"          # 退场：超窗关轮
            elif idle > 0:
                phase, status = "divergence", "provisional"    # 休整中，未关轮
            elif len(active_dates) <= 2:
                phase, status = "startup", "provisional"
            elif current >= 4 and current == peak:
                phase, status = "climax", "provisional"
            elif current > daily[active_dates[-2]]:
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
            # 同期开放轮次成员重叠告警：只提示人工并轮，不自动合并。
            if status == "provisional":
                for other_id, members in open_members.items():
                    denom = min(len(codes), len(members))
                    if denom and len(codes & members) / denom >= cfg_ep["overlap_warn"]:
                        warnings.append(
                            f"轮次重叠 ≥{int(cfg_ep['overlap_warn'] * 100)}%："
                            f"{concept_name(conn, cid)}#{episode_id} ×"
                            f" {open_names[other_id]}#{other_id}"
                            f"（交集{len(codes & members)}只）")
                open_members[episode_id] = codes
                open_names[episode_id] = concept_name(conn, cid)
            n += 1
    return n, warnings


def concept_name(conn, cid: int) -> str:
    row = conn.execute("SELECT name FROM concepts WHERE id=?", (cid,)).fetchone()
    return row[0] if row else f"cid{cid}"


import re

EVENT_TYPE_PATTERNS = [
    ("clarification", re.compile(r"澄清|不存在应披露|说明公告|异动公告|风险提示")),
    ("ma_intent", re.compile(r"收购|并购|重组|购买资产|吸收合并")),
    ("contract_win", re.compile(r"中标|中选|签订.*合同|框架协议")),
    ("earnings", re.compile(r"业绩|预增|预告|扭亏|分红|利润分配")),
    ("approval", re.compile(r"获批|批复|注册|许可|通过.*审核|受理")),
    ("invest", re.compile(r"投建|投资|扩产|设立|增资")),
    ("listing", re.compile(r"上市|发行|挂牌|定增|募集")),
]


def derive_corporate_events(conn) -> int:
    """公告 → 公司事件记录（可整体重建）。event 从此不再是标签词典成员。"""
    conn.execute("DELETE FROM corporate_events")
    now = common.now_iso()
    n = 0
    for evidence_id, code, title, published in conn.execute(
            "SELECT id,subject_code,title,COALESCE(published_at,'') "
            "FROM evidence_items WHERE evidence_type='announcement' "
            "AND subject_code IS NOT NULL"):
        text = title or ""
        etype = next((t for t, pat in EVENT_TYPE_PATTERNS if pat.search(text)),
                     "other")
        conn.execute(
            "INSERT OR IGNORE INTO corporate_events("
            "code,event_date,event_type,title,evidence_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (code, published[:10], etype, title, evidence_id, now))
        n += 1
    return n


def apply_clarification_kills(conn) -> int:
    """澄清击杀通道：澄清公告标题/原文点名题材 → 对应候选归因直接 rejected。

    保守规则：只杀"澄清公告明确出现题材名"的候选；泛泛的异动/风险提示不杀。
    窗口：涨停日前1个交易日 ～ 后3个自然日内发布的澄清。
    """
    now = common.now_iso()
    killed = 0
    for ce_id, code, ce_date, evidence_id in conn.execute(
            "SELECT id,code,event_date,evidence_id FROM corporate_events "
            "WHERE event_type='clarification'"):
        row = conn.execute(
            "SELECT COALESCE(title,''),COALESCE(claim,'') FROM evidence_items "
            "WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            continue
        text = row[0] + "\n" + row[1]
        if "澄清" not in text and "不存在" not in text:
            continue                       # 仅异动/风险提示，不构成否认
        for eid, cid, theme in conn.execute(
                """
                SELECT l.event_id,l.concept_id,c.name
                FROM event_theme_links l
                JOIN limit_up_events e ON e.id=l.event_id
                JOIN concepts c ON c.id=l.concept_id
                WHERE e.code=? AND l.source='derived'
                  AND l.status IN ('candidate','expired')
                  AND date(e.trade_date) >= date(?, '-4 day')
                  AND date(e.trade_date) <= date(?, '+1 day')
                """, (code, ce_date, ce_date)):
            if theme and theme in text:
                conn.execute(
                    "UPDATE event_theme_links SET status='rejected', "
                    "rationale=?, updated_at=? WHERE event_id=? AND concept_id=?",
                    (f"澄清公告点名否认（corporate_event#{ce_id}）",
                     now, eid, cid))
                killed += 1
    return killed


def load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {"link_verdicts": [], "episode_verdicts": [], "leaders": []}
    raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    return {
        "link_verdicts": raw.get("link_verdicts", []),
        "episode_verdicts": raw.get("episode_verdicts", []),
        "leaders": raw.get("leaders", []),
    }


def apply_link_verdicts(conn, overrides: dict) -> int:
    """判决重放（轮次归组前）：人工 verified/rejected 覆盖派生候选状态。"""
    now = common.now_iso()
    n = 0
    for v in overrides["link_verdicts"]:
        verdict = v.get("verdict")
        if verdict not in ("verified", "rejected"):
            raise ValueError(f"link_verdicts 非法 verdict：{v}")
        event = find_preferred_event(conn, v["code"], v["trade_date"])
        cid_row = conn.execute(
            "SELECT id FROM concepts WHERE name=?", (v["theme"],)).fetchone()
        if not event or not cid_row:
            print(f"⚠️ link_verdicts 找不到目标（跳过）：{v.get('code')} "
                  f"{v.get('trade_date')} {v.get('theme')}")
            continue
        if verdict == "verified":
            # 判决引用的叙事必须落成可追溯证据（2026-08-03）：把该事件的
            # 新闻/公告证据绑为题材级旁证（supporting 优先，至多3条）。
            # 一条都绑不上的 verified 不落地——保持 candidate 走过期通道，
            # 门禁才能对全部 verified 强制证据而不阻断发布。
            conn.execute(
                "INSERT OR IGNORE INTO event_theme_evidence"
                "(event_id,concept_id,evidence_id) "
                "SELECT ee.event_id, ?, ee.evidence_id FROM event_evidence ee "
                "WHERE ee.event_id=? AND ee.relevance_status!='rejected' "
                "ORDER BY CASE ee.relevance_status WHEN 'supporting' THEN 0 "
                "WHEN 'context' THEN 1 ELSE 2 END LIMIT 3",
                (cid_row[0], event[0]))
            if not conn.execute(
                    "SELECT 1 FROM event_theme_evidence "
                    "WHERE event_id=? AND concept_id=? LIMIT 1",
                    (event[0], cid_row[0])).fetchone():
                print(f"⚠️ verified 判决无可绑证据，保持 candidate："
                      f"{v['code']} {v['trade_date']} {v['theme']}")
                continue
        cur = conn.execute(
            "UPDATE event_theme_links SET status=?, "
            "theme_role=CASE WHEN ?='verified' THEN ? ELSE theme_role END, "
            "rationale=?, updated_at=? WHERE event_id=? AND concept_id=?",
            (verdict, verdict, v.get("role", "primary"),
             f"人工判决：{v.get('note') or verdict}（{v.get('decided_at', '')[:10]}）",
             now, event[0], cid_row[0]))
        n += cur.rowcount
    return n


def apply_episode_overrides(conn, overrides: dict) -> tuple[int, int]:
    """判决重放（轮次归组后）：轮次成立/否定 + 龙头认定。"""
    now = common.now_iso()
    n_ep = n_leader = 0
    def find_episode(theme: str, start: str):
        return conn.execute(
            "SELECT ep.id FROM theme_episodes ep JOIN concepts c ON c.id=ep.concept_id "
            "WHERE c.name=? AND ep.start_date=?", (theme, start)).fetchone()
    for v in overrides["episode_verdicts"]:
        verdict = v.get("verdict")
        if verdict not in ("verified", "rejected"):
            raise ValueError(f"episode_verdicts 非法 verdict：{v}")
        row = find_episode(v["theme"], v["start_date"])
        if not row:
            print(f"⚠️ episode_verdicts 找不到轮次（跳过）：{v.get('theme')} {v.get('start_date')}")
            continue
        conn.execute(
            "UPDATE theme_episodes SET status=?, "
            "catalyst_summary=COALESCE(?,catalyst_summary), updated_at=? WHERE id=?",
            (verdict, v.get("catalyst"), now, row[0]))
        n_ep += 1
    for v in overrides["leaders"]:
        row = find_episode(v["theme"], v["start_date"])
        if not row:
            print(f"⚠️ leaders 找不到轮次（跳过）：{v.get('theme')} {v.get('start_date')}")
            continue
        cur = conn.execute(
            "UPDATE event_theme_links SET market_role='leader', updated_at=? "
            "WHERE episode_id=? AND event_id IN "
            "(SELECT id FROM limit_up_events WHERE code=?)",
            (now, row[0], v["code"]))
        n_leader += cur.rowcount and 1
    return n_ep, n_leader


def corroborate_mappings(conn) -> int:
    """映射核实飞轮（2026-08-04 阶段一）：用自己的判决数据检验映射对错。

    一条"题材→业务标签"映射被 ≥3 只**不同股票**的 verified 归因作为业务凭据
    引用（basis_kind='business_fact' 且凭据事实的标签正是该映射的标签）
    → 自动转正 verified，rationale 追加留痕。纯确定性、每次重建幂等重算
    （导入会从 JSON 重置状态，本工序在判决重放后执行故结果稳定）。
    退场规则（题材归因活跃但映射长期零命中→retired）等第二层核实量到位后再开。
    """
    now = common.now_iso()
    n = 0
    for cid, tag, hits in conn.execute("""
        SELECT m.concept_id, m.business_tag_name, COUNT(DISTINCT e.code)
        FROM theme_business_mappings m
        JOIN event_theme_links l ON l.concept_id=m.concept_id
             AND l.status='verified' AND l.basis_kind='business_fact'
        JOIN stock_business_facts f ON f.id=l.basis_id
             AND f.tag_name=m.business_tag_name
        JOIN limit_up_events e ON e.id=l.event_id
        WHERE m.status='candidate'
        GROUP BY m.concept_id, m.business_tag_name
        HAVING COUNT(DISTINCT e.code) >= 3
    """).fetchall():
        conn.execute(
            "UPDATE theme_business_mappings SET status='verified', "
            "rationale=COALESCE(rationale,'')||'｜印证转正：'||?||'只已核实归因引用'"
            ", updated_at=? WHERE concept_id=? AND business_tag_name=?",
            (hits, now, cid, tag))
        n += 1
    return n


def derive_current_leaders(conn) -> int:
    """龙头当前口径（2026-08-03 修正）：每次重建全量重算，不存在过期龙头。

    开放轮次（provisional/verified）：只看轮次最新交易日在场的股票，取当日最高
    连板（首板不认，平局取当日首封最早）——回答"现在谁是龙头"；
    已关轮次（closed）：整轮最高板——回答"那一轮谁曾是龙头"（历史语义）。
    facts_overrides 的 leaders（人工/LLM bad case 覆盖）已先重放，标过的轮次跳过。
    """
    now = common.now_iso()
    n = 0
    for ep_id, status in conn.execute(
            "SELECT id, status FROM theme_episodes WHERE status!='rejected'"):
        if conn.execute(
                "SELECT 1 FROM event_theme_links WHERE episode_id=? "
                "AND market_role='leader' LIMIT 1", (ep_id,)).fetchone():
            continue
        day_filter = """AND e.trade_date=(
                SELECT MAX(e2.trade_date) FROM event_theme_links l2
                JOIN limit_up_events e2 ON e2.id=l2.event_id
                WHERE l2.episode_id=?)""" if status != "closed" else ""
        params = (ep_id, ep_id) if status != "closed" else (ep_id,)
        row = conn.execute(f"""
            SELECT e.code FROM event_theme_links l
            JOIN limit_up_events e ON e.id=l.event_id
            WHERE l.episode_id=? AND e.lb_count>=2 {day_filter}
            ORDER BY e.lb_count DESC, COALESCE(e.first_time,'99:99') ASC
            LIMIT 1""", params).fetchone()
        if not row:
            continue                        # 最新日全是首板/无成员 → 本轮暂无龙头
        conn.execute(
            "UPDATE event_theme_links SET market_role='leader', updated_at=? "
            "WHERE episode_id=? AND event_id IN "
            "(SELECT id FROM limit_up_events WHERE code=?)",
            (now, ep_id, row[0]))
        n += 1
    return n


def expire_stale_candidates(conn, events: list[tuple], cfg: dict) -> int:
    """退场：候选归因 N 个交易日内未进任何轮次、且无 supporting 旁证 → expired。"""
    all_dates = sorted({row[1] for row in events})
    days = cfg["candidate_expire_days"]
    if len(all_dates) <= days:
        return 0
    cutoff = all_dates[-days - 1]
    cur = conn.execute(
        """
        UPDATE event_theme_links SET status='expired', updated_at=?
        WHERE source='derived' AND status='candidate' AND episode_id IS NULL
          AND event_id IN (SELECT id FROM limit_up_events WHERE trade_date<=?)
          AND NOT EXISTS (
            SELECT 1 FROM attribution_reviews r
            WHERE r.event_id=event_theme_links.event_id
              AND r.concept_id=event_theme_links.concept_id
              AND r.verdict='supporting')
        """,
        (common.now_iso(), cutoff),
    )
    return cur.rowcount


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
        "expired_links": conn.execute(
            "SELECT COUNT(*) FROM event_theme_links WHERE status='expired'"
        ).fetchone()[0],
        "t0_snapshots": conn.execute(
            "SELECT COUNT(*) FROM attribution_snapshots"
        ).fetchone()[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="预演并回滚数据行变更")
    args = parser.parse_args()

    conn = common.open_db()
    tag_meta = load_tag_meta()
    cfg = load_config()
    stock_names = dict(conn.execute("SELECT code,name FROM stocks"))
    events = preferred_events(conn)

    conn.execute("BEGIN")
    try:
        # 只删除可重建的自动派生记录；人工业务事实和人工归因保留并由 JSON 同步。
        # attribution_snapshots 是 T0 点值台账，永不删除。
        conn.execute("DELETE FROM event_theme_links WHERE source='derived'")
        conn.execute("DELETE FROM theme_episodes WHERE source='derived'")
        n_facts = import_business_facts(conn, stock_names)
        n_promoted = promote_business_candidates(conn)
        n_repaired = ensure_fact_nodes(conn)
        n_tree = import_business_tree(conn)
        n_mappings = import_theme_business_mappings(conn, tag_meta)
        n_evidence = import_event_evidence(conn, events)
        n_candidates = derive_candidate_theme_links(conn, events, tag_meta)
        n_manual = import_manual_attributions(conn, stock_names)
        n_corp = derive_corporate_events(conn)
        n_killed = apply_clarification_kills(conn)   # 澄清击杀先于轮次归组
        overrides = load_overrides()
        n_verdicts = apply_link_verdicts(conn, overrides)  # 人工判决先于归组
        n_episodes, overlap_warnings = derive_episodes(conn, events, cfg)
        n_ep_verdicts, n_leaders = apply_episode_overrides(conn, overrides)
        n_corroborated = corroborate_mappings(conn)
        n_cur_leaders = derive_current_leaders(conn)
        n_expired = expire_stale_candidates(conn, events, cfg)
        # T+复核跨重建存活（自然键，无外键）；只清理链接永久消失的真孤儿
        n_orphan_reviews = conn.execute(
            "DELETE FROM attribution_reviews WHERE NOT EXISTS ("
            "  SELECT 1 FROM event_theme_links l"
            "  WHERE l.event_id=attribution_reviews.event_id"
            "    AND l.concept_id=attribution_reviews.concept_id)"
        ).rowcount
        counts = audit(conn)
        if args.dry_run:
            conn.rollback()
            mode = "dry-run（已回滚）"
        else:
            conn.commit()
            mode = "已写入"
        print(
            f"✅ 语义层{mode}：人工业务事实 {n_facts}（年报自动晋升 {n_promoted}，"
            f"节点兜底修复 {n_repaired}，自动挂树边 {n_tree}），"
            f"自动题材候选 {n_candidates}，"
            f"人工涨停归因 {n_manual}，题材业务映射 {n_mappings}，"
            f"题材轮次 {n_episodes}（阈值 ≥{cfg['episode']['min_codes']}只/"
            f"同日≥{cfg['episode']['min_same_day']}家/首封极差≤"
            f"{cfg['episode']['first_seal_span_min']}min），"
            f"候选过期 {n_expired}，公司事件 {n_corp}（澄清击杀 {n_killed}），"
            f"人工判决重放 链{n_verdicts}/轮{n_ep_verdicts}/龙头覆盖{n_leaders}，"
            f"当前龙头重算 {n_cur_leaders}，映射印证转正 {n_corroborated}，"
            f"导入事件证据 {n_evidence}，清理孤儿复核 {n_orphan_reviews}"
        )
        for w in overlap_warnings[:10]:
            print(f"⚠️ {w}")
        print(
            "   当前汇总：" + "；".join(f"{key}={value}" for key, value in counts.items())
        )
        return 0
    except Exception:
        conn.rollback()
        raise


if __name__ == "__main__":
    sys.exit(main())
