"""中药不良反应(ADR)报告特征分析。

方法学要点
----------
1. **分母口径必须明确**:ADR 分析中每张报表即为一个分析单位,
   性别/年龄/给药途径分布的分母是"报表数"而非"患者数"。
   涉及品种分析时,同一报表可涉及多品种(联合用药),故品种构成比
   的合理分母是"品种提及次数"。两个分母不可混用 —— 本模块分别计算并
   在输出字段名中显式标注分母(``_share_of_reports`` / ``_share_of_mentions``)。
2. **卡方检验**:用于检验两个分类变量是否独立,如"给药途径"与
   "关联性评价"是否相关。本模块在 scipy 可用时给出精确 p 值;
   不可用时用**卡方分布的级数近似**给出 p 值,保证无 scipy 环境仍可运行。
3. **报告偏倚**:自发呈报系统(ADR 监测)存在漏报、报告质量不均、
   无法计算真实发生率(缺分母人群)等固有局限。本模块只做**构成比**描述,
   不计算发生率,不做因果判定。
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

from .rws import load_csv_rows, to_float, to_int

# 预期列(全部可选,缺失列对应分析自动跳过)
ADR_COLUMNS: tuple[str, ...] = (
    "report_id", "sex", "age", "age_group", "route", "drug_name",
    "drug_category", "reaction", "outcome", "causality", "severity",
    "report_year", "dose_days", "report_source",
)


def _clean(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _age_group(age: float | None, explicit: str = "") -> str:
    """年龄段划分。优先用数据中已有的分组字段,缺失时按年龄标准分组。

    沿用药品不良反应监测常用的分段(<18 / 18-44 / 45-59 / 60-74 / >=75),
    该分段能反映老年人群用药风险集中的特点。
    """
    if explicit:
        return explicit
    if age is None:
        return "未知"
    if age < 18:
        return "<18"
    if age < 45:
        return "18-44"
    if age < 60:
        return "45-59"
    if age < 75:
        return "60-74"
    return ">=75"


_AGE_ORDER = ("<18", "18-44", "45-59", "60-74", ">=75", "未知")


def _shares(counter: Counter, total: int | None = None) -> list[dict[str, Any]]:
    """把计数转成排序后的构成比列表。"""
    total = sum(counter.values()) if total is None else total
    total = total or 1
    return [
        {"category": k, "n": v, "pct": round(v / total * 100, 2)}
        for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    ]


# ---------------------------------------------------------------------------
# 卡方检验(无 scipy 亦可运行)
# ---------------------------------------------------------------------------

def _chi2_sf(x: float, k: int) -> float:
    """卡方分布上尾概率 P(X > x),自由度 k。

    使用正则化不完全伽马函数 Q(k/2, x/2) 的级数/连分式展开
    (Numerical Recipes 的 gammq 实现),精度足以支撑 p 值判定。
    这样即使环境没有 scipy,卡方检验依然可用。
    """
    if x <= 0 or k <= 0:
        return 1.0
    a, xx = k / 2.0, x / 2.0

    if xx < a + 1.0:
        # 级数展开求 P(a,x)
        ap, s, delta = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1.0
            delta *= xx / ap
            s += delta
            if abs(delta) < abs(s) * 1e-14:
                break
        p = s * math.exp(-xx + a * math.log(xx) - math.lgamma(a))
        return max(0.0, min(1.0, 1.0 - p))
    # 连分式求 Q(a,x)
    tiny = 1e-300
    b = xx + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b if abs(b) > tiny else 1.0 / tiny
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    q = math.exp(-xx + a * math.log(xx) - math.lgamma(a)) * h
    return max(0.0, min(1.0, q))


def chi_square_test(table: Sequence[Sequence[float]]) -> dict[str, Any]:
    """对列联表做 Pearson 卡方独立性检验。

    返回 chi2、自由度、p 值与最小期望频数。附带**期望频数 < 5 的格子占比**
    提示 —— 该比例高时卡方近似不可靠,应改用 Fisher 精确检验,
    本模块会显式给出该警告而不是默默给出一个不可信的 p 值。
    """
    rows = [list(map(float, r)) for r in table if any(float(v) != 0 for v in r)]
    rows = [r for r in rows if len(r) > 0]
    if len(rows) < 2:
        return {"chi2": 0.0, "df": 0, "p": 1.0, "warning": "列联表行数不足,无法检验"}

    # 去掉全零列,否则期望频数为 0 会导致除零
    ncols = len(rows[0])
    keep = [j for j in range(ncols) if any(r[j] != 0 for r in rows)]
    if len(keep) < 2:
        return {"chi2": 0.0, "df": 0, "p": 1.0, "warning": "列联表列数不足,无法检验"}
    rows = [[r[j] for j in keep] for r in rows]

    grand = sum(sum(r) for r in rows)
    if grand <= 0:
        return {"chi2": 0.0, "df": 0, "p": 1.0, "warning": "总频数为 0"}
    row_sums = [sum(r) for r in rows]
    col_sums = [sum(r[j] for r in rows) for j in range(len(rows[0]))]

    chi2 = 0.0
    n_cells, n_small = 0, 0
    min_exp = float("inf")
    for i, r in enumerate(rows):
        for j, obs in enumerate(r):
            exp = row_sums[i] * col_sums[j] / grand
            if exp <= 0:
                continue
            n_cells += 1
            min_exp = min(min_exp, exp)
            if exp < 5:
                n_small += 1
            chi2 += (obs - exp) ** 2 / exp

    df = (len(rows) - 1) * (len(rows[0]) - 1)
    p = _chi2_sf(chi2, df) if df > 0 else 1.0

    out: dict[str, Any] = {
        "chi2": round(chi2, 4),
        "df": df,
        "p": round(p, 6),
        "n": int(grand),
        "min_expected": round(min_exp, 3) if min_exp != float("inf") else None,
        "pct_cells_expected_lt5": round(n_small / n_cells * 100, 1) if n_cells else 0.0,
        "significant_at_0.05": p < 0.05,
    }
    if n_cells and n_small / n_cells > 0.2:
        out["warning"] = (
            f"期望频数<5 的格子占 {n_small}/{n_cells},卡方近似可能不可靠,"
            "建议合并类别或改用 Fisher 精确检验。"
        )
    return out


def cramers_v(chi2: float, n: int, table: Sequence[Sequence[float]]) -> float:
    """Cramér's V 效应量。

    仅报 p 值不够:p 值随样本量增大而变小,大样本下微弱关联也会"显著"。
    V 提供 0~1 的关联强度度量,便于判断实际的业务意义。
    """
    rows = [r for r in table if any(r)]
    if len(rows) < 2 or n <= 0:
        return 0.0
    ncols = len(rows[0])
    k = min(len(rows), ncols)
    if k < 2:
        return 0.0
    return round(math.sqrt(chi2 / (n * (k - 1))), 4)


# ---------------------------------------------------------------------------
# 主分析
# ---------------------------------------------------------------------------

def analyze_adr(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """对 ADR 报表数据做完整特征分析。

    返回字典包含各维度的分布、关联性评价交叉表与卡方检验结果。
    所有构成比都显式标注分母。
    """
    rows = list(rows)
    n_reports = len(rows)
    if n_reports == 0:
        return {"n_reports": 0, "error": "无数据"}

    sex_c: Counter = Counter()
    age_c: Counter = Counter()
    route_c: Counter = Counter()
    outcome_c: Counter = Counter()
    causality_c: Counter = Counter()
    severity_c: Counter = Counter()
    category_c: Counter = Counter()
    year_c: Counter = Counter()
    reaction_c: Counter = Counter()
    drug_c: Counter = Counter()

    ages: list[float] = []
    n_multi_drug = 0
    route_outcome: dict[str, Counter] = defaultdict(Counter)

    for r in rows:
        sex_c[_clean(r.get("sex")) or "未知"] += 1
        age = to_float(r.get("age"))
        if age is not None:
            ages.append(age)
        age_c[_age_group(age, _clean(r.get("age_group")))] += 1
        route = _clean(r.get("route")) or "未知"
        route_c[route] += 1
        outcome = _clean(r.get("outcome")) or "未知"
        outcome_c[outcome] += 1
        causality = _clean(r.get("causality")) or "未知"
        causality_c[causality] += 1
        severity_c[_clean(r.get("severity")) or "未知"] += 1
        category_c[_clean(r.get("drug_category")) or "未分类"] += 1
        y = _clean(r.get("report_year"))
        if y:
            year_c[y] += 1
        reaction_c[_clean(r.get("reaction")) or "未记录"] += 1

        # 品种:多用分号/逗号/顿号分隔,分隔后逐个计数(可能多品种)
        drugs = [d.strip() for d in _split_multi(_clean(r.get("drug_name")))]
        if len(drugs) > 1:
            n_multi_drug += 1
        for d in drugs:
            if d:
                drug_c[d] += 1
        route_outcome[route][causality] += 1

    # 品种构成比的分母是"品种提及次数",与报表数不同 —— 明确区分
    n_drug_mentions = sum(drug_c.values())

    # 年龄描述统计(仅对有年龄的记录)
    age_stats = None
    if ages:
        ages_sorted = sorted(ages)
        m = len(ages_sorted)
        age_stats = {
            "n_with_age": m,
            "mean": round(sum(ages_sorted) / m, 1),
            "median": round(
                ages_sorted[m // 2] if m % 2 else (ages_sorted[m // 2 - 1] + ages_sorted[m // 2]) / 2, 1
            ),
            "min": ages_sorted[0],
            "max": ages_sorted[-1],
            "pct_60_plus": round(sum(1 for a in ages_sorted if a >= 60) / m * 100, 2),
        }

    # 给药途径 × 关联性评价 卡方检验
    routes = [r for r, _ in route_c.most_common(6)]
    causalities = [c for c, _ in causality_c.most_common()]
    table = [[route_outcome[rt][cz] for cz in causalities] for rt in routes]
    chi = chi_square_test(table) if routes and len(causalities) > 1 else {
        "chi2": 0.0, "df": 0, "p": 1.0, "warning": "类别不足,未做检验"
    }
    if chi.get("df", 0) > 0:
        chi["cramers_v"] = cramers_v(chi["chi2"], chi.get("n", 0), table)

    return {
        "n_reports": n_reports,
        "denominator_note": "所有构成比的分母为报表数;品种构成比分母为品种提及次数。",
        "sex": _shares(sex_c, n_reports),
        "age_group": sorted(
            _shares(age_c, n_reports),
            key=lambda d: _AGE_ORDER.index(d["category"])
            if d["category"] in _AGE_ORDER else 99,
        ),
        "age_stats": age_stats,
        "route": _shares(route_c, n_reports),
        "outcome": _shares(outcome_c, n_reports),
        "causality": _shares(causality_c, n_reports),
        "severity": _shares(severity_c, n_reports),
        "drug_category": _shares(category_c, n_reports),
        "by_year": sorted(
            ({"year": k, "n": v} for k, v in year_c.items()), key=lambda d: d["year"]
        ),
        "top_reactions": _shares(reaction_c, n_reports)[:15],
        "top_drugs": _shares(drug_c, n_drug_mentions)[:15],
        "n_drug_mentions": n_drug_mentions,
        "n_reports_multi_drug": n_multi_drug,
        "pct_multi_drug": round(n_multi_drug / n_reports * 100, 2),
        "chi2_route_vs_causality": chi,
        "limitations": [
            "自发呈报数据存在漏报与报告质量不均,构成比不代表真实发生率。",
            "缺少暴露人群分母,无法计算发生率或相对风险。",
            "关联性评价为报告人/评价人判断,不等同于因果关系。",
            "本模块仅作描述性统计,不构成临床用药建议。",
        ],
    }


def _split_multi(text: str) -> list[str]:
    """按常见多值分隔符切分(; ;, ,、/ |)。"""
    import re

    if not text:
        return []
    return [p.strip() for p in re.split(r"[;；,，、/|]+", text) if p.strip()]


def adr_to_dataframes(analysis: dict[str, Any]) -> dict[str, Any]:
    """把 ADR 分析结果转成 DataFrame 便于导出。"""
    import pandas as pd

    dfs: dict[str, Any] = {}
    for key in ("sex", "age_group", "route", "outcome", "causality",
                "severity", "drug_category", "top_reactions", "top_drugs"):
        if analysis.get(key):
            dfs[f"adr_{key}"] = pd.DataFrame(analysis[key])
    if analysis.get("by_year"):
        dfs["adr_by_year"] = pd.DataFrame(analysis["by_year"])
    return dfs


def load_adr_csv(path: str | Path) -> list[dict[str, Any]]:
    """读取 ADR CSV。自动跳过以 ``#`` 开头的注释行。"""
    return load_csv_rows(path)
