#!/usr/bin/env python3
"""读取 CSV 生成论文级 SVG 图，零第三方依赖（不需要 matplotlib）。

用法：
  python ra_plot.py --csv results.csv --x epochs --y acc,f1 --type line --out fig.svg
  python ra_plot.py --csv results.csv --x method --y acc,f1 --type bar --out bar.svg
  python ra_plot.py --csv xy.csv --x x --y y --type scatter --out sc.svg
  python ra_plot.py --csv results.csv --list          # 只看列名，不出图

CSV 约定：第一行是表头。--x 指定横轴列，--y 指定一个或多个纵轴列（逗号分隔）。
空白单元格会被跳过；非数值型横轴（如方法名）自动按类别处理。
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import net, svg  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_parser():
    p = argparse.ArgumentParser(description="CSV -> SVG 图表（纯标准库）")
    p.add_argument("--csv", help="输入 CSV 路径")
    p.add_argument("--x", default=None, help="横轴列名")
    p.add_argument("--y", default=None, help="纵轴列名，多个用逗号分隔")
    p.add_argument("--type", choices=["line", "bar", "scatter"], default="line")
    p.add_argument("--out", default=None, help="输出 SVG 路径")
    p.add_argument("--title", default="", help="图标题")
    p.add_argument("--xlabel", default="", help="横轴标签")
    p.add_argument("--ylabel", default="", help="纵轴标签")
    p.add_argument("--ymin", type=float, default=None)
    p.add_argument("--ymax", type=float, default=None)
    p.add_argument("--width", type=int, default=900)
    p.add_argument("--height", type=int, default=540)
    p.add_argument("--list", action="store_true", help="只列出列名与预览")
    p.add_argument("--encoding", default="utf-8-sig",
                   help="CSV 编码，默认 utf-8-sig（兼容 Excel 导出的 BOM）")
    return p


def load_csv(path, encoding="utf-8-sig"):
    with open(path, "r", encoding=encoding, newline="") as fh:
        reader = csv.reader(fh)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    header = [h.strip() for h in rows[0]]
    width = len(header)
    data = []
    for r in rows[1:]:
        r = (r + [""] * width)[:width]
        data.append(dict(zip(header, [c.strip() for c in r])))
    return header, data


def _to_float(v):
    try:
        return float(str(v).strip())
    except Exception:  # noqa: BLE001
        return None


def main(argv=None):
    net._ensure_utf8()
    args = build_parser().parse_args(argv)

    if not args.csv:
        print("[错误] 需要 --csv")
        return 2
    if not os.path.isfile(args.csv):
        print("[错误] 文件不存在: %s" % args.csv)
        return 2

    header, data = load_csv(args.csv, args.encoding)
    if not header:
        print("[错误] CSV 为空")
        return 2

    if args.list or not args.x or not args.y:
        print("列名（%d 个）：" % len(header))
        for i, h in enumerate(header, 1):
            preview = [d.get(h, "") for d in data[:3]]
            print("  %2d. %-22s  示例: %s" % (i, h, " | ".join(preview)))
        print("")
        print("用法示例：--x %s --y %s" % (header[0], ",".join(header[1:3]) or header[-1]))
        return 0

    if args.x not in header:
        print("[错误] 找不到横轴列 '%s'。可用列名: %s" % (args.x, ", ".join(header)))
        return 2
    ycols = [c.strip() for c in args.y.split(",") if c.strip()]
    missing = [c for c in ycols if c not in header]
    if missing:
        print("[错误] 找不到纵轴列: %s。可用列名: %s" % (", ".join(missing), ", ".join(header)))
        return 2

    numeric_x = all(_to_float(d.get(args.x, "")) is not None for d in data) and data

    if args.type in ("line", "scatter"):
        if not numeric_x:
            print("[错误] %s 图要求横轴是数值列，'%s' 含非数值。改用 --type bar。"
                  % (args.type, args.x))
            return 2
        series = []
        for col in ycols:
            pts = []
            for d in data:
                yv = _to_float(d.get(col, ""))
                if yv is None:
                    continue
                pts.append((_to_float(d.get(args.x, "")), yv))
            pts.sort()
            series.append({
                "name": col,
                "x": [p[0] for p in pts],
                "y": [p[1] for p in pts],
            })
        series = [s for s in series if s["y"]]
        if not series:
            print("[错误] 没有可绘制的数值")
            return 2
        if args.type == "line":
            out_svg = svg.line_chart(series, xlabel=args.xlabel or args.x,
                                     ylabel=args.ylabel or ycols[0],
                                     title=args.title, width=args.width,
                                     height=args.height, ymin=args.ymin, ymax=args.ymax)
        else:
            out_svg = svg.scatter(series, xlabel=args.xlabel or args.x,
                                  ylabel=args.ylabel or ycols[0],
                                  title=args.title, width=args.width,
                                  height=args.height)
    else:
        labels = [d.get(args.x, "") for d in data]
        if len(ycols) == 1:
            values = [_to_float(d.get(ycols[0], "")) for d in data]
            names = [ycols[0]]
        else:
            values = [[_to_float(d.get(c, "")) for d in data] for c in ycols]
            names = ycols
        out_svg = svg.bar_chart(labels, values, xlabel=args.xlabel or args.x,
                                ylabel=args.ylabel or (ycols[0] if len(ycols) == 1 else ""),
                                title=args.title, width=args.width,
                                height=args.height, ymax=args.ymax,
                                group_names=names)

    out = args.out or os.path.join(SKILL_ROOT, "output", "figures", "figure.svg")
    net.write_text(out, out_svg)
    print("图已生成: %s" % out)
    print("类型 %s | 横轴 %s | 纵轴 %s | 数据点 %d"
          % (args.type, args.x, ", ".join(ycols), len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
