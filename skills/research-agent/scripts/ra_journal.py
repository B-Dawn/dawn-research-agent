#!/usr/bin/env python3
"""投稿选刊初筛：按摘要与期刊 scope 的契合度打分。

用法：
  python ra_journal.py --abstract abstract.txt
  python ra_journal.py --text "Explainable credibility monitoring for UAV IDS ..." --top 6
  python ra_journal.py --abstract abstract.txt --target-if 7 --out journal.md

重要：输出只是**初筛排序**，不是决策。IF 与分区会逐年变动，
选刊前必须用最新 JCR / 中科院分区表核实，并亲自看期刊近 3 年是否发过同主题文章。
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import net  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(SKILL_ROOT, "assets", "journals.json")

QSCORE = {"Q1": 15, "Q2": 11, "Q3": 6, "Q4": 2}


def build_parser():
    p = argparse.ArgumentParser(description="期刊匹配初筛")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--abstract", help="摘要文本文件路径")
    g.add_argument("--text", help="直接给出摘要文本")
    p.add_argument("--db", default=DEFAULT_DB, help="期刊库 JSON")
    p.add_argument("--top", type=int, default=8, help="显示前 N 个")
    p.add_argument("--target-if", type=float, default=7.0, help="目标影响因子")
    p.add_argument("--min-if", type=float, default=0.0, help="最低 IF 门槛，低于此直接淘汰")
    p.add_argument("--out", default=None, help="输出 Markdown 报告路径")
    return p


def _tokenize(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff\s\-]", " ", text)
    return set(t for t in re.split(r"[\s\-]+", text) if len(t) > 2)


def score_journal(journal, tokens, target_if, raw_text):
    """三档打分：scope 契合 45 分 + IF 贴合 25 分 + 分区与周期 30 分。"""
    scopes = journal.get("scope") or []
    hits = []
    for term in scopes:
        term_l = term.lower()
        if " " in term_l or "-" in term_l:
            if term_l in raw_text.lower():
                hits.append(term)
        elif term_l in tokens:
            hits.append(term)
    coverage = len(hits) / len(scopes) if scopes else 0.0
    scope_score = min(45.0, coverage * 45.0 * 1.6)

    jif = float(journal.get("if") or 0)
    if target_if <= 0:
        if_score = 12.0
    else:
        gap = abs(jif - target_if)
        if_score = max(0.0, 25.0 - gap * 2.2)
        if jif >= target_if:
            if_score = min(25.0, if_score + 4.0)

    q = (journal.get("jcr") or "").upper()
    quar_score = QSCORE.get(q, 5)
    cyc = journal.get("cycle_weeks") or [12, 24]
    avg_cycle = sum(cyc) / 2.0
    cycle_score = max(0.0, 15.0 - (avg_cycle - 8) * 0.55)

    total = scope_score + if_score + quar_score + cycle_score
    return {
        "abbr": journal.get("abbr") or journal.get("name", ""),
        "name": journal.get("name", ""),
        "if": jif,
        "jcr": journal.get("jcr", "-"),
        "cas": journal.get("cas", "-"),
        "cycle": "%d-%d 周" % (cyc[0], cyc[1]),
        "apc": journal.get("apc", "-"),
        "url": journal.get("url", ""),
        "note": journal.get("note", ""),
        "hits": hits,
        "scope_score": round(scope_score, 1),
        "if_score": round(if_score, 1),
        "total": round(min(100.0, total), 1),
    }


def _odds(total):
    """命中概率是启发式估计，仅用于排序，不构成承诺。"""
    if total >= 78:
        return "较高"
    if total >= 62:
        return "中等"
    if total >= 46:
        return "偏低"
    return "低"


def render(scored, target_if, text_preview):
    lines = ["# 选刊初筛报告", ""]
    lines.append("> 自动生成，仅供排序参考。**IF 与分区须以最新 JCR / 中科院分区表核实**")
    lines.append("> 命中概率为启发式估计，不是承诺。")
    lines.append("")
    lines.append("目标 IF：%.1f" % target_if)
    lines.append("")
    lines.append("稿件指纹（前 200 字）：")
    lines.append("")
    lines.append("```")
    lines.append(text_preview[:200])
    lines.append("```")
    lines.append("")
    lines.append("| 排名 | 期刊 | IF | JCR | 中科院 | 一审周期 | 契合词 | 总分 | 命中概率 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, s in enumerate(scored, 1):
        lines.append("| %d | %s | %.1f | %s | %s | %s | %d 个 | %.1f | %s |" % (
            i, s["abbr"], s["if"], s["jcr"], s["cas"], s["cycle"],
            len(s["hits"]), s["total"], _odds(s["total"])))
    lines.append("")
    lines.append("## 决策表（人工补）")
    lines.append("")
    lines.append("| 期刊 | scope 契合度 | 近3年同主题论文数 | 命中概率 | 风险 | 决定 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for s in scored[:5]:
        lines.append("| %s | %s（命中：%s） | 待核 | %s | 待核 | 待定 |" % (
            s["abbr"], "高" if s["scope_score"] > 30 else "中",
            "、".join(s["hits"][:5]) or "无", _odds(s["total"])))
    lines.append("")
    lines.append("## 下一步必做")
    lines.append("")
    lines.append("1. 到期刊官网核实 Aim & Scope，确认不是擦边")
    lines.append("2. 检索该刊近 3 年是否发表过同主题论文（用 ra_search.py 加刊名限定）")
    lines.append("3. 核实最新 IF 与分区，替换本表数据")
    lines.append("4. 确认单位认定规则与 APC 预算")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    net._ensure_utf8()
    args = build_parser().parse_args(argv)

    if args.abstract:
        if not os.path.isfile(args.abstract):
            print("[错误] 文件不存在: %s" % args.abstract)
            return 2
        text = net.read_text(args.abstract)
    else:
        text = args.text or ""

    if len(text.strip()) < 40:
        print("[错误] 文本太短，至少给 40 个字符的摘要或关键词")
        return 2

    if not os.path.isfile(args.db):
        print("[错误] 期刊库不存在: %s" % args.db)
        return 2
    db = net.read_json(args.db)
    journals = db.get("journals") or []
    if not journals:
        print("[错误] 期刊库为空")
        return 2

    tokens = _tokenize(text)
    scored = [score_journal(j, tokens, args.target_if, text) for j in journals]
    if args.min_if > 0:
        scored = [s for s in scored if s["if"] >= args.min_if]
    scored.sort(key=lambda s: s["total"], reverse=True)
    scored = scored[:max(1, args.top)]

    print("选刊初筛（目标 IF %.1f，共 %d 个候选）" % (args.target_if, len(scored)))
    print("")
    for i, s in enumerate(scored, 1):
        print("%2d. [%.1f 分] %-16s IF %4.1f | %s | %s | 命中%s"
              % (i, s["total"], s["abbr"], s["if"], s["jcr"], s["cas"], _odds(s["total"])))
        if s["hits"]:
            print("      契合词: %s" % "、".join(s["hits"][:8]))
        if s["note"]:
            print("      注意: %s" % s["note"])
    print("")
    print("提醒：IF/分区为参考值，须以最新 JCR 核实；命中概率仅用于排序。")

    if args.out:
        net.write_text(args.out, render(scored, args.target_if, text.strip()))
        print("")
        print("报告: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
