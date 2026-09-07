"""纯标准库 SVG 图表生成，替代 matplotlib。

论文用图为白底黑字，直接生成独立 .svg 文件，可插入 Word / LaTeX，
矢量不失真，且不依赖任何第三方包。
"""

import math

INK = "#1a1a1a"
MUTED = "#6b7280"
GRID = "#e5e7eb"
PALETTE = [
    "#2563eb", "#dc2626", "#059669", "#d97706",
    "#7c3aed", "#0891b2", "#be185d", "#65a30d",
]
MARKERS = ["circle", "square", "triangle", "diamond", "cross"]


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _nice_ticks(lo, hi, count=6):
    """生成人类可读的坐标轴刻度。"""
    if hi <= lo:
        hi = lo + 1.0
    span = hi - lo
    raw = span / max(1, count)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if step >= raw:
            break
    start = math.floor(lo / step) * step
    ticks = []
    val = start
    while val <= hi + step * 0.5:
        ticks.append(round(val, 10))
        val += step
    return ticks, step


def _fmt(v):
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".")
    return str(v)


def _frame(width, height, title):
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" font-family="Arial, Helvetica, sans-serif">'
        % (width, height, width, height),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    if title:
        parts.append('<text x="%d" y="28" font-size="15" font-weight="600" '
                     'fill="%s" text-anchor="middle">%s</text>'
                     % (width // 2, INK, _esc(title)))
    return parts


def _axes(parts, box, xlabel, ylabel, xticks, yticks, xfmt=_fmt, yfmt=_fmt):
    x0, y0, x1, y1 = box
    for t in yticks:
        y = y1 - (t - yticks[0]) / (yticks[-1] - yticks[0] or 1) * (y1 - y0)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="1"/>' % (x0, y, x1, y, GRID))
        parts.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s" '
                     'text-anchor="end" dominant-baseline="middle">%s</text>'
                     % (x0 - 8, y, MUTED, _esc(yfmt(t))))
    for t in xticks:
        x = x0 + (t - xticks[0]) / (xticks[-1] - xticks[0] or 1) * (x1 - x0)
        parts.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s" '
                     'text-anchor="middle">%s</text>'
                     % (x, y1 + 20, MUTED, _esc(xfmt(t))))
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="1.2"/>' % (x0, y1, x1, y1, INK))
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="1.2"/>' % (x0, y0, x0, y1, INK))
    if xlabel:
        parts.append('<text x="%d" y="%.1f" font-size="13" fill="%s" '
                     'text-anchor="middle">%s</text>'
                     % ((x0 + x1) // 2, y1 + 44, INK, _esc(xlabel)))
    if ylabel:
        cy = (y0 + y1) // 2
        parts.append('<text x="18" y="%d" font-size="13" fill="%s" '
                     'text-anchor="middle" transform="rotate(-90 18 %d)">%s</text>'
                     % (cy, INK, cy, _esc(ylabel)))
    return parts


def _legend(parts, names, box, width):
    if not names:
        return
    x0, _, _, y0 = box
    y = y0 - 18
    total = sum(len(n) for n in names) * 8 + len(names) * 40
    start = max(0, (width - total) // 2)
    cursor = start
    for idx, name in enumerate(names):
        color = PALETTE[idx % len(PALETTE)]
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" '
                     'stroke-width="2.5"/>' % (cursor, y, cursor + 22, y, color))
        parts.append('<circle cx="%d" cy="%d" r="3.5" fill="%s"/>' % (cursor + 11, y, color))
        parts.append('<text x="%d" y="%d" font-size="12" fill="%s" '
                     'dominant-baseline="middle">%s</text>'
                     % (cursor + 28, y, INK, _esc(name)))
        cursor += 28 + len(name) * 8 + 18
    del x0


def _marker(kind, cx, cy, color):
    r = 3.6
    if kind == "square":
        return '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (
            cx - r, cy - r, r * 2, r * 2, color)
    if kind == "triangle":
        return ('<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="%s"/>' % (
            cx, cy - r - 1, cx - r - 1, cy + r, cx + r + 1, cy + r, color))
    if kind == "diamond":
        return ('<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="%s"/>' % (
            cx, cy - r - 1, cx + r + 1, cy, cx, cy + r + 1, cx - r - 1, cy, color))
    return '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/>' % (cx, cy, r, color)


def line_chart(series, xlabel="", ylabel="", title="", width=900, height=540,
               ymin=None, ymax=None, draw_line=True):
    """series: [{"name": str, "x": [...], "y": [...]}, ...]

    draw_line=False 时只画点不连线，用于散点图。
    """
    all_x, all_y = [], []
    for s in series:
        all_x.extend(s.get("x") or [])
        all_y.extend([v for v in (s.get("y") or []) if v is not None])
    if not all_y:
        return ""
    xmin, xmax = min(all_x), max(all_x)
    lo = ymin if ymin is not None else min(all_y)
    hi = ymax if ymax is not None else max(all_y)
    pad = (hi - lo) * 0.08 or 1.0
    if ymin is None:
        lo -= pad
    if ymax is None:
        hi += pad

    parts = _frame(width, height, title)
    top = 74 if title else 44
    box = (78, top, width - 34, height - 78)
    x0, y0, x1, y1 = box
    xticks, _ = _nice_ticks(xmin, xmax, 6)
    yticks, _ = _nice_ticks(lo, hi, 6)

    def px(v):
        return x0 + (v - xticks[0]) / (xticks[-1] - xticks[0] or 1) * (x1 - x0)

    def py(v):
        return y1 - (v - yticks[0]) / (yticks[-1] - yticks[0] or 1) * (y1 - y0)

    _axes(parts, box, xlabel, ylabel, xticks, yticks)

    for idx, s in enumerate(series):
        color = PALETTE[idx % len(PALETTE)]
        marker = MARKERS[idx % len(MARKERS)]
        pts = [(px(x), py(y)) for x, y in zip(s.get("x") or [], s.get("y") or [])
               if y is not None]
        if draw_line and len(pts) > 1:
            d = " ".join("%.1f,%.1f" % p for p in pts)
            parts.append('<polyline points="%s" fill="none" stroke="%s" '
                         'stroke-width="2.2" stroke-linejoin="round"/>' % (d, color))
        for cx, cy in pts:
            parts.append(_marker(marker, cx, cy, color))

    _legend(parts, [s.get("name", "") for s in series], box, width)
    parts.append("</svg>")
    return "\n".join(parts)


def bar_chart(labels, values, xlabel="", ylabel="", title="", width=900,
              height=540, ymax=None, group_names=None):
    """单组或分组柱状图。values: [..] 或 [[..],[..]] 与 group_names 对应。"""
    multi = bool(values) and isinstance(values[0], (list, tuple))
    groups = [list(v) for v in values] if multi else [list(values)]
    names = list(group_names or (["value"] if not multi else
                                 ["series%d" % (i + 1) for i in range(len(groups))]))
    flat = [v for g in groups for v in g if v is not None]
    if not flat:
        return ""
    hi = ymax if ymax is not None else max(flat) * 1.12
    lo = 0.0

    parts = _frame(width, height, title)
    top = 74 if title else 44
    box = (78, top, width - 34, height - 78)
    x0, y0, x1, y1 = box
    yticks, _ = _nice_ticks(lo, hi, 6)

    def py(v):
        return y1 - (v - yticks[0]) / (yticks[-1] - yticks[0] or 1) * (y1 - y0)

    for t in yticks:
        y = py(t)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="1"/>' % (x0, y, x1, y, GRID))
        parts.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s" '
                     'text-anchor="end" dominant-baseline="middle">%s</text>'
                     % (x0 - 8, y, MUTED, _esc(_fmt(t))))
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="1.2"/>' % (x0, y1, x1, y1, INK))
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="1.2"/>' % (x0, y0, x0, y1, INK))
    if xlabel:
        parts.append('<text x="%d" y="%.1f" font-size="13" fill="%s" '
                     'text-anchor="middle">%s</text>'
                     % ((x0 + x1) // 2, y1 + 44, INK, _esc(xlabel)))
    if ylabel:
        cy = (y0 + y1) // 2
        parts.append('<text x="18" y="%d" font-size="13" fill="%s" '
                     'text-anchor="middle" transform="rotate(-90 18 %d)">%s</text>'
                     % (cy, INK, cy, _esc(ylabel)))

    slot = (x1 - x0) / max(1, len(labels))
    gcount = max(1, len(groups))
    bar_w = slot * 0.72 / gcount
    for li, label in enumerate(labels):
        base = x0 + slot * li + slot * 0.14
        for gi, g in enumerate(groups):
            if li >= len(g) or g[li] is None:
                continue
            color = PALETTE[gi % len(PALETTE)]
            bx = base + bar_w * gi
            top_y = py(g[li])
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                         'fill="%s"/>' % (bx, top_y, bar_w, y1 - top_y, color))
        cx = x0 + slot * li + slot / 2
        parts.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s" '
                     'text-anchor="middle">%s</text>'
                     % (cx, y1 + 20, MUTED, _esc(label)))

    _legend(parts, names, box, width)
    parts.append("</svg>")
    return "\n".join(parts)


def scatter(points, xlabel="", ylabel="", title="", width=900, height=540):
    """points: [{"name": str, "x": [...], "y": [...]}, ...]"""
    return line_chart(
        [{"name": p.get("name", ""), "x": p.get("x") or [], "y": p.get("y") or []}
         for p in points],
        xlabel=xlabel, ylabel=ylabel, title=title, width=width, height=height,
        draw_line=False,
    )
