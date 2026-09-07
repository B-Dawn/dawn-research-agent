#!/usr/bin/env python3
"""周频文献追踪：跑一组固定检索式，与上次快照比对，只报新增。

用法：
  python ra_track.py --queries queries.txt --days 7
  python ra_track.py --query "UAV intrusion detection" --query "graph IDS" --limit 15
  python ra_track.py --queries queries.txt --reset        # 清空快照，重新建基线
  python ra_track.py --queries queries.txt --out output/weekly/2026-09-06.md

queries.txt 每行一条检索式，# 开头为注释，空行忽略。
首次运行只建基线不报新增；之后每次运行输出「上次之后的新论文」。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import net, sources  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(SKILL_ROOT, "output", ".state", "track_state.json")


def build_parser():
    p = argparse.ArgumentParser(description="周频文献追踪")
    p.add_argument("--queries", default=None, help="检索式文件路径，每行一条")
    p.add_argument("--query", action="append", default=[], help="直接指定检索式，可重复")
    p.add_argument("--sources", default=",".join(sources.ALL_SOURCES))
    p.add_argument("--limit", type=int, default=12, help="每个检索式每源取多少条")
    p.add_argument("--days", type=int, default=7, help="只看最近 N 天发表的（arXiv 按提交日）")
    p.add_argument("--out", default=None, help="输出 Markdown 报告路径")
    p.add_argument("--reset", action="store_true", help="清空快照重建基线")
    p.add_argument("--state", default=STATE_PATH, help="快照文件路径")
    return p


def load_queries(path):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def _key(rec):
    return (rec.get("doi") or "").lower() or "t:" + (
        "".join(ch for ch in (rec.get("title") or "").lower() if ch.isalnum())[:80])


def run(queries, srcs, limit, days, state_path, reset):
    state = {}
    if not reset and os.path.isfile(state_path):
        try:
            state = net.read_json(state_path)
        except Exception:  # noqa: BLE001
            state = {}

    seen_map = state.get("queries") or {}
    report = []
    total_new = 0

    for q in queries:
        print("追踪: %s" % q)
        records = sources.search(q, sources=srcs, limit=limit)
        # 各源能给出的时间粒度不一（arXiv 只到年份），统一退化为年份过滤。
        # 真正的「新增」判定靠快照去重，不靠时间窗，避免漏报。
        min_year = time.gmtime(time.time() - days * 86400).tm_year if days > 0 else None
        fresh = records
        if min_year:
            fresh = [r for r in records if (r.get("year") or 0) >= min_year]

        prev = seen_map.get(q, {})
        new_items, all_keys = [], {}
        for r in fresh:
            k = _key(r)
            all_keys[k] = {
                "title": r.get("title") or "",
                "year": r.get("year"),
                "venue": r.get("venue") or "",
                "first_seen": time.strftime("%Y-%m-%d"),
                "url": r.get("url") or "",
                "citations": r.get("citations"),
            }
            if k not in prev:
                new_items.append(r)

        new_items.sort(key=lambda r: -(r.get("citations") or 0))
        seen_map[q] = all_keys
        total_new += len(new_items)
        report.append({"query": q, "total": len(fresh), "new": new_items})
        print("  命中 %d 条，新增 %d 条" % (len(fresh), len(new_items)))

    net.write_json(state_path, {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "days": days,
        "queries": seen_map,
    })
    return report, total_new


def render(report, total_new, days):
    stamp = time.strftime("%Y-%m-%d")
    lines = ["# 文献追踪周报 · %s" % stamp, ""]
    lines.append("窗口：最近 %d 天 | 本次新增 %d 篇" % (days, total_new))
    lines.append("")
    if total_new == 0:
        lines.append("本次没有新论文。要么方向确实冷清，要么检索式太窄——后者更常见。")
        lines.append("")
    for block in report:
        lines.append("## %s" % block["query"])
        lines.append("")
        lines.append("命中 %d 条，新增 %d 条" % (block["total"], len(block["new"])))
        lines.append("")
        if not block["new"]:
            lines.append("_无新增_")
            lines.append("")
            continue
        lines.append("| 年份 | 标题 | 发表处 | 被引 | 链接 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in block["new"]:
            title = (r.get("title") or "").replace("|", "\\|")[:110]
            url = r.get("url") or (("https://doi.org/" + r["doi"]) if r.get("doi") else "")
            lines.append("| %s | %s | %s | %s | %s |" % (
                r.get("year") or "-", title,
                (r.get("venue") or "-")[:30].replace("|", "\\|"),
                r.get("citations") if r.get("citations") is not None else "-",
                ("[链接](%s)" % url) if url else "-"))
        lines.append("")
    lines.append("## 处置动作")
    lines.append("")
    lines.append("- [ ] 扫一遍标题，把明显无关的剔除")
    lines.append("- [ ] 相关的 2-3 篇下载精读，补进文献矩阵")
    lines.append("- [ ] 发现新的强竞品，回到 02-innovation.md 更新差异化对照")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    net._ensure_utf8()
    args = build_parser().parse_args(argv)

    queries = list(args.query or [])
    if args.queries:
        if not os.path.isfile(args.queries):
            print("[错误] 检索式文件不存在: %s" % args.queries)
            return 2
        queries.extend(load_queries(args.queries))
    queries = [q for q in dict.fromkeys(queries) if q]
    if not queries:
        print("[错误] 至少给一条检索式（--query 或 --queries）")
        return 2

    srcs = [s.strip() for s in args.sources.split(",") if s.strip()]
    is_first = not os.path.isfile(args.state) or args.reset
    report, total_new = run(queries, srcs, args.limit, args.days,
                            args.state, args.reset)

    print("")
    if is_first:
        print("首次运行，已建立基线快照。下次运行才会报新增。")
        print("快照: %s" % args.state)
        return 0

    md = render(report, total_new, args.days)
    out = args.out or os.path.join(
        SKILL_ROOT, "output", "weekly", "track-%s.md" % time.strftime("%Y%m%d"))
    net.write_text(out, md)
    print("新增 %d 篇" % total_new)
    print("报告: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
