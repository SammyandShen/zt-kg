#!/usr/bin/env python3
"""
review_theme_attributions.py — 对候选涨停题材执行 T0/T+1/T+2 确定性复核。

复核只生成旁证强度和人工审核建议，绝不自动把 event_theme_links 升级为 verified。
T+1/T+2 按库内后续交易日计算，不按自然日。

用法：
  python3 scripts/review_theme_attributions.py --days 5
  python3 scripts/review_theme_attributions.py --date 2026-07-23
"""

from __future__ import annotations

import argparse
import sys

import common

RELATION_BONUS = {
    "core": 0.12,
    "secondary": 0.09,
    "research": 0.05,
    "holding": 0.06,
    "supply_chain": 0.06,
    "planned_acquisition": 0.04,
}
RELATION_PRIORITY = {
    "core": 6,
    "secondary": 5,
    "research": 4,
    "holding": 3,
    "supply_chain": 2,
    "planned_acquisition": 1,
}


def theme_codes(conn, concept_id: int, trade_date: str | None) -> set[str]:
    if not trade_date:
        return set()
    return {
        row[0] for row in conn.execute("""
            SELECT DISTINCT e.code
            FROM event_theme_links l
            JOIN limit_up_events e ON e.id=l.event_id
            WHERE l.concept_id=? AND e.trade_date=?
              AND e.pool='zt' AND l.status!='rejected'
        """, (concept_id, trade_date))
    }


def business_relation(conn, concept_id: int, code: str) -> str | None:
    rows = [
        row[0] for row in conn.execute("""
            SELECT f.relation_type
            FROM theme_business_mappings m
            JOIN stock_business_facts f ON f.tag_name=m.business_tag_name
            WHERE m.concept_id=? AND f.code=?
              AND m.status!='rejected'
              AND f.status NOT IN ('rejected','expired')
        """, (concept_id, code))
    ]
    return max(rows, key=lambda value: RELATION_PRIORITY.get(value, 0)) if rows else None


def review_link(conn, row: tuple, dates: list[str],
                date_index: dict[str, int], latest_date: str) -> None:
    event_id, concept_id, code, trade_date, base_confidence = row
    index = date_index[trade_date]
    age = len(dates) - 1 - index
    stage = min(2, max(0, age))
    next_date = dates[index + 1] if index + 1 < len(dates) else None
    t2_date = dates[index + 2] if index + 2 < len(dates) else None

    day_codes = theme_codes(conn, concept_id, trade_date)
    next_codes = theme_codes(conn, concept_id, next_date)
    t2_codes = theme_codes(conn, concept_id, t2_date)
    retained = day_codes & next_codes
    retained_rate = len(retained) / len(day_codes) if day_codes else 0
    evidence_count, avg_reliability = conn.execute("""
        SELECT COUNT(*),COALESCE(AVG(e.reliability),0)
        FROM event_theme_evidence ete
        JOIN evidence_items e ON e.id=ete.evidence_id
        WHERE ete.event_id=? AND ete.concept_id=?
    """, (event_id, concept_id)).fetchone()
    relation = business_relation(conn, concept_id, code)

    score = float(base_confidence)
    if evidence_count:
        score += min(0.16, 0.08 + 0.04 * (evidence_count - 1))
        if avg_reliability >= 0.75:
            score += 0.03
    if len(day_codes) >= 5:
        score += 0.12
    elif len(day_codes) >= 3:
        score += 0.08
    elif len(day_codes) >= 2:
        score += 0.04
    if stage >= 1 and len(next_codes) >= 2:
        score += 0.05
    if stage >= 1 and retained_rate >= 0.3:
        score += 0.04
    if stage >= 2 and len(t2_codes) >= 2:
        score += 0.04
    score += RELATION_BONUS.get(relation, 0)
    score = min(0.95, round(score, 3))

    # 旁证较强必须至少具备题材级证据或客观业务映射，且不是单股孤立标签。
    has_independent_support = bool(evidence_count or relation)
    if score >= 0.72 and has_independent_support and len(day_codes) >= 2:
        verdict = "supporting"
    elif score >= 0.55 and (has_independent_support or len(day_codes) >= 3):
        verdict = "weak"
    else:
        verdict = "insufficient"
    rationale = (
        f"同日{len(day_codes)}股；题材级证据{evidence_count}条；"
        f"T+1题材{len(next_codes)}股/同股延续{len(retained)}股"
        f"（{retained_rate:.0%}）；T+2题材{len(t2_codes)}股；"
        f"业务关系{relation or '未映射'}。"
    )
    conn.execute(
        """
        INSERT INTO attribution_reviews(
          event_id,concept_id,stage,as_of_date,verdict,score,evidence_count,
          same_day_breadth,next_day_breadth,retained_count,retained_rate,
          t2_breadth,business_relation,rationale,source,mature,reviewed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(event_id,concept_id,stage) DO UPDATE SET
          as_of_date=excluded.as_of_date,verdict=excluded.verdict,
          score=excluded.score,evidence_count=excluded.evidence_count,
          same_day_breadth=excluded.same_day_breadth,
          next_day_breadth=excluded.next_day_breadth,
          retained_count=excluded.retained_count,
          retained_rate=excluded.retained_rate,t2_breadth=excluded.t2_breadth,
          business_relation=excluded.business_relation,
          rationale=excluded.rationale,source=excluded.source,
          mature=excluded.mature,reviewed_at=excluded.reviewed_at
        """,
        (
            event_id, concept_id, stage, latest_date, verdict, score,
            evidence_count, len(day_codes), len(next_codes), len(retained),
            retained_rate, len(t2_codes), relation, rationale,
            "deterministic-v1", int(stage >= 2), common.now_iso(),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--date")
    args = parser.parse_args()
    conn = common.open_db()
    dates = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM limit_up_events "
            "WHERE pool='zt' ORDER BY trade_date"
        )
    ]
    if not dates:
        print("无涨停交易日")
        return 0
    date_index = {value: index for index, value in enumerate(dates)}
    targets = (
        [args.date] if args.date
        else dates[-max(1, args.days):]
    )
    unknown = [value for value in targets if value not in date_index]
    if unknown:
        print("不存在的交易日：" + "、".join(unknown), file=sys.stderr)
        return 1
    placeholders = ",".join("?" for _ in targets)
    rows = conn.execute(f"""
        SELECT l.event_id,l.concept_id,e.code,e.trade_date,l.confidence
        FROM event_theme_links l
        JOIN limit_up_events e ON e.id=l.event_id
        WHERE e.trade_date IN ({placeholders})
          AND e.pool='zt' AND l.status='candidate'
        ORDER BY e.trade_date,e.code,l.confidence DESC
    """, targets).fetchall()
    for row in rows:
        review_link(conn, row, dates, date_index, dates[-1])
    conn.commit()
    counts = dict(conn.execute("""
        SELECT verdict,COUNT(*) FROM attribution_reviews
        WHERE as_of_date=? GROUP BY verdict
    """, (dates[-1],)))
    print(
        f"✅ 复核 {len(rows)} 条候选；截至 {dates[-1]}："
        f"旁证较强 {counts.get('supporting', 0)}，"
        f"旁证较弱 {counts.get('weak', 0)}，"
        f"证据不足 {counts.get('insufficient', 0)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
