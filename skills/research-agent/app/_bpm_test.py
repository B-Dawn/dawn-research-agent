# -*- coding: utf-8 -*-
"""流程引擎全生命周期测试。"""
import json
import os
import bpm

# 每次运行前重置流程数据，保证测试可重复（沙箱禁止删文件，改用清空写回）
bpm._save(json.loads(json.dumps(bpm._EMPTY)))

OK = FAIL = 0

def ck(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  ✓ %s %s" % (label, extra))
    else:
        FAIL += 1
        print("  ✗ %s %s" % (label, extra))


def todo_of(iid, user=None):
    """取指定实例的待办任务 id（避免与其他用例串扰）。"""
    def scan(u):
        for t in bpm.todo(u):
            if t["inst_id"] == iid:
                return t
        return None
    if user:
        return scan(user)
    for u in ("admin", "CNL"):
        t = scan(u)
        if t:
            return t
    return None

bpm.ensure_templates()
defs = bpm.def_list("admin")
print("=== 1. 内置模板 ===")
for d in defs:
    print("   %s | %s | %s 节点" % (d["id"], d["name"], len(d.get("nodes") or [])))
ck("内置模板已生成", len(defs) >= 4)

print("\n=== 2. 线性审批：提交 → 导师(CNL) → 学院(teacher) → 通过 ===")
r = bpm.def_save("admin", {
    "name": "测试·论文送审", "category": "论文",
    "form": [{"key": "title", "label": "标题", "type": "text", "required": True}],
    "nodes": [
        {"id": "n1", "type": "start", "name": "提交"},
        {"id": "n2", "type": "approve", "name": "导师审批", "assignee_type": "user", "assignee": "CNL",
         "sign_mode": "or", "allow_reject": True, "reject_to": "initiator"},
        {"id": "n3", "type": "approve", "name": "学院审批", "assignee_type": "role", "assignee": "teacher",
         "sign_mode": "or", "allow_reject": True, "reject_to": "n2"},
        {"id": "n4", "type": "end", "name": "结束"}],
    "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}, {"from": "n3", "to": "n4"}]})
ck("定义保存", r.get("ok"), r.get("error", ""))
did = r["id"]

r = bpm.wf_start(did, "admin", title="送审：图增强 IDS", form={"title": "图增强 IDS"})
ck("发起流程", r.get("ok"), r.get("error", ""))
inst = r["instance"]; iid = inst["id"]
ck("状态=running", inst["status"] == "running")
ck("当前节点=导师审批", (inst.get("cur") or [""])[0] == "n2")

# 必填校验
r2 = bpm.wf_start(did, "admin", title="x", form={})
ck("必填校验生效", not r2.get("ok"), r2.get("error", ""))

todo_cnl = bpm.todo("CNL")
ck("CNL 有待办", len(todo_cnl) == 1, "todo=%d" % len(todo_cnl))
ck("admin 无待办", len(bpm.todo("admin")) == 0)
tid = todo_cnl[0]["id"]
ck("节点名正确", todo_cnl[0]["name"] == "导师审批")

r = bpm.act(iid, tid, "admin", "approve")
ck("越权被拒", not r.get("ok"), r.get("error", ""))
r = bpm.act(iid, tid, "CNL", "approve", "同意")
ck("导师通过", r.get("ok"), r.get("error", ""))
inst = r["instance"]
ck("推进到学院审批", (inst.get("cur") or [""])[0] == "n3")

t3 = bpm.todo("CNL")
ck("学院审批待办(role=teacher→CNL)", len(t3) == 1)
r = bpm.act(iid, t3[0]["id"], "CNL", "approve", "学院同意")
ck("学院通过", r.get("ok"), r.get("error", ""))
inst = r["instance"]
ck("流程已通过", inst["status"] == "approved", inst["status"])
prog = bpm.progress(inst)
ck("进度图全 done", all(p["state"] == "done" for p in prog), str([p["state"] for p in prog]))

print("\n=== 3. 驳回 → 发起人 → 重新提交 ===")
r = bpm.wf_start(did, "admin", title="送审2", form={"title": "送审2"})
iid2 = r["instance"]["id"]
tid2 = todo_of(iid2, "CNL")["id"]
r = bpm.act(iid2, tid2, "CNL", "reject", "格式不符")
ck("驳回成功", r.get("ok"), r.get("error", ""))
ck("状态=rejected", r["instance"]["status"] == "rejected", r["instance"]["status"])
r = bpm.resubmit(iid2, "CNL")
ck("非发起人不能重提", not r.get("ok"), r.get("error", ""))
r = bpm.resubmit(iid2, "admin", form={"title": "送审2-已修改"})
ck("发起人重提", r.get("ok"), r.get("error", ""))
ck("重提后回到导师节点", (r["instance"].get("cur") or [""])[0] == "n2")
ck("表单已更新", r["instance"]["form"]["title"] == "送审2-已修改")

print("\n=== 4. 中间节点驳回到上一节点 ===")
tid3 = todo_of(iid2, "CNL")["id"]
bpm.act(iid2, tid3, "CNL", "approve", "导师同意")
t = todo_of(iid2, "CNL")
r = bpm.act(iid2, t["id"], "CNL", "reject", "学院要求补充材料")
ck("驳回到上一节点", r.get("ok"), r.get("error", ""))
ck("回到导师审批", (r["instance"].get("cur") or [""])[0] == "n2", str(r["instance"].get("cur")))
ck("状态仍 running", r["instance"]["status"] == "running")

print("\n=== 5. 转办 / 委派 / 抄送 / 加签 ===")
# 转办
tid4 = todo_of(iid2, "CNL")["id"]
r = bpm.act(iid2, tid4, "CNL", "transfer", payload={"to": "admin"})
ck("转办给 admin", r.get("ok"), r.get("error", ""))
ck("admin 收到待办(转办)", todo_of(iid2, "admin") is not None)
ck("CNL 已无该待办", todo_of(iid2, "CNL") is None)
# 委派
tadmin = todo_of(iid2, "admin")["id"]
r = bpm.act(iid2, tadmin, "admin", "delegate", payload={"to": "CNL"})
ck("委派给 CNL", r.get("ok"), r.get("error", ""))
ck("CNL 显示委派待办", todo_of(iid2, "CNL") is not None)
r = bpm.act(iid2, tadmin, "CNL", "approve", "代办完成")
ck("委派对象可办理", r.get("ok"), r.get("error", ""))
ck("委派办完退回委托人(仍待办)", todo_of(iid2, "admin") is not None)
# 抄送
r = bpm.act(iid2, todo_of(iid2, "admin")["id"], "admin", "cc", payload={"users": ["CNL"]})
ck("抄送成功", r.get("ok"), r.get("error", ""))
ck("CNL 收到抄送", len(bpm.cc_list("CNL")) >= 1)
# 前加签
tid5 = todo_of(iid2, "admin")["id"]
r = bpm.act(iid2, tid5, "admin", "addsign_before", payload={"users": ["CNL"]})
ck("前加签成功", r.get("ok"), r.get("error", ""))
ck("加签任务出现在 CNL 待办", any("前加签" in t["name"] for t in bpm.todo("CNL")))
r = bpm.act(iid2, tid5, "admin", "approve")
ck("前加签未完成时不能审批", not r.get("ok"), r.get("error", ""))
addsign_t = [t for t in bpm.todo("CNL") if "前加签" in t["name"] and t["inst_id"] == iid2][0]
r = bpm.act(iid2, addsign_t["id"], "CNL", "approve", "加签同意")
ck("加签完成", r.get("ok"), r.get("error", ""))
r = bpm.act(iid2, tid5, "admin", "approve", "最终通过")
ck("加签后可审批", r.get("ok"), r.get("error", ""))

print("\n=== 6. 会签 + 条件分支 ===")
r = bpm.def_save("admin", {
    "name": "测试·会签+分支", "category": "测试",
    "form": [{"key": "title", "label": "标题", "type": "text", "required": True},
             {"key": "amount", "label": "数量", "type": "number", "required": True}],
    "nodes": [
        {"id": "n1", "type": "start", "name": "提交"},
        {"id": "n2", "type": "approve", "name": "会签节点", "assignee_type": "role", "assignee": "teacher",
         "sign_mode": "and", "allow_reject": True, "reject_to": "initiator"},
        {"id": "n3", "type": "approve", "name": "大额审批", "assignee_type": "role", "assignee": "admin",
         "sign_mode": "or", "allow_reject": True, "reject_to": "n2"},
        {"id": "n4", "type": "end", "name": "结束"}],
    "edges": [{"from": "n1", "to": "n2"},
              {"from": "n2", "to": "n3", "cond": "amount > 10"},
              {"from": "n2", "to": "n4", "cond": ""},
              {"from": "n3", "to": "n4"}]})
did2 = r["id"]
# 小额：跳过 n3
r = bpm.wf_start(did2, "admin", title="小额", form={"title": "小额", "amount": 5})
iid3 = r["instance"]["id"]
ck("小额走默认分支", (r["instance"].get("cur") or [""])[0] == "n2")
_t = todo_of(iid3, "CNL")
r = bpm.act(iid3, _t["id"], "CNL", "approve", "会签通过")
ck("小额直接结束", r["instance"]["status"] == "approved", r["instance"]["status"])
# 大额：走 n3
r = bpm.wf_start(did2, "admin", title="大额", form={"title": "大额", "amount": 20})
iid4 = r["instance"]["id"]
_t = todo_of(iid4, "CNL")
r = bpm.act(iid4, _t["id"], "CNL", "approve", "会签通过")
ck("大额进入大额审批", (r["instance"].get("cur") or [""])[0] == "n3", str(r["instance"].get("cur")))
_ta = todo_of(iid4, "admin")
ck("admin 收到待办", _ta is not None)
r = bpm.act(iid4, _ta["id"], "admin", "approve", "同意")
ck("大额通过", r["instance"]["status"] == "approved")

print("\n=== 7. 撤销 / 终止 ===")
r = bpm.wf_start(did, "admin", title="待撤销", form={"title": "待撤销"})
iid5 = r["instance"]["id"]
r = bpm.revoke(iid5, "CNL")
ck("非发起人不能撤销", not r.get("ok"), r.get("error", ""))
r = bpm.revoke(iid5, "admin")
ck("发起人撤销", r.get("ok"), r.get("error", ""))
ck("状态=canceled", r["instance"]["status"] == "canceled")
ck("撤销后待办清零", len([t for t in bpm.todo("CNL") if t["inst_id"] == iid5]) == 0)

print("\n=== 8. 查询与统计 ===")
s = bpm.stats("admin", is_admin=True)
print("   stats:", json.dumps(s, ensure_ascii=False))
ck("我的发起计数", s["mine_total"] >= 4)
ck("实例列表可查", len(bpm.inst_list("admin")) >= 4)
ck("已办列表可查", len(bpm.done_list("CNL")) >= 3)
inst = bpm.inst_get(iid)
ck("实例详情含历史", len(inst["history"]) >= 3, "history=%d" % len(inst["history"]))
ck("实例详情含进度", len(bpm.progress(inst)) == 4)

print("\n==== 结果：通过 %d，失败 %d ====" % (OK, FAIL))

print("\n=== 9. 清理测试数据 ===")
def _clean(obj):
    keep = [d for d in obj.get("defs", []) if d.get("builtin")]
    obj["defs"] = keep
    obj["instances"] = []
    return obj
bpm._mutate(_clean)
ck("测试数据已清理", len(bpm._load()["instances"]) == 0 and len(bpm._load()["defs"]) == 4)
print("\n==== 最终：通过 %d，失败 %d ====" % (OK, FAIL))
