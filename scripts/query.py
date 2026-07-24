#!/usr/bin/env python3
"""
query.py — zt-kg CLI 查询（也是对话内查询接口）。

用法：
  python3 scripts/query.py stock 300750              # 个股涨停史+概念分布
  python3 scripts/query.py concept 算力 [--days 30]  # 概念成分股+活跃度时间线
  python3 scripts/query.py date 2026-07-21           # 某日按概念分组复盘
  python3 scripts/query.py codes 300750,600519,...   # 批量归类（核心场景）
  python3 scripts/query.py similar                   # 疑似应合并的概念对
"""

import argparse
import re
import sys

import common


def _concept_id(conn, name: str) -> tuple[int, str] | None:
    """概念名/别名 → (id, 规范名)。"""
    alias_map = common.load_aliases()
    canon = alias_map.get(name, name)
    row = conn.execute("SELECT id, name FROM concepts WHERE name=?", (canon,)).fetchone()
    return (row[0], row[1]) if row else None


def cmd_stock(conn, code: str) -> int:
    stock = conn.execute("SELECT code, name, market_type FROM stocks WHERE code=?",
                         (code,)).fetchone()
    if not stock:
        # 试按名称查
        stock = conn.execute("SELECT code, name, market_type FROM stocks WHERE name LIKE ?",
                             (f"%{code}%",)).fetchone()
        if not stock:
            print(f"库内无 {code} 的涨停记录")
            return 1
        code = stock[0]
    print(f"\n{stock[1]} ({code}) [{common.board_of(code)}]")

    facts = conn.execute("""
        SELECT tag_name,relation_type,maturity,status,confidence,summary
        FROM stock_business_facts WHERE code=? AND status!='rejected'
        ORDER BY (relation_type='core') DESC,confidence DESC
    """, (code,)).fetchall()
    print("\n公司客观业务（有证据的主营/产品/参股/拟收购关系）：")
    if facts:
        for tag, relation, maturity, status, confidence, summary in facts:
            print(f"  {tag}｜{relation}｜{maturity}｜{status} {confidence:.0%}")
            if summary:
                print(f"    {summary}")
    else:
        print("  暂无已核实业务事实；不能用历史涨停原因代替主营业务")

    print("\n历史原因标签（供应商线索，不等于主营或已核实题材）：")
    for name, cnt in conn.execute("""
        SELECT c.name, COUNT(*) n FROM limit_up_events e
        JOIN event_concepts ec ON ec.event_id=e.id JOIN concepts c ON c.id=ec.concept_id
        WHERE e.code=? GROUP BY c.id ORDER BY n DESC""", (code,)):
        print(f"  {name} ×{cnt}")

    print("\n涨停/触及记录：")
    for row in conn.execute("""
        SELECT trade_date, high_days, limit_up_type, open_num, order_amount, reason_type, pool
        FROM limit_up_events WHERE code=? ORDER BY trade_date DESC""", (code,)):
        d, hd, lt, opens, amt, reason, pool = row
        amt_s = f"封单{amt / 1e8:.2f}亿" if amt else ""
        mark = "⚡触及未封" if pool == "touch" else ""
        themes = conn.execute("""
            SELECT c.name,l.theme_role,l.status,l.confidence,l.relation_type
            FROM event_theme_links l
            JOIN limit_up_events e ON e.id=l.event_id
            JOIN concepts c ON c.id=l.concept_id
            WHERE e.code=? AND e.trade_date=? AND l.status!='rejected'
            ORDER BY (l.theme_role='primary') DESC,l.confidence DESC
        """, (code, d)).fetchall()
        theme_text = "；".join(
            f"{name}({role}/{status}/{confidence:.0%}/{relation})"
            for name, role, status, confidence, relation in themes
        ) or "未归因"
        print(f"  {d}  {hd or '—':<6} {lt or ''} {mark} 炸板{opens or 0}次 {amt_s}")
        print(f"    本次题材：{theme_text}")
        print(f"    原始原因：{reason or '(无原因)'}")
    return 0


def cmd_concept(conn, name: str, days: int) -> int:
    hit = _concept_id(conn, name)
    if not hit:
        # 模糊提示
        cands = [r[0] for r in conn.execute(
            "SELECT name FROM concepts WHERE name LIKE ? LIMIT 10", (f"%{name}%",))]
        print(f"无概念 '{name}'" + (f"，相近：{', '.join(cands)}" if cands else ""))
        return 1
    cid, canon = hit
    aliases = [r[0] for r in conn.execute(
        "SELECT alias FROM concept_aliases WHERE concept_id=? AND alias!=?", (cid, canon))]
    total, ndays, lo, hi = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT e.trade_date), MIN(e.trade_date), MAX(e.trade_date)
        FROM event_concepts ec JOIN limit_up_events e ON e.id=ec.event_id
        WHERE ec.concept_id=?""", (cid,)).fetchone()
    print(f"\n标签：{canon}" + (f"（别名：{'、'.join(aliases)}）" if aliases else ""))
    print(f"供应商历史标签：累计 {total} 股次 · 活跃 {ndays} 天 · {lo} ~ {hi}")

    print(f"\n供应商历史标签近 {days} 天活跃度：")
    rows = conn.execute("""
        SELECT e.trade_date, COUNT(*) FROM event_concepts ec
        JOIN limit_up_events e ON e.id=ec.event_id
        WHERE ec.concept_id=? GROUP BY e.trade_date ORDER BY e.trade_date DESC LIMIT ?""",
        (cid, days)).fetchall()
    for d, n in rows:
        print(f"  {d}  {'█' * min(n, 40)} {n}")

    event_theme_rows = conn.execute("""
        SELECT e.code,e.name,COUNT(*) n,MAX(e.trade_date) last_date,
               SUM(l.status='verified') verified
        FROM event_theme_links l
        JOIN limit_up_events e ON e.id=l.event_id
        WHERE l.concept_id=? AND l.status!='rejected'
        GROUP BY e.code ORDER BY n DESC,last_date DESC
    """, (cid,)).fetchall()
    if event_theme_rows:
        print("\n单次涨停题材关系（候选/已核实分开）：")
        for code, sname, cnt, last, verified in event_theme_rows[:40]:
            print(f"  {sname}({code}) ×{cnt} 最近{last} "
                  f"[已核实{verified}/{cnt}]")

    business_rows = conn.execute("""
        SELECT f.code,s.name,f.tag_name,f.relation_type,f.maturity,
               f.status,f.confidence,m.mapping_type,m.status,m.confidence
        FROM theme_business_mappings m
        JOIN stock_business_facts f ON f.tag_name=m.business_tag_name
        JOIN stocks s ON s.code=f.code
        WHERE m.concept_id=? AND m.status!='rejected'
          AND f.status NOT IN ('rejected','expired')
        ORDER BY (f.relation_type='core') DESC,f.confidence DESC
    """, (cid,)).fetchall()
    if business_rows:
        print("\n题材对应的业务候选池（不等于本轮已炒作成分）：")
        for (code, sname, tag, relation, maturity, fact_status, fact_conf,
             mapping_type, mapping_status, mapping_conf) in business_rows:
            print(f"  {sname}({code})｜{tag}｜{relation}/{maturity} "
                  f"[事实{fact_status} {fact_conf:.0%}；"
                  f"映射{mapping_type}/{mapping_status} {mapping_conf:.0%}]")

    print("\n供应商历史标签成分股（按涨停次数，仅作线索）：")
    for code, sname, cnt, maxlb, last in conn.execute("""
        SELECT e.code, e.name, COUNT(*) AS n, MAX(e.lb_count),
               MAX(e.trade_date) AS last_date
        FROM event_concepts ec JOIN limit_up_events e ON e.id=ec.event_id
        WHERE ec.concept_id=? GROUP BY e.code
        ORDER BY n DESC, last_date DESC LIMIT 40""", (cid,)):
        print(f"  {sname}({code}) [{common.board_of(code)}] ×{cnt} 最高{maxlb or '?'}板 最近{last}")
    return 0


def cmd_date(conn, d: str) -> int:
    d = d.replace("/", "-")
    if re.fullmatch(r"\d{8}", d):
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    stats = conn.execute("SELECT num, history_num, rate, open_num FROM day_stats "
                         "WHERE trade_date=?", (d,)).fetchone()
    n_events, n_touch = conn.execute(
        "SELECT SUM(pool='zt'), SUM(pool='touch') FROM limit_up_events WHERE trade_date=?",
        (d,)).fetchone()
    if not n_events:
        print(f"{d} 无涨停数据（非交易日或未抓取）")
        return 1
    head = f"\n===== {d} 涨停复盘：{n_events} 只" + (f"（另触及未封{n_touch}只）" if n_touch else "")
    if stats:
        num, hist, rate, opens = stats
        head += f" · 触板{hist} · 封板率{rate * 100:.0f}% · 炸板{opens}家" if rate else ""
    print(head + " =====")

    groups = conn.execute("""
        SELECT c.name,COUNT(DISTINCT e.code) n,
               SUM(l.status='verified') verified
        FROM event_theme_links l
        JOIN limit_up_events e ON e.id=l.event_id
        JOIN concepts c ON c.id=l.concept_id
        WHERE e.trade_date=? AND e.pool='zt' AND l.status!='rejected'
        GROUP BY c.id HAVING n>=2 ORDER BY n DESC""", (d,)).fetchall()
    for cname, n, verified in groups:
        state = f"已核实{verified}/{n}" if verified else "候选题材"
        print(f"\n【{cname}】{n} 只｜{state}")
        for code, sname, hd, lt, link_status in conn.execute("""
            SELECT e.code,e.name,e.high_days,e.limit_up_type,l.status
            FROM event_theme_links l
            JOIN limit_up_events e ON e.id=l.event_id
            JOIN concepts c ON c.id=l.concept_id
            WHERE e.trade_date=? AND c.name=? AND l.status!='rejected'
            ORDER BY e.lb_count DESC,e.first_time""",
            (d, cname)):
            print(f"  {sname}({code}) {hd or ''} {lt or ''} [{link_status}]")
    return 0


def cmd_codes(conn, codes_str: str) -> int:
    codes = re.findall(r"\d{6}", codes_str)
    if not codes:
        print("未提取到6位股票代码")
        return 1
    recent5 = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up_events ORDER BY trade_date DESC LIMIT 5")]
    ph5 = ",".join("?" * len(recent5))
    for code in codes:
        row = conn.execute("SELECT name FROM stocks WHERE code=?", (code,)).fetchone()
        if not row:
            print(f"\n{code}: 无涨停记录")
            continue
        n_zt, last = conn.execute(
            "SELECT COUNT(*), MAX(trade_date) FROM limit_up_events WHERE code=? AND pool='zt'",
            (code,)).fetchone()
        print(f"\n{row[0]}({code}) [{common.board_of(code)}] 历史涨停{n_zt}次，最近{last}")
        for cname, cid, n in conn.execute("""
            SELECT c.name, c.id, COUNT(*) n FROM limit_up_events e
            JOIN event_concepts ec ON ec.event_id=e.id JOIN concepts c ON c.id=ec.concept_id
            WHERE e.code=? GROUP BY c.id ORDER BY n DESC LIMIT 8""", (code,)):
            hot = conn.execute(
                f"SELECT COUNT(*) FROM event_concepts ec "
                f"JOIN limit_up_events e ON e.id=ec.event_id "
                f"WHERE ec.concept_id=? AND e.trade_date IN ({ph5})",
                [cid] + recent5).fetchone()[0]
            print(f"  {cname} ×{n}" + (f"  🔥近5日{hot}家涨停" if hot >= 3 else ""))
    return 0


def cmd_tree(conn) -> int:
    """打印 taxonomy 标签层级树（热度=后代概念去重股·日事件数）。"""
    import json
    tax_path = common.REPO_ROOT / "data" / "taxonomy.json"
    if not tax_path.exists():
        print("无 data/taxonomy.json")
        return 1
    tax = json.loads(tax_path.read_text(encoding="utf-8"))
    tax.pop("$note", None)
    children = set(sum(tax.values(), []))
    roots = [p for p in tax if p not in children]
    name_cid = {r[1]: r[0] for r in conn.execute("SELECT id, name FROM concepts")}

    def cids_of(name, stack=None):
        stack = stack or set()
        if name in stack:
            return set()
        s = {name_cid[name]} if name in name_cid else set()
        for ch in tax.get(name, []):
            s |= cids_of(ch, stack | {name})
        return s

    recent5 = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up_events ORDER BY trade_date DESC LIMIT 5")]

    def heat(cids, dates=None):
        if not cids:
            return 0
        ph = ",".join("?" * len(cids))
        sql = (f"SELECT COUNT(*) FROM (SELECT DISTINCT e.trade_date, e.code "
               f"FROM limit_up_events e JOIN event_concepts ec ON ec.event_id=e.id "
               f"WHERE ec.concept_id IN ({ph})")
        args = list(cids)
        if dates:
            sql += f" AND e.trade_date IN ({','.join('?' * len(dates))})"
            args += dates
        return conn.execute(sql + ")", args).fetchone()[0]

    def walk(name, depth):
        cids = cids_of(name)
        h5 = heat(cids, recent5)
        mark = " 🔥" if h5 >= 10 else ""
        print("  " * depth + f"{name}  [{heat(cids)}次 | 近5日{h5}]{mark}")
        kids = sorted(tax.get(name, []), key=lambda ch: -heat(cids_of(ch), recent5))
        for ch in kids:
            if ch in tax:
                walk(ch, depth + 1)
            else:
                ch5 = heat(cids_of(ch), recent5)
                print("  " * (depth + 1) + f"{ch}  [{heat(cids_of(ch))}次 | 近5日{ch5}]"
                      + (" 🔥" if ch5 >= 5 else ""))

    for r in sorted(roots, key=lambda n: -heat(cids_of(n), recent5)):
        walk(r, 0)
    return 0


def cmd_review_tags(conn) -> int:
    """待审核标签清单：达标(单日共振≥3股 或 ≥3股且≥2日)但尚未 active 的标签。"""
    import json
    import gen_tag_meta
    meta_path = common.REPO_ROOT / "data" / "tag_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.pop("$note", None)
    tax = json.loads((common.REPO_ROOT / "data" / "taxonomy.json").read_text(encoding="utf-8"))
    tax.pop("$note", None)
    taxnames = set(tax) | {c for v in tax.values() for c in v}

    rows = conn.execute("""
        WITH s AS (SELECT ec.concept_id cid, e.trade_date d, COUNT(DISTINCT e.code) ds
                   FROM event_concepts ec JOIN limit_up_events e ON e.id=ec.event_id
                   GROUP BY cid, d),
        agg AS (SELECT cid, MAX(ds) mx, COUNT(*) days FROM s GROUP BY cid),
        st AS (SELECT ec.concept_id cid, COUNT(DISTINCT e.code) stocks, COUNT(*) total
               FROM event_concepts ec JOIN limit_up_events e ON e.id=ec.event_id GROUP BY cid)
        SELECT c.id, c.name, st.total, st.stocks, agg.days, agg.mx,
          (SELECT s2.d FROM s s2 WHERE s2.cid=c.id ORDER BY s2.ds DESC LIMIT 1)
        FROM agg JOIN st USING(cid) JOIN concepts c ON c.id=agg.cid
        WHERE agg.mx>=3 OR (st.stocks>=3 AND agg.days>=2)
        ORDER BY agg.mx DESC, st.total DESC""").fetchall()

    n = 0
    for cid, name, total, stocks, days, mx, mxd in rows:
        m = meta.get(name)
        if m and m.get("status") == "active":
            continue
        n += 1
        examples = "、".join(r[0] for r in conn.execute(
            "SELECT e.name FROM event_concepts ec JOIN limit_up_events e ON e.id=ec.event_id "
            "WHERE ec.concept_id=? GROUP BY e.code ORDER BY COUNT(*) DESC LIMIT 3", (cid,)))
        sug = m["type"] if m else gen_tag_meta.infer_type(name)
        print(f"{name}  ×{total} · {stocks}股 · {days}日 · 最大共振{mx}家({mxd})")
        print(f"  建议类型: {sug}{'（已登记candidate）' if m else '（未登记）'}"
              f"{' · 已在taxonomy' if name in taxnames else ''} · 代表: {examples}")
    print(f"\n共 {n} 个待审核标签。审核方式：改 data/tag_meta.json 的 type/status，"
          f"需归树的同时编辑 taxonomy.json，然后跑 build_site.py")
    return 0


def cmd_similar(conn) -> int:
    names = [r[0] for r in conn.execute(
        "SELECT c.name FROM concepts c JOIN event_concepts ec ON ec.concept_id=c.id "
        "GROUP BY c.id HAVING COUNT(*)>=3")]
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a in b or b in a:
                pairs.append((a, b))
    if not pairs:
        print("无疑似重复概念对")
        return 0
    print("疑似应合并的概念对（人工判断后编辑 data/aliases.json 再跑 rebuild_tags.py）：")
    for a, b in sorted(pairs):
        print(f"  {a}  ~  {b}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stock").add_argument("code")
    p = sub.add_parser("concept")
    p.add_argument("name")
    p.add_argument("--days", type=int, default=30)
    sub.add_parser("date").add_argument("d")
    sub.add_parser("codes").add_argument("codes_str")
    sub.add_parser("similar")
    sub.add_parser("tree")
    sub.add_parser("review-tags")
    args = ap.parse_args()

    conn = common.open_db()
    if args.cmd == "stock":
        return cmd_stock(conn, args.code)
    if args.cmd == "concept":
        return cmd_concept(conn, args.name, args.days)
    if args.cmd == "date":
        return cmd_date(conn, args.d)
    if args.cmd == "codes":
        return cmd_codes(conn, args.codes_str)
    if args.cmd == "similar":
        return cmd_similar(conn)
    if args.cmd == "tree":
        return cmd_tree(conn)
    if args.cmd == "review-tags":
        return cmd_review_tags(conn)
    return 1


if __name__ == "__main__":
    sys.exit(main())
