#!/usr/bin/env python3
"""科研画像解析 + 方向推荐。

用法：
  python ra_profile.py --show                       # 解析并打印画像
  python ra_profile.py --recommend                  # 联网检索后推荐方向
  python ra_profile.py --recommend --offline        # 不联网，只给矩阵骨架
  python ra_profile.py --recommend --max-queries 6 --out directions.md

推荐逻辑（不是拍脑袋）：
  候选方向 = 技能模块 × 研究方向 的交叉组合，
  每个组合用真实检索测「近两年论文热度」和「可复用度」，
  再按 复用度35% + 热度25% + 数据可获得性20% - 时间成本20% 打分。
  推荐数封顶 5 个，A 级最多 2 个——推多了等于没推。
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import net, sources  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_profile():
    """按优先级找画像文件，兼容「源码目录」与「安装到 ~/.workbuddy/skills 后」两种布局。"""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(SKILL_ROOT, "profile", "researcher-profile.md"),
        os.path.join(SKILL_ROOT, "..", "..", "profile", "researcher-profile.md"),
        os.path.join(home, ".workbuddy", "skills", "research-agent",
                     "profile", "researcher-profile.md"),
        os.path.join(home, ".workbuddy", "profile", "researcher-profile.md"),
        os.path.join(home, ".workbuddy", "researcher-profile.md"),
    ]
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.isfile(c):
            return c
    return os.path.abspath(candidates[0])


DEFAULT_PROFILE = _default_profile()

DATA_HINTS = [
    "dataset", "benchmark", "ns3", "omnet", "simulation", "cicids", "unsw-nb15",
    "kdd", "ton_iot", "dataset available", "public dataset", "open-source",
    "code available", "github",
]
WEEK_BUDGET_DEFAULT = 12.0


def build_parser():
    p = argparse.ArgumentParser(description="科研画像与方向推荐")
    p.add_argument("--profile", default=None,
                   help="画像 Markdown 路径，默认自动查找：%s" % DEFAULT_PROFILE)
    p.add_argument("--show", action="store_true", help="解析并打印画像")
    p.add_argument("--recommend", action="store_true", help="生成方向推荐")
    p.add_argument("--offline", action="store_true", help="不联网，只给矩阵骨架")
    p.add_argument("--max-queries", type=int, default=8, help="最多跑多少条交叉检索")
    p.add_argument("--limit", type=int, default=10, help="每条交叉检索取多少篇")
    p.add_argument("--years", type=int, default=3, help="热度统计窗口（年），太窄会全部返回 0 篇")
    p.add_argument("--out", default=None, help="输出 Markdown 路径")
    return p


def parse_profile(path):
    """按 '## 标题' 切分章节，每章取 '- ' 开头的条目。"""
    if not os.path.isfile(path):
        return None
    text = net.read_text(path)
    sections, cur, items = {}, None, []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if cur:
                sections[cur] = items
            cur = line[3:].strip()
            items = []
        elif cur and line.startswith("- "):
            items.append(line[2:].strip())
        elif cur and line.startswith("  - ") and items:
            items[-1] += " " + line[4:].strip()
    if cur:
        sections[cur] = items
    return sections


def _keywords(items):
    """从条目里抽关键词。

    括号里的英文会单独抽成一条关键词——中文词拿去英文数据库检索是无效的，
    所以画像里建议写成「低空经济安全（low-altitude economy security）」这种中英对照。
    冒号后的说明性文字直接丢弃。
    """
    words = []
    for it in items:
        it = re.sub(r"^[-*]\s*", "", str(it))
        it = re.sub(r"[:：].*$", "", it)
        parens = re.findall(r"[（(](.*?)[)）]", it)
        main = re.sub(r"[（(].*?[)）]", "", it)
        chunks = [c.strip() for c in re.split(r"[,，;/、]+", main)]
        chunks += [p.strip() for p in parens]
        for c in chunks:
            if 1 <= len(c) <= 40:
                words.append(c)
    return [w for w in dict.fromkeys(words) if w]


def _is_en(text):
    """含拉丁字母且不含中日韩字符，才适合当英文检索式。"""
    if not re.search(r"[a-zA-Z]", text or ""):
        return False
    return not re.search(r"[\u4e00-\u9fff]", text or "")


def _pick(sections, names):
    for n in names:
        if n in sections and sections[n]:
            return sections[n]
    return []


def _contains_any(text, words):
    low = (text or "").lower()
    return sum(1 for w in words if str(w).lower() in low)


def recommend(sections, args):
    domains = _pick(sections, ["研究方向", "Research Directions", "方向"])
    skills = _pick(sections, ["技能模块", "Skills", "技能"])
    assets = _pick(sections, ["已有积累", "已有资产", "Assets"])
    constraints = _pick(sections, ["目标与约束", "约束", "Constraints"])

    domain_kw = _keywords(domains)
    skill_kw = _keywords(skills)
    asset_kw = _keywords(assets)

    if not domain_kw:
        domain_kw = ["network security"]
    if not skill_kw:
        skill_kw = ["machine learning"]

    budget = WEEK_BUDGET_DEFAULT
    for c in constraints:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:小时|h|H)", c)
        if m and ("周" in c or "每周" in c or "week" in c.lower()):
            budget = float(m.group(1))
            break

    # 只有中英双语都能匹配的组合才拿去检索，纯中文词进英文库等于噪音
    en_skills = [s for s in skill_kw if _is_en(s)]
    en_domains = [d for d in domain_kw if _is_en(d)]
    skipped = [d for d in domain_kw if not _is_en(d)]

    # 均匀取样：在「技能 × 方向」二维网格上按步长取点，
    # 保证前 N 个组合既覆盖不同技能、也覆盖不同方向，
    # 不会把名额全压在一个技能或某一个方向上（否则等于没推荐）。
    pool = [(s, d) for s in (en_skills or skill_kw) for d in (en_domains or domain_kw)]
    n = len(pool)
    m = max(1, args.max_queries)
    if n <= m:
        combos = pool[:]
    else:
        idx = sorted(set(int(round(i * n / m)) for i in range(m)))
        combos = [pool[i] for i in idx[:m]]
    if skipped:
        print("提示：以下方向词是纯中文，无法用于英文检索，已跳过：%s"
              % "、".join(skipped))
        print("      建议在画像里补英文，如「%s（english term）」" % skipped[0])
        print("")

    rows = []
    print("交叉组合 %d 个（上限 %d）" % (len(combos), args.max_queries))
    print("")

    for s, d in combos:
        query = "%s %s" % (s, d)
        row = {
            "skill": s,
            "domain": d,
            "query": query,
            "papers": None,
            "heat": 0.0,
            "reuse": 0.0,
            "data": 0.0,
            "hours": None,
            "score": 0.0,
            "evidence": [],
        }
        if args.offline:
            rows.append(row)
            continue

        year_from = time.gmtime().tm_year - args.years + 1
        recs = sources.search(query, limit=args.limit, year_from=year_from)
        recent = [r for r in recs if (r.get("year") or 0) >= year_from]
        row["papers"] = len(recent)

        texts = [((r.get("title") or "") + " " + (r.get("abstract") or "")) for r in recent]
        blob = " ".join(texts).lower()

        # 复用度 = 该方向的近期论文里，能命中你多少项「其它技能」和「已有积累」
        others = [k for k in skill_kw if k.lower() != s.lower()] + asset_kw
        reusable = sum(1 for k in others if k.lower() in blob)
        row["reuse"] = min(1.0, reusable / max(1, len(others)) * 1.4)
        row["data"] = min(1.0, _contains_any(blob, DATA_HINTS) / 6.0)
        row["heat"] = min(1.0, len(recent) / max(3.0, float(args.limit)))
        row["hours"] = round(40.0 + 60.0 * (1.0 - row["reuse"]), 0)
        row["score"] = round(
            (0.35 * row["reuse"] + 0.25 * row["heat"]
             + 0.20 * row["data"] - 0.20 * min(1.0, row["hours"] / 140.0)) * 100, 1)
        row["evidence"] = [
            "%s (%s)" % ((r.get("title") or "")[:70], r.get("year") or "-")
            for r in recent[:3]
        ]
        print("  %-58s 近%d年 %2d 篇 | 复用%.2f 数据%.2f 热度%.2f -> %.1f"
              % (query[:58], args.years, len(recent), row["reuse"],
                 row["data"], row["heat"], row["score"]))
        rows.append(row)

    # 0 篇的组合说明检索没跑通或方向过窄，一律打「数据不足」，
    # 绝不能让它们靠负分之外的信息挤进 A/B 级——没有证据的方向不配被推荐。
    for r in rows:
        r["insufficient"] = (r["papers"] == 0)

    rows.sort(key=lambda r: (r["insufficient"], -r["score"]))
    rows = rows[:5]
    for i, r in enumerate(rows, 1):
        if r["papers"] is None:
            r["grade"] = "待评（离线）"
        elif r["insufficient"]:
            r["grade"] = "D（数据不足）"
        elif i <= 2 and r["score"] >= 45:
            r["grade"] = "A"
        elif r["score"] >= 30:
            r["grade"] = "B"
        else:
            r["grade"] = "C"
    return rows, budget, combos


def render(rows, budget, sections, offline, years=3):
    lines = ["# 研究方向推荐", ""]
    lines.append("生成时间：%s" % time.strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    if offline:
        lines.append("> 离线模式：未做联网检索，热度/复用/数据三列需人工补。")
        lines.append("")
    starved = [r for r in rows if r.get("insufficient")]
    if starved:
        lines.append("> **警告**：有 %d 个方向在近 %d 年内检索到 0 篇论文，已标记为「数据不足」。"
                     % (len(starved), years))
        lines.append("> 常见原因是检索式太长太窄、或网络不通导致各源全部失败。")
        lines.append("> 先跑 `python ra.py doctor` 确认数据源可用，再放宽 `--years` 或改短检索式。")
        lines.append("")
    lines.append("每周可投入：约 %.0f 小时" % budget)
    lines.append("")
    lines.append("## 候选方向评分表")
    lines.append("")
    lines.append("| 等级 | 方向 | 复合技能 | 复用度 | 热度 | 数据可获得性 | 预计工时 | 综合分 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        if r.get("insufficient"):
            cells = ["—"] * 5 + ["待重跑", "—"]
        elif offline:
            cells = ["待补", "待补", "待补", "待估", "—"]
        else:
            cells = ["%.2f" % r["reuse"], "%.2f" % r["heat"], "%.2f" % r["data"],
                     "%.0f h" % r["hours"], "%.1f" % r["score"]]
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["grade"], r["query"], "%s × %s" % (r["skill"], r["domain"]),
            cells[0], cells[1], cells[2], cells[3], cells[4]))
    lines.append("")
    lines.append("## 证据（每个方向的代表性近期论文）")
    lines.append("")
    for r in rows:
        lines.append("**%s** —— %s" % (r["grade"], r["query"]))
        lines.append("")
        if r["evidence"]:
            for e in r["evidence"]:
                lines.append("- %s" % e)
        else:
            lines.append("- 待补（离线模式未检索）")
        lines.append("")
    lines.append("## 方向 -> 首个可行动作")
    lines.append("")
    for r in rows:
        lines.append("### %s · %s" % (r["grade"], r["query"]))
        lines.append("")
        lines.append("- 第一周：用 `ra_search.py \"%s\"` 跑 20 篇，建对比矩阵" % r["query"])
        lines.append("- 第二周：确认数据集是否拿得到，拿不到就换方向")
        lines.append("- 第三周：写出一条可证伪的 claim，跑最小实验")
        lines.append("- **终止条件**：连续两周拿不到数据、或最强竞品已覆盖该 claim，立即止损换方向")
        lines.append("")
    lines.append("## 打分口径")
    lines.append("")
    lines.append("`综合分 = 复用度×35% + 热度×25% + 数据可获得性×20% − 时间成本×20%`")
    lines.append("")
    lines.append("- **复用度**：该方向的近期论文里，出现了你多少项其它技能——复用越多越省事")
    lines.append("- **热度**：近 %d 年论文数，太多说明红海，太少可能没社区" % years)
    lines.append("- **数据可获得性**：摘要中出现公开数据集/开源代码的密度")
    lines.append("- **时间成本**：按复用度反推的预计工时，每周 %.0f 小时下折算" % budget)
    lines.append("")
    return "\n".join(lines)


def show(sections, path):
    print("画像：%s" % path)
    print("")
    for name, items in sections.items():
        print("## %s (%d 条)" % (name, len(items)))
        for it in items:
            print("  - %s" % it)
        print("")


def main(argv=None):
    net._ensure_utf8()
    args = build_parser().parse_args(argv)

    profile_path = os.path.abspath(args.profile or DEFAULT_PROFILE)
    sections = parse_profile(profile_path)
    if not sections:
        print("[错误] 画像文件不存在或为空: %s" % profile_path)
        print("提示：包内 profile/researcher-profile.md 是模板，先填再用。")
        return 2

    if args.show or not args.recommend:
        show(sections, profile_path)
        if not args.recommend:
            return 0

    rows, budget, combos = recommend(sections, args)
    print("")
    print("推荐 %d 个方向（封顶 5，A 级 ≤2），每周可投入约 %.0f 小时"
          % (len(rows), budget))

    md = render(rows, budget, sections, args.offline, args.years)
    out = args.out or os.path.join(SKILL_ROOT, "output", "directions.md")
    net.write_text(out, md)
    print("报告: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
