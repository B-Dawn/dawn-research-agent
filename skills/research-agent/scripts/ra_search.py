#!/usr/bin/env python3
"""多源文献检索。

用法：
  python ra_search.py "UAV ad hoc network intrusion detection"
  python ra_search.py "graph neural network IDS" --sources arxiv,s2 --limit 30
  python ra_search.py "low-altitude economy security" --from 2022 --to 2026 --out out.json --md

零第三方依赖。输出 JSON + Markdown 摘要。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import net, sources  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_parser():
    p = argparse.ArgumentParser(
        description="多源学术检索（arXiv / Semantic Scholar / OpenAlex / Crossref）")
    p.add_argument("query", help="检索式，建议用英文")
    p.add_argument("--sources", default=",".join(sources.ALL_SOURCES),
                   help="逗号分隔，可选 arxiv,s2,openalex,crossref")
    p.add_argument("--limit", type=int, default=20, help="每个源返回条数上限")
    p.add_argument("--from", dest="year_from", type=int, default=None,
                   help="起始年份")
    p.add_argument("--to", dest="year_to", type=int, default=None, help="截止年份")
    p.add_argument("--out", default=None, help="结果 JSON 输出路径")
    p.add_argument("--md", default=None, help="额外输出 Markdown 清单路径")
    p.add_argument("--abstract", action="store_true", help="摘要中打印摘要片段")
    p.add_argument("--min-year", type=int, default=None, help="结果过滤：丢弃早于该年份的记录")
    return p


def to_markdown(records, query, clip=260):
    lines = ["# 检索结果：%s" % query, "",
             "共 %d 条（已跨源去重）" % len(records), "",
             "| # | 年份 | 标题 | 一作 | 发表处 | 被引 | 来源 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(records, 1):
        title = (r.get("title") or "").replace("|", "\\|")
        first = (r.get("authors") or [""])[0].replace("|", "\\|")
        cites = r.get("citations")
        lines.append("| %d | %s | %s | %s | %s | %s | %s |" % (
            i,
            r.get("year") or "-",
            title[:110],
            first[:24] or "-",
            (r.get("venue") or "-")[:34].replace("|", "\\|"),
            cites if cites is not None else "-",
            r.get("source") or "-",
        ))
    lines.append("")
    lines.append("## 摘要摘录")
    lines.append("")
    for i, r in enumerate(records, 1):
        abstract = (r.get("abstract") or "").strip()
        if not abstract:
            continue
        lines.append("**%d. %s**" % (i, r.get("title") or ""))
        lines.append("")
        lines.append(abstract[:clip] + ("…" if len(abstract) > clip else ""))
        lines.append("")
        if r.get("doi"):
            lines.append("- DOI: `%s`" % r["doi"])
        if r.get("url"):
            lines.append("- 链接: %s" % r["url"])
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    net._ensure_utf8()
    args = build_parser().parse_args(argv)

    srcs = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    unknown = [s for s in srcs if s not in sources.DISPATCH]
    if unknown:
        print("[错误] 未知数据源: %s；可选 %s"
              % (", ".join(unknown), ", ".join(sources.ALL_SOURCES)))
        return 2

    print("检索式: %s" % args.query)
    print("数据源: %s | 每源上限 %d | 年份 %s-%s"
          % (", ".join(srcs), args.limit,
             args.year_from or "不限", args.year_to or "不限"))
    started = time.time()

    records = sources.search(args.query, sources=srcs, limit=args.limit,
                             year_from=args.year_from, year_to=args.year_to)

    if args.min_year:
        records = [r for r in records
                   if not r.get("year") or int(r["year"]) >= args.min_year]

    records.sort(key=lambda r: (-(r.get("citations") or 0), -(r.get("year") or 0)))
    print("命中 %d 条，耗时 %.1fs" % (len(records), time.time() - started))
    print("")

    if not records:
        print("没有结果。建议：换更短的英文检索式、放宽年份、或减少 --sources。")
        return 0

    for i, r in enumerate(records, 1):
        cites = r.get("citations")
        cite_txt = ("被引 %d" % cites) if cites is not None else "被引 -"
        print("[%02d] (%s) %s" % (i, r.get("year") or "????", r.get("title") or ""))
        print("     %s | %s | %s" % (
            (r.get("authors") or ["未知"])[0],
            r.get("venue") or r.get("source") or "-",
            cite_txt))
        if args.abstract and r.get("abstract"):
            snippet = r["abstract"][:180]
            print("     %s%s" % (snippet, "…" if len(r["abstract"]) > 180 else ""))
        if r.get("doi"):
            print("     DOI: %s" % r["doi"])
        print("")

    out_json = args.out
    if not out_json:
        out_dir = os.path.join(SKILL_ROOT, "output", "literature")
        out_json = os.path.join(out_dir, net.slugify(args.query) + ".json")
    net.write_json(out_json, {
        "query": args.query,
        "sources": srcs,
        "year_from": args.year_from,
        "year_to": args.year_to,
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(records),
        "records": records,
    })
    print("结果 JSON: %s" % out_json)

    out_md = args.md
    if out_md:
        net.write_text(out_md, to_markdown(records, args.query))
        print("Markdown 清单: %s" % out_md)

    return 0


if __name__ == "__main__":
    sys.exit(main())
