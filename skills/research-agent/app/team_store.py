#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""团队协作数据层（零依赖）。

JSON 文件存储 + 原子写 + 线程锁。数据统一放在 skill 根目录的 data/ 下：

    data/members.json       成员档案（角色/分工/专长）
    data/tasks.json         任务板（待办/进行中/评审/完成，可指派成员）
    data/papers.json        论文协作（章节拆分/分工/草稿内容/审阅意见）
    data/experiments.json   实验台账（假设/数据集/超参/指标/结果/负责人）
    data/reproductions.json 复现记录（谁复现了哪个实验、结论是否一致）

所有写操作经 _mutate() 串行化，防止多请求并发写坏文件。
"""

import json
import os
import re
import threading
import time

_LOCK = threading.Lock()

# 当前文件位于 <skill>/app/，数据目录为 <skill>/data/
_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))

_EMPTY = {
    "members": {"members": []},
    "tasks": {"tasks": []},
    "papers": {"papers": []},
    "experiments": {"experiments": []},
    "reproductions": {"reproductions": []},
}


# ---------------------------------------------------------------- 基础 IO
def data_dir():
    os.makedirs(_DATA_DIR, exist_ok=True)
    return _DATA_DIR


def _file(name):
    return os.path.join(data_dir(), name + ".json")


def _load(name):
    p = _file(name)
    if not os.path.isfile(p):
        return json.loads(json.dumps(_EMPTY.get(name, {})))  # 深拷贝默认
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
    for _try in range(5):  # Windows 下瞬时文件锁（杀毒/句柄回收）重试
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
    """读-改-写，串行化。fn 接收当前对象，返回新对象或 None(放弃)。"""
    with _LOCK:
        obj = _load(name)
        new = fn(obj)
        if new is None:
            return obj
        _save(name, new)
        return new


def now():
    return time.strftime("%Y-%m-%d %H:%M")


def _uid(prefix, items):
    used = {it.get("id", "") for it in items}
    n = 1
    while "%s%03d" % (prefix, n) in used:
        n += 1
    return "%s%03d" % (prefix, n)


def _pick(src, keys):
    return {k: src[k] for k in keys if k in src and src[k] not in (None, "")}


# ---------------------------------------------------------------- 成员
MEMBER_FIELDS = ("name", "role", "title", "skills", "joined", "notes")


def list_members():
    return _load("members").get("members", [])


def add_member(data):
    def fn(obj):
        m = _pick(data, MEMBER_FIELDS)
        if not m.get("name"):
            return None
        for k, n in (("name", 40), ("display", 40), ("role", 20), ("expertise", 200), ("notes", 1000)):
            m[k] = _clip(m.get(k), n)
        m["id"] = _uid("m", obj.get("members", []))
        m.setdefault("role", "成员")
        m.setdefault("joined", now().split(" ")[0])
        obj.setdefault("members", []).append(m)
        return obj

    _mutate("members", fn)
    return {"ok": True, "members": list_members()}


def update_member(mid, patch):
    def fn(obj):
        for m in obj.get("members", []):
            if m["id"] == mid:
                m.update(_pick(patch, MEMBER_FIELDS))
                return obj
        return None

    _mutate("members", fn)
    return {"ok": True, "members": list_members()}


def remove_member(mid):
    def fn(obj):
        obj["members"] = [m for m in obj.get("members", []) if m["id"] != mid]
        return obj

    _mutate("members", fn)
    return {"ok": True, "members": list_members()}


# ---------------------------------------------------------------- 任务板
TASK_STATUS = ("todo", "doing", "review", "done")
TASK_FIELDS = ("title", "desc", "module", "assignee", "priority", "due", "tags", "status")


def list_tasks(**flt):
    tasks = _load("tasks").get("tasks", [])
    if flt.get("status"):
        tasks = [t for t in tasks if t.get("status") == flt["status"]]
    if flt.get("assignee"):
        tasks = [t for t in tasks if t.get("assignee") == flt["assignee"]]
    return tasks


def add_task(data):
    def fn(obj):
        t = _pick(data, TASK_FIELDS)
        if not t.get("title"):
            return None
        for k, n in (("title", 150), ("assignee", 40), ("priority", 10), ("module", 20), ("notes", 1000), ("status", 20)):
            t[k] = _clip(t.get(k), n)
        t["id"] = _uid("t", obj.get("tasks", []))
        t.setdefault("status", "todo")
        t.setdefault("priority", "中")
        t.setdefault("module", "general")
        t["created"] = now()
        t["updated"] = now()
        obj.setdefault("tasks", []).append(t)
        return obj

    _mutate("tasks", fn)
    return {"ok": True}


def update_task(tid, patch):
    def fn(obj):
        for t in obj.get("tasks", []):
            if t["id"] == tid:
                t.update(_pick(patch, TASK_FIELDS))
                if patch.get("status") and patch["status"] not in TASK_STATUS:
                    return None
                if "status" in patch:
                    t["status"] = patch["status"]
                t["updated"] = now()
                return obj
        return None

    _mutate("tasks", fn)
    return {"ok": True}


def remove_task(tid):
    def fn(obj):
        obj["tasks"] = [t for t in obj.get("tasks", []) if t["id"] != tid]
        return obj

    _mutate("tasks", fn)
    return {"ok": True}


# ---------------------------------------------------------------- 论文协作
def list_papers():
    return _load("papers").get("papers", [])


def get_paper(pid):
    for p in list_papers():
        if p["id"] == pid:
            return p
    return None


def _clip(v, n):
    """字段统一截断：超长输入静默裁剪到上限。"""
    return str(v or "").strip()[:n]


def create_paper(data):
    def fn(obj):
        title = (data.get("title") or "").strip()
        if not title:
            return None
        p = {
            "id": _uid("p", obj.get("papers", [])),
            "title": _clip(title, 200),
            "owner": _clip(data.get("owner"), 40),
            "created_by": _clip(data.get("created_by"), 40),
            "status": _clip(data.get("status"), 20) or "draft",
            "target_journal": _clip(data.get("target_journal"), 120),
            "created": now(),
            "updated": now(),
            "sections": [],   # {key,title,assignee,status,version,content}
            "comments": [],   # {id,section,by,at,text,resolved}
        }
        obj.setdefault("papers", []).append(p)
        return obj

    _mutate("papers", fn)
    return {"ok": True}


def remove_paper(pid):
    def fn(obj):
        obj["papers"] = [p for p in obj.get("papers", []) if p["id"] != pid]
        return obj
    _mutate("papers", fn)
    return {"ok": True}


def set_collaborator(pid, name, perm):
    """添加/更新论文协作者（perm: edit|view）。论文不存在抛 ValueError。"""
    if perm not in ("edit", "view"):
        raise ValueError("权限必须是 edit 或 view")

    def fn(obj):
        for p in obj.get("papers", []):
            if p["id"] == pid:
                cl = p.setdefault("collaborators", [])
                for c in cl:
                    if c.get("name") == name:
                        c["perm"] = perm
                        c["updated"] = now()
                        return obj
                cl.append({"name": name, "perm": perm, "added": now()})
                return obj
        raise ValueError("论文不存在: %s" % pid)

    _mutate("papers", fn)
    return {"ok": True}


def remove_collaborator(pid, name):
    def fn(obj):
        for p in obj.get("papers", []):
            if p["id"] == pid:
                p["collaborators"] = [c for c in p.get("collaborators", [])
                                      if c.get("name") != name]
                return obj
        raise ValueError("论文不存在: %s" % pid)

    _mutate("papers", fn)
    return {"ok": True}


def set_paper_meta(pid, patch):
    def fn(obj):
        for p in obj.get("papers", []):
            if p["id"] == pid:
                for k in ("status", "target_journal", "owner", "title"):
                    if patch.get(k):
                        p[k] = patch[k]
                p["updated"] = now()
                return obj
        return None

    _mutate("papers", fn)
    return {"ok": True}


def upsert_section(pid, data):
    """新增或更新论文的一个章节块。key 相同的则覆盖（版本号 +1）。"""
    key = (data.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "缺少章节 key"}

    def fn(obj):
        for p in obj.get("papers", []):
            if p["id"] != pid:
                continue
            title = data.get("title") or key
            for s in p.get("sections", []):
                if s["key"] == key:
                    if "content" in data and data["content"] is not None:
                        s["content"] = data["content"]
                    if data.get("assignee"):
                        s["assignee"] = data["assignee"]
                    if data.get("status") in ("draft", "done"):
                        s["status"] = data["status"]
                    s["version"] = int(s.get("version") or 1) + 1
                    s["updated"] = now()
                    p["updated"] = now()
                    return obj
            p.setdefault("sections", []).append({
                "key": key, "title": title,
                "assignee": data.get("assignee") or "",
                "status": data.get("status") or "draft",
                "version": 1, "content": data.get("content") or "",
                "created": now(), "updated": now(),
            })
            p["updated"] = now()
            return obj
        return None

    _mutate("papers", fn)
    return {"ok": True}


def add_comment(pid, data):
    def fn(obj):
        for p in obj.get("papers", []):
            if p["id"] != pid:
                continue
            c = {
                "id": _uid("c", p.get("comments", [])),
                "section": data.get("section") or "*",
                "by": data.get("by") or "匿名",
                "at": now(),
                "text": (data.get("text") or "").strip(),
                "resolved": False,
            }
            if not c["text"]:
                return None
            p.setdefault("comments", []).append(c)
            p["updated"] = now()
            return obj
        return None

    _mutate("papers", fn)
    return {"ok": True}


def resolve_comment(pid, cid, resolved=True):
    def fn(obj):
        for p in obj.get("papers", []):
            if p["id"] != pid:
                continue
            for c in p.get("comments", []):
                if c["id"] == cid:
                    c["resolved"] = bool(resolved)
                    return obj
        return None

    _mutate("papers", fn)
    return {"ok": True}


# ---------------------------------------------------------------- 实验台账
EXP_FIELDS = ("name", "hypothesis", "dataset", "model", "metrics", "seed",
              "runs", "owner", "status", "result", "files", "note")


def list_experiments(**flt):
    exps = _load("experiments").get("experiments", [])
    if flt.get("owner"):
        exps = [e for e in exps if e.get("owner") == flt["owner"]]
    if flt.get("status"):
        exps = [e for e in exps if e.get("status") == flt["status"]]
    return exps


def get_experiment(eid):
    for e in list_experiments():
        if e["id"] == eid:
            return e
    return None


def add_experiment(data):
    def fn(obj):
        e = _pick(data, EXP_FIELDS)
        if not e.get("name"):
            return None
        for k, n in (("name", 150), ("dataset", 120), ("owner", 40), ("status", 20),
                     ("seed", 60), ("hypothesis", 300), ("notes", 2000)):
            e[k] = _clip(e.get(k), n)
        e["id"] = _uid("e", obj.get("experiments", []))
        e.setdefault("owner", "")
        e.setdefault("status", "registered")
        e.setdefault("metrics", [])
        if isinstance(e.get("metrics"), str):
            e["metrics"] = [m.strip() for m in e["metrics"].split(",") if m.strip()]
        e["created"] = now()
        e["updated"] = now()
        obj.setdefault("experiments", []).append(e)
        return obj

    _mutate("experiments", fn)
    return {"ok": True}


def update_experiment(eid, patch):
    def fn(obj):
        for e in obj.get("experiments", []):
            if e["id"] != eid:
                continue
            e.update(_pick(patch, EXP_FIELDS))
            if isinstance(e.get("metrics"), str):
                e["metrics"] = [m.strip() for m in e["metrics"].split(",") if m.strip()]
            if patch.get("result") and isinstance(patch["result"], dict):
                e.setdefault("result", {})
                e["result"].update(patch["result"])
            e["updated"] = now()
            return obj
        return None

    _mutate("experiments", fn)
    return {"ok": True}


def remove_experiment(eid):
    def fn(obj):
        obj["experiments"] = [e for e in obj.get("experiments", []) if e["id"] != eid]
        return obj

    _mutate("experiments", fn)
    return {"ok": True}


# ---------------------------------------------------------------- 用户画像补充资料
# 用户在界面上"自主输入"的画像字段（researcher-profile.md 之外的自由补充），
# 供规则解析与 AI 判断共同消费。字段统一为字符串/多行文本。
PROFILE_EXTRA_KEYS = (
    "direction_cn", "direction_en",       # 研究方向：中文 / 英文
    "skills",                              # 技能栈（逗号分隔）
    "works",                               # 在投/已发工作
    "innovations",                         # N1-N5 创新点（每行一条）
    "target_journals",                     # 目标期刊（逗号分隔）
    "constraints",                         # 时间/资源约束
    "goals",                               # 短期目标
    "notes",                               # 其他
)


def load_profile_extra():
    obj = _load("profile_extra")
    return obj.get("profile_extra") or {}


def save_profile_extra(data):
    def fn(obj):
        cur = obj.get("profile_extra") or {}
        for k in PROFILE_EXTRA_KEYS:
            if k in data and isinstance(data[k], str):
                cur[k] = data[k].strip()
        obj["profile_extra"] = cur
        return obj

    _mutate("profile_extra", fn)
    return {"ok": True, "profile_extra": load_profile_extra()}



# ---------------------------------------------------------------- 复现记录
REP_FIELDS = ("exp_id", "by", "env", "result", "diff_note", "metrics_diff", "note")


def list_reproductions(exp_id=None):
    reps = _load("reproductions").get("reproductions", [])
    if exp_id:
        reps = [r for r in reps if r.get("exp_id") == exp_id]
    return reps


def add_reproduction(data):
    def fn(obj):
        if not data.get("exp_id"):
            return None
        r = _pick(data, REP_FIELDS)
        r["id"] = _uid("r", obj.get("reproductions", []))
        r.setdefault("by", "")
        r.setdefault("result", "running")  # running | reproduced | partial | failed
        r.setdefault("env", "")
        r["date"] = now().split(" ")[0]
        obj.setdefault("reproductions", []).append(r)
        return obj

    _mutate("reproductions", fn)
    return {"ok": True}


def update_reproduction(rid, patch):
    def fn(obj):
        for r in obj.get("reproductions", []):
            if r["id"] == rid:
                r.update(_pick(patch, REP_FIELDS))
                if patch.get("metrics_diff") and isinstance(patch["metrics_diff"], dict):
                    r.setdefault("metrics_diff", {})
                    r["metrics_diff"].update(patch["metrics_diff"])
                return obj
        return None

    _mutate("reproductions", fn)
    return {"ok": True}


# ---------------------------------------------------------------- 校验
def validate_csv_text(csv_text):
    """对结果 CSV 做常规校验，返回结构化报告。不引入第三方 CSV 依赖，手写解析。

    检查项：空文件 / 表头缺失 / 空列 / 列数不齐 / 非数值(应有数值的列) / 重复表头。
    供「实验数据验证」接口与前端直接使用。
    """
    import io as _io

    report = {"ok": True, "lines": [], "errors": [], "header": [], "rows": 0}
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if not lines:
        report["ok"] = False
        report["errors"].append("文件为空")
        return report

    def _split(line):
        # 简单逗号切分，忽略引号内逗号（最小实现）
        out, cur, inq = [], "", False
        for ch in line:
            if ch == '"':
                inq = not inq
            elif ch == "," and not inq:
                out.append(cur.strip())
                cur = ""
            else:
                cur += ch
        out.append(cur.strip())
        return out

    header = _split(lines[0])
    if not header or not any(h for h in header):
        report["ok"] = False
        report["errors"].append("首行不是有效表头")
        return report
    dup = sorted({h for h in header if header.count(h) > 1})
    if dup:
        report["ok"] = False
        report["errors"].append("表头有重复列：" + ", ".join(dup))
    report["header"] = header
    ncols = len(header)

    numeric_like = {}
    body = []
    for i, ln in enumerate(lines[1:], start=2):
        cells = _split(ln)
        if len(cells) != ncols:
            report["ok"] = False
            report["errors"].append("第 %d 行列数 %d ≠ 表头 %d" % (i, len(cells), ncols))
            continue
        body.append(cells)
        for ci, h in enumerate(header):
            v = cells[ci]
            if v and not numeric_like.get(h, False) and _is_num(v):
                numeric_like[h] = True
    report["rows"] = len(body)
    report["lines"].append("表头 %d 列：%s" % (ncols, ", ".join(header)))
    report["lines"].append("数据行：%d" % len(body))

    # 表头含数值期望词（acc/f1/loss/score…）时，该列任何非数值单元格都报错
    want_num = [h for h in header if re.search(r"(acc|f1|loss|score|time|epoch|err|metric|数值|指标)", h, re.I)]
    for h in want_num:
        idx = header.index(h)
        bad = sum(1 for cells in body if not _is_num(cells[idx]))
        if bad:
            report["ok"] = False
            report["errors"].append("指标列「%s」有 %d 个非数值单元格" % (h, bad))

    if report["ok"]:
        report["lines"].append("校验通过：列数一致，数值列合法。")
    return report


def _is_num(v):
    try:
        float(v)
        return True
    except Exception:
        return False


def diff_metrics(a, b):
    """比较两组指标字典，返回一致项与偏差（用于复现一致性判断）。"""
    same, diff = {}, {}
    for k in set(a) | set(b):
        if k not in a or k not in b:
            diff[k] = {"原": a.get(k), "复现": b.get(k)}
            continue
        try:
            x, y = float(a[k]), float(b[k])
            if abs(x - y) <= 1e-6 or (abs(x) > 1e-9 and abs(x - y) / abs(x) <= 0.02):
                same[k] = y
            else:
                diff[k] = {"原": x, "复现": y}
        except Exception:
            same[k] = b[k] if a[k] == b[k] else diff.setdefault(k, {"原": a[k], "复现": b[k]})
    return same, diff
