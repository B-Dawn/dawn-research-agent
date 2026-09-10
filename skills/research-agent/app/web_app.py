#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""科研智能体 · 零依赖 B/S Web 后端。

用标准库 http.server 起一个本地服务，前端（index.html）通过 /api/* 调用
现有 11 个脚本里的函数。无需任何第三方包，任意 Python 3.7+ 可直接跑。

  启动：  python web_app.py            （默认 http://127.0.0.1:8787）
  指定端口：python web_app.py --port 9000
  不自动开浏览器：python web_app.py --no-browser

所有检索类接口在沙箱/断网环境下会自动降级（返回空或离线骨架），不会崩溃。
"""

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 定位 scripts 目录并把lib包加入 sys.path ----
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import lib.net as net  # noqa: E402
net._ensure_utf8()
import lib.sources as sources  # noqa: E402
import lib.svg as svg  # noqa: E402
import ra_search  # noqa: E402
import ra_matrix  # noqa: E402
import ra_plot  # noqa: E402
import ra_journal  # noqa: E402
import ra_profile  # noqa: E402
import team_store as store  # noqa: E402
import model_bridge as model  # noqa: E402
import auth  # noqa: E402
import workbench as wb  # noqa: E402
import bpm  # noqa: E402  流程引擎（JeecgBoot/Flowable 风格 BPM）
import pipeline  # noqa: E402  全流程编排（①定方向→②调研→③创新点/可行性→④苏格拉底定题→⑤人工审核）
import agent  # noqa: E402  智能体内核（规划→工具调用→观察→反思，ReAct/Plan-Execute/Reflection）

# 服务端内存：保存最近一次检索结果，供「对比矩阵」直接使用
_LAST_SEARCH = {"query": "", "records": []}

# 权限表：除认证接口(auth)外，所有接口都要求登录；模型配置仅限管理员
ADMIN_ONLY_APIS = ("model_set",)

STATIC_DIR = HERE
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")
APP_VERSION = "1.1.0"  # 版本号唯一来源：改这里，页面（标题/登录页/侧栏）自动同步
APPJS_FILE = os.path.join(STATIC_DIR, "app.js")


# ---------------------------------------------------------------- 工具
def _json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _to_float(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


# ---------------------------------------------------------------- 业务接口
def api_search(params):
    query = (params.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "检索式不能为空"}
    srcs = [s.strip() for s in (params.get("sources") or "arxiv,openalex,crossref,s2").split(",") if s.strip()]
    limit = int(params.get("limit") or 20)
    yf = params.get("from") or None
    yt = params.get("to") or None
    yf = int(yf) if yf else None
    yt = int(yt) if yt else None

    records = sources.search(query, sources=srcs, limit=limit,
                             year_from=yf, year_to=yt)
    records.sort(key=lambda r: (-(r.get("citations") or 0), -(r.get("year") or 0)))
    md = ra_search.to_markdown(records, query)
    _LAST_SEARCH["query"] = query
    _LAST_SEARCH["records"] = records
    return {
        "ok": True,
        "query": query,
        "count": len(records),
        "records": records,
        "markdown": md,
        "note": "" if records else "没有结果，可能是断网或检索式太窄。",
    }


def api_matrix(params):
    # 优先用「上一次检索结果」，其次用前端粘贴的 JSON 文本
    raw = params.get("json")
    records = None
    queries = []
    if raw and str(raw).strip():
        try:
            data = json.loads(raw)
            records = data.get("records") if isinstance(data, dict) else data
            queries = [data.get("query")] if isinstance(data, dict) else []
        except Exception as e:
            return {"ok": False, "error": "JSON 解析失败：%s" % e}
    if records is None:
        records = _LAST_SEARCH["records"]
        if _LAST_SEARCH["query"]:
            queries = [_LAST_SEARCH["query"]]
    if not records:
        return {"ok": False, "error": "还没有检索结果，请先跑一次「文献检索」，或在此粘贴检索 JSON。"}

    merged = sources.dedup(records)
    rows = sources.to_rows(merged)
    sort = params.get("sort") or "year"
    if sort == "citation":
        rows.sort(key=lambda r: (r.get("citations") or 0), reverse=True)
    elif sort == "title":
        rows.sort(key=lambda r: str(r.get("title") or ""))
    else:
        rows.sort(key=lambda r: (r.get("year") or 0), reverse=True)
    maxn = int(params.get("max") or 0)
    if maxn > 0:
        rows = rows[:maxn]
    md = ra_matrix.render(rows, params.get("title") or "文献对比矩阵", queries)
    return {"ok": True, "rows": len(rows), "markdown": md}


def api_plot(params):
    csv_text = params.get("csv") or ""
    if not csv_text.strip():
        return {"ok": False, "error": "CSV 内容不能为空"}
    xcol = params.get("x")
    ycols = [c.strip() for c in (params.get("y") or "").split(",") if c.strip()]
    ptype = params.get("type") or "line"
    title = params.get("title") or ""
    xlabel = params.get("xlabel") or ""
    ylabel = params.get("ylabel") or ""

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".csv", prefix="raplot_")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(csv_text)
        header, data = ra_plot.load_csv(tmp)
        if not header:
            return {"ok": False, "error": "CSV 为空"}
        if not xcol or xcol not in header:
            return {"ok": False, "error": "横轴列 '%s' 不存在。可用列：%s" % (xcol, ", ".join(header))}
        if not ycols or any(c not in header for c in ycols):
            return {"ok": False, "error": "纵轴列不存在。可用列：%s" % ", ".join(header)}
        numeric_x = bool(data) and all(_to_float(d.get(xcol, "")) is not None for d in data)

        if ptype in ("line", "scatter"):
            if not numeric_x:
                return {"ok": False, "error": "%s 图要求横轴为数值列，'%s' 含非数值，请改用 bar。" % (ptype, xcol)}
            series = []
            for col in ycols:
                pts = []
                for d in data:
                    yv = _to_float(d.get(col, ""))
                    if yv is None:
                        continue
                    pts.append((_to_float(d.get(xcol, "")), yv))
                pts.sort()
                series.append({"name": col, "x": [p[0] for p in pts], "y": [p[1] for p in pts]})
            series = [s for s in series if s["y"]]
            if not series:
                return {"ok": False, "error": "没有可绘制的数值点"}
            if ptype == "line":
                out_svg = svg.line_chart(series, xlabel=xlabel or xcol, ylabel=ylabel or ycols[0],
                                         title=title, ymin=_to_float(params.get("ymin")),
                                         ymax=_to_float(params.get("ymax")))
            else:
                out_svg = svg.scatter(series, xlabel=xlabel or xcol, ylabel=ylabel or ycols[0], title=title)
        else:
            labels = [d.get(xcol, "") for d in data]
            if len(ycols) == 1:
                values = [_to_float(d.get(ycols[0], "")) for d in data]
                names = [ycols[0]]
            else:
                values = [[_to_float(d.get(c, "")) for d in data] for c in ycols]
                names = ycols
            out_svg = svg.bar_chart(labels, values, xlabel=xlabel or xcol,
                                    ylabel=ylabel or (ycols[0] if len(ycols) == 1 else ""),
                                    title=title, ymax=_to_float(params.get("ymax")), group_names=names)

        return {"ok": True, "svg": out_svg}
    except Exception as e:
        return {"ok": False, "error": "绘图失败：%s" % e}
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def api_journal(params):
    text = (params.get("abstract") or params.get("text") or "").strip()
    if len(text) < 40:
        return {"ok": False, "error": "摘要/关键词至少 40 个字符"}
    db_path = params.get("db") or ra_journal.DEFAULT_DB
    if not os.path.isfile(db_path):
        return {"ok": False, "error": "期刊库不存在: %s" % db_path}
    db = net.read_json(db_path)
    journals = db.get("journals") or []
    if not journals:
        return {"ok": False, "error": "期刊库为空"}
    scope = params.get("scope") or "all"
    if scope in ("intl", "cn"):
        journals = [j for j in journals if j.get("region", "intl") == scope]
    target_if = _to_float(params.get("target_if")) or 7.0
    top = int(params.get("top") or 8)
    min_if = _to_float(params.get("min_if")) or 0.0

    tokens = ra_journal._tokenize(text)
    scored = [ra_journal.score_journal(j, tokens, target_if, text) for j in journals]
    if min_if > 0:
        scored = [s for s in scored if s["if"] >= min_if]
    scored.sort(key=lambda s: s["total"], reverse=True)
    scored = scored[:max(1, top)]
    md = ra_journal.render(scored, target_if, text.strip())
    warn = "IF/分区为参考值，须以最新 JCR 核实；命中概率仅用于排序。"
    if scope == "cn":
        warn = "已限定国内期刊（中文期刊无 JCR IF，排序主要按 scope 契合度）；" + warn
    return {"ok": True, "markdown": md, "warning": warn}


def api_recommend(params):
    offline = bool(params.get("offline"))
    a = argparse.Namespace()
    a.offline = offline
    a.max_queries = int(params.get("max_queries") or 8)
    a.years = int(params.get("years") or 3)
    a.limit = int(params.get("limit") or 10)
    profile_path = ra_profile._default_profile()
    sections = ra_profile.parse_profile(profile_path)
    if not sections:
        return {"ok": False, "error": "画像文件不存在或为空: %s" % profile_path}
    rows, budget, combos = ra_profile.recommend(sections, a)
    md = ra_profile.render(rows, budget, sections, a.offline, a.years)
    return {"ok": True, "markdown": md, "profile": profile_path, "budget": budget}


def api_profile(params):
    profile_path = ra_profile._default_profile()
    sections = ra_profile.parse_profile(profile_path)
    if not sections:
        return {"ok": False, "error": "画像文件不存在或为空: %s" % profile_path}
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        ra_profile.show(sections, profile_path)
    finally:
        sys.stdout = old
    return {"ok": True, "text": buf.getvalue(), "profile": profile_path}


def api_profile_extra(params):
    if params.get("action") == "save":
        return {"ok": True, **store.save_profile_extra(params)}
    return {"ok": True, "profile_extra": store.load_profile_extra()}


def api_auth(params):
    """账户：注册 / 登录 / 退出 / 当前用户；用户管理类操作仅限管理员。"""
    action = params.get("action") or "me"
    user = params.get("_user")
    if action == "register":
        return auth.register(params)
    if action == "login":
        return auth.login(params)
    if action == "logout":
        auth.logout(params.get("token"))
        return {"ok": True}
    if action == "me":
        return {"ok": True, "user": user, "stats": auth.stats(),
                "admin_default": "%s / %s" % auth.DEFAULT_ADMIN}
    if action == "self_update":
        if not user:
            return {"ok": False, "error": "请先登录"}
        return auth.self_update({**params, "_name": user.get("name", "")})
    if action == "directory":
        # 用户目录按角色过滤（仅公开字段）：导师=其他导师+全部学生；学生=仅导师；管理员=全部
        if not user:
            return {"ok": False, "need_login": True, "error": "请先登录"}
        role = user.get("role")
        users = auth.list_users()
        if role == "teacher":
            users = [u for u in users if u["name"] != user["name"] and u.get("role") in ("teacher", "member")]
        elif role == "member":
            users = [u for u in users if u.get("role") == "teacher" and u.get("active", True)]
        return {"ok": True, "users": users}
    # ---- 以下需要管理员权限 ----
    if not user or user.get("role") != "admin":
        return {"ok": False, "error": "需要管理员权限（当前：%s）"
                % (user["name"] if user else "未登录")}
    if action == "list":
        return {"ok": True, "users": auth.list_users(), "stats": auth.stats()}
    if action == "update":
        return auth.update_user(params)
    if action == "delete":
        return auth.delete_user(params)
    return {"ok": False, "error": "未知操作 %s" % action}


# ================================================================ 模型 / AI 判断
def _profile_md():
    """把画像（md 解析 + 用户自主输入补充）拼成一段可读文本，供 AI 判断消费。"""
    parts = []
    profile_path = ra_profile._default_profile()
    sections = ra_profile.parse_profile(profile_path)
    if sections:
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            ra_profile.show(sections, profile_path)
        finally:
            sys.stdout = old
        parts.append("## 画像文件解析\n```\n%s\n```" % buf.getvalue().strip())
    extra = store.load_profile_extra()
    filled = {k: v for k, v in extra.items() if v}
    if filled:
        zh = {"direction_cn": "研究方向（中文）", "direction_en": "研究方向（英文）",
              "skills": "技能栈", "works": "在投/已发工作", "innovations": "创新点 N1-N5",
              "target_journals": "目标期刊", "constraints": "时间/资源约束",
              "goals": "短期目标", "notes": "其他"}
        seg = ["## 用户补充输入"]
        for k, v in filled.items():
            seg.append("- **%s**：%s" % (zh.get(k, k), v.replace("\n", " ")))
        parts.append("\n".join(seg))
    return "\n\n".join(parts) if parts else "（画像为空）"


def api_model_status(params):
    return {"ok": True, **model.status()}


def api_model_set(params):
    cfg = model.save_cfg(params)
    st = model.status()
    result = {"ok": True, "saved": cfg, "status": st}
    if params.get("test"):
        t = model.test()
        result["test"] = t
        result["ok"] = t["ok"]
        if not t["ok"]:
            result["error"] = "测试失败：%s（配置已保存，可重试）" % t.get("error", "")
    return result


def _model_unavailable():
    st = model.status()
    return {"ok": False, "ai": False, "reason": st.get("reason", "模型不可用")}


def api_ask(params):
    """自由问答：任何科研问题走真模型（带上画像 + 该用户长期记忆作背景）。"""
    if not model.status()["ok"]:
        return _model_unavailable()
    text = (params.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "问题不能为空"}
    sys_p = ("你是一位严谨的科研助手，服务于做 AI+低空经济安全（UAV 自组网/IDS/图神经网络/可解释 AI）"
             "方向的研究生。回答要具体、可执行、不编造文献与数据；涉及数据/引用时必须明说需要核实。\n\n"
             "以下是该用户的研究画像（可能不完整，以用户提问为准）：\n" + _profile_md())
    mem = wb.mem_context(_who(params))
    if mem:
        sys_p += "\n\n" + mem
    try:
        reply = model.quick_ask(text, system=sys_p, max_tokens=1400,
                                cfg=model.cfg_for_preset(params.get("model_preset")))
        return {"ok": True, "reply": reply, "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_ai_profile(params):
    """AI 诊断画像：聚焦度 / 空白 / 风险 / 下一步。"""
    if not model.status()["ok"]:
        return _model_unavailable()
    sys_p = ("你是一位资深科研导师，为研究生做研究方向诊断。基于用户的画像，输出一份中文诊断（Markdown），"
             "结构：## 方向诊断（清晰度/是否有聚焦）→ ## 优势与可复用积累 → ## 论证缺口与空白 → "
             "## 关键风险（3-5 条）→ ## 90 天首个可行动作（具体到可执行）。只基于用户实际提供的信息判断，"
             "不要臆造用户没写的研究经历；引用论文必须注明需另行核实。\n\n画像如下：\n" + _profile_md())
    try:
        reply = model.quick_ask("请诊断这个科研画像并给出建议。", system=sys_p, max_tokens=1500)
        return {"ok": True, "reply": reply, "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_ai_recommend(params):
    """AI 深度方向推荐：规则引擎产出候选（含离线骨架）+ 画像 → 大模型判断排序并给首个动作。"""
    if not model.status()["ok"]:
        return _model_unavailable()
    offline = bool(params.get("offline", True))
    # 1) 规则候选作"证据"（复用现有引擎，保证每个方向都来自画像技能×方向组合）
    try:
        profile_path = ra_profile._default_profile()
        sections = ra_profile.parse_profile(profile_path)
        a = argparse.Namespace(offline=offline, max_queries=int(params.get("max_queries") or 8),
                               years=int(params.get("years") or 3), limit=int(params.get("limit") or 10))
        rows, budget, combos = ra_profile.recommend(sections, a)
        rule_md = ra_profile.render(rows, budget, sections, a.offline, a.years)
    except Exception as e:
        rule_md = "（规则引擎生成失败：%s）" % e
    sys_p = ("你是一位既懂学术前沿又务实的研究规划师。用户将给你：他的画像 + 一组由规则引擎按"
             "'技能×方向'生成的方向候选（分数是启发式，不代表定论）。你的任务：综合判断后输出 Markdown：\n"
             "## 推荐结论（A/B 分级，最多 3 个方向，必须能复用用户已有技能或数据）\n"
             "每个方向给：一句话理由（结合画像里的创新点/约束）+ 热度与竞争判断 + 数据可获得性提示。\n"
             "## 不建议的方向及原因（如果候选里有）\n## 首个可行动作（本周可做，具体到检索式或实验名）\n"
             "只依据给定材料判断，不编造用户经历；引用文献须注明需核实。\n\n"
             "### 用户画像\n" + _profile_md() + "\n\n### 规则候选\n" + rule_md[:6000])
    try:
        reply = model.quick_ask("请基于画像与候选方向给出最终推荐。", system=sys_p, max_tokens=1600)
        return {"ok": True, "reply": reply, "ai": True, "rule_md": rule_md}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ================================================================ 团队协作
_TASK_ST_ZH = {"todo": "待办", "doing": "进行中", "review": "评审", "done": "完成"}
_EXP_ST_ZH = {"registered": "已登记", "running": "运行中", "done": "已完成", "failed": "失败"}
_REP_ST_ZH = {"running": "复现中", "reproduced": "一致", "partial": "部分一致", "failed": "不一致"}


def render_task_board(tasks):
    if not tasks:
        return "**任务板**：暂无任务。去「协作中心」添加，或直接对我说“添加任务 写实验部分”。"
    order = ("todo", "doing", "review", "done")
    groups = {st: [t for t in tasks if t.get("status") == st] for st in order}
    summary = " / ".join("%s %d" % (_TASK_ST_ZH[st], len(groups[st])) for st in order)
    md = ["# 任务板", "", "> 概览：%s" % summary, ""]
    for st in order:
        if not groups[st]:
            continue
        md.append("## %s" % _TASK_ST_ZH[st])
        md.append("")
        md.append("| ID | 任务 | 指派 | 优先级 | 模块 |")
        md.append("|---|---|---|---|---|")
        for t in groups[st]:
            md.append("| %s | %s | %s | %s | %s |" % (
                t.get("id", ""), (t.get("title") or "").replace("|", "\\|"),
                t.get("assignee") or "—", t.get("priority") or "中", t.get("module") or "—"))
        md.append("")
    return "\n".join(md)


def api_member(params):
    action = params.get("action") or "list"
    if action == "list":
        return {"ok": True, "members": store.list_members()}
    if action == "add":
        if not (params.get("name") or "").strip():
            return {"ok": False, "error": "成员姓名不能为空"}
        store.add_member(params)
        return {"ok": True, "members": store.list_members()}
    mid = params.get("id")
    if not mid:
        return {"ok": False, "error": "缺少成员 id"}
    if action == "update":
        store.update_member(mid, params)
    elif action == "remove":
        store.remove_member(mid)
    else:
        return {"ok": False, "error": "未知操作 %s" % action}
    return {"ok": True, "members": store.list_members()}


def api_task(params):
    action = params.get("action") or "list"
    if action == "list":
        tasks = store.list_tasks(status=params.get("status") or None,
                                 assignee=params.get("assignee") or None)
        return {"ok": True, "tasks": tasks, "markdown": render_task_board(tasks)}
    if action == "add":
        if not (params.get("title") or "").strip():
            return {"ok": False, "error": "任务标题不能为空"}
        store.add_task(params)
        return {"ok": True, "markdown": render_task_board(store.list_tasks())}
    tid = params.get("id")
    if not tid:
        return {"ok": False, "error": "缺少任务 id"}
    if action == "update":
        store.update_task(tid, params)
    elif action == "remove":
        store.remove_task(tid)
    else:
        return {"ok": False, "error": "未知操作 %s" % action}
    return {"ok": True, "markdown": render_task_board(store.list_tasks())}


def _paper_md(papers, detail_id=None):
    md = []
    for p in papers:
        secs = p.get("sections", [])
        done = sum(1 for s in secs if s.get("status") == "done")
        open_c = sum(1 for c in p.get("comments", []) if not c.get("resolved"))
        md.append("## %s  `%s`" % (p.get("title", ""), p.get("status", "")))
        md.append("")
        md.append("- 负责人：%s | 目标期刊：%s | 章节 %d/%d 完成 | 未解决意见 %d" % (
            p.get("owner") or "—", p.get("target_journal") or "—", done, len(secs), open_c))
        cl = p.get("collaborators") or []
        extra = []
        if p.get("created_by"):
            extra.append("创建者 @%s" % p["created_by"])
        if cl:
            extra.append("协作者：" + "、".join(
                "@%s（%s）" % (c.get("name", ""), "可编辑" if c.get("perm") == "edit" else "仅查看")
                for c in cl))
        if extra:
            md.append("- " + " | ".join(extra))
        if not secs:
            md.append("- 尚未拆分章节。")
        else:
            md.append("")
            md.append("| 章节 | 指派 | 状态 | 版本 | 更新 |")
            md.append("|---|---|---|---|---|")
            for s in secs:
                md.append("| %s | %s | %s | v%s | %s |" % (
                    s.get("title") or s.get("key"), s.get("assignee") or "—",
                    "完成" if s.get("status") == "done" else "草稿",
                    s.get("version", 1), s.get("updated", "")[:16]))
        if detail_id == p.get("id") and p.get("comments"):
            md.append("")
            md.append("### 审阅意见")
            md.append("")
            for c in p.get("comments", []):
                md.append("- [%s] **%s** @%s：%s%s" % (
                    "x" if c.get("resolved") else " ",
                    c.get("by", "?"), c.get("at", "")[:16], c.get("text", ""),
                    "  *(已解决)*" if c.get("resolved") else ""))
        md.append("")
    if not papers:
        return "**论文协作**：还没有论文项目。可在「协作中心」创建，或对我说“新建论文 <标题>”。"
    return "\n".join(md).strip()


def _paper_perm(p, user):
    """返回用户对该论文的权限：admin / creator / edit / view / None。"""
    if not user:
        return None
    if user.get("role") == "admin":
        return "admin"
    name = user.get("name", "")
    if p.get("created_by") == name:
        return "creator"
    for c in p.get("collaborators", []):
        if c.get("name") == name:
            return c.get("perm", "view")
    return None


def _visible_papers(user):
    """论文列表按身份过滤：admin 全部；其他人 = 自己创建 + 被授权协作的。"""
    papers = store.list_papers()
    if user and user.get("role") == "admin":
        return papers
    name = (user or {}).get("name", "")
    out = []
    for p in papers:
        if p.get("created_by") == name or any(
                c.get("name") == name for c in p.get("collaborators", [])):
            out.append(p)
    return out


def api_paper(params):
    action = params.get("action") or "list"
    pid = params.get("id")
    user = params.get("_user") or {}
    if action == "list":
        papers = _visible_papers(user)
        return {"ok": True, "papers": papers, "markdown": _paper_md(papers)}
    if action == "create":
        if not (params.get("title") or "").strip():
            return {"ok": False, "error": "论文标题不能为空"}
        params["created_by"] = _who(params)  # 记录创建者账户，用于删除权限
        store.create_paper(params)
        papers = _visible_papers(user)
        return {"ok": True, "papers": papers, "markdown": _paper_md(papers)}
    if not pid:
        return {"ok": False, "error": "缺少论文 id"}
    p = store.get_paper(pid)
    if not p:
        return {"ok": False, "error": "论文不存在: %s" % pid}
    perm = _paper_perm(p, user)

    if action == "get":
        if not perm:
            return {"ok": False, "error": "无权查看该论文项目"}
        return {"ok": True, "paper": p, "my_perm": perm,
                "markdown": _paper_md([p], detail_id=pid)}
    if action == "remove":
        creator = p.get("created_by") or ""
        if perm not in ("creator", "admin"):
            if creator:
                return {"ok": False, "error": "仅创建者（%s）或管理员可删除该项目" % creator}
            return {"ok": False, "error": "该项目未记录创建者，请联系管理员删除"}
        store.remove_paper(pid)
        papers = _visible_papers(user)
        return {"ok": True, "papers": papers, "markdown": _paper_md(papers)}
    if action == "collab_add":
        if perm not in ("creator", "admin"):
            return {"ok": False, "error": "仅创建者或管理员可添加协作者"}
        cname = (params.get("cname") or "").strip()
        cperm = params.get("cperm") or "edit"
        if cname == p.get("created_by"):
            return {"ok": False, "error": "该用户是创建者，无需添加为协作者"}
        if not any(u.get("name") == cname and u.get("active", True) for u in auth.list_users()):
            return {"ok": False, "error": "用户 %s 不存在或已停用" % cname}
        try:
            store.set_collaborator(pid, cname, cperm)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "paper": store.get_paper(pid), "my_perm": perm,
                "markdown": _paper_md([store.get_paper(pid)], detail_id=pid)}
    if action == "collab_remove":
        if perm not in ("creator", "admin"):
            return {"ok": False, "error": "仅创建者或管理员可移除协作者"}
        try:
            store.remove_collaborator(pid, (params.get("cname") or "").strip())
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "paper": store.get_paper(pid), "my_perm": perm,
                "markdown": _paper_md([store.get_paper(pid)], detail_id=pid)}

    # 以下写操作需要 编辑权限（创建者 / edit 协作者 / 管理员）
    if perm not in ("creator", "edit", "admin"):
        if perm == "view":
            return {"ok": False, "error": "你对该论文只有查看权限，如需修改请联系创建者提权"}
        return {"ok": False, "error": "无权修改该论文项目（你不是创建者或协作者）"}
    if action == "meta":
        store.set_paper_meta(pid, params)
    elif action == "section":
        if not (params.get("key") or "").strip():
            return {"ok": False, "error": "缺少章节 key"}
        store.upsert_section(pid, params)
    elif action == "comment":
        store.add_comment(pid, params)
    elif action == "resolve":
        store.resolve_comment(pid, params.get("cid"), params.get("resolved", True))
    else:
        return {"ok": False, "error": "未知操作 %s" % action}
    p = store.get_paper(pid)
    return {"ok": True, "paper": p, "my_perm": perm,
            "markdown": _paper_md([p], detail_id=pid)}


def render_exp_table(exps):
    if not exps:
        return "**实验台账**：暂无实验。去「实验复现」登记，或对我说“登记实验 <名称>”。"
    md = ["# 实验台账", "",
          "| ID | 实验 | 数据集 | 负责人 | 状态 | 关键结果 |",
          "|---|---|---|---|---|---|"]
    for e in exps:
        res = e.get("result") or {}
        res_s = ", ".join("%s=%.4g" % (k, v) for k, v in list(res.items())[:3]) if res else "—"
        md.append("| %s | %s | %s | %s | %s | %s |" % (
            e.get("id", ""), (e.get("name") or "").replace("|", "\\|"),
            e.get("dataset") or "—", e.get("owner") or "—",
            _EXP_ST_ZH.get(e.get("status", ""), e.get("status", "")), res_s))
    return "\n".join(md)


def api_experiment(params):
    action = params.get("action") or "list"
    if action == "list":
        exps = store.list_experiments(owner=params.get("owner") or None,
                                      status=params.get("status") or None)
        return {"ok": True, "experiments": exps, "markdown": render_exp_table(exps)}
    if action == "add":
        if not (params.get("name") or "").strip():
            return {"ok": False, "error": "实验名称不能为空"}
        store.add_experiment(params)
        return {"ok": True, "markdown": render_exp_table(store.list_experiments())}
    eid = params.get("id")
    if action == "get" and eid:
        e = store.get_experiment(eid)
        if not e:
            return {"ok": False, "error": "实验不存在: %s" % eid}
        return {"ok": True, "experiment": e}
    if not eid:
        return {"ok": False, "error": "缺少实验 id"}
    if action == "update":
        store.update_experiment(eid, params)
    elif action == "remove":
        store.remove_experiment(eid)
    else:
        return {"ok": False, "error": "未知操作 %s" % action}
    return {"ok": True, "markdown": render_exp_table(store.list_experiments())}


def render_rep_table(reps):
    if not reps:
        return "**复现记录**：还没有复现请求。去「实验复现」发起。"
    exps = {e["id"]: e for e in store.list_experiments()}
    md = ["# 复现记录", "",
          "| 复现 | 实验 | 复现人 | 结果 | 日期 | 环境/说明 |",
          "|---|---|---|---|---|---|"]
    for r in reps:
        ename = exps.get(r.get("exp_id", ""), {}).get("name", r.get("exp_id", ""))
        md.append("| %s | %s | %s | %s | %s | %s |" % (
            r.get("id", ""), ename.replace("|", "\\|"), r.get("by") or "—",
            _REP_ST_ZH.get(r.get("result", ""), r.get("result", "")),
            r.get("date", "")[:10], (r.get("env") or r.get("diff_note") or "")[:24].replace("|", "\\|")))
    return "\n".join(md)


def api_reproduce(params):
    action = params.get("action") or "list"
    if action == "list":
        reps = store.list_reproductions(exp_id=params.get("exp_id") or None)
        return {"ok": True, "reproductions": reps, "markdown": render_rep_table(reps)}
    if action == "add":
        if not params.get("exp_id"):
            return {"ok": False, "error": "缺少 exp_id"}
        if not store.get_experiment(params["exp_id"]):
            return {"ok": False, "error": "实验不存在: %s" % params["exp_id"]}
        store.add_reproduction(params)
        return {"ok": True, "markdown": render_rep_table(store.list_reproductions(params["exp_id"]))}
    rid = params.get("id")
    if not rid:
        return {"ok": False, "error": "缺少复现 id"}
    if action == "update":
        store.update_reproduction(rid, params)
    else:
        return {"ok": False, "error": "未知操作 %s" % action}
    return {"ok": True, "markdown": render_rep_table(store.list_reproductions())}


def api_validate(params):
    csv_text = params.get("csv") or params.get("text") or ""
    if not csv_text.strip():
        return {"ok": False, "error": "CSV 内容不能为空"}
    report = store.validate_csv_text(csv_text)
    body = ["# 实验结果 CSV 校验", "", "- 状态：%s" % ("通过" if report["ok"] else "未通过")]
    if report["errors"]:
        body += ["", "**发现问题：**", ""]
        body += ["1. " + e for e in report["errors"]]
    body += [""] + ["> " + ln for ln in report["lines"]]
    return {"ok": True, "pass": report["ok"], "errors": report["errors"],
            "markdown": "\n".join(body)}


# ================================================================ 工作台扩展：写作 / 综述 / 统计 / 记忆
def _who(params):
    return (params.get("_user") or {}).get("name", "")


def api_write(params):
    """AI 写作助手：生成 + 草稿库。"""
    action = params.get("action") or "gen"
    by = _who(params)
    if action == "gen":
        r = wb.gen_doc(kind=params.get("kind"), topic=params.get("topic"),
                       points=params.get("points"), extra=params.get("extra"),
                       max_tokens=int(params.get("max_tokens") or 1500))
        if not r.get("ok") and r.get("ai") is False:
            return {"ok": False, "ai": False,
                    "error": (r.get("reason") or r.get("error") or "模型不可用") +
                             "\n\n> 到「模型与API」选预设（默认腾讯混元）填 API Key，或装本地 Ollama。"}
        return r
    if action == "save":
        return wb.docs_save(params.get("kind"), params.get("title"),
                            params.get("content"), by)
    if action == "list":
        return {"ok": True, "docs": wb.docs_list()}
    if action == "remove":
        return wb.docs_remove(params.get("id"))
    return {"ok": False, "error": "未知操作 %s" % action}


def api_review(params):
    """文献综述初稿：基于检索结果/粘贴清单生成 + 存档。"""
    action = params.get("action") or "gen"
    by = _who(params)
    if action == "gen":
        records = None
        raw = (params.get("json") or "").strip()
        if raw:
            try:
                data = json.loads(raw)
                records = data.get("records") if isinstance(data, dict) else data
            except Exception as e:
                return {"ok": False, "error": "粘贴的文献 JSON 解析失败：%s" % e}
        if not records:
            records = _LAST_SEARCH["records"] or []
        r = wb.gen_review(params.get("topic"), params.get("angle"),
                          records=records, extra=params.get("extra"))
        if r.get("ok") and records:
            r["paper_count"] = len(records)
        if not r.get("ok") and r.get("ai") is False:
            return {"ok": False, "ai": False, "error": r.get("reason") or r.get("error", "")}
        return r
    if action == "save":
        return wb.rev_save(params.get("title"), params.get("angle"), params.get("content"), by)
    if action == "list":
        return {"ok": True, "reviews": wb.rev_list()}
    if action == "remove":
        return wb.rev_remove(params.get("id"))
    return {"ok": False, "error": "未知操作 %s" % action}


def api_format(params):
    """论文格式检查：规则版（离线）+ 自定义模板 + 可选 AI 补充 + 文件上传。"""
    action = params.get("action") or "check"
    if action == "upload_doc":
        import base64 as _b64, io as _io, zipfile as _zf
        data_b64 = (params.get("data_b64") or "").strip()
        kind = params.get("kind") or "paper"
        fname = (params.get("filename") or "").lower()
        if not data_b64:
            return {"ok": False, "error": "未收到文件"}
        try:
            raw = _b64.b64decode(data_b64)
        except Exception as e:
            return {"ok": False, "error": "文件解码失败：%s" % e}
        text = ""
        if fname.endswith(".docx"):
            try:
                z = _zf.ZipFile(_io.BytesIO(raw))
                xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
                paras = []
                for pm in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
                    seg = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", pm, re.S))
                    seg = seg.replace("&amp;", "&").strip()
                    if seg:
                        paras.append(seg)
                text = "\n".join(paras)
            except Exception as e:
                return {"ok": False, "error": "docx 解析失败：%s" % e}
        else:
            text = raw.decode("utf-8", errors="ignore")
        if kind == "paper":
            if len(text.strip()) < 100:
                return {"ok": False, "error": "论文文本太短（≥100 字），请确认上传了正确的论文文件"}
            return {"ok": True, "text": text[:300000], "chars": len(text)}
        # 论文要求 → 必需章节清单
        items = []
        if fname.endswith(".json"):
            try:
                j = json.loads(text)
                items = [str(x).strip() for x in (j.get("items") or j) if str(x).strip()]
            except Exception:
                items = []
        if not items:
            items = [ln.strip() for ln in text.splitlines() if ln.strip()]
        items = items[:60]
        if len(items) < 3:
            return {"ok": False, "error": "论文要求太少（至少 3 条章节/要素），请确认上传了要求文件"}
        ptype = params.get("ptype") if params.get("ptype") in wb.FT_TYPES else "thesis"
        sv = wb.ft_template_save(ptype, "\n".join(items))
        if not sv.get("ok"):
            return sv
        return {"ok": True, "items": items, "ptype": ptype, "saved": True,
                "note": "已按上传的要求文件保存为 %s 模板（%d 项）" % (wb.FT_TYPES.get(ptype, ptype), len(items))}
    if action == "check":
        return wb.ft_check(params.get("text"), params.get("ptype") or "journal",
                           bool(params.get("ai")))
    if action == "template_save":
        return wb.ft_template_save(params.get("ptype"), params.get("text"))
    if action == "template_get":
        return wb.ft_template_get(params.get("ptype") or "journal")
    return {"ok": False, "error": "未知操作 %s" % action}


def api_skills(params):
    """技能中心：内置技能（源自论文辅助全流程方法论）+ 用户上传的自定义技能。"""
    action = params.get("action") or "list"
    by = _who(params)
    if action == "run":
        r = wb.skill_run(params.get("skill"), params.get("input"), params.get("extra"))
        if not r.get("ok") and r.get("ai") is False:
            return {"ok": False, "ai": False, "error": r.get("error", "")}
        return r
    if action == "upload":
        import base64 as _b64
        name, desc, prompt = params.get("name"), params.get("desc"), params.get("prompt")
        data_b64 = (params.get("data_b64") or "").strip()
        if data_b64:
            try:
                raw = _b64.b64decode(data_b64)
            except Exception as e:
                return {"ok": False, "error": "文件解码失败：%s" % e}
            fname = (params.get("filename") or "skill.txt").strip()
            if fname.endswith(".zip"):
                # zip：自动解压，把包内每个 .md/.txt/.json 注册为一个技能
                import io as _io, zipfile as _zf
                try:
                    zf = _zf.ZipFile(_io.BytesIO(raw))
                except Exception as e:
                    return {"ok": False, "error": "zip 打开失败：%s" % e}
                added, skipped = [], []
                for info in zf.infolist():
                    if info.is_dir() or ".." in info.filename or info.filename.startswith("/"):
                        continue
                    bn_low = info.filename.lower()
                    if not bn_low.endswith((".md", ".txt", ".json")) or bn_low.startswith("readme"):
                        continue
                    try:
                        text = zf.read(info).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    bn = info.filename.rsplit("/", 1)[-1]
                    if bn.lower().endswith(".json"):
                        try:
                            j = json.loads(text)
                            r_add = wb.cs_add(by, j.get("name") or bn, j.get("desc") or "自定义技能（zip 导入）",
                                              j.get("prompt") or text)
                        except Exception:
                            skipped.append(bn)
                        else:
                            added.append(bn) if r_add.get("ok") else skipped.append(bn)
                    else:
                        r_add = wb.cs_add(by, name or bn.rsplit(".", 1)[0], desc or "自定义技能（zip 导入）", text)
                        added.append(bn) if r_add.get("ok") else skipped.append(bn)
                return {"ok": True, "zip_import": True, "added": added, "skipped": skipped,
                        "note": "zip 内注册 %d 个技能，跳过 %d 个（非文本/空内容）" % (len(added), len(skipped))}
            text = raw.decode("utf-8", errors="ignore")
            if fname.endswith(".json"):
                try:
                    j = json.loads(text)
                    name, desc, prompt = j.get("name") or fname, j.get("desc") or "自定义技能", j.get("prompt") or text
                except Exception as e:
                    return {"ok": False, "error": "JSON 解析失败：%s" % e}
            else:
                name, desc, prompt = name or fname.rsplit(".", 1)[0], desc or "自定义技能", text
        return wb.cs_add(by, name, desc, prompt)
    if action == "export":
        ids = params.get("ids") or []
        merged = wb.cs_all()
        picks = [merged[i] for i in ids if i in merged] or list(merged.values())
        import io as _io, zipfile as _zf, base64 as _b64
        buf = _io.BytesIO()
        with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zf:
            used = set()
            for idx, sk in enumerate(picks, 1):
                fn = "%02d_%s.json" % (idx, re.sub(r"[^\w\-]+", "_", sk.get("name", "skill")))
                if fn in used:
                    fn = "%02d_%s.json" % (idx, re.sub(r"[^\w\-]+", "_", sk.get("name", "skill")) + "_" + str(idx))
                used.add(fn)
                zf.writestr(fn, json.dumps({"name": sk.get("name", ""), "desc": sk.get("desc", ""),
                                            "prompt": sk.get("prompt", ""), "tab": sk.get("tab", "")},
                                           ensure_ascii=False, indent=1))
            zf.writestr("README.txt", "破晓技能包：把本 zip 在「设置 → 技能中心」重新导入即可。\n每个 .json 为一个技能（name/desc/prompt）。")
        return {"ok": True, "zip_b64": _b64.b64encode(buf.getvalue()).decode(),
                "filename": "dawn_skills_%s.zip" % time.strftime("%Y%m%d_%H%M"), "count": len(picks)}
    if action == "remove":
        return wb.cs_remove(by, params.get("id"))
    merged = wb.cs_all()
    return {"ok": True, "skills": [{"id": k, "name": v.get("name", k), "desc": v.get("desc", ""),
                                    "tab": v.get("tab", ""), "custom": v.get("custom", False),
                                    "by": v.get("by", "")} for k, v in merged.items()]}


def api_reffolder(params):
    """文献库：真实检索结果的参考文献文件夹（检索/收录/批量精读/综合创新点）。"""
    action = params.get("action") or "list"
    by = _who(params)
    if action == "create":
        return wb.rf_create(by, params.get("name"))
    if action == "remove":
        return wb.rf_remove(params.get("id"), by)
    if action == "add_papers":
        return wb.rf_add_papers(params.get("id"), by, params.get("papers") or [])
    if action == "remove_paper":
        return wb.rf_remove_paper(params.get("id"), by, int(params.get("idx") or -1))
    if action == "set_analysis":
        return wb.rf_set_analysis(params.get("id"), by, params.get("title"), params.get("analysis"))
    if action == "patch_paper":
        return wb.rf_patch_paper(params.get("id"), by, params.get("title"),
                                  abstract=params.get("abstract"))
    if action == "fetch_abstract":
        return wb.rf_fetch_abstract(params.get("title"), params.get("link") or "")
    if action == "synthesize":
        return wb.rf_synthesize(params.get("id"), by)
    return {"ok": True, "folders": wb.rf_list(by)}


def api_reviewflow(params):
    """评审工作台：AI 专家盲审（匿名化）+ 协作者审核。"""
    action = params.get("action") or "list"
    by = _who(params)
    if action == "create":
        return wb.review_create(by, params.get("title"), params.get("content"),
                                params.get("kind"), params.get("reviewers") or [])
    if action == "run_blind":
        r = wb.review_run_blind(params.get("id"), by, params.get("note") or "")
        if not r.get("ok") and r.get("ai") is False:
            return {"ok": False, "ai": False, "error": r.get("error", "")}
        return r
    if action == "submit":
        return wb.review_submit(params.get("id"), by, params.get("scores") or {},
                                params.get("comments"), params.get("verdict"))
    if action == "mine":
        return {"ok": True, "mine": wb.review_mine(by)}
    return {"ok": True, "reviews": wb.review_list(by)}


def api_workflow(params):
    """方向链：定方向（AI 判断 + 人工确认）→ 文献调研。"""
    action = params.get("action") or "get"
    by = _who(params)
    if action == "from_resume":
        if not model.status()["ok"]:
            return {"ok": False, "ai": False,
                    "error": "工作流启动需要模型。去「模型与API」选预设并填 API Key。"}
        text = (params.get("text") or "").strip()
        if len(text) < 30:
            return {"ok": False, "error": "请把简历/擅长技术/想要的方向描述得更充分一些（≥30 字）"}
        sys_p = ("你是科研方向规划师。用户会给出个人简历/擅长技术/想要的研究方向（可能只有其中一两项）。"
                 "请只依据给定材料，返回 STRICT JSON（无代码栅栏）："
                 '{"analysis_md": "Markdown 分析：## 你的优势 → ## 可行的研究方向（2-3 个，每个含一句话可行性判断：数据/算力/时间）", '
                 '"direction_cn": "最适合的研究方向（中文，一句话）", '
                 '"skills": "用户技能栈（逗号分隔，未提及的不要编造）", '
                 '"direction_final": "建议定稿的最终研究方向（一句话，具体到可检索）"}。'
                 "不编造用户经历与文献；方向必须能复用用户已有技能。")
        try:
            raw = model.quick_ask(text[:8000], system=sys_p, max_tokens=1300, temperature=0.4)
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        except Exception as e:
            return {"ok": False, "error": "解析失败（可重试）：%s" % e}
        # 自动写入画像（十步走第①步的画像部分）
        try:
            store.save_profile_extra({"direction_cn": data.get("direction_cn", ""),
                                      "skills": data.get("skills", "")})
        except Exception:
            pass
        return {"ok": True, "analysis_md": data.get("analysis_md", ""),
                "direction_cn": data.get("direction_cn", ""),
                "direction_final": data.get("direction_final", ""),
                "saved": "已把方向与技能栈写入「科研画像」"}
    if action == "set_direction":
        return wb.wf_set_direction(by, params.get("direction"), params.get("refined"))
    return {"ok": True, **wb.wf_get_direction(by)}


def _bpm_inst_view(i):
    """列表用的精简实例视图。"""
    defn = bpm.def_get(i.get("def_id"))
    cur_names = []
    for nid in (i.get("cur") or []):
        nd = bpm._node(defn, nid) if defn else None
        cur_names.append((nd or {}).get("name") or nid)
    handlers, seen = [], set()
    for t in i.get("tasks", []):
        if t.get("status") != "todo":
            continue
        for u in (t.get("assignees") or []) + ([t["delegated_to"]] if t.get("delegated_to") else []):
            if u and u not in seen:
                seen.add(u)
                handlers.append(u)
    return {
        "id": i.get("id"), "def_id": i.get("def_id"), "def_name": i.get("def_name"),
        "category": i.get("category"), "title": i.get("title"),
        "business_key": i.get("business_key"), "form": i.get("form"),
        "initiator": i.get("initiator"), "status": i.get("status"),
        "status_zh": bpm.STATUS_ZH.get(i.get("status"), i.get("status")),
        "ts": i.get("ts"), "end_ts": i.get("end_ts"),
        "cur_names": cur_names, "handlers": handlers, "progress": bpm.progress(i),
        "cc": i.get("cc") or [], "task_count": len(i.get("tasks") or []),
    }


def _bpm_ai_gen(params):
    """可选增强：一句话生成流程定义。

    注意：流程引擎本身**不需要 AI**；此处模型不可用时只提示，不影响手工配置与内置模板。
    """
    if not model.status()["ok"]:
        return {"ok": False, "ai": False,
                "error": "AI 生成流程需要配置模型（当前不可用）。"
                         "可先直接用内置模板，或在「流程设计」里手工配置——流程引擎不需要 AI。"}
    text = (params.get("text") or "").strip()
    if len(text) < 6:
        return {"ok": False, "error": "请描述审批场景（≥6 字），例如「论文投稿需要导师和学院两级审批」"}
    sys_p = ("你是 BPM 流程设计助手。用户用自然语言描述一个审批场景，你返回 STRICT JSON（无代码栅栏）："
             '{"name":"流程名（≤10字）","category":"分类（如 论文/开题/实验/通用）",'
             '"desc":"一句话说明","nodes":["节点1名","节点2名",...],'
             '"role":"审批角色（admin/teacher/member 三选一，默认 admin）"}。'
             "nodes 只列需要人工审批的节点（2-5 个），不要包含开始/结束。用中文。")
    try:
        raw = model.quick_ask(text[:2000], system=sys_p, max_tokens=1000, temperature=0.3)
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception as e:
        return {"ok": False, "error": "AI 返回解析失败（可重试）：%s" % e}
    names = [str(x).strip() for x in (data.get("nodes") or []) if str(x).strip()][:6]
    if not names:
        return {"ok": False, "error": "AI 未给出有效审批节点，请换一种说法描述"}
    role = data.get("role") if data.get("role") in ("admin", "teacher", "member") else "admin"
    nodes = [{"id": "n1", "type": "start", "name": "提交"}]
    edges = []
    for idx, nm in enumerate(names, start=2):
        nodes.append({"id": "n%d" % idx, "type": "approve", "name": nm,
                      "assignee_type": "role", "assignee": role, "sign_mode": "or",
                      "allow_reject": True, "reject_to": "initiator"})
        edges.append({"from": "n%d" % (idx - 1), "to": "n%d" % idx})
    end_id = "n%d" % (len(nodes) + 1)
    nodes.append({"id": end_id, "type": "end", "name": "结束"})
    edges.append({"from": "n%d" % (len(nodes) - 1), "to": end_id})
    return {"ok": True, "draft": {
        "name": (data.get("name") or ("AI流程·" + text[:10]))[:20],
        "category": (data.get("category") or "AI生成")[:10],
        "desc": (data.get("desc") or text)[:200],
        "form": [{"key": "title", "label": "标题", "type": "text", "required": True},
                 {"key": "detail", "label": "说明", "type": "textarea", "required": False}],
        "nodes": nodes, "edges": edges, "cc_on_end": []}}


def api_bpm(params):
    """流程中心（参照 JeecgBoot/Flowable）：定义 / 发起 / 待办 / 审批 / 驳回 / 转办 / 委派 / 加签 / 抄送 / 跟踪。"""
    action = params.get("action") or "overview"
    by = _who(params)
    admin = (params.get("_user") or {}).get("role") == "admin"
    if action == "overview":
        return {"ok": True, "stats": bpm.stats(by, admin), "todo": bpm.todo(by),
                "defs": bpm.def_list(by), "mine": [_bpm_inst_view(i)
                                                    for i in bpm.mine(by)[:10]]}
    if action == "defs":
        return {"ok": True, "defs": bpm.def_list(by)}
    if action == "def_get":
        d = bpm.def_get(params.get("id"))
        return {"ok": bool(d), "def": d, "error": "" if d else "流程定义不存在"}
    if action == "def_save":
        return bpm.def_save(by, params.get("def") or {k: v for k, v in params.items()
                                                     if k not in ("action", "_user", "_token")})
    if action == "def_remove":
        if not admin:
            return {"ok": False, "error": "仅管理员可删除流程定义"}
        return bpm.def_remove(params.get("id"))
    if action == "start":
        return bpm.wf_start(params.get("def_id"), by, params.get("title"),
                            params.get("form") or {}, params.get("business_key") or "")
    if action == "todo":
        return {"ok": True, "todo": bpm.todo(by), "stats": bpm.stats(by, admin)}
    if action == "mine":
        return {"ok": True, "instances": [_bpm_inst_view(i) for i in bpm.mine(by)]}
    if action == "done":
        return {"ok": True, "done": bpm.done_list(by)}
    if action == "cc":
        return {"ok": True, "cc": bpm.cc_list(by)}
    if action == "inst_list":
        items = bpm.inst_list(by, all_=admin and bool(params.get("all")))
        return {"ok": True, "instances": [_bpm_inst_view(i) for i in items]}
    if action == "inst":
        i = bpm.inst_get(params.get("id"))
        if not i:
            return {"ok": False, "error": "流程实例不存在"}
        v = _bpm_inst_view(i)
        v["history"] = i.get("history") or []
        v["tasks"] = i.get("tasks") or []
        v["can_manage"] = admin or i.get("initiator") == by
        return {"ok": True, "instance": v, "progress": bpm.progress(i)}
    if action == "act":
        return bpm.act(params.get("inst_id"), params.get("task_id"), by,
                       params.get("op") or "approve", params.get("comment") or "",
                       {"to": params.get("to"), "users": params.get("users") or []},
                       is_admin=admin)
    if action == "revoke":
        return bpm.revoke(params.get("inst_id"), by, is_admin=admin)
    if action == "resubmit":
        return bpm.resubmit(params.get("inst_id"), by, params.get("form"))
    if action == "users":
        us = auth.list_users()
        return {"ok": True, "users": [{"name": u.get("name"), "role": u.get("role"),
                                       "display": u.get("display") or u.get("name")}
                                      for u in us if u.get("active", True)]}
    if action == "status_zh":
        return {"ok": True, "status": bpm.STATUS_ZH, "action": bpm.ACTION_ZH}
    if action == "ai_gen":
        return _bpm_ai_gen(params)
    return {"ok": False, "error": "未知操作 %s" % action}


def api_pipeline(params):
    """全流程一键编排：①定方向 →②文献调研 →③创新点/可行性 →④苏格拉底问询定题 →⑤提交人工审核。

    交付规则：只有经过苏格拉底问询并逐条回应的题目才判为合格，合格后才提交人工审核；
    「交给人工审核之前的内容」全部自动完成（不需要 AI 的环节在未配模型时也照常执行）。
    """
    action = params.get("action") or "run"
    by = _who(params)
    if action == "state":
        return {"ok": True, "state": pipeline.state(by), "report_md": pipeline.report_md(pipeline.state(by))}
    if action == "reset":
        return pipeline.reset(by)
    if action == "run":
        opts = {
            "resume_text": params.get("resume_text") or params.get("text") or "",
            "query": params.get("query") or "",
            "sources": params.get("sources") or "arxiv,openalex,crossref,s2",
            "limit": params.get("limit") or 20,
            "collect_n": params.get("collect_n") or 30,
            "matrix_max": params.get("matrix_max") or 15,
            "digest_n": params.get("digest_n") or 3,
            "folder_name": params.get("folder_name") or "",
            "auto_review": params.get("auto_review", True),
            "only": params.get("only") or [],
            "force": bool(params.get("force")),
        }
        r = pipeline.run(by, opts)
        st = pipeline.state(by)
        return {"ok": True, "module": "bpm", "report_md": r["report_md"], "state": st,
                "topic": r.get("topic"), "review_inst": r.get("review_inst"),
                "results": r.get("results"), "guide_steps": r.get("guide_steps")}
    return {"ok": False, "error": "未知操作 %s" % action}


def api_agent(params):
    """智能体内核：规划 → 工具调用 → 观察 → 反思。

    多步目标会真正串起来执行（例：「检索X，然后精读前三篇，再帮我选刊」），
    而不是只回一段文字。无模型时用规则规划器（零成本）仍可多步执行。
    """
    action = params.get("action") or "run"
    by = _who(params)
    if action == "run":
        r = agent.run(by, params.get("goal") or params.get("text") or "",
                      {"deep": params.get("deep", True)})
        if not r.get("ok"):
            return r
        return {"ok": True, "module": r.get("module") or "ask",
                "reply": agent.tradec_to_md(r.get("trace")) + r.get("answer_md", ""),
                "plan": r.get("plan"), "trace": r.get("trace"),
                "planner": r.get("planner"), "used_llm": r.get("used_llm")}
    if action == "catalog":
        return {"ok": True, "tools": agent.tool_catalog()}
    if action == "last":
        return {"ok": True, "turn": agent.last_trace(by)}
    if action == "reset":
        return agent.reset(by)
    return {"ok": False, "error": "未知操作 %s" % action}


def api_dataset(params):
    """实验数据集：从论文/开题提取数据集名 → Zenodo 检索 → 下载。"""
    action = params.get("action") or "search"
    if action == "extract":
        text = params.get("text") or ""
        if len(text) < 50:
            return {"ok": False, "error": "请先粘贴论文/开题文本（≥50 字）"}
        return {"ok": True, "names": wb.ds_extract_names(text),
                "ai_note": "名称来自文本模式匹配；检索结果以真实数据源返回为准。"}
    if action == "search":
        q = (params.get("query") or "").strip()
        if not q:
            return {"ok": False, "error": "检索词不能为空"}
        return wb.ds_search_zenodo(q, size=int(params.get("size") or 5))
    if action == "download":
        return wb.ds_download(params.get("url"), params.get("dest") or "misc",
                              params.get("filename"))
    if action == "downloaded":
        return {"ok": True, "downloaded": wb.ds_list_downloaded()}
    return {"ok": False, "error": "未知操作 %s" % action}


def api_model_list(params):
    """对话模型选择：列出可用预设与各自 Key 状态。"""
    cfg = model.load_cfg()
    out = []
    for name, p in model.PRESETS.items():
        out.append({"id": name, "label": p["label"],
                    "is_current": cfg.get("preset") == name,
                    "has_key": bool(cfg.get("api_key")) if p["backend"] == "openai" else True,
                    "need_key": p["backend"] == "openai"})
    return {"ok": True, "models": out,
            "current": cfg.get("preset", "hunyuan")}


# 我的论文：Word 导入（base64）与文件保存
def _safe_id(v):
    """id 仅允许字母数字下划线连字符，防路径穿越。"""
    v = str(v or "")
    return v if re.fullmatch(r"[\w\-]{1,64}", v) else ""


def _mypaper_file(mid, by):
    mid = _safe_id(mid)
    if not mid:
        return os.path.join(wb._DATA_DIR, "paper_files", "_invalid_.docx")
    return os.path.join(wb._DATA_DIR, "paper_files", "%s_%s.docx" % (mid, re.sub(r"[^\w\-]", "", by or "u")))


def api_mypaper(params):
    """我的论文：个人论文台账（增删改查，仅本人可见可改）+ Word 导入与下载。"""
    action = params.get("action") or "list"
    if action in ("update", "remove", "get_text") and not _safe_id(params.get("id")):
        return {"ok": False, "error": "非法的条目 id"}
    by = _who(params)
    if action == "add":
        return wb.mp_add(by, params)
    if action == "list":
        shared = []
        if by:
            for p in store.list_papers():
                perms = {c.get("name"): c.get("perm") for c in p.get("collaborators", [])}
                if by in perms:
                    shared.append({
                        "id": p["id"], "title": p.get("title", ""),
                        "status": p.get("status", ""), "target_journal": p.get("target_journal", ""),
                        "owner": p.get("owner", ""), "creator": p.get("created_by", ""),
                        "perm": perms[by]})
        papers = wb.mp_list(by)
        for p in papers:
            fp = _mypaper_file(p["id"], by)
            if os.path.isfile(fp):
                p["file_name"] = os.path.basename(fp)
        return {"ok": True, "papers": papers, "shared": shared}
    if action == "upload_word":
        import base64
        data_b64 = (params.get("data_b64") or "").strip()
        title = (params.get("title") or "").strip()
        if not data_b64:
            return {"ok": False, "error": "未收到文件数据"}
        try:
            raw = base64.b64decode(data_b64)
        except Exception as e:
            return {"ok": False, "error": "文件解码失败：%s" % e}
        if len(raw) > 20 * 1024 * 1024:
            return {"ok": False, "error": "文件超过 20MB 上限"}
        r = wb.mp_add(by, {"title": title or "导入的论文", "ptype": params.get("ptype") or "期刊论文",
                           "status": params.get("status") or "writing"})
        if not r.get("ok"):
            return r
        mid = r["papers"][0]["id"]
        fp = _mypaper_file(mid, by)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as f:
            f.write(raw)
        # 从 docx 提取全文（供 AI 训练设计/精读调用），并持久化
        text_head = ""
        try:
            import zipfile
            with zipfile.ZipFile(fp) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
            paras = [re.sub(r"<[^>]+>", "", t) for t in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)]
            full = "\n".join(x for x in paras if x.strip())[:300000]
            text_head = full[:1500]
            tp_dir = os.path.join(wb._DATA_DIR, "paper_texts")
            os.makedirs(tp_dir, exist_ok=True)
            with open(os.path.join(tp_dir, "%s_%s.txt" % (mid, re.sub(r"[^\w\-]", "", by or "u"))), "w", encoding="utf-8") as f:
                f.write(full)
            guess = paras[0].strip()[:120] if paras else ""
            if guess and (not title):
                wb.mp_update(mid, by, {"title": guess})
        except Exception:
            pass
        papers = wb.mp_list(by)
        for p in papers:
            if p["id"] == mid:
                p["file_name"] = os.path.basename(fp)
        return {"ok": True, "papers": papers, "imported_text_head": text_head,
                "note": "Word 已导入并保存到该条目；正文前 1500 字提取成功，可用于「论文精读」。" if text_head
                        else "Word 已保存，但未能提取正文（可能不是标准 .docx）。"}
    if action == "get_text":
        fp = os.path.join(wb._DATA_DIR, "paper_texts", "%s_%s.txt" % (_safe_id(params.get("id")) or "_none_", re.sub(r"[^\w\-]", "", by or "u")))
        if not os.path.isfile(fp):
            return {"ok": False, "error": "该条目没有已导入的 Word 全文（请先在「我的论文」导入 .docx）"}
        with open(fp, "r", encoding="utf-8") as f:
            return {"ok": True, "text": f.read()[:120000]}
    if action == "update":
        return wb.mp_update(params.get("id"), by, params)
    if action == "remove":
        r = wb.mp_remove(params.get("id"), by)
        for fp in (_mypaper_file(params.get("id"), by),
                   os.path.join(wb._DATA_DIR, "paper_texts", "%s_%s.txt" % (_safe_id(params.get("id")) or "_none_", re.sub(r"[^\w\-]", "", by or "u")))):
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
        fp = _mypaper_file(params.get("id"), by)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
            except Exception:
                pass
        return r
    return {"ok": False, "error": "未知操作 %s" % action}
def api_stats(params):
    """实验统计分析：描述统计 + Welch t（可选 AI 解读）。"""
    text = params.get("text") or ""
    if not text.strip():
        return {"ok": False, "error": "请按格式粘贴数据，例如：\n组A: 0.71, 0.68, 0.80, 0.75\n组B: 0.60, 0.55, 0.66, 0.58"}
    r = wb.stat_groups(text)
    if r.get("ok") and params.get("ai") and model.status().get("ok"):
        try:
            interp = model.quick_ask(
                "请解读下面的实验统计结果（只基于给定数字，不添加任何未给出的数据或结论）：\n\n" + r["markdown"],
                system="你是严谨的数据解读助手：只解释算出来的数值意味着什么、有哪些注意事项（样本量、标准差、效应量），不编造因果或文献。",
                max_tokens=600, temperature=0.3)
            r["markdown"] += "\n\n## AI 解读\n" + interp
            r["ai_interpret"] = True
        except Exception:
            pass
    return r


def api_memory(params):
    """长期记忆：按当前登录用户存取，AI 问答会自动带上。"""
    action = params.get("action") or "list"
    by = _who(params)
    if action == "add":
        return wb.mem_add(params.get("text"), params.get("kind"), by)
    if action == "list":
        return {"ok": True, "entries": wb.mem_list(by=by)}
    if action == "remove":
        return wb.mem_remove(params.get("id"), by=by)
    return {"ok": False, "error": "未知操作 %s" % action}


def api_collab(params):
    """合作对接：向其他注册用户发起/处理合作邀请。"""
    action = params.get("action") or "list"
    by = _who(params)
    if action == "invite":
        return wb.inv_send(by, params.get("to"), params.get("message") or "", params.get("paper") or "")
    if action == "list":
        return {"ok": True, **wb.inv_lists(by)}
    if action == "accept":
        return wb.inv_respond(params.get("id"), by, accept=True)
    if action == "decline":
        return wb.inv_respond(params.get("id"), by, accept=False)
    if action == "cancel":
        return wb.inv_cancel(params.get("id"), by)
    return {"ok": False, "error": "未知操作 %s" % action}


def api_guide(params):
    """论文引导：按用户保存每步完成时间。steps 形如 {"s1": "2026-09-07 00:20", ...}"""
    by = _who(params)
    if params.get("action") == "save":
        steps = params.get("steps") or {}
        if not isinstance(steps, dict):
            return {"ok": False, "error": "steps 格式错误"}
        return {"ok": True, **wb.guide_save(by, steps)}
    return {"ok": True, "steps": wb.guide_load(by)}


def api_algo(params):
    """算法生成（训练脚本/伪代码/训练方案）与代码校验（静态 + AI 复审）。"""
    action = params.get("action") or "gen"
    if action == "gen":
        return wb.algo_gen(params.get("kind"), params.get("desc"))
    if action == "check":
        return wb.algo_check(params.get("code"), ai_review=bool(params.get("ai")))
    return {"ok": False, "error": "未知操作 %s" % action}


_AUTO_LANG = {
    "en": ("English", "学术英语"),
    "zh": ("Chinese", "学术中文"),
}


def api_autopaper(params):
    """开题报告 → 论文初稿流水线（分步驱动：outline → section×N → abstract）。"""
    if not model.status()["ok"]:
        return _model_unavailable()
    action = params.get("action") or "outline"
    lang_key = params.get("lang") if params.get("lang") in _AUTO_LANG else "en"
    lang, lang_zh = _AUTO_LANG[lang_key]
    journal = (params.get("journal") or "the target journal").strip()
    proposal = (params.get("proposal") or "").strip()
    base_sys = ("You are an rigorous academic paper writer. You must base every sentence on the THESIS PROPOSAL "
                "provided by the user; never invent experimental numbers, citations outside the proposal's reference "
                "list, or claims the proposal does not support. Where experiment results are required but not yet "
                "available, insert the placeholder 【TBD: 待补实验数据】. Do not fabricate references. Output in %s." % lang)
    if action == "outline":
        if len(proposal) < 200:
            return {"ok": False, "error": "请先粘贴开题报告全文（至少 200 字符）"}
        sys_p = base_sys + (" You are now designing the paper skeleton. Read the proposal and return STRICT JSON only "
                            "(no markdown fence, no prose): {\"title\": \"paper title in %s\", \"sections\": ["
                            "{\"title\": \"section title (e.g. I. Introduction)\", \"req\": \"one-line what this section "
                            "must cover, grounded in the proposal\"}]} — 7 to 9 sections covering Introduction, Related Work, "
                            "framework/method, experiment setup, results (mark it needs TBD data), discussion, conclusion. "
                            "Target venue: %s." % (lang, journal))
        try:
            raw = model.quick_ask(proposal[:12000], system=sys_p, max_tokens=1200, temperature=0.3)
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0))
            secs = [s for s in data.get("sections", []) if isinstance(s, dict) and s.get("title")]
            if not secs:
                return {"ok": False, "error": "大纲 JSON 解析成功但没有章节，请重试"}
            return {"ok": True, "title": data.get("title", ""), "sections": secs[:10], "outline_raw": raw}
        except Exception as e:
            return {"ok": False, "error": "大纲生成/解析失败：%s（可重试）" % e}
    if action == "section":
        sec_title = (params.get("sec_title") or "").strip()
        if not sec_title or not proposal:
            return {"ok": False, "error": "缺少章节名或开题报告"}
        sys_p = base_sys + (" You are writing ONE section of the paper (title: %s; target venue: %s). "
                            "The full paper outline is provided; write ONLY this section: 300-600 words, academic tone, "
                            "with subsections if needed. Cite only references that exist in the proposal using [n] markers. "
                            "If the section needs experimental results, use 【TBD: 待补实验数据】 placeholders with a note "
                            "of which table/figure will hold them." % (sec_title, journal))
        body = "### OUTLINE\n%s\n\n### PROPOSAL\n%s\n\n### WRITE THIS SECTION\n%s\n%s" % (
            (params.get("outline") or "")[:4000], proposal[:10000],
            sec_title, (params.get("sec_req") or ""))
        try:
            text = model.quick_ask(body, system=sys_p, max_tokens=1400, temperature=0.4)
            return {"ok": True, "text": text.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    if action == "abstract":
        body = (params.get("body") or "")[:14000]
        if len(body) < 200:
            return {"ok": False, "error": "正文为空，无法生成摘要"}
        sys_p = base_sys + (" Write the paper's Abstract (180-250 words) followed by a line 'Index Terms: ...' "
                            "(5-7 terms). Base it strictly on the paper body given; keep any 【TBD】 claims phrased as "
                            "expected/initial results, and do not invent numbers.")
        try:
            text = model.quick_ask(body, system=sys_p, max_tokens=700, temperature=0.3)
            return {"ok": True, "text": text.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "未知操作 %s" % action}


DIGEST_KINDS = {
    "tech": ("技术点分析",
             "请基于给定论文文本做**技术点分析**（Markdown）：## 方法框架（各模块及其输入输出）→ "
             "## 关键技术（每个技术点：作用/实现思路/依赖条件）→ ## 实验设计要点（数据集/指标/对比）。"
             "只依据文本中真实出现的内容；文本没写清的地方标注【原文未详】。"),
    "innovation": ("创新点总结",
                   "请基于给定论文文本总结**创新点**（Markdown）：## 文中声称的创新点（逐条列出）→ "
                   "## 实际支撑程度（每条：有实验支撑/仅口头声称/部分支撑）→ ## 与已有工作的差异（仅当文本有线索）。"
                   "不得替论文编造未声称的创新点或实验结果。"),
    "feasibility": ("可行性分析",
                    "请基于给定论文文本做**可行性分析**（Markdown）：## 复现所需资源（数据/算力/代码/人天估计）→ "
                    "## 难点与风险（逐条，注明依据）→ ## 若移植到自己课题的适配建议。只依据文本；"
                    "文本未提及的资源需求标注【需向作者确认】。"),
    "gap": ("空白研究分析",
            "请基于给定论文文本做**研究空白分析**（Markdown）：## 论文自己承认的局限 → "
            "## 从文本推断的未覆盖点（注明推断依据）→ ## 可延伸的研究方向（每条给一句为什么值得做）。"
            "区分『原文承认』与『你的推断』，不编造文献。"),
    "translate": ("翻译",
                  "请将给定文本做**学术翻译**：自动判断原文语言——英文则译成中文，中文则译成英文。"
                  "要求：术语准确、保持段落结构；专有名词首次出现时括注原文；不增删信息。只输出译文。"),
}


def api_digest(params):
    """论文精读：技术点/创新点/可行性/空白/翻译。只基于给定文本，不编造。"""
    if not model.status()["ok"]:
        return _model_unavailable()
    kind = params.get("kind") or "tech"
    if kind not in DIGEST_KINDS:
        return {"ok": False, "error": "未知精读类型 %s" % kind}
    text = (params.get("text") or "").strip()
    if len(text) < 100:
        return {"ok": False, "error": "请粘贴论文全文/章节/摘要（至少 100 字）再精读"}
    label, sys_p = DIGEST_KINDS[kind]
    sys_p += (" 回答用 Markdown；你是严谨的论文精读助手，只依据给定文本，"
              "不引入文本之外的文献或数据。")
    try:
        reply = model.quick_ask(text[:12000], system=sys_p, max_tokens=1600, temperature=0.3)
        return {"ok": True, "reply": reply, "kind": kind, "kind_zh": label, "ai": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


HELP_TEXT = (
    "你可以这样和我说话（自然语言即可）：\n"
    "- 检索 UAV 入侵检测 近三年论文\n"
    "- 把刚才的结果生成对比矩阵\n"
    "- 帮我选刊：<在此粘贴摘要>\n"
    "- 推荐下个研究方向\n"
    "- 看看我的科研画像\n"
    "- 出图：<在此粘贴 CSV>\n"
    "- 粘贴个人简历或擅长技术 → 我按十步走帮你定研究方向\n"
    "- 确认方向：<最终方向>（定稿后进入文献调研）\n"
    "- 记住：实验一律先跑 3 个随机种子（写入长期记忆）\n"
    "- 查看记忆 / 删除记忆 <关键词> / 我的论文\n"
    "- 我的待办 / 我发起的流程 / 发起流程（流程中心：审批流转）\n"
    "- 一键全流程（自动跑：定方向→文献调研→创新点/可行性→苏格拉底问询定题→提交人工审核）\n"
    "- 苏格拉底 / 定题目 / 全流程进度\n"
    "\n**多步任务（智能体自动串起来做）：**\n"
    "- 检索 UAV 入侵检测 最新论文，然后精读前三篇，再帮我选刊\n"
    "- 检索 图神经网络 入侵检测，然后生成对比矩阵并收录到文献库\n"
    "- /智能模式 <任意复杂目标>（强制执行「规划→工具→观察→反思」全过程）"
)


def _strip_query(text):
    stop = ['请', '帮我', '帮忙', '想', '要', '做', '一下', '检索', '搜索', '查', '找',
            '文献', '论文', '相关', '关于', '方面', '方向', '近', '年以来', '从', '到',
            '之间', '前', '篇', '篇文', '生成', '对比', '矩阵', '出图', '画图', '画个',
            '图表', '折线', '柱状', '散点', '选刊', '投稿', '投到', '投给', '投哪',
            '看看', '我的', '画像', '推荐', '推荐一下', '给我', '并', '然后', '再', '也']
    q = text
    for w in stop:
        q = q.replace(w, ' ')
    q = re.sub(r'\s+', ' ', q).strip(' ,，。.、:：')
    return q


# 智能体接管的信号词：长任务/多步动作/平台操作
_AGENT_VERBS = ["检索", "搜", "查一下", "查查", "找找", "分析", "精读", "选刊", "投稿",
                "文献", "论文", "实验", "画像", "记忆", "待办", "审批", "流程", "题目",
                "方向", "矩阵", "收录", "摘要", "整理", "总结", "生成", "帮我", "记一下",
                "全流程", "苏格拉底", "选题"]


def _agent_reply(by, text, force=False):
    """试着让智能体接管（规划→工具→观察→反思）。

    返回 None 表示「不接管」，交回原有意图路由，保证既有行为不回归。
    """
    try:
        steps = agent.plan_rules(text)
    except Exception:
        steps = []
    gate = force or len(steps) >= 2 or (len(text) >= 4 and any(k in text for k in _AGENT_VERBS))
    if not gate:
        return None
    try:
        r = agent.run(by, text)
    except Exception as e:
        return {"ok": True, "reply": "智能体执行异常：%s" % e, "module": "help"}
    if not r.get("ok"):
        return None
    trace = r.get("trace") or []
    real_tools = [t.get("tool") for t in trace
                  if t.get("tool") not in ("-", "finish", "ask_model")]
    terminal = next((t for t in trace if t.get("tool") == "finish"), None)
    # 纯问答（只有 ask_model）时不接管，交回 api_ask 得到干净回答
    if not force and not real_tools and not terminal:
        return None
    reply = agent.tradec_to_md(trace) + r.get("answer_md", "")
    return {"ok": True, "reply": reply, "module": r.get("module") or "ask",
            "agent": True, "planner": r.get("planner"), "steps": r.get("steps"),
            "plan": r.get("plan"), "trace": trace}


def _year_from(text, cur=None):
    cur = cur or time.gmtime().tm_year
    m = re.search(r'(\d{4})\s*年以来', text) or re.search(r'从\s*(\d{4})', text)
    if m:
        return int(m.group(1))
    m = re.search(r'近\s*(\d+)\s*年', text)
    if m:
        return cur - int(m.group(1)) + 1
    return None


def _limit(text):
    m = re.search(r'(\d+)\s*篇', text)
    if m:
        return min(30, int(m.group(1)))
    return 15


def _strip_abstract(text):
    m = re.search(r'[:：]', text)
    if m and len(text[m.end():].strip()) >= 20:
        return text[m.end():].strip()
    s = text
    for w in ['帮我', '请', '帮忙', '想', '要', '做', '一下', '选刊', '投稿', '投到',
              '投给', '投哪', '匹配', '期刊', '：', ':', '。']:
        s = s.replace(w, ' ')
    return re.sub(r'\s+', ' ', s).strip(' ,，。.、:：')


def api_chat(params):
    """自然语言入口：识别意图 → 调用对应模块 → 返回 Markdown 回复。"""
    text = (params.get("text") or "").strip()
    if not text:
        return {"ok": True, "reply": HELP_TEXT, "module": "help"}

    # 0) 智能体优先：显式指令（/xxx、智能模式）或检测到**多步目标**时交给智能体接管
    #    单步请求不接管，继续走下面的意图路由，保证既有行为零回归。
    if text.startswith("/") or "智能模式" in text:
        _ar = _agent_reply(_who(params),
                           text.lstrip("/").replace("智能模式", "").strip() or text, force=True)
        if _ar:
            return _ar
    try:
        if len(agent.plan_rules(text)) >= 2:
            _ar = _agent_reply(_who(params), text)
            if _ar:
                return _ar
    except Exception:
        pass

    # 0) 长期记忆：记住：… / 查看记忆 / 删除记忆 <关键词>
    m = re.match(r"^(?:帮我)?(?:记住|记一下)[：:，,]?\s*(.+)$", text, re.S)
    if m:
        content = m.group(1).strip()
        if any(k in content for k in ["经验", "踩坑", "教训", "试过", "别再"]):
            kind = "experience"
        elif any(k in content for k in ["约定", "规矩", "以后都", "默认", "一律"]):
            kind = "rule"
        else:
            kind = "note"
        r = wb.mem_add(content, kind, _who(params))
        if r.get("ok"):
            return {"ok": True, "reply": "已记住（%s）：%s\n\n> 到「长期记忆」标签页可管理；智能对话的 AI 问答会自动带上。"
                    % ({"note": "备忘", "knowledge": "知识", "experience": "经验", "rule": "约定"}[kind],
                       content[:100]), "module": "memory"}
        return {"ok": True, "reply": "记不住：" + (r.get("error") or ""), "module": "memory"}
    if any(k in text for k in ["查看记忆", "我的记忆", "记忆列表", "看看记忆"]):
        es = wb.mem_list(by=_who(params))
        if not es:
            return {"ok": True, "reply": "你还没有长期记忆。对我说「记住：<内容>」，或到「长期记忆」标签页添加。",
                    "module": "memory"}
        md = ["# 我的长期记忆（%d 条）" % len(es), ""]
        for e in es[:20]:
            md.append("- [%s｜%s] %s" % (e.get("ts", ""), e.get("kind_zh", "备忘"),
                                         (e.get("text") or "").replace("\n", " ")[:120]))
        if len(es) > 20:
            md.append("\n> 仅显示最近 20 条，完整列表见「长期记忆」标签页。")
        return {"ok": True, "reply": "\n".join(md), "module": "memory"}
    m = re.match(r"^(?:删除记忆|忘掉|删掉记忆)[：:\s]?(.+)$", text)
    if m:
        kw = m.group(1).strip()
        es = wb.mem_list(by=_who(params))
        hit = next((e for e in es if kw and (kw in e.get("text", "") or kw in e.get("id", ""))), None)
        if not hit:
            return {"ok": True, "reply": "没找到匹配「%s」的记忆。说「查看记忆」看看有哪些。" % kw, "module": "memory"}
        wb.mem_remove(hit["id"], by=_who(params))
        return {"ok": True, "reply": "已删除记忆（%s）：%s" % (hit.get("ts", ""), hit.get("text", "")[:80]),
                "module": "memory"}

    # 0.33) 智能体能力自述：工具清单（让人知道智能体能干什么）
    if any(k in text for k in ["你会什么", "你能做什么", "有什么工具", "工具列表", "能力清单",
                               "智能体能干什么", "你有哪些能力"]):
        cats = agent.tool_catalog()
        md = ["# 我可以调用的工具（%d 个）" % len(cats), "",
              "你说一句带目标的话，我会**规划 → 调用这些工具 → 看结果 → 必要时补一步**，"
              "把多步任务真正串起来做完。", ""]
        for t in cats:
            if t["name"] == "finish":
                continue
            ps = "、".join(t["params"].keys()) or "无参数"
            md.append("- **%s** — %s（参数：%s）" % (t["name"], t["desc"], ps))
        md += ["", "**例**：`检索 UAV 入侵检测 最新论文，然后精读前三篇，再帮我选刊`"]
        return {"ok": True, "reply": "\n".join(md), "module": "agent"}

    # 0.34) 全流程一键编排（①定方向→②调研→③创新点/可行性→④苏格拉底定题→⑤人工审核）
    if any(k in text for k in ["一键全流程", "全流程", "一键跑完", "自动化全流程", "全自动",
                               "跑流程", "跑全流程", "一键完成", "继续全流程", "接着跑"]):
        resume = ""
        m0 = re.search(r"(?:简历|方向|我的方向)[：:]\s*(.+)$", text, re.S)
        if m0 and len(m0.group(1).strip()) >= 20:
            resume = m0.group(1).strip()
        only = ["search", "collect", "matrix"] if "文献" in text and "题目" not in text else []
        r = api_pipeline({"action": "run", "_user": params.get("_user"),
                          "resume_text": resume, "only": only})
        rep = r.get("report_md") or ""
        return {"ok": True, "reply": rep, "module": "bpm" if r.get("review_inst") else "workflow"}
    if any(k in text for k in ["苏格拉底", "苏格拉底式", "问询", "追问"]):
        r = api_pipeline({"action": "run", "_user": params.get("_user"),
                          "only": ["socratic", "topic", "review"]})
        return {"ok": True, "reply": r.get("report_md") or "", "module": "bpm"}
    if any(k in text for k in ["定题目", "确定题目", "生成题目", "拟定题目", "起个题目"]):
        r = api_pipeline({"action": "run", "_user": params.get("_user"),
                          "only": ["socratic", "topic", "review"]})
        return {"ok": True, "reply": r.get("report_md") or "", "module": "bpm"}
    if any(k in text for k in ["提交审核", "送审", "提交人工审核", "交给人工审核"]):
        r = api_pipeline({"action": "run", "_user": params.get("_user"), "only": ["review"]})
        return {"ok": True, "reply": r.get("report_md") or "", "module": "bpm"}
    if any(k in text for k in ["全流程进度", "全流程状态", "流程跑到哪"]):
        st = pipeline.state(_who(params))
        return {"ok": True, "reply": pipeline.report_md(st), "module": "workflow"}

    # 0.35) 流程中心（BPM 审批流转）：待办 / 我发起的 / 抄送 / 发起
    if any(k in text for k in ["我的待办", "待办任务", "待我审批", "我的审批", "待办审批"]):
        items = bpm.todo(_who(params))
        if not items:
            return {"ok": True, "module": "bpm",
                    "reply": "你当前没有待办审批任务。\n\n"
                             "去「🔀 流程中心 → 发起流程」提交一个审批；内置模板："
                             "论文送审审批、开题报告审批、实验资源申请、通用审批。"}
        md = ["# 我的待办（%d 条）" % len(items), ""]
        for t in items[:15]:
            md.append("- **%s** — 流程「%s」 · 当前节点：%s · 发起人：%s" % (
                t.get("inst_title") or "(无标题)", t.get("def_name"),
                t.get("name"), t.get("initiator")))
        md.append("\n> 在「🔀 流程中心 → 我的待办」可执行：通过 / 驳回 / 转办 / 委派 / 加签 / 抄送 / 催办。")
        return {"ok": True, "reply": "\n".join(md), "module": "bpm"}
    if any(k in text for k in ["我发起的流程", "我的流程", "流程进度", "流程跟踪", "流程实例"]):
        items = bpm.mine(_who(params))
        if not items:
            return {"ok": True, "module": "bpm",
                    "reply": "你还没有发起过流程。去「🔀 流程中心 → 发起流程」选一个模板试试。"}
        md = ["# 我发起的流程（%d 条）" % len(items), ""]
        for i in items[:15]:
            prog = " → ".join("%s%s" % (p.get("name"), "✓" if p.get("state") == "done"
                                      else ("●" if p.get("state") == "current" else "○"))
                             for p in bpm.progress(i))
            md.append("- **%s**（%s）\n  %s" % (
                i.get("title"), bpm.STATUS_ZH.get(i.get("status"), i.get("status")), prog))
        md.append("\n> 完整跟踪与撤销见「🔀 流程中心 → 我发起的」。")
        return {"ok": True, "reply": "\n".join(md), "module": "bpm"}
    if any(k in text for k in ["我的抄送", "抄送的流程", "抄送我的"]):
        items = bpm.cc_list(_who(params))
        md = ["# 我的抄送（%d 条）" % len(items), ""] if items else []
        if not items:
            return {"ok": True, "module": "bpm", "reply": "还没有抄送给你的流程。"}
        for c in items[:15]:
            md.append("- **%s**（%s）· 抄送人：%s · %s" % (
                c.get("title"), c.get("status"), c.get("by"), c.get("ts")))
        return {"ok": True, "reply": "\n".join(md), "module": "bpm"}
    if any(k in text for k in ["发起流程", "发起审批", "提交审批", "流程中心", "工作流审批", "审批流程", "新建流程"]):
        defs = bpm.def_list(_who(params))
        md = ["# 流程中心\n", "可用的流程定义（%d 个）：" % len(defs), ""]
        for d in defs:
            nodes = " → ".join(n.get("name", "") for n in (d.get("nodes") or []))
            md.append("- **%s**（%s）　%s" % (d.get("name"), d.get("category"), nodes))
        md.append("\n> 到「🔀 流程中心」：**发起流程**（选模板 + 填表单）、**我的待办**（审批）、"
                  "**流程设计**（自定义节点/审批人/会签/条件分支）、**流程监控**（管理员）。")
        return {"ok": True, "reply": "\n".join(md), "module": "bpm"}

    # 0.4) 工作流：简历/技能 → 十步走到定方向；确认方向
    if any(k in text for k in ["个人简历", "简历", "启动工作流", "开始十步走", "十步走启动", "从简历开始"]) \
            or ("擅长" in text and any(k in text for k in ["方向", "研究", "论文"])):
        r = api_workflow({"action": "from_resume", "text": text, "_user": params.get("_user")})
        if not r.get("ok"):
            return {"ok": True, "reply": "启动失败：" + (r.get("error") or ""), "module": "workflow"}
        reply = ("**十步走 · 第①步：定方向（AI 已根据你的材料完成初判）**\n\n" + r.get("analysis_md", "") +
                 "\n\n**建议定稿的最终方向**：" + r.get("direction_final", "") +
                 "\n\n> 已把方向与技能栈写入「科研画像」（可人工修改）。"
                 "\n> 确认无误后，回复「**确认方向：<照抄上面的最终方向>**」，"
                 "或直接去「科研画像」改完保存，然后「带着方向去文献调研」。")
        return {"ok": True, "reply": reply, "module": "workflow"}
    m_dir = re.match(r"^确认方向[：:]\s*(.+)$", text, re.S)
    if m_dir:
        d = m_dir.group(1).strip()
        r = wb.wf_set_direction(_who(params), d)
        if r.get("ok"):
            return {"ok": True, "reply": "✅ 最终研究方向已定稿：**%s**\n\n下一步：去「文献检索」点「用我的最终方向调研」，"
                                         "或直接说「检索 <关键词>」。十步走进度可在「论文十步走」页查看。" % d[:120],
                    "module": "workflow"}
        return {"ok": True, "reply": "定稿失败：" + (r.get("error") or ""), "module": "workflow"}

    # 0.5) 我的论文（个人台账）
    if "我的论文" in text:
        ps = wb.mp_list(_who(params))
        if not ps:
            return {"ok": True, "reply": "「我的论文」还没有记录。到「我的论文」标签页添加（标题/类型/目标期刊/截止日期），"
                                         "要与其他作者合作可到同页「合作对接」发起邀请。", "module": "mypaper"}
        md = ["# 我的论文（%d 篇）" % len(ps), "",
              "| 标题 | 类型 | 目标期刊 | 状态 | 截止 |", "|---|---|---|---|---|"]
        for p in ps:
            md.append("| %s | %s | %s | %s | %s |" % (
                (p.get("title") or "").replace("|", "\\|"), p.get("ptype") or "—",
                p.get("target_journal") or "—", wb.MP_STATUS.get(p.get("status", ""), p.get("status", "")),
                p.get("deadline") or "—"))
        md.append("\n> 改状态/删除请到「我的论文」标签页；与其他作者合作在「合作对接」发邀请。")
        return {"ok": True, "reply": "\n".join(md), "module": "mypaper"}

    # 1) 选刊
    if any(k in text for k in ['选刊', '投稿', '投到', '投给', '投哪', 'journal',
                                'submit', 'where to submit']):
        rest = _strip_abstract(text)
        if len(rest) >= 40:
            r = api_journal({"abstract": rest, "target_if": 7, "top": 6, "min_if": 0})
            if r.get("ok"):
                warn = r.get("warning", "")
                return {"ok": True, "reply": r["markdown"] + ("\n\n> " + warn if warn else ""),
                        "module": "journal"}
            return {"ok": True, "reply": "选刊失败：" + r.get("error", ""), "module": "journal"}
        return {"ok": True, "reply": "请把论文摘要贴在「选刊」标签页，或在这里直接发给我，格式：\n\n"
                                       "> 帮我选刊：<粘贴摘要>\n\n摘要至少 40 字，英文最佳。",
                "module": "journal"}

    # 2) 出图（需要 CSV，引导用户粘贴）
    if any(k in text for k in ['出图', '画图', '画个图', '图表', '折线', '柱状', '散点',
                                'plot', 'chart']) and 'csv' not in text.lower():
        return {"ok": True, "reply": "出图需要 CSV 数据。把 CSV（首行表头）直接发给我，例如：\n\n"
                                       "> 出图：\n> epochs,Ours,GAT\n> 10,0.71,0.68\n> 20,0.80,0.76\n\n"
                                       "或直接用「出图」标签页填表。", "module": "plot"}

    # 2.5) AI 画像诊断 / AI 深度方向推荐（真模型判断）
    _ai_word = any(k in text for k in ['AI', 'ai', '智能', '深度', '诊断', '评估'])
    if _ai_word and any(k in text for k in ['画像', '方向', '推荐', '分析']):
        if any(k in text for k in ['推荐', '方向', '下一步']):
            r = api_ai_recommend({"offline": True, "max_queries": 8, "years": 3, "limit": 10})
            if not r.get("ok"):
                return {"ok": True, "reply": "深度推荐需要先配置模型：\n\n" +
                        (r.get("reason") or r.get("error") or "") +
                        "\n\n> 到「模型与API」选「腾讯混元」预设并填写 OpenAI 兼容 API（api_key/model，base_url 自动填好）或安装本地 Ollama。",
                        "module": "ai"}
            return {"ok": True, "reply": r["reply"] +
                    "\n\n---\n\n**附：规则引擎候选（AI 判断依据）**\n\n" + r["rule_md"],
                    "module": "ai"}
        if any(k in text for k in ['画像', 'profile']):
            r = api_ai_profile({})
            if not r.get("ok"):
                return {"ok": True, "reply": "画像 AI 诊断需要先配置模型：\n\n" +
                        (r.get("reason") or r.get("error") or ""),
                        "module": "ai"}
            return {"ok": True, "reply": r["reply"], "module": "ai"}

    # 3) 方向推荐（默认离线骨架，快且稳）
    if any(k in text for k in ['方向', '推荐', '下一步', '做什么', 'recommend', 'next']) \
            and '画像' not in text:
        r = api_recommend({"offline": True, "max_queries": 6, "years": 3, "limit": 10})
        if r.get("ok"):
            return {"ok": True, "reply": r["markdown"] +
                    "\n\n> 当前为**离线骨架**（热度/复用/数据列待联网补）。"
                    "要算真实热度，请用「方向推荐」标签页取消离线勾选。", "module": "recommend"}
        return {"ok": True, "reply": "推荐失败：" + r.get("error", ""), "module": "recommend"}

    # 4) 科研画像
    if any(k in text for k in ['画像', '我是谁', '关于我', 'profile', 'who am i']):
        r = api_profile({})
        if r.get("ok"):
            return {"ok": True, "reply": "```\n" + r["text"] + "\n```", "module": "profile"}
        return {"ok": True, "reply": "解析失败：" + r.get("error", ""), "module": "profile"}

    # 5) 团队任务板（“论文”字样的请求让位给 6 的论文协作分支）
    if any(k in text for k in ['任务', '分工', '派活', '进度', '派个']) \
            and not any(k in text for k in ['论文', 'paper', '写作']):
        act = "add" if any(k in text for k in ['添加任务', '加个任务', '新建任务', '创建任务', '派个']) else "list"
        if act == "add":
            title = re.sub(r'(添加任务|加个任务|新建任务|创建任务|派个|给我|请|帮我|一个|个)', ' ', text)
            title = re.sub(r'\s+', ' ', title).strip(' ，,。')
            if title:
                store.add_task({"title": title, "assignee": "",
                                "priority": "中", "module": "general"})
                return {"ok": True,
                        "reply": "已添加任务：**%s**\n\n%s" % (title, render_task_board(store.list_tasks())),
                        "module": "task"}
            return {"ok": True, "reply": "想添加什么任务？把标题发给我，例如：\n> 添加任务 补实验：拓扑变化鲁棒性",
                    "module": "task"}
        r = api_task({"action": "list"})
        return {"ok": True, "reply": r["markdown"] +
                "\n\n> 指派成员、改状态请到「协作中心」标签页操作。", "module": "task"}

    # 6) 论文协作
    if any(k in text for k in ['论文', 'paper', '章节', '分工写', '草稿', '写作进度']):
        r = api_paper({"action": "list"})
        md = r["markdown"]
        if r.get("papers") and any(k in text for k in ['意见', '审阅', '评论', '评审']):
            md += "\n\n> 写审阅意见请用「协作中心」→ 某篇论文 → 添加意见。"
        return {"ok": True, "reply": md + "\n\n> 新建论文/拆分章节/写审阅意见，去「协作中心」标签页。",
                "module": "paper"}

    # 7) 实验台账与复现（仅"台账/登记/查/复现"等命令句才路由，普通含"实验"的问句交给 AI/兜底）
    if ('实验' in text or '复现' in text) and any(k in text for k in
            ['台账', '登记', '记录', '新建', '注册', '进度', '复现', '看看', '查看', '查一下', '有哪些', '列表', '状态']):
        act = "add" if any(k in text for k in ['登记', '记录', '新建实验', '注册']) else "list"
        if act == "add":
            name = re.sub(r'(登记|记录|新建|注册|实验|请|帮我|一个|个)', ' ', text)
            name = re.sub(r'\s+', ' ', name).strip(' ，,。')
            if len(name) >= 3:
                store.add_experiment({"name": name, "owner": "",
                                      "metrics": [], "status": "registered"})
                return {"ok": True,
                        "reply": "已登记实验：**%s**\n\n%s" % (name, render_exp_table(store.list_experiments())),
                        "module": "experiment"}
            return {"ok": True, "reply": "登记实验需要名称，例如：\n> 登记实验 GNN-IDS 在 UAV-DS1 上的 F1 评测\n\n"
                                         "更完整的字段（假设/数据集/超参/指标）请到「实验复现」标签页填写。",
                    "module": "experiment"}
        exps = store.list_experiments()
        if not exps:
            return {"ok": True, "reply": render_exp_table(exps) +
                    "\n\n> 对我说“登记实验 <名称>”即可快速建档，或去「实验复现」标签页填完整信息。",
                    "module": "experiment"}
        md = render_exp_table(exps)
        reps = store.list_reproductions()
        if reps:
            md += "\n\n" + render_rep_table(reps)
        return {"ok": True, "reply": md + "\n\n> 对某实验发起复现、上传 CSV 校验，请到「实验复现」标签页。",
                "module": "experiment"}

    # 8) CSV 数据校验
    if any(k in text for k in ['校验', '验证', 'validate', '数据对不对', 'csv']) and '出图' not in text:
        return {"ok": True, "reply": "把实验结果 CSV 发给我即可校验（检查表头、列数、指标列数值），例如：\n\n"
                                     "> 校验：\n> epochs,F1\n> 10,0.712\n> 20,0.803\n\n"
                                     "或直接在「实验复现」标签页粘贴 CSV 点校验。", "module": "validate"}

    # 5) 对比矩阵（有检索意图则先检索再矩阵，否则用上一次结果）
    if any(k in text for k in ['矩阵', '对比矩阵', 'matrix']):
        q = _strip_query(text)
        if len(q) >= 6 and any(k in text for k in ['检索', '搜索', '查', '找']):
            sr = api_search({"query": q, "sources": "arxiv,openalex,crossref,s2",
                             "limit": 20, "from": _year_from(text), "to": None})
            if not sr.get("ok") or not sr.get("records"):
                note = sr.get("note") or sr.get("error") or ""
                return {"ok": True,
                        "reply": "先检索「%s」但没拿到结果（可能断网或检索式太窄）。\n\n%s"
                                 % (q, note), "module": "matrix"}
            mr = api_matrix({"sort": "year", "max": 0, "json": ""})
            if mr.get("ok"):
                return {"ok": True,
                        "reply": "先检索到 %d 篇，再生成对比矩阵：\n\n%s" % (sr["count"], mr["markdown"]),
                        "module": "matrix"}
            return {"ok": True, "reply": "矩阵生成失败：" + mr.get("error", ""), "module": "matrix"}
        mr = api_matrix({"sort": "year", "max": 0, "json": ""})
        if mr.get("ok"):
            return {"ok": True, "reply": mr["markdown"], "module": "matrix"}
        return {"ok": True, "reply": "还没有检索结果。先说一句「检索 <你的主题>」，"
                                      "或切到「对比矩阵」标签页粘贴 JSON。", "module": "matrix"}

    # 6) 检索（显式检索意图时）
    q = _strip_query(text)
    hint = any(k in text for k in
               ['检索', '搜索', '查', '找', '搜', '看看', '近', '年', '论文', '文献',
                '相关', '关于', '有关', '进展', '综述'])
    en_words = len(re.findall(r'[A-Za-z]{3,}', text))
    search_intent = any(k in text for k in ['检索', '搜索', '查一下', '搜一下', '帮我查', '帮我找', '找找'])
    search_intent = search_intent or (len(q) >= 3 and en_words >= 2 and hint)
    if search_intent:
        sr = api_search({"query": q, "sources": "arxiv,openalex,crossref,s2",
                         "limit": _limit(text), "from": _year_from(text), "to": None})
        if sr.get("ok"):
            if sr.get("records"):
                reply = ("检索式：**%s**\n\n%s\n\n"
                         "> 说「生成对比矩阵」可把以上结果转成对比矩阵；选刊前先准备好摘要。"
                         % (q, sr["markdown"]))
            else:
                reply = ("检索式：**%s**\n\n没有结果——可能断网或检索式太窄。\n\n%s"
                         % (q, sr.get("note") or ""))
            return {"ok": True, "reply": reply, "module": "search"}
        return {"ok": True, "reply": "检索失败：" + sr.get("error", ""), "module": "search"}

    # 7) 其余非命令消息：先让智能体尝试接管（多步/工具型目标），再退回自由问答
    _ar = _agent_reply(_who(params), text)
    if _ar:
        return _ar
    if model.status()["ok"]:
        ar = api_ask({"text": text, "_user": params.get("_user"),
                      "model_preset": params.get("model_preset")})
        if ar.get("ok"):
            return {"ok": True, "reply": "（AI 回答）\n\n" + ar["reply"], "module": "ask", "ai": True}
    return {"ok": True, "reply": "这句话我没听懂是要做哪件事。\n\n" + HELP_TEXT +
            "\n\n> 提示：在「模型与API」配置一个模型（默认腾讯混元，填 API Key 即可）后，"
            "我可以直接回答你的科研问题、诊断画像、深度推荐方向，而不只做命令检索。",
            "module": "help"}


API_MAP = {
    "search": api_search,
    "matrix": api_matrix,
    "plot": api_plot,
    "journal": api_journal,
    "recommend": api_recommend,
    "profile": api_profile,
    "profile_extra": api_profile_extra,
    "chat": api_chat,
    "member": api_member,
    "task": api_task,
    "paper": api_paper,
    "experiment": api_experiment,
    "reproduce": api_reproduce,
    "validate": api_validate,
    "model_status": api_model_status,
    "model_set": api_model_set,
    "ask": api_ask,
    "ai_profile": api_ai_profile,
    "ai_recommend": api_ai_recommend,
    "auth": api_auth,
    "write": api_write,
    "review": api_review,
    "stats": api_stats,
    "memory": api_memory,
    "mypaper": api_mypaper,
    "collab": api_collab,
    "guide": api_guide,
    "digest": api_digest,
    "algo": api_algo,
    "autopaper": api_autopaper,
    "skills": api_skills,
    "reffolder": api_reffolder,
    "reviewflow": api_reviewflow,
    "workflow": api_workflow,
    "bpm": api_bpm,
    "pipeline": api_pipeline,
    "agent": api_agent,
    "dataset": api_dataset,
    "format": api_format,
    "model_list": api_model_list,
}


# ---------------------------------------------------------------- HTTP 处理器
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默默认日志
        pass

    def _serve_file(self, path, content_type):
        if not os.path.isfile(path):
            self.send_error(404, "Not found: %s" % os.path.basename(path))
            return
        with open(path, "rb") as f:
            body = f.read()
        if path == INDEX_FILE:
            body = body.replace(b"__APP_VERSION__", APP_VERSION.encode())
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")  # 确保更新后浏览器立即拿到新代码
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_file(INDEX_FILE, "text/html; charset=utf-8")
        elif path in ("/app.js",):
            self._serve_file(APPJS_FILE, "application/javascript; charset=utf-8")
        elif path.startswith("/download/mp/"):
            # 我的论文 Word 附件下载（带 token 鉴权 + 所有权校验）
            mid = path.rsplit("/", 1)[-1]
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            token = ""
            for kv in q.split("&"):
                if kv.startswith("token="):
                    token = kv[6:]
            user = auth.user_by_token(token) or {}
            papers = wb.mp_list(user.get("name", ""))
            p = next((x for x in papers if x.get("id") == mid), None)
            fp = _mypaper_file(mid, user.get("name", ""))
            if not p or not os.path.isfile(fp):
                self.send_error(404, "文件不存在或无权下载")
                return
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"paper_%s.docx\"" % mid)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "Unknown path")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self.send_error(404, "Unknown api")
            return
        name = path[len("/api/"):]
        fn = API_MAP.get(name)
        if not fn:
            _json_response(self, {"ok": False, "error": "未知接口: %s" % name}, 404)
            return
        params = _read_json_body(self)
        # 解析身份：Authorization: Bearer <token>
        token = (self.headers.get("Authorization") or "").replace("Bearer", "").strip()
        params["_user"] = auth.user_by_token(token)
        params["_token"] = token
        # 权限门禁：除认证接口(auth)外，全部要求登录；模型配置仅限管理员
        if name != "auth":
            if not params["_user"]:
                _json_response(self, {"ok": False, "error": "请先登录后再使用（科研智能体要求登录）",
                                      "need_login": True}, 401)
                return
            if name in ADMIN_ONLY_APIS and params["_user"].get("role") != "admin":
                _json_response(self, {"ok": False, "error": "该操作仅限管理员（模型配置属于全局设置）",
                                      "need_admin": True}, 403)
                return
        try:
            result = fn(params)
        except Exception as e:  # 兜底，绝不让服务挂掉
            result = {"ok": False, "error": "服务器内部错误：%s" % e}
        _json_response(self, result)


def main():
    ap = argparse.ArgumentParser(description="科研智能体 Web 服务")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # 首次启动确保超级管理员存在（admin / 初始口令见 auth.DEFAULT_ADMIN）
    try:
        auth.ensure_admin()
    except Exception as e:
        print("警告：创建初始管理员失败：%s" % e)

    # 首次启动写入内置流程模板（论文送审/开题/资源申请/通用）
    try:
        bpm.ensure_templates()
    except Exception as e:
        print("警告：写入内置流程模板失败：%s" % e)

    # 防重复启动：Windows 下 SO_REUSEADDR 允许重复绑定，先探测端口是否已有服务
    import socket as _socket
    probe = _socket.socket()
    probe.settimeout(1.0)
    if probe.connect_ex((args.host, args.port)) == 0:
        probe.close()
        print("错误：端口 %d 已有服务在运行（可能是旧的破晓实例）。请先关闭旧进程再启动。" % args.port)
        return
    probe.close()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("破晓（科研智能体）Web 已启动：%s" % url)
    print("按 Ctrl+C 停止。")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
