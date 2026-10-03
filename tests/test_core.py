"""单元测试:PDC/MPR、卡方检验、时间线分桶等核心计算。

设计原则
--------
1. **手算期望值**。每个断言的期望值都在注释中给出推导过程,
   便于人工复核 —— 测试的价值在于"我知道正确答案是什么",
   而不是"程序输出什么我就断言什么"。
2. **既可 pytest 运行,也可独立运行**:
       pytest tests/            # 或 python -m pytest tests/
       python tests/test_core.py
   独立运行时不依赖 pytest(手工收集 ``test_*`` 函数并执行)。
3. **不联网**。所有测试只使用本地构造的数据。
"""

from __future__ import annotations

import sys
import traceback
from datetime import date
from pathlib import Path

# 允许从仓库根目录或 tests/ 目录直接运行
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import adr, compliance, db, drg_tcm, policy_analysis as pa, rws  # noqa: E402


# ===========================================================================
# 依从性:PDC / MPR
# ===========================================================================

def _mk(pid: str, fill: str, days: int) -> dict:
    return {"patient_id": pid, "fill_date": fill, "days_supply": str(days)}


def test_pdc_simple_non_overlapping():
    """不重叠取药:PDC = 覆盖天数 / 窗口天数。

    构造:P001 取药 2 次,各 30 天,间隔正好接续。
      2024-01-01 供 30 天 -> 覆盖 01-01 .. 01-30
      2024-01-31 供 30 天 -> 覆盖 01-31 .. 03-01  (1月有31天:01-31 + 29 天 = 02-29,
                                                 再加 1 天 = 03-01)
      验证:2024 是闰年,2 月有 29 天。
        01-31 起 30 天:01-31,02-01..02-29 (29 天) -> 共 30 天,末日 = 02-29。
        改为:01-31 + 29 天 = 02-29。故第二段覆盖 01-31 .. 02-29,共 30 天。
      合并覆盖:01-01 .. 02-29 = 31 + 29 = 60 天。
    显式指定窗口 2024-01-01 .. 2024-03-31(闰年 1-3 月 = 31+29+31 = 91 天)。
      PDC = 60 / 91 ≈ 0.6593
      MPR = (30+30) / 91 = 60/91 ≈ 0.6593
    两段接续无重叠,故 PDC == MPR。这正是设计意图:无重叠时两者应相等。
    """
    rows = [_mk("P001", "2024-01-01", 30), _mk("P001", "2024-01-31", 30)]
    res = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 3, 31)
    )
    assert res["n_patients"] == 1
    assert res["window_days"] == 91, res["window_days"]
    p = res["patients"][0]
    assert p["covered_days"] == 60, p["covered_days"]
    assert p["total_days_supply"] == 60, p["total_days_supply"]
    assert abs(p["pdc"] - round(60 / 91, 4)) < 1e-9, p["pdc"]
    assert abs(p["mpr"] - round(60 / 91, 4)) < 1e-9, p["mpr"]
    assert p["pdc"] == p["mpr"], "无重叠时 PDC 与 MPR 必须相等"


def test_pdc_vs_mpr_with_overlap():
    """重叠取药:PDC < MPR(PDC 按不重复天数计,MPR 累加供药天数)。

    构造:同一患者 2024-01-01 取 30 天,又在 2024-01-11 取 30 天(提前续方)。
      第 1 段:01-01 .. 01-30 (30 天)
      第 2 段:01-11 .. 02-09 (30 天)
      并集:01-01 .. 02-09 = 31 + 9 = 40 天
      供药天数累加 = 60 天
    窗口 2024-01-01 .. 2024-02-29(闰年,31+29 = 60 天)
      PDC = 40 / 60 = 0.6667
      MPR(截尾)= min(60/60, 1) = 1.0
      验证 PDC < MPR,且 MPR 被截尾到 1。
    """
    rows = [_mk("P001", "2024-01-01", 30), _mk("P001", "2024-01-11", 30)]
    res = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 2, 29)
    )
    p = res["patients"][0]
    assert res["window_days"] == 60, res["window_days"]
    assert p["covered_days"] == 40, p["covered_days"]
    assert p["total_days_supply"] == 60, p["total_days_supply"]
    assert p["pdc"] == round(40 / 60, 4), p["pdc"]
    assert p["mpr_raw"] == 1.0, p["mpr_raw"]
    assert p["mpr"] == 1.0, p["mpr"]
    assert p["pdc"] < p["mpr"], "重叠时 PDC 应小于 MPR"


def test_mpr_capped_at_one():
    """MPR 截尾:供药天数远超窗口时 MPR 仍为 1,而 raw 值 > 1。

    构造:2024-01-01 取 100 天,窗口仅 2024-01-01 .. 2024-01-10(10 天)。
      MPR_raw = 100 / 10 = 10.0 -> 截尾为 1.0
      PDC:覆盖裁剪到窗口 = 10 天 -> 10/10 = 1.0
    """
    rows = [_mk("P001", "2024-01-01", 100)]
    res = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 1, 10)
    )
    p = res["patients"][0]
    assert res["window_days"] == 10
    assert p["mpr_raw"] == 10.0, p["mpr_raw"]
    assert p["mpr"] == 1.0, p["mpr"]
    assert p["pdc"] == 1.0, p["pdc"]


def test_pdc_gap_and_threshold():
    """用药间隙与 PDC>=80% 判定。

    构造:P001 取 10 天(01-01..01-10),再取 10 天(01-21..01-30)。
      中间 01-11..01-20 为空档,共 10 天间隙。
      覆盖天数 = 20。
    窗口 2024-01-01 .. 2024-01-30 = 30 天。
      PDC = 20/30 = 0.6667 < 0.8 -> 不计入良好依从
      n_gaps = 1,max_gap_days = 10
    """
    rows = [_mk("P001", "2024-01-01", 10), _mk("P001", "2024-01-21", 10)]
    res = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 1, 30)
    )
    p = res["patients"][0]
    assert p["covered_days"] == 20, p["covered_days"]
    assert p["pdc"] == round(20 / 30, 4), p["pdc"]
    assert p["n_gaps"] == 1, p["n_gaps"]
    assert p["max_gap_days"] == 10, p["max_gap_days"]
    assert p["pdc_ge_80"] is False
    assert res["summary"]["n_pdc_ge_80"] == 0


def test_pdc_ge_80_true():
    """PDC 恰好 >= 80% 时标记为良好依从。

    构造:窗口 2024-01-01 .. 2024-01-10(10 天),取药 8 天(01-01..01-08)。
      PDC = 8/10 = 0.8 -> >= 0.8 为 True(边界值必须计入)。
    """
    rows = [_mk("P001", "2024-01-01", 8)]
    res = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 1, 10)
    )
    p = res["patients"][0]
    assert p["pdc"] == 0.8, p["pdc"]
    assert p["pdc_ge_80"] is True, "0.8 为阈值边界,应判定为达标"
    assert res["summary"]["pct_pdc_ge_80"] == 100.0


def test_persistence_with_grace_period():
    """持续性:允许间隙内的用药视为连续。

    构造:P001 取 10 天(01-01..01-10),间隙 5 天后取 10 天(01-16..01-25)。
      间隙 = 01-11..01-15 = 5 天,grace_days=10 时可跨越。
      持续性 = 01-01 .. 01-25 = 25 天。
    对照 grace_days=3 时不可跨越:
      持续性 = 01-01 .. 01-10 = 10 天。
    """
    rows = [_mk("P001", "2024-01-01", 10), _mk("P001", "2024-01-16", 10)]

    r10 = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 1, 31),
        grace_days=10,
    )
    assert r10["patients"][0]["persistence_days"] == 25, \
        r10["patients"][0]["persistence_days"]

    r3 = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 1, 31),
        grace_days=3,
    )
    assert r3["patients"][0]["persistence_days"] == 10, \
        r3["patients"][0]["persistence_days"]

    # PDC 不受 grace_days 影响 —— 它只统计覆盖天数
    assert r10["patients"][0]["pdc"] == r3["patients"][0]["pdc"]


def test_bad_records_excluded_not_defaulted():
    """缺失/非法 days_supply 的记录必须被排除,而不是默认按 1 天计入。

    若默认按 1 天计入会系统性低估依从性,因此这里显式验证排除计数。
    构造:1 条正常记录(10 天)+ 1 条 days_supply 为空 + 1 条为 0。
      应只保留 1 条,excluded_bad_supply = 2。
    """
    rows = [
        _mk("P001", "2024-01-01", 10),
        {"patient_id": "P001", "fill_date": "2024-01-15", "days_supply": ""},
        {"patient_id": "P001", "fill_date": "2024-01-20", "days_supply": "0"},
    ]
    res = compliance.patient_pdc_mpr(
        rows, window_start=date(2024, 1, 1), window_end=date(2024, 1, 31)
    )
    assert res["n_records_excluded_bad_supply"] == 2, res
    assert res["patients"][0]["n_fills"] == 1, res["patients"][0]


def test_age_group_boundaries():
    """ADR 年龄段边界(手算校验分段边界)。"""
    assert adr._age_group(17.9) == "<18"
    assert adr._age_group(18) == "18-44"
    assert adr._age_group(44.9) == "18-44"
    assert adr._age_group(45) == "45-59"
    assert adr._age_group(59.9) == "45-59"
    assert adr._age_group(60) == "60-74"
    assert adr._age_group(74.9) == "60-74"
    assert adr._age_group(75) == ">=75"
    assert adr._age_group(None) == "未知"
    # 显式分组字段优先于按年龄推算
    assert adr._age_group(30, "自定义组") == "自定义组"


# ===========================================================================
# 卡方检验
# ===========================================================================

def test_chi_square_hand_computed():
    """手算 2x2 列联表的卡方值。

    表:
              A    B    合计
        行1   10   20    30
        行2   30   40    70
        合计  40   60   100

    期望频数 E_ij = R_i * C_j / N:
        E11 = 30*40/100 = 12
        E12 = 30*60/100 = 18
        E21 = 70*40/100 = 28
        E22 = 70*60/100 = 42

    chi2 = Σ (O-E)²/E
         = (10-12)²/12 + (20-18)²/18 + (30-28)²/28 + (40-42)²/42
         = 4/12 + 4/18 + 4/28 + 4/42
         = 0.333333 + 0.222222 + 0.142857 + 0.095238
         = 0.793650...
    df = (2-1)(2-1) = 1
    最小期望频数 = 12
    """
    res = adr.chi_square_test([[10, 20], [30, 40]])
    assert res["df"] == 1, res
    assert abs(res["chi2"] - 0.79365) < 1e-4, res["chi2"]
    assert res["n"] == 100
    assert abs(res["min_expected"] - 12.0) < 1e-9, res["min_expected"]
    # p 值:chi2=0.7937, df=1 -> 双尾 p 约 0.373
    assert abs(res["p"] - 0.3730) < 0.002, res["p"]
    assert res["significant_at_0.05"] is False
    assert res["pct_cells_expected_lt5"] == 0.0


def test_chi_square_perfect_independence():
    """完全独立(观测=期望)时卡方为 0,p 值为 1。

    表:
        10  20   (行合计 30)
        20  40   (行合计 60,总 90)
    期望:E11 = 30*30/90 = 10,E12 = 30*60/90 = 20,
         E21 = 60*30/90 = 20,E22 = 60*60/90 = 40 —— 全部等于观测值,故 chi2 = 0。
    """
    res = adr.chi_square_test([[10, 20], [20, 40]])
    assert abs(res["chi2"]) < 1e-9, res["chi2"]
    assert res["p"] == 1.0, res["p"]


def test_chi_square_known_p_value():
    """校验卡方上尾概率计算:chi2=3.841459, df=1 时 p 应≈0.05。

    这是卡方分布 95% 分位数的定义式,用于验证 _chi2_sf 的实现正确性。
    """
    p = adr._chi2_sf(3.841459, 1)
    assert abs(p - 0.05) < 1e-4, p
    # df=2 时 5.991465 是 95% 分位点
    p2 = adr._chi2_sf(5.991465, 2)
    assert abs(p2 - 0.05) < 1e-4, p2


def test_cramers_v():
    """Cramér's V 与手算一致。

    对 test_chi_square_hand_computed 的表:chi2≈0.79365, n=100, k=min(2,2)=2
      V = sqrt(chi2 / (n * (k-1))) = sqrt(0.79365 / 100) = sqrt(0.0079365)
        = 0.08909...
    """
    v = adr.cramers_v(0.79365, 100, [[10, 20], [30, 40]])
    assert abs(v - 0.0891) < 1e-3, v


def test_chi_square_small_expected_warning():
    """期望频数过小时必须给出警告,而不是默默输出不可信的 p 值。

    小样本表(总 n=20,多数格子期望 < 5)应触发警告。
    """
    res = adr.chi_square_test([[1, 2], [3, 14]])
    assert "warning" in res, res
    assert res["pct_cells_expected_lt5"] > 20.0, res


def test_chi_square_degenerate_tables():
    """退化输入不应崩溃:单行、全零、空表。"""
    assert adr.chi_square_test([])["df"] == 0
    assert adr.chi_square_test([[0, 0], [0, 0]])["p"] == 1.0
    r = adr.chi_square_test([[5, 0], [7, 0]])
    assert r["df"] == 0, r  # 第二列全零被剔除后不足 2 列


# ===========================================================================
# 时间线分桶
# ===========================================================================

def _make_db(tmpdir: Path, docs: list[dict]) -> "db.sqlite3.Connection":
    conn = db.connect(tmpdir / "t.db")
    for d in docs:
        db.upsert_policy(conn, d)
    conn.commit()
    return conn


def test_timeline_year_bucketing(tmp_path=None):
    """按年分桶统计必须准确,且按年升序。

    构造 5 份文件:2019×3、2020×1、2022×1。
    期望:year_counts = [(2019,3),(2020,1),(2022,1)];
          timeline_by_year 中 2020 的同比 = (1-3)/3 = -66.7%,
          2022 的前一年计数为 1(< 3),故 yoy 为 None(基数过小不计算)。
    """
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    docs = [
        {"doc_key": f"d{i}", "title": f"文件{i}", "pubdate": dt, "puborg": "甲机关"}
        for i, dt in enumerate([
            "2019-01-01", "2019-05-05", "2019-12-31",
            "2020-06-01",
            "2022-03-15",
        ])
    ]
    conn = _make_db(tmpdir, docs)
    try:
        yc = db.year_counts(conn)
        got = [(int(r["year"]), int(r["n"])) for r in yc]
        assert got == [(2019, 3), (2020, 1), (2022, 1)], got

        tl = pa.timeline_by_year(conn)
        by = {d["year"]: d for d in tl}
        assert by[2019]["yoy_pct"] is None, "首年无同比"
        assert by[2020]["yoy_pct"] == round((1 - 3) / 3 * 100, 1), by[2020]
        assert by[2020]["yoy_pct"] == -66.7, by[2020]
        assert by[2022]["yoy_pct"] is None, "前一年基数 <3 时不应给出增长率"
    finally:
        conn.close()


def test_year_counts_ignores_null_year():
    """无日期的记录不应进入年度分桶(避免污染时间线)。"""
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    docs = [
        {"doc_key": "a", "title": "有日期", "pubdate": "2021-01-01"},
        {"doc_key": "b", "title": "无日期", "pubdate": ""},
    ]
    conn = _make_db(tmpdir, docs)
    try:
        yc = db.year_counts(conn)
        assert [(int(r["year"]), int(r["n"])) for r in yc] == [(2021, 1)]
        # 但总数仍应包含无日期的记录
        assert db.stats(conn)["n_docs"] == 2
    finally:
        conn.close()


def test_eras_partition_covers_all_docs():
    """阶段划分必须覆盖全部文件(各阶段计数之和 == 总数)。

    这是分桶正确性的关键不变量:阶段边界若有重叠或遗漏,
    求和就会不等于总数。
    """
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    docs = [
        {"doc_key": f"d{i}", "title": f"文件{i}", "pubdate": f"{y}-06-01",
         "puborg": "国务院办公厅"}
        for i, y in enumerate([2005, 2010, 2012, 2017, 2019, 2020, 2023, 2024, 2026])
    ]
    conn = _make_db(tmpdir, docs)
    try:
        pa.annotate_themes(conn)
        eras = pa.era_summary(conn)
        total = sum(e["n_docs"] for e in eras)
        assert total == 9, f"阶段计数之和 {total} != 文件总数 9"
        for e in eras:
            assert e["n_docs"] > 0
    finally:
        conn.close()


def test_robust_z_shift_detection():
    """稳健 z 值断点检测:明显离群的年份应被标记。

    构造 2015-2022 年,计数为 [3,3,3,3,20,3,3,3]
    —— 索引 4 对应年份 **2019**,即激增年在 2019(不是 2020;这里显式写出
    年份而非依赖索引推断,避免测试与被测代码犯同一个错)。

    中位数 = 3,MAD = median(|x-3|) = median([0,0,0,0,17,0,0,0]) = 0
    MAD 为 0 时走退化分支,改用均值比较:
      均值 = (3*7 + 20)/8 = 41/8 = 5.125
      z(2019) = (20 - 5.125)/5.125 = 2.9024... >= 1.5 -> 标记为激增
      其余年份 z = (3-5.125)/5.125 = -0.4146,绝对值 < 1.5 -> 不标记
    """
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    # 显式映射年份 -> 计数,激增年定为 2019
    plan = {2015: 3, 2016: 3, 2017: 3, 2018: 3, 2019: 20, 2020: 3, 2021: 3, 2022: 3}
    docs = []
    for y, n in plan.items():
        for j in range(n):
            docs.append({"doc_key": f"d{y}_{j}", "title": f"文件{y}-{j}",
                         "pubdate": f"{y}-01-{min(j + 1, 28):02d}"})
    conn = _make_db(tmpdir, docs)
    try:
        # 先确认分桶正确,否则后续断言会掩盖真实问题
        yc = {int(r["year"]): int(r["n"]) for r in db.year_counts(conn)}
        assert yc == plan, yc

        shifts = pa.detect_shifts(conn, z_threshold=1.5)
        years = [s["year"] for s in shifts]
        assert years == [2019], f"仅 2019 应被标记为断点,实际 {years}"
        s = shifts[0]
        assert s["n"] == 20, s
        assert s["direction"] == "激增", s
        assert abs(s["z_robust"] - 2.9) < 0.05, s
        assert s["mad"] == 0.0, s   # 确认走了退化分支
    finally:
        conn.close()


def test_median_helper():
    """中位数:奇数取中间,偶数取两中间值平均。"""
    assert pa._median([1, 2, 3]) == 2.0
    assert pa._median([1, 2, 3, 4]) == 2.5
    assert pa._median([5]) == 5.0
    assert pa._median([]) == 0.0


# ===========================================================================
# 数据库与检索
# ===========================================================================

def test_fts_trigram_and_like_fallback():
    """FTS5 trigram 用于 >=3 字查询,<3 字查询必须回退到 LIKE。

    这是本项目最关键的检索设计约束:trigram 无法索引 2 字词,
    若不回退则「中医」「中药」这类最常用检索词会静默返回空结果。

    语料设计(刻意让每个关键词的命中数可区分):
      doc a:标题「中医药发展战略规划」—— 含「中医」「中医药」
             注意:「中医药」(中-医-药)中 **不** 含连续子串「中药」(中-药)
      doc b:标题「关于提升中药质量的意见」—— 含「中药」
    期望:
      查「中医药」(3 字)-> doc a,走 FTS
      查「中医」(2 字)  -> doc a,走 LIKE 回退
      查「中药」(2 字)  -> doc b,走 LIKE 回退
    """
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    docs = [
        {"doc_key": "a", "title": "中医药发展战略规划", "pubdate": "2016-02-22",
         "body": "中医药 战略 规划"},
        {"doc_key": "b", "title": "关于提升中药质量的意见", "pubdate": "2025-03-20",
         "body": "中药 质量 产业"},
    ]
    conn = _make_db(tmpdir, docs)
    try:
        # 3 字:走 FTS
        rows3, fb3 = db.search(conn, "中医药")
        assert len(rows3) == 1, [r["title"] for r in rows3]
        assert rows3[0]["title"] == "中医药发展战略规划"
        if db.has_fts(conn):
            assert fb3 is False, "3 字查询应走 FTS trigram"

        # 2 字「中医」-> LIKE 回退,命中 doc a
        rows2, fb2 = db.search(conn, "中医")
        assert len(rows2) == 1, [r["title"] for r in rows2]
        assert rows2[0]["title"] == "中医药发展战略规划"
        assert fb2 is True, "2 字查询必须使用 LIKE 回退"

        # 2 字「中药」-> LIKE 回退,命中 doc b
        # (「中医药」不含连续子串「中药」,故 doc a 不命中)
        rows3b, fb3b = db.search(conn, "中药")
        assert len(rows3b) == 1, [r["title"] for r in rows3b]
        assert rows3b[0]["title"] == "关于提升中药质量的意见"
        assert fb3b is True

        # 对照:强制 LIKE 的 3 字查询结果应与 FTS 一致(验证两条路径等价)
        rows_like, fb_like = db.search(conn, "中医药", force_like=True)
        assert fb_like is True
        assert {r["title"] for r in rows_like} == {r["title"] for r in rows3}, \
            "FTS 与 LIKE 路径对同一 3 字查询应给出一致结果"
    finally:
        conn.close()


def test_fts_special_chars_not_crashing():
    """含 FTS5 语法字符的查询不应导致 SQL 错误。"""
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    conn = _make_db(tmpdir, [{"doc_key": "a", "title": "中医药政策", "pubdate": "2020-01-01"}])
    try:
        for term in ['中医"药', "中医 AND 药", "中医*", "NEAR(中医)", '"']:
            rows, _ = db.search(conn, term)
            assert isinstance(rows, list)
    finally:
        conn.close()


def test_upsert_idempotent():
    """重复写入同一 doc_key 不应产生重复行。"""
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    doc = {"doc_key": "same", "title": "中医药政策", "pubdate": "2020-01-01"}
    conn = db.connect(tmpdir / "t.db")
    try:
        assert db.upsert_policy(conn, doc) is True
        assert db.upsert_policy(conn, doc) is False, "第二次写入应报告已存在"
        conn.commit()
        assert db.stats(conn)["n_docs"] == 1
    finally:
        conn.close()


# ===========================================================================
# 其他计算
# ===========================================================================

def test_pubdate_parsing():
    """日期归一化:多种格式 -> ISO。"""
    from src import policy_crawler as pc

    assert pc.parse_pubdate("2025.03.20") == "2025-03-20"
    assert pc.parse_pubdate("2025-3-5") == "2025-03-05"
    assert pc.parse_pubdate("2016年12月25日") == "2016-12-25"
    assert pc.parse_pubdate("2025.3") == "2025-03-01"
    assert pc.parse_pubdate("") == ""
    assert pc.parse_pubdate("不是日期") == ""
    # 非法日期(2月30日)应返回空串而非崩溃
    assert pc.parse_pubdate("2025.02.30") == ""


def test_title_cleaning_strips_em_tags():
    """检索接口标题含 <em> 高亮标签,入库前必须剥离。"""
    from src import policy_crawler as pc

    assert pc._clean_title("国务院关于《<em>中医药</em>振兴》的批复") == \
        "国务院关于《中医药振兴》的批复"
    assert pc._clean_title("关于提升中药质量促进中医药产业发展的意见") == \
        "关于提升中药质量促进中医药产业发展的意见"


def test_org_normalization_variants():
    """发文机关归一化:同一机构的各种写法必须映射到同一规范名。

    这是按机关聚合能否成立的前提。若不归一化,gov.cn 的自由文本 puborg
    会产生 80+ 个"机关",其中多数实为同一机构。
    """
    from src import policy_crawler as pc

    # 国家中医药管理局的各种变体
    for v in ["国家中医药管理局", "国家中医药局", "中医药管理局", "中医药局",
              "中医药管理局办公室", "中医药局办公室", "国家中医药局综合司",
              "国家中医药管理局医政司", "国家中医药局等"]:
        assert pc.normalize_org_name(v) == "国家中医药管理局", v

    # 其他机构
    assert pc.normalize_org_name("国家药监局") == "国家药品监督管理局"
    assert pc.normalize_org_name("药监局办公室") == "国家药品监督管理局"
    assert pc.normalize_org_name("国家卫生健康委") == "国家卫生健康委员会"
    assert pc.normalize_org_name("卫生健康委办公厅") == "国家卫生健康委员会"
    assert pc.normalize_org_name("国家医保局") == "国家医疗保障局"
    assert pc.normalize_org_name("国家发展改革委") == "国家发展和改革委员会"


def test_org_split_invisible_separators():
    """联合发文拆分:必须正确处理 \\u2002(EN SPACE)等不可见分隔符。

    gov.cn 的 puborg 字段混用普通空格与 \\u2002 分隔多机关。
    若不归一化空白,「国家药监局\\u2002国家卫生健康委」会被当成一个机关。
    """
    from src import policy_crawler as pc

    # \u2002 (EN SPACE) 分隔
    assert pc.split_orgs("国家药监局\u2002国家卫生健康委\u2002国家医保局") == [
        "国家药品监督管理局", "国家卫生健康委员会", "国家医疗保障局"
    ]
    # \u00a0 (NBSP) 分隔
    assert pc.split_orgs("商务部\u00a0国家中医药管理局") == [
        "商务部", "国家中医药管理局"
    ]
    # \u3000 (全角空格) 分隔
    assert pc.split_orgs("教育部\u3000国家中医药管理局") == [
        "教育部", "国家中医药管理局"
    ]
    # 普通空格与顿号
    assert pc.split_orgs("国家中医药管理局 国家卫生健康委") == [
        "国家中医药管理局", "国家卫生健康委员会"
    ]
    assert pc.split_orgs("国家中医药管理局、国家药监局") == [
        "国家中医药管理局", "国家药品监督管理局"
    ]
    # 去重保序
    assert pc.split_orgs("中医药局 国家中医药管理局") == ["国家中医药管理局"]
    # 空输入
    assert pc.split_orgs("") == []


def test_normalize_item_sets_org_fields():
    """normalize_item 应同时写入主要机关、原始串、完整列表与机关数。"""
    from src import policy_crawler as pc

    item = {
        "title": "关于印发中医药服务管理规范的通知",
        "puborg": "国家中医药局\u2002国家卫生健康委",
        "pubtimeStr": "2025.06.01",
        "url": "https://example.gov.cn/x.htm",
        "pcode": "国中医药发〔2025〕1号",
    }
    d = pc.normalize_item(item, "中医药", "zhengcelibrary_bm")
    assert d["puborg"] == "国家中医药管理局", d       # 主要发文机关 = 第一个
    assert d["puborg_all"] == "国家中医药管理局、国家卫生健康委员会", d
    assert d["n_orgs"] == 2, d
    # puborg_raw 保留原始机关串,但不可见空白已归一为普通空格
    # (归一化是刻意的:否则同一机构会因分隔符不同而被算作不同实体)
    assert d["puborg_raw"] == "国家中医药局 国家卫生健康委", d
    assert "\u2002" not in d["puborg_raw"], "不可见分隔符必须被归一化"
    assert d["doc_key"] == "https://example.gov.cn/x.htm"


def test_theme_classification_priority():
    """主题分类:具体主题优先于宽泛主题(规则有序)。"""
    assert pa.classify_theme("关于印发医院中药饮片管理规范的通知") == "中药质量与标准"
    assert pa.classify_theme("关于中医药医保支付方式改革的意见") == "医保与价格"
    assert pa.classify_theme("关于印发三级中医医院评审标准的通知") == "医院管理与服务体系"
    assert pa.classify_theme("关于少数民族医药发展的意见") == "民族药与少数民族医药"
    # 无关键词命中时归入「其他」
    assert pa.classify_theme("关于开展某项工作的通知") == "其他"


def test_keyword_extraction_counts():
    """关键词计数:非重叠计数,累加出现次数。"""
    text = "中医药 中医药 中药 中药饮片"
    c = pa.extract_keywords(text)
    assert c.get("中医药") == 2, c
    assert c.get("中药") == 2, c   # "中药" 与 "中药饮片" 中的 "中药" 各计一次
    assert c.get("中药饮片") == 1, c
    assert pa.extract_keywords("") == {}


def test_coprescribing_pairs_hand_computed():
    """联合用药:共现对计数手算校验。

    构造 3 组:
      组1: A;B      -> 对 (A,B) +1
      组2: A;B;C    -> 对 (A,B),(A,C),(B,C) 各 +1
      组3: A        -> 无对
    期望:
      n_groups = 3,多药组 = 2 -> 66.67%
      对 (A,B) 出现 2 次(组1、组2),占 2/3 = 66.67%
      对 (A,C)、(B,C) 各 1 次(仅组2)
      A 共现伙伴 = {B, C} -> 2
      B 共现伙伴 = {A, C} -> 2
      C 共现伙伴 = {A, B} -> 2   (C 虽只出现在 1 组,但该组含 A、B 两个伙伴)
      平均每组品种数 = (2 + 3 + 1)/3 = 2.0
    """
    rows = [
        {"patient_id": "1", "drug_names": "A;B"},
        {"patient_id": "2", "drug_names": "A;B;C"},
        {"patient_id": "3", "drug_names": "A"},
    ]
    res = rws.coprescribing_analysis(rows)
    assert res["n_groups"] == 3, res
    assert res["n_groups_multi_drug"] == 2, res
    assert res["pct_multi_drug_groups"] == round(2 / 3 * 100, 2), res
    assert res["mean_drugs_per_group"] == 2.0, res["mean_drugs_per_group"]
    assert res["n_unique_pairs"] == 3, res

    pairs = {(d["drug_a"], d["drug_b"]): d["n_groups"] for d in res["top_pairs"]}
    assert pairs[("A", "B")] == 2, pairs
    assert pairs[("A", "C")] == 1, pairs
    assert pairs[("B", "C")] == 1, pairs

    drugs = {d["drug"]: d for d in res["top_drugs"]}
    assert drugs["A"]["n_comentioned_partners"] == 2, drugs["A"]
    assert drugs["B"]["n_comentioned_partners"] == 2, drugs["B"]
    # C 只出现在组2,但组2 同时含 A、B,故 C 有 2 个共现伙伴
    assert drugs["C"]["n_comentioned_partners"] == 2, drugs["C"]
    # 出现组数(非伙伴数)才是 C 的区分度所在
    assert drugs["C"]["n_groups"] == 1, drugs["C"]


def test_coprescribing_multi_separators():
    """多值分隔符应被统一识别(分号/逗号/顿号/斜杠/加号)。"""
    rows = [{"patient_id": "1", "drug_names": "A、B,C;D/E+F"}]
    res = rws.coprescribing_analysis(rows)
    assert res["n_unique_drugs"] == 6, res


def test_drg_margin_hand_computed():
    """DRG 盈亏:支付标准 = 权重 × 费率,盈亏 = 支付 − 实际成本。

    构造两个病组(费率 10000):
      G1: 权重 1.0 -> 支付 10000;成本 12000 -> 盈亏 -2000(超支)
          中医成本 3000 -> 占比 25%
      G2: 权重 1.5 -> 支付 15000;成本 13000 -> 盈亏 +2000(结余)
          中医成本 5000 -> 占比 38.46%
    汇总:支付 25000,成本 25000,差额 0,超支病组 1 个(50%)。
    """
    rows = [
        {"drg_code": "G1", "weight": "1.0", "rate": "10000",
         "total_cost": "12000", "tcm_cost": "3000"},
        {"drg_code": "G2", "weight": "1.5", "rate": "10000",
         "total_cost": "13000", "tcm_cost": "5000"},
    ]
    res = drg_tcm.analyze_drg(rows)
    cases = {c["drg_code"]: c for c in res["cases"]}
    assert cases["G1"]["payment_standard"] == 10000.0, cases["G1"]
    assert cases["G1"]["margin"] == -2000.0, cases["G1"]
    assert cases["G1"]["status"] == "超支", cases["G1"]
    assert cases["G1"]["tcm_cost_share_pct"] == 25.0, cases["G1"]
    assert cases["G2"]["margin"] == 2000.0, cases["G2"]
    assert cases["G2"]["status"] == "结余", cases["G2"]
    assert abs(cases["G2"]["tcm_cost_share_pct"] - round(5000 / 13000 * 100, 2)) < 0.01

    s = res["summary"]
    assert s["total_payment"] == 25000.0, s
    assert s["total_cost"] == 25000.0, s
    assert s["total_margin"] == 0.0, s
    assert s["n_overspent_groups"] == 1, s
    assert s["pct_overspent_groups"] == 50.0, s


def test_drg_breakeven_rate_hand_computed():
    """盈亏平衡费率 = 实际成本 / 权重。

    G1: 12000 / 1.0 = 12000,当前 10000 -> 缺口 2000
    G2: 13000 / 1.5 = 8666.67,当前 10000 -> 缺口 -1333.33
    """
    rows = [
        {"drg_code": "G1", "weight": "1.0", "rate": "10000",
         "total_cost": "12000", "tcm_cost": "0"},
        {"drg_code": "G2", "weight": "1.5", "rate": "10000",
         "total_cost": "13000", "tcm_cost": "0"},
    ]
    res = drg_tcm.analyze_drg(rows)
    be = {d["drg_code"]: d for d in res["breakeven_rates"]}
    assert be["G1"]["breakeven_rate"] == 12000.0, be["G1"]
    assert be["G1"]["rate_gap"] == 2000.0, be["G1"]
    assert abs(be["G2"]["breakeven_rate"] - round(13000 / 1.5, 2)) < 0.01, be["G2"]
    assert abs(be["G2"]["rate_gap"] - (-1333.33)) < 0.01, be["G2"]


def test_drg_long_format_classification():
    """长表(项目明细)应自动按病组汇总并按项目名归类。"""
    rows = [
        {"drg_code": "G1", "item_name": "中药饮片费", "item_cost": "1000", "weight": "1.0", "rate": "10000"},
        {"drg_code": "G1", "item_name": "针灸治疗费", "item_cost": "500", "weight": "1.0", "rate": "10000"},
        {"drg_code": "G1", "item_name": "CT检查", "item_cost": "300", "weight": "1.0", "rate": "10000"},
        {"drg_code": "G1", "item_name": "床位费", "item_cost": "200", "weight": "1.0", "rate": "10000"},
    ]
    res = drg_tcm.analyze_drg(rows)
    assert res["input_format"].startswith("长表"), res["input_format"]
    c = res["cases"][0]
    assert c["total_cost"] == 2000.0, c
    assert c["tcm_cost"] == 1500.0, c        # 中药饮片 + 针灸
    assert c["western_cost"] == 300.0, c     # CT
    assert c["tcm_cost_share_pct"] == 75.0, c


def test_item_classification():
    """收费项目归类。"""
    assert drg_tcm.classify_item("中药饮片费") == "中医类"
    assert drg_tcm.classify_item("针灸治疗") == "中医类"
    assert drg_tcm.classify_item("推拿治疗") == "中医类"
    assert drg_tcm.classify_item("CT平扫") == "检查检验类"
    assert drg_tcm.classify_item("血常规检验") == "检查检验类"
    assert drg_tcm.classify_item("床位费") == "其他"
    assert drg_tcm.classify_item("") == "其他"


def test_pearson_hand_computed():
    """皮尔逊相关系数手算校验(完全正相关)。

    x = [1,2,3,4], y = [2,4,6,8] -> 完全线性正相关,r = 1.0
    x = [1,2,3,4], y = [8,6,4,2] -> 完全线性负相关,r = -1.0
    """
    r1 = drg_tcm._pearson([1, 2, 3, 4], [2, 4, 6, 8])
    assert abs(r1["r"] - 1.0) < 1e-9, r1
    r2 = drg_tcm._pearson([1, 2, 3, 4], [8, 6, 4, 2])
    assert abs(r2["r"] - (-1.0)) < 1e-9, r2
    # 样本量不足时应拒绝计算
    r3 = drg_tcm._pearson([1, 2], [3, 4])
    assert r3["r"] is None, r3


def test_data_type_coercion():
    """宽松数值转换:容忍单位与脏值,失败返回 None。"""
    assert rws.to_float("12.5") == 12.5
    assert rws.to_float("30 天") == 30.0
    assert rws.to_float("12.5mg") == 12.5
    assert rws.to_float("") is None
    assert rws.to_float("NA") is None
    assert rws.to_float("未知") is None
    assert rws.to_int("7") == 7
    assert rws.to_int("abc") is None


def test_parse_date_variants():
    """日期解析多格式容忍。"""
    assert rws.parse_date("2024-01-15") == date(2024, 1, 15)
    assert rws.parse_date("2024/1/5") == date(2024, 1, 5)
    assert rws.parse_date("20240115") == date(2024, 1, 15)
    assert rws.parse_date("2024年1月15日") == date(2024, 1, 15)
    assert rws.parse_date("") is None
    assert rws.parse_date("无效") is None


def test_covered_days_merging():
    """覆盖天数合并:重叠与相邻区间均需正确合并。"""
    from datetime import date as D

    # 完全重叠的两个区间 -> 只算一次
    assert compliance._covered_days([(D(2024, 1, 1), D(2024, 1, 10)),
                                     (D(2024, 1, 5), D(2024, 1, 15))]) == 15
    # 相邻区间(01-01..01-10 与 01-11..01-20)应合并为 20 天,无间隙
    assert compliance._covered_days([(D(2024, 1, 1), D(2024, 1, 10)),
                                     (D(2024, 1, 11), D(2024, 1, 20))]) == 20
    # 有 1 天间隙(01-11 缺失)则总计 19 天
    assert compliance._covered_days([(D(2024, 1, 1), D(2024, 1, 10)),
                                     (D(2024, 1, 12), D(2024, 1, 20))]) == 19
    assert compliance._covered_days([]) == 0


def test_adr_analysis_end_to_end():
    """ADR 端到端:小规模手算校验各维度分母一致。"""
    rows = [
        {"sex": "男", "age": "70", "route": "口服", "drug_name": "药A",
         "reaction": "皮疹", "outcome": "好转", "causality": "可能"},
        {"sex": "女", "age": "30", "route": "静脉滴注", "drug_name": "药B",
         "reaction": "头晕", "outcome": "痊愈", "causality": "很可能"},
        {"sex": "男", "age": "50", "route": "口服", "drug_name": "药A;药B",
         "reaction": "皮疹", "outcome": "好转", "causality": "可能"},
    ]
    res = adr.analyze_adr(rows)
    assert res["n_reports"] == 3
    # 性别构成比之和应为 100%(男 2,女 1)
    sex = {d["category"]: d for d in res["sex"]}
    assert sex["男"]["n"] == 2 and sex["男"]["pct"] == 66.67, sex
    assert sex["女"]["pct"] == 33.33, sex
    # 年龄段:70 -> 60-74,30 -> 18-44,50 -> 45-59
    ag = {d["category"]: d["n"] for d in res["age_group"]}
    assert ag.get("60-74") == 1 and ag.get("18-44") == 1 and ag.get("45-59") == 1, ag
    # 品种提及:药A 2 次,药B 2 次 -> 共 4 次提及
    assert res["n_drug_mentions"] == 4, res["n_drug_mentions"]
    assert res["n_reports_multi_drug"] == 1, res
    assert res["pct_multi_drug"] == round(1 / 3 * 100, 2), res
    drugs = {d["category"]: d["pct"] for d in res["top_drugs"]}
    assert drugs["药A"] == 50.0, drugs   # 2/4


def test_adr_empty_input():
    """空输入应返回结构化的错误标记而非抛异常。"""
    assert adr.analyze_adr([])["n_reports"] == 0
    assert compliance.patient_pdc_mpr([])["n_patients"] == 0
    assert rws.coprescribing_analysis([])["n_groups"] == 0
    assert drg_tcm.analyze_drg([])["n_cases"] == 0


def test_charts_backend_reported():
    """图表后端必须可探测并给出描述(供报告如实标注)。"""
    from src import charts

    name = charts.backend_name()
    assert isinstance(name, str) and name
    ok, font = charts.cjk_font_available()
    assert isinstance(ok, bool)
    if ok:
        assert font, "报告可用字体时字体名不应为空"


# ===========================================================================
# 独立运行支持(不依赖 pytest)
# ===========================================================================

def _run_standalone() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"运行 {len(tests)} 个测试(独立模式,不依赖 pytest)\n")
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  [PASS] {name}")
        except Exception as exc:
            failed.append((name, exc))
            print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
    print(f"\n结果:{passed} 通过,{len(failed)} 失败,共 {len(tests)} 个")
    if failed:
        print("\n失败详情:")
        for name, exc in failed:
            print(f"\n--- {name} ---")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
