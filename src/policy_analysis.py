"""中医药政策变迁分析:时间线、发文机关、主题演变与阶段断点检测。

分析口径说明(重要)
------------------
所有统计**只基于本工具实际采集到的语料**,即"中国政府网政策文件库中
标题命中特定关键词的公开文件"。它**不等于**中国中医药政策的全集:

* 政策文件库收录范围本身有取舍,且不收录全部地方性文件;
* 本工具按**标题**匹配,正文提及但标题不含关键词的文件不在库内;
* 存在**回溯性收录偏差** —— 早期(2000 年前)电子化文件少,年份计数偏低,
  因此"某年文件少"不能直接解释为"该年政策关注度低"。

在报告与解读中必须保留这一限定。本模块提供的数字用于描述**本语料**的
结构特征,而非推断总体政策强度。
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Iterable, Sequence

from . import db

# ---------------------------------------------------------------------------
# 政策主题分类
# ---------------------------------------------------------------------------
# 采用**有序**规则:先匹配到的主题优先落库。顺序按"特异性由高到低"排列,
# 避免宽泛规则(如"中医药")吞掉具体主题(如"中药质量监管")。
# 每条规则 = (主题名, [正则关键词])。正则用于覆盖"医保/医疗保障"等变体。
THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("中药质量与标准", (r"中药质量", r"质量标准", r"中药饮片", r"配方颗粒", r"中药标准", r"炮制")),
    ("经典名方与传承创新", (r"经典名方", r"传承创新", r"古代经典名方", r"老字号", r"传统知识")),
    ("医保与价格", (r"医保", r"医疗保障", r"价格", r"支付", r"集采", r"药品目录")),
    ("医院管理与服务体系", (r"中医医院", r"中医馆", r"中医科", r"服务体系", r"基层", r"社区卫生", r"乡镇卫生院", r"评审标准")),
    ("人才培养与教育", (r"人才", r"教育", r"师承", r"培训", r"院校", r"住院医师")),
    ("科研创新与产业发展", (r"科研", r"科技创新", r"产业", r"研发", r"实验室", r"工业", r"制造")),
    ("法规与监督执法", (r"法\b", r"条例", r"监督", r"执法", r"备案", r"注册", r"许可", r"合规")),
    ("民族药与少数民族医药", (r"民族药", r"少数民族", r"藏医", r"蒙医", r"维医", r"壮医", r"苗医")),
    ("治未病与健康促进", (r"治未病", r"养生", r"保健", r"健康促进", r"康复", r"预防")),
    ("国际化与对外合作", (r"国际", r"海外", r"走出去", r"一带一路", r"港澳台", r"对外")),
    ("疫情防控与公共卫生", (r"疫情", r"新冠", r"公共卫生", r"传染病", r"应急")),
    ("规划与综合改革", (r"规划", r"实施方案", r"意见", r"改革", r"试点", r"纲要")),
)

# 主题抽取用的领域词表:政策文本中真正有分析价值的术语,
# 而非通用词。用于词频随时间窗口的演变分析。
DOMAIN_KEYWORDS: tuple[str, ...] = (
    "中医药", "中药", "中医", "中西医结合", "中药饮片", "中成药", "经典名方",
    "中医医院", "中医馆", "基层中医药", "治未病", "康复", "养生保健",
    "民族药", "中药配方颗粒", "中药材", "中药质量", "炮制", "质量标准",
    "医保支付", "医疗服务价格", "药品集中采购", "医保目录", "DRG", "按病种付费",
    "人才培养", "师承教育", "住院医师规范化培训", "科研创新", "重点实验室",
    "产业发展", "中药工业", "智能制造", "国际化", "一带一路",
    "传承创新", "古籍", "传统知识", "知识产权",
    "分级诊疗", "医联体", "县域", "绩效考核", "评审标准",
    "监督管理", "药品监管", "不良反应", "药物警戒", "追溯",
    "公共卫生", "传染病", "疫情防控", "应急预案",
)

# 里程碑政策:人工确认的标志性文件,用于时间线标注。
# 键为(年份, 标题特征子串),仅当语料中确实存在匹配项时才标注 —— 
# 不做任何"补造"。
MILESTONES: tuple[tuple[int, str, str], ...] = (
    (2009, "关于扶持和促进中医药事业发展的若干意见", "新医改背景下首个国家级中医药扶持文件"),
    (2016, "中华人民共和国中医药法", "中医药第一部专门法律"),
    (2016, "中医药发展战略规划纲要", "中长期战略规划"),
    (2017, "中华人民共和国中医药法", "《中医药法》正式施行"),
    (2019, "关于促进中医药传承创新发展的意见", "中共中央、国务院首个中医药专门意见(landmark)"),
    (2021, "关于加快中医药特色发展的若干政策措施", "特色发展政策措施"),
    (2022, "十四五", "“十四五”中医药发展规划"),
    (2023, "中医药振兴发展重大工程", "重大工程实施方案"),
    (2025, "提升中药质量", "中药质量与产业高质量发展意见"),
)

# 政策阶段划分:用于对比不同时期的主题结构。
# 断点选取依据中医药治理的关键制度节点(2009 新医改、2016 中医药法、
# 2019 传承创新意见、2021 十四五),而非数据驱动的自动分段。
POLICY_ERAS: tuple[tuple[str, int, int], ...] = (
    ("奠基期 (2003-2008)", 2003, 2008),
    ("新医改期 (2009-2015)", 2009, 2015),
    ("法治化期 (2016-2018)", 2016, 2018),
    ("传承创新期 (2019-2020)", 2019, 2020),
    ("十四五期 (2021-2025)", 2021, 2025),
    ("十五五期 (2026-)", 2026, 2100),
)


# ---------------------------------------------------------------------------
# 主题标注与关键词抽取
# ---------------------------------------------------------------------------

def classify_theme(title: str, category: str = "", body: str = "") -> str:
    """按规则给单篇政策打主题标签。

    以**标题为主**判断依据(权重最高),标题无法判定时才看正文开头,
    因为正文常大段引述其他文件,容易误判主题。
    """
    haystack = f"{title or ''} {category or ''}"
    for theme, patterns in THEME_RULES:
        for pat in patterns:
            if re.search(pat, haystack):
                return theme
    if body:
        head = body[:1500]
        for theme, patterns in THEME_RULES:
            for pat in patterns:
                if re.search(pat, head):
                    return theme
    return "其他"


def extract_keywords(text: str) -> dict[str, int]:
    """统计领域词表在文本中的出现次数。

    使用 ``str.count`` 做非重叠计数并累加。注意:中文无词边界,
    "中药"会被计入"中药材""中药饮片"等更长词内部,故该计数是
    **主题热度指标**而非严格词频,报告中按此口径解释。
    """
    counts: dict[str, int] = {}
    if not text:
        return counts
    for kw in DOMAIN_KEYWORDS:
        n = text.count(kw)
        if n:
            counts[kw] = n
    return counts


def annotate_themes(conn: sqlite3.Connection) -> int:
    """为全部记录计算主题标签,返回更新条数。"""
    rows = conn.execute("SELECT id, title, category, body FROM policy").fetchall()
    updates = []
    for r in rows:
        theme = classify_theme(r["title"], r["category"] or "", r["body"] or "")
        updates.append((theme, r["id"]))
    conn.executemany("UPDATE policy SET theme=? WHERE id=?", updates)
    conn.commit()
    return len(updates)


def extract_keywords_bulk(conn: sqlite3.Connection) -> int:
    """为全部记录抽取领域关键词并入库,返回处理条数。"""
    rows = conn.execute("SELECT id, title, body, summary FROM policy").fetchall()
    n = 0
    for r in rows:
        text = f"{r['title']}\n{r['body'] or ''}\n{r['summary'] or ''}"
        counts = extract_keywords(text)
        if counts:
            db.save_keywords(conn, r["id"], counts)
            n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 时间线分析
# ---------------------------------------------------------------------------

def timeline_by_year(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """逐年文件数与**同比变化率**。

    变化率的用途是识别发布节奏的转折点。增长率在基数极小
    (前一年 < 3 篇)时不稳定,故该情形返回 None 而非虚高百分比。
    """
    rows = db.year_counts(conn)
    out: list[dict[str, Any]] = []
    prev: int | None = None
    for r in rows:
        year, n = int(r["year"]), int(r["n"])
        if prev is None:
            yoy = None
        elif prev < 3:
            yoy = None  # 基数过小,增长率无解释力
        else:
            yoy = round((n - prev) / prev * 100, 1)
        out.append({"year": year, "n": n, "yoy_pct": yoy, "prev_n": prev})
        prev = n
    return out


def timeline_completeness(conn: sqlite3.Connection) -> dict[str, Any]:
    """评估逐年计数的**收录完整度**,用于诚实披露数据局限。

    2000 年前后政策文件库电子化程度差异极大。若直接画逐年折线,
    早期的低值会被误读为"政策关注度低"。因此这里显式给出:
    起始年份、各年文件数、以及文件数少于 3 篇的年份列表。
    """
    rows = db.year_counts(conn)
    years = [int(r["year"]) for r in rows]
    sparse = [int(r["year"]) for r in rows if int(r["n"]) < 3]
    counts = {int(r["year"]): int(r["n"]) for r in rows}
    return {
        "years": years,
        "counts": counts,
        "sparse_years": sparse,
        "first_year": min(years) if years else None,
        "last_year": max(years) if years else None,
        "n_years_covered": len(years),
        "note": "收录完整度受政策文件库电子化历史限制,早期年份计数偏低,"
                "不可解释为政策强度的实际变化。",
    }


def timeline_by_agency(conn: sqlite3.Connection, top: int = 20) -> list[dict[str, Any]]:
    """按发文机关统计。含占比与年份跨度。

    统计口径
    --------
    机关名称已在采集阶段归一化(见 ``policy_crawler.ORG_ALIASES``),
    ``puborg`` 存的是**主要发文机关**(联合发文的第一个规范名),
    因此本表的"文件数"是"以该机关为主要发文机关的文件数",
    各机关计数之和等于文件总数,便于核对。

    另提供 ``n_joint``(该机关参与的联合发文数)与 ``n_sole``,
    用于区分单独发文与牵头联合发文。
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM policy WHERE is_tcm=1").fetchone()["n"]
    rows = conn.execute(
        """SELECT puborg, COUNT(*) AS n, MIN(pubyear) AS y0, MAX(pubyear) AS y1,
                  SUM(CASE WHEN COALESCE(n_orgs,1) > 1 THEN 1 ELSE 0 END) AS n_joint
           FROM policy
           WHERE is_tcm=1 AND COALESCE(puborg,'')<>''
           GROUP BY puborg ORDER BY n DESC, puborg"""
    ).fetchall()
    out = []
    for r in rows[:top]:
        n = int(r["n"])
        n_joint = int(r["n_joint"] or 0)
        out.append(
            {
                "puborg": r["puborg"],
                "n": n,
                "share_pct": round(n / total * 100, 1) if total else 0.0,
                "first_year": r["y0"],
                "last_year": r["y1"],
                "n_joint": n_joint,
                "n_sole": n - n_joint,
                "joint_pct": round(n_joint / n * 100, 1) if n else 0.0,
            }
        )
    return out


def participation_by_agency(conn: sqlite3.Connection, top: int = 20) -> list[dict[str, Any]]:
    """按"参与发文"口径统计机关影响力。

    ``timeline_by_agency`` 只算主要发文机关,会低估在联合发文中排名靠后的
    机关(如国家中医药管理局常出现在多部委联合发文的中间位置)。
    本函数按 ``puborg_all``(全部参与机关)拆分统计,**分母是机关提及次数**,
    与文件数不同 —— 两个口径各有用途,不可混用。
    """
    rows = conn.execute(
        "SELECT puborg_all FROM policy WHERE is_tcm=1 AND COALESCE(puborg_all,'')<>''"
    ).fetchall()
    counter: Counter[str] = Counter()
    n_docs = 0
    for r in rows:
        orgs = [o.strip() for o in (r["puborg_all"] or "").split("、") if o.strip()]
        if not orgs:
            continue
        n_docs += 1
        for o in set(orgs):     # 同一文件内去重(机关列表本身已去重,双保险)
            counter[o] += 1
    total_mentions = sum(counter.values())
    return [
        {
            "puborg": org,
            "n_docs_participated": n,
            "share_of_mentions_pct": round(n / total_mentions * 100, 1) if total_mentions else 0.0,
            "share_of_docs_pct": round(n / n_docs * 100, 1) if n_docs else 0.0,
        }
        for org, n in counter.most_common(top)
    ]


def timeline_by_theme(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """按政策主题统计(含首末年份)。"""
    total = conn.execute("SELECT COUNT(*) AS n FROM policy WHERE is_tcm=1").fetchone()["n"]
    rows = conn.execute(
        """SELECT theme, COUNT(*) AS n, MIN(pubyear) AS y0, MAX(pubyear) AS y1
           FROM policy WHERE is_tcm=1 AND COALESCE(theme,'')<>''
           GROUP BY theme ORDER BY n DESC"""
    ).fetchall()
    return [
        {
            "theme": r["theme"],
            "n": int(r["n"]),
            "share_pct": round(int(r["n"]) / total * 100, 1) if total else 0.0,
            "first_year": r["y0"],
            "last_year": r["y1"],
        }
        for r in rows
    ]


def theme_by_year_matrix(conn: sqlite3.Connection) -> dict[str, Any]:
    """主题 × 年份 交叉表,用于观察主题结构如何随时间迁移。"""
    rows = conn.execute(
        """SELECT theme, pubyear, COUNT(*) AS n FROM policy
           WHERE is_tcm=1 AND pubyear IS NOT NULL AND COALESCE(theme,'')<>''
           GROUP BY theme, pubyear"""
    ).fetchall()
    themes = sorted({r["theme"] for r in rows})
    years = sorted({int(r["pubyear"]) for r in rows})
    matrix = {t: {y: 0 for y in years} for t in themes}
    for r in rows:
        matrix[r["theme"]][int(r["pubyear"])] = int(r["n"])
    return {"themes": themes, "years": years, "matrix": matrix}


# ---------------------------------------------------------------------------
# 主题演变(关键词随时间窗口)
# ---------------------------------------------------------------------------

def keyword_evolution(
    conn: sqlite3.Connection, *, window: int = 3, top: int = 15
) -> dict[str, Any]:
    """按固定年份窗口统计主题关键词词频演变。

    采用**文档覆盖率**(含该词的文档数 / 该窗口文档总数)而非原始词频:
    原始词频受文本长度影响极大(长文件天然词多),覆盖率才是可比的热度指标。
    """
    rows = conn.execute(
        """SELECT k.keyword AS kw, p.pubyear AS year, COUNT(*) AS n
           FROM policy_keyword k JOIN policy p ON p.id = k.policy_id
           WHERE p.is_tcm=1 AND p.pubyear IS NOT NULL
           GROUP BY k.keyword, p.pubyear"""
    ).fetchall()
    if not rows:
        return {"windows": [], "keywords": [], "coverage": {}}

    years = sorted({int(r["year"]) for r in rows})
    y0 = years[0]
    # 窗口按起始年份切分,保证窗口宽度一致(末窗可短于 window,单独标注)
    windows: list[tuple[int, int]] = []
    start = y0
    while start <= years[-1]:
        windows.append((start, min(start + window - 1, years[-1])))
        start += window

    docs_per_window = {}
    for a, b in windows:
        docs_per_window[(a, b)] = conn.execute(
            "SELECT COUNT(*) AS n FROM policy WHERE is_tcm=1 AND pubyear BETWEEN ? AND ?",
            (a, b),
        ).fetchone()["n"]

    total_kw: Counter[str] = Counter()
    for r in rows:
        total_kw[r["kw"]] += int(r["n"])
    top_kws = [k for k, _ in total_kw.most_common(top)]

    per_kw: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        y = int(r["year"])
        for a, b in windows:
            if a <= y <= b:
                per_kw[r["kw"]][f"{a}-{b}"] += int(r["n"])
                break

    labels = [f"{a}-{b}" for a, b in windows]
    coverage: dict[str, list[float]] = {}
    for kw in top_kws:
        vals = []
        for (a, b), lab in zip(windows, labels):
            denom = docs_per_window[(a, b)] or 1
            # 分子是该窗口内含此关键词的**文档数**(去重),故需单独查询
            num = conn.execute(
                """SELECT COUNT(DISTINCT p.id) AS n FROM policy_keyword k
                   JOIN policy p ON p.id=k.policy_id
                   WHERE p.is_tcm=1 AND k.keyword=? AND p.pubyear BETWEEN ? AND ?""",
                (kw, a, b),
            ).fetchone()["n"]
            vals.append(round(num / denom * 100, 1))
        coverage[kw] = vals

    return {
        "windows": labels,
        "keywords": top_kws,
        "coverage": coverage,
        "docs_per_window": {f"{a}-{b}": docs_per_window[(a, b)] for a, b in windows},
        "window_size": window,
        "metric": "文档覆盖率(%),即窗口内含该关键词的文件数 / 窗口内文件总数",
    }


def keyword_trend(kw_coverage: dict[str, Any], keyword: str) -> str:
    """给出单词的时间趋势判断(上升/下降/平稳),基于覆盖率首末窗口比较。

    阈值设为 5 个百分点:低于此值的波动更可能来自窗口内文件数变化,
    不具备解释力,故判为"平稳"。
    """
    cov = kw_coverage.get("coverage", {})
    if keyword not in cov or len(cov[keyword]) < 2:
        return "数据不足"
    series = cov[keyword]
    delta = series[-1] - series[0]
    if delta >= 5:
        return "上升"
    if delta <= -5:
        return "下降"
    return "平稳"


# ---------------------------------------------------------------------------
# 阶段划分与断点检测
# ---------------------------------------------------------------------------

def era_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """按 POLICY_ERAS 划分阶段,汇总各阶段文件量与主导主题。"""
    out = []
    for name, a, b in POLICY_ERAS:
        rows = conn.execute(
            """SELECT theme, COUNT(*) AS n FROM policy
               WHERE is_tcm=1 AND pubyear BETWEEN ? AND ?
               GROUP BY theme ORDER BY n DESC""",
            (a, b),
        ).fetchall()
        total = sum(int(r["n"]) for r in rows)
        if total == 0:
            continue
        org = conn.execute(
            """SELECT puborg, COUNT(*) AS n FROM policy
               WHERE is_tcm=1 AND pubyear BETWEEN ? AND ? AND COALESCE(puborg,'')<>''
               GROUP BY puborg ORDER BY n DESC LIMIT 3""",
            (a, b),
        ).fetchall()
        y0 = conn.execute(
            "SELECT MIN(pubdate) AS d FROM policy WHERE is_tcm=1 AND pubyear BETWEEN ? AND ?",
            (a, b),
        ).fetchone()["d"]
        y1 = conn.execute(
            "SELECT MAX(pubdate) AS d FROM policy WHERE is_tcm=1 AND pubyear BETWEEN ? AND ?",
            (a, b),
        ).fetchone()["d"]
        out.append(
            {
                "era": name,
                "year_from": a,
                "year_to": b if b < 2100 else None,
                "n_docs": total,
                "date_from": y0,
                "date_to": y1,
                "top_themes": [
                    {"theme": r["theme"], "n": int(r["n"]),
                     "share_pct": round(int(r["n"]) / total * 100, 1)}
                    for r in rows[:3]
                ],
                "top_orgs": [{"puborg": r["puborg"], "n": int(r["n"])} for r in org],
            }
        )
    return out


def detect_shifts(
    conn: sqlite3.Connection, *, z_threshold: float = 1.5
) -> list[dict[str, Any]]:
    """检测发布量的结构性跃变年份。

    方法:对年度计数序列做**稳健**标准化(中位数/MAD),标记显著偏离年份。
    用 MAD 而非标准差,是因为标准差本身被离群年份抬高,会掩盖断点。
    MAD = 0(如多年计数相同)时退化为均值比较,避免除零。

    这是**描述性**的断点提示,不是因果推断 —— 某年文件激增可能源于
    政策密集出台,也可能源于文件库补录,需结合里程碑逐条核验。
    """
    rows = db.year_counts(conn)
    if len(rows) < 4:
        return []
    counts = [int(r["n"]) for r in rows]
    med = _median(counts)
    mad = _median([abs(c - med) for c in counts])
    out: list[dict[str, Any]] = []
    for r in rows:
        n = int(r["n"])
        if mad > 0:
            z = 0.6745 * (n - med) / mad
        else:
            mean = sum(counts) / len(counts)
            z = 0.0 if mean == 0 else (n - mean) / mean
        if abs(z) >= z_threshold:
            out.append(
                {
                    "year": int(r["year"]),
                    "n": n,
                    "z_robust": round(z, 2),
                    "direction": "激增" if z > 0 else "低谷",
                    "median": med,
                    "mad": mad,
                }
            )
    return sorted(out, key=lambda d: d["year"])


def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def milestones_present(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """在**实际语料**中定位里程碑文件。

    仅当语料中确有匹配记录时才输出 —— 缺失的里程碑会被列出并标记为
    ``found=False``,用于诚实披露语料覆盖不足,而不是假装它存在。
    """
    out = []
    for year, needle, desc in MILESTONES:
        row = conn.execute(
            """SELECT title, pubdate, puborg, pcode, url FROM policy
               WHERE is_tcm=1 AND pubyear=? AND title LIKE ?
               ORDER BY length(title) LIMIT 1""",
            (year, f"%{needle}%"),
        ).fetchone()
        out.append(
            {
                "year": year,
                "marker": needle,
                "description": desc,
                "found": row is not None,
                "title": row["title"] if row else None,
                "pubdate": row["pubdate"] if row else None,
                "puborg": row["puborg"] if row else None,
                "pcode": row["pcode"] if row else None,
                "url": row["url"] if row else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def full_analysis(conn: sqlite3.Connection) -> dict[str, Any]:
    """一次性产出报告所需的全部政策分析结果。"""
    return {
        "stats": db.stats(conn),
        "timeline_year": timeline_by_year(conn),
        "completeness": timeline_completeness(conn),
        "by_agency": timeline_by_agency(conn),
        "by_agency_participation": participation_by_agency(conn),
        "by_theme": timeline_by_theme(conn),
        "theme_year": theme_by_year_matrix(conn),
        "keyword_evolution": keyword_evolution(conn),
        "eras": era_summary(conn),
        "shifts": detect_shifts(conn),
        "milestones": milestones_present(conn),
    }


def to_dataframes(analysis: dict[str, Any]) -> dict[str, Any]:
    """把分析结果转成 pandas DataFrame,便于导出 XLSX/CSV。

    pandas 是硬依赖之一(环境中已提供);若缺失则由调用方跳过表格导出。
    """
    import pandas as pd

    dfs: dict[str, Any] = {}
    if analysis.get("timeline_year"):
        dfs["timeline_year"] = pd.DataFrame(analysis["timeline_year"])
    if analysis.get("by_agency"):
        dfs["timeline_by_agency"] = pd.DataFrame(analysis["by_agency"])
    if analysis.get("by_agency_participation"):
        dfs["by_agency_participation"] = pd.DataFrame(analysis["by_agency_participation"])
    if analysis.get("by_theme"):
        dfs["timeline_by_theme"] = pd.DataFrame(analysis["by_theme"])
    if analysis.get("eras"):
        rows = []
        for e in analysis["eras"]:
            rows.append(
                {
                    "era": e["era"],
                    "year_from": e["year_from"],
                    "year_to": e["year_to"],
                    "n_docs": e["n_docs"],
                    "top_theme": e["top_themes"][0]["theme"] if e["top_themes"] else "",
                    "top_theme_n": e["top_themes"][0]["n"] if e["top_themes"] else 0,
                    "top_org": e["top_orgs"][0]["puborg"] if e["top_orgs"] else "",
                }
            )
        dfs["eras"] = pd.DataFrame(rows)
    ke = analysis.get("keyword_evolution") or {}
    if ke.get("coverage"):
        dfs["keyword_coverage"] = pd.DataFrame(
            ke["coverage"], index=ke["windows"]
        ).T.reset_index(names="keyword")
    if analysis.get("shifts"):
        dfs["shifts"] = pd.DataFrame(analysis["shifts"])
    if analysis.get("milestones"):
        dfs["milestones"] = pd.DataFrame(analysis["milestones"])
    ty = analysis.get("theme_year") or {}
    if ty.get("matrix"):
        mat = pd.DataFrame(ty["matrix"]).fillna(0).astype(int)
        dfs["theme_year_matrix"] = mat.reset_index(names="theme")
    return dfs
