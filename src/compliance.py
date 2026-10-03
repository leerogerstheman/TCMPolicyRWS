"""中药用药依从性分析:PDC / MPR / 持续性与停药分析。

指标定义(严格按药物流行病学惯例,避免与"依从性"混用)
----------------------------------------------------
设观察窗为 ``[window_start, window_end]``(含),共 ``D`` 天。

**MPR (Medication Possession Ratio, 药物持有率)**::

    MPR = 观察窗内供药总天数 / D

计算简单,但当患者提前停药时会被**低估**(剩余天数无药可持),
且可能 > 1(重叠取药),故通常截尾到 1。

**PDC (Proportion of Days Covered, 服药天数覆盖率)**::

    PDC = 观察窗内被药品覆盖的**不重复天数** / D

与 MPR 的关键区别:PDC 对同一天的多张处方**只计一次**,故 PDC <= 1
且不会被重叠取药抬高。国际上 PDC 是首选指标,MPR 用于敏感性分析。

**持续性 (persistence)**:从首次供药到**允许的间隙**(grace period)内
最后一次供药的间隔天数。与 PDC 的区别:PDC 度量"覆盖比例",
持续性度量"坚持了多久"。患者可能 PDC 很高但早期停药(短窗),
也可能 PDC 中等但持续整年(间歇用药)。

实现要点
--------
1. 观察窗需**显式给定或推导**。若由数据推导(首末供药日),需注意
   这会系统性高估依从性(把停药后的空白期排除在外)。本模块默认
   由数据推导并在结果中**显式标注**所用窗口,提示偏倚方向。
2. 供药天数按 ``days_supply`` 累加,起始日含当天,故覆盖
   ``[start, start + days_supply - 1]``。
3. 重叠处方按并集合并到"覆盖天数集合",这是 PDC 与 MPR 的唯一差别来源。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .rws import parse_date, to_float, to_int

# 药物持有率 >= 80% 通常被视为"良好依从"的操作性阈值。
# 注意:该阈值源自心血管等慢性病领域的研究惯例,迁移到中药场景
# 缺乏专门验证,本模块仅作分组参考并在报告中标注这一局限。
PDC_THRESHOLD = 0.8


def _covered_days(intervals: Sequence[tuple[date, date]]) -> int:
    """合并重叠/相邻区间后统计覆盖的**不重复**天数。

    区间按起点排序后贪心合并。相邻(前区间结束日 +1 == 后区间开始日)
    也合并,避免因恰好接续而产生额外的"间隙日"计数错误。
    """
    if not intervals:
        return 0
    ivs = sorted(intervals, key=lambda t: t[0])
    total = 0
    cur_s, cur_e = ivs[0]
    for s, e in ivs[1:]:
        if s <= cur_e + timedelta(days=1):
            cur_e = max(cur_e, e)
        else:
            total += (cur_e - cur_s).days + 1
            cur_s, cur_e = s, e
    total += (cur_e - cur_s).days + 1
    return total


def _merge_intervals(intervals: Sequence[tuple[date, date]]) -> list[tuple[date, date]]:
    if not intervals:
        return []
    ivs = sorted(intervals, key=lambda t: t[0])
    out = [ivs[0]]
    for s, e in ivs[1:]:
        if s <= out[-1][1] + timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def patient_pdc_mpr(
    records: Iterable[dict[str, Any]],
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    grace_days: int = 30,
) -> dict[str, Any]:
    """按患者计算 PDC / MPR / 持续性。

    ``records`` 每条为一次供药记录,需含:
    ``patient_id``, ``fill_date``(取药/处方日期), ``days_supply``(供药天数)。

    参数
    ----
    window_start/end : 观察窗。两者皆空时由全部记录的首末供药日推导,
                       并在 ``window_derived`` 中标记 True —— 此时
                       PDC/MPR 会**偏高**,因为窗口恰好贴合用药期。
    grace_days       : 持续性判定的允许间隙。超过该间隙视为中断,
                       持续性终点取中断前最后一次供药结束日。
    """
    by_patient: dict[str, list[tuple[date, int]]] = defaultdict(list)
    n_bad_date = 0
    n_bad_supply = 0

    for r in records:
        pid = str(r.get("patient_id") or "").strip()
        if not pid:
            continue
        d = parse_date(r.get("fill_date"))
        ds = to_int(r.get("days_supply"))
        if d is None:
            n_bad_date += 1
            continue
        if ds is None or ds <= 0:
            # days_supply 缺失时不能默认 1 天(会系统性低估),直接排除并计数
            n_bad_supply += 1
            continue
        by_patient[pid].append((d, ds))

    if not by_patient:
        return {"n_patients": 0, "error": "无有效供药记录"}

    all_dates = [d for recs in by_patient.values() for d, _ in recs]
    derived = window_start is None or window_end is None
    if window_start is None:
        window_start = min(all_dates)
    if window_end is None:
        window_end = max(
            d + timedelta(days=ds - 1) for recs in by_patient.values() for d, ds in recs
        )
    if window_end < window_start:
        return {"n_patients": 0, "error": "观察窗终点早于起点"}

    window_days = (window_end - window_start).days + 1

    patients: list[dict[str, Any]] = []
    for pid, recs in sorted(by_patient.items()):
        intervals: list[tuple[date, date]] = []
        total_supply = 0
        for d, ds in sorted(recs, key=lambda t: t[0]):
            # 供药天数含起始日,故结束日为 d + ds - 1
            end = d + timedelta(days=ds - 1)
            intervals.append((d, end))
            total_supply += ds
        merged = _merge_intervals(intervals)

        # 裁剪到观察窗后再统计覆盖
        clipped = []
        for s, e in merged:
            cs, ce = max(s, window_start), min(e, window_end)
            if cs <= ce:
                clipped.append((cs, ce))
        covered = _covered_days(clipped)

        mpr_raw = total_supply / window_days
        mpr = min(mpr_raw, 1.0)          # 截尾:重叠取药不使 MPR > 1
        pdc = min(covered / window_days, 1.0)

        # 持续性:从首次供药起,容忍 grace_days 间隙所能坚持到的最后覆盖日
        first = merged[0][0]
        last_end = merged[0][1]
        for s, e in merged[1:]:
            if (s - last_end).days - 1 > grace_days:
                break
            last_end = max(last_end, e)
        persistence_days = min((last_end - first).days + 1, window_days)

        # 停药间隙:合并区间之间的空档
        gaps = []
        for (s1, e1), (s2, _) in zip(merged, merged[1:]):
            gap = (s2 - e1).days - 1
            if gap > 0:
                gaps.append({"start": (e1 + timedelta(days=1)).isoformat(),
                             "days": gap})

        patients.append(
            {
                "patient_id": pid,
                "n_fills": len(recs),
                "first_fill": first.isoformat(),
                "last_fill": max(d for d, _ in recs).isoformat(),
                "total_days_supply": total_supply,
                "covered_days": covered,
                "window_days": window_days,
                "pdc": round(pdc, 4),
                "mpr_raw": round(mpr_raw, 4),
                "mpr": round(mpr, 4),
                "persistence_days": persistence_days,
                "n_gaps": len(gaps),
                "max_gap_days": max((g["days"] for g in gaps), default=0),
                "pdc_ge_80": pdc >= PDC_THRESHOLD,
            }
        )

    n = len(patients)
    summary = {
        "n_patients": n,
        "mean_pdc": round(sum(p["pdc"] for p in patients) / n, 4),
        "median_pdc": round(_median([p["pdc"] for p in patients]), 4),
        "mean_mpr": round(sum(p["mpr"] for p in patients) / n, 4),
        "n_pdc_ge_80": sum(1 for p in patients if p["pdc_ge_80"]),
        "pct_pdc_ge_80": round(sum(1 for p in patients if p["pdc_ge_80"]) / n * 100, 2),
        "mean_persistence_days": round(sum(p["persistence_days"] for p in patients) / n, 1),
        "mean_fills": round(sum(p["n_fills"] for p in patients) / n, 2),
        "n_with_gap": sum(1 for p in patients if p["n_gaps"] > 0),
    }

    return {
        "n_patients": n,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_days": window_days,
        "window_derived": derived,
        "grace_days": grace_days,
        "pdc_threshold": PDC_THRESHOLD,
        "n_records_excluded_bad_date": n_bad_date,
        "n_records_excluded_bad_supply": n_bad_supply,
        "summary": summary,
        "patients": patients,
        "limitations": [
            "PDC/MPR 依赖供药记录完整性;实际服药行为无法从处方数据观测"
            "(持有药物不等于按时服药),故为依从性的代理指标。",
            "观察窗由数据推导时(见 window_derived=True)会系统性高估依从性,"
            "因为停药后的空白期被排除在窗口外。",
            "PDC>=80% 的阈值源自慢性病研究惯例,在中药疗程场景缺乏专门验证,"
            "仅作分组参考。",
            "未考虑住院期间用药、院内调剂、自费购药等可能不在处方库中的用药。",
        ],
    }


def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def pdc_by_stratum(
    patient_result: dict[str, Any], strata: dict[str, str]
) -> list[dict[str, Any]]:
    """按分层变量(如年龄段、病种)汇总 PDC。

    ``strata`` 为 ``patient_id -> 分层标签`` 的映射。缺少标签的患者计入"未知",
    以保证分母与总数一致(便于核对)。
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for p in patient_result.get("patients", []):
        label = strata.get(p["patient_id"], "未知")
        buckets[label].append(p["pdc"])
    out = []
    for label, vals in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        n = len(vals)
        out.append(
            {
                "stratum": label,
                "n_patients": n,
                "mean_pdc": round(sum(vals) / n, 4),
                "median_pdc": round(_median(vals), 4),
                "n_pdc_ge_80": sum(1 for v in vals if v >= PDC_THRESHOLD),
                "pct_pdc_ge_80": round(sum(1 for v in vals if v >= PDC_THRESHOLD) / n * 100, 2),
            }
        )
    return out


def compliance_to_dataframes(result: dict[str, Any]) -> dict[str, Any]:
    import pandas as pd

    dfs = {}
    if result.get("patients"):
        dfs["compliance_patients"] = pd.DataFrame(result["patients"])
    if result.get("summary"):
        dfs["compliance_summary"] = pd.DataFrame([result["summary"]])
    return dfs
