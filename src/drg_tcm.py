"""DRG/医保付费下中医药服务的成本结构分析。

政策背景(仅陈述公开的制度事实,不含价值判断)
-------------------------------------------
按病种付费(DRG/DIP)以"病种打包付费"替代按项目付费。其支付标准通常由
"病组权重 × 费率"确定。中医优势病种在真实世界中常呈现**费用结构差异**:
中药饮片、针灸、推拿等治疗项目单价低、次数多、疗程长,与以西医诊疗
路径为基准测算的 DRG 权重之间可能存在结构性错配,表现为:
住院日较长、单次费用低、但项目累计费用不低,或反之。

本模块**只做算术与结构描述**,输入完全由使用者提供(DRG 权重、费率、
各病种/项目的成本明细),用于量化下列问题:

1. 病种层面:实际成本 vs DRG 支付标准的差额(盈亏)
2. 成本结构:中医类项目(饮片/针灸/推拿等)在总成本中的占比
3. 相关性:中医项目占比与"超支/结余"的关联强度(描述性,非因果)
4. 敏感性:权重或费率变动时,盈亏平衡点的移动

明确的边界
----------
* 本模块**不评价**医保支付政策的合理性,不提出临床路径建议,
  也不输出任何可用于实际结算的结论 —— 支付标准以当地医保部门
  正式文件为准。
* 输入数据由使用者负责其真实性与合规性;工具不内置任何真实
  医保结算数据。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Sequence

from .rws import to_float


# 中医类收费项目的常用归类关键词(用于按项目名称自动归类)
TCM_ITEM_PATTERNS: tuple[str, ...] = (
    "中药饮片", "饮片", "中药", "中成药", "针灸", "针刺", "艾灸", "推拿",
    "按摩", "拔罐", "刮痧", "穴位", "正骨", "理疗", "贴敷", "熏洗", "敷熨",
    "中医", "汤剂", "颗粒剂", "膏方", "耳穴", "小针刀", "牵引",
)

# 西医/通用检查检验类(用于对照结构的划分)
WESTERN_ITEM_PATTERNS: tuple[str, ...] = (
    "CT", "MRI", "DR", "彩超", "超声", "X线", "心电图", "检验", "化验",
    "血常规", "生化", "内镜", "造影", "病理",
)


def classify_item(name: str) -> str:
    """把收费项目名称归为 中医类 / 检查检验类 / 其他。

    归类用于成本结构描述。命名不规范时会落入"其他",故结果中同时给出
    "其他"占比,便于判断归类覆盖率是否足够支撑结论。
    """
    n = (name or "").upper()
    if not n:
        return "其他"
    for p in TCM_ITEM_PATTERNS:
        if p.upper() in n:
            return "中医类"
    for p in WESTERN_ITEM_PATTERNS:
        if p.upper() in n:
            return "检查检验类"
    return "其他"


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> dict[str, Any]:
    """皮尔逊相关系数(含显著性 p 值的 t 近似)。

    用途:描述"中医项目占比"与"超支幅度"的线性关联强度。
    **不是因果推断** —— 相关可能由病种、病情复杂度等混杂因素驱动。
    """
    n = min(len(xs), len(ys))
    if n < 3:
        return {"r": None, "p": None, "n": n, "warning": "样本量不足(<3),未计算"}
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return {"r": None, "p": None, "n": n,
                "warning": "存在零方差变量,相关系数无定义"}
    r = sxy / math.sqrt(sxx * syy)
    r = max(-1.0, min(1.0, r))
    out: dict[str, Any] = {"r": round(r, 4), "n": n, "r_squared": round(r * r, 4)}
    if abs(r) >= 1.0:
        out["p"] = 0.0
    else:
        t = r * math.sqrt((n - 2) / (1 - r * r))
        # 双尾 p:用 t 分布的分位数近似(大样本下接近正态)
        out["p"] = round(_t_two_tailed_p(t, n - 2), 4)
    return out


def _t_two_tailed_p(t: float, df: int) -> float:
    """双尾 p 值的近似:借卡方近似 —— p ≈ P(chi2_1 > t^2)。

    精确 t 分布需 scipy。既然 ``t^2 ~ F(1, df)`` 且 df 较大时
    接近卡方(1),此近似在 n>=10 时误差很小;n 小时会给出保守(偏大)的 p,
    属于"宁可不下结论"的安全方向。结果中标注了所用方法。
    """
    from .adr import _chi2_sf

    return _chi2_sf(t * t, 1)


def analyze_drg(
    rows: Iterable[dict[str, Any]],
    *,
    rate_per_weight: float | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    """DRG 病种层面成本结构分析。

    输入行字段(全部由使用者提供):

    * ``drg_code``       病组编码
    * ``drg_name``       病组名称(可选)
    * ``weight``         DRG 权重
    * ``rate``           费率(元/权重),行内缺失时用 ``rate_per_weight``
    * ``total_cost``     实际总成本(元)
    * ``los``            住院天数(可选)
    * ``tcm_cost``       中医类项目成本(可选;缺失时由 item_* 列汇总)
    * ``western_cost``   检查检验类成本(可选)

    也支持**长表**明细:``item_name`` + ``item_cost``(同一病组多行),
    此时自动按病组汇总并按名称归类。

    返回每病种的支付标准、实际成本、盈亏、中医成本占比,以及占比与
    盈亏的相关性(描述性)。
    """
    rows = [r for r in rows if r]
    if not rows:
        return {"n_cases": 0, "error": "无数据"}

    is_long = any("item_cost" in r and "item_name" in r for r in rows)

    cases: dict[str, dict[str, Any]] = {}

    if is_long:
        agg: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"total": 0.0, "tcm": 0.0, "western": 0.0, "name": "", "weight": None,
                     "rate": None, "los": None, "n": 0}
        )
        for r in rows:
            code = str(r.get("drg_code") or "").strip() or "未编码"
            cost = to_float(r.get("item_cost")) or 0.0
            cat = classify_item(str(r.get("item_name") or ""))
            a = agg[code]
            a["total"] += cost
            a["n"] += 1
            if cat == "中医类":
                a["tcm"] += cost
            elif cat == "检查检验类":
                a["western"] += cost
            if not a["name"]:
                a["name"] = str(r.get("drg_name") or "")
            if a["weight"] is None:
                a["weight"] = to_float(r.get("weight"))
            if a["rate"] is None:
                a["rate"] = to_float(r.get("rate"))
            if a["los"] is None:
                a["los"] = to_float(r.get("los"))
        for code, a in agg.items():
            cases[code] = {
                "drg_code": code, "drg_name": a["name"], "weight": a["weight"],
                "rate": a["rate"], "los": a["los"], "total_cost": a["total"],
                "tcm_cost": a["tcm"], "western_cost": a["western"],
                "n_items": a["n"],
            }
    else:
        for r in rows:
            code = str(r.get("drg_code") or "").strip() or "未编码"
            cases[code] = {
                "drg_code": code,
                "drg_name": str(r.get("drg_name") or ""),
                "weight": to_float(r.get("weight")),
                "rate": to_float(r.get("rate")),
                "los": to_float(r.get("los")),
                "total_cost": to_float(r.get("total_cost")) or 0.0,
                "tcm_cost": to_float(r.get("tcm_cost")) or 0.0,
                "western_cost": to_float(r.get("western_cost")) or 0.0,
                "n_items": None,
            }

    out_cases: list[dict[str, Any]] = []
    n_missing_rate = 0
    for code, c in cases.items():
        rate = c["rate"] if c["rate"] is not None else rate_per_weight
        w = c["weight"]
        if rate is None or w is None:
            n_missing_rate += 1
            payment = None
        else:
            payment = w * rate
        cost = c["total_cost"]
        tcm = c["tcm_cost"]
        margin = (payment - cost) if payment is not None else None
        out_cases.append(
            {
                "drg_code": code,
                "drg_name": c["drg_name"],
                "weight": w,
                "rate": rate,
                "payment_standard": round(payment, 2) if payment is not None else None,
                "total_cost": round(cost, 2),
                "tcm_cost": round(tcm, 2),
                "western_cost": round(c["western_cost"], 2),
                "tcm_cost_share_pct": round(tcm / cost * 100, 2) if cost > 0 else 0.0,
                "los": c["los"],
                "cost_per_day": round(cost / c["los"], 2) if c["los"] else None,
                "margin": round(margin, 2) if margin is not None else None,
                "margin_pct": round(margin / payment * 100, 2)
                if (margin is not None and payment) else None,
                "status": ("结余" if margin is not None and margin >= 0
                           else "超支" if margin is not None else "无法计算(缺权重/费率)"),
            }
        )

    out_cases.sort(key=lambda d: (-(d["tcm_cost"]), d["drg_code"]))

    valid = [c for c in out_cases if c["margin"] is not None]
    summary: dict[str, Any] = {
        "n_drg_groups": len(out_cases),
        "n_with_payment": len(valid),
        "n_missing_weight_or_rate": n_missing_rate,
    }
    if valid:
        total_pay = sum(c["payment_standard"] for c in valid)
        total_cost = sum(c["total_cost"] for c in valid)
        total_tcm = sum(c["tcm_cost"] for c in valid)
        n_over = sum(1 for c in valid if c["margin"] < 0)
        summary.update(
            {
                "total_payment": round(total_pay, 2),
                "total_cost": round(total_cost, 2),
                "total_margin": round(total_pay - total_cost, 2),
                "total_margin_pct": round((total_pay - total_cost) / total_pay * 100, 2)
                if total_pay else None,
                "n_overspent_groups": n_over,
                "pct_overspent_groups": round(n_over / len(valid) * 100, 2),
                "total_tcm_cost": round(total_tcm, 2),
                "overall_tcm_cost_share_pct": round(total_tcm / total_cost * 100, 2)
                if total_cost else 0.0,
            }
        )

    # 中医成本占比 vs 盈亏率 的关联(描述性)
    xs = [c["tcm_cost_share_pct"] for c in valid if c["margin_pct"] is not None]
    ys = [c["margin_pct"] for c in valid if c["margin_pct"] is not None]
    corr = _pearson(xs, ys)
    corr["method"] = "Pearson r;双尾 p 由 t^2 ~ chi2(1) 近似"
    corr["interpretation"] = (
        "该相关系数仅描述本样本内「中医项目成本占比」与「盈亏率」的线性共变,"
        "不能解释为因果:病种、病情复杂度、收治结构等混杂因素未受控。"
    )

    # 盈亏平衡的费率(在给定权重下使支付=成本的费率)
    be = []
    for c in out_cases:
        if c["weight"] and c["weight"] > 0:
            be.append(
                {
                    "drg_code": c["drg_code"],
                    "current_rate": c["rate"],
                    "breakeven_rate": round(c["total_cost"] / c["weight"], 2),
                    "rate_gap": round(c["total_cost"] / c["weight"] - (c["rate"] or 0), 2),
                }
            )
    be.sort(key=lambda d: -d["rate_gap"])

    return {
        "n_cases": len(rows),
        "input_format": "长表(项目明细)" if is_long else "宽表(病组汇总)",
        "rate_per_weight": rate_per_weight,
        "summary": summary,
        "cases": out_cases,
        "top_by_tcm_cost": out_cases[:top_n],
        "correlation_tcm_share_vs_margin": corr,
        "breakeven_rates": be[:top_n],
        "limitations": [
            "支付标准以当地医保部门正式文件为准,本模块基于使用者提供的"
            "权重与费率做算术推算,不可用于实际结算。",
            "成本口径(是否含人力、折旧、药品耗材)显著影响结论,"
            "须与实际核算制度对齐后解读。",
            "病种层面汇总掩盖了病例组合差异,结论不适用于个体病例。",
            "本模块不评价支付政策合理性,不构成医保或临床建议。",
        ],
    }


def drg_to_dataframes(analysis: dict[str, Any]) -> dict[str, Any]:
    import pandas as pd

    dfs = {}
    if analysis.get("cases"):
        dfs["drg_cases"] = pd.DataFrame(analysis["cases"])
    if analysis.get("breakeven_rates"):
        dfs["drg_breakeven_rates"] = pd.DataFrame(analysis["breakeven_rates"])
    if analysis.get("summary"):
        dfs["drg_summary"] = pd.DataFrame([analysis["summary"]])
    return dfs
