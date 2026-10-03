"""Markdown 报告生成:方法学、结果与局限。

写作原则
--------
1. **所有数字来自实际运行**。报告中的统计量由分析函数实时计算并格式化,
   不存在硬编码的示例数值。
2. **局限与结果同等显著**。数据来源偏差、口径限制、非因果性说明随结果
   一并输出,不放在末尾小字里。
3. **标注渲染后端与数据来源**,使读者能判断图表可信度与合成数据边界。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import charts


def _fmt_num(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "_(无数据)_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _rel(path: str | Path, base: Path) -> str:
    """把绝对路径转成相对报告目录的链接,使 Markdown 可移植。"""
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


# ---------------------------------------------------------------------------
# 政策部分
# ---------------------------------------------------------------------------

def render_policy_section(a: dict[str, Any], *, base_dir: Path,
                          chart_files: dict[str, str]) -> str:
    st = a["stats"]
    L: list[str] = []
    L.append("## 一、中医药政策库与变迁分析\n")
    L.append(
        f"本工具从**中国政府网政策文件库**(`www.gov.cn`,公开检索接口)采集"
        f"标题命中中医药相关关键词的政策文件,共收录 **{st['n_docs']}** 份,"
        f"时间跨度 **{st['first_date']} 至 {st['last_date']}**"
        f"(涉及 {st['year_span']} 个年度),"
        f"涉及发文机关 **{st['n_orgs']}** 个,"
        f"归纳政策主题 **{st['n_themes']}** 类。\n"
    )
    L.append(
        f"其中 {st['n_with_body']} 份已抓取正文全文(占 "
        f"{st.get('pct_with_body', 0)}%,合计 {st.get('total_body_chars', 0):,} 字符),"
        f"{st['n_with_pcode']} 份带有发文字号,"
        f"{st['n_with_org']} 份带有发文机关信息,"
        f"联合发文 {st.get('n_joint', 0)} 份(占 {st.get('pct_joint', 0)}%)。\n"
    )
    # 正文抓取状态必须显式披露,避免读者误判语料完整度
    _ok, _fail, _pend = (st.get("n_body_ok", 0), st.get("n_body_failed", 0),
                         st.get("n_body_pending", 0))
    L.append(
        f"**正文抓取状态**:成功 {_ok} 篇,失败 {_fail} 篇,"
        f"待抓取 {_pend} 篇。\n"
    )
    if _fail:
        L.append(
            f"\n> ⚠ 有 {_fail} 篇正文抓取失败。失败记录已在数据库中标记"
            f"`body_status='failed'` 并保存 `body_error` 原因,"
            f"**未被静默写为空正文** —— 否则将无法区分「抓取失败」与"
            f"「该文件确实没有正文」。失败条目不影响其元数据参与"
            f"时间线与机关统计,但会削弱全文检索与主题词频分析的覆盖度。\n"
        )
    if _pend:
        L.append(
            f"\n> ⚠ 仍有 {_pend} 篇待抓取(可能因运行时长上限或中断)。"
            f"重新执行 `crawl` 会自动续抓未成功项,不会重复抓取已成功页面。\n"
        )
    L.append(f"全文检索:SQLite FTS5,分词器 `trigram`"
             f"({'可用' if st['fts5_trigram'] else '不可用,已降级为 LIKE 检索'})。"
             f"注意 trigram 无法索引长度 < 3 的查询串,"
             f"因此 2 字关键词(如「中医」「中药」)自动改用 `LIKE` 回退检索。\n")

    # 时间线
    L.append("### 1.1 政策发布年度分布\n")
    if "timeline_year" in chart_files:
        L.append(f"![政策年度分布]({chart_files['timeline_year']})\n")
    tl = a.get("timeline_year") or []
    rows = [(d["year"], d["n"], f"{d['yoy_pct']:+.1f}%" if d["yoy_pct"] is not None else "—")
            for d in tl]
    L.append(_table(["年份", "文件数", "同比变化"], rows))

    comp = a.get("completeness") or {}
    if comp.get("sparse_years"):
        L.append(
            f"\n> **收录完整度提示**:以下年份收录文件数少于 3 份 —— "
            f"{'、'.join(str(y) for y in comp['sparse_years'])}。"
            f"政策文件库的电子化回溯覆盖不均,早期年份计数偏低,"
            f"**不能据此推断当年政策关注度低**。\n"
        )

    # 发文机关
    L.append("### 1.2 发文机关分布\n")
    if "by_agency" in chart_files:
        L.append(f"![发文机关分布]({chart_files['by_agency']})\n")
    rows = [(d["puborg"], d["n"], f"{d['share_pct']}%",
             f"{d['first_year']}–{d['last_year']}",
             f"{d['n_sole']}/{d['n_joint']}")
            for d in (a.get("by_agency") or [])[:15]]
    L.append(_table(["发文机关(主要)", "牵头文件数", "占比", "年份范围", "单独/联合"], rows))
    L.append(
        "\n> **口径**:机关名称已在采集阶段归一化"
        "(gov.cn 的 puborg 为自由文本,同一机构存在「中医药局 / 国家中医药局 /"
        "国家中医药管理局 / 国家中医药管理局医政司」等大量变体,"
        "且联合发文用普通空格与 `\\u2002`(EN SPACE)等不可见字符混合分隔)。"
        "归一化后 `puborg` 存**主要发文机关**(联合发文的第一个),"
        "故各机关计数之和等于文件总数;"
        "原始字符串保留在 `puborg_raw` 字段,保证数字可回溯核验。\n"
    )

    # 参与口径(拆分联合发文)
    part = a.get("by_agency_participation") or []
    if part:
        L.append("\n**按「参与发文」口径的机关分布**(含联合发文中的非牵头机关)\n")
        L.append(_table(
            ["发文机关", "参与文件数", "占机关提及次数", "占文件数"],
            [(d["puborg"], d["n_docs_participated"],
              f"{d['share_of_mentions_pct']}%", f"{d['share_of_docs_pct']}%")
             for d in part[:15]],
        ))
        L.append(
            "\n> 上表按 `puborg_all`(全部参与机关)拆分统计,**分母是机关提及次数**,"
            "与按文件数统计的口径不同,两者不可混用。"
            "该口径可揭示在联合发文中排名靠后的机关的实际参与度 ——"
            "例如国家中医药管理局常出现在多部委联合发文的中间位置,"
            "仅按牵头机关统计会显著低估其参与度。\n"
        )

    # 主题
    L.append("### 1.3 政策主题构成\n")
    if "by_theme" in chart_files:
        L.append(f"![主题构成]({chart_files['by_theme']})\n")
    rows = [(d["theme"], d["n"], f"{d['share_pct']}%",
             f"{d['first_year']}–{d['last_year']}")
            for d in (a.get("by_theme") or [])]
    L.append(_table(["政策主题", "文件数", "占比", "年份范围"], rows))
    L.append(
        "\n> 主题由**基于规则的关键词分类器**(`policy_analysis.THEME_RULES`)"
        "按标题优先标注,一篇文件归入唯一主题。该分类是可复核的确定性规则,"
        "而非模型推断;规则边界会影响主题占比,解读时需注意。\n"
    )

    # 主题演变
    ke = a.get("keyword_evolution") or {}
    if ke.get("coverage"):
        L.append("### 1.4 政策主题演变(关键词文档覆盖率)\n")
        if "keyword_evolution" in chart_files:
            L.append(f"![主题演变]({chart_files['keyword_evolution']})\n")
        wins = ke["windows"]
        rows = []
        for kw in ke["keywords"]:
            vals = ke["coverage"].get(kw, [])
            trend = _trend(vals)
            rows.append([kw] + [f"{v}%" for v in vals] + [trend])
        L.append(_table(["关键词"] + wins + ["趋势"], rows))
        L.append(
            f"\n> 口径:{ke.get('metric','')}。"
            "使用**文档覆盖率**而非原始词频,因为原始词频受文件长度影响,"
            "长文件天然词多,覆盖率才是跨时期可比的热度指标。\n"
            "> 趋势按首末窗口覆盖率差值判定(±5 个百分点阈值,"
            "低于阈值判为「平稳」)。\n"
        )

    # 阶段
    eras = a.get("eras") or []
    if eras:
        L.append("### 1.5 政策阶段划分与主导主题\n")
        rows = []
        for e in eras:
            top = e["top_themes"][0] if e["top_themes"] else None
            rows.append([
                e["era"], e["n_docs"],
                f"{top['theme']}({top['n']})" if top else "—",
                e["top_orgs"][0]["puborg"] if e["top_orgs"] else "—",
                e["date_from"] or "—",
            ])
        L.append(_table(["阶段", "文件数", "主导主题", "主要发文机关", "起始日期"], rows))
        L.append(
            "\n> 阶段断点依据中医药治理的关键制度节点(2009 新医改、"
            "2016《中医药法》、2019 传承创新意见、2021「十四五」规划)人工设定,"
            "**不是数据驱动的自动分段**。\n"
        )

    # 断点
    shifts = a.get("shifts") or []
    if shifts:
        L.append("### 1.6 发布量结构性跃变年份\n")
        rows = [(d["year"], d["n"], d["z_robust"], d["direction"],
                 d["median"]) for d in shifts]
        L.append(_table(["年份", "文件数", "稳健 z 值", "方向", "年度中位数"], rows))
        L.append(
            "\n> 方法:对年度计数序列做**稳健标准化**(中位数 / MAD),"
            "|z| ≥ 1.5 者标记。采用 MAD 而非标准差,"
            "因为标准差会被离群年份自身抬高,从而掩盖断点。\n"
            "> 这是**描述性**提示而非因果推断:某年文件激增既可能源于政策密集出台,"
            "也可能源于文件库补录,需逐条核验。\n"
        )

    # 里程碑
    ms = a.get("milestones") or []
    if ms:
        L.append("### 1.7 里程碑政策核验\n")
        rows = []
        for m in ms:
            rows.append([
                m["year"],
                "✔" if m["found"] else "未收录",
                (m["title"] or m["marker"])[:60],
                m["pcode"] or "—",
                m["puborg"] or "—",
            ])
        L.append(_table(["年份", "语料中", "文件标题", "发文字号", "发文机关"], rows))
        missing = [m for m in ms if not m["found"]]
        if missing:
            L.append(
                f"\n> **诚实披露**:下列预设里程碑在本语料中**未检索到** —— "
                f"{'、'.join(str(m['year']) + ' ' + m['marker'] for m in missing)}。"
                f"这些文件可能因标题关键词不匹配或未被政策文件库收录而缺席。"
                f"本工具**不补造**缺失文件,空缺即为语料覆盖的真实边界。\n"
            )
    return "\n".join(L) + "\n"


def _trend(vals: Sequence[float]) -> str:
    if len(vals) < 2:
        return "数据不足"
    d = vals[-1] - vals[0]
    if d >= 5:
        return "↑ 上升"
    if d <= -5:
        return "↓ 下降"
    return "→ 平稳"


# ---------------------------------------------------------------------------
# RWS 部分
# ---------------------------------------------------------------------------

def render_rws_section(results: dict[str, Any], *, chart_files: dict[str, str]) -> str:
    L: list[str] = []
    L.append("## 二、中医药真实世界研究(RWS)分析\n")
    L.append(
        "> **数据声明**:本部分的分析对象是使用者提供的 CSV。"
        "仓库 `examples/` 下的样例数据为**人工构造的合成数据**,"
        "已在文件头部显著标注,不对应任何真实患者。"
        "本节所有数字来自合成样例,其**唯一用途是验证代码路径与统计口径**,"
        "**不具备任何临床或流行病学含义**。\n"
    )

    adr = results.get("adr")
    if adr and adr.get("n_reports"):
        L.append("### 2.1 中药不良反应(ADR)报告特征\n")
        L.append(f"分析报表数 **{adr['n_reports']}** 份。{adr['denominator_note']}\n")
        if "adr_sex" in chart_files:
            L.append(f"![ADR 性别分布]({chart_files['adr_sex']})\n")
        if "adr_age" in chart_files:
            L.append(f"![ADR 年龄段分布]({chart_files['adr_age']})\n")
        L.append("**性别分布**\n")
        L.append(_table(["性别", "报表数", "占比"],
                        [(d["category"], d["n"], f"{d['pct']}%") for d in adr["sex"]]))
        L.append("**年龄段分布**\n")
        L.append(_table(["年龄段", "报表数", "占比"],
                        [(d["category"], d["n"], f"{d['pct']}%") for d in adr["age_group"]]))
        if adr.get("age_stats"):
            s = adr["age_stats"]
            L.append(f"年龄(有记录者 n={s['n_with_age']}):"
                     f"均值 {s['mean']} 岁,中位 {s['median']} 岁,"
                     f"范围 {s['min']}–{s['max']} 岁,"
                     f"≥60 岁占 **{s['pct_60_plus']}%**。\n")
        L.append("**给药途径分布**\n")
        L.append(_table(["给药途径", "报表数", "占比"],
                        [(d["category"], d["n"], f"{d['pct']}%") for d in adr["route"]]))
        if "adr_route" in chart_files:
            L.append(f"![ADR 给药途径]({chart_files['adr_route']})\n")
        L.append("**关联性评价分布**\n")
        L.append(_table(["关联性评价", "报表数", "占比"],
                        [(d["category"], d["n"], f"{d['pct']}%") for d in adr["causality"]]))
        L.append("**转归分布**\n")
        L.append(_table(["转归", "报表数", "占比"],
                        [(d["category"], d["n"], f"{d['pct']}%") for d in adr["outcome"]]))
        L.append("**涉及品种(按品种提及次数)**\n")
        L.append(_table(["品种", "提及次数", "占比"],
                        [(d["category"], d["n"], f"{d['pct']}%") for d in adr["top_drugs"][:8]]))
        L.append(f"\n品种提及总次数 {adr['n_drug_mentions']};"
                 f"涉及多品种的报表 {adr['n_reports_multi_drug']} 份"
                 f"({adr['pct_multi_drug']}%)。\n")
        chi = adr.get("chi2_route_vs_causality") or {}
        if chi.get("df", 0) > 0:
            L.append("**给药途径 × 关联性评价 卡方检验**\n")
            L.append(_table(
                ["统计量", "值"],
                [["χ²", chi["chi2"]], ["自由度", chi["df"]],
                 ["p 值", chi["p"]],
                 ["Cramér's V", chi.get("cramers_v", "—")],
                 ["最小期望频数", chi.get("min_expected")],
                 ["期望频数<5 的格子占比", f"{chi.get('pct_cells_expected_lt5')}%"],
                 ["α=0.05 下显著", "是" if chi.get("significant_at_0.05") else "否"]],
            ))
            L.append(
                "\n> 检验方法:Pearson 卡方独立性检验。p 值由卡方分布上尾概率"
                "计算(scipy 可用时用 scipy,否则用正则化不完全伽马函数级数展开,"
                "精度足以支撑显著性判定)。\n"
                "> **同时报告 Cramér's V 的原因**:p 值随样本量增大而系统性变小,"
                "大样本下极弱关联也会「显著」。V 提供 0–1 的关联强度度量,"
                "避免仅凭 p 值下结论。\n"
            )
            if chi.get("warning"):
                L.append(f"> ⚠ {chi['warning']}\n")
        L.append("**ADR 分析的固有局限**\n")
        for lim in adr.get("limitations", []):
            L.append(f"- {lim}")
        L.append("")

    comp = results.get("compliance")
    if comp and comp.get("n_patients"):
        s = comp["summary"]
        L.append("### 2.2 中药用药依从性(PDC / MPR / 持续性)\n")
        L.append(f"分析患者数 **{comp['n_patients']}**。"
                 f"观察窗 **{comp['window_start']} 至 {comp['window_end']}**"
                 f"({comp['window_days']} 天)。\n")
        L.append(_table(
            ["指标", "值"],
            [["平均 PDC", s["mean_pdc"]],
             ["中位 PDC", s["median_pdc"]],
             ["平均 MPR(截尾)", s["mean_mpr"]],
             [f"PDC ≥ {comp['pdc_threshold']} 患者数",
              f"{s['n_pdc_ge_80']} ({s['pct_pdc_ge_80']}%)"],
             ["平均持续性(天)", s["mean_persistence_days"]],
             ["平均取药次数", s["mean_fills"]],
             ["存在用药间隙的患者数", s["n_with_gap"]]],
        ))
        if "compliance_pdc" in chart_files:
            L.append(f"![PDC 分布]({chart_files['compliance_pdc']})\n")
        L.append(
            f"\n> **指标定义**:PDC = 观察窗内被药品覆盖的**不重复天数** / 窗口天数;"
            f"MPR = 窗口内供药总天数 / 窗口天数(截尾至 1)。"
            f"两者差异仅在重叠处方处理:PDC 对同日多张处方只计一次,"
            f"故 PDC ≤ 1 且更稳健,国际研究通常以 PDC 为首选指标。\n"
        )
        if comp.get("window_derived"):
            L.append(
                f"> ⚠ **偏倚提示**:观察窗由数据推导得出(取首末取药日),"
                f"而非独立设定。这会**系统性高估**依从性 —— "
                f"患者停药后的空白期被排除在窗口之外。"
                f"若使用真实数据,应依据研究设计预先指定固定的观察窗。\n"
            )
        if comp.get("n_records_excluded_bad_supply"):
            L.append(f"> 已排除 {comp['n_records_excluded_bad_supply']} 条"
                     f"`days_supply` 缺失/非正数的记录(未默认按 1 天计入,"
                     f"否则会系统性低估依从性),"
                     f"以及 {comp['n_records_excluded_bad_date']} 条日期无法解析的记录。\n")
        L.append("**依从性分析的固有局限**\n")
        for lim in comp.get("limitations", []):
            L.append(f"- {lim}")
        L.append("")

    cop = results.get("coprescribing")
    if cop and cop.get("n_groups"):
        L.append("### 2.3 中成药联合用药(合并用药)模式\n")
        L.append(
            f"分析用药记录组 **{cop['n_groups']}** 组,"
            f"涉及品种 **{cop['n_unique_drugs']}** 个,"
            f"平均每组 **{cop['mean_drugs_per_group']}** 个品种。"
            f"多药联用组 {cop['n_groups_multi_drug']} 组"
            f"(**{cop['pct_multi_drug_groups']}%**)。\n"
        )
        L.append("**高频联用组合**\n")
        L.append(_table(["药品 A", "药品 B", "共现组数", "占组比例"],
                        [(d["drug_a"], d["drug_b"], d["n_groups"], f"{d['pct_of_groups']}%")
                         for d in cop["top_pairs"][:10]]))
        if "coprescribing_pairs" in chart_files:
            L.append(f"![高频联用组合]({chart_files['coprescribing_pairs']})\n")
        L.append("**高共现品种(按共现伙伴数)**\n")
        L.append(_table(["品种", "出现组数", "占组比例", "共现伙伴数"],
                        [(d["drug"], d["n_groups"], f"{d['pct_of_groups']}%",
                          d["n_comentioned_partners"]) for d in cop["top_drugs"][:10]]))
        L.append(f"\n> **关键限定**:{cop['interpretation_note']}\n")
        L.append("**联合用药分析的固有局限**\n")
        for lim in cop.get("limitations", []):
            L.append(f"- {lim}")
        L.append("")

    drg = results.get("drg")
    if drg and drg.get("summary", {}).get("n_with_payment"):
        s = drg["summary"]
        L.append("### 2.4 DRG/医保付费下中医药服务的成本结构\n")
        L.append(
            f"分析病组 **{s['n_drg_groups']}** 个,其中 "
            f"{s['n_with_payment']} 个具备完整权重/费率。"
            f"合计实际成本 {_fmt_num(s['total_cost'])} 元,"
            f"对应支付标准 {_fmt_num(s['total_payment'])} 元,"
            f"差额 **{_fmt_num(s['total_margin'])} 元**"
            f"({_fmt_num(s.get('total_margin_pct'))}%)。"
            f"超支病组 {s['n_overspent_groups']} 个"
            f"(**{s['pct_overspent_groups']}%**)。"
            f"中医类项目成本合计 {_fmt_num(s['total_tcm_cost'])} 元,"
            f"占总成本 **{s['overall_tcm_cost_share_pct']}%**。\n"
        )
        if "drg_margin" in chart_files:
            L.append(f"![DRG 盈亏]({chart_files['drg_margin']})\n")
        L.append("**分病组成本结构(按中医成本降序前 10)**\n")
        L.append(_table(
            ["病组", "权重", "支付标准", "实际成本", "中医成本占比", "盈亏", "状态"],
            [[c["drg_code"], c["weight"], _fmt_num(c["payment_standard"], 0),
              _fmt_num(c["total_cost"], 0), f"{c['tcm_cost_share_pct']}%",
              _fmt_num(c["margin"], 0), c["status"]]
             for c in drg["cases"][:10]],
        ))
        corr = drg.get("correlation_tcm_share_vs_margin") or {}
        if corr.get("r") is not None:
            L.append("\n**中医成本占比 与 盈亏率 的关联(描述性)**\n")
            L.append(_table(
                ["统计量", "值"],
                [["Pearson r", corr["r"]], ["r²", corr.get("r_squared")],
                 ["p 值(近似)", corr.get("p")], ["n", corr["n"]]],
            ))
            L.append(f"\n> {corr.get('interpretation','')}\n")
            L.append(f"> p 值计算方法:{corr.get('method','')}。\n")
        L.append("\n**盈亏平衡费率(成本等于支付时的费率)**\n")
        L.append(_table(["病组", "当前费率", "盈亏平衡费率", "费率缺口"],
                        [[d["drg_code"], _fmt_num(d["current_rate"], 0),
                          _fmt_num(d["breakeven_rate"], 0), _fmt_num(d["rate_gap"], 0)]
                         for d in drg["breakeven_rates"][:10]]))
        L.append("\n**DRG 分析的固有局限**\n")
        for lim in drg.get("limitations", []):
            L.append(f"- {lim}")
        L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 完整报告
# ---------------------------------------------------------------------------

def build_report(
    policy_analysis: dict[str, Any] | None,
    rws_results: dict[str, Any],
    *,
    out_dir: str | Path,
    chart_files: dict[str, str],
    title: str = "中医药政策与真实世界研究分析报告",
    extra_notes: Sequence[str] = (),
) -> Path:
    """生成完整 Markdown 报告,写入 ``out_dir``,返回文件路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    L: list[str] = []
    L.append(f"# {title}\n")
    L.append(f"生成时间:{now}\n")
    L.append(
        "> 本报告由 **中医药政策与真实世界研究工具 (TCMPolicyRWS)** 自动生成。"
        "报告中的统计量均由程序在本次运行中实时计算,"
        "政策数据来自中国政府网政策文件库的公开检索结果;"
        "RWS 数据来自使用者提供的 CSV(仓库样例为合成数据)。\n"
    )
    L.append("## 摘要\n")
    if policy_analysis:
        st = policy_analysis["stats"]
        L.append(
            f"- **政策语料**:{st['n_docs']} 份文件,"
            f"覆盖 {st['first_date']} 至 {st['last_date']},"
            f"发文机关 {st['n_orgs']} 个,主题 {st['n_themes']} 类。"
        )
    if rws_results.get("adr", {}).get("n_reports"):
        L.append(f"- **ADR 分析**:{rws_results['adr']['n_reports']} 份报表。")
    if rws_results.get("compliance", {}).get("n_patients"):
        c = rws_results["compliance"]["summary"]
        L.append(f"- **依从性分析**:{rws_results['compliance']['n_patients']} 名患者,"
                 f"平均 PDC {c['mean_pdc']}。")
    if rws_results.get("coprescribing", {}).get("n_groups"):
        cp = rws_results["coprescribing"]
        L.append(f"- **联合用药分析**:{cp['n_groups']} 组,"
                 f"多药联用率 {cp['pct_multi_drug_groups']}%。")
    if rws_results.get("drg", {}).get("summary", {}).get("n_with_payment"):
        d = rws_results["drg"]["summary"]
        L.append(f"- **DRG 分析**:{d['n_drg_groups']} 个病组,"
                 f"超支病组占比 {d['pct_overspent_groups']}%。")
    L.append("")

    if policy_analysis:
        L.append(render_policy_section(policy_analysis, base_dir=out_dir,
                                       chart_files=chart_files))
    if any(rws_results.get(k) for k in ("adr", "compliance", "coprescribing", "drg")):
        L.append(render_rws_section(rws_results, chart_files=chart_files))

    # 数据与方法
    L.append("## 三、数据来源与方法\n")
    L.append("### 3.1 政策数据\n")
    L.append(
        "- **来源**:中国政府网政策文件库公开检索接口 "
        "`sousuo.www.gov.cn/search-gov/data`,"
        "检索范围覆盖国务院文件(`zhengcelibrary_gw`)与部门文件"
        "(`zhengcelibrary_bm`)。\n"
        "- **检索策略**:按**标题**匹配中医药相关关键词(中医药、中药、中医、"
        "经典名方、中药饮片、中西医结合、中成药、民族药等)。"
        "限定标题匹配可显著降低噪音 —— 全文匹配常因附件表格偶现「中药」"
        "二字而引入大量非政策域文件。\n"
        "- **存储**:SQLite,正文与元数据分表;"
        "FTS5 `trigram` 分词器建全文索引。\n"
        "- **未访问的站点**:`nmpa.gov.cn`、`cde.org.cn`、`nhc.gov.cn` "
        "部署了 JavaScript 反爬(返回 412/202)。本工具**不尝试绕过**此类"
        "访问控制,因此 NMPA/CDE/NHC 的原始文件未纳入语料。\n"
    )
    L.append("### 3.2 RWS 数据\n")
    L.append(
        "- 分析对象为使用者提供的 CSV,表结构见 README。\n"
        "- 仓库内 `examples/` 样例为**合成数据**,文件头部均带显著声明,"
        "仅用于验证代码路径,**不可作为临床或流行病学证据**。\n"
        "- 本工具不采集、不推断真实患者数据;"
        "运行时无需任何真实患者数据即可完整跑通演示流程。\n"
    )
    L.append("### 3.3 图表渲染\n")
    L.append(
        f"- 本次运行的渲染后端:**{charts.backend_name()}**。\n"
        "- 中文字体处理:matplotlib 默认字体不含 CJK 字形,"
        "中文标签会渲染为方块。本工具按优先级探测系统中文字体"
        "(微软雅黑 / 黑体 / 等线 / 宋体),并**实际渲染中文字符验证无缺字**"
        "后才采用;全部不可用时自动回退为**手写 SVG**"
        "(SVG 由浏览器渲染文本,不存在缺字问题)。\n"
    )
    L.append("\n## 四、总体局限与免责声明\n")
    L.append(
        "1. **政策语料不等于政策全集**。语料来自单一公开文件库的标题检索,"
        "未覆盖地方性文件、未被收录的文件,以及标题不含关键词的文件。"
        "文件库的电子化回溯覆盖不均,早期年份计数系统性偏低。\n"
        "2. **年度计数差异不等于政策强度差异**。本工具的断点检测是描述性的,"
        "不构成因果推断。\n"
        "3. **主题归类依赖规则**。`THEME_RULES` 的边界直接决定主题占比,"
        "规则是公开可复核的,但不同规则会得出不同分布。\n"
        "4. **RWS 结论受数据来源限制**。自发呈报数据存在漏报;"
        "处方数据无法观测实际服药行为;两者都不能计算真实发生率。\n"
        "5. **合成数据不具备实证含义**。仓库样例数据的任何「结果」"
        "只反映代码逻辑,不得引用为研究发现。\n"
        "6. **本工具不提供临床建议、不作为医保结算依据、不构成法律意见**。"
        "支付标准、用药决策须以官方文件与有资质人员的判断为准。\n"
        "7. **本文档不构成对任何政策条文的解释**。政策原文以发布机关"
        "正式文本为准。\n"
    )
    if extra_notes:
        L.append("\n### 附加说明\n")
        for n in extra_notes:
            L.append(f"- {n}\n")

    path.write_text("\n".join(L), encoding="utf-8")
    return path
