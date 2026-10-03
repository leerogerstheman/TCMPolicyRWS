"""命令行入口:采集、分析、导出、出图、生成报告。

子命令
------
crawl       采集 gov.cn 中医药政策文件入库
search      在本地政策库中全文检索
analyze     对已入库政策做变迁分析并导出
rws         运行真实世界研究分析(ADR / 依从性 / 联合用药 / DRG)
demo        一键跑通全流程(政策分析 + RWS 合成样例)
info        显示数据库与运行环境信息

编码处理:Windows 控制台默认可能为 GBK,中文输出会抛 UnicodeEncodeError。
启动时主动把 stdout/stderr 重配置为 UTF-8,失败则在写日志时逐条降级,
保证不因终端编码问题而中断分析。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

# 项目根目录(本文件位于 <root>/src/cli.py)
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "tcm_policy.db"
DEFAULT_OUT = ROOT / "output"
EXAMPLES = ROOT / "examples"


def _setup_encoding() -> None:
    """把标准输出切到 UTF-8;不支持时静默跳过。

    不做任何强制假设:某些重定向环境不支持 reconfigure,
    此时后续 print 由 ``_safe_print`` 兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _safe_print(msg: str = "") -> None:
    """打印,遇到编码错误时降级为可编码字符。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def _header(title: str) -> None:
    _safe_print()
    _safe_print("=" * 68)
    _safe_print(f"  {title}")
    _safe_print("=" * 68)


# ---------------------------------------------------------------------------
# 导出工具
# ---------------------------------------------------------------------------

def export_tables(dfs: dict[str, Any], out_dir: Path, *, xlsx_name: str) -> tuple[list[str], Path | None]:
    """把若干 DataFrame 导出为 CSV(逐个)与单个 XLSX(多 sheet)。

    同时产出两种格式:CSV 便于脚本二次处理,XLSX 便于人工查看与交付。
    返回 ``(csv 文件名列表, xlsx 路径)``。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, df in dfs.items():
        if df is None or getattr(df, "empty", True):
            continue
        p = csv_dir / f"{name}.csv"
        # utf-8-sig 使 Excel 直接双击打开时中文不乱码
        df.to_csv(p, index=False, encoding="utf-8-sig")
        written.append(p.name)

    xlsx_path: Path | None = None
    nonempty = {k: v for k, v in dfs.items() if v is not None and not getattr(v, "empty", True)}
    if nonempty:
        xlsx_path = out_dir / xlsx_name
        try:
            with __import__("pandas").ExcelWriter(xlsx_path, engine="xlsxwriter") as xw:
                for name, df in nonempty.items():
                    # Excel sheet 名上限 31 字符且禁用 : \ / ? * [ ]
                    sheet = name[:31].replace("/", "_").replace("\\", "_")
                    df.to_excel(xw, sheet_name=sheet, index=False)
        except Exception as exc:  # 导出失败不应让整个分析崩掉
            _safe_print(f"  [警告] XLSX 导出失败: {exc}")
            xlsx_path = None
    return written, xlsx_path


# ---------------------------------------------------------------------------
# 各子命令
# ---------------------------------------------------------------------------

def cmd_crawl(args: argparse.Namespace) -> int:
    from . import policy_crawler as pc

    _header("采集:中国政府网政策文件库(中医药相关)")
    kw = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords \
        else list(pc.DEFAULT_KEYWORDS)
    _safe_print(f"关键词({len(kw)} 个): {'、'.join(kw)}")
    _safe_print(f"来源: {', '.join(pc.SOURCES[s] for s in args.sources)}")
    _safe_print(f"数据库: {args.db}")
    _safe_print(f"正文抓取: {'开启' if not args.no_body else '关闭'}"
                f"{f',上限 {args.max_docs} 篇' if args.max_docs else ''}")
    _safe_print()

    stats = pc.crawl(
        args.db,
        keywords=kw,
        sources=args.sources,
        max_pages=args.max_pages,
        page_size=args.page_size,
        with_body=not args.no_body,
        sleep=args.sleep,
        max_docs=args.max_docs,
    )
    _header("采集完成")
    _safe_print(f"  检索请求数      : {stats.queries_run}")
    _safe_print(f"  命中条目(含重复): {stats.items_seen}")
    _safe_print(f"  新增入库        : {stats.items_new}")
    _safe_print(f"  正文抓取成功    : {stats.bodies_fetched}")
    _safe_print(f"  正文抓取失败    : {stats.bodies_failed}")
    if stats.errors:
        _safe_print(f"  错误(前 5 条):")
        for e in stats.errors[:5]:
            _safe_print(f"    - {e}")

    from . import db
    conn = db.connect(args.db)
    try:
        st = db.stats(conn)
        _safe_print()
        _safe_print(f"  库内中医药文件总数: {st['n_docs']}")
        _safe_print(f"  时间跨度          : {st['first_date']} ~ {st['last_date']}")
        _safe_print(f"  发文机关数        : {st['n_orgs']}")
    finally:
        conn.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from . import db

    _header(f"全文检索: {args.term}")
    conn = db.connect(args.db)
    try:
        rows, fallback = db.search(conn, args.term, limit=args.limit,
                                   year_from=args.year_from, year_to=args.year_to)
        if fallback:
            _safe_print("[检索方式] LIKE 回退(查询串 < 3 字符或 FTS 不可用)")
            _safe_print("           注:FTS5 trigram 无法为 < 3 字符的查询生成三元组,")
            _safe_print("           2 字中文词(中医/中药)必须走 LIKE。")
        else:
            _safe_print("[检索方式] FTS5 trigram 索引")
        _safe_print(f"[命中] {len(rows)} 条\n")
        for i, r in enumerate(rows, 1):
            _safe_print(f"{i:>3}. {r['pubdate'] or '日期缺失'} | {r['puborg'] or '机关缺失'}")
            _safe_print(f"     {r['title']}")
            if r["pcode"]:
                _safe_print(f"     发文字号: {r['pcode']}")
            if r["url"]:
                _safe_print(f"     {r['url']}")
    finally:
        conn.close()
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from . import db, policy_analysis as pa, report

    _header("政策变迁分析")
    conn = db.connect(args.db)
    try:
        st = db.stats(conn)
        if st["n_docs"] == 0:
            _safe_print("库中无数据,请先运行: python tcm_rws.py crawl")
            return 1
        _safe_print(f"语料:{st['n_docs']} 份,{st['first_date']} ~ {st['last_date']}")
        pa.annotate_themes(conn)
        pa.extract_keywords_bulk(conn)

        analysis = pa.full_analysis(conn)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        chart_dir = out_dir / "charts"

        _safe_print("\n生成图表 ...")
        chart_files = _render_policy_charts(analysis, chart_dir, out_dir)

        _safe_print("导出表格 ...")
        dfs = pa.to_dataframes(analysis)
        names, xlsx = export_tables(dfs, out_dir, xlsx_name="policy_analysis.xlsx")
        _safe_print(f"  CSV: {len(names)} 个 -> {out_dir / 'csv'}")
        if xlsx:
            _safe_print(f"  XLSX: {xlsx}")

        _safe_print("生成报告 ...")
        rp = report.build_report(analysis, {}, out_dir=out_dir, chart_files=chart_files)
        _safe_print(f"  报告: {rp}")

        # 控制台摘要(与报告同源,数字一致)
        _header("关键结果")
        for d in analysis["timeline_year"][-8:]:
            yoy = f"  同比 {d['yoy_pct']:+.1f}%" if d["yoy_pct"] is not None else ""
            _safe_print(f"  {d['year']}: {d['n']:>3} 份{yoy}")
        _safe_print("\n  发文机关 Top5:")
        for d in analysis["by_agency"][:5]:
            _safe_print(f"    {d['puborg'][:30]:<32} {d['n']:>3} 份 ({d['share_pct']}%)")
        _safe_print("\n  政策主题 Top5:")
        for d in analysis["by_theme"][:5]:
            _safe_print(f"    {d['theme']:<24} {d['n']:>3} 份 ({d['share_pct']}%)")
    finally:
        conn.close()
    return 0


def _render_policy_charts(analysis: dict[str, Any], chart_dir: Path,
                          out_dir: Path) -> dict[str, str]:
    """渲染政策分析图表,返回 {键: 相对报告目录的路径}。"""
    from . import charts

    chart_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def _p(path: Path) -> str:
        try:
            return path.resolve().relative_to(out_dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    tl = analysis.get("timeline_year") or []
    if tl:
        p = charts.bar_chart(
            "中医药相关政策文件年度分布", [str(d["year"]) for d in tl],
            [d["n"] for d in tl], chart_dir / "policy_timeline_year.png",
            xlabel="发布年份", ylabel="文件数", rotate=45,
        )
        files["timeline_year"] = _p(p)

    ag = analysis.get("by_agency") or []
    if ag:
        top = ag[:15]
        p = charts.hbar_chart(
            "中医药政策主要发文机关(前 15)", [d["puborg"] for d in top],
            [d["n"] for d in top], chart_dir / "policy_by_agency.png",
        )
        files["by_agency"] = _p(p)

    th = analysis.get("by_theme") or []
    if th:
        p = charts.pie_chart(
            "中医药政策主题构成", [d["theme"] for d in th], [d["n"] for d in th],
            chart_dir / "policy_by_theme.png",
        )
        files["by_theme"] = _p(p)

    ke = analysis.get("keyword_evolution") or {}
    if ke.get("coverage"):
        series = {kw: ke["coverage"][kw] for kw in ke["keywords"][:8]}
        p = charts.line_chart(
            "政策主题关键词覆盖率演变(文档覆盖率 %)",
            ke["windows"], series, chart_dir / "policy_keyword_evolution.png",
            xlabel="年份窗口", ylabel="文档覆盖率 (%)", rotate=0,
        )
        files["keyword_evolution"] = _p(p)

    ty = analysis.get("theme_year") or {}
    if ty.get("matrix") and ty.get("years"):
        # 只取文件数最多的前 8 个主题,否则热力图行数过多不可读
        order = [d["theme"] for d in (analysis.get("by_theme") or [])][:8]
        order = [t for t in order if t in ty["matrix"]]
        if order:
            p = charts.heatmap(
                "政策主题 × 年份 分布热力图", order,
                [str(y) for y in ty["years"]],
                [[ty["matrix"][t].get(y, 0) for y in ty["years"]] for t in order],
                chart_dir / "policy_theme_year_heatmap.png",
                xlabel="年份", ylabel="政策主题",
            )
            files["theme_year"] = _p(p)
    return files


def cmd_rws(args: argparse.Namespace) -> int:
    from . import adr, charts, compliance, drg_tcm, report, rws

    _header("真实世界研究(RWS)分析")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    chart_dir = out_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {}
    chart_files: dict[str, str] = {}
    all_dfs: dict[str, Any] = {}

    def _p(path: Path) -> str:
        try:
            return path.resolve().relative_to(out_dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    # --- ADR ---
    if args.adr:
        _safe_print(f"\n[1/4] ADR 分析: {args.adr}")
        rows = adr.load_adr_csv(args.adr)
        _safe_print(f"      读入 {len(rows)} 条报表")
        a = adr.analyze_adr(rows)
        if a.get("n_reports"):
            results["adr"] = a
            all_dfs.update(adr.adr_to_dataframes(a))
            sex = a["sex"]
            if sex:
                p = charts.bar_chart(
                    "ADR 报表性别分布", [d["category"] for d in sex],
                    [d["n"] for d in sex], chart_dir / "adr_sex.png", ylabel="报表数")
                chart_files["adr_sex"] = _p(p)
            ag = a["age_group"]
            if ag:
                p = charts.bar_chart(
                    "ADR 报表年龄段分布", [d["category"] for d in ag],
                    [d["n"] for d in ag], chart_dir / "adr_age.png", ylabel="报表数")
                chart_files["adr_age"] = _p(p)
            rt = a["route"]
            if rt:
                p = charts.hbar_chart(
                    "ADR 报表给药途径分布", [d["category"] for d in rt],
                    [d["n"] for d in rt], chart_dir / "adr_route.png", xlabel="报表数")
                chart_files["adr_route"] = _p(p)
            _safe_print(f"      报表 {a['n_reports']} 份;"
                        f"≥60 岁占 {a['age_stats']['pct_60_plus'] if a['age_stats'] else '—'}%")
            chi = a["chi2_route_vs_causality"]
            if chi.get("df", 0) > 0:
                _safe_print(f"      给药途径×关联性评价: χ²={chi['chi2']}, "
                            f"df={chi['df']}, p={chi['p']}, V={chi.get('cramers_v')}")

    # --- 依从性 ---
    if args.rx:
        _safe_print(f"\n[2/4] 依从性分析: {args.rx}")
        rows = rws.load_csv_rows(args.rx)
        _safe_print(f"      读入 {len(rows)} 条取药记录")
        c = compliance.patient_pdc_mpr(rows, grace_days=args.grace_days)
        if c.get("n_patients"):
            results["compliance"] = c
            all_dfs.update(compliance.compliance_to_dataframes(c))
            s = c["summary"]
            _safe_print(f"      患者 {c['n_patients']} 名;平均 PDC {s['mean_pdc']};"
                        f"PDC≥{c['pdc_threshold']} 占 {s['pct_pdc_ge_80']}%")
            pdcs = [p["pdc"] for p in c["patients"]]
            bins = [0, 0.2, 0.4, 0.6, 0.8, 1.0001]
            labels = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]
            counts = [0] * 5
            for v in pdcs:
                for i in range(5):
                    if bins[i] <= v < bins[i + 1]:
                        counts[i] += 1
                        break
            p = charts.bar_chart(
                "患者 PDC 分布", labels, counts,
                chart_dir / "compliance_pdc.png", ylabel="患者数", xlabel="PDC 区间")
            chart_files["compliance_pdc"] = _p(p)

    # --- 联合用药 ---
    if args.coprescribing:
        _safe_print(f"\n[3/4] 联合用药分析: {args.coprescribing}")
        rows = rws.load_csv_rows(args.coprescribing)
        _safe_print(f"      读入 {len(rows)} 条记录")
        cp = rws.coprescribing_analysis(rows)
        if cp.get("n_groups"):
            results["coprescribing"] = cp
            all_dfs.update(rws.coprescribing_to_dataframes(cp))
            _safe_print(f"      用药组 {cp['n_groups']};"
                        f"多药联用率 {cp['pct_multi_drug_groups']}%")
            pairs = cp["top_pairs"][:10]
            if pairs:
                p = charts.hbar_chart(
                    "高频中成药联用组合(共现组数)",
                    [f"{d['drug_a'][-4:]} + {d['drug_b'][-4:]}" for d in pairs],
                    [d["n_groups"] for d in pairs],
                    chart_dir / "coprescribing_pairs.png", xlabel="共现组数")
                chart_files["coprescribing_pairs"] = _p(p)

    # --- DRG ---
    if args.drg:
        _safe_print(f"\n[4/4] DRG 分析: {args.drg}")
        rows = rws.load_csv_rows(args.drg)
        _safe_print(f"      读入 {len(rows)} 行")
        d = drg_tcm.analyze_drg(rows, rate_per_weight=args.rate)
        if d.get("summary", {}).get("n_with_payment"):
            results["drg"] = d
            all_dfs.update(drg_tcm.drg_to_dataframes(d))
            s = d["summary"]
            _safe_print(f"      病组 {s['n_drg_groups']} 个;"
                        f"超支占比 {s['pct_overspent_groups']}%;"
                        f"中医成本占比 {s['overall_tcm_cost_share_pct']}%")
            cases = d["cases"][:12]
            if cases:
                p = charts.bar_chart(
                    "DRG 病组盈亏(支付标准 − 实际成本,元)",
                    [c["drg_code"] for c in cases],
                    [c["margin"] or 0 for c in cases],
                    chart_dir / "drg_margin.png",
                    ylabel="盈亏(元)", xlabel="病组", rotate=45)
                chart_files["drg_margin"] = _p(p)

    if not results:
        _safe_print("\n未提供任何 RWS 输入数据。示例:")
        _safe_print("  python tcm_rws.py rws --adr examples/sample_adr_synthetic.csv")
        return 1

    _safe_print("\n导出表格 ...")
    names, xlsx = export_tables(all_dfs, out_dir, xlsx_name="rws_analysis.xlsx")
    _safe_print(f"  CSV: {len(names)} 个 -> {out_dir / 'csv'}")
    if xlsx:
        _safe_print(f"  XLSX: {xlsx}")

    rp = report.build_report(None, results, out_dir=out_dir, chart_files=chart_files,
                             title="中医药真实世界研究(RWS)分析报告")
    _safe_print(f"\n报告: {rp}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """一键跑通:政策分析(真实 gov.cn 数据) + RWS(合成样例)。"""
    _header("演示:政策分析 + RWS 分析")
    args_db = args.db
    from . import db

    conn = db.connect(args_db)
    try:
        n = db.stats(conn)["n_docs"]
    finally:
        conn.close()
    if n == 0:
        _safe_print(f"政策库为空({args_db}),先执行采集 ...")
        rc = cmd_crawl(argparse.Namespace(
            db=args_db, keywords=None, sources=("zhengcelibrary_bm", "zhengcelibrary_gw"),
            max_pages=args.max_pages, page_size=20, no_body=False,
            max_docs=args.max_docs, sleep=args.sleep,
        ))
        if rc != 0:
            return rc
    else:
        _safe_print(f"政策库已有 {n} 份文件,跳过采集。")

    # 政策分析
    cmd_analyze(argparse.Namespace(db=args_db, out=str(DEFAULT_OUT)))

    # RWS(合成样例)
    adr_f = EXAMPLES / "sample_adr_synthetic.csv"
    rx_f = EXAMPLES / "sample_prescriptions_synthetic.csv"
    cop_f = EXAMPLES / "sample_coprescribing_synthetic.csv"
    drg_f = EXAMPLES / "sample_drg_synthetic.csv"
    _safe_print("\n" + "-" * 68)
    _safe_print("  注意:以下 RWS 分析使用 examples/ 下的【合成数据】,")
    _safe_print("  仅用于验证代码路径,不具备任何临床或流行病学含义。")
    _safe_print("-" * 68)
    cmd_rws(argparse.Namespace(
        adr=str(adr_f) if adr_f.exists() else None,
        rx=str(rx_f) if rx_f.exists() else None,
        coprescribing=str(cop_f) if cop_f.exists() else None,
        drg=str(drg_f) if drg_f.exists() else None,
        rate=11000.0, grace_days=30, out=str(DEFAULT_OUT),
    ))

    # 合并成一份总报告
    from . import policy_analysis as pa, report, rws, adr as adr_mod, compliance, drg_tcm

    conn = db.connect(args_db)
    try:
        analysis = pa.full_analysis(conn)
    finally:
        conn.close()
    rws_results: dict[str, Any] = {}
    if adr_f.exists():
        rws_results["adr"] = adr_mod.analyze_adr(adr_mod.load_adr_csv(adr_f))
    if rx_f.exists():
        rws_results["compliance"] = compliance.patient_pdc_mpr(
            rws.load_csv_rows(rx_f), grace_days=30)
    if cop_f.exists():
        rws_results["coprescribing"] = rws.coprescribing_analysis(
            rws.load_csv_rows(cop_f))
    if drg_f.exists():
        rws_results["drg"] = drg_tcm.analyze_drg(
            rws.load_csv_rows(drg_f), rate_per_weight=11000.0)

    chart_dir = DEFAULT_OUT / "charts"
    chart_files = _render_policy_charts(analysis, chart_dir, DEFAULT_OUT)
    # 复用手上已有的 RWS 图(由 cmd_rws 生成)
    for k, fn in (("adr_sex", "adr_sex.png"), ("adr_age", "adr_age.png"),
                  ("adr_route", "adr_route.png"),
                  ("compliance_pdc", "compliance_pdc.png"),
                  ("coprescribing_pairs", "coprescribing_pairs.png"),
                  ("drg_margin", "drg_margin.png")):
        if (chart_dir / fn).exists():
            chart_files[k] = f"charts/{fn}"

    rp = report.build_report(
        analysis, rws_results, out_dir=DEFAULT_OUT, chart_files=chart_files,
        title="中医药政策变迁与真实世界研究分析报告",
        extra_notes=[
            "本报告中政策部分基于中国政府网政策文件库的真实采集数据;",
            "RWS 部分基于 examples/ 下的合成数据,仅供流程演示。",
        ],
    )
    _header("完成")
    _safe_print(f"  综合报告: {rp}")
    _safe_print(f"  图表目录: {chart_dir}")
    _safe_print(f"  表格目录: {DEFAULT_OUT / 'csv'}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    from . import charts, db

    _header("运行环境与数据库信息")
    _safe_print(f"Python       : {sys.version.split()[0]}")
    _safe_print(f"项目根目录   : {ROOT}")
    for name in ("pandas", "numpy", "openpyxl", "xlsxwriter", "matplotlib",
                 "bs4", "scipy"):
        try:
            mod = __import__(name)
            _safe_print(f"{name:<13}: {getattr(mod, '__version__', '已安装')}")
        except ImportError:
            _safe_print(f"{name:<13}: 未安装(可选依赖)")
    ok, font = charts.cjk_font_available()
    _safe_print(f"图表后端     : {charts.backend_name()}")
    _safe_print(f"CJK 字体可用 : {ok}{f' ({font})' if font else ''}")

    p = Path(args.db)
    _safe_print(f"\n数据库       : {p} ({'存在' if p.exists() else '不存在'})")
    if p.exists():
        conn = db.connect(p)
        try:
            st = db.stats(conn)
            _safe_print(f"  中医药文件数: {st['n_docs']}")
            _safe_print(f"  时间跨度    : {st['first_date']} ~ {st['last_date']}")
            _safe_print(f"  发文机关数  : {st['n_orgs']}")
            _safe_print(f"  主题数      : {st['n_themes']}")
            _safe_print(f"  有正文      : {st['n_with_body']}")
            _safe_print(f"  FTS5 trigram: {st['fts5_trigram']}")
        finally:
            conn.close()
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tcm_rws",
        description="中医药政策与真实世界研究工具 (TCMPolicyRWS)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python tcm_rws.py crawl --max-pages 3\n"
               "  python tcm_rws.py search 中医药\n"
               "  python tcm_rws.py analyze\n"
               "  python tcm_rws.py rws --adr examples/sample_adr_synthetic.csv\n"
               "  python tcm_rws.py demo\n",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("crawl", help="采集 gov.cn 中医药政策文件")
    c.add_argument("--db", default=str(DEFAULT_DB))
    c.add_argument("--keywords", default=None, help="逗号分隔;默认使用内置关键词集")
    c.add_argument("--sources", nargs="+",
                   default=["zhengcelibrary_bm", "zhengcelibrary_gw"],
                   choices=["zhengcelibrary_bm", "zhengcelibrary_gw"])
    c.add_argument("--max-pages", type=int, default=3)
    c.add_argument("--page-size", type=int, default=20)
    c.add_argument("--no-body", action="store_true", help="不抓取正文(更快)")
    c.add_argument("--max-docs", type=int, default=None, help="正文抓取条数上限")
    c.add_argument("--sleep", type=float, default=1.0, help="请求间隔秒数")
    c.set_defaults(func=cmd_crawl)

    s = sub.add_parser("search", help="本地政策库全文检索")
    s.add_argument("term")
    s.add_argument("--db", default=str(DEFAULT_DB))
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--year-from", type=int, default=None)
    s.add_argument("--year-to", type=int, default=None)
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("analyze", help="政策变迁分析并导出")
    a.add_argument("--db", default=str(DEFAULT_DB))
    a.add_argument("--out", default=str(DEFAULT_OUT))
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("rws", help="真实世界研究分析")
    r.add_argument("--adr", default=None, help="ADR 报表 CSV")
    r.add_argument("--rx", default=None, help="取药/处方记录 CSV(依从性)")
    r.add_argument("--coprescribing", default=None, help="联合用药 CSV")
    r.add_argument("--drg", default=None, help="DRG 病组成本 CSV")
    r.add_argument("--rate", type=float, default=None, help="默认费率(元/权重)")
    r.add_argument("--grace-days", type=int, default=30)
    r.add_argument("--out", default=str(DEFAULT_OUT))
    r.set_defaults(func=cmd_rws)

    d = sub.add_parser("demo", help="一键跑通全流程")
    d.add_argument("--db", default=str(DEFAULT_DB))
    d.add_argument("--max-pages", type=int, default=3)
    d.add_argument("--max-docs", type=int, default=None)
    d.add_argument("--sleep", type=float, default=1.0)
    d.set_defaults(func=cmd_demo)

    i = sub.add_parser("info", help="显示环境与数据库信息")
    i.add_argument("--db", default=str(DEFAULT_DB))
    i.set_defaults(func=cmd_info)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    _setup_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _safe_print("\n已中断。")
        return 130
    except Exception as exc:
        _safe_print(f"\n[错误] {type(exc).__name__}: {exc}")
        _safe_print("详细堆栈:")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
