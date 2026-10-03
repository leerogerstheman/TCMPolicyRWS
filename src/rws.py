"""真实世界研究(RWS)通用工具:数据加载、类型转换、联合用药模式分析。

数据边界(重要)
--------------
本模块**只**处理使用者自行提供的 CSV。``examples/`` 下的演示数据是
**明确标注的合成数据**,用于验证代码路径,不含任何真实患者信息。
本工具不采集、不推断真实患者数据,也不做个体化临床判断。

支持的输入格式
--------------
CSV(UTF-8,可带 BOM),首行表头。以 ``#`` 开头的行视为注释并跳过,
便于在样例文件中写入说明。缺失值(空串、NA、NaN、-)统一转为 None。
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

_NA_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-", "--", "未知", "不详"}


# ---------------------------------------------------------------------------
# 读取与转换
# ---------------------------------------------------------------------------

def load_csv_rows(path: str | Path) -> list[dict[str, Any]]:
    """读取 CSV 为字典列表(UTF-8 / UTF-8-BOM 兼容,跳过 ``#`` 注释行)。

    用 ``utf-8-sig`` 解码可自动吞掉 Excel 另存为 CSV 时加入的 BOM,
    否则第一列列名会变成 ``\\ufeffreport_id`` 而导致整列读不到。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = []
    for raw in reader:
        row = {(k or "").strip(): (v if v is not None else "") for k, v in raw.items()}
        rows.append(row)
    return rows


def is_na(v: Any) -> bool:
    return v is None or str(v).strip().lower() in _NA_TOKENS


def to_float(v: Any) -> float | None:
    """宽松转 float。失败返回 None(不抛异常,避免单行脏数据中断分析)。"""
    if is_na(v):
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        # 容忍 "12.5mg"、"30 天" 这类带单位的写法
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None


def to_int(v: Any) -> int | None:
    f = to_float(v)
    return None if f is None else int(round(f))


def parse_date(v: Any) -> Any:
    """解析日期为 ``datetime.date``。支持 YYYY-MM-DD / YYYY/M/D / YYYYMMDD。

    返回 None 表示无法解析 —— 调用方需自行决定是跳过该行还是记录为缺失,
    而不是让异常冒泡。
    """
    from datetime import date, datetime

    if is_na(v):
        return None
    s = str(v).strip()
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def require_columns(rows: Sequence[dict[str, Any]], required: Iterable[str], what: str) -> None:
    """校验必需列是否存在,缺失时抛出带清晰提示的异常。

    早失败优于静默产出全 None 的错误结果。
    """
    if not rows:
        return
    present = set(rows[0].keys())
    missing = [c for c in required if c not in present]
    if missing:
        raise ValueError(
            f"{what} 缺少必需列: {missing};实际列: {sorted(present)}"
        )


# ---------------------------------------------------------------------------
# 联合用药模式分析
# ---------------------------------------------------------------------------

def _split_drugs(text: str) -> list[str]:
    if not text:
        return []
    return [p.strip() for p in re.split(r"[;；,，、/|+]+", str(text)) if p.strip()]


def coprescribing_analysis(
    rows: Iterable[dict[str, Any]],
    *,
    drug_field: str = "drug_names",
    group_field: str = "patient_id",
    top_pairs: int = 20,
    top_drugs: int = 15,
) -> dict[str, Any]:
    """中成药真实世界联合用药(合并用药)模式分析。

    分析单位是一个"用药记录组"(默认按 ``patient_id`` 聚合,即一次处方/一次
    就诊涉及的药品种类)。核心指标:

    * **多药联用率**:组内品种数 >= 2 的组占比
    * **联合用药对频次**:同组内共现的药品无序对计数
    * **药物共现网络**:每个品种的共现伙伴数(度中心性),识别联用枢纽品种

    注意事项:共现(association)不等于相互作用(interaction)。
    本模块只描述**处方层面的共存模式**,不推断配伍合理性、不评价安全性、
    不构成用药建议。真实的药物相互作用评估需药理与临床证据支持。
    """
    groups: dict[str, list[str]] = defaultdict(list)
    single_rows = 0
    for r in rows:
        key = (str(r.get(group_field) or "").strip())
        drugs = _split_drugs(r.get(drug_field))
        if not key:
            # 无分组键时退化为每条记录一组
            key = f"__row{single_rows}"
            single_rows += 1
        groups[key].extend(drugs)

    n_groups = len(groups)
    if n_groups == 0:
        return {"n_groups": 0, "error": "无数据"}

    drug_counter: Counter = Counter()
    pair_counter: Counter = Counter()
    drug_in_groups: Counter = Counter()   # 品种出现在多少个"组"中(去重)
    group_sizes: Counter = Counter()
    n_multi = 0

    for key, drugs in groups.items():
        uniq = sorted(set(d for d in drugs if d))
        group_sizes[len(uniq)] += 1
        if len(uniq) >= 2:
            n_multi += 1
        for d in uniq:
            drug_counter[d] += 1
            drug_in_groups[d] += 1
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pair_counter[(uniq[i], uniq[j])] += 1

    # 共现伙伴数(度中心性)
    degree: Counter = Counter()
    for (a, b), n in pair_counter.items():
        degree[a] += 1
        degree[b] += 1

    pairs_out = [
        {"drug_a": a, "drug_b": b, "n_groups": n,
         "pct_of_groups": round(n / n_groups * 100, 2)}
        for (a, b), n in pair_counter.most_common(top_pairs)
    ]

    drugs_out = [
        {
            "drug": d,
            "n_groups": n,
            "pct_of_groups": round(n / n_groups * 100, 2),
            "n_comentioned_partners": degree.get(d, 0),
        }
        for d, n in drug_counter.most_common(top_drugs)
    ]

    return {
        "n_groups": n_groups,
        "n_unique_drugs": len(drug_counter),
        "n_groups_multi_drug": n_multi,
        "pct_multi_drug_groups": round(n_multi / n_groups * 100, 2),
        "mean_drugs_per_group": round(sum(k * v for k, v in group_sizes.items()) / n_groups, 2),
        "group_size_distribution": [
            {"n_drugs": k, "n_groups": v, "pct": round(v / n_groups * 100, 2)}
            for k, v in sorted(group_sizes.items())
        ],
        "top_drugs": drugs_out,
        "top_pairs": pairs_out,
        "n_unique_pairs": len(pair_counter),
        "interpretation_note": (
            "共现频次描述的是处方层面的药品共存模式,不表示药物相互作用,"
            "也不构成联合用药合理性或安全性评价。"
        ),
        "limitations": [
            "受处方记录完整性与拼接准确性影响,同一患者跨机构用药可能不完整。",
            "共现频次高可能仅反映适应症聚集或临床路径习惯,需结合病种分层解释。",
            "本分析不评估相互作用、不评价配伍合理性、不建议用药方案。",
        ],
    }


def coprescribing_to_dataframes(analysis: dict[str, Any]) -> dict[str, Any]:
    import pandas as pd

    dfs = {}
    if analysis.get("top_pairs"):
        dfs["coprescribing_pairs"] = pd.DataFrame(analysis["top_pairs"])
    if analysis.get("top_drugs"):
        dfs["coprescribing_drugs"] = pd.DataFrame(analysis["top_drugs"])
    if analysis.get("group_size_distribution"):
        dfs["coprescribing_group_sizes"] = pd.DataFrame(analysis["group_size_distribution"])
    return dfs
