"""图表渲染:matplotlib + 中文字体,无可用字体时回退手写 SVG。

为什么必须处理字体
------------------
matplotlib 默认字体(DejaVu Sans)不含 CJK 字形,中文标签会渲染成
"豆腐块"(□□□),这属于**可见的图表缺陷**而非化妆问题。
本模块按优先级探测系统字体(微软雅黑 / 黑体 / 宋体 / 等线),
并**实际渲染一个中文字符验证**后才采用;全部失败时改为输出
手写 SVG(SVG 用文本标签,由浏览器/系统字体渲染中文,不存在缺字问题)。

两条路径产出同一批图表名,调用方无需分支处理。
"""

from __future__ import annotations

import html
import math
import os
from pathlib import Path
from typing import Any, Sequence

# 候选中文字体(Windows 常见),按显示质量排序
_CJK_CANDIDATES: tuple[str, ...] = (
    "Microsoft YaHei", "微软雅黑",
    "SimHei", "黑体",
    "DengXian", "等线",
    "SimSun", "宋体",
    "Microsoft JhengHei",
)

_MATPLOTLIB_READY: bool | None = None
_CJK_FONT: str | None = None

# 配色:低饱和度,避免在报告里喧宾夺主
_COLOR_MAIN = "#2f6f8f"
_COLOR_ALT = "#c1663a"
_COLOR_ACCENT = "#7a9a5b"
_PALETTE = ("#2f6f8f", "#c1663a", "#7a9a5b", "#8a6ea8", "#b08b3e",
            "#4f8fa8", "#a85454", "#5f7f5f", "#6f6f9f", "#9f7f4f")


def cjk_font_available() -> tuple[bool, str | None]:
    """探测 matplotlib 是否可用中文字体。

    先看字体表,再**真实渲染**一次中文串并检查是否有缺字警告 ——
    仅靠字体名存在性判断不够,字重/子集缺失仍可能出现豆腐块。
    """
    global _MATPLOTLIB_READY, _CJK_FONT
    if _MATPLOTLIB_READY is not None:
        return _MATPLOTLIB_READY, _CJK_FONT

    _MATPLOTLIB_READY, _CJK_FONT = False, None
    try:
        import warnings

        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import font_manager

        installed = {f.name for f in font_manager.fontManager.ttflist}
        for cand in _CJK_CANDIDATES:
            if cand not in installed:
                continue
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                import matplotlib.pyplot as plt

                fig = plt.figure(figsize=(2, 1))
                fig.text(0.1, 0.5, "中医药政策", fontname=cand, fontsize=12)
                fig.canvas.draw()
                plt.close(fig)
                missing = [w for w in caught if "missing from font" in str(w.message).lower()]
            if not missing:
                _MATPLOTLIB_READY, _CJK_FONT = True, cand
                break
    except Exception:
        _MATPLOTLIB_READY, _CJK_FONT = False, None
    return _MATPLOTLIB_READY, _CJK_FONT


def _setup_matplotlib() -> Any:
    """配置 matplotlib 全局中文字体与负号显示。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ok, font = cjk_font_available()
    if ok and font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False  # 否则负号显示为方块
    plt.rcParams["figure.dpi"] = 110
    plt.rcParams["savefig.bbox"] = "tight"
    plt.rcParams["axes.grid"] = True
    plt.rcParams["grid.alpha"] = 0.25
    plt.rcParams["axes.axisbelow"] = True
    return plt


# ---------------------------------------------------------------------------
# 手写 SVG 回退
# ---------------------------------------------------------------------------

def _svg_header(w: int, h: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="Microsoft YaHei, SimHei, sans-serif">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<text x="{w/2}" y="28" text-anchor="middle" font-size="17" '
        f'font-weight="600" fill="#22333b">{html.escape(title)}</text>',
    ]


def _svg_bar_chart(
    title: str, labels: Sequence[str], values: Sequence[float], out: Path,
    *, xlabel: str = "", ylabel: str = "", w: int = 980, h: int = 460,
    rotate: int = 0,
) -> None:
    """竖向柱状图的手写 SVG 实现。"""
    left, right, top, bottom = 70, 30, 60, 90
    plot_w, plot_h = w - left - right, h - top - bottom
    vmax = max(values) if values else 1
    vmax = vmax if vmax > 0 else 1
    n = max(len(values), 1)
    slot = plot_w / n
    bw = slot * 0.62

    parts = _svg_header(w, h, title)
    # Y 轴网格与刻度
    for i in range(6):
        y = top + plot_h - plot_h * i / 5
        val = vmax * i / 5
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" '
                     f'stroke="#e3e8ea" stroke-width="1"/>')
        parts.append(f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="#6b7a80">{val:.0f}</text>')
    # 柱体
    for i, (lab, val) in enumerate(zip(labels, values)):
        bh = plot_h * (val / vmax)
        x = left + slot * i + (slot - bw) / 2
        y = top + plot_h - bh
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                     f'fill="{_COLOR_MAIN}" opacity="0.9"/>')
        parts.append(f'<text x="{x+bw/2:.1f}" y="{y-5:.1f}" text-anchor="middle" '
                     f'font-size="10" fill="#33474d">{val:g}</text>')
        tx, ty = x + bw / 2, top + plot_h + 16
        transform = f' transform="rotate(-{rotate} {tx:.1f} {ty:.1f})"' if rotate else ""
        anchor = "end" if rotate else "middle"
        parts.append(f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{anchor}" '
                     f'font-size="10" fill="#33474d"{transform}>{html.escape(str(lab))}</text>')
    parts.append(f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" '
                 f'y2="{top+plot_h}" stroke="#9aa8ad" stroke-width="1.2"/>')
    if ylabel:
        parts.append(f'<text x="18" y="{top+plot_h/2:.0f}" text-anchor="middle" '
                     f'font-size="12" fill="#33474d" '
                     f'transform="rotate(-90 18 {top+plot_h/2:.0f})">{html.escape(ylabel)}</text>')
    if xlabel:
        parts.append(f'<text x="{left+plot_w/2:.0f}" y="{h-14}" text-anchor="middle" '
                     f'font-size="12" fill="#33474d">{html.escape(xlabel)}</text>')
    parts.append("</svg>")
    out.write_text("\n".join(parts), encoding="utf-8")


def _svg_hbar_chart(
    title: str, labels: Sequence[str], values: Sequence[float], out: Path,
    *, xlabel: str = "", w: int = 980, h: int | None = None,
) -> None:
    """横向条形图手写 SVG —— 机关名称很长时优于竖向柱状图。"""
    n = max(len(values), 1)
    rowh = 26
    h = h or (70 + rowh * n + 50)
    left, right, top = 250, 70, 52
    plot_w = w - left - right
    vmax = max(values) if values else 1
    vmax = vmax if vmax > 0 else 1

    parts = _svg_header(w, h, title)
    for i, (lab, val) in enumerate(zip(labels, values)):
        y = top + i * rowh
        bw = plot_w * (val / vmax)
        color = _PALETTE[i % len(_PALETTE)]
        parts.append(f'<text x="{left-8}" y="{y+13}" text-anchor="end" font-size="11" '
                     f'fill="#33474d">{html.escape(str(lab)[:26])}</text>')
        parts.append(f'<rect x="{left}" y="{y+2}" width="{bw:.1f}" height="{rowh-9}" '
                     f'fill="{color}" opacity="0.88"/>')
        parts.append(f'<text x="{left+bw+6:.1f}" y="{y+13}" font-size="10.5" '
                     f'fill="#33474d">{val:g}</text>')
    if xlabel:
        parts.append(f'<text x="{left+plot_w/2:.0f}" y="{h-12}" text-anchor="middle" '
                     f'font-size="12" fill="#33474d">{html.escape(xlabel)}</text>')
    parts.append("</svg>")
    out.write_text("\n".join(parts), encoding="utf-8")


def _svg_line_chart(
    title: str, xs: Sequence[Any], series: dict[str, Sequence[float]], out: Path,
    *, xlabel: str = "", ylabel: str = "", w: int = 980, h: int = 470,
) -> None:
    """多系列折线图手写 SVG。"""
    left, right, top, bottom = 70, 170, 58, 70
    plot_w, plot_h = w - left - right, h - top - bottom
    allv = [v for s in series.values() for v in s]
    vmax = max(allv) if allv else 1
    vmax = vmax if vmax > 0 else 1
    n = max(len(xs), 1)

    def px(i: int) -> float:
        return left + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def py(v: float) -> float:
        return top + plot_h - plot_h * (v / vmax)

    parts = _svg_header(w, h, title)
    for i in range(6):
        y = top + plot_h - plot_h * i / 5
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" '
                     f'stroke="#e3e8ea"/>')
        parts.append(f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" '
                     f'fill="#6b7a80">{vmax*i/5:.0f}</text>')
    for si, (name, vals) in enumerate(series.items()):
        if not vals:
            continue
        color = _PALETTE[si % len(_PALETTE)]
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     f'stroke-width="2"/>')
        for i, v in enumerate(vals):
            parts.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="2.8" fill="{color}"/>')
        ly = top + 14 + si * 19
        parts.append(f'<line x1="{left+plot_w+14}" y1="{ly}" x2="{left+plot_w+34}" y2="{ly}" '
                     f'stroke="{color}" stroke-width="2.5"/>')
        parts.append(f'<text x="{left+plot_w+40}" y="{ly+4}" font-size="11" '
                     f'fill="#33474d">{html.escape(name[:14])}</text>')
    step = max(1, n // 14)
    for i, lab in enumerate(xs):
        if i % step and i != n - 1:
            continue
        parts.append(f'<text x="{px(i):.1f}" y="{top+plot_h+18}" text-anchor="end" '
                     f'font-size="10" fill="#33474d" '
                     f'transform="rotate(-45 {px(i):.1f} {top+plot_h+18})">{html.escape(str(lab))}</text>')
    parts.append(f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" '
                 f'stroke="#9aa8ad" stroke-width="1.2"/>')
    if ylabel:
        parts.append(f'<text x="18" y="{top+plot_h/2:.0f}" text-anchor="middle" font-size="12" '
                     f'fill="#33474d" transform="rotate(-90 18 {top+plot_h/2:.0f})">'
                     f'{html.escape(ylabel)}</text>')
    if xlabel:
        parts.append(f'<text x="{left+plot_w/2:.0f}" y="{h-10}" text-anchor="middle" '
                     f'font-size="12" fill="#33474d">{html.escape(xlabel)}</text>')
    parts.append("</svg>")
    out.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# 公共入口:统一按可用字体选择渲染后端
# ---------------------------------------------------------------------------

def _finish(fig: Any, out: Path) -> Path:
    fig.savefig(out)
    import matplotlib.pyplot as plt
    plt.close(fig)
    return out


def bar_chart(
    title: str, labels: Sequence[str], values: Sequence[float], out: str | Path,
    *, xlabel: str = "", ylabel: str = "文件数", rotate: int = 0,
    figsize: tuple[float, float] = (12, 5), annotate: bool = True,
) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ok, _ = cjk_font_available()
    if not ok:
        _svg_bar_chart(title, labels, values, out.with_suffix(".svg"),
                       xlabel=xlabel, ylabel=ylabel, rotate=rotate)
        return out.with_suffix(".svg")

    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=figsize)
    xs = range(len(values))
    ax.bar(list(xs), list(values), color=_COLOR_MAIN, width=0.64, alpha=0.9)
    if annotate:
        for x, v in zip(xs, values):
            ax.annotate(f"{v:g}", (x, v), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=9)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([str(l) for l in labels], rotation=rotate,
                       ha="right" if rotate else "center")
    ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    return _finish(fig, out)


def hbar_chart(
    title: str, labels: Sequence[str], values: Sequence[float], out: str | Path,
    *, xlabel: str = "文件数", figsize: tuple[float, float] | None = None,
) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ok, _ = cjk_font_available()
    if not ok:
        _svg_hbar_chart(title, labels, values, out.with_suffix(".svg"), xlabel=xlabel)
        return out.with_suffix(".svg")

    plt = _setup_matplotlib()
    n = max(len(values), 1)
    figsize = figsize or (11, max(3.2, 0.42 * n + 1.6))
    fig, ax = plt.subplots(figsize=figsize)
    ys = list(range(n))[::-1]  # 最大值显示在顶部
    ax.barh(ys, list(values), color=[_PALETTE[i % len(_PALETTE)] for i in range(n)],
            height=0.66, alpha=0.9)
    ax.set_yticks(ys)
    ax.set_yticklabels([str(l) for l in labels], fontsize=9.5)
    for y, v in zip(ys, values):
        ax.annotate(f"{v:g}", (v, y), textcoords="offset points", xytext=(4, -3),
                    fontsize=9)
    ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel(xlabel)
    ax.grid(axis="y", alpha=0)
    return _finish(fig, out)


def line_chart(
    title: str, xs: Sequence[Any], series: dict[str, Sequence[float]], out: str | Path,
    *, xlabel: str = "", ylabel: str = "", figsize: tuple[float, float] = (12, 5.5),
    rotate: int = 45, markers: bool = True,
) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ok, _ = cjk_font_available()
    if not ok:
        _svg_line_chart(title, xs, series, out.with_suffix(".svg"),
                        xlabel=xlabel, ylabel=ylabel)
        return out.with_suffix(".svg")

    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=figsize)
    for i, (name, vals) in enumerate(series.items()):
        ax.plot(list(xs), list(vals), marker="o" if markers else None,
                markersize=3.6, linewidth=1.9,
                color=_PALETTE[i % len(_PALETTE)], label=name)
    ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", rotation=rotate)
    if len(series) > 1:
        ax.legend(fontsize=9, frameon=False, ncol=2)
    return _finish(fig, out)


def stacked_bar_chart(
    title: str, labels: Sequence[str], series: dict[str, Sequence[float]],
    out: str | Path, *, xlabel: str = "", ylabel: str = "文件数",
    figsize: tuple[float, float] = (13, 6), rotate: int = 45,
) -> Path:
    """堆叠柱状图(主题 × 年份)。SVG 回退时降级为多系列折线 + 提示。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ok, _ = cjk_font_available()
    if not ok:
        _svg_line_chart(title + "(SVG 回退:堆叠图降级为折线)", labels, series,
                        out.with_suffix(".svg"), xlabel=xlabel, ylabel=ylabel)
        return out.with_suffix(".svg")

    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=figsize)
    bottom = [0.0] * len(labels)
    for i, (name, vals) in enumerate(series.items()):
        vals = list(vals)
        ax.bar(list(range(len(labels))), vals, bottom=bottom, width=0.72,
               color=_PALETTE[i % len(_PALETTE)], label=name, alpha=0.92)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_xticks(list(range(len(labels))))
    ax.set_xticklabels([str(l) for l in labels], rotation=rotate, ha="right")
    ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.legend(fontsize=9, frameon=False, ncol=2)
    return _finish(fig, out)


def heatmap(
    title: str, row_labels: Sequence[str], col_labels: Sequence[str],
    matrix: Sequence[Sequence[float]], out: str | Path,
    *, xlabel: str = "", ylabel: str = "", figsize: tuple[float, float] = (13, 6.5),
    cmap: str = "YlGnBu",
) -> Path:
    """热力图。SVG 回退时改用堆叠折线表达同样的二维分布。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ok, _ = cjk_font_available()
    if not ok:
        series = {
            str(r): [float(v) for v in row] for r, row in zip(row_labels, matrix)
        }
        _svg_line_chart(title + "(SVG 回退)", col_labels, series,
                        out.with_suffix(".svg"), xlabel=xlabel, ylabel=ylabel)
        return out.with_suffix(".svg")

    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=figsize)
    data = [list(map(float, r)) for r in matrix]
    im = ax.imshow(data, aspect="auto", cmap=cmap)
    ax.set_xticks(range(len(col_labels)))
    ax.set_xticklabels([str(c) for c in col_labels], rotation=45, ha="right", fontsize=9)
    ax.set_yticks(range(len(row_labels)))
    ax.set_yticklabels([str(r) for r in row_labels], fontsize=9)
    ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(False)
    fig.colorbar(im, ax=ax, shrink=0.8, label="文件数")
    return _finish(fig, out)


def pie_chart(
    title: str, labels: Sequence[str], values: Sequence[float], out: str | Path,
    *, figsize: tuple[float, float] = (9, 6.5), min_pct: float = 2.0,
) -> Path:
    """饼图。占比低于 min_pct 的类别合并为"其他",避免标签重叠不可读。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    total = sum(values) or 1
    keep_l, keep_v, other = [], [], 0.0
    for l, v in zip(labels, values):
        if v / total * 100 < min_pct:
            other += v
        else:
            keep_l.append(str(l))
            keep_v.append(float(v))
    if other > 0:
        keep_l.append("其他")
        keep_v.append(other)

    ok, _ = cjk_font_available()
    if not ok:
        _svg_hbar_chart(title, keep_l, keep_v, out.with_suffix(".svg"),
                        xlabel="文件数")
        return out.with_suffix(".svg")

    plt = _setup_matplotlib()
    fig, ax = plt.subplots(figsize=figsize)
    ax.pie(keep_v, labels=keep_l, autopct="%1.1f%%", startangle=100,
           colors=[_PALETTE[i % len(_PALETTE)] for i in range(len(keep_v))],
           textprops={"fontsize": 9.5}, wedgeprops={"edgecolor": "white", "linewidth": 1})
    ax.set_title(title, fontsize=14, pad=12)
    return _finish(fig, out)


def backend_name() -> str:
    """返回当前实际使用的渲染后端描述,供报告如实标注。"""
    ok, font = cjk_font_available()
    if ok:
        return f"matplotlib (CJK 字体: {font})"
    return "手写 SVG (未探测到可用 CJK 字体,已回退)"
