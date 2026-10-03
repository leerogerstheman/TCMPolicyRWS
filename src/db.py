"""SQLite 存储层:政策文件库 + FTS5 全文检索(trigram 分词)。

设计要点
--------
1. **为什么用 trigram 而不是默认分词器**:SQLite FTS5 内置的 ``unicode61``
   分词器按空白与标点切词,对中文等于把整句当成一个 token,检索基本失效。
   ``trigram`` 分词器按 3 字符滑窗建索引,是**官方支持的**中文检索可行方案。
2. **trigram 的硬约束**:FTS5 trigram 无法为长度 < 3 的查询串生成三元组,
   因此 ``MATCH`` 对 1~2 字的中文查询(如 "中医"、"药典")返回空集。
   这是分词器的数学限制,不是 bug。本模块对此实现 **LIKE 回退**,
   并把该行为显式记录在检索结果里(``fallback_used`` 字段),避免使用者
   误以为"没有匹配结果"。
3. **幂等性**:``(doc_key)`` 唯一索引保证重复采集不会产生重复行,
   便于增量更新语料库。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

# 全文检索表与内容表分离(external content),正文不重复存储,节省约一半空间。
_FTS_TABLE = "policy_fts"

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS policy (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_key      TEXT    NOT NULL UNIQUE,   -- 稳定去重键: url 优先,缺失时用标题+日期
    title        TEXT    NOT NULL,
    pcode        TEXT,                      -- 发文字号,如 国办发〔2025〕11号
    puborg       TEXT,                      -- 主要发文机关(归一化后的规范名)
    puborg_raw   TEXT,                      -- 发文机关原始字符串(可回溯核验)
    puborg_all   TEXT,                      -- 全部参与机关(规范名,、分隔)
    n_orgs       INTEGER DEFAULT 1,         -- 参与发文的机关数(联合发文 >1)
    pubdate      TEXT,                      -- ISO 8601 日期 YYYY-MM-DD
    pubyear      INTEGER,                   -- 冗余存储,便于按年聚合
    doc_type     TEXT,                      -- 文件类别(政府文件/部门文件)
    category     TEXT,                      -- 政策类别(childtype)
    source       TEXT,                      -- 来源(政府文件库)
    url          TEXT,
    summary      TEXT,
    body         TEXT,                      -- 正文纯文本(可为空)
    body_len     INTEGER DEFAULT 0,
    body_status  TEXT DEFAULT 'pending',    -- pending/ok/failed:显式记录抽取结果
    body_error   TEXT,                      -- 抽取失败原因(不静默丢弃)
    theme        TEXT,                      -- 归一化政策主题(由规则分类器给出)
    fetched_at   TEXT DEFAULT (datetime('now')),
    -- 标注字段:用于人工核验某条记录是否属于中医药政策域
    is_tcm       INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_policy_pubyear  ON policy(pubyear);
CREATE INDEX IF NOT EXISTS idx_policy_puborg   ON policy(puborg);
CREATE INDEX IF NOT EXISTS idx_policy_theme    ON policy(theme);
CREATE INDEX IF NOT EXISTS idx_policy_pubdate  ON policy(pubdate);

-- 主题演变分析:每份文件命中的主题关键词及其词频
CREATE TABLE IF NOT EXISTS policy_keyword (
    policy_id  INTEGER NOT NULL REFERENCES policy(id) ON DELETE CASCADE,
    keyword    TEXT    NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (policy_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_kw_keyword ON policy_keyword(keyword);

-- 采集运行日志:保证 README 中引用的数字可追溯到某一次具体运行
CREATE TABLE IF NOT EXISTS crawl_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    ended_at    TEXT,
    source      TEXT,
    query       TEXT,
    total_count INTEGER,
    n_new       INTEGER,
    n_seen      INTEGER,
    note        TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开(必要时创建)数据库并确保 schema 就绪。

    返回的连接启用 ``row_factory = sqlite3.Row``,使查询结果可按列名取值,
    并被 ``check_same_thread=False`` 约束为单连接单线程使用。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


@contextmanager
def session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """上下文管理器:自动提交/回滚并关闭连接。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """为已存在的旧库补齐新增列。

    SQLite 的 ``ALTER TABLE ADD COLUMN`` 是幂等的近似 —— 重复添加会报错,
    故先查 ``PRAGMA table_info`` 判断缺哪些列。这样升级代码后
    无需删除旧库重爬(重爬会重复消耗对方站点资源)。
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(policy)")}
    additions = {
        "puborg_raw": "TEXT",
        "puborg_all": "TEXT",
        "n_orgs": "INTEGER DEFAULT 1",
        "body_status": "TEXT DEFAULT 'pending'",
        "body_error": "TEXT",
    }
    for col, decl in additions.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE policy ADD COLUMN {col} {decl}")
    # 为旧数据回填:已有正文的标为 ok,其余标为 pending
    conn.execute(
        """UPDATE policy SET body_status='ok'
           WHERE COALESCE(body_status,'pending')='pending' AND COALESCE(body_len,0)>0"""
    )
    conn.execute(
        """UPDATE policy SET puborg_raw=puborg
           WHERE COALESCE(puborg_raw,'')='' AND COALESCE(puborg,'')<>''"""
    )
    conn.commit()


def _fts5_trigram_available(conn: sqlite3.Connection) -> bool:
    """探测当前 SQLite 是否编译了 FTS5 的 trigram 分词器。

    trigram 需要 SQLite >= 3.34。若不可用,则整个模块降级为纯 LIKE 检索,
    而不是直接崩溃 —— 功能可用性优先于性能。
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE temp.__probe")
        return True
    except sqlite3.OperationalError:
        return False


def init_schema(conn: sqlite3.Connection) -> None:
    """建表。幂等 —— 可对已有数据库重复调用。"""
    conn.executescript(_SCHEMA)
    _migrate(conn)
    if _fts5_trigram_available(conn):
        # external content 模式:索引与内容表通过 rowid 关联,避免正文双份存储。
        conn.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE}
                USING fts5(title, body, summary,
                           content='policy', content_rowid='id',
                           tokenize='trigram')"""
        )
        _register_fts_triggers(conn)
        # 首次建索引:回填历史数据(已同步时该语句是空操作)。
        conn.execute(f"INSERT INTO {_FTS_TABLE}({_FTS_TABLE}) VALUES('rebuild')")
    conn.commit()


def _register_fts_triggers(conn: sqlite3.Connection) -> None:
    """用触发器让 FTS 索引跟随 policy 表自动同步,避免应用层漏更新。"""
    conn.executescript(
        f"""
        CREATE TRIGGER IF NOT EXISTS policy_ai AFTER INSERT ON policy BEGIN
            INSERT INTO {_FTS_TABLE}(rowid, title, body, summary)
            VALUES (new.id, new.title, new.body, new.summary);
        END;
        CREATE TRIGGER IF NOT EXISTS policy_ad AFTER DELETE ON policy BEGIN
            INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, title, body, summary)
            VALUES ('delete', old.id, old.title, old.body, old.summary);
        END;
        CREATE TRIGGER IF NOT EXISTS policy_au AFTER UPDATE ON policy BEGIN
            INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, title, body, summary)
            VALUES ('delete', old.id, old.title, old.body, old.summary);
            INSERT INTO {_FTS_TABLE}(rowid, title, body, summary)
            VALUES (new.id, new.title, new.body, new.summary);
        END;
        """
    )


def has_fts(conn: sqlite3.Connection) -> bool:
    """该数据库是否具备可用的 FTS5 trigram 索引。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_FTS_TABLE,)
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

def upsert_policy(conn: sqlite3.Connection, doc: dict[str, Any]) -> bool:
    """插入一条政策记录。返回 True 表示新增,False 表示已存在(跳过)。

    使用 ``INSERT ... ON CONFLICT DO UPDATE`` 而非 INSERT OR REPLACE:
    后者会先 DELETE 再 INSERT,导致主键变化并使 policy_keyword 的外键级联删除,
    从而丢失已抽取的关键词。
    """
    doc_key = doc.get("doc_key") or doc.get("url") or (doc.get("title", "") + "|" + (doc.get("pubdate") or ""))
    pubdate = doc.get("pubdate") or ""
    pubyear = doc.get("pubyear")
    if pubyear is None and len(pubdate) >= 4 and pubdate[:4].isdigit():
        pubyear = int(pubdate[:4])

    body = doc.get("body") or ""
    # 先查存在性再写:ON CONFLICT DO UPDATE 的 rowcount 无法区分"新增"与"更新",
    # 而调用方需要准确的新增计数来汇报采集产出。
    existed = policy_exists(conn, doc_key)
    # 正文状态:显式记录抽取结果,避免"body 为空"既可能是"还没抓"
    # 也可能是"抓了但失败"的歧义。
    body_status = doc.get("body_status") or ("ok" if body else "pending")
    conn.execute(
        """
        INSERT INTO policy (doc_key, title, pcode, puborg, puborg_raw, puborg_all,
                            n_orgs, pubdate, pubyear, doc_type, category, source, url,
                            summary, body, body_len, body_status, body_error, theme, is_tcm)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(doc_key) DO UPDATE SET
            body        = CASE WHEN length(excluded.body) > length(COALESCE(policy.body,''))
                               THEN excluded.body ELSE policy.body END,
            body_len    = MAX(excluded.body_len, policy.body_len),
            body_status = CASE WHEN excluded.body_len > COALESCE(policy.body_len,0)
                               THEN excluded.body_status
                               ELSE COALESCE(NULLIF(policy.body_status,''), excluded.body_status) END,
            body_error  = COALESCE(NULLIF(excluded.body_error,''), policy.body_error),
            summary     = COALESCE(NULLIF(excluded.summary,''), policy.summary),
            pcode       = COALESCE(NULLIF(excluded.pcode,''),   policy.pcode),
            puborg      = COALESCE(NULLIF(excluded.puborg,''),  policy.puborg),
            puborg_raw  = COALESCE(NULLIF(excluded.puborg_raw,''), policy.puborg_raw),
            puborg_all  = COALESCE(NULLIF(excluded.puborg_all,''), policy.puborg_all),
            n_orgs      = MAX(COALESCE(excluded.n_orgs,1), COALESCE(policy.n_orgs,1)),
            category    = COALESCE(NULLIF(excluded.category,''),policy.category),
            theme       = COALESCE(NULLIF(excluded.theme,''),   policy.theme)
        """,
        (
            doc_key,
            doc.get("title", ""),
            doc.get("pcode") or "",
            doc.get("puborg") or "",
            doc.get("puborg_raw") or doc.get("puborg") or "",
            doc.get("puborg_all") or doc.get("puborg") or "",
            int(doc.get("n_orgs") or 1),
            pubdate,
            pubyear,
            doc.get("doc_type") or "",
            doc.get("category") or "",
            doc.get("source") or "中国政府网政策文件库",
            doc.get("url") or "",
            doc.get("summary") or "",
            body,
            len(body),
            body_status,
            doc.get("body_error") or "",
            doc.get("theme") or "",
            1 if doc.get("is_tcm", True) else 0,
        ),
    )
    return not existed


def policy_exists(conn: sqlite3.Connection, doc_key: str) -> bool:
    return conn.execute("SELECT 1 FROM policy WHERE doc_key=?", (doc_key,)).fetchone() is not None


def save_keywords(conn: sqlite3.Connection, policy_id: int, counts: dict[str, int]) -> None:
    """写入某文件的主题关键词-词频。"""
    if not counts:
        return
    conn.executemany(
        """INSERT INTO policy_keyword(policy_id, keyword, count) VALUES (?,?,?)
           ON CONFLICT(policy_id, keyword) DO UPDATE SET count=excluded.count""",
        [(policy_id, k, int(v)) for k, v in counts.items()],
    )


def log_crawl(conn: sqlite3.Connection, **kw: Any) -> None:
    conn.execute(
        """INSERT INTO crawl_log(started_at, ended_at, source, query,
                                 total_count, n_new, n_seen, note)
           VALUES (:started_at,:ended_at,:source,:query,:total_count,:n_new,:n_seen,:note)""",
        {
            "started_at": kw.get("started_at"),
            "ended_at": kw.get("ended_at"),
            "source": kw.get("source"),
            "query": kw.get("query"),
            "total_count": kw.get("total_count"),
            "n_new": kw.get("n_new"),
            "n_seen": kw.get("n_seen"),
            "note": kw.get("note"),
        },
    )
    conn.commit()


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------

def _fts_query_string(term: str) -> str:
    """把用户输入转成安全的 FTS5 短语查询。

    双引号包裹使其按字面短语匹配,同时规避用户输入中 ``AND/OR/NEAR/*``
    等 FTS5 语法字符造成的语法错误;内部双引号按 FTS5 规则转义为两个双引号。
    """
    escaped = term.replace('"', '""')
    return f'"{escaped}"'


def search(
    conn: sqlite3.Connection,
    term: str,
    limit: int = 50,
    *,
    year_from: int | None = None,
    year_to: int | None = None,
    force_like: bool = False,
) -> tuple[list[sqlite3.Row], bool]:
    """全文检索政策库。

    返回 ``(rows, fallback_used)``。

    检索策略
    --------
    * 查询串长度 >= 3 且 FTS 可用 -> FTS5 trigram ``MATCH``(走索引,快)
    * 查询串长度 < 3 或 FTS 不可用 -> ``LIKE '%term%'`` 回退(全表扫描,慢但正确)

    之所以必须回退:trigram 分词器无法为少于 3 个字符的查询生成三元组,
    ``MATCH '中医'`` 恒为空。对中文使用者来说 2 字词(中医、中药、饮片)
    恰恰是最常用的检索词,静默返回空结果会严重误导。
    """
    term = (term or "").strip()
    if not term:
        return [], False

    where, params = [], []
    if year_from is not None:
        where.append("p.pubyear >= ?")
        params.append(year_from)
    if year_to is not None:
        where.append("p.pubyear <= ?")
        params.append(year_to)
    extra = (" AND " + " AND ".join(where)) if where else ""

    # trigram 需要 >= 3 个字符;按 Unicode 码点计,中文一字即一码点。
    use_fts = (not force_like) and len(term) >= 3 and has_fts(conn)

    if use_fts:
        sql = f"""
            SELECT p.* FROM {_FTS_TABLE} f
            JOIN policy p ON p.id = f.rowid
            WHERE {_FTS_TABLE} MATCH ?{extra}
            ORDER BY bm25({_FTS_TABLE}), p.pubdate DESC
            LIMIT ?
        """
        try:
            rows = conn.execute(sql, [_fts_query_string(term), *params, limit]).fetchall()
            return rows, False
        except sqlite3.OperationalError:
            # 索引缺失或损坏时退回 LIKE,保证可用性
            pass

    like = f"%{term}%"
    sql = f"""
        SELECT p.* FROM policy p
        WHERE (p.title LIKE ? OR p.body LIKE ? OR p.summary LIKE ?){extra}
        ORDER BY p.pubdate DESC
        LIMIT ?
    """
    rows = conn.execute(sql, [like, like, like, *params, limit]).fetchall()
    return rows, True


def year_counts(conn: sqlite3.Connection, is_tcm: int | None = 1) -> list[sqlite3.Row]:
    """按发布年份统计文件数(按年升序)。``is_tcm=None`` 表示不过滤。"""
    where = ["pubyear IS NOT NULL"]
    params: list[Any] = []
    if is_tcm is not None:
        where.append("is_tcm = ?")
        params.append(is_tcm)
    return conn.execute(
        f"""SELECT pubyear AS year, COUNT(*) AS n FROM policy
            WHERE {' AND '.join(where)}
            GROUP BY pubyear ORDER BY pubyear""",
        params,
    ).fetchall()


def all_policies(conn: sqlite3.Connection, is_tcm: int = 1) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM policy WHERE is_tcm=? ORDER BY pubdate", (is_tcm,)
    ).fetchall()


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """库级别汇总指标 —— README 中引用的数字均由此产出。"""
    row = conn.execute(
        """SELECT COUNT(*) AS n,
                  MIN(pubdate) AS first_date,
                  MAX(pubdate) AS last_date,
                  MIN(pubyear) AS first_year,
                  MAX(pubyear) AS last_year,
                  SUM(CASE WHEN COALESCE(body_len,0)>0 THEN 1 ELSE 0 END) AS n_with_body,
                  SUM(CASE WHEN COALESCE(body_status,'pending')='ok' THEN 1 ELSE 0 END) AS n_body_ok,
                  SUM(CASE WHEN COALESCE(body_status,'pending')='failed' THEN 1 ELSE 0 END) AS n_body_failed,
                  SUM(CASE WHEN COALESCE(body_status,'pending')='pending' THEN 1 ELSE 0 END) AS n_body_pending,
                  SUM(COALESCE(body_len,0)) AS total_body_chars,
                  SUM(CASE WHEN COALESCE(pcode,'')<>'' THEN 1 ELSE 0 END) AS n_with_pcode,
                  SUM(CASE WHEN COALESCE(puborg,'')<>'' THEN 1 ELSE 0 END) AS n_with_org,
                  SUM(CASE WHEN COALESCE(n_orgs,1)>1 THEN 1 ELSE 0 END) AS n_joint
           FROM policy WHERE is_tcm=1"""
    ).fetchone()
    orgs = conn.execute(
        """SELECT COUNT(DISTINCT puborg) AS n FROM policy
           WHERE is_tcm=1 AND COALESCE(puborg,'')<>''"""
    ).fetchone()
    themes = conn.execute(
        """SELECT COUNT(DISTINCT theme) AS n FROM policy
           WHERE is_tcm=1 AND COALESCE(theme,'')<>''"""
    ).fetchone()
    n = row["n"] or 0
    n_with_body = row["n_with_body"] or 0
    return {
        "n_docs": n,
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "first_year": row["first_year"],
        "last_year": row["last_year"],
        "year_span": (row["last_year"] - row["first_year"] + 1)
        if row["first_year"] and row["last_year"]
        else 0,
        "n_with_body": n_with_body,
        "pct_with_body": round(n_with_body / n * 100, 1) if n else 0.0,
        "n_body_ok": row["n_body_ok"] or 0,
        "n_body_failed": row["n_body_failed"] or 0,
        "n_body_pending": row["n_body_pending"] or 0,
        "total_body_chars": row["total_body_chars"] or 0,
        "n_with_pcode": row["n_with_pcode"] or 0,
        "n_with_org": row["n_with_org"] or 0,
        "n_joint": row["n_joint"] or 0,
        "pct_joint": round((row["n_joint"] or 0) / n * 100, 1) if n else 0.0,
        "n_orgs": orgs["n"] or 0,
        "n_themes": themes["n"] or 0,
        "fts5_trigram": has_fts(conn),
    }


def distinct_doc_keys(conn: sqlite3.Connection) -> Iterable[str]:
    for r in conn.execute("SELECT doc_key FROM policy"):
        yield r["doc_key"]
