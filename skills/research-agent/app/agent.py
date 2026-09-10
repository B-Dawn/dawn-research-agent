#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""破晓 Agent 内核：把「关键词路由」升级为「规划 → 工具调用 → 观察 → 反思」的智能体循环。

设计依据（2026 主流 Agent 架构共识）：
    Agent = LLM(大脑) + Planning(规划) + Memory(记忆) + Tool Use(工具调用)

借鉴的范式与对应的工程约束：
  · ReAct            思考→行动→观察循环；**必须有 max_steps 上限 + 相同调用去重 + 放弃指引**，
                     否则会反复用略不同的参数调同一个工具直到烧完额度。
  · Plan-and-Execute 先出计划再执行；计划对用户可见可审计；执行失败/偏离时**重规划**。
  · Reflection       生成→评审→修正；评审必须是**窄而具体**的检查项
                     （"是否回答了目标/是否基于真实工具输出/是否还缺一步"），
                     宽泛的"看起来还行吗"只会变成幻觉放大器。
  · Router + Agent   廉价确定性路由处理常见请求（零 token），复杂/长程目标才进 LLM 循环。
  · 记忆分层          工作记忆(scratchpad，落盘) / 语义记忆(长期记忆) / 情景记忆(对话历史)。
  · 工具 schema 优先   工具描述比推理逻辑更决定可靠性——每个工具都写清用途与参数。

关键纪律：
  1. **无模型也能多步执行**：规则规划器覆盖常见组合意图，保证「流程联动」在离线时依然成立。
  2. 不编造：工具返回什么就说什么；回答里区分「来自工具的真实结果」与「模型推断」。
  3. 全程留痕：返回 trace（思考/动作/观察），让用户看得见智能体在干什么。
"""

import json
import os
import re
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import model_bridge as model
import workbench as wb
import bpm
import pipeline
import team_store as store

import lib.net as net
from lib import sources
import ra_matrix
import ra_journal

_LOCK = threading.Lock()
_DATA_DIR = os.path.abspath(os.path.join(_HERE, "..", "data"))
_FILE = os.path.join(_DATA_DIR, "agent.json")
_EMPTY = {"users": {}}

MAX_STEPS = 6          # 单轮最大工具调用数（防跑飞）
MAX_LLM_CALLS = 8      # 单轮最大模型调用数
MAX_REFLECT = 1        # 反思后最多再补一轮

_AI_HINT = "（该步需要 AI：请到「🔧 模型与API」配置可用模型；不需要 AI 的工具已照常执行。）"


# ================================================================ 存储（工作记忆）
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


def _user(by):
    return _load().get("users", {}).get(by or "", {})


def scratch(by):
    """工作记忆：跨轮保留的中间结果（上一次检索、最近题目等）。"""
    return (_user(by).get("scratch") or {})


def _push_turn(by, turn):
    def fn(obj):
        u = obj.setdefault("users", {}).setdefault(by or "", {})
        turns = u.setdefault("turns", [])
        turns.append(turn)
        u["turns"] = turns[-20:]          # 只留最近 20 轮（情景记忆）
        u["scratch"] = turn.get("scratch") or u.get("scratch") or {}
        u["ts"] = _now()
        return obj
    with _LOCK:
        _save(fn(_load()))


def history(by, n=8):
    return (_user(by).get("turns") or [])[-n:]


# ================================================================ 工具注册表
def _tool(name, desc, params, handler):
    return {"name": name, "desc": desc, "params": params, "handler": handler}


def _t_search(by, args, ctx):
    q = (args.get("query") or "").strip()
    if len(q) < 3:
        return {"ok": False, "summary": "检索式太短", "error": "请给出检索关键词"}
    srcs = [s.strip() for s in str(args.get("sources") or "arxiv,openalex,crossref,s2").split(",") if s.strip()]
    limit = max(3, min(60, int(args.get("limit") or 20)))
    recs = sources.search(q, sources=srcs, limit=limit,
                          year_from=args.get("year_from"), year_to=args.get("year_to"))
    recs.sort(key=lambda r: (-(r.get("citations") or 0), -(r.get("year") or 0)))
    ctx["records"] = recs
    ctx["query"] = q
    top = recs[:8]
    md = ["**检索到 %d 篇**（检索式：%s）" % (len(recs), q), ""] + [
        "%d. %s（%s，%s%s）" % (i, r.get("title") or "", r.get("year") or "—",
                               r.get("source") or "", "，被引 %s" % r.get("citations")
                               if r.get("citations") else "")
        for i, r in enumerate(top, 1)]
    if len(recs) > 8:
        md.append("…… 其余 %d 篇请在「文献检索」查看完整清单。" % (len(recs) - 8))
    return {"ok": True, "summary": "检索命中 %d 篇" % len(recs), "markdown": "\n".join(md),
            "data": {"count": len(recs), "query": q}}


def _t_matrix(by, args, ctx):
    recs = ctx.get("records") or scratch(by).get("records") or []
    if not recs:
        return {"ok": False, "summary": "没有可用的检索结果", "error": "请先检索文献，或先跑一次「文献检索」"}
    rows = sources.to_rows(sources.dedup(recs))
    rows.sort(key=lambda r: (r.get("year") or 0), reverse=True)
    maxn = max(3, min(50, int(args.get("max_rows") or 15)))
    md = ra_matrix.render(rows[:maxn], "文献对比矩阵 · " + (ctx.get("query") or ""),
                          [ctx.get("query") or ""])
    return {"ok": True, "summary": "生成 %d 行对比矩阵" % min(len(rows), maxn), "markdown": md}


def _t_collect(by, args, ctx):
    recs = ctx.get("records") or scratch(by).get("records") or []
    if not recs:
        return {"ok": False, "summary": "没有可收录的文献", "error": "请先检索文献"}
    q = ctx.get("query") or scratch(by).get("query") or "自动收录"
    fname = (args.get("folder") or ("对话收录 · " + q[:20])).strip()[:60]
    n = max(1, min(100, int(args.get("n") or 30)))
    folders = wb.rf_list(by)
    fid = next((f.get("id") for f in folders if f.get("name") == fname), None)
    if not fid:
        r = wb.rf_create(by, fname)
        fid = ((r.get("folders") or [{}])[0]).get("id")
    papers = [{"title": p.get("title"), "year": p.get("year"), "venue": p.get("venue"),
               "abstract": p.get("abstract") or "", "link": p.get("link") or "",
               "source": p.get("source") or ""} for p in recs[:n] if p.get("title")]
    r2 = wb.rf_add_papers(fid, by, papers)
    ctx["folder_id"] = fid
    return {"ok": True, "summary": "收录 %d 篇 →「文献库 · %s」" % (r2.get("added") or 0, fname),
            "markdown": "- 已收录 **%d** 篇到「文献库 · %s」（到「文献库」可批量精读/综合分析）"
                        % (r2.get("added") or 0, fname)}


_DIGEST = {
    "gap": ("研究空白分析", "## 论文自己承认的局限 → ## 从文本推断的未覆盖点（注明依据）→ ## 可延伸方向"),
    "feasibility": ("可行性分析", "## 复现所需资源 → ## 难点与风险 → ## 移植到本课题的适配建议"),
    "innovation": ("创新点总结", "## 文中声称的创新点 → ## 实际支撑程度 → ## 与已有工作的差异"),
    "tech": ("技术点分析", "## 方法框架 → ## 关键技术 → ## 实验设计要点"),
}


def _t_digest(by, args, ctx):
    if not model.status()["ok"]:
        return {"ok": False, "summary": "精读需要 AI", "error": "精读分析需要模型。" + _AI_HINT}
    kinds = [k for k in (args.get("kinds") or ["gap", "feasibility"]) if k in _DIGEST]
    top_n = max(1, min(5, int(args.get("top_n") or 3)))
    text = (args.get("text") or "").strip()
    picked = []
    if text and len(text) >= 100:
        picked = [(args.get("title") or "（用户粘贴的文本）", text)]
    else:
        recs = ctx.get("records") or scratch(by).get("records") or []
        if not recs:
            return {"ok": False, "summary": "没有可精读的文献", "error": "请先检索文献，或直接粘贴论文文本"}
        for p in recs[:top_n]:
            body = " ".join(x for x in [p.get("title") or "", p.get("venue") or "",
                                        str(p.get("year") or ""), p.get("abstract") or ""] if x).strip()
            if len(body) < 100:
                a = _abs(p)
                if a:
                    body = (p.get("title") or "") + ". " + a
            if len(body) >= 100:
                picked.append((p.get("title") or "", body))
    if not picked:
        return {"ok": False, "summary": "文献缺摘要，无法精读", "error": "前几篇缺摘要且自动补齐失败"}
    out, ok_n, err = [], 0, ""
    for i, (title, body) in enumerate(picked, 1):
        block = ["#### %d. %s" % (i, title)]
        for k in kinds:
            zh, brief = _DIGEST[k]
            try:
                reply = model.quick_ask(body[:12000],
                                        system="请基于给定论文文本做**%s**（Markdown）：%s。"
                                               "只依据文本真实内容，未写明处标注【原文未详】。" % (zh, brief),
                                        max_tokens=1000, temperature=0.3)
                block.append("**%s**\n\n%s" % (zh, reply))
                ok_n += 1
            except Exception as e:
                block.append("**%s**：（失败：%s）" % (zh, e))
                err = err or str(e)
        out.append("\n\n".join(block))
    if not ok_n:
        return {"ok": False, "summary": "精读失败", "error": err or "模型调用失败"}
    md = "\n\n".join(out)
    ctx["digest_md"] = md
    return {"ok": True, "summary": "精读 %d 篇 × %d 维度" % (len(picked), len(kinds)), "markdown": md}


def _abs(p):
    try:
        r = wb.rf_fetch_abstract(p.get("title") or "", p.get("link") or "")
        return r.get("abstract") if r.get("ok") else ""
    except Exception:
        return ""


def _t_abstract(by, args, ctx):
    title = (args.get("title") or "").strip()
    if not title:
        recs = ctx.get("records") or []
        if not recs:
            return {"ok": False, "summary": "缺少标题", "error": "请给出文献标题，或先检索"}
        title = recs[0].get("title") or ""
    r = wb.rf_fetch_abstract(title, args.get("link") or "")
    if not r.get("ok"):
        return {"ok": False, "summary": "未找到摘要", "error": r.get("error") or "外部数据源无摘要"}
    return {"ok": True, "summary": "已获取摘要（%s，%d 字）" % (r.get("source"), len(r.get("abstract"))),
            "markdown": "**%s**\n\n> %s" % (title, r["abstract"][:900])}


def _t_journal(by, args, ctx):
    text = (args.get("text") or "").strip()
    if len(text) < 40:
        recs = ctx.get("records") or scratch(by).get("records") or []
        pool = " ".join((p.get("abstract") or p.get("title") or "") for p in recs[:5])
        text = (text + " " + pool).strip()
    if len(text) < 40:
        return {"ok": False, "summary": "材料不足", "error": "选刊需要摘要/关键词（≥40 字），或先检索文献"}
    db_path = ra_journal.DEFAULT_DB
    if not os.path.isfile(db_path):
        return {"ok": False, "summary": "期刊库缺失", "error": "期刊库不存在：%s" % db_path}
    db = net.read_json(db_path)
    journals = db.get("journals") or []
    target_if = float(args.get("target_if") or 7.0)
    tokens = ra_journal._tokenize(text)
    scored = [ra_journal.score_journal(j, tokens, target_if, text) for j in journals]
    scored.sort(key=lambda s: s["total"], reverse=True)
    md = ra_journal.render(scored[:8], target_if, text.strip())
    return {"ok": True, "summary": "已按契合度排序 %d 个候选期刊" % min(len(scored), 8), "markdown": md,
            "warning": "IF/分区为参考值，须以最新 JCR 核实。"}


def _t_profile_get(by, args, ctx):
    pe = {}
    try:
        pe = store.load_profile_extra() or {}
    except Exception:
        pass
    wfd = wb.wf_get_direction(by) or {}
    lines = ["**科研画像**", ""]
    for k, zh in (("direction_cn", "研究方向"), ("direction_en", "英文方向"), ("skills", "技能栈"),
                  ("innovations", "创新点"), ("constraints", "约束"), ("goals", "目标"),
                  ("target_journals", "目标期刊"), ("works", "已有成果")):
        if pe.get(k):
            lines.append("- %s：%s" % (zh, pe[k]))
    if wfd.get("direction_final"):
        lines.append("- 最终方向：%s" % wfd["direction_final"])
    if len(lines) == 2:
        lines.append("（画像为空——到「科研画像」填写，或直接告诉我你的方向/技能）")
    return {"ok": True, "summary": "已读取科研画像", "markdown": "\n".join(lines)}


def _t_profile_set(by, args, ctx):
    data = {k: str(v)[:1500] for k, v in (args.get("fields") or {}).items()
            if k in ("direction_cn", "direction_en", "skills", "innovations", "constraints",
                     "goals", "target_journals", "works")}
    if not data:
        return {"ok": False, "summary": "没有可写入的字段", "error": "请给出要写入画像的内容"}
    store.save_profile_extra(data)
    return {"ok": True, "summary": "已更新画像 %d 个字段" % len(data),
            "markdown": "已写入「科研画像」：" + "、".join(data.keys())}


def _t_remember(by, args, ctx):
    text = (args.get("text") or "").strip()
    if not text:
        return {"ok": False, "summary": "记忆内容为空", "error": "请给出要记住的内容"}
    r = wb.mem_add(text, kind=args.get("kind") or "note", by=by)
    if not r.get("ok"):
        return {"ok": False, "summary": "写入失败", "error": r.get("error")}
    return {"ok": True, "summary": "已写入长期记忆", "markdown": "已记住：%s" % text[:120]}


def _t_recall(by, args, ctx):
    es = wb.mem_list(by=by)
    if not es:
        return {"ok": True, "summary": "暂无长期记忆", "markdown": "你还没有长期记忆条目。"}
    md = ["**长期记忆（%d 条）**" % len(es), ""] + [
        "- [%s｜%s] %s" % (e.get("ts", ""), e.get("kind_zh", "备忘"), (e.get("text") or "")[:100])
        for e in es[:15]]
    return {"ok": True, "summary": "读取 %d 条记忆" % len(es), "markdown": "\n".join(md)}


def _t_library(by, args, ctx):
    fs = wb.rf_list(by)
    if not fs:
        return {"ok": True, "summary": "文献库为空", "markdown": "文献库还没有文件夹。检索后可一键收录。"}
    md = ["**文献库**", ""] + [
        "- %s：%d 篇%s" % (f.get("name"), len(f.get("papers") or []),
                         "，已精读 %d 篇" % len(f.get("analyses") or {}) if f.get("analyses") else "")
        for f in fs]
    return {"ok": True, "summary": "文献库 %d 个文件夹" % len(fs), "markdown": "\n".join(md)}


def _t_todo(by, args, ctx):
    items = bpm.todo(by)
    if not items:
        return {"ok": True, "summary": "无待办", "markdown": "你当前没有待办审批任务。"}
    md = ["**我的待办（%d）**" % len(items), ""] + [
        "- %s — 流程「%s」· 节点：%s · 发起人：%s" % (t.get("inst_title"), t.get("def_name"),
                                                   t.get("name"), t.get("initiator"))
        for t in items[:12]]
    md.append("\n> 到「🔀 流程中心 → 我的待办」办理（通过/驳回/转办/委派/加签/抄送）。")
    return {"ok": True, "summary": "待办 %d 条" % len(items), "markdown": "\n".join(md)}


def _t_start_approval(by, args, ctx):
    name = (args.get("def_name") or "").strip()
    defs = bpm.def_list(by)
    defn = next((d for d in defs if d.get("name") == name), None) if name else (defs[0] if defs else None)
    if not defn:
        names = "、".join(d.get("name") for d in defs) or "（无）"
        return {"ok": False, "summary": "找不到流程", "error": "可用流程：%s" % names}
    form = {k: str(v)[:1500] for k, v in (args.get("form") or {}).items()}
    r = bpm.wf_start(defn["id"], by, title=args.get("title") or form.get("title") or defn["name"],
                     form=form, business_key=args.get("business_key") or "")
    if not r.get("ok"):
        return {"ok": False, "summary": "发起失败", "error": r.get("error")}
    iid = (r.get("instance") or {}).get("id")
    return {"ok": True, "summary": "已发起「%s」(%s)" % (defn["name"], iid),
            "markdown": "已发起流程 **%s**（实例 %s），已推送至首个审批节点；到「🔀 流程中心 → 我的待办」查看。"
                        % (defn["name"], iid)}


def _t_pipeline(by, args, ctx):
    only = args.get("only") or []
    r = pipeline.run(by, {"only": only, "force": bool(args.get("force")) or bool(only),
                          "query": args.get("query") or "", "digest_n": args.get("digest_n") or 3,
                          "collect_n": args.get("collect_n") or 30})
    okc = sum(1 for s in r["stages"] if s.get("status") == "ok")
    return {"ok": True, "summary": "全流程完成 %d/%d 环节" % (okc, len(r["stages"])),
            "markdown": r["report_md"], "data": {"topic": r.get("topic"), "inst": r.get("review_inst")}}


def _t_socratic(by, args, ctx):
    if not model.status()["ok"]:
        return {"ok": False, "summary": "苏格拉底问询需要 AI", "error": "苏格拉底问询需要模型。" + _AI_HINT}
    body = (args.get("idea") or "").strip()
    if len(body) < 10:
        pe = {}
        try:
            pe = store.load_profile_extra() or {}
        except Exception:
            pass
        body = (ctx.get("direction") or (wb.wf_get_direction(by) or {}).get("direction_final")
                or pe.get("direction_cn") or "")
        if ctx.get("digest_md"):
            body += "\n\n调研发现：\n" + ctx["digest_md"][:4000]
    if len(body) < 10:
        return {"ok": False, "summary": "缺少研究方向", "error": "请先给出研究方向（或先在「科研画像」填写）"}
    r = wb.skill_run("socratic_idea", body)
    if not r.get("ok"):
        return {"ok": False, "summary": "问询失败", "error": r.get("error")}
    ctx["socratic_md"] = r["reply"]
    return {"ok": True, "summary": "已生成苏格拉底追问", "markdown": r["reply"]}


def _t_mark_step(by, args, ctx):
    sid = (args.get("step") or "").strip()
    if not re.match(r"^s(10|[1-9])$", sid):
        return {"ok": False, "summary": "步骤号无效", "error": "step 形如 s1..s10"}
    done = wb.guide_load(by) or {}
    done[sid] = _now()
    wb.guide_save(by, done)
    return {"ok": True, "summary": "已标记 %s 完成" % sid,
            "markdown": "已在「论文十步走」把 **%s** 标记为完成。" % sid}


def _t_experiment(by, args, ctx):
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "summary": "缺少实验名", "error": "请给出实验名称"}
    store.add_experiment({"name": name, "owner": by, "dataset": args.get("dataset") or "",
                          "hypothesis": args.get("hypothesis") or "",
                          "notes": args.get("notes") or ""})
    return {"ok": True, "summary": "已登记实验「%s」" % name,
            "markdown": "已登记实验 **%s** →「实验台账」。" % name}


def _t_ask(by, args, ctx):
    if not model.status()["ok"]:
        return {"ok": False, "summary": "需要模型", "error": "自由问答需要模型。" + _AI_HINT}
    q = (args.get("question") or "").strip()
    if not q:
        return {"ok": False, "summary": "问题为空", "error": "请给出问题"}
    sysp = "你是严谨的科研助手（中文）。只依据给定信息与常识回答，不编造文献与数据；不确定处标注【需核实】。"
    extra = _context_block(by, ctx)
    if extra:
        sysp += "\n\n可参考的用户信息：\n" + extra
    reply = model.quick_ask(q, system=sysp, max_tokens=1200, temperature=0.4)
    return {"ok": True, "summary": "已生成回答", "markdown": reply}


def _t_finish(by, args, ctx):
    return {"ok": True, "summary": "结束", "markdown": (args.get("answer") or "").strip(),
            "terminal": True}


TOOLS = [
    _tool("search_papers", "多源检索文献（arXiv/OpenAlex/Crossref/SemanticScholar），返回真实命中清单",
          {"query": "检索式（建议英文）", "limit": "每源上限，默认20", "year_from": "起始年，可选",
           "year_to": "截止年，可选", "sources": "数据源，逗号分隔"}, _t_search),
    _tool("build_matrix", "基于上一次检索结果生成论文对比矩阵", {"max_rows": "最多行数，默认15"}, _t_matrix),
    _tool("collect_papers", "把检索结果收录进「文献库」文件夹",
          {"folder": "文件夹名，可选", "n": "收录篇数，默认30"}, _t_collect),
    _tool("digest_papers", "精读文献（研究空白/可行性/创新点/技术点）；可用检索结果前 N 篇或用户粘贴的文本",
          {"top_n": "用检索结果前几篇，默认3", "kinds": "维度数组，可选 gap/feasibility/innovation/tech",
           "title": "粘贴文本时的标题", "text": "直接精读的文本"}, _t_digest),
    _tool("fetch_abstract", "按标题/DOI 精确补齐文献摘要",
          {"title": "文献标题", "link": "DOI 链接，可选"}, _t_abstract),
    _tool("match_journal", "按摘要/关键词给候选期刊排序（选刊）",
          {"text": "摘要或关键词，≥40字", "target_if": "目标影响因子，默认7"}, _t_journal),
    _tool("get_profile", "读取用户科研画像与最终方向", {}, _t_profile_get),
    _tool("update_profile", "写入科研画像字段",
          {"fields": "对象，可含 direction_cn/direction_en/skills/innovations/constraints/goals/target_journals/works"},
          _t_profile_set),
    _tool("remember", "写入长期记忆（跨会话可用）", {"text": "记忆内容", "kind": "note|experience|convention"}, _t_remember),
    _tool("recall", "读取长期记忆", {}, _t_recall),
    _tool("list_library", "查看文献库文件夹与篇数", {}, _t_library),
    _tool("my_todo", "查看我的待办审批任务", {}, _t_todo),
    _tool("start_approval", "发起审批流程（流程中心）",
          {"def_name": "流程名，如 论文选题审核/论文送审审批/通用审批", "title": "标题",
           "form": "表单对象", "business_key": "业务单号，可选"}, _t_start_approval),
    _tool("run_pipeline", "跑全流程（①定方向→②调研→③创新点/可行性→④苏格拉底定题→⑤提交人工审核）",
          {"only": "只跑指定环节数组，可选", "force": "是否强制重跑", "query": "检索式", "digest_n": "精读篇数"},
          _t_pipeline),
    _tool("socratic", "苏格拉底式追问（打磨 idea；定题目的必要关卡）", {"idea": "研究想法/方向"}, _t_socratic),
    _tool("mark_step", "把「论文十步走」的某一步标记为完成", {"step": "s1..s10"}, _t_mark_step),
    _tool("register_experiment", "在「实验台账」登记一个实验",
          {"name": "实验名", "dataset": "数据集", "hypothesis": "假设", "notes": "备注"}, _t_experiment),
    _tool("ask_model", "自由问答（会带上画像与长期记忆）", {"question": "问题"}, _t_ask),
    _tool("finish", "任务完成，输出最终答复", {"answer": "Markdown 最终答复"}, _t_finish),
]
_TOOL_MAP = {t["name"]: t for t in TOOLS}
_TOOL_SIG = "、".join(t["name"] for t in TOOLS)   # 供规则匹配与提示词使用


def tool_catalog():
    return [{"name": t["name"], "desc": t["desc"], "params": t["params"]} for t in TOOLS]


# ================================================================ 规则规划器（廉价路由）
# 说明：常见/组合意图先用确定性规则拆成多步计划，无需模型即可完成「流程联动」。
# 顺序敏感：更具体、更"动作化"的意图必须排在更宽泛的意图之前。
_RULES = [
    (["一键全流程", "全流程", "全自动跑", "跑完流程"], lambda g: [
        {"tool": "run_pipeline", "args": {}, "why": "用户要一键跑完全流程"}]),
    (["苏格拉底"], lambda g: [
        {"tool": "socratic", "args": {"idea": _strip(g)}, "why": "苏格拉底问询是定题目的必要关卡"}]),
    (["发起审批", "发起流程", "提交审批"], lambda g: [
        {"tool": "start_approval",
         "args": {"def_name": _pick(g, ["论文选题审核", "论文送审审批", "开题报告审批", "通用审批"]),
                  "title": _strip(g)}, "why": "发起审批流程"}]),
    (["定题目", "确定题目", "定选题", "确定选题", "拟定题目"], lambda g: [
        {"tool": "run_pipeline", "args": {"only": ["socratic", "topic", "review"]},
         "why": "生成题目须先过问询，合格后自动提交人工审核"}]),
    (["我的待办", "待我审批", "待办任务", "待办审批", "待办"], lambda g: [
        {"tool": "my_todo", "args": {}, "why": "查询当前待办"}]),
    (["登记实验", "新建实验", "登记一个实验"], lambda g: [
        {"tool": "register_experiment", "args": {"name": _strip(g)}, "why": "登记实验"}]),
    # ---- 独立的动作型意图（务必在「文献库列表」之前）----
    (["收录", "入库", "存到文献库", "放进文献库"], lambda g: [
        {"tool": "collect_papers", "args": {"folder": _folder_of(g), "n": _limit_of(g)},
         "why": "把检索结果收录进文献库"}]),
    (["矩阵", "对比矩阵"], lambda g: [
        {"tool": "build_matrix", "args": {}, "why": "生成论文对比矩阵"}]),
    (["精读", "空白分析", "可行性分析", "创新点总结"], lambda g: [
        {"tool": "digest_papers", "args": {"kinds": _kinds_of(g), "top_n": _nth(g) or 3},
         "why": "对文献做精读分析"}]),
    (["选刊", "投稿到哪", "投哪", "投哪个期刊"], lambda g: [
        {"tool": "match_journal", "args": {}, "why": "按契合度排序候选期刊"}]),
    (["画像"], lambda g: [{"tool": "get_profile", "args": {}, "why": "读取科研画像"}]),
    (["记忆"], lambda g: [{"tool": "recall", "args": {}, "why": "读取长期记忆"}]),
]
# 「查看文献库」需要同时含"查看类"动词 + 文献库名词，避免误吞"收录到文献库"
_LIB_VERBS = ["查看", "看看", "有哪些", "列出", "显示", "有多少", "我的文献库", "文献库情况"]


def _dedup_steps(steps):
    out, seen = [], set()
    for s in steps:
        sig = s["tool"] + "|" + json.dumps(s.get("args") or {}, ensure_ascii=False, sort_keys=True)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(s)
    return out


def plan_rules(goal):
    """确定性规划：支持「A，然后B，再C」形式的组合意图串联；无模型也能多步执行。"""
    g = (goal or "").strip()
    if not g:
        return []
    # 组合意图：按连接词切分，各自规划后串联（要求至少 2 个子意图都有计划）
    parts = [x.strip() for x in re.split(r"(?:然后|接着|之后|并且|以及|同时|再(?:帮我|来|去)?|并|，|,)",
                                        g) if x.strip() and len(x.strip()) >= 2]
    if len(parts) > 1:
        merged, hit = [], 0
        for p in parts:
            st = _plan_single(p)
            if st:
                merged.extend(st)
                hit += 1
        merged = _dedup_steps(merged)
        if hit >= 2 and len(merged) >= 2:
            return merged[:MAX_STEPS]
    return _plan_single(g)


def _plan_single(g):
    for keys, build in _RULES:
        if any(k in g for k in keys):
            return build(g)
    # 查看文献库
    if "文献库" in g and any(k in g for k in _LIB_VERBS):
        return [{"tool": "list_library", "args": {}, "why": "查看文献库"}]
    # 检索族：检索 →（矩阵 / 收录 / 精读 / 选刊）
    if (any(k in g for k in ["检索", "搜索", "查一下", "查查", "找找", "找一下", "文献", "论文"])
            or re.search(r"\b(paper|search)\b", g, re.I)):
        steps = [{"tool": "search_papers",
                  "args": {"query": _query_of(g), "limit": _limit_of(g)},
                  "why": "先检索拿到真实文献"}]
        if any(k in g for k in ["矩阵", "对比", "比较"]):
            steps.append({"tool": "build_matrix", "args": {}, "why": "用户要对比矩阵"})
        if any(k in g for k in ["收录", "入库", "存到文献库", "放进文献库"]):
            steps.append({"tool": "collect_papers",
                          "args": {"folder": _folder_of(g), "n": _limit_of(g)},
                          "why": "用户要收录入库"})
        if any(k in g for k in ["精读", "分析", "空白", "可行性", "创新点"]):
            steps.append({"tool": "digest_papers",
                          "args": {"kinds": _kinds_of(g), "top_n": _nth(g) or 3},
                          "why": "用户要精读分析"})
        if any(k in g for k in ["选刊", "投稿", "投哪", "期刊"]):
            steps.append({"tool": "match_journal", "args": {}, "why": "用户要选刊"})
        return _dedup_steps(steps)[:MAX_STEPS]
    m = re.match(r"^(?:帮我)?(?:记住|记一下)[：:，,]?\s*(.+)$", g, re.S)
    if m:
        return [{"tool": "remember", "args": {"text": m.group(1).strip()}, "why": "写入长期记忆"}]
    return []


def _kinds_of(g):
    kinds = []
    if any(k in g for k in ["空白", "gap"]):
        kinds.append("gap")
    if "可行性" in g:
        kinds.append("feasibility")
    if "创新点" in g:
        kinds.append("innovation")
    if "技术点" in g:
        kinds.append("tech")
    return kinds or ["gap", "feasibility"]


def _folder_of(g):
    m = re.search(r"[「\"']([^」\"']{2,40})[」\"']", g or "")
    if m:
        return m.group(1)
    m = re.search(r"(?:文件夹|目录)[：:为叫]?\s*([\u4e00-\u9fff\w\- ]{2,30})", g or "")
    return m.group(1).strip() if m else ""


def _has(g, keys):
    return any(k in g for k in keys)


def _strip(g):
    return re.sub(r"^(?:请|帮我|帮忙|麻烦|我想|我要|想|要)+", "", (g or "").strip())[:200]


def _query_of(g):
    q = re.sub(r"(请|帮我|帮忙|麻烦|检索|搜索|搜一下|查一下|查查|找找|找一下|文献|论文|相关的|有关|关于|最新|近[一二三四五六七八九十\d]*年|的)", " ", g or "")
    q = re.sub(r"\s+", " ", q).strip(" ，。、,.")
    if len(q) < 3:
        q = re.sub(r"(文献|论文|检索|搜索)", "", g or "").strip() or (g or "")[:60]
    return q[:200]


def _limit_of(g):
    m = re.search(r"(\d+)\s*篇", g or "")
    return min(60, int(m.group(1))) if m else 20


def _nth(g):
    m = re.search(r"前\s*(\d+)\s*篇", g or "") or re.search(r"(\d+)\s*篇.*?(?:精读|分析)", g or "")
    return int(m.group(1)) if m else 0


def _pick(g, options):
    for o in options:
        if o in (g or ""):
            return o
    return ""


# ================================================================ LLM 规划器
_PLAN_SYS = (
    "你是任务规划器。根据用户目标，从给定工具中选择最少的必要步骤，输出 STRICT JSON（无代码栅栏）："
    '{"thought":"你的整体思路（一句话）",'
    '"steps":[{"tool":"工具名","args":{...},"why":"为什么需要这一步"}]}'
    "规则：① 最多 6 步；② 只使用给定工具名；③ 参数必须符合该工具的参数说明；"
    "④ 不要臆造数据来源，需要事实就先调检索类工具；⑤ 若目标已经简单到一步可答，就只给一步。"
)


def plan_llm(goal, by):
    cats = "\n".join("- %s：%s 参数：%s" % (t["name"], t["desc"],
                                          json.dumps(t["params"], ensure_ascii=False))
                     for t in TOOLS if t["name"] != "finish")
    sysp = _PLAN_SYS + "\n\n可用工具：\n" + cats
    hist = history(by, 4)
    if hist:
        sysp += "\n\n用户最近的诉求（供理解上下文）：\n" + "\n".join("- %s" % h.get("goal", "") for h in hist)
    raw = model.quick_ask(goal, system=sysp, max_tokens=1200, temperature=0.2)
    data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    steps = []
    for s in (data.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(s, dict):
            continue
        name = s.get("tool")
        if name not in _TOOL_MAP or name == "finish":
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        steps.append({"tool": name, "args": args, "why": str(s.get("why") or "")[:120]})
    return steps, str(data.get("thought") or "")[:300]


# ================================================================ 反思（窄评审）
_REFLECT_SYS = (
    "你是任务的验收评审，只做**窄而具体**的检查，返回 STRICT JSON（无代码栅栏）："
    '{"satisfied":true/false,"missing":"哪个点没被满足（没有就空串）",'
    '"next":[{"tool":"工具名","args":{...},"why":"补这一步的理由"}]}'
    "检查项只有三条：① 用户的原始目标是否被真正满足；② 回答是否基于工具返回的真实结果而非臆测；"
    "③ 是否还缺关键的一步。不要泛泛评价质量；不需要补充就给空数组。"
)


def reflect(goal, trace, answer, by):
    cats = "、".join(t["name"] for t in TOOLS if t["name"] != "finish")
    obs = "\n".join("- [%s] %s → %s" % (t.get("tool"), json.dumps(t.get("args") or {}, ensure_ascii=False)[:80],
                                        (t.get("observation") or "")[:120]) for t in trace[-6:])
    body = ("用户目标：%s\n\n已执行步骤：\n%s\n\n当前答复（截断）：\n%s" % (goal, obs or "（无）", (answer or "")[:1500]))
    raw = model.quick_ask(body, system=_REFLECT_SYS + "\n\n可用工具名：" + cats,
                          max_tokens=700, temperature=0.2)
    data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    nxt = []
    for s in (data.get("next") or [])[:2]:
        if isinstance(s, dict) and s.get("tool") in _TOOL_MAP and s.get("tool") != "finish":
            nxt.append({"tool": s["tool"], "args": s.get("args") if isinstance(s.get("args"), dict) else {},
                        "why": "[反思补充] " + str(s.get("why") or "")[:100]})
    return bool(data.get("satisfied")), str(data.get("missing") or "")[:200], nxt


# ================================================================ 执行器
def _context_block(by, ctx):
    parts = []
    try:
        pe = store.load_profile_extra() or {}
        if pe.get("direction_cn"):
            parts.append("研究方向：%s" % pe["direction_cn"])
        if pe.get("skills"):
            parts.append("技能栈：%s" % pe["skills"])
        if pe.get("innovations"):
            parts.append("创新点：%s" % pe["innovations"][:300])
    except Exception:
        pass
    wfd = wb.wf_get_direction(by) or {}
    if wfd.get("direction_final"):
        parts.append("最终方向：%s" % wfd["direction_final"])
    return "\n".join(parts)


def _run_steps(by, steps, ctx, trace, seen):
    """顺序执行步骤；相同 (tool,args) 去重；单步失败不中断（继续后续可能的独立步骤）。"""
    for st in steps:
        if len(trace) >= MAX_STEPS:
            trace.append({"tool": "-", "args": {}, "ok": False,
                          "observation": "已达单轮最大步数 %d，停止执行。" % MAX_STEPS})
            break
        name, args = st.get("tool"), st.get("args") or {}
        sig = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
        if sig in seen:
            trace.append({"tool": name, "args": args, "ok": False, "skipped": True,
                          "observation": "该调用已执行过（去重跳过），避免重复消耗。"})
            continue
        seen.add(sig)
        t0 = time.time()
        t = _TOOL_MAP.get(name)
        if not t:
            trace.append({"tool": name, "args": args, "ok": False, "observation": "未知工具"})
            continue
        try:
            res = t["handler"](by, args, ctx) or {}
        except Exception as e:
            res = {"ok": False, "summary": "执行异常", "error": "%s：%s" % (type(e).__name__, e)}
        trace.append({"tool": name, "args": args, "why": st.get("why") or "",
                      "ok": bool(res.get("ok")), "summary": res.get("summary") or "",
                      "observation": (res.get("markdown") or res.get("error") or "")[:4000],
                      "ms": int((time.time() - t0) * 1000),
                      "terminal": bool(res.get("terminal"))})
        if res.get("terminal"):
            break


def _compose_answer(goal, trace, ctx, by):
    """汇总：把各步 observation 组装成结构化答复（不编造，逐段标注来源）。"""
    if not trace:
        return "我还没能规划出可执行的步骤。可以试试：\n- 检索 UAV 入侵检测 最新论文，然后精读前三篇并帮我选刊\n- 一键全流程\n- 我的待办"
    blocks = []
    for i, t in enumerate(trace, 1):
        if t.get("terminal") or t.get("skipped"):
            continue
        head = "### %d. %s" % (i, _TOOL_MAP.get(t["tool"], {}).get("desc", t["tool"]).split("（")[0])
        if t["tool"] == "finish":
            continue
        body = t.get("observation") or t.get("summary") or "（无输出）"
        if not t.get("ok"):
            body = "⚠️ " + body
        blocks.append("%s\n\n%s" % (head, body))
    got = [t for t in trace if t.get("tool") != "finish" and t.get("ok") and not t.get("skipped")]
    done = "、".join("%s" % t.get("summary") for t in got) or "无"
    return ("**执行摘要**：共 %d 步，完成 %d 步（%s）\n\n" % (len(trace), len(got), done)) + "\n\n".join(blocks)


# ================================================================ 主入口
def run(by, goal, opts=None):
    """跑一轮智能体。返回 {ok, plan, trace, answer_md, module, used_llm, ...}"""
    opts = opts or {}
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "请输入你的目标"}
    ctx = {"records": scratch(by).get("records") or [], "query": scratch(by).get("query") or "",
           "direction": scratch(by).get("direction") or ""}
    trace, seen = [], set()
    ai_ok = model.status()["ok"]
    used_llm = False
    thought = ""

    # ① 规划：规则优先（零成本、可离线），规则给不出计划时才用 LLM
    steps = plan_rules(goal)
    planner = "rules"
    if not steps and ai_ok:
        try:
            steps, thought = plan_llm(goal, by)
            planner = "llm"
            used_llm = True
        except Exception as e:
            steps = []
            thought = "LLM 规划失败：%s" % e
    # 规则只给了单步且模型可用时，仍可让 LLM 看看是否该做更多（长程目标）
    if planner == "rules" and ai_ok and len(steps) == 1 and opts.get("deep", True):
        try:
            s2, th2 = plan_llm(goal, by)
            if len(s2) > len(steps):
                steps, thought, planner, used_llm = s2, th2, "llm(补充)", True
        except Exception:
            pass

    if not steps:
        # ② 规划失败 → 兜底：交给自由问答 / 或提示
        if ai_ok:
            steps = [{"tool": "ask_model", "args": {"question": goal}, "why": "无匹配工具，走自由问答"}]
        else:
            return {"ok": True, "plan": [], "trace": [],
                    "answer_md": "我没能把这个请求拆成可执行的步骤，且当前没有可用模型。\n\n"
                                 "可以试试这些说法（不需要模型也能多步执行）：\n"
                                 "- 检索 UAV 入侵检测 最新论文，然后精读前三篇，再帮我选刊\n"
                                 "- 收录到文献库\n- 我的待办\n- 一键全流程",
                    "module": "agent", "used_llm": False, "planner": planner}

    # ③ 执行
    _run_steps(by, steps, ctx, trace, seen)

    # ④ 反思（窄评审）→ 必要时补一轮（硬上限 MAX_REFLECT）
    rounds = 0
    while ai_ok and rounds < MAX_REFLECT:
        ans = _compose_answer(goal, trace, ctx, by)
        try:
            ok, missing, nxt = reflect(goal, trace, ans, by)
            used_llm = True
        except Exception:
            break
        if ok or not nxt:
            break
        rounds += 1
        trace.append({"tool": "-", "args": {}, "ok": True, "why": "反思",
                      "observation": "评审认为还缺：%s → 补充执行 %d 步" % (missing or "—", len(nxt))})
        _run_steps(by, nxt, ctx, trace, seen)
        break   # 只补一轮，防"反思放大器"

    answer = _compose_answer(goal, trace, ctx, by)
    module = _module_of(trace)

    # ⑤ 写入工作记忆（scratchpad + 情景记忆）
    new_scratch = {}
    if ctx.get("records"):
        new_scratch = {"records": [_trim(r) for r in ctx["records"][:40]],
                       "query": ctx.get("query") or "", "direction": ctx.get("direction") or ""}
    _push_turn(by, {"ts": _now(), "goal": goal, "planner": planner, "steps": len(trace),
                    "answer": answer[:2000], "scratch": new_scratch,
                    "tools": [t.get("tool") for t in trace if t.get("tool") not in ("-", "finish")]})

    return {"ok": True, "plan": steps, "trace": trace, "answer_md": answer,
            "module": module, "used_llm": used_llm, "planner": planner,
            "thought": thought, "steps": len(trace), "reflect_rounds": rounds}


def _trim(r):
    return {"title": r.get("title") or "", "year": r.get("year") or "",
            "venue": r.get("venue") or "", "source": r.get("source") or "",
            "citations": r.get("citations") or 0,
            "abstract": (r.get("abstract") or "")[:1500], "link": r.get("link") or ""}


_MODULE_BY_TOOL = {
    "search_papers": "search", "build_matrix": "matrix", "collect_papers": "reflib",
    "digest_papers": "digest", "fetch_abstract": "reflib", "match_journal": "journal",
    "get_profile": "profile", "update_profile": "profile", "remember": "memory",
    "recall": "memory", "list_library": "reflib", "my_todo": "bpm",
    "start_approval": "bpm", "run_pipeline": "bpm", "socratic": "profile",
    "mark_step": "guide", "register_experiment": "lab", "ask_model": "ask",
}


def _module_of(trace):
    """挑一个最有代表性的模块，供对话下方渲染跳转按钮。"""
    for t in trace:
        m = _MODULE_BY_TOOL.get(t.get("tool"))
        if m and m != "ask":
            return m
    for t in trace:
        if t.get("tool") == "ask_model":
            return "ask"
    return "ask"


def tradec_to_md(trace):
    """把执行轨迹渲染成纯 Markdown（用户可见智能体的「思考—行动—观察」）。

    注意：必须输出纯 Markdown，前端 renderMarkdown 会转义原始 HTML。
    """
    if not trace:
        return ""
    lines = ["**🧠 智能体执行轨迹（%d 步）**" % len(trace), ""]
    for i, t in enumerate(trace, 1):
        name = t.get("tool") or "-"
        icon = "✅" if t.get("ok") else ("⏭️" if t.get("skipped") else "⚠️")
        why = ("（%s）" % t["why"]) if t.get("why") else ""
        lines.append("%d. %s `%s`%s" % (i, icon, name, why))
        if t.get("args"):
            arg = json.dumps(t["args"], ensure_ascii=False)
            if arg not in ("{}", "null"):
                lines.append("   - 参数：`%s`" % arg[:160])
        obs = (t.get("observation") or t.get("summary") or "").replace("\n", " ").strip()
        if obs:
            lines.append("   - 观察:%s" % obs[:200])
    lines.append("")
    return "\n".join(lines)


def reset(by):
    def fn(obj):
        obj.setdefault("users", {})[by or ""] = {"turns": [], "scratch": {}}
        return obj
    with _LOCK:
        _save(fn(_load()))
    return {"ok": True}


def last_trace(by):
    turns = _user(by).get("turns") or []
    return turns[-1] if turns else None
