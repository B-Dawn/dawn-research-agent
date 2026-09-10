#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""破晓流程引擎（零依赖，参照 JeecgBoot / Flowable 的 BPM 能力模型）。

设计目标：把「论文/开题/投稿/资源申请」等审批流转做成可配置、可跟踪的流程，
且**完全不依赖 AI**——引擎本身是确定性的；AI 只用于「一句话生成流程」这类可选增强。

能力清单（对齐 JeecgBoot 工作流）：
  设计：流程定义（开始/审批/结束 节点 + 连线 + 条件分支）、表单挂靠、审批人（用户/角色/发起人）、
        或签 / 会签(and, 支持一票否决)、驳回目标、抄送人
  发起：选定义 → 填表单 → 生成流程实例 + 首个待办任务
  办理：签收认领 / 通过 / 驳回 / 转办 / 委派 / 向前加签 / 向后加签 / 抄送 / 催办 / 终止
  跟踪：我的待办 / 我发起的 / 我的抄送 / 已办 / 流程监控（管理员）/ 流程图进度 + 审批历史

数据文件：data/bpm.json
    {"defs": [...], "instances": [...], "seq": {"def": 0, "inst": 0, "task": 0}}
"""

import json
import os
import re
import threading
import time

import auth

_LOCK = threading.Lock()
_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
_FILE = os.path.join(_DATA_DIR, "bpm.json")
_EMPTY = {"defs": [], "instances": [], "seq": {"def": 0, "inst": 0, "task": 0}}

NODE_TYPES = ("start", "approve", "end")
ASSIGNEE_TYPES = ("user", "role", "initiator")
SIGN_MODES = ("or", "and")          # or=或签（任一人通过即可） and=会签（全部通过）
ACTIONS = ("approve", "reject", "transfer", "delegate", "addsign_before",
           "addsign_after", "cc", "urge", "terminate", "claim")


# ================================================================ 存储
def _load():
    if not os.path.isfile(_FILE):
        return json.loads(json.dumps(_EMPTY))
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            o = json.load(f)
    except Exception:
        return json.loads(json.dumps(_EMPTY))
    for k, v in _EMPTY.items():
        o.setdefault(k, json.loads(json.dumps(v)))
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


def _mutate(fn):
    with _LOCK:
        obj = _load()
        new = fn(obj)
        if new is None:
            return obj
        _save(new)
        return new


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _next_id(obj, kind):
    seq = obj.setdefault("seq", {"def": 0, "inst": 0, "task": 0})
    seq[kind] = int(seq.get(kind, 0)) + 1
    return seq[kind]


# ================================================================ 内置流程模板
def _tpl(name, category, desc, form, nodes, edges, cc=None):
    return {"name": name, "category": category, "desc": desc, "form": form,
            "nodes": nodes, "edges": edges, "cc_on_end": cc or [], "builtin": True}


def builtin_templates():
    """内置模板：覆盖线性审批 / 会签 / 条件分支 / 抄送 四类常见场景。"""
    return [
        _tpl("论文送审审批", "论文", "学生提交论文 → 导师审批 → 学院审批 → 通过后抄送本人",
             [{"key": "title", "label": "论文标题", "type": "text", "required": True},
              {"key": "journal", "label": "拟投期刊/会议", "type": "text", "required": False},
              {"key": "summary", "label": "摘要或说明", "type": "textarea", "required": False}],
             [{"id": "n1", "type": "start", "name": "提交申请"},
              {"id": "n2", "type": "approve", "name": "导师审批", "assignee_type": "user",
               "assignee": "", "sign_mode": "or", "allow_reject": True, "reject_to": "initiator"},
              {"id": "n3", "type": "approve", "name": "学院审批", "assignee_type": "role",
               "assignee": "teacher", "sign_mode": "or", "allow_reject": True, "reject_to": "n2"},
              {"id": "n4", "type": "end", "name": "结束"}],
             [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}, {"from": "n3", "to": "n4"}]),
        _tpl("开题报告审批", "开题", "提交开题报告 → 导师审批 → 专家组会签（全部通过）→ 结束",
             [{"key": "title", "label": "开题题目", "type": "text", "required": True},
              {"key": "keywords", "label": "关键词", "type": "text", "required": False},
              {"key": "plan", "label": "研究计划", "type": "textarea", "required": False}],
             [{"id": "n1", "type": "start", "name": "提交开题"},
              {"id": "n2", "type": "approve", "name": "导师审批", "assignee_type": "user",
               "assignee": "", "sign_mode": "or", "allow_reject": True, "reject_to": "initiator"},
              {"id": "n3", "type": "approve", "name": "专家组会签", "assignee_type": "role",
               "assignee": "teacher", "sign_mode": "and", "veto": True,
               "allow_reject": True, "reject_to": "n2"},
              {"id": "n4", "type": "end", "name": "结束"}],
             [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}, {"from": "n3", "to": "n4"}]),
        _tpl("实验资源申请", "实验", "资源量 > 10 时需学院审批（条件分支），否则直接通过",
             [{"key": "title", "label": "申请事由", "type": "text", "required": True},
              {"key": "amount", "label": "资源量（卡时/核时）", "type": "number", "required": True},
              {"key": "detail", "label": "详细说明", "type": "textarea", "required": False}],
             [{"id": "n1", "type": "start", "name": "提交申请"},
              {"id": "n2", "type": "approve", "name": "导师审批", "assignee_type": "user",
               "assignee": "", "sign_mode": "or", "allow_reject": True, "reject_to": "initiator"},
              {"id": "n3", "type": "approve", "name": "学院审批", "assignee_type": "role",
               "assignee": "admin", "sign_mode": "or", "allow_reject": True, "reject_to": "n2"},
              {"id": "n4", "type": "end", "name": "结束"}],
             [{"from": "n1", "to": "n2"},
              {"from": "n2", "to": "n3", "cond": "amount > 10"},
              {"from": "n2", "to": "n4", "cond": ""},
              {"from": "n3", "to": "n4"}]),
        _tpl("通用审批", "通用", "最简流程：提交 → 单人审批 → 结束（可自行加节点）",
             [{"key": "title", "label": "标题", "type": "text", "required": True},
              {"key": "detail", "label": "内容", "type": "textarea", "required": False}],
             [{"id": "n1", "type": "start", "name": "提交"},
              {"id": "n2", "type": "approve", "name": "审批", "assignee_type": "role",
               "assignee": "admin", "sign_mode": "or", "allow_reject": True, "reject_to": "initiator"},
              {"id": "n3", "type": "end", "name": "结束"}],
             [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}]),
    ]


def ensure_templates():
    """首次使用时写入内置模板（不覆盖用户已有定义）。"""
    def fn(obj):
        if obj.get("defs"):
            return None
        for t in builtin_templates():
            t = dict(t)
            t["id"] = "wfd%03d" % _next_id(obj, "def")
            t["owner"] = "system"
            t["ts"] = _now()
            obj.setdefault("defs", []).append(t)
        return obj
    try:
        _mutate(fn)
    except Exception:
        pass


# ================================================================ 用户/角色解析
def _active_users():
    try:
        return [u for u in auth.list_users() if u.get("active", True)]
    except Exception:
        return []


def _users_of_role(role):
    return [u.get("name") for u in _active_users() if u.get("role") == role and u.get("name")]


def _resolve_assignees(node, inst):
    """把节点的审批人配置解析成具体用户名列表。"""
    t = node.get("assignee_type") or "role"
    raw = node.get("assignee") or ""
    if t == "initiator":
        return [inst.get("initiator")] if inst.get("initiator") else []
    if t == "role":
        return _users_of_role(raw)
    # user：支持多个人（逗号或顿号分隔）
    names = [x.strip() for x in re.split(r"[,，、;；\s]+", str(raw)) if x.strip()]
    valid = {u.get("name") for u in _active_users()}
    return [n for n in names if n in valid] or names


# ================================================================ 条件表达式
_COND_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*(==|!=|>=|<=|>|<|contains)\s*(.*?)\s*$")


def eval_cond(cond, data):
    """条件语法：`field op value`，op ∈ ==,!=,>,>=,<,<=,contains。空条件视为默认分支。"""
    c = (cond or "").strip()
    if not c:
        return True
    m = _COND_RE.match(c)
    if not m:
        return False
    field, op, rhs = m.group(1), m.group(2), m.group(3).strip().strip('"\'')
    val = (data or {}).get(field, "")
    if op == "contains":
        return str(rhs) in str(val)
    try:
        a, b = float(val), float(rhs)
    except (TypeError, ValueError):
        a, b = str(val).strip().lower(), str(rhs).lower()
    try:
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
    except TypeError:
        return False
    return False


def _next_node(defn, node_id, data):
    """按连线与条件选下一个节点；条件非空的优先，空条件作默认。"""
    edges = [e for e in (defn.get("edges") or []) if e.get("from") == node_id]
    default = None
    for e in edges:
        if (e.get("cond") or "").strip():
            if eval_cond(e.get("cond"), data):
                return e.get("to")
        else:
            default = default or e.get("to")
    return default


def _node(defn, nid):
    for n in defn.get("nodes") or []:
        if n.get("id") == nid:
            return n
    return None


def _ordered_nodes(defn):
    """从 start 沿连线走出主链（用于进度展示）。"""
    nodes = defn.get("nodes") or []
    start = next((n for n in nodes if n.get("type") == "start"), None)
    if not start:
        return list(nodes)
    order, seen, cur = [], set(), start.get("id")
    while cur and cur not in seen:
        seen.add(cur)
        nd = _node(defn, cur)
        if not nd:
            break
        order.append(nd)
        edges = [e for e in (defn.get("edges") or []) if e.get("from") == cur]
        if not edges:
            break
        nxt = next((e.get("to") for e in edges if not (e.get("cond") or "").strip()),
                   edges[0].get("to"))
        cur = nxt
    # 补上未被主链覆盖的节点
    for n in nodes:
        if n.get("id") not in seen:
            order.append(n)
    return order


# ================================================================ 定义管理
def def_list(by, include_builtin=True):
    out = []
    for d in _load().get("defs", []):
        if d.get("builtin") and not include_builtin:
            continue
        out.append({**d, "inst_count": sum(1 for i in _load().get("instances", [])
                                          if i.get("def_id") == d.get("id"))})
    out.sort(key=lambda d: (d.get("category") or "", d.get("ts") or ""))
    return out


def def_get(did):
    for d in _load().get("defs", []):
        if d.get("id") == did:
            return d
    return None


def def_save(by, data):
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "流程名称不能为空"}
    nodes = data.get("nodes") or []
    if not any(n.get("type") == "start" for n in nodes):
        return {"ok": False, "error": "流程缺少「开始」节点"}
    if not any(n.get("type") == "end" for n in nodes):
        return {"ok": False, "error": "流程缺少「结束」节点"}
    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids):
        return {"ok": False, "error": "节点 ID 重复"}
    for e in (data.get("edges") or []):
        if e.get("from") not in ids or e.get("to") not in ids:
            return {"ok": False, "error": "连线指向了不存在的节点"}
    did = data.get("id")

    def fn(obj):
        payload = {
            "name": name,
            "category": (data.get("category") or "通用").strip(),
            "desc": (data.get("desc") or "").strip()[:400],
            "form": data.get("form") or [],
            "nodes": nodes,
            "edges": data.get("edges") or [],
            "cc_on_end": data.get("cc_on_end") or [],
            "owner": by,
            "ts": _now(),
        }
        if did:
            for d in obj.get("defs", []):
                if d.get("id") == did:
                    if d.get("builtin") and d.get("owner") == "system":
                        payload["builtin"] = True
                    d.update(payload)
                    return obj
        payload["id"] = "wfd%03d" % _next_id(obj, "def")
        obj.setdefault("defs", []).append(payload)
        return obj

    new = _mutate(fn)
    saved = next((d for d in new.get("defs", []) if d.get("name") == name), None)
    return {"ok": True, "id": (saved or {}).get("id", did), "defs": def_list(by)}


def def_remove(did):
    def fn(obj):
        obj["defs"] = [d for d in obj.get("defs", []) if d.get("id") != did]
        return obj
    _mutate(fn)
    return {"ok": True}


# ================================================================ 实例推进
def _mk_tasks(obj, inst, node):
    """按节点配置生成任务（会签则每人一条）。"""
    sign = node.get("sign_mode") or "or"
    names = _resolve_assignees(node, inst)
    if not names:
        names = [inst.get("initiator")] if inst.get("initiator") else []
    tid_list = []
    if sign == "and" and len(names) > 1:
        for nm in names:
            tid = "tk%04d" % _next_id(obj, "task")
            inst.setdefault("tasks", []).append({
                "id": tid, "node": node.get("id"), "name": node.get("name"),
                "assignees": [nm], "claimed_by": "", "status": "todo",
                "action": "", "comment": "", "ts": _now(), "done_ts": "",
                "sign_mode": "and", "delegated_to": "", "delegated_from": "",
                "pending_signs": []})
            tid_list.append(tid)
    else:
        tid = "tk%04d" % _next_id(obj, "task")
        inst.setdefault("tasks", []).append({
            "id": tid, "node": node.get("id"), "name": node.get("name"),
            "assignees": names, "claimed_by": "", "status": "todo",
            "action": "", "comment": "", "ts": _now(), "done_ts": "",
            "sign_mode": "or", "delegated_to": "", "delegated_from": "",
            "pending_signs": []})
        tid_list.append(tid)
    return tid_list


def _enter_node(obj, inst, node_id, actor="system", action="enter", comment=""):
    defn = next((d for d in obj.get("defs", []) if d.get("id") == inst.get("def_id")), None)
    if not defn:
        inst["status"] = "terminated"
        return
    nd = _node(defn, node_id)
    if not nd:
        inst["status"] = "terminated"
        return
    inst["cur"] = [node_id]
    if nd.get("type") == "end":
        inst["status"] = "approved"
        inst["end_ts"] = _now()
        inst["cur"] = []
        for u in (defn.get("cc_on_end") or []):
            _add_cc(inst, [u], actor or "system")
        return
    _mk_tasks(obj, inst, nd)


def _add_cc(inst, users, by):
    users = [u for u in (users or []) if u]
    if not users:
        return
    inst.setdefault("cc", []).append({"users": users, "by": by, "ts": _now(),
                                      "at": (inst.get("cur") or [""])[0]})


def wf_start(def_id, by, title="", form=None, business_key=""):
    defn = def_get(def_id)
    if not defn:
        return {"ok": False, "error": "流程定义不存在"}
    form = form or {}
    # 必填校验
    for f in defn.get("form") or []:
        if f.get("required") and not str(form.get(f.get("key"), "")).strip():
            return {"ok": False, "error": "「%s」为必填项" % f.get("label")}
    start = next((n for n in defn.get("nodes") or [] if n.get("type") == "start"), None)
    if not start:
        return {"ok": False, "error": "流程定义缺少开始节点"}

    def fn(obj):
        pid = "pi%04d" % _next_id(obj, "inst")
        inst = {
            "id": pid, "def_id": def_id, "def_name": defn.get("name"),
            "category": defn.get("category"), "title": (title or "").strip()
            or (form.get("title") or defn.get("name")),
            "business_key": (business_key or "").strip(),
            "form": form, "initiator": by, "status": "running", "cur": [],
            "tasks": [], "cc": [], "history": [], "ts": _now(), "end_ts": "",
        }
        inst["history"].append({"node": start.get("id"), "name": start.get("name"),
                                "actor": by, "action": "submit",
                                "comment": "", "ts": _now()})
        nxt = _next_node(defn, start.get("id"), form)
        if not nxt:
            _enter_node(obj, inst, start.get("id"), actor=by, action="submit")
        else:
            _enter_node(obj, inst, nxt, actor=by, action="submit")
        obj.setdefault("instances", []).append(inst)
        return obj

    new = _mutate(fn)
    inst = new.get("instances", [])[-1]
    return {"ok": True, "instance": inst, "todo": todo(by)}


# ================================================================ 查询
def _visible(inst, by):
    return inst


def inst_get(iid):
    for i in _load().get("instances", []):
        if i.get("id") == iid:
            return i
    return None


def _task_open(t):
    return t.get("status") == "todo"


def todo(by):
    """我的待办：我被指派/被委派/认领的任务。"""
    out = []
    for inst in _load().get("instances", []):
        if inst.get("status") != "running":
            continue
        for t in inst.get("tasks", []):
            if not _task_open(t):
                continue
            mine = (by in (t.get("assignees") or [])) or t.get("claimed_by") == by \
                or t.get("delegated_to") == by
            if not mine:
                continue
            if t.get("claimed_by") and t.get("claimed_by") != by and t.get("delegated_to") != by:
                continue
            blocked = bool(t.get("pending_signs"))
            out.append({**t, "inst_id": inst.get("id"), "inst_title": inst.get("title"),
                        "def_id": inst.get("def_id"), "def_name": inst.get("def_name"),
                        "initiator": inst.get("initiator"),
                        "form": inst.get("form"), "inst_ts": inst.get("ts"),
                        "blocked": blocked, "candidates": t.get("assignees") or []})
    out.sort(key=lambda t: t.get("ts") or "")
    return out


def mine(by):
    return [i for i in _load().get("instances", []) if i.get("initiator") == by]


def done_list(by):
    out = []
    for inst in _load().get("instances", []):
        for t in inst.get("tasks", []):
            if t.get("status") == "done" and (t.get("claimed_by") == by
                                              or by in (t.get("assignees") or [])):
                out.append({**t, "inst_id": inst.get("id"), "inst_title": inst.get("title"),
                            "def_name": inst.get("def_name")})
    out.sort(key=lambda t: t.get("done_ts") or "", reverse=True)
    return out


def cc_list(by):
    out = []
    for inst in _load().get("instances", []):
        for c in inst.get("cc", []):
            if by in (c.get("users") or []):
                out.append({"inst_id": inst.get("id"), "title": inst.get("title"),
                            "def_name": inst.get("def_name"), "by": c.get("by"),
                            "ts": c.get("ts"), "status": inst.get("status")})
    out.sort(key=lambda c: c.get("ts") or "", reverse=True)
    return out


def inst_list(by, all_=False):
    items = _load().get("instances", [])
    if not all_:
        items = [i for i in items if i.get("initiator") == by
                 or any(by in (t.get("assignees") or []) or t.get("claimed_by") == by
                        for t in i.get("tasks", []))
                 or any(by in (c.get("users") or []) for c in i.get("cc", []))]
    return sorted(items, key=lambda i: i.get("ts") or "", reverse=True)


def progress(inst):
    """给前端画流程进度用：每个节点 done / current / pending。"""
    defn = def_get(inst.get("def_id"))
    if not defn:
        return []
    done_nodes = {h.get("node") for h in inst.get("history", [])
                  if h.get("action") in ("approve", "submit", "auto")}
    cur = set(inst.get("cur") or [])
    out = []
    for nd in _ordered_nodes(defn):
        if nd.get("id") in cur:
            st = "current"
        elif nd.get("id") in done_nodes:
            st = "done"
        elif nd.get("type") == "end" and inst.get("status") == "approved":
            st = "done"
        else:
            st = "pending"
        out.append({"id": nd.get("id"), "name": nd.get("name"),
                    "type": nd.get("type"), "state": st})
    return out


# ================================================================ 办理动作
def _find_task(inst, tid):
    for t in inst.get("tasks", []):
        if t.get("id") == tid:
            return t
    return None


def _can_act(inst, task, by, is_admin):
    if is_admin:
        return True
    if task.get("delegated_to") == by:
        return True
    if task.get("claimed_by"):
        # 已被认领：只有认领人（或委派对象）能处理
        return task.get("claimed_by") == by
    return by in (task.get("assignees") or [])


def _open_siblings(inst, task):
    """同节点、仍待办、且非加签产生的任务（会签用）。"""
    return [t for t in inst.get("tasks", [])
            if t.get("node") == task.get("node") and t.get("id") != task.get("id")
            and t.get("status") == "todo" and not t.get("addsign_of")]


def _cancel_branch(inst, node_id, reason):
    """作废该节点上所有残留待办（含加签）。"""
    for t in inst.get("tasks", []):
        if t.get("node") == node_id and t.get("status") == "todo":
            t["status"] = "done"
            t["action"] = "canceled"
            t["comment"] = reason
            t["done_ts"] = _now()


def _advance_from(obj, inst, defn, node_id, by, comment=""):
    """节点处理完毕：或签作废兄弟 / 会签需全部完成 → 走向下一节点或结束。"""
    nxt = _next_node(defn, node_id, inst.get("form")) if defn else None
    if not nxt:
        inst["status"] = "approved"
        inst["cur"] = []
        inst["end_ts"] = _now()
        if defn:
            for u in (defn.get("cc_on_end") or []):
                _add_cc(inst, [u], by)
        return
    _enter_node(obj, inst, nxt, actor=by, action="approve", comment=comment)


def act(iid, tid, by, action, comment="", payload=None, is_admin=False):
    payload = payload or {}
    if action not in ACTIONS:
        return {"ok": False, "error": "未知操作 %s" % action}
    err = []

    def fn(obj):
        inst = next((i for i in obj.get("instances", []) if i.get("id") == iid), None)
        if not inst:
            err.append("流程实例不存在")
            return None
        if inst.get("status") != "running":
            err.append("流程已结束（%s），不能再办理" % STATUS_ZH.get(inst.get("status"), inst.get("status")))
            return None
        task = _find_task(inst, tid)
        if not task:
            err.append("任务不存在")
            return None
        if not _can_act(inst, task, by, is_admin):
            err.append("你没有该任务的处理权限（可能已被他人认领）")
            return None
        defn = next((d for d in obj.get("defs", []) if d.get("id") == inst.get("def_id")), None)
        nd = _node(defn, task.get("node")) if defn else None

        def _hist(a, cm=""):
            inst["history"].append({"node": task.get("node"), "name": task.get("name"),
                                    "actor": by, "action": a, "comment": cm, "ts": _now()})

        # ---------------- 签收 / 催办 / 抄送 ----------------
        if action == "claim":
            if task.get("claimed_by"):
                err.append("该任务已被 %s 认领" % task.get("claimed_by"))
                return None
            task["claimed_by"] = by
            _hist("claim")
            return obj
        if action == "urge":
            _hist("urge", comment or "催办")
            return obj
        if action == "cc":
            users = [u for u in (payload.get("users") or []) if u]
            if not users:
                err.append("请选择抄送人员")
                return None
            _add_cc(inst, users, by)
            _hist("cc", "抄送 " + "、".join(users))
            return obj
        if action == "transfer":          # 转办：交给别人，自己退出
            to = (payload.get("to") or "").strip()
            if not to:
                err.append("请选择转办对象")
                return None
            task["assignees"] = [to]
            task["claimed_by"] = ""
            task["delegated_to"] = ""
            task["delegated_from"] = ""
            _hist("transfer", comment or ("转办给 %s" % to))
            return obj
        if action == "delegate":          # 委派：别人办，办完回到自己
            to = (payload.get("to") or "").strip()
            if not to:
                err.append("请选择委派对象")
                return None
            task["delegated_to"] = to
            task["delegated_from"] = task.get("claimed_by") or by
            task["claimed_by"] = to
            _hist("delegate", comment or ("委派给 %s" % to))
            return obj
        if action in ("addsign_before", "addsign_after"):   # 加签
            users = [u for u in (payload.get("users") or []) if u]
            if not users:
                err.append("请选择加签人员")
                return None
            before = action.endswith("before")
            new_ids = []
            for u in users:
                ntid = "tk%04d" % _next_id(obj, "task")
                inst.setdefault("tasks", []).append({
                    "id": ntid, "node": task.get("node"),
                    "name": task.get("name") + ("·前加签" if before else "·后加签"),
                    "assignees": [u], "claimed_by": "", "status": "todo", "action": "",
                    "comment": "", "ts": _now(), "done_ts": "", "sign_mode": "or",
                    "delegated_to": "", "delegated_from": "", "pending_signs": [],
                    "post_signs": [], "addsign_of": task.get("id"),
                    "addsign_kind": "before" if before else "after"})
                new_ids.append(ntid)
            if before:
                task.setdefault("pending_signs", []).extend(new_ids)
            else:
                task.setdefault("post_signs", []).extend(new_ids)
            _hist(action, "、".join(users))
            return obj

        # ---------------- 驳回 ----------------
        if action == "reject":
            if nd and nd.get("allow_reject") is False:
                err.append("该节点不允许驳回")
                return None
            task["status"] = "done"
            task["action"] = "reject"
            task["comment"] = comment
            task["claimed_by"] = task.get("claimed_by") or by
            task["done_ts"] = _now()
            _hist("reject", comment)
            _cancel_branch(inst, task.get("node"), "同节点已被驳回")
            target = (nd or {}).get("reject_to") or "initiator"
            if target in ("initiator", "start", ""):
                inst["status"] = "rejected"
                inst["cur"] = []
                inst["end_ts"] = _now()
            else:
                inst["status"] = "running"
                _enter_node(obj, inst, target, actor=by, action="reject", comment=comment)
            return obj

        # ---------------- 加签任务自身被审批 ----------------
        if task.get("addsign_of"):
            parent = _find_task(inst, task.get("addsign_of"))
            task["status"] = "done"
            task["action"] = "approve"
            task["comment"] = comment
            task["claimed_by"] = task.get("claimed_by") or by
            task["done_ts"] = _now()
            _hist("approve", comment)
            if not parent:
                return obj
            if task.get("addsign_kind") == "before":
                ps = parent.get("pending_signs") or []
                parent["pending_signs"] = [x for x in ps if x != task.get("id")]
                return obj
            # 后加签：等本节点所有后加签都完成，才推进
            posts = parent.get("post_signs") or []
            undone = [t for t in inst.get("tasks", [])
                      if t.get("id") in posts and t.get("status") == "todo"]
            if undone:
                return obj
            _cancel_branch(inst, task.get("node"), "后加签已完成，节点推进")
            _advance_from(obj, inst, defn, task.get("node"), by, comment)
            return obj

        # ---------------- 通过 ----------------
        if task.get("pending_signs"):
            err.append("还有前加签任务未完成，暂不能审批")
            return None
        if task.get("delegated_from"):      # 委派对象办完 → 回到委派人确认
            owner = task.get("delegated_from")
            task["delegated_to"] = ""
            task["delegated_from"] = ""
            task["claimed_by"] = owner
            _hist("delegate_done", comment or "委派事项已处理，退回委托人确认")
            return obj
        task["status"] = "done"
        task["action"] = "approve"
        task["comment"] = comment
        task["claimed_by"] = task.get("claimed_by") or by
        task["done_ts"] = _now()
        _hist("approve", comment)
        if task.get("post_signs"):          # 还有后加签要处理
            return obj
        if (nd or {}).get("sign_mode") == "and" and _open_siblings(inst, task):
            return obj                       # 会签：等其他人
        _cancel_branch(inst, task.get("node"), "或签：同节点已有人通过")
        # 兄弟任务判据在作废后失效，此处按节点推进
        _advance_from(obj, inst, defn, task.get("node"), by, comment)
        return obj

    new = _mutate(fn)
    if err:
        return {"ok": False, "error": err[0]}
    if new is None:
        return {"ok": False, "error": "操作失败"}
    inst = next((i for i in new.get("instances", []) if i.get("id") == iid), None)
    return {"ok": True, "instance": inst, "todo": todo(by)}


def revoke(iid, by, is_admin=False):
    err = []

    def fn(obj):
        inst = next((i for i in obj.get("instances", []) if i.get("id") == iid), None)
        if not inst:
            err.append("流程实例不存在")
            return None
        if inst.get("initiator") != by and not is_admin:
            err.append("只有发起人（或管理员）可以撤销")
            return None
        if inst.get("status") != "running":
            err.append("流程已结束，无法撤销")
            return None
        inst["status"] = "canceled"
        inst["cur"] = []
        inst["end_ts"] = _now()
        for t in inst.get("tasks", []):
            if t.get("status") == "todo":
                t["status"] = "done"
                t["action"] = "canceled"
                t["comment"] = "发起人撤销"
                t["done_ts"] = _now()
        inst["history"].append({"node": "", "name": "撤销", "actor": by,
                                "action": "revoke", "comment": "", "ts": _now()})
        return obj

    new = _mutate(fn)
    if err:
        return {"ok": False, "error": err[0]}
    inst = next((i for i in new.get("instances", []) if i.get("id") == iid), None)
    return {"ok": True, "instance": inst}


def resubmit(iid, by, form=None):
    """驳回后由发起人修改并重新提交，回到第一个审批节点。"""
    err = []

    def fn(obj):
        inst = next((i for i in obj.get("instances", []) if i.get("id") == iid), None)
        if not inst:
            err.append("流程实例不存在")
            return None
        if inst.get("initiator") != by:
            err.append("只有发起人可以重新提交")
            return None
        if inst.get("status") != "rejected":
            err.append("只有被驳回的流程可以重新提交")
            return None
        defn = next((d for d in obj.get("defs", []) if d.get("id") == inst.get("def_id")), None)
        if not defn:
            err.append("流程定义已删除")
            return None
        if form:
            inst["form"] = {**(inst.get("form") or {}), **form}
        inst["status"] = "running"
        inst["end_ts"] = ""
        inst["history"].append({"node": "", "name": "重新提交", "actor": by,
                                "action": "resubmit", "comment": "", "ts": _now()})
        start = next((n for n in defn.get("nodes") or [] if n.get("type") == "start"), None)
        nxt = _next_node(defn, (start or {}).get("id"), inst.get("form"))
        _enter_node(obj, inst, nxt, actor=by, action="resubmit")
        return obj

    new = _mutate(fn)
    if err:
        return {"ok": False, "error": err[0]}
    inst = next((i for i in new.get("instances", []) if i.get("id") == iid), None)
    return {"ok": True, "instance": inst, "todo": todo(by)}


def stats(by, is_admin=False):
    insts = _load().get("instances", [])
    mine_ = [i for i in insts if i.get("initiator") == by]
    return {
        "defs": len(_load().get("defs", [])),
        "todo": len(todo(by)),
        "mine_running": sum(1 for i in mine_ if i.get("status") == "running"),
        "mine_total": len(mine_),
        "cc": len(cc_list(by)),
        "all": len(insts) if is_admin else None,
    }


STATUS_ZH = {"running": "进行中", "approved": "已通过", "rejected": "已驳回",
             "canceled": "已撤销", "terminated": "已终止"}
ACTION_ZH = {"submit": "提交", "approve": "通过", "reject": "驳回", "transfer": "转办",
             "delegate": "委派", "delegate_done": "委派处理完成", "addsign_before": "前加签",
             "addsign_after": "后加签", "cc": "抄送", "urge": "催办", "claim": "签收",
             "revoke": "撤销", "resubmit": "重新提交", "canceled": "已作废", "auto": "自动"}
