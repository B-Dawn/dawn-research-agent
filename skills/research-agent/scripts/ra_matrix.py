#!/usr/bin/env python3
"""把检索结果转成文献对比矩阵（Markdown 表格）。

用法：
  python ra_matrix.py --input output/literature/uav-ids.json
  python ra_matrix.py --input a.json --input b.json --out matrix.md --sort year

自动填充的字段：年份、标题、作者、发表处、被引、来源、链接。
留空待人工补的字段：方法、数据集、核心思路（预填摘要片段）、局限、与本文关系。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import net, sources  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLUMNS = [
    ("year", "年份"),
    ("title", "标题"),
    ("authors", "作者"),
    ("venue", "发表处"),
    ("method", "方法"),
    ("dataset", "数据集"),
    ("core_idea", "核心思路"),
    ("limitation", "局限"),
    ("relation", "与本文关系"),
    ("citations", "被引"),
    ("source", "来源"),
]


def build_parser():
    p = argparse.ArgumentParser(description="生成文献对比矩阵")
    p.add_argument("--input", action="append", required=True,
                   help="ra_search.py 产出的 JSON，可重复传入多个")
    p.add_argument("--out", default=None, help="输出 Markdown 路径")
    p.add_argument("--sort", choices=["year", "citation", "title"], default="year",
                   help="排序方式")
    p.add_argument("--desc", action="store_true", help="降序排列")
    p.add_argument("--max", type=int, default=0, help="最多保留多少行，0 为不限")
    p.add_argument("--title", default="文献对比矩阵", help="文档标题")
    return p


def _load(path):
    data = net.read_json(path)
    records = data.get("records")
    if records is None:
        records = data if isinstance(data, list) else []
    return data.get("query", os.path.basename(path)), records


def render(rows, title, queries):
    lines = ["# %s" % title, ""]
    if queries:
        lines.append("检索式：")
        lines.append("")
        for q in queries:
            lines.append("- `%s`" % q)
        lines.append("")
    lines.append("> 自动填充：年份 / 标题 / 作者 / 发表处 / 被引 / 来源 / 核心思路（摘要片段）。")
    lines.append("> 需人工补：方法 / 数据集 / 局限 / 与本文关系。")
    lines.append("")
    lines.append("| " + " | ".join(label for _, label in COLUMNS) + " |")
    lines.append("| " + " | ".join("---" for _ in COLUMNS) + " |")
    for r in rows:
        cells = []
        for key, _ in COLUMNS:
            val = r.get(key, "")
            # 先截断再转义、再拼链接，否则会把 markdown 链接本身截断
            if key == "title" and len(str(val)) > 110:
                val = str(val)[:110] + "…"
            if key == "core_idea" and len(str(val)) > 160:
                val = str(val)[:160] + "…"
            val = str(val).replace("|", "\\|").replace("\n", " ")
            if key == "title" and val:
                url = r.get("url") or (("https://doi.org/" + r["doi"]) if r.get("doi") else "")
                if url:
                    val = "[%s](%s)" % (val, url)
            cells.append(val)
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## 填写提示")
    lines.append("")
    lines.append("- **方法**：一句话说清技术手段，如「GAT + 时序注意力」")
    lines.append("- **数据集**：用了什么数据，没有公开数据就写「自建/未公开」")
    lines.append("- **局限**：优先写作者自己在 Limitations 里承认的，其次是你判断的")
    lines.append("- **与本文关系**：基线 / 竞品 / 可借鉴 / 需对比，四选一并写清理由")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    net._ensure_utf8()
    args = build_parser().parse_args(argv)

    queries, records = [], []
    for path in args.input:
        if not os.path.isfile(path):
            print("[错误] 文件不存在: %s" % path)
            return 2
        q, recs = _load(path)
        queries.append(q)
        records.extend(recs)

    if not records:
        print("[错误] 没有可处理的记录")
        return 2

    merged = sources.dedup(records)
    rows = sources.to_rows(merged)

    reverse = True
    if args.sort == "year":
        rows.sort(key=lambda r: (r.get("year") or 0), reverse=not args.desc)
    elif args.sort == "citation":
        rows.sort(key=lambda r: (r.get("citations") or 0), reverse=not args.desc)
    else:
        rows.sort(key=lambda r: str(r.get("title") or ""), reverse=args.desc)
    del reverse

    if args.max and args.max > 0:
        rows = rows[:args.max]

    md = render(rows, args.title, queries)
    out = args.out or os.path.join(SKILL_ROOT, "output", "literature", "matrix.md")
    net.write_text(out, md)
    print("对比矩阵已生成: %s" % out)
    print("共 %d 行（合并自 %d 条原始记录）" % (len(rows), len(records)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
