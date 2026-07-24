#!/usr/bin/env python3
"""
common.py — zt-kg 共享层：同花顺涨停池抓取、SQLite DDL、概念归一化、幂等入库。

数据源：同花顺数据中心 limit_up_pool（公开接口，约1年滚动窗口，更早报"date参数不合法"）。
原则：limit_up_events.reason_type 永存原始字符串；event_concepts 为纯派生表，
      可随时由 rebuild_tags.py 从 raw + data/aliases.json 全量重建。
"""

import json
import random
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "ztkg.db"
ALIASES_PATH = REPO_ROOT / "data" / "aliases.json"
EXPANSIONS_PATH = REPO_ROOT / "data" / "tag_expansions.json"

API_BASE = "https://data.10jqka.com.cn/dataapi/limit_up/"
POOL_ENDPOINTS = {"zt": "limit_up_pool",      # 收盘封住
                  "touch": "open_limit_pool"}  # 触及涨停但收盘未封（炸板池）
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html",
}
FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
PAGE_LIMIT = 200
TIMEOUT_SEC = 20


class DateOutOfRangeError(Exception):
    """date 超出同花顺约1年的滚动窗口。"""


# ---------------------------------------------------------------- 抓取

def fetch_pool(date_yyyymmdd: str, pool: str = "zt") -> tuple[list[dict], dict | None]:
    """抓取某日涨停池（pool='zt'）或炸板池（pool='touch'）全部记录 + 当日市场级统计。

    返回 (rows, stats)。rows 为空列表 = 当日无数据（非交易日）。
    stats 取自响应 limit_up_count.today：{num, history_num, rate, open_num}
    （num=收盘封住家数，history_num=盘中触板家数，rate=封板率，open_num=炸板家数）。
    炸板池记录 reason_type/high_days/limit_up_type 为 null，change_tag='LIMIT_FAILED'。
    抛 DateOutOfRangeError = 超出滚动窗口；其他异常原样抛给调用方。
    """
    api = API_BASE + POOL_ENDPOINTS[pool]
    rows: list[dict] = []
    stats: dict | None = None
    page = 1
    while True:
        params = {
            "page": page, "limit": PAGE_LIMIT, "field": FIELDS,
            "filter": "HS,GEM2STAR", "order_field": "330324", "order_type": "0",
            "date": date_yyyymmdd,
        }
        req = urllib.request.Request(api + "?" + urllib.parse.urlencode(params), headers=HEADERS)
        payload = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT_SEC).read())
        status = payload.get("status_code")
        if status != 0:
            msg = str(payload.get("status_msg", ""))
            if "date" in msg and "不合法" in msg:
                raise DateOutOfRangeError(f"{date_yyyymmdd}: {msg}")
            # 非交易日等场景实测同样可能走非0分支，由调用方按语义区分
            raise RuntimeError(f"THS status_code={status} msg={msg}")
        data = payload.get("data") or {}
        info = data.get("info") or []
        rows.extend(info)
        if stats is None:
            stats = (data.get("limit_up_count") or {}).get("today")
        total = (data.get("page") or {}).get("total") or 0
        if page * PAGE_LIMIT >= total or not info:
            break
        page += 1
    return rows, stats


# ---------------------------------------------------------------- DDL

DDL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS stocks (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    market_type     TEXT,
    first_seen_date TEXT,
    last_seen_date  TEXT
);

CREATE TABLE IF NOT EXISTS limit_up_events (
    id                INTEGER PRIMARY KEY,
    trade_date        TEXT NOT NULL,
    code              TEXT NOT NULL,
    name              TEXT NOT NULL,
    reason_type       TEXT,
    high_days         TEXT,
    high_days_value   INTEGER,
    lb_count          INTEGER,
    limit_up_type     TEXT,
    first_time        INTEGER,
    last_time         INTEGER,
    open_num          INTEGER,
    order_amount      REAL,
    turnover_rate     REAL,
    currency_value    REAL,
    change_rate       REAL,
    limit_up_suc_rate REAL,
    is_new            INTEGER,
    is_again_limit    INTEGER,
    change_tag        TEXT,
    market_type       TEXT,
    source            TEXT NOT NULL DEFAULT 'ths',
    pool              TEXT NOT NULL DEFAULT 'zt',  -- zt=收盘封住 / touch=触及未封(炸板)
    fetched_at        TEXT NOT NULL,
    UNIQUE (trade_date, code, source)
);
CREATE INDEX IF NOT EXISTS idx_events_date ON limit_up_events(trade_date);
CREATE INDEX IF NOT EXISTS idx_events_code ON limit_up_events(code, trade_date);

CREATE TABLE IF NOT EXISTS concepts (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS concept_aliases (
    alias      TEXT PRIMARY KEY,
    concept_id INTEGER NOT NULL REFERENCES concepts(id)
);

CREATE TABLE IF NOT EXISTS event_concepts (
    event_id   INTEGER NOT NULL REFERENCES limit_up_events(id) ON DELETE CASCADE,
    concept_id INTEGER NOT NULL REFERENCES concepts(id),
    raw_tag    TEXT NOT NULL,
    PRIMARY KEY (event_id, concept_id)
);
CREATE INDEX IF NOT EXISTS idx_ec_concept ON event_concepts(concept_id, event_id);

CREATE TABLE IF NOT EXISTS fetch_log (
    trade_date   TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'ths',
    fetched_at   TEXT NOT NULL,
    status       TEXT NOT NULL,      -- ok / empty / out_of_range / error
    record_count INTEGER,
    error_msg    TEXT,
    PRIMARY KEY (trade_date, source)
);

CREATE TABLE IF NOT EXISTS day_stats (
    trade_date  TEXT PRIMARY KEY,
    num         INTEGER,   -- 收盘封住家数
    history_num INTEGER,   -- 盘中触板家数
    rate        REAL,      -- 封板率 num/history_num
    open_num    INTEGER    -- 炸板家数
);

CREATE TABLE IF NOT EXISTS news (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL,
    trade_date TEXT NOT NULL,       -- 关联的涨停日
    title      TEXT NOT NULL,
    url        TEXT NOT NULL,
    source     TEXT,                -- 媒体名
    pub_time   TEXT,                -- 新闻发布时间
    snippet    TEXT,                -- 摘要（东财搜索返回的内容片段）
    fetched_at TEXT NOT NULL,
    UNIQUE (code, trade_date, url)
);
CREATE INDEX IF NOT EXISTS idx_news_code_date ON news(code, trade_date);

CREATE TABLE IF NOT EXISTS news_log (
    code       TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    n_found    INTEGER,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS briefs (
    code       TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    brief      TEXT NOT NULL,        -- LLM一句话涨停归因
    model      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);

-- ---------------------------------------------------------------- 语义证据层
-- 原始 reason_type/event_concepts 只是供应商给出的候选线索；以下表保存可追溯的
-- 公司业务事实、单次涨停题材归因和题材轮次。自动生成记录一律为 candidate /
-- provisional，只有人工或后续核验流程才能改成 verified。
CREATE TABLE IF NOT EXISTS evidence_items (
    id             INTEGER PRIMARY KEY,
    evidence_key   TEXT NOT NULL UNIQUE,
    evidence_type  TEXT NOT NULL,     -- ths_reason/news/announcement/report/regulator/llm_summary
    source_name    TEXT,
    title          TEXT,
    url            TEXT,
    published_at   TEXT,
    subject_code   TEXT,
    subject_name   TEXT,
    subject_status TEXT NOT NULL DEFAULT 'unknown', -- direct/market/third_party/unknown/mismatch
    claim          TEXT NOT NULL,
    reliability    REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_subject
    ON evidence_items(subject_code, published_at);

CREATE TABLE IF NOT EXISTS stock_business_facts (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL REFERENCES stocks(code),
    tag_name       TEXT NOT NULL,
    fact_type      TEXT NOT NULL,     -- sector/subindustry/product/service/growth/attribute
    relation_type  TEXT NOT NULL,     -- core/secondary/research/holding/supply_chain/planned_acquisition
    maturity       TEXT NOT NULL,     -- core_revenue/commercialized/early_revenue/research/holding/proposed
    status         TEXT NOT NULL DEFAULT 'candidate', -- candidate/verified/rejected/expired
    confidence     REAL NOT NULL DEFAULT 0,
    summary        TEXT,
    valid_from     TEXT,
    valid_to       TEXT,
    source         TEXT NOT NULL DEFAULT 'manual',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (code, tag_name, relation_type, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_business_code
    ON stock_business_facts(code, status);

CREATE TABLE IF NOT EXISTS business_fact_evidence (
    fact_id      INTEGER NOT NULL REFERENCES stock_business_facts(id) ON DELETE CASCADE,
    evidence_id  INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS theme_episodes (
    id               INTEGER PRIMARY KEY,
    concept_id       INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    start_date       TEXT NOT NULL,
    end_date         TEXT,
    phase            TEXT NOT NULL DEFAULT 'candidate', -- candidate/startup/fermentation/climax/divergence/recession
    status           TEXT NOT NULL DEFAULT 'provisional', -- provisional/verified/closed/rejected
    catalyst_summary TEXT,
    confidence       REAL NOT NULL DEFAULT 0,
    source           TEXT NOT NULL DEFAULT 'derived',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (concept_id, start_date)
);
CREATE INDEX IF NOT EXISTS idx_episode_concept
    ON theme_episodes(concept_id, start_date);

CREATE TABLE IF NOT EXISTS event_theme_links (
    event_id       INTEGER NOT NULL REFERENCES limit_up_events(id) ON DELETE CASCADE,
    concept_id     INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    episode_id     INTEGER REFERENCES theme_episodes(id) ON DELETE SET NULL,
    theme_role     TEXT NOT NULL DEFAULT 'candidate', -- primary/secondary/candidate
    relation_type  TEXT NOT NULL DEFAULT 'unverified',
    market_role    TEXT,             -- leader/pioneer/core/follower/latecomer/independent
    status         TEXT NOT NULL DEFAULT 'candidate', -- candidate/verified/rejected
    confidence     REAL NOT NULL DEFAULT 0,
    rationale      TEXT,
    source         TEXT NOT NULL DEFAULT 'derived',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (event_id, concept_id)
);
CREATE INDEX IF NOT EXISTS idx_event_theme_episode
    ON event_theme_links(episode_id, event_id);

CREATE TABLE IF NOT EXISTS event_evidence (
    event_id         INTEGER NOT NULL REFERENCES limit_up_events(id) ON DELETE CASCADE,
    evidence_id      INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
    relevance_status TEXT NOT NULL DEFAULT 'unknown', -- supporting/context/unknown/rejected
    note             TEXT,
    PRIMARY KEY (event_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS event_theme_evidence (
    event_id     INTEGER NOT NULL,
    concept_id   INTEGER NOT NULL,
    evidence_id  INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, concept_id, evidence_id),
    FOREIGN KEY (event_id, concept_id)
      REFERENCES event_theme_links(event_id, concept_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS theme_business_mappings (
    concept_id        INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    business_tag_name TEXT NOT NULL,
    mapping_type      TEXT NOT NULL DEFAULT 'exact', -- exact/semantic/supply_chain
    status            TEXT NOT NULL DEFAULT 'candidate', -- candidate/verified/rejected
    confidence        REAL NOT NULL DEFAULT 0,
    rationale         TEXT,
    source            TEXT NOT NULL DEFAULT 'manual',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (concept_id, business_tag_name)
);

CREATE TABLE IF NOT EXISTS company_reports (
    code          TEXT NOT NULL REFERENCES stocks(code),
    report_year   INTEGER NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    published_at  TEXT,
    announcement_id TEXT,
    sha256        TEXT,
    pdf_path      TEXT,
    text_path     TEXT,
    status        TEXT NOT NULL DEFAULT 'fetched', -- fetched/extracted/error
    error_message TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (code, report_year)
);

CREATE TABLE IF NOT EXISTS business_fact_candidates (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL REFERENCES stocks(code),
    report_year    INTEGER NOT NULL,
    tag_name       TEXT NOT NULL,
    fact_type      TEXT NOT NULL,
    relation_type  TEXT NOT NULL,
    maturity       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'candidate', -- candidate/accepted/rejected
    confidence     REAL NOT NULL DEFAULT 0,
    summary        TEXT,
    evidence_key   TEXT NOT NULL REFERENCES evidence_items(evidence_key),
    extractor      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (code, report_year, tag_name, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_business_candidate_status
    ON business_fact_candidates(status, code, report_year);

CREATE TABLE IF NOT EXISTS attribution_reviews (
    event_id          INTEGER NOT NULL,
    concept_id        INTEGER NOT NULL,
    stage             INTEGER NOT NULL, -- 0=T0, 1=T+1, 2=T+2/成熟
    as_of_date        TEXT NOT NULL,
    verdict           TEXT NOT NULL, -- supporting/weak/insufficient
    score             REAL NOT NULL,
    evidence_count    INTEGER NOT NULL DEFAULT 0,
    same_day_breadth  INTEGER NOT NULL DEFAULT 0,
    next_day_breadth  INTEGER NOT NULL DEFAULT 0,
    retained_count    INTEGER NOT NULL DEFAULT 0,
    retained_rate     REAL NOT NULL DEFAULT 0,
    t2_breadth        INTEGER NOT NULL DEFAULT 0,
    business_relation TEXT,
    rationale         TEXT,
    source            TEXT NOT NULL DEFAULT 'deterministic-v1',
    mature            INTEGER NOT NULL DEFAULT 0,
    reviewed_at       TEXT NOT NULL,
    PRIMARY KEY (event_id, concept_id, stage),
    FOREIGN KEY (event_id, concept_id)
      REFERENCES event_theme_links(event_id, concept_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attribution_review_stage
    ON attribution_reviews(stage, verdict, as_of_date);

CREATE TABLE IF NOT EXISTS theme_episode_evidence (
    episode_id   INTEGER NOT NULL REFERENCES theme_episodes(id) ON DELETE CASCADE,
    evidence_id  INTEGER NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
    PRIMARY KEY (episode_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")  # SQLite 默认每连接关闭，必须显式开
    conn.executescript(DDL)
    # 迁移：老库补 pool 列（CREATE IF NOT EXISTS 不会改已有表）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(limit_up_events)")}
    if "pool" not in cols:
        conn.execute("ALTER TABLE limit_up_events ADD COLUMN pool TEXT NOT NULL DEFAULT 'zt'")
        conn.commit()
    return conn


# ---------------------------------------------------------------- 归一化

def split_tags(reason_type: str | None) -> list[str]:
    """'创新药+CAR-T研究+医保销售' → ['创新药','CAR-T研究','医保销售']。仅拆分，不做语义合并。"""
    if not reason_type:
        return []
    seen, out = set(), []
    for t in reason_type.split("+"):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_aliases() -> dict[str, str]:
    """aliases.json {规范名: [别名,...]} → {别名: 规范名}。规范名也映射到自身。"""
    if not ALIASES_PATH.exists():
        return {}
    raw = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for canon, aliases in raw.items():
        if canon.startswith("$"):
            continue
        mapping[canon] = canon
        for a in aliases:
            if a in mapping and mapping[a] != canon:
                raise ValueError(f"别名 '{a}' 同时映射到 '{mapping[a]}' 和 '{canon}'")
            mapping[a] = canon
    return mapping


def load_expansions(alias_map: dict[str, str] | None = None) -> dict[str, list[str]]:
    """tag_expansions.json {原始标签: [目标标签,...]} 一对多语义拆解表。

    校验：目标非空；不得自展开；目标不得再是展开键（禁止链式）；
    展开键不得与 aliases 体系（别名或规范名）重叠——同一标签走两条改写路径必然歧义。
    """
    if not EXPANSIONS_PATH.exists():
        return {}
    raw = json.loads(EXPANSIONS_PATH.read_text(encoding="utf-8"))
    exp = {k: v for k, v in raw.items() if not k.startswith("$")}
    if alias_map is None:
        alias_map = load_aliases()
    for src, targets in exp.items():
        if not targets or not isinstance(targets, list):
            raise ValueError(f"展开 '{src}' 目标必须是非空数组")
        if src in targets:
            raise ValueError(f"展开 '{src}' 不能包含自身")
        for t in targets:
            if t in exp:
                raise ValueError(f"展开 '{src}' → '{t}'，但 '{t}' 自己也是展开键（禁止链式）")
        if src in alias_map:
            raise ValueError(f"'{src}' 同时出现在 aliases 体系和 tag_expansions 中，二选一")
    return exp


def normalize_tags(reason_type: str | None, alias_map: dict[str, str],
                   expansions: dict[str, list[str]]) -> list[tuple[str, str]]:
    """归一化总管线：拆分 → 一对多展开 → 别名归一。

    返回 [(规范名, 原始标签)]，规范名去重保序；raw_tag 记录展开/归一前的原始写法。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag in split_tags(reason_type):
        for t in expansions.get(tag, [tag]):
            canon = alias_map.get(t, t)
            if canon not in seen:
                seen.add(canon)
                out.append((canon, tag))
    return out


def get_or_create_concept(conn: sqlite3.Connection, name: str, cache: dict[str, int]) -> int:
    if name in cache:
        return cache[name]
    row = conn.execute("SELECT id FROM concepts WHERE name=?", (name,)).fetchone()
    if row:
        cache[name] = row[0]
        return row[0]
    cur = conn.execute("INSERT INTO concepts(name, created_at) VALUES(?,?)",
                       (name, now_iso()))
    cache[name] = cur.lastrowid
    return cur.lastrowid


# ---------------------------------------------------------------- 入库

def decode_high_days(value) -> int | None:
    """high_days_value 编码为 (板数<<16 | 天数)，如 65537=1天1板、196612=4天3板。返回板数。"""
    if value is None:
        return None
    try:
        return int(value) >> 16
    except (TypeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def upsert_events(conn: sqlite3.Connection, trade_date: str, rows: list[dict],
                  alias_map: dict[str, str] | None = None, source: str = "ths",
                  expansions: dict[str, list[str]] | None = None,
                  pool: str = "zt") -> int:
    """单事务幂等入库：events upsert + stocks upsert + 重建该事件概念关系。
    trade_date 格式 'YYYY-MM-DD'。返回入库条数。"""
    if alias_map is None:
        alias_map = load_aliases()
    if expansions is None:
        expansions = load_expansions(alias_map)
    cache: dict[str, int] = {}
    fetched_at = now_iso()
    with conn:
        for r in rows:
            code = str(r.get("code") or "").strip()
            if not code:
                continue
            vals = dict(
                trade_date=trade_date, code=code, name=r.get("name"),
                reason_type=r.get("reason_type"), high_days=r.get("high_days"),
                high_days_value=r.get("high_days_value"),
                lb_count=decode_high_days(r.get("high_days_value")),
                limit_up_type=r.get("limit_up_type"),
                first_time=_to_int(r.get("first_limit_up_time")),
                last_time=_to_int(r.get("last_limit_up_time")),
                open_num=_to_int(r.get("open_num")),
                order_amount=_to_float(r.get("order_amount")),
                turnover_rate=_to_float(r.get("turnover_rate")),
                currency_value=_to_float(r.get("currency_value")),
                change_rate=_to_float(r.get("change_rate")),
                limit_up_suc_rate=_to_float(r.get("limit_up_suc_rate")),
                is_new=_to_int(r.get("is_new")),
                is_again_limit=_to_int(r.get("is_again_limit")),
                change_tag=r.get("change_tag"), market_type=r.get("market_type"),
                source=source, pool=pool, fetched_at=fetched_at,
            )
            cols = ",".join(vals)
            placeholders = ",".join("?" for _ in vals)
            updates = ",".join(f"{c}=excluded.{c}" for c in vals
                               if c not in ("trade_date", "code", "source"))
            conn.execute(
                f"INSERT INTO limit_up_events({cols}) VALUES({placeholders}) "
                f"ON CONFLICT(trade_date, code, source) DO UPDATE SET {updates}",
                tuple(vals.values()))
            event_id = conn.execute(
                "SELECT id FROM limit_up_events WHERE trade_date=? AND code=? AND source=?",
                (trade_date, code, source)).fetchone()[0]

            conn.execute(
                "INSERT INTO stocks(code, name, market_type, first_seen_date, last_seen_date) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
                "market_type=excluded.market_type, "
                "first_seen_date=MIN(first_seen_date, excluded.first_seen_date), "
                "last_seen_date=MAX(last_seen_date, excluded.last_seen_date)",
                (code, r.get("name"), r.get("market_type"), trade_date, trade_date))

            conn.execute("DELETE FROM event_concepts WHERE event_id=?", (event_id,))
            for canon, tag in normalize_tags(r.get("reason_type"), alias_map, expansions):
                cid = get_or_create_concept(conn, canon, cache)
                conn.execute(
                    "INSERT OR IGNORE INTO event_concepts(event_id, concept_id, raw_tag) "
                    "VALUES(?,?,?)", (event_id, cid, tag))
    return len(rows)


def upsert_day_stats(conn: sqlite3.Connection, trade_date: str, stats: dict | None) -> None:
    if not stats:
        return
    with conn:
        conn.execute(
            "INSERT INTO day_stats(trade_date, num, history_num, rate, open_num) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET num=excluded.num, "
            "history_num=excluded.history_num, rate=excluded.rate, open_num=excluded.open_num",
            (trade_date, _to_int(stats.get("num")), _to_int(stats.get("history_num")),
             _to_float(stats.get("rate")), _to_int(stats.get("open_num"))))


def log_fetch(conn: sqlite3.Connection, trade_date: str, status: str,
              record_count: int | None = None, error_msg: str | None = None,
              source: str = "ths") -> None:
    with conn:
        conn.execute(
            "INSERT INTO fetch_log(trade_date, source, fetched_at, status, record_count, error_msg) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(trade_date, source) DO UPDATE SET fetched_at=excluded.fetched_at, "
            "status=excluded.status, record_count=excluded.record_count, error_msg=excluded.error_msg",
            (trade_date, source, now_iso(), status, record_count, error_msg))


# ---------------------------------------------------------------- 杂项

def board_of(code: str) -> str:
    """按代码前缀推断板块。"""
    if code.startswith("68"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("60", "00")):
        return "主板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "其他"


def polite_sleep(base: float = 1.5) -> None:
    time.sleep(base + random.random())


def _to_int(v):
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
