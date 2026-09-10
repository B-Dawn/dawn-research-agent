#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""破晓「全流程」编排引擎（零依赖）。

目标（对齐用户要求）：
    ① 定方向 → ② 文献调研 → ③ 创新点与可行性 → ④ 苏格拉底问询定题 → ⑤（合格的题目）提交人工审核
    **交给人工审核之前的内容全自动**；人工审核交给「流程中心」（BPM）。

关键纪律：
    1. 只有**经过苏格拉底问询并逐条回应**的题目才判为「合格」，才允许提交人工审核。
    2. 不做假数据：需要 AI 而模型不可用时，该环节标记 blocked 并说明如何续跑，
       但**不阻断不需要 AI 的环节**（文献检索/收录/对比矩阵/提交审批照常执行）。
    3. 每步完成自动回写「论文十步走」进度，实现对话 ↔ 十步走 ↔ 各模块联动。

数据：data/pipeline.json = {"users": {"<name>": {stages, topic, cache, ts}}}
"""

import json
import os
import re
import sys
import threading
import time

# 与 web_app 一致：把 scripts 目录加入 sys.path 后可复用检索/矩阵能力
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import model_bridge as model
import workbench as wb
import bpm
import team_store as store

from lib import sources
import ra_matrix

_LOCK = threading.Lock()
_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
_FILE = os.path.join(_DATA_DIR, "pipeline.json")
_EMPTY = {"users": {}}

# AI 不可用时的统一话术
_AI_HINT = ("该环节需要 AI：请到「🔧 模型与API」配置可用模型（推荐智谱 glm-4-flash / SiliconFlow 等免费额度），"
            "配好后回到这里点「⚡ 一键全流程」或「继续全流程」即可从断点续跑。"
            "不需要 AI 的环节（文献检索 / 收录文献库 / 对比矩阵 / 提交审核）已照常完成。")

# 判定「模型不可用」的错误特征（401/403/Key/未配置…）→ 归为 blocked 而非 fail
_KEYERR = re.compile(r"401|403|API Key|未授权|无权限|模型不可用|未配置|令牌")

DIGEST_PROMPTS = {
    "gap": ("请基于给定论文文本做**研究空白分析**（Markdown）：## 论文自己承认的局限 → "
            "## 从文本推断的未覆盖点（注明推断依据）→ ## 可延伸的研究方向（每条给一句为什么值得做）。"
            "区分『原文承认』与『你的推断』，不编造文献。"),
    "feasibility": ("请基于给定论文文本做**可行性分析**（Markdown）：## 复现所需资源（数据/算力/代码/人天估计）→ "
                    "## 难点与风险（逐条，注明依据）→ ## 若移植到自己课题的适配建议。只依据文本；"
                    "文本未提及的资源需求标注【需向作者确认】。"),
}


def _stage(sid, name, step, needs):
    return {"id": sid, "name": name, "step": step, "needs": needs}


STAGES = [
    _stage("direction", "① 定方向（AI 分析简历/方向）", "s1", "ai"),
    _stage("search",    "② 文献调研（多源检索）",        "s2", "net"),
    _stage("collect",   "②b 收录文献库",                 "s2", "none"),
    _stage("matrix",    "②c 生成对比矩阵",               "s2", "none"),
    _stage("digest",    "③ 创新点与可行性（精读分析）",   "s3", "ai"),
    _stage("socratic",  "④ 苏格拉底问询（题目合格性关卡）", "s4", "ai"),
    _stage("topic",     "④b 生成合格题目",                "s4", "ai"),
    _stage("review",    "⑤ 提交人工审核（流程中心）",      "s4", "none"),
]
_STAGE_BY_ID = {s["id"]: s for s in STAGES}
_STAGE_ORDER = [s["id"] for s in STAGES]


# ================================================================ 存储
def _load():
    if not os.path.isfile(_FILE):
        return json.loads(json.dumps(_EMPTY))
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            o = json.load(f)
    except Exception:
        return json.loads(json.dumps(_EMPTY))
    o.setdefault("users", {})
    return o


def _save(obj):
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    for i in range(5):
        try:
            os.replace(tmp, _FILE)
            return
        except PermissionError:
            if i == 4:
                with open(_FILE, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=1)
                return
            time.sleep(0.15)


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _u(by):
    return _load().get("users", {}).get(by or "", {})


def _set_user(by, data):
    def fn(obj):
        obj.setdefault("users", {})[by or ""] = data
        return obj
    with _LOCK:
        obj = _load()
        new = fn(obj)
        _save(new)


def state(by):
    """给前端：每步状态 + 题目 + 缓存摘要。"""
    u = _u(by)
    return {
        "stages": [{"id": s["id"], "name": s["name"], "step": s["step"], "needs": s["needs"],
                    **(u.get("stages", {}).get(s["id"]) or {"status": "todo"})} for s in STAGES],
        "topic": u.get("topic") or None,
        "direction": u.get("direction") or "",
        "query": u.get("query") or "",
        "review_inst": u.get("review_inst") or "",
        "ts": u.get("ts") or "",
        "cache_counts": {
            "records": len((u.get("cache") or {}).get("records") or []),
            "folder_id": (u.get("cache") or {}).get("folder_id") or "",
            "has_matrix": bool((u.get("cache") or {}).get("matrix_md")),
            "has_digest": bool((u.get("cache") or {}).get("digest_md")),
            "has_socratic": bool((u.get("cache") or {}).get("socratic_md")),
        },
    }


def reset(by):
    def fn(obj):
        obj.setdefault("users", {})[by or ""] = {}
        return obj
    with _LOCK:
        _save(fn(_load()))
    return {"ok": True, "state": state(by)}


# ================================================================ 各环节
def _run_direction(by, opts, cache):
    prof = {}
    try:
        prof = store.load_profile_extra() or {}
    except Exception:
        pass
    resume = (opts.get("resume_text") or "").strip()
    if resume and len(resume) >= 20 and model.status()["ok"]:
        sys_p = ("你是科研方向规划师。用户会给出个人简历/擅长技术/想要的研究方向（可能只有其中一两项）。"
                 "请只依据给定材料，返回 STRICT JSON（无代码栅栏）："
                 '{"analysis_md": "Markdown 分析：## 你的优势 → ## 可行的研究方向（2-3 个，每个含一句话可行性判断：数据/算力/时间）", '
                 '"direction_cn": "最适合的研究方向（中文，一句话）", '
                 '"skills": "用户技能栈（逗号分隔，未提及的不要编造）", '
                 '"direction_final": "建议定稿的最终研究方向（一句话，具体到可检索）"}。'
                 "不编造用户经历与文献；方向必须能复用用户已有技能。")
        try:
            raw = model.quick_ask(resume[:8000], system=sys_p, max_tokens=1300, temperature=0.4)
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        except Exception as e:
            return {"status": "fail", "error": "AI 方向分析失败：%s" % e}
        try:
            store.save_profile_extra({k: data.get(k, "") for k in ("direction_cn", "skills")})
        except Exception:
            pass
        d = (data.get("direction_final") or data.get("direction_cn") or "").strip()
        if not d:
            return {"status": "fail", "error": "AI 未给出可用方向，请补充简历/方向描述后重试"}
        wb.wf_set_direction(by, d, data.get("analysis_md", ""))
        cache["direction"] = d
        return {"status": "ok", "note": "AI 已分析简历并定稿方向：%s" % d,
                "detail": (data.get("analysis_md") or "")[:3000]}
    # 不需要 AI：沿用已有方向
    d = ((wb.wf_get_direction(by) or {}).get("direction_final")
         or prof.get("direction_cn") or opts.get("query") or "").strip()
    if d:
        cache["direction"] = d
        return {"status": "ok", "note": "沿用已保存/已填写的方向（本步未调用 AI）", "detail": d}
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error":
                "没有可用方向。请任选其一：① 在「⚡ 一键全流程」里粘贴你的简历/方向描述（≥20字）后重跑；"
                "② 到「科研画像」填写研究方向；③ 配置模型后由 AI 从简历自动定方向。"}
    return {"status": "fail", "error": "请提供简历/方向描述（≥20字）或先在「科研画像」填写方向"}


def _run_search(by, opts, cache):
    q = (opts.get("query") or cache.get("direction") or "").strip()
    if len(q) < 4:
        return {"status": "skip", "note": "没有可用检索式（方向为空），跳过文献调研"}
    srcs = [s.strip() for s in (opts.get("sources") or "arxiv,openalex,crossref,s2").split(",") if s.strip()]
    limit = max(3, min(60, int(opts.get("limit") or 20)))
    try:
        recs = sources.search(q, sources=srcs, limit=limit, year_from=None, year_to=None)
    except Exception as e:
        return {"status": "fail", "error": "检索失败：%s" % e}
    recs.sort(key=lambda r: (-(r.get("citations") or 0), -(r.get("year") or 0)))
    cache["query"] = q
    cache["records"] = [_trim(r) for r in recs]
    if not recs:
        return {"status": "warn", "note": "检索无结果（可能断网，或检索式过窄）——可改检索式后重跑"}
    return {"status": "ok", "note": "命中 %d 条（检索式：%s）" % (len(recs), q)}


def _trim(r):
    """裁剪记录，便于落盘与后续环节使用。"""
    return {"title": r.get("title") or "", "year": r.get("year") or "",
            "venue": r.get("venue") or "", "source": r.get("source") or "",
            "citations": r.get("citations") or 0,
            "abstract": (r.get("abstract") or "")[:1500],
            "link": r.get("link") or ""}


def _run_collect(by, opts, cache):
    recs = cache.get("records") or []
    if not recs:
        return {"status": "skip", "note": "没有检索结果，跳过收录"}
    q = cache.get("query") or ""
    fname = (opts.get("folder_name") or ("自动调研 · " + q[:24])).strip()[:60]
    top = max(1, min(100, int(opts.get("collect_n") or 30)))
    try:
        folders = wb.rf_list(by)
        fid = next((f.get("id") for f in folders if f.get("name") == fname), None)
        if not fid:
            r = wb.rf_create(by, fname)
            fid = ((r.get("folders") or [{}])[0]).get("id")
        papers = [{"title": p.get("title"), "year": p.get("year"), "venue": p.get("venue"),
                   "abstract": p.get("abstract") or "", "link": p.get("link") or "",
                   "source": p.get("source") or ""} for p in recs[:top] if p.get("title")]
        r2 = wb.rf_add_papers(fid, by, papers)
    except Exception as e:
        return {"status": "fail", "error": "收录失败：%s" % e}
    cache["folder_id"] = fid
    cache["folder_name"] = fname
    return {"status": "ok", "note": "已收录 %d 篇 →「文献库 · %s」" % (r2.get("added") or 0, fname),
            "folder_id": fid}


def _run_matrix(by, opts, cache):
    recs = cache.get("records") or []
    if not recs:
        return {"status": "skip", "note": "没有检索结果，跳过对比矩阵"}
    try:
        rows = sources.to_rows(sources.dedup(recs))
    except Exception as e:
        return {"status": "fail", "error": "矩阵生成失败：%s" % e}
    rows.sort(key=lambda r: (r.get("year") or 0), reverse=True)
    maxn = max(3, min(50, int(opts.get("matrix_max") or 15)))
    md = ra_matrix.render(rows[:maxn], "文献对比矩阵 · " + (cache.get("query") or ""),
                          [cache.get("query") or ""])
    cache["matrix_md"] = md
    return {"status": "ok", "note": "已生成 %d 行对比矩阵" % min(len(rows), maxn)}


def _abs_of(p):
    return " ".join(x for x in [p.get("title") or "", p.get("venue") or "",
                                str(p.get("year") or ""), p.get("abstract") or ""] if x).strip()


def _run_digest(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "精读分析需要 AI。" + _AI_HINT}
    recs = cache.get("records") or []
    if not recs:
        return {"status": "skip", "note": "没有检索结果，跳过精读"}
    # 取前 N 篇有摘要的；缺摘要时尝试自动补齐
    top_n = max(1, min(8, int(opts.get("digest_n") or 3)))
    pool = [p for p in recs if len((p.get("abstract") or "").strip()) >= 80]
    if len(pool) < top_n:
        for p in recs:
            if len(pool) >= top_n:
                break
            if p in pool:
                continue
            a = _fetch_abs(p)
            if a:
                p["abstract"] = a
                pool.append(p)
    pool = pool[:top_n]
    if not pool:
        return {"status": "warn", "note": "前几篇文献都缺摘要且自动补齐失败，无法精读（可手动补摘要后重跑）"}
    parts, ok_n, fail_n, first_err = [], 0, 0, ""
    for i, p in enumerate(pool, 1):
        text = _abs_of(p)
        if len(text) < 100:
            continue
        chunk = ["### %d. %s（%s）" % (i, p.get("title") or "", p.get("year") or "—")]
        for kind in ("gap", "feasibility"):
            zh = "研究空白分析" if kind == "gap" else "可行性分析"
            try:
                reply = model.quick_ask(text[:12000],
                                        system=DIGEST_PROMPTS[kind] +
                                        " 回答用 Markdown；只依据给定文本，不引入文本之外的文献或数据。",
                                        max_tokens=1200, temperature=0.3)
                chunk.append("#### %s\n\n%s" % (zh, reply))
                ok_n += 1
            except Exception as e:
                chunk.append("#### %s\n\n（生成失败：%s）" % (zh, e))
                fail_n += 1
                first_err = first_err or str(e)
        parts.append("\n\n".join(chunk))
    if not ok_n:
        return {"status": "fail", "error": "精读分析全部失败：%s" % (first_err or "模型调用失败")}
    if not parts:
        return {"status": "fail", "error": "精读未产生任何结果"}
    # 汇总一句结论
    summary = ""
    try:
        summary = model.quick_ask("以下是多篇文献的空白分析与可行性分析，请汇总输出（Markdown）："
                                  "## 领域共识 → ## 尚未被覆盖的空白（按可信度排序）→ ## 本方向可行的切入点建议\n\n"
                                  + "\n\n".join(parts)[:14000],
                                  system="你是严谨的科研综述助手（中文），只依据给定材料，不编造文献。",
                                  max_tokens=1200, temperature=0.3)
    except Exception:
        pass
    cache["digest_md"] = "\n\n".join(parts)
    cache["digest_summary"] = summary
    note = "已精读 %d 篇（空白 + 可行性）" % len(parts)
    if fail_n:
        note += "，%d 次调用失败" % fail_n
    return {"status": "ok" if ok_n else "warn", "note": note,
            "detail": (summary or cache["digest_md"])[:4000]}


def _fetch_abs(p):
    """缺摘要时按 DOI 精确补齐（复用文献库的实现）。"""
    try:
        r = wb.rf_fetch_abstract(p.get("title") or "", p.get("link") or "")
        return r.get("abstract") if r.get("ok") else ""
    except Exception:
        return ""


def _run_socratic(by, opts, cache):
    """苏格拉底问询：题目合格性的**必要关卡**。"""
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai",
                "error": "苏格拉底问询需要 AI（这是题目合格性的必要关卡，不能用规则替代）。" + _AI_HINT}
    direction = cache.get("direction") or ""
    if not direction:
        return {"status": "fail", "error": "缺少研究方向，无法问询"}
    evidence = cache.get("digest_summary") or cache.get("digest_md") or ""
    body = ("研究方向：%s\n\n调研与精读发现（空白/可行性）：\n%s" % (direction, evidence[:5000])).strip()
    r = wb.skill_run("socratic_idea", body)
    if not r.get("ok"):
        return {"status": "fail", "error": r.get("error") or "问询生成失败"}
    cache["socratic_md"] = r.get("reply") or ""
    cache["socratic_input"] = body[:4000]
    qn = len(re.findall(r"^\s*(?:\d+[\.、)]|[①②③④⑤⑥⑦⑧⑨⑩]|[-*])\s*", cache["socratic_md"], re.M))
    return {"status": "ok", "note": "已生成苏格拉底追问（约 %d 条）——题目须逐条回应后才算合格" % max(qn, 1),
            "detail": cache["socratic_md"][:4000]}


_TOPIC_SYS = (
    "你是研究选题教练。用户给出研究方向、文献调研/精读结论，以及一份苏格拉底式追问清单。"
    "请**逐条回答这些追问**，再据此收敛出最终论文题目，并自评合格性。"
    "合格标准：每条追问都有明确、具体、可检验的回应；题目一句话能说清「用什么方法解决什么问题、比谁好在哪」。"
    "返回 STRICT JSON（无代码栅栏）："
    '{"qa":[{"q":"追问原文","a":"你的回应"}],'
    '"topic":"最终论文题目（中文，具体可检索）","topic_en":"English title",'
    '"hypothesis":"核心假设/命题（一句可检验的话）",'
    '"innovations":["创新点1","创新点2"],'
    '"gap_type":"工具缺口|证据缺口|理解缺口",'
    '"operational_def":"核心概念的操作性定义与度量方式",'
    '"feasibility":"可行性结论（数据/算力/时间）",'
    '"self_check":{"qualified":true,"missing":["仍未回应的点"],"reason":"判定理由"},'
    '"rationale":"为什么这个题目值得做（3 句内）"}。'
    "不编造文献与数据；无法确定处写【需核实】。"
)


def _run_topic(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "生成题目需要 AI。" + _AI_HINT}
    soc = cache.get("socratic_md") or ""
    if not soc:
        return {"status": "fail", "error": "尚未完成苏格拉底问询——按规则，未经问询的题目不合格，不能进入下一步"}
    body = ("研究方向：%s\n\n调研与精读结论：\n%s\n\n苏格拉底追问清单：\n%s"
            % (cache.get("direction") or "",
               (cache.get("digest_summary") or cache.get("digest_md") or "")[:6000], soc[:6000]))
    try:
        raw = model.quick_ask(body, system=_TOPIC_SYS, max_tokens=2000, temperature=0.3)
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception as e:
        return {"status": "fail", "error": "题目生成/解析失败（可重试）：%s" % e}
    sc = data.get("self_check") or {}
    qa = data.get("qa") or []
    qualified = bool(sc.get("qualified")) and bool(qa) and not (sc.get("missing") or [])
    topic = {
        "topic": (data.get("topic") or "").strip(),
        "topic_en": (data.get("topic_en") or "").strip(),
        "hypothesis": (data.get("hypothesis") or "").strip(),
        "innovations": [str(x).strip() for x in (data.get("innovations") or []) if str(x).strip()],
        "gap_type": (data.get("gap_type") or "").strip(),
        "operational_def": (data.get("operational_def") or "").strip(),
        "feasibility": (data.get("feasibility") or "").strip(),
        "rationale": (data.get("rationale") or "").strip(),
        "qa": [{"q": str(x.get("q", "")), "a": str(x.get("a", ""))} for x in qa if isinstance(x, dict)],
        "self_check": {"qualified": qualified, "missing": sc.get("missing") or [],
                       "reason": sc.get("reason") or ""},
        "qualified": qualified,
        "ts": _now(),
    }
    if not topic["topic"]:
        return {"status": "fail", "error": "AI 未给出题目，请重试"}
    cache["topic"] = topic
    if qualified:
        try:
            store.save_profile_extra({"innovations": "\n".join(topic["innovations"])[:1500],
                                      "direction_en": topic["topic_en"][:200]})
        except Exception:
            pass
        return {"status": "ok", "note": "已产出并通过问询的合格题目：%s" % topic["topic"],
                "detail": _topic_md(topic)}
    return {"status": "warn",
            "note": "已产出题目《%s》，但自评为**尚未完全合格**：%s" % (topic["topic"], topic["self_check"]["reason"]),
            "detail": _topic_md(topic)}


def _topic_md(t):
    if not t:
        return ""
    lines = ["## 候选题目", "", "**%s**" % t.get("topic"), ""]
    if t.get("topic_en"):
        lines += ["*%s*" % t["topic_en"], ""]
    lines += ["- 合格判定：**%s**（%s）" % ("合格 ✅" if t.get("qualified") else "待完善 ⚠️",
                                        t.get("self_check", {}).get("reason") or "—")]
    if t.get("hypothesis"):
        lines.append("- 核心假设：%s" % t["hypothesis"])
    if t.get("gap_type"):
        lines.append("- Gap 类型：%s" % t["gap_type"])
    if t.get("innovations"):
        lines += ["- 创新点："] + ["  - %s" % x for x in t["innovations"]]
    if t.get("operational_def"):
        lines.append("- 操作性定义：%s" % t["operational_def"])
    if t.get("feasibility"):
        lines.append("- 可行性：%s" % t["feasibility"])
    if t.get("rationale"):
        lines.append("- 选题理由：%s" % t["rationale"])
    miss = t.get("self_check", {}).get("missing") or []
    if miss:
        lines += ["", "**仍未回应（需补问）**："] + ["- %s" % x for x in miss]
    qa = t.get("qa") or []
    if qa:
        lines += ["", "## 苏格拉底问询与逐条回应（题目合格依据）", ""]
        for i, x in enumerate(qa, 1):
            lines += ["**Q%d. %s**" % (i, x.get("q")), "", "> %s" % x.get("a"), ""]
    return "\n".join(lines)


def _run_review(by, opts, cache):
    """把「合格题目」提交人工审核（BPM 流程中心）。"""
    t = cache.get("topic") or {}
    if not t.get("topic"):
        return {"status": "skip", "note": "尚无题目，不提交审核"}
    if not t.get("qualified"):
        return {"status": "skip", "note": "题目尚未通过苏格拉底问询（不合格），**按规则不提交人工审核**"}
    if not opts.get("auto_review", True):
        return {"status": "skip", "note": "已按要求跳过自动提交（题目合格，可手动提交）"}
    defn = next((d for d in bpm.def_list(by) if d.get("name") == "论文选题审核"), None)
    if not defn:
        return {"status": "fail", "error": "找不到内置流程「论文选题审核」，请到「流程中心 → 流程设计」确认"}
    recs = cache.get("records") or []
    evidence = "\n".join("- %s（%s）" % (p.get("title"), p.get("year") or "—")
                        for p in recs[:12] if p.get("title"))
    qa_txt = "\n".join("Q: %s\nA: %s" % (x.get("q"), x.get("a")) for x in (t.get("qa") or []))
    form = {
        "topic": t.get("topic"),
        "direction": cache.get("direction") or "",
        "hypothesis": t.get("hypothesis") or "",
        "innovations": "\n".join(t.get("innovations") or []),
        "feasibility": t.get("feasibility") or "",
        "socratic": qa_txt[:4000],
        "evidence": evidence,
    }
    r = bpm.wf_start(defn["id"], by, title="选题审核：" + t["topic"], form=form,
                     business_key="TOPIC-" + time.strftime("%Y%m%d%H%M"))
    if not r.get("ok"):
        return {"status": "fail", "error": r.get("error") or "提交审核失败"}
    iid = (r.get("instance") or {}).get("id")
    cache["review_inst"] = iid
    return {"status": "ok", "note": "已提交人工审核（流程实例 %s）——审核在「🔀 流程中心 → 我的待办」" % iid,
            "inst_id": iid}


_RUNNERS = {
    "direction": _run_direction, "search": _run_search, "collect": _run_collect,
    "matrix": _run_matrix, "digest": _run_digest, "socratic": _run_socratic,
    "topic": _run_topic, "review": _run_review,
}


# ================================================================ 编排
def _mark_guide(by, stages):
    """把成功环节回写「论文十步走」进度（对话 ↔ 十步走 联动的关键）。"""
    per_step = {}
    for s in STAGES:
        per_step.setdefault(s["step"], []).append((stages.get(s["id"]) or {}).get("status"))
    done = wb.guide_load(by) or {}
    changed = False
    for step, sts in per_step.items():
        if any(x in ("fail", "blocked", "warn") for x in sts):
            continue
        if any(x == "ok" for x in sts):
            if not done.get(step):
                done[step] = _now()
                changed = True
    if changed:
        wb.guide_save(by, done)
    return done


def run(by, opts=None):
    """跑全流程。opts: {resume_text, query, sources, limit, collect_n, matrix_max,
    digest_n, folder_name, auto_review, only:[stage_id], force:bool}"""
    opts = opts or {}
    only = [x for x in (opts.get("only") or []) if x in _STAGE_BY_ID]
    force = bool(opts.get("force"))
    prev = _u(by)
    stages = dict(prev.get("stages") or {})
    cache = dict(prev.get("cache") or {})
    cache["direction"] = cache.get("direction") or prev.get("direction") or ""
    results = {}

    for sid in _STAGE_ORDER:
        st = _STAGE_BY_ID[sid]
        if only and sid not in only:
            results[sid] = stages.get(sid) or {"status": "todo"}
            continue
        # 已成功且非强制 → 跳过（支持「继续全流程」从断点续跑）
        if not force and not only and (stages.get(sid) or {}).get("status") == "ok":
            results[sid] = stages[sid]
            continue
        # 题目生成严格依赖苏格拉底问询：上游未完成时不再浪费一次 AI 调用
        if sid == "topic" and (stages.get("socratic") or {}).get("status") in ("blocked", "fail"):
            r = {"status": "blocked", "needs": "ai",
                 "error": "苏格拉底问询（④）未完成——按规则，未经问询的题目不合格，故不生成题目。"}
            r["ts"] = _now()
            results[sid] = r
            stages[sid] = r
            continue
        try:
            r = _RUNNERS[sid](by, opts, cache) or {"status": "fail", "error": "无返回"}
        except Exception as e:
            r = {"status": "fail", "error": "%s：%s" % (type(e).__name__, e)}
        # 把「模型不可用」类错误统一归类为 blocked（引导配置后从断点续跑，而非报成失败）
        if r.get("status") == "fail" and _KEYERR.search(str(r.get("error") or "")):
            r = {"status": "blocked", "needs": "ai",
                 "error": "模型暂不可用：%s" % str(r["error"])[:140],
                 "hint": _AI_HINT}
        r["ts"] = _now()
        results[sid] = r
        stages[sid] = r
        if r.get("status") == "blocked":
            # 后续 AI 环节也会 blocked，不必再逐个调用
            pass

    data = {
        "stages": stages,
        "cache": _slim_cache(cache),
        "topic": cache.get("topic") or prev.get("topic"),
        "direction": cache.get("direction") or "",
        "query": cache.get("query") or "",
        "review_inst": cache.get("review_inst") or "",
        "ts": _now(),
    }
    _set_user(by, data)
    guide = _mark_guide(by, stages)
    return {"ok": True, "stages": state(by)["stages"], "results": results,
            "topic": data["topic"], "direction": data["direction"],
            "review_inst": data["review_inst"], "guide_steps": guide,
            "report_md": report_md(state(by)), "cache": state(by)["cache_counts"]}


def _slim_cache(cache):
    """落盘裁剪：records 只留前 40 条，长文本截断。"""
    out = dict(cache)
    recs = out.get("records") or []
    out["records"] = recs[:40]
    for k in ("matrix_md", "digest_md", "socratic_md"):
        if out.get(k) and len(out[k]) > 20000:
            out[k] = out[k][:20000]
    return out


_STATUS_ICON = {"ok": "✅", "warn": "⚠️", "blocked": "⛔", "fail": "❌", "skip": "⏭️", "todo": "·"}


def report_md(st):
    """把流程状态渲染成 Markdown 报告单。"""
    lines = ["# 全流程报告", "", "> 交付规则：**只有经过苏格拉底问询并逐条回应的题目才合格**，合格后才提交人工审核。", "",
             "| 环节 | 状态 | 说明 |", "|---|---|---|"]
    for s in st.get("stages") or []:
        icon = _STATUS_ICON.get(s.get("status"), "·")
        txt = s.get("note") or s.get("error") or ""
        lines.append("| %s | %s %s | %s |" % (s.get("name"), icon, s.get("status"), txt[:160]))
    if st.get("direction"):
        lines += ["", "**研究方向**：%s" % st["direction"]]
    if st.get("query"):
        lines += ["", "**检索式**：%s" % st["query"]]
    t = st.get("topic")
    if t:
        lines += ["", _topic_md(t)]
    if st.get("review_inst"):
        lines += ["", "**人工审核**：已提交，流程实例 `%s`（在「🔀 流程中心 → 我的待办」处理）" % st["review_inst"]]
    blocked = [s for s in (st.get("stages") or []) if s.get("status") == "blocked"]
    if blocked:
        lines += ["", "## 如何继续", "",
                  "以下环节需要 AI，配置模型后点「⚡ 一键全流程 / 继续全流程」即可**从断点续跑**"
                  "（前面已完成的环节不会重跑）：", ""] + ["- %s" % s["name"] for s in blocked]
        lines += ["", "> 不需要 AI 的环节（文献检索 / 收录文献库 / 对比矩阵 / 提交审核）已照常完成，无需重跑。"]
    return "\n".join(lines)
