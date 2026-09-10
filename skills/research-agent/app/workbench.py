#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""科研工作台扩展模块（零依赖）。

承载四个新增能力，数据沿用 JSON 原子写 + 线程锁模式：

1. AI 写作助手   data/writing.json  大纲/初稿/润色/投稿信/审稿回复 + 草稿库
2. 文献综述初稿 data/reviews.json   基于检索结果或粘贴清单 → 综述初稿 + 存档
3. 实验统计分析  纯 Python 描述统计 + Welch t 检验（t/df/Cohen's d/近似 p）
4. 长期记忆       data/memory.json  知识/经验/约定条目，问答时注入为上下文

纪律：不编造文献与数据——AI 输出要求用【待补：…】占位，统计只给算出来的数。
"""

import difflib
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
import re
import threading
import time

import model_bridge as model  # 复用模型层（无循环依赖）
import auth  # 用户校验（合作邀请要确认收件人存在且启用）
import team_store  # 接受邀请后自动把双方加进「协作中心」成员

_LOCK = threading.Lock()
_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))

_EMPTY = {
    "writing": {"docs": []},
    "reviews": {"reviews": []},
    "memory": {"entries": []},
    "mypapers": {"papers": []},
    "invites": {"invites": []},
    "guide": {"users": {}},
    "reffolders": {"folders": []},
    "reviewflow": {"reviews": []},
    "workflow": {"users": {}},
    "custom_skills": {"skills": []},
    "format_templates": {"templates": {}},
}


# ---------------------------------------------------------------- IO（同 team_store 模式）
def _file(name):
    os.makedirs(_DATA_DIR, exist_ok=True)
    return os.path.join(_DATA_DIR, name + ".json")


def _load(name):
    p = _file(name)
    if not os.path.isfile(p):
        return json.loads(json.dumps(_EMPTY.get(name, {})))
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(_EMPTY.get(name, {})))


def _save(name, obj):
    p = _file(name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    for _try in range(5):
        try:
            os.replace(tmp, p)
            break
        except PermissionError:
            if _try == 4:
                # 兜底：极少数情况下目标被系统瞬时锁死，退化为直接写入
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=1)
                return
            time.sleep(0.15)


def _mutate(name, fn):
    with _LOCK:
        obj = _load(name)
        new = fn(obj)
        if new is None:
            return obj
        _save(name, new)
        return new


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _uid(prefix, items):
    used = {it.get("id", "") for it in items}
    n = 1
    while "%s%03d" % (prefix, n) in used:
        n += 1
    return "%s%03d" % (prefix, n)


# ================================================================ 1. 写作助手
WRITE_KINDS = {"outline": "论文大纲", "draft": "章节初稿", "polish": "学术润色",
               "cover": "投稿信 Cover Letter", "response": "审稿回复",
               "algo": "算法代码", "trainplan": "训练方案", "paper": "论文初稿",
               "check": "算法校验报告", "dup": "降重改写"}

_PROMPTS = {
    "outline": (
        "你是一名资深学术写作教练。请基于用户给的主题与要点，输出一份可直接执行的论文大纲（Markdown）。\n"
        "要求：1) 结构符合目标论文类型（方法型/实证型/综述型由主题判断，不确定时询问或按实证型给）；"
        "2) 每一节写清：目的、核心论点/要回答的问题、需要哪些素材（数据/图/表/引用）；"
        "3) 没有真实数据与文献处一律写【待补：…】占位，绝不编造实验结果、数据或参考文献；"
        "4) 末尾给一栏「写作顺序建议」（先写哪节再写哪节，为什么）。只输出 Markdown。"),
    "draft": (
        "你是一名科研写作助手。请根据用户提供的章节/主题与要点撰写**初稿**（Markdown）。\n"
        "要求：1) 逻辑连贯、按学术写作规范行文（先铺垫→问题→做法→预期/结果→小结）；"
        "2) 凡是需要用户真实数据、图表、实验细节、具体引用文献的地方，一律用【待补：…】明确占位，"
        "绝不编造实验数字、仿真结果或文献条目；3) 术语保持统一（以用户提供为准）；"
        "4) 控制在用户指定篇幅内（默认 600–900 字），只输出正文。"),
    "polish": (
        "你是一名学术英文/中文润色专家。请对用户粘贴的段落做**学术润色**。\n"
        "要求：1) 保留全部原意与信息，不增删论点，不添加数据或引用；2) 去除口语化、冗余、重复表达，"
        "统一术语；3) 若原文是中文，默认输出中文润色版；4) 先输出「润色后」，再输出「改动要点」列表（中文），"
        "说明改了什么、为什么。只输出 Markdown。"),
    "cover": (
        "你是论文作者的投稿助理。请基于用户提供的论文信息撰写一封投稿 Cover Letter（英文，Markdown）。\n"
        "结构：给编辑的称呼 → 首段：标题+为何投稿该刊（结合用户目标期刊，不确定则不点刊名写『the journal』）"
        "→ 第二段：主要贡献（用用户给的创新点，未给则用【待补：…】）→ 第三段：原创性与无利益冲突声明 → 结尾。\n"
        "要求：绝不编造编辑姓名、投稿系统状态或审稿周期承诺；用户没给的信息一律【待补：…】。只输出 Markdown。"),
    "response": (
        "你是论文作者处理审稿意见的助手。请把用户粘贴的审稿意见整理成**逐条回复**（Markdown）。\n"
        "每条回复：引用原意见 → 你的修改动作（已做/计划做，用【已修改：…】或【计划：…】标注，不夸大）→ "
        "修改后所在位置（节/图/表，未知写【待补：…】）。要求：语气专业谦和，不编造已完成的修改或实验。"
        "只输出 Markdown。"),
}


def gen_doc(kind, topic, points, extra="", max_tokens=1500):
    """调用模型生成写作内容；模型不可用时 outline 给通用骨架，其余给引导。"""
    kind = kind or "outline"
    if kind not in _PROMPTS:
        return {"ok": False, "error": "未知写作类型 %s" % kind}
    if not model.status()["ok"]:
        if kind == "outline":
            sk = ["# 论文大纲（模板 · 需人工填充）", "",
                  "> 当前未配置模型，这是通用骨架，配置模型后可一键生成个性化大纲。", ""]
            sk += ["## 1 引言",
                   "- 研究背景与动机（【待补：两段背景 + 一个痛点】）",
                   "- 现有方法不足（【待补：2–3 条，每条需配真实文献引用】）",
                   "- 本文贡献（【待补：结合用户创新点 N1–N5】）",
                   "- 组织结构（一句话带过）",
                   "",
                   "## 2 相关工作",
                   "- 与本文最相关的 2–3 条线，各 1 段（【待补：真实文献，需核实】）",
                   "",
                   "## 3 方法",
                   "- 问题定义与符号（【待补】）",
                   "- 整体框架图（【待补：示意图】）",
                   "- 各模块：动机→设计→伪代码/公式（【待补】）",
                   "",
                   "## 4 实验",
                   "- 数据集与评估指标（【待补】）",
                   "- 对比方法（【待补：与本文方法可比的对象】）",
                   "- 结果与分析：主表/消融/鲁棒性/可视化（【待补：真实数字，禁止编造】）",
                   "",
                   "## 5 结论与展望",
                   "- 总结贡献 + 局限 + 下一步（【待补】）"]
            return {"ok": True, "offline": True,
                    "reply": "\n".join(sk),
                    "hint": "未配置模型，给出通用骨架；配置模型后可生成贴合主题与要点的个性化大纲。"}
        return {"ok": False, "ai": False,
                "reason": model.status().get("reason", "模型不可用") +
                          "。去「设置 → 模型设置」选预设并填 API Key。"}
    topic = (topic or "").strip()
    body = []
    if topic:
        body.append("## 主题 / 章节\n%s" % topic)
    if points and points.strip():
        body.append("## 用户要点\n%s" % points)
    if extra and extra.strip():
        body.append("## 素材 / 原文（润色、审稿回复用）\n%s" % extra)
    if not body:
        body.append("（用户未提供具体内容，请先给 2–3 个引导性问题再输出，不要硬编。）")
    try:
        reply = model.quick_ask("\n\n".join(body),
                                system=_PROMPTS[kind], max_tokens=max_tokens, temperature=0.4)
        return {"ok": True, "reply": reply, "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def docs_save(kind, title, content, by=""):
    title = (title or "").strip() or ("%s %s" % (WRITE_KINDS.get(kind, "草稿"), _now()[:16]))
    content = (content or "").strip()
    if len(content) < 20:
        return {"ok": False, "error": "内容太短，暂不存档"}
    def fn(obj):
        obj.setdefault("docs", []).append({
            "id": _uid("w", obj.get("docs", [])), "kind": kind or "draft",
            "kind_zh": WRITE_KINDS.get(kind, "草稿"), "title": title,
            "content": content, "ts": _now(), "by": by or ""})
        return obj
    _mutate("writing", fn)
    return {"ok": True, "docs": docs_list()}


def docs_list():
    docs = _load("writing").get("docs", [])
    docs.sort(key=lambda d: d.get("ts", ""), reverse=True)
    return docs


def docs_remove(did):
    def fn(obj):
        obj["docs"] = [d for d in obj.get("docs", []) if d.get("id") != did]
        return obj
    _mutate("writing", fn)
    return {"ok": True, "docs": docs_list()}


# ================================================================ 2. 文献综述
REV_ANGLES = {
    "timeline": "发展脉络（按时间推进的综述）",
    "methods": "方法对比（按方法/技术路线组织）",
    "app": "应用与场景（按下游任务/场景组织）",
    "gap": "研究空白（面向找 gap 的批判性综述）",
}


def _paper_lines(records, maxn=30):
    """把检索记录压成给模型的清单。"""
    lines = []
    for r in (records or [])[:maxn]:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        year = r.get("year") or ""
        venue = (r.get("venue") or r.get("source") or "")
        abs_ = (r.get("abstract") or r.get("summary") or "").replace("\n", " ")[:420]
        lines.append("- [%s] %s (%s%s)%s" % (
            year, title, venue, ", cited %s" % r.get("citations") if r.get("citations") else "",
            ("｜" + abs_) if abs_ else ""))
    return lines


def gen_review(topic, angle, records=None, extra="", max_tokens=1800):
    """综述初稿：模型可用 → 真生成；不可用 → 离线骨架（脉络+方法计数），诚实标注。"""
    topic = (topic or "").strip()
    lines = _paper_lines(records or [])
    if not topic and not lines:
        return {"ok": False, "error": "需要综述主题，或先用「文献检索」拿到一批结果。"}
    angle_key = angle if angle in REV_ANGLES else "gap"
    angle_desc = REV_ANGLES.get(angle_key, REV_ANGLES["gap"])

    if not model.status()["ok"]:
        md = ["# 综述骨架：%s" % (topic or "（未给定主题，基于检索结果）"), "",
              "> 未配置模型，以下为**离线骨架**（按年份与来源统计），配置模型后可生成有行文的综述初稿。", ""]
        if lines:
            years = sorted({str(r.get("year", "")) for r in (records or []) if r.get("year")})
            md += ["**时间跨度**：%s（%d 篇）" % (" – ".join([y for y in years if y]) or "—", len(records or [])), ""]
            md += ["**按时间脉络速览**：", ""]
            for r in sorted((records or [])[:20], key=lambda x: x.get("year") or 0):
                md.append("- %s｜%s（%s）" % (r.get("year", "—"), (r.get("title") or "").strip()[:90],
                                             (r.get("venue") or r.get("source") or "")[:40]))
        else:
            md += ["（暂无检索记录，请先去「文献检索」跑一次，或在下方粘贴文献清单）"]
        md += ["", "**下一步（配置模型后）**：选择综述角度【%s】，AI 会基于以上清单生成有组织的行文。" % angle_desc]
        return {"ok": True, "offline": True, "reply": "\n".join(md),
                "hint": "未配置模型，仅生成骨架；配置模型后可用真模型写综述初稿。"}
    if not lines:
        return {"ok": False, "error": "没有可综述的文献记录——请先「文献检索」，或把文献 JSON 粘贴到下方素材框。"}
    sys_p = (
        "你是严谨的科研综述作者。基于给定的文献清单与综述角度，输出**综述初稿**（Markdown，中文）。\n"
        "要求：1) 只依据清单里真实出现的信息写作，主题统一、有逻辑组织（按角度 %s）；"
        "2) 不编造清单里没有的论文、作者名、数字或结论；每篇只描述你能从清单看到的要点；"
        "3) 结构：## 引言（范围与方法）→ 按角度的主体章节 → ## 趋势与空白（诚实，只讲证据支持的）→ ## 待补问题清单；"
        "4) 标注不足：缺少的关键对比、需要回原文精读的条目用【待补：…】。" % angle_desc)
    try:
        reply = model.quick_ask(
            "综述主题：%s\n请按角度「%s」撰写综述初稿。\n\n文献清单如下：\n%s\n%s" % (
                topic or "（未指定，从清单归纳）", angle_desc, "\n".join(lines), extra or ""),
            system=sys_p, max_tokens=max_tokens, temperature=0.35)
        return {"ok": True, "reply": reply, "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def rev_save(title, angle, content, by=""):
    content = (content or "").strip()
    if len(content) < 30:
        return {"ok": False, "error": "内容为空，无法存档"}
    def fn(obj):
        obj.setdefault("reviews", []).append({
            "id": _uid("r", obj.get("reviews", [])), "title": (title or "").strip() or _now()[:16],
            "angle": REV_ANGLES.get(angle, angle), "content": content,
            "ts": _now(), "by": by or ""})
        return obj
    _mutate("reviews", fn)
    return {"ok": True, "reviews": rev_list()}


def rev_list():
    rs = _load("reviews").get("reviews", [])
    rs.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rs


def rev_remove(rid):
    def fn(obj):
        obj["reviews"] = [r for r in obj.get("reviews", []) if r.get("id") != rid]
        return obj
    _mutate("reviews", fn)
    return {"ok": True, "reviews": rev_list()}


# ================================================================ 3. 实验统计分析
def _parse_groups(text):
    """每行一组：'组名: v1, v2, v3…'；无组名冒号的整块按数值解析。"""
    groups = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(.{1,24}?)[:：]\s*(.*)$", line)
        if m:
            name, vals = m.group(1).strip(), m.group(2)
        else:
            name, vals = "组%d" % (len(groups) + 1), line
        nums = []
        for tk in re.split(r"[,，\s;；]+", vals):
            tk = tk.strip().rstrip("%")
            try:
                nums.append(float(tk))
            except Exception:
                continue
        if nums:
            groups.append({"name": name, "values": nums})
    return groups


def _mean(xs):
    return sum(xs) / len(xs)


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _std(xs, ddof=1):
    if len(xs) - ddof <= 0:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - ddof))


def _betacf(a, b, x, itermax=200, eps=3e-12):
    """正则化不完全 beta 的连分数（Numerical Recipes betacf）。"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itermax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        del_ = d * c
        h *= del_
        if abs(del_ - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
          + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _welch_p(t, df):
    """双尾 Welch t 检验 p 值：p = I_{df/(df+t^2)}(df/2, 1/2)。"""
    if df <= 0 or not math.isfinite(t):
        return None
    x = df / (df + t * t)
    return max(0.0, min(1.0, _betai(df / 2.0, 0.5, x)))


def _fmt(x, nd=4):
    return "%.*g" % (nd, x) if x is not None and math.isfinite(x) else "—"


def stat_groups(text):
    """解析多组数值 → 描述统计；两组时给 Welch t 检验。"""
    groups = _parse_groups(text or "")
    if len(groups) < 1:
        return {"ok": False, "error": "没解析到数值。格式示例：\n组A: 0.71, 0.68, 0.80, 0.75\n组B: 0.60, 0.55, 0.66, 0.58"}
    rows = []
    for g in groups:
        vs = g["values"]
        rows.append({
            "name": g["name"], "n": len(vs), "mean": _mean(vs),
            "std": _std(vs), "median": _median(vs),
            "min": min(vs), "max": max(vs)})
    md = ["# 实验统计对比", "",
          "| 组 | n | 均值 | 标准差 | 中位数 | 最小值 | 最大值 |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append("| %s | %d | %s | %s | %s | %s | %s |" % (
            r["name"], r["n"], _fmt(r["mean"]), _fmt(r["std"]), _fmt(r["median"]),
            _fmt(r["min"]), _fmt(r["max"])))
    md.append("")
    result = {"ok": True, "groups": rows, "markdown": ""}
    if len(rows) >= 2:
        a, b = rows[0], rows[1]
        if a["n"] > 1 and b["n"] > 1:
            sa, sb = a["std"], b["std"]
            na, nb = a["n"], b["n"]
            var_a, var_b = sa * sa, sb * sb
            se = math.sqrt(var_a / na + var_b / nb)
            t = (a["mean"] - b["mean"]) / se if se > 0 else None
            df = None
            if se > 0 and (var_a + var_b) > 0:
                den = (var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1)
                df = den if den > 0 else None
                if df:
                    df = (var_a / na + var_b / nb) ** 2 / den
            pooled = math.sqrt(((na - 1) * var_a + (nb - 1) * var_b) / max(1, na + nb - 2))
            d = (a["mean"] - b["mean"]) / pooled if pooled > 0 else None
            p = _welch_p(t, df) if (t is not None and df) else None
            cmp = {"a": a["name"], "b": b["name"], "diff": (a["mean"] - b["mean"]) if t is not None else None,
                   "t": t, "df": df, "cohens_d": d, "p": p}
            sig = ""
            if p is not None:
                sig = "p≈%.4g → %s" % (p, "差异显著(p<0.05)" if p < 0.05 else "未达 0.05 显著")
                if p < 0.01:
                    sig = "p≈%.4g → 差异高度显著(p<0.01)" % p
            md += ["**%s vs %s**：均值差=%s，Cohen's d=%s，Welch t=%s（df≈%s），双尾 %s。" % (
                a["name"], b["name"], _fmt(cmp["diff"]), _fmt(d, 3),
                _fmt(t, 3), _fmt(df, 2), sig or "p 无法计算（样本过小）")]
            md += ["> 说明：p 由 Welch t 检验近似给出（双尾）；样本量小(n<10)时仅供参考，"
                   "正式论文请用专业统计工具复核；显著性 ≠ 实际重要性。"]
            result["compare"] = cmp
    else:
        md += ["> 只有一组数据：仅给出描述统计。要对比请至少给两组，例如「组A: … / 组B: …」。"]
    result["markdown"] = "\n".join(md)
    return result


# ================================================================ 8. 算法生成 / 校验 / 训练设计
import ast  # 零依赖静态校验

ALGO_KINDS = {
    "train_script": (
        "训练脚本骨架",
        "You are a senior ML engineer. Based on the user's description, generate a **runnable PyTorch training-script "
        "skeleton** (Python). Requirements: 1) clear structure: config(dataclass/argparse) -> dataset/dataloader -> model "
        "definition -> loss/optimizer/scheduler -> train loop (with epochs, logging) -> evaluation -> checkpoint saving; "
        "2) fill in reasonable defaults ONLY where the user gave enough info; anything the user did not specify must be "
        "marked with a `# TODO(待补): ...` comment — never invent dataset statistics or claimed results; 3) concise "
        "comments in Chinese explaining each block. Output a single fenced ```python block, no extra prose."),
    "pseudocode": (
        "算法伪代码",
        "You are an academic writing expert. Based on the user's description of a method, produce **paper-style pseudocode** "
        "(Algorithm environment style, plain text with line numbers) plus a short variable table. Only use information given "
        "by the user; unknown details marked 【待补】. Output Markdown."),
    "train_plan": (
        "自动化训练方案",
        "You are an auto-ML experiment designer. Based on the given paper text / method description, output a **complete "
        "automatic training plan** (Markdown, Chinese): ## 实验目标 → ## 数据准备（数据集/划分/预处理，未指明处【待补】）→ "
        "## 模型与超参（表格：名称/取值/来源依据）→ ## 训练流程（epoch/lr/early-stop 等）→ ## 评估协议（指标/对比方法/随机种子）→ "
        "## 预期产出与图表 → ## 训练脚本骨架（单个 ```python 块，可运行结构，未知处 # TODO(待补) 注释）。"
        "不得编造论文里没有的实验数值；引用论文中的设置时注明出自原文。"),
}


def algo_gen(kind, desc, max_tokens=2000):
    kind = kind or "train_script"
    if kind not in ALGO_KINDS:
        return {"ok": False, "error": "未知生成类型 %s" % kind}
    if not (desc or "").strip() or len(desc.strip()) < 20:
        return {"ok": False, "error": "请先描述方法/数据/指标（至少 20 字）"}
    if not model.status()["ok"]:
        return {"ok": False, "ai": False,
                "error": "算法生成需要模型。去「设置 → 模型设置」选预设（默认腾讯混元）填 API Key。"}
    label, sys_p = ALGO_KINDS[kind]
    try:
        reply = model.quick_ask(desc.strip(), system=sys_p, max_tokens=max_tokens, temperature=0.3)
        return {"ok": True, "reply": reply, "kind": kind, "kind_zh": label, "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def algo_check(code, ai_review=False):
    """算法代码校验：静态检查（零依赖 ast）+ 可选 AI 复审。"""
    code = (code or "").strip()
    if len(code) < 20:
        return {"ok": False, "error": "请粘贴要校验的代码（至少 20 字符）"}
    findings, level = [], "pass"
    syntax_ok = True
    try:
        tree = ast.parse(code)
        src = code
        has = lambda kw: kw in src  # 轻量关键词探测
        if not (has("import") or has("from ")):
            findings.append("未发现任何 import 语句——脚本可能缺少依赖声明")
        if has("for ") and (has("epoch") or has("range(")):
            findings.append("✓ 检测到训练循环（for / epoch）")
        else:
            findings.append("未检测到训练循环（for epoch ...）——若这是训练脚本需确认")
        if not (has("loss") or has("criterion")):
            findings.append("未检测到损失定义（loss/criterion）——训练脚本通常必需")
        if not (has("optimizer") or has("SGD") or has("Adam")):
            findings.append("未检测到优化器（optimizer/Adam/SGD）")
        if has("save") or has("torch.save") or has("joblib.dump"):
            findings.append("✓ 检测到模型/结果保存逻辑")
        else:
            findings.append("未检测到 checkpoint 保存——建议训练完成后保存模型")
        if has("eval(") or has("test") or has("f1") or has("accuracy") or has("auc"):
            findings.append("✓ 检测到评估相关代码")
        # 常见风险
        if re.search(r"device\s*=\s*[\"']cuda[\"']", src) and "cuda.is_available" not in src:
            findings.append("⚠ 硬编码 cuda：建议 `device = 'cuda' if torch.cuda.is_available() else 'cpu'`")
        if re.search(r"random_state\s*=\s*None", src):
            findings.append("⚠ 随机种子未固定（random_state=None）：复现实验请固定 seed")
    except SyntaxError as e:
        syntax_ok = False
        level = "fail"
        findings.append("语法错误：%s（第 %s 行）" % (e.msg, e.lineno))
    md = ["# 算法静态校验报告", "",
          "- 语法：**%s**" % ("通过" if syntax_ok else "未通过"),
          "- 结构检查：", ""]
    md += ["- " + f for f in findings] or ["- 未发现问题"]
    md += ["", "> 静态检查只覆盖结构与常见风险，不能代替真实运行。"]
    result = {"ok": True, "syntax_ok": syntax_ok, "level": level, "markdown": "\n".join(md)}
    if ai_review:
        if not model.status()["ok"]:
            result["markdown"] += "\n\n> （AI 复审跳过：未配置模型）"
        else:
            try:
                review = model.quick_ask(
                    "请审查以下实验代码，只指出：正确性风险、实验方法学问题（数据泄漏/种子/评估不当）、"
                    "可复现性缺陷；不要改写整个代码，不要编造库的 API。输出 Markdown 要点。\n\n```python\n" + code[:8000] + "\n```",
                    system="你是严格的 ML 代码审稿人：只基于给定代码与常识审查，不确定的 API 行为要标注【需核实】。",
                    max_tokens=800, temperature=0.2)
                result["markdown"] += "\n\n## AI 复审\n" + review
                result["ai_review"] = True
            except Exception as e:
                result["markdown"] += "\n\n> （AI 复审失败：%s）" % e
    return result


# ================================================================ 4. 长期记忆
MEM_KINDS = {"knowledge": "知识", "experience": "经验", "rule": "约定", "note": "备忘"}


def mem_add(text, kind="note", by=""):
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "记忆内容不能为空"}
    if len(text) > 600:
        return {"ok": False, "error": "单条记忆请控制在 600 字内"}
    kind = kind if kind in MEM_KINDS else "note"
    def fn(obj):
        obj.setdefault("entries", []).append({
            "id": _uid("m", obj.get("entries", [])),
            "kind": kind, "kind_zh": MEM_KINDS[kind], "text": text,
            "ts": _now(), "by": by or ""})
        # 每人只留最近 60 条，防止无限膨胀
        mine = [e for e in obj["entries"] if e.get("by") == (by or "")]
        if len(mine) > 60:
            drop = mine[:len(mine) - 60]
            keep = {e["id"] for e in drop}
            obj["entries"] = [e for e in obj["entries"] if e["id"] not in keep]
        return obj
    _mutate("memory", fn)
    return {"ok": True, "entries": mem_list(by=by)}


def mem_list(by=None):
    es = _load("memory").get("entries", [])
    if by:
        es = [e for e in es if e.get("by") == by]
    es.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return es


def mem_remove(mid, by=None):
    def fn(obj):
        es = obj.get("entries", [])
        obj["entries"] = [e for e in es if not (e.get("id") == mid and (by is None or e.get("by") == by))]
        return obj
    _mutate("memory", fn)
    return {"ok": True, "entries": mem_list(by=by)}


def mem_context(by, limit=8):
    """取某用户最近的记忆，拼成给模型的上下文片段。"""
    es = mem_list(by=by)[:limit]
    if not es:
        return ""
    seg = ["## 该用户的长期记忆（近期，按需参考，如有冲突以最新为准）"]
    for e in reversed(es):  # 旧的在前，新的在后
        seg.append("- [%s｜%s] %s" % (e.get("ts", ""), e.get("kind_zh", "备忘"),
                                      (e.get("text") or "").replace("\n", " ")))
    return "\n".join(seg)


# ================================================================ 5. 我的论文（个人台账）
MP_STATUS = {"planning": "规划中", "writing": "撰写中", "submitted": "已投稿",
             "review": "评审中", "revision": "修回中", "accepted": "已录用", "rejected": "已拒"}
MP_FIELDS = ("title", "ptype", "target_journal", "deadline", "notes", "status")


MP_CAPS = {"title": 200, "ptype": 20, "target_journal": 120, "deadline": 20, "notes": 4000, "status": 20}


def mp_add(owner, data):
    title = (data.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "论文标题不能为空"}
    def fn(obj):
        p = {k: (data.get(k) or "").strip()[:MP_CAPS.get(k, 200)] for k in MP_FIELDS}
        p["status"] = p["status"] if p["status"] in MP_STATUS else "writing"
        p["ptype"] = p["ptype"] or "期刊论文"
        p["id"] = _uid("p", obj.get("papers", []))
        p["owner"] = owner or ""
        p["ts"] = _now()
        obj.setdefault("papers", []).append(p)
        return obj
    _mutate("mypapers", fn)
    return {"ok": True, "papers": mp_list(owner)}


def mp_list(owner):
    ps = [p for p in _load("mypapers").get("papers", []) if p.get("owner") == owner]
    ps.sort(key=lambda p: p.get("ts", ""), reverse=True)
    return ps


def mp_update(mid, owner, patch):
    def fn(obj):
        for p in obj.get("papers", []):
            if p.get("id") == mid and p.get("owner") == owner:
                for k in MP_FIELDS:
                    if patch.get(k) is not None:
                        v = str(patch[k]).strip()[:MP_CAPS.get(k, 200)]
                        if v or k in ("deadline", "notes", "target_journal"):
                            p[k] = v
                if p.get("status") not in MP_STATUS:
                    p["status"] = "writing"
                p["updated"] = _now()
                return obj
        raise ValueError("论文不存在或无权修改")
    try:
        _mutate("mypapers", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "papers": mp_list(owner)}


def mp_remove(mid, owner):
    def fn(obj):
        obj["papers"] = [p for p in obj.get("papers", [])
                         if not (p.get("id") == mid and p.get("owner") == owner)]
        return obj
    _mutate("mypapers", fn)
    return {"ok": True, "papers": mp_list(owner)}


# ================================================================ 6. 合作对接（作者邀请）
def _user_exists(name):
    for u in auth.list_users():
        if u.get("name") == name:
            return bool(u.get("active", True))
    return False


def _user_display(name):
    for u in auth.list_users():
        if u.get("name") == name:
            return u.get("display") or name
    return name


def inv_send(frm, to, message="", paper=""):
    to = (to or "").strip()
    if not frm:
        return {"ok": False, "error": "未登录"}
    if to == frm:
        return {"ok": False, "error": "不能邀请自己"}
    if not _user_exists(to):
        return {"ok": False, "error": "用户 %s 不存在或已停用" % to}
    def fn(obj):
        invs = obj.get("invites", [])
        for v in invs:
            if v.get("status") == "pending" and {v.get("from"), v.get("to")} == {frm, to}:
                raise ValueError("已有一条待处理的合作邀请，等待对方处理即可")
        invs.append({"id": _uid("i", invs), "from": frm, "to": to,
                     "paper": (paper or "").strip(), "message": (message or "").strip()[:300],
                     "status": "pending", "ts": _now(), "responded": ""})
        return obj
    try:
        _mutate("invites", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **inv_lists(frm)}


def inv_lists(user):
    invs = _load("invites").get("invites", [])
    inbox = [v for v in invs if v.get("to") == user and v.get("status") != "cancelled"]
    sent = [v for v in invs if v.get("from") == user and v.get("status") != "cancelled"]
    inbox.sort(key=lambda v: v.get("ts", ""), reverse=True)
    sent.sort(key=lambda v: v.get("ts", ""), reverse=True)
    return {"inbox": inbox, "sent": sent, "partners": partners(user)}


def _ensure_members(*names):
    """把双方补进「协作中心」成员列表（已存在则跳过）。"""
    try:
        have = {m.get("name") for m in team_store.list_members()}
        for name in names:
            if name and name not in have:
                team_store.add_member({"name": name, "role": "合作者"})
    except Exception:
        pass  # 成员列表写入失败不影响邀请本身


def inv_respond(iid, user, accept):
    def fn(obj):
        for v in obj.get("invites", []):
            if v.get("id") == iid and v.get("to") == user:
                if v.get("status") != "pending":
                    raise ValueError("该邀请已处理（%s）" % v.get("status"))
                v["status"] = "accepted" if accept else "declined"
                v["responded"] = _now()
                return obj
        raise ValueError("邀请不存在或不是发给你的")
    try:
        _mutate("invites", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if accept:
        other = None
        for v in _load("invites").get("invites", []):
            if v.get("id") == iid:
                other = v.get("from")
                break
        _ensure_members(user, other)
    return {"ok": True, **inv_lists(user)}


def inv_cancel(iid, user):
    def fn(obj):
        for v in obj.get("invites", []):
            if v.get("id") == iid and v.get("from") == user:
                if v.get("status") != "pending":
                    raise ValueError("只有待处理的邀请可以取消")
                v["status"] = "cancelled"
                return obj
        raise ValueError("邀请不存在或不是你发出的")
    try:
        _mutate("invites", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **inv_lists(user)}


def partners(user):
    """所有与 user 互为"已接受"的合作者。"""
    invs = _load("invites").get("invites", [])
    out = []
    seen = set()
    for v in invs:
        if v.get("status") != "accepted":
            continue
        other = v.get("to") if v.get("from") == user else (v.get("from") if v.get("to") == user else None)
        if other and other not in seen:
            seen.add(other)
            out.append({"name": other, "display": _user_display(other), "since": v.get("responded", "")})
    return out


# ================================================================ 7. 论文引导进度
def guide_load(by):
    """返回某用户的步骤完成表 {"s1": ts, ...}。"""
    g = _load("guide").get("users", {})
    return g.get(by or "", {})


def guide_save(by, steps):
    clean = {str(k)[:8]: str(v)[:20] for k, v in (steps or {}).items() if k and v}
    def fn(obj):
        obj.setdefault("users", {})[by or ""] = clean
        return obj
    _mutate("guide", fn)
    return {"steps": clean}


# ================================================================ 9. 技能注册表（源自"论文辅助全流程"方法论）
SKILLS = {
    "socratic_idea": {
        "name": "苏格拉底式 Idea 打磨",
        "desc": "像审稿人一样反复追问：最小命题、创新点取舍、操作性定义、Gap 归类。一个 idea 至少扛过七八轮追问。",
        "tab": "profile",
        "prompt": ("你是苛刻但建设性的审稿人，对用户的研究想法做苏格拉底式追问（中文）。第一轮提 5-6 个问题，覆盖："
                   "①最小命题——你最想让审稿人相信的一句可检验的话是什么；②创新点取舍——若只能保留一个创新点，留哪个、为什么；"
                   "③操作性定义——核心概念在论文里到底指什么、怎么度量；④Gap 归类——你声称的缺口是工具缺口、证据缺口还是理解缺口；"
                   "⑤核心瓶颈——这个流程最脆弱的一环。问题要犀利具体，针对用户给的材料；不要给答案，只追问。")},
    "lit_strategy": {
        "name": "文献调研策略（反编造）",
        "desc": "先反问检索边界再出策略：结合你的方向给检索式组合（快速调研 + 细分领域调研），文献只从真实检索入口进入。",
        "tab": "search",
        "prompt": ("你是文献调研策略师（中文）。用户给你研究方向；你先列出 3 个需要用户确认的检索边界问题（时间窗/子领域/排除项），"
                   "然后给出：①快速调研检索式 3 条（英文章节式，适配 arXiv/OpenAlex/Crossref）；②细分领域深挖检索式 3 条；"
                   "③建议的筛选标准（被引/年份/会议）。提醒：所有入选文献必须来自系统内真实检索结果，AI 不生成文献条目。")},
    "idea_pre_feed": {
        "name": "喂透 10-20 篇再构思",
        "desc": "构思前先让 AI 读透最相关的 10-20 篇（文献库批量精读），产出领域共识地图后再对话。",
        "tab": "reflib",
        "prompt": ("基于给定的一组论文精读分析，输出领域共识地图（中文 Markdown）：①主流方法谱系；②数据与评测惯例；"
                   "③公认的开放问题；④与本用户方向最相关的 10-20 篇排序及一句话理由。只依据给定分析，不引入新文献。")},
    "cs_writing": {
        "name": "CS/AI 分学科写作",
        "desc": "计算机/AI 方向学术写作：baseline 选择、实验设计表述、评估指标、LaTeX/引用格式规范。",
        "tab": "write",
        "prompt": ("你是 CS/AI 方向学术写作教练（中文说明+英文例句）。针对用户给出的段落/章节：①检查 baseline 与对比实验表述是否完整；"
                   "②评估指标与实验设置表述是否符合领域惯例（数据集划分、随机种子、消融）；③引用格式与声称强度是否匹配证据；"
                   "④给出修改后的英文表述。不编造引用与数据。")},
    "figure_redline": {
        "name": "科研绘图红线检查",
        "desc": "硬红线：AI 整图生成绝不能进论文（像素不可追溯=诚信问题）。检查你的图是否由真实数据/矢量工具产出。",
        "tab": "plot",
        "prompt": ("你是科研诚信审查员（中文）。用户描述其论文图的做法；你判定：①图中每个元素是否可追溯到真实数据或矢量工具；"
                   "②若使用了 AI 生成图，指出哪些元素必须用矢量工具重排；③给出合规的绘图流程建议（Python/matplotlib 矢量输出等）。")},
    "autoresearch": {
        "name": "自动实验调参参谋",
        "desc": "CS/AI 向：反复审视实验结果与超参，给出下一轮实验的建议（跑实验仍由你执行）。",
        "tab": "lab",
        "prompt": ("你是自动研究参谋（中文）。基于用户给的实验设置与结果，诊断：①当前结果说明什么/不说明什么；"
                   "②下一轮最值得改的 3 个超参或设置及理由；③需要补的对照/消融。不编造数值，不确定处标注【需核实】。")},
    "daily_radar": {
        "name": "每日论文雷达",
        "desc": "定向追踪某个方向的新论文（一键版；周期自动化可在 WorkBuddy 里建定时任务）。",
        "tab": "chat",
        "prompt": ("你是文献雷达（中文）。用户给一个方向；输出：①今日最值得关注的 8 篇论文清单（必须标注：以下条目为候选框架，"
                   "请用系统「文献检索」验证真实存在后再入库——AI 不直接生成可信条目）；②研究脉络总结；③可深化的 3 个方向。")},
    "paraphrase_dup": {
        "name": "降重 · 查重版",
        "desc": "中文学术降重：句式重构+同义替换+语态转换，确保无连续八字与原文相同；术语/引用/数据/公式原样保留。（融合 GitHub otisgodwin92 降重指令与 ECNU-ICALK/AutoSkill 反模式）",
        "tab": "mypaper",
        "prompt": ("你是中文学术论文降重专家（灵感来源：GitHub otisgodwin92 论文降重指令、ECNU-ICALK/AutoSkill academic_paraphrasing_expansion）。"
                   "对用户给出的中文学术文本做深度改写以降低查重率。核心规则："
                   "①改写后不得出现与原文连续八字相同的片段；②句式重构为主（主被动转换、拆分/合并长句、调整语序），同义替换为辅（如：采用→运用/选用、基于→立足于/在…基础上、通过→借助/凭借、提升→实现…的提高）；"
                   "③动词短语可适度扩展（管理→开展…的管理工作）、括号内解释性信息可用'也就是/具体而言'融入句子；④可使用把字句/被字句、适当增加连接词使表达更自然；"
                   "⑤硬性保真：专业术语、英文缩写、数字、单位、公式、引用标注（如[1]）一律原样保留，不增删任何事实信息；"
                   "⑥禁止：直接照抄原句、纯同义词交换而不改结构、使用第一人称、过度口语化、降低学术语域、缩写或概括原文（改写后字数应与原文相当）；"
                   "⑦只输出改写后的文本本身，不要任何解释、前言或对比说明。")},
    "paraphrase_aigc": {
        "name": "降重 · 降AI率版",
        "desc": "针对 AIGC 检测（知网等）：打破句式节奏/信息密度/术语位置/连接词/模板结构五大标记，术语主宾换位；事实数据引用严格保真。（灵感来源：GitHub cnki-aigc-skill）",
        "tab": "mypaper",
        "prompt": ("你是学术文本 AIGC 检测优化专家（灵感来源：GitHub cnki-aigc-skill 的五维改写框架）。"
                   "目标：降低文本被 AI 检测系统（知网 AIGC 等）标记的概率，同时严格保真。"
                   "针对五大检测标记逐条处理：①句式节奏——打散均匀的'主谓宾'节奏，长短句交错，个别句子用把字句/被字句或条件句改写；"
                   "②信息密度——把高度凝练的句子适度展开解释（一句话拆成'结论+解释'两步说），避免每句信息量完全均等；"
                   "③术语位置——把术语从主语位置移到宾语或话题位置（如'本框架实现监控'→'在监控层面，本框架…'）；"
                   "④连接词——替换高频模板连接词（此外→另外/与此同时/值得一提的是；因此→正因如此/由此），并允许部分句子省略连接词；"
                   "⑤模板结构——避免连续段落使用相同的展开模板，改用举例、补充说明、设问后自答等人类惯用展开方式。"
                   "硬性保真：事实、数字、公式、引用标注、专业术语原样保留，不增删观点，不缩写内容；避免过度口语化与第一人称；字数与原文相当。"
                   "输出末尾附一段'高风险特征修改清单'（列出你改动的 3-5 处及对应手法）。正文与清单之间用一行'---'分隔。")},
    "paraphrase_en": {
        "name": "降重 · 英文改写版",
        "desc": "英文 academic paraphrasing：0% plagiarism 导向，重构句式+高级词汇替换，字数不减、意义/引用/术语完整。（移植 GitHub ECNU-ICALK/AutoSkill academic_paraphrasing_expansion）",
        "tab": "mypaper",
        "prompt": ("You are an academic writing assistant specialized in paraphrasing to minimize plagiarism scores "
                   "(adapted from GitHub ECNU-ICALK/AutoSkill 'academic_paraphrasing_expansion'). "
                   "Rewrite the given English academic text: ①Completely restructure sentences and replace vocabulary with sophisticated alternatives; "
                   "②Do NOT copy any phrase or sentence directly from the input; ③Do not reduce the word count - output must be equal or longer; never summarize or condense; "
                   "④Preserve meaning, facts, citations ([n] markers), and technical terms exactly; ⑤Keep professional academic register - avoid rare/awkward word combinations that look artificially complex; "
                   "⑥Ensure smooth, natural, grammatically correct flow; ⑦Output ONLY the rewritten text, no explanations or comparisons. "
                   "If the input is Chinese, translate-and-rewrite it into academic English instead.")},
    "html_slides": {
        "name": "HTML 汇报幻灯",
        "desc": "把论文/报告转成 HTML 幻灯（组会汇报用；现代做法是 HTML 而非 PPT）。",
        "tab": "write",
        "prompt": ("把用户给的论文内容转成一份单文件 HTML 幻灯（10-14 页，含封面/贡献/方法/实验/结论），"
                   "使用内联 CSS 的简洁学术风格（白底蓝强调），每页 <section>，键盘翻页用少量原生 JS。只输出完整 HTML 代码块。")},
}


def skill_run(skill_id, user_input, extra=""):
    sk = cs_all().get(skill_id)
    if not sk:
        return {"ok": False, "error": "未知技能 %s" % skill_id}
    if not model.status()["ok"]:
        return {"ok": False, "ai": False,
                "error": "技能需要模型。去「设置 → 模型设置」选预设并填 API Key。"}
    body = user_input or ""
    if extra and extra.strip():
        body += "\n\n【附加材料】\n" + extra.strip()[:6000]
    if len(body.strip()) < 10:
        return {"ok": False, "error": "请先填入要处理的内容（≥10 字）"}
    try:
        reply = model.quick_ask(body, system=sk["prompt"], max_tokens=1600, temperature=0.4)
        return {"ok": True, "reply": reply, "skill": skill_id, "name": sk["name"], "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ================================================================ 10. 文献库（参考文献文件夹）
RF_EMPTY = {"folders": []}


def rf_list(by):
    fs = [f for f in _load("reffolders").get("folders", []) if f.get("owner") == by]
    fs.sort(key=lambda f: f.get("ts", ""), reverse=True)
    return fs


def rf_create(by, name):
    name = (name or "").strip()[:60]
    if not name:
        return {"ok": False, "error": "文件夹名不能为空"}
    def fn(obj):
        obj.setdefault("folders", []).append({
            "id": _uid("rf", obj.get("folders", [])), "owner": by or "",
            "name": name, "papers": [], "analyses": {}, "ts": _now()})
        return obj
    _mutate("reffolders", fn)
    return {"ok": True, "folders": rf_list(by)}


def rf_remove(rid, by):
    def fn(obj):
        obj["folders"] = [f for f in obj.get("folders", [])
                          if not (f.get("id") == rid and f.get("owner") == by)]
        return obj
    _mutate("reffolders", fn)
    return {"ok": True, "folders": rf_list(by)}


def _rf_get(obj, rid, by):
    for f in obj.get("folders", []):
        if f.get("id") == rid and f.get("owner") == by:
            return f
    raise ValueError("文献库不存在或无权访问")


def rf_add_papers(rid, by, papers):
    if not papers:
        return {"ok": False, "error": "未选择任何文献"}
    added = 0
    def fn(obj):
        nonlocal added
        f = _rf_get(obj, rid, by)
        have = {(p.get("title") or "").lower() for p in f.get("papers", [])}
        added = 0
        for p in papers[:100]:
            t = (p.get("title") or "").strip()
            if not t or t.lower() in have:
                continue
            f.setdefault("papers", []).append({
                "title": t, "year": p.get("year") or "", "venue": p.get("venue") or "",
                "abstract": (p.get("abstract") or "")[:1500], "link": p.get("link") or "",
                "source": p.get("source") or "", "ts": _now()})
            have.add(t.lower())
            added += 1
        f["ts"] = _now()
        return obj
    try:
        _mutate("reffolders", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "folders": rf_list(by), "added": added}


def rf_remove_paper(rid, by, idx):
    def fn(obj):
        f = _rf_get(obj, rid, by)
        ps = f.get("papers", [])
        if 0 <= idx < len(ps):
            t = ps[idx].get("title", "")
            ps.pop(idx)
            f.get("analyses", {}).pop(t, None)
        return obj
    try:
        _mutate("reffolders", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "folders": rf_list(by)}


def rf_patch_paper(rid, by, title, **updates):
    """按标题补全/更新单篇文献字段（如 abstract）。"""
    def fn(obj):
        f = _rf_get(obj, rid, by)
        for p in f.get("papers", []):
            if (p.get("title") or "").strip().lower() == (title or "").strip().lower():
                for k, v in updates.items():
                    if v is not None:
                        p[k] = str(v)[:1500] if k == "abstract" else v
                break
        return obj
    try:
        _mutate("reffolders", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _norm_title(s):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (s or "").lower())


def _title_similar(a, b):
    """判断两个标题是否指向同一篇。

    用 difflib 序列相似度（对词序、标点、中英文都稳健）。
    实测：同一篇（含副标题差异）= 0.86~1.00；
    检索 API 返回的“相关但不同”文献 = 0.62~0.78。
    阈值取 0.86，宁可漏也不要错配。
    """
    na, nb = _norm_title(a), _norm_title(b)
    if len(na) < 5 or len(nb) < 5:
        return False
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.86


# 摘要最低长度：低于此值视为空壳（OpenAlex 个别中文文献只返回“摘要：”）
_MIN_ABSTRACT = 120


def _get_json_retry(url, tries=3, base=3.0, timeout=20):
    """GET JSON，遇 429/503 限流按 3s/6s 退避重试（学术 API 免费额度有频控）。"""
    last = None
    for i in range(tries):
        try:
            return model._http_get_json(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503) and i < tries - 1:
                time.sleep(base * (i + 1))
                continue
            raise
    raise last


def _inv_to_text(ii):
    """OpenAlex 的 abstract_inverted_index（词→位置列表）还原成正文。"""
    if not ii:
        return ""
    pos = {}
    for word, idxs in ii.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[k] for k in sorted(pos)).strip()


def _doi_from_link(link):
    """从 link 里抽 DOI（支持 https://doi.org/xxx 或裸 DOI）。"""
    s = (link or "").strip()
    m = re.search(r"10\.\d{4,9}/[^\s\"'<>),]+", s)
    return m.group(0).rstrip(".") if m else ""


def _clean_cr_abstract(raw):
    a = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", a).strip()


# ---- ① 按 DOI 精确取（无错配风险，首选） ----
def _abs_doi_openalex(doi):
    j = _get_json_retry("https://api.openalex.org/works/https://doi.org/%s"
                        "?mailto=dawn-agent@example.com" % doi, timeout=20)
    return _inv_to_text(j.get("abstract_inverted_index"))


def _abs_doi_s2(doi):
    j = _get_json_retry("https://api.semanticscholar.org/graph/v1/paper/DOI:%s"
                        "?fields=abstract" % doi, timeout=20)
    return (j.get("abstract") or "").strip()


def _abs_doi_crossref(doi):
    j = _get_json_retry("https://api.crossref.org/works/%s" % doi, timeout=20)
    return _clean_cr_abstract((j.get("message") or {}).get("abstract"))


# ---- ② 按标题检索（有错配风险，阈值从严，作兜底） ----
def _abs_title_openalex(title):
    j = _get_json_retry("https://api.openalex.org/works?search=%s&per-page=3"
                        "&mailto=dawn-agent@example.com" % urllib.parse.quote(title), timeout=20)
    for w in (j.get("results") or []):
        if _title_similar(title, w.get("title") or ""):
            t = _inv_to_text(w.get("abstract_inverted_index"))
            if len(t) >= _MIN_ABSTRACT:
                return t
    return ""


def _abs_title_s2(title):
    j = _get_json_retry("https://api.semanticscholar.org/graph/v1/paper/search"
                        "?query=%s&fields=title,abstract&limit=3" % urllib.parse.quote(title), timeout=20)
    for d in (j.get("data") or []):
        if _title_similar(title, d.get("title") or ""):
            a = (d.get("abstract") or "").strip()
            if len(a) >= _MIN_ABSTRACT:
                return a
    return ""


def _abs_title_crossref(title):
    j = _get_json_retry("https://api.crossref.org/works?query.bibliographic=%s&rows=3"
                        "&select=title,abstract" % urllib.parse.quote(title), timeout=20)
    for item in ((j.get("message") or {}).get("items") or []):
        ti = item.get("title") or ""
        if isinstance(ti, list):
            ti = " ".join(ti)
        if _title_similar(title, ti):
            a = _clean_cr_abstract(item.get("abstract"))
            if len(a) >= _MIN_ABSTRACT:
                return a
    return ""


def rf_fetch_abstract(title, link=""):
    """自动抓取摘要（免费、无需 Key）。

    策略：**优先按 DOI 精确取**（零错配风险），失败再按标题从严检索兜底。
    顺序：DOI×OpenAlex → DOI×S2 → DOI×Crossref → 标题×OpenAlex → 标题×S2 → 标题×Crossref。
    放在后端执行：①避免浏览器 CORS 限制；②复用模型层的直连/代理回退逻辑。
    """
    t = (title or "").strip()
    doi = _doi_from_link(link)
    plan = []
    if doi:
        plan += [("doi:semantic_scholar", lambda: _abs_doi_s2(doi)),
                 ("doi:crossref", lambda: _abs_doi_crossref(doi)),
                 ("doi:openalex", lambda: _abs_doi_openalex(doi))]
    if len(t) >= 5:
        plan += [("semantic_scholar", lambda: _abs_title_s2(t)),
                 ("crossref", lambda: _abs_title_crossref(t)),
                 ("openalex", lambda: _abs_title_openalex(t))]
    if not plan:
        return {"ok": False, "error": "缺少标题与 DOI，无法检索摘要"}
    for src, fn in plan:
        try:
            abst = (fn() or "").strip()
        except Exception:
            abst = ""
        if len(abst) >= _MIN_ABSTRACT:
            return {"ok": True, "abstract": abst[:1500], "source": src}
    return {"ok": False, "error": "外部数据源未找到该文献摘要（可手动粘贴）"}


def rf_set_analysis(rid, by, title, analysis):
    def fn(obj):
        f = _rf_get(obj, rid, by)
        f.setdefault("analyses", {})[title] = (analysis or "")[:6000]
        return obj
    try:
        _mutate("reffolders", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def rf_synthesize(rid, by):
    """基于文件夹内全部精读分析，综合找创新点 + 可行性。"""
    f = None
    for x in rf_list(by):
        if x.get("id") == rid:
            f = x
            break
    if not f:
        return {"ok": False, "error": "文献库不存在或无权访问"}
    analyses = f.get("analyses", {})
    if not analyses:
        return {"ok": False, "error": "请先对文献批量生成精读分析"}
    if not model.status()["ok"]:
        return {"ok": False, "ai": False, "error": "综合分析需要模型（设置 → 模型设置）"}
    joined = "\n\n".join("【%s】\n%s" % (t, a[:1800]) for t, a in list(analyses.items())[:25])
    try:
        reply = model.quick_ask(
            "以下是文献库中 %d 篇论文的精读分析。请综合（只依据这些分析，不引入新文献）：\n"
            "## 可切入的创新点（3-5 条，每条注明源自哪些文献的哪个空白）\n"
            "## 可行性评估（数据/算力/时间，逐条）\n## 最值得先做的一条及理由\n\n%s" % (len(analyses), joined[:9000]),
            system="你是严谨的科研策略顾问：只依据给定分析综合，不编造。",
            max_tokens=1800, temperature=0.35)
        return {"ok": True, "reply": reply, "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ================================================================ 11. 评审工作台（AI 专家盲审 + 协作者审核）
RV_SCORE_DIMS = ["创新性", "严谨性", "可行性", "清晰度"]


def review_create(by, title, content, kind, reviewers=None):
    kind = "ai_blind" if kind == "ai_blind" else "peer"
    content = (content or "").strip()
    if len(content) < 50:
        return {"ok": False, "error": "评审材料太短（≥50 字）"}
    if kind == "peer" and not reviewers:
        return {"ok": False, "error": "协作者评审需要选择至少一位评审人"}
    def fn(obj):
        obj.setdefault("reviews", []).append({
            "id": _uid("v", obj.get("reviews", [])), "owner": by or "",
            "title": (title or "").strip()[:60] or "未命名评审", "content": content[:8000],
            "kind": kind, "reviewers": reviewers or [], "results": [],
            "status": "pending", "ts": _now()})
        return obj
    _mutate("reviewflow", fn)
    return {"ok": True, "reviews": review_list(by)}


def review_list(by):
    rs = [r for r in _load("reviewflow").get("reviews", []) if r.get("owner") == by]
    rs.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rs


def review_mine(by):
    out = []
    for r in _load("reviewflow").get("reviews", []):
        if by and by in (r.get("reviewers") or []) and r.get("status") == "pending":
            done = any(x.get("reviewer") == by for x in r.get("results", []))
            if not done:
                out.append({"id": r["id"], "title": r["title"], "content": r["content"],
                            "owner": r.get("owner"), "ts": r.get("ts")})
    return out


def _anon_names():
    """用户可配置的匿名化名单（data/anonymize.json: {"names": ["张三", "2025001"]}）。"""
    p2 = os.path.join(_DATA_DIR, "anonymize.json")
    try:
        return json.load(open(p2, encoding="utf-8")).get("names", [])
    except Exception:
        return []


def _blind_review(content):
    """三位匿名专家独立评审（内容去标识化后送审；名单见 data/anonymize.json）。"""
    anon = content
    for name in _anon_names():
        if name:
            anon = re.sub(re.escape(name), "[作者匿名]", anon, flags=re.IGNORECASE)
    anon = re.sub(r"\b[A-Z]\d{4,}\b", "[学号匿名]", anon)
    anon = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[邮箱匿名]", anon)
    personas = [
        ("资深领域专家（UAV 安全）", "重点关注：动机是否成立、与已有工作的差异是否真实、创新点是否站得住。"),
        ("方法学审稿人", "重点关注：实验设计、变量控制、评估指标、可复现性；实验未跑之处按占位处理但要指出缺什么。"),
        ("低空经济安全产业专家", "重点关注：工程可行性、合规与监管相关性、落地价值。"),
    ]
    dims = "、".join(RV_SCORE_DIMS)
    results = []
    for role, focus in personas:
        try:
            reply = model.quick_ask(
                "以下是匿名化后的评审材料：\n\n" + anon[:8000] + "\n\n"
                "请以%s身份盲审。%s\n输出 Markdown：\n"
                "## 评分（%s，各 1-5 分）\n## 主要优点（≤3 条）\n## 主要问题（≤3 条，含必须补的实验）\n"
                "## 结论（Accept / Minor Revision / Major Revision / Reject + 一句话理由）" % (role, focus, dims),
                system="你是独立盲审专家：只依据给定材料评审，不编造文献与数据；材料中实验结果若为占位符，"
                       "如实按'未完成'处理。全程不得尝试推测或还原作者身份。",
                max_tokens=900, temperature=0.4)
            results.append({"reviewer": "盲审·" + role, "scores": {}, "comments": reply[:5000],
                            "verdict": "", "blind": True, "ts": _now()})
        except Exception as e:
            results.append({"reviewer": "盲审·" + role, "scores": {}, "comments": "评审失败：%s" % e,
                            "verdict": "", "blind": True, "ts": _now()})
    return results


def review_run_blind(rid, by, note=""):
    """运行 AI 盲审；已有轮次时为下一轮（可附修改说明），轮次留痕支持迭代改进。"""
    if not model.status()["ok"]:
        return {"ok": False, "ai": False, "error": "AI 盲审需要模型（设置 → 模型设置）"}
    def fn(obj):
        for r in obj.get("reviews", []):
            if r.get("id") == rid and r.get("owner") == by:
                if r.get("kind") != "ai_blind":
                    raise ValueError("该评审不是 AI 盲审类型")
                r["_run"] = True
                return obj
        raise ValueError("评审不存在")
    _mutate("reviewflow", fn)
    cur = next((r for r in review_list(by) if r["id"] == rid), None)
    if not cur:
        return {"ok": False, "error": "评审不存在"}
    round_no = len(cur.get("rounds", [])) + 1
    material = cur.get("content", "")
    if str(note or "").strip():
        material += "\n\n【作者修改说明（第 %d 轮，匿名送审）】\n%s" % (round_no, str(note).strip()[:2000])
    results = _blind_review(material)
    def fn2(obj):
        for r in obj.get("reviews", []):
            if r.get("id") == rid and r.get("owner") == by:
                rounds = r.setdefault("rounds", [])
                rounds.append({"round": round_no, "note": str(note or "").strip()[:2000],
                               "results": results, "ts": _now()})
                r["results"] = results  # 兼容：始终指向最新一轮
                r["status"] = "done"
                return obj
        return obj
    _mutate("reviewflow", fn2)
    return {"ok": True, "round": round_no, "reviews": review_list(by)}


def review_submit(rid, by, scores, comments, verdict):
    def fn(obj):
        for r in obj.get("reviews", []):
            if r.get("id") == rid and by in (r.get("reviewers") or []):
                if not any(x.get("reviewer") == by for x in r.get("results", [])):
                    r.setdefault("results", []).append({
                        "reviewer": by, "scores": scores or {}, "comments": (comments or "")[:4000],
                        "verdict": (verdict or "")[:40], "blind": False, "ts": _now()})
                    if all(any(x.get("reviewer") == rv for x in r["results"]) for rv in r["reviewers"]):
                        r["status"] = "done"
                    return obj
                raise ValueError("你已提交过该评审")
        raise ValueError("评审不存在或你不是评审人")
    try:
        _mutate("reviewflow", fn)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "reviews": review_list(_who_of(rid)) or [], "mine": review_mine(by)}


def _who_of(rid):
    for r in _load("reviewflow").get("reviews", []):
        if r.get("id") == rid:
            return r.get("owner")
    return None


# ================================================================ 12. 方向链（定方向 → 文献调研）
def wf_set_direction(by, direction, refined=None):
    d = (direction or "").strip()
    if len(d) < 6:
        return {"ok": False, "error": "方向描述太短（≥6 字）"}
    def fn(obj):
        u = obj.setdefault("users", {}).setdefault(by or "", {})
        u["direction_final"] = d[:300]
        if refined:
            u["direction_refined"] = str(refined)[:2000]
        u["direction_ts"] = _now()
        return obj
    _mutate("workflow", fn)
    return {"ok": True, "direction": d}


def wf_get_direction(by):
    u = _load("workflow").get("users", {}).get(by or "", {})
    return {"direction_final": u.get("direction_final", ""),
            "direction_refined": u.get("direction_refined", ""),
            "ts": u.get("direction_ts", "")}


# ================================================================ 13. 数据集检索与下载（Zenodo 公开 API）
def ds_extract_names(text):
    """从论文/开题文本提取候选数据集名：大写缩写 + 已知数据集词 + 年份模式。"""
    text = text or ""
    names = set()
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9]*-(?:\d{4}|DS\d*|VIDS\w*|[A-Z]{2,}\d*))\b", text):
        names.add(m.group(1))
    for kw in ("UAVIDS", "CICIDS", "NSL-KDD", "CIC-IDS", "ToN_IoT", "TON_IoT", "AWID", "Wi-Fi"):
        if kw.lower() in text.lower():
            names.add(kw)
    return sorted(names)[:8] or []


def ds_search_zenodo(query, size=5):
    import urllib.parse
    q = urllib.parse.quote(query)
    url = "https://zenodo.org/api/records?q=%s&size=%d" % (q, max(1, min(10, size)))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "research-agent/0.3"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        hits = []
        for h in (data.get("hits", {}) or {}).get("hits", [])[:size]:
            md = h.get("metadata", {}) or {}
            files = [{"name": f.get("key", ""), "size": f.get("size", 0),
                      "url": (f.get("links") or {}).get("self", "")} for f in (h.get("files") or [])[:5]]
            hits.append({"title": md.get("title", ""), "doi": h.get("doi", ""),
                         "link": h.get("links", {}).get("html", "") or h.get("links", {}).get("self_html", ""),
                         "files": files, "source": "Zenodo"})
        return {"ok": True, "hits": hits}
    except Exception as e:
        return {"ok": False, "error": "Zenodo 检索失败（可能断网）：%s" % e,
                "hits": [], "fallback_links": [
                    {"title": "IEEE DataPort 搜索", "link": "https://ieee-dataport.org/search?query=" + q},
                    {"title": "Google Dataset Search", "link": "https://datasetsearch.research.google.com/search?query=" + q},
                    {"title": "Kaggle 搜索", "link": "https://www.kaggle.com/search?q=" + q}]}


def ds_download(url, dest_dir, filename):
    if not (url or "").startswith(("http://", "https://")):
        return {"ok": False, "error": "非法下载地址"}
    dest_dir = os.path.join(_DATA_DIR, "datasets", dest_dir or "misc")
    os.makedirs(dest_dir, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", filename or "dataset.bin")[-80:]
    dest = os.path.join(dest_dir, safe)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "research-agent/0.3"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
            n = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
                if n > 500 * 1024 * 1024:
                    raise ValueError("超过 500MB 上限，已中止（请手动下载大文件）")
        return {"ok": True, "path": dest, "bytes": n}
    except Exception as e:
        if os.path.isfile(dest):
            try:
                os.remove(dest)
            except Exception:
                pass
        return {"ok": False, "error": "下载失败：%s" % e}


def ds_list_downloaded():
    base = os.path.join(_DATA_DIR, "datasets")
    out = []
    if os.path.isdir(base):
        for d in sorted(os.listdir(base)):
            dd = os.path.join(base, d)
            if os.path.isdir(dd):
                fs = [(f, os.path.getsize(os.path.join(dd, f))) for f in os.listdir(dd)
                      if os.path.isfile(os.path.join(dd, f))]
                out.append({"name": d, "files": fs})
    return out


# ================================================================ 14. 自定义技能（上传/管理）
def cs_all():
    """内置技能 + 用户上传的自定义技能合并表。"""
    built = {k: dict(v, custom=False) for k, v in SKILLS.items()}
    for c in _load("custom_skills").get("skills", []):
        built[c["id"]] = dict(c, custom=True)
    return built


def cs_add(by, name, desc, prompt, tab="chat"):
    name = (name or "").strip()[:40]
    prompt = (prompt or "").strip()
    if not name:
        return {"ok": False, "error": "技能名称不能为空"}
    if len(prompt) < 20:
        return {"ok": False, "error": "提示词太短（≥20 字）——它决定技能的行为"}
    def fn(obj):
        cid = "c_" + re.sub(r"[^\w]", "", name)[:14] + "_%03d" % (len(obj.get("skills", [])) + 1)
        obj.setdefault("skills", []).append({
            "id": cid, "name": name, "desc": (desc or "自定义技能").strip()[:150],
            "prompt": prompt[:4000], "tab": tab, "by": by or "", "ts": _now()})
        return obj
    out = _mutate("custom_skills", fn)
    cid = next((x["id"] for x in out.get("skills", []) if x.get("name") == name and x.get("by") == by), None)
    return {"ok": True, "id": cid}


def cs_remove(by, cid):
    def fn(obj):
        obj["skills"] = [x for x in obj.get("skills", [])
                         if not (x.get("id") == cid and x.get("by") == by)]
        return obj
    _mutate("custom_skills", fn)
    return {"ok": True}


# ================================================================ 15. 论文格式检查器（规则版 + 模板）
FT_TYPES = {"journal": "期刊论文", "thesis": "本科毕业论文"}
FT_DEFAULT_STRUCT = {
    "journal": ["摘要", "关键词", "引言", "结论", "参考文献"],
    "thesis": ["摘要", "关键词", "Abstract", "目录", "绪论", "结论", "参考文献", "致谢"],
}


def ft_templates():
    return _load("format_templates").get("templates", {})


def ft_template_save(ptype, text):
    if ptype not in FT_TYPES:
        return {"ok": False, "error": "未知论文类型"}
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 3:
        return {"ok": False, "error": "模板要求至少 3 行（每行一个必需章节/要素）"}
    def fn(obj):
        obj.setdefault("templates", {})[ptype] = {
            "items": lines[:60], "ts": _now()}
        return obj
    _mutate("format_templates", fn)
    return {"ok": True, "count": len(lines[:60])}


def ft_template_get(ptype):
    t = ft_templates().get(ptype)
    if not t:
        return {"ok": True, "items": FT_DEFAULT_STRUCT.get(ptype, []), "default": True}
    return {"ok": True, "items": t.get("items", []), "default": False, "ts": t.get("ts", "")}


def ft_check(text, ptype="journal", ai=False):
    """规则版格式检查：结构 / 参考文献编号 / 图表标号 / 标点 / 段落。"""
    text = (text or "").strip()
    if len(text) < 100:
        return {"ok": False, "error": "请粘贴论文全文或较长章节（≥100 字）"}
    if ptype not in FT_TYPES:
        return {"ok": False, "error": "未知论文类型"}
    problems, good = [], []
    norm = re.sub(r"[ \t]+", " ", text)

    # 1) 结构完整性（模板优先，否则内置）；忽略全部空白，兼容"摘 要"等写法
    tpl = ft_template_get(ptype)
    required = tpl.get("items") or FT_DEFAULT_STRUCT.get(ptype, [])
    flat = re.sub(r"\s+", "", text)
    miss = [k for k in required if k.replace(" ", "").lower() not in flat.lower()]
    if miss:
        problems.append("**缺少必需章节/要素**（%s）：%s" % (
            "模板" if not tpl.get("default") else "内置标准", "、".join(miss[:12])))
    else:
        good.append("必需章节/要素齐全（%d/%d）" % (len(required), len(required)))

    # 2) 参考文献编号连续性：[1] [2] ... 应从 1 递增且无断号
    refs = [int(m) for m in re.findall(r"\[(\d{1,3})\]", text)]
    if refs:
        seq = sorted(set(x for x in refs if x <= 200))
        gaps = [i for i in range(1, max(seq) + 1) if i not in seq] if seq else []
        if gaps:
            problems.append("**参考文献编号断号**：缺少 %s（正文中出现编号 %s）"
                            % (gaps[:10], "…" if len(seq) > 12 else seq))
        else:
            good.append("参考文献编号连续（[1]–[%d]）" % max(seq))
        # 引用从未出现在文末列表（粗查：引用数 vs 列表条目数）

    # 3) 图表标号：定义（图1/图 1）与引用配对
    figs = set(int(m) for m in re.findall(r"图\s*(\d{1,2})", text))
    tabs = set(int(m) for m in re.findall(r"表\s*(\d{1,2})", text))
    orphan_figs = [f for f in sorted(figs) if not re.search(r"如[图图]\s*%d|[如见如图]\s*%d" % (f, f), text)]
    if len(figs) > 1 and orphan_figs:
        problems.append("**图片编号疑似未在正文引用**：图 %s（检查是否有“如图X所示”）" % orphan_figs[:8])
    elif figs:
        good.append("图片编号出现 %d 个（图 %s）" % (len(figs), sorted(figs)[:8]))
    if len(tabs) > 1:
        good.append("表格编号出现 %d 个（表 %s）" % (len(tabs), sorted(tabs)[:8]))

    # 4) 中英文标点混用：中文句子里出现半角逗号/句号
    cn_half = re.findall(r"[\u4e00-\u9fff] [,.] [\u4e00-\u9fff]|[\u4e00-\u9fff][,.][\u4e00-\u9fff]", text)
    if cn_half:
        problems.append("**疑似中英文标点混用** %d 处（中文语句中出现半角 ',' '.' ，建议改全角'，''。'；代码/公式除外）" % len(cn_half))
    # 全角引号/括号一致性粗查
    if text.count("（") != text.count("）"):
        problems.append("**全角括号不配对**：%d 个'（' vs %d 个'）'" % (text.count("（"), text.count("）")))

    # 5) 段落长度：超长段落（>800 字）提示分节
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    long_ps = [i + 1 for i, p in enumerate(paras) if len(p) > 800]
    if long_ps:
        problems.append("**超长段落**：第 %s 段超过 800 字，建议按要点拆分" % long_ps[:5])
    if paras:
        good.append("共 %d 个自然段，平均 %.0f 字/段" % (len(paras), len(text) / len(paras)))

    md = ["# 格式检查报告 · %s" % FT_TYPES.get(ptype, ptype), ""]
    md.append("> 模板来源：%s" % ("你的自定义模板" if not tpl.get("default") else "内置标准结构"))
    md.append("")
    md.append("**发现 %d 个问题，%d 项通过。**" % (len(problems), len(good))) if (problems or good) else None
    if problems:
        md += ["", "## 需要处理", ""] + ["- " + x for x in problems]
    if good:
        md += ["", "## 已通过", ""] + ["- " + x for x in good]
    if not problems:
        md += ["", "🎉 未发现规则层面的格式问题。注意：字体/行距/页边距等排版细节在 Word 层面，本工具检查的是文本结构。"]
    md += ["", "> 排版类要求（字体/行距/页边距/页码）属于 Word 模板层面，导入 Word 前请套用学校模板。"]

    out = {"ok": True, "problems": len(problems), "pass": len(good), "markdown": "\n".join(md)}
    if ai and model.status()["ok"]:
        try:
            reply = model.quick_ask(
                "以下是一篇%s的格式检查报告与论文节选。请从学术写作规范角度补充 3-5 条报告未覆盖的格式/表达问题"
                "（标题层级、术语一致性、时态、引用格式等），只依据给定文本，不编造：\n\n%s\n\n%s"
                % (FT_TYPES.get(ptype, ptype), "\n".join(md)[:2500], text[:6000]),
                system="你是论文格式审查助理：只谈格式与表达规范，不评价学术内容；不编造。", max_tokens=900, temperature=0.3)
            out["ai_md"] = reply[:4000]
            out["markdown"] += "\n\n## AI 深度检查（补充）\n\n" + out["ai_md"]
        except Exception as e:
            out["ai_error"] = str(e)
    return out
