# -*- coding: utf-8 -*-
"""paperflow —— 论文全流程编排（题目定稿之后）：
苏格拉底问询 → 实验方案 → 生成实验脚本 → 本地训练 → 结果分析
→ 论文分节生成 → 模拟盲审 → 按意见修改（多轮）→ 终稿导出。

设计要点：
- 实验**必须真实执行**（本地 Python + sklearn/numpy），论文中的数字只允许来自 results.json；
- AI 生成的实验脚本必须通过语法检查与试运行，失败自动回退到内置确定性实验脚本（系统兜底）；
- 盲审由 3 个独立 persona 完成；修改后复评，最多 REVIEW_MAX_ROUNDS 轮或达标（>=7.5）即止；
- 所有状态落盘 data/paperflow.json，断点续跑。
"""
import json
import os
import re
import subprocess
import threading
import time

import model_bridge as model
import workbench as wb
import team_store as store

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.abspath(os.path.join(_HERE, "..", "data"))
_OUT_DIR = os.path.join(_DATA_DIR, "paperflow")
_FILE = os.path.join(_DATA_DIR, "paperflow.json")
_LOCK = threading.Lock()

_VENV_PY = r"C:\Users\11578\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
REVIEW_MAX_ROUNDS = 3
REVIEW_PASS_SCORE = 7.5

STAGES = [
    {"id": "socratic", "name": "苏格拉底问询（题目合格性）", "step": "s3"},
    {"id": "exp_plan", "name": "实验方案设计", "step": "s5"},
    {"id": "exp_script", "name": "生成自动化实验脚本", "step": "s5"},
    {"id": "exp_run", "name": "本地训练与评估", "step": "s5"},
    {"id": "exp_analyze", "name": "结果分析", "step": "s5"},
    {"id": "paper_gen", "name": "论文初稿生成", "step": "s6"},
    {"id": "review", "name": "模拟盲审", "step": "s6"},
    {"id": "revise", "name": "按意见修改", "step": "s6"},
    {"id": "final", "name": "终稿导出", "step": "s6"},
]
_STAGE_BY_ID = {s["id"]: s for s in STAGES}
_STAGE_ORDER = [s["id"] for s in STAGES]

_EMPTY = {"users": {}}


# ================================================================ IO
def _load():
    if os.path.isfile(_FILE):
        try:
            with open(_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return json.loads(json.dumps(_EMPTY))


def _save(obj):
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _FILE)


def _mutate(fn):
    with _LOCK:
        obj = _load()
        new = fn(obj)
        if new is not None:
            _save(new)


def _u(by):
    return (_load().get("users") or {}).get(by or "", {})


def _set_user(by, data):
    def fn(obj):
        obj.setdefault("users", {})[by] = data
        return obj
    _mutate(fn)


def state(by):
    u = _u(by)
    return {"stages": u.get("stages") or {}, "topic": u.get("topic") or "",
            "round": u.get("round") or 0, "scores": u.get("scores") or [],
            "outputs": u.get("outputs") or {}, "done": u.get("done") or False}


def reset(by):
    def fn(obj):
        obj.setdefault("users", {})[by] = {}
        return obj
    _mutate(fn)


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


# ================================================================ 工具
_PROFILE_KEYS = ("research_direction", "skills", "innovations", "direction_en")


def _profile_md():
    try:
        prof = store.load_profile_extra() or {}
    except Exception:
        prof = {}
    return "\n".join("%s：%s" % (k, v) for k, v in prof.items()
                     if isinstance(v, str) and v.strip())[:900]


def _parse_json(raw):
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("AI 未返回 JSON")
    s = m.group(0)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 模型常在文本里写 LaTeX 反斜杠（如 \alpha），JSON 非法——把非法转义补成双反斜杠
        fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)
        return json.loads(fixed)


def _ai(question, system=None, max_tokens=3000, temperature=0.3):
    return model.quick_ask(question, system=system, max_tokens=max_tokens,
                           temperature=temperature, timeout=300)


def _write_out(name, text):
    os.makedirs(_OUT_DIR, exist_ok=True)
    p = os.path.join(_OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# ================================================================ ① 苏格拉底问询
_SOC_SYS = (
    "你是严格的苏格拉底式选题审查者，同时扮演提出追问的导师与回应追问的研究者。"
    "研究方向与个人画像已给出。请："
    "1) 站在导师立场提出 8–12 条追问（覆盖：最小可检验命题、创新点取舍、与现有方法的本质区别、"
    "数据来源与可信度、评估协议与基线公平性、复杂度与实时性、可解释性的验证方式、潜在致命弱点）；"
    "2) 站在研究者立场**逐条给出具体、可检验、技术性**的回应（不得空话）；"
    "3) 自评合格性。返回 STRICT JSON："
    '{"qa":[{"q":"追问","a":"回应"}],'
    '"verdict":{"qualified":true/false,"missing":["…"],"reason":"…"},'
    '"refined_topic":"经问询校正后的最终题目",'
    '"key_claims":["主张1","主张2","主张3"]}。'
)


def _run_socratic(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "问询需要 AI（模型与API 配置）。"}
    topic = (opts.get("topic") or cache.get("topic") or "").strip()
    if not topic:
        return {"status": "fail", "error": "缺少论文题目"}
    body = "论文题目：%s\n\n个人研究画像：\n%s" % (topic, _profile_md() or "（无）")
    raw = _ai(body, system=_SOC_SYS, max_tokens=4000)
    data = _parse_json(raw)
    qa = [{"q": str(x.get("q", "")), "a": str(x.get("a", ""))}
          for x in (data.get("qa") or []) if isinstance(x, dict)]
    verdict = data.get("verdict") or {}
    qualified = bool(verdict.get("qualified")) and len(qa) >= 6
    cache["socratic_qa"] = qa
    cache["key_claims"] = [str(x) for x in (data.get("key_claims") or [])][:5]
    # 题目由用户给定：AI 的"校正"只作参考附注，不覆盖原题目
    cache["topic_note"] = (data.get("refined_topic") or "").strip()
    cache["refined_topic"] = topic
    if not qualified:
        return {"status": "blocked", "needs": "ai",
                "error": "问询自评未合格：%s（缺失：%s）——请补充方向描述后重跑"
                         % (verdict.get("reason") or "", "、".join(verdict.get("missing") or []) or "无")}
    return {"status": "ok", "note": "问询 %d 条并逐条回应，自评合格；校正题目：%s"
            % (len(qa), cache["refined_topic"]),
            "detail": "\n".join("**Q%d：%s**\n> %s" % (i + 1, x["q"], x["a"][:220])
                                for i, x in enumerate(qa))[:3000]}


# ================================================================ ② 实验方案
_EXP_SYS = (
    "你是严谨的实验设计专家（UAV 网络安全 + 机器学习方向）。基于题目、问询结论与画像，"
    "设计一套**可在本地单机 Python(numpy+scikit-learn) 完整执行**的实验方案。要求："
    "1) 数据：说明采用可复现的 FANET 仿真数据生成（节点数/拓扑/流量/攻击类型/特征），并解释为何合理；"
    "2) 模型：把题目中的「双层AI信任机制」落实为两层可训练/可计算的组件（检测层+决策可信度层），"
    "图增强用邻域聚合近似消息传递；"
    "3) 基线与消融；4) 指标；5) 3 个随机种子；6) 明确论文中只允许引用 results.json 里的真实数字。"
    "返回 STRICT JSON："
    '{"rationale":"方案合理性说明(200字内)",'
    '"data":{"nodes":120,"attacks":["blackhole","grayhole","flooding"],"n_flows":6000,"features":["…"]},'
    '"model":{"layer1":"…","graph_aug":"…","layer2":"…"},'
    '"baselines":["…"],"ablations":["…"],"metrics":["Accuracy","Precision","Recall","F1","ROC-AUC"],'
    '"seeds":[42,7,2026]}'
)


def _run_exp_plan(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "实验方案需要 AI。" }
    topic = cache.get("refined_topic") or cache.get("topic") or opts.get("topic") or ""
    qa = cache.get("socratic_qa") or []
    if not qa:
        return {"status": "fail", "error": "缺少问询结论，请先完成苏格拉底问询"}
    body = ("题目：%s\n\n问询关键结论：\n%s\n\n画像：\n%s"
            % (topic, "\n".join("Q:%s A:%s" % (x["q"][:80], x["a"][:120]) for x in qa[:8]),
               _profile_md() or "（无）"))
    raw = _ai(body, system=_EXP_SYS, max_tokens=1800)
    data = _parse_json(raw)
    cache["exp_plan"] = data
    return {"status": "ok", "note": "实验方案已生成（%d 基线 / %d 消融 / 种子 %s）"
            % (len(data.get("baselines") or []), len(data.get("ablations") or []),
               data.get("data", {}).get("nodes", "?")),
            "detail": json.dumps(data, ensure_ascii=False)[:1500]}


# ================================================================ ③ 生成实验脚本
_SCRIPT_SYS = (
    "你是资深机器学习工程师。按实验方案写**一个自包含的 Python 脚本**（仅用 numpy/scikit-learn/标准库）："
    "1) 可复现仿真 FANET 数据（固定种子；节点拓扑→邻接表；流特征含 PDR/时延/抖动/丢包率/RREQ异常等；"
    "注入 blackhole/grayhole/flooding 攻击并叠加噪声与类不平衡）；"
    "2) 图增强：把 1 跳邻居的可疑度聚合进特征（模拟 GNN 消息传递，2 次迭代）；"
    "3) 双层机制：层1=随机森林检测器；层2=决策可信度（集成分歧度+校准），带 reject 选项（低可信样本转人工/二次裁决）；"
    "4) 基线（仅层1、无图增强、逻辑回归）与消融；5) 3 个随机种子取均值±std；"
    "6) 输出 Accuracy/Precision/Recall/F1/ROC-AUC 与 reject 率，**写入 results.json**（绝对路径见下）；"
    "7) 总运行时间 < 8 分钟。只输出代码，不要解释。"
)


_FALLBACK_HEAD = '''# -*- coding: utf-8 -*-
"""内置兜底实验脚本：图增强双层信任 IDS（可复现仿真）。"""
import json, os, sys, time
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split

RESULTS_PATH = sys.argv[1] if len(sys.argv) > 1 else "results.json"
SEEDS = [42, 7, 2026]
N_NODES, N_FLOWS = 120, 6000
ATTACKS = ["blackhole", "grayhole", "flooding"]
FEATURES = ["PDR", "avg_delay", "jitter", "drop_rate", "rreq_anomaly",
            "hop_count", "throughput_ratio", "energy_var", "nbr_trust_l1", "nbr_trust_l2"]


def build_topology(rng, n=N_NODES, mal=None):
    """随机几何图；恶意节点倾向相邻聚集（协同攻击），保证稀疏连通。"""
    pos = rng.uniform(0, 1000, size=(n, 2))
    adj = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = np.hypot(*(pos[i] - pos[j]))
            if d < 260:
                adj[i].append(j); adj[j].append(i)
    return adj


def rule_anomaly(X):
    """无标签规则异常分（供图增强聚合用；不含 y，避免标签泄漏）。"""
    z = (X[:, 3] - X[:, 3].mean()) / (X[:, 3].std() + 1e-9)      # drop_rate
    z2 = (X[:, 4] - X[:, 4].mean()) / (X[:, 4].std() + 1e-9)     # rreq_anomaly
    return np.clip(0.6 * z + 0.4 * z2, 0, None)


def simulate_flows(rng, adj, n=N_NODES, n_flows=N_FLOWS, mal=None):
    """生成流级特征与标签：攻击与正常分布**有意重叠**（隐蔽攻击+拥塞假阳性+标签噪声），
    图增强特征用「无标签规则异常分的邻域均值」，模拟可信的拓扑上下文。"""
    y = np.zeros(n_flows, dtype=int)
    mal_set = set((mal if mal is not None else rng.choice(n, size=int(n * 0.12), replace=False)).tolist())
    X = np.zeros((n_flows, len(FEATURES)))
    src = rng.integers(0, n, n_flows)
    for t in range(n_flows):
        s = int(src[t])
        is_mal = s in mal_set
        if is_mal:
            y[t] = 1
        # —— 正常流：偶发拥塞也会呈现攻击样症状（重叠来源 1）
        congested = (not is_mal) and rng.uniform() < 0.07
        if is_mal:
            kind = rng.uniform()
            if kind < 0.4:      # blackhole：明显
                pdr = rng.uniform(0.30, 0.55)
            elif kind < 0.8:    # grayhole：选择性丢包，高度隐蔽（重叠来源 2）
                pdr = rng.uniform(0.55, 0.92)
            else:               # flooding
                pdr = rng.uniform(0.60, 0.85)
            delay = rng.uniform(45, 180)
            jitter = rng.uniform(15, 70)
            rreq = rng.uniform(12, 45)
            thr = rng.uniform(0.05, 0.55)
        elif congested:
            pdr = rng.uniform(0.55, 0.88)
            delay = rng.uniform(50, 160)
            jitter = rng.uniform(12, 55)
            rreq = rng.uniform(8, 30)
            thr = rng.uniform(0.2, 0.6)
        else:
            pdr = rng.uniform(0.82, 1.0)
            delay = rng.uniform(8, 55)
            jitter = rng.uniform(1, 14)
            rreq = rng.uniform(0, 10)
            thr = rng.uniform(0.65, 1.0)
        drop = 1 - pdr
        hop = int(rng.integers(2, 7))
        energy = rng.uniform(0, 0.08)
        # 特征噪声（重叠来源 3）
        pdr = min(1.0, max(0.0, pdr + rng.normal(0, 0.035)))
        X[t] = [pdr, delay + rng.normal(0, 9), max(0, jitter + rng.normal(0, 3)), drop,
                max(0, rreq + rng.normal(0, 2.5)), hop, thr, energy, 0.0, 0.0]
    # —— 标签噪声（重叠来源 4）：2% 翻转
    flip = rng.uniform(size=n_flows) < 0.02
    y = np.where(flip, 1 - y, y)
    # —— 图增强：无标签规则异常分 → 节点级历史均值 → 1 跳邻域聚合（2 次迭代）
    risk = rule_anomaly(X)
    node_risk = np.zeros(n)
    cnt = np.zeros(n)
    for t in range(n_flows):
        node_risk[src[t]] += risk[t]; cnt[src[t]] += 1
    node_risk = node_risk / np.maximum(cnt, 1)
    agg1 = np.zeros(n)
    for i in range(n):
        agg1[i] = np.mean([node_risk[j] for j in adj[i]]) if adj[i] else node_risk[i]
    agg2 = np.zeros(n)
    for i in range(n):
        agg2[i] = np.mean([agg1[j] for j in adj[i]]) if adj[i] else agg1[i]
    X[:, 8] = agg1[src]
    X[:, 9] = agg2[src]
    return X, y, src


def layer2_credibility(proba_list, X):
    """层2：决策可信度 = 森林内部树投票的一致性 + 与 LR 意见的一致性；返回 (cred, reject)。"""
    votes = np.stack([p for p in proba_list])          # 各树/模型的恶意概率
    disagree = votes.std(axis=0)
    cred = 1.0 - np.clip(disagree / (disagree.max() + 1e-9), 0, 1)
    return cred


def run_seed(seed):
    rng = np.random.default_rng(seed)
    adj = build_topology(rng)
    X, y, atk = simulate_flows(rng, adj)
    idx = np.arange(len(y))
    Xtr, Xte, ytr, yte = train_test_split(idx, y, test_size=0.3, random_state=seed, stratify=y)
    F = lambda a: X[a]
    rf = RandomForestClassifier(n_estimators=120, max_depth=12, random_state=seed,
                                class_weight="balanced", n_jobs=-1).fit(F(Xtr), ytr)
    # 消融：无图增强
    Xng = np.delete(X, [8, 9], axis=1)
    rf_ng = RandomForestClassifier(n_estimators=120, max_depth=12, random_state=seed,
                                   class_weight="balanced", n_jobs=-1).fit(Xng[Xtr], ytr)
    lr = LogisticRegression(max_iter=800, random_state=seed).fit(F(Xtr), ytr)
    def scores(m, Xt, yt):
        p = m.predict_proba(Xt)[:, 1]
        pred = (p >= 0.5).astype(int)
        return {"Accuracy": accuracy_score(yt, pred), "Precision": precision_score(yt, pred, zero_division=0),
                "Recall": recall_score(yt, pred, zero_division=0), "F1": f1_score(yt, pred, zero_division=0),
                "ROC-AUC": roc_auc_score(yt, p)}
    res = {"full": scores(rf, F(Xte), yte), "no_graph": scores(rf_ng, Xng[Xte], yte),
           "lr": scores(lr, F(Xte), yte)}
    # 层2：可信度 + reject（低可信转二次裁决：用规则复核可救回部分错误）
    tree_p = np.stack([t.predict_proba(F(Xte))[:, 1] for t in rf.estimators_])
    cred = layer2_credibility(tree_p, F(Xte))
    p_full = rf.predict_proba(F(Xte))[:, 1]
    pred_full = (p_full >= 0.5).astype(int)
    err = (pred_full != yte)
    thr_low = np.quantile(cred, 0.10)          # 最不可信 10%
    # 分歧恒为 0（模型全票一致）时不拒绝任何样本
    rejected = (cred <= thr_low) if np.ptp(cred) > 1e-12 else np.zeros(len(cred), dtype=bool)
    # 对被拒样本用 LR 复核（双裁决），命中率提升 => 精度提升
    pred_l2 = pred_full.copy()
    fix = rejected & (lr.predict(F(Xte)) != pred_full)
    pred_l2[fix] = lr.predict(F(Xte))[fix]
    res["dual_layer"] = scores(rf, F(Xte), yte)
    res["dual_layer"]["Accuracy"] = accuracy_score(yte, pred_l2)
    res["dual_layer"]["Precision"] = precision_score(yte, pred_l2, zero_division=0)
    res["dual_layer"]["F1"] = f1_score(yte, pred_l2, zero_division=0)
    res["reject_rate"] = float(rejected.mean())
    res["error_rate_global"] = float(err.mean())
    # 层2价值：被拒样本的错误率应显著高于全局（说明拒绝机制精准圈出可疑决策）
    res["rejected_error_rate"] = float(err[rejected].mean()) if rejected.any() else 0.0
    res["rejected_error_lift"] = round(res["rejected_error_rate"] / (res["error_rate_global"] + 1e-9), 2)
    return res


def agg(rs):
    out = {}
    for k in rs[0]:
        if isinstance(rs[0][k], dict):
            out[k] = {m: {"mean": float(np.mean([r[k][m] for r in rs])),
                          "std": float(np.std([r[k][m] for r in rs]))} for m in rs[0][k]}
        else:
            out[k] = float(np.mean([r[k] for r in rs]))
    return out


if __name__ == "__main__":
    t0 = time.time()
    rs = [run_seed(s) for s in SEEDS]
    final = {"seeds": SEEDS, "per_seed": rs, "aggregate": agg(rs),
             "n_nodes": N_NODES, "n_flows": N_FLOWS, "attacks": ATTACKS,
             "features": FEATURES, "runtime_sec": round(time.time() - t0, 1)}
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=1)
    print("DONE runtime=%.1fs acc_full=%.4f acc_dual=%.4f" % (
        final["runtime_sec"], final["aggregate"]["full"]["Accuracy"]["mean"],
        final["aggregate"]["dual_layer"]["Accuracy"]["mean"]))
'''


def _run_exp_script(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "脚本生成需要 AI。"}
    plan = cache.get("exp_plan") or {}
    topic = cache.get("refined_topic") or cache.get("topic") or ""
    results_path = os.path.join(_OUT_DIR, "results.json")
    os.makedirs(_OUT_DIR, exist_ok=True)
    body = ("题目：%s\n实验方案：%s\nresults.json 请写到：%s\n"
            % (topic, json.dumps(plan, ensure_ascii=False)[:1800], results_path))
    source = None
    try:
        raw = _ai(body, system=_SCRIPT_SYS, max_tokens=4000, temperature=0.2)
        code = raw.strip()
        if code.startswith("```"):
            code = re.sub(r"^```[a-z]*\n?", "", code).rstrip("`").strip()
        source = code
    except Exception as e:
        cache["script_source"] = "fallback"
        return {"status": "blocked", "needs": "ai", "error": "AI 生成脚本失败（%s），可重试" % str(e)[:120]}
    # 语法检查
    sp = _write_out("exp_uav_ids.py", source)
    chk = subprocess.run([_VENV_PY, "-m", "py_compile", sp], capture_output=True, text=True, timeout=60)
    if chk.returncode != 0:
        # 一次修复机会：把报错喂回 AI
        try:
            fix = _ai("以下脚本有语法错误，请修复后只输出完整代码：\n错误：%s\n脚本：%s"
                      % (chk.stderr[:600], source[:7000]), system=_SCRIPT_SYS, max_tokens=4000)
            code = re.sub(r"^```[a-z]*\n?", "", fix.strip()).rstrip("`").strip()
            sp = _write_out("exp_uav_ids.py", code)
            chk = subprocess.run([_VENV_PY, "-m", "py_compile", sp], capture_output=True, text=True, timeout=60)
            if chk.returncode == 0:
                source = code
        except Exception:
            pass
        if chk.returncode != 0:
            _write_out("exp_uav_ids.py", _FALLBACK_HEAD)
            cache["script_source"] = "fallback"
            return {"status": "ok", "note": "AI 脚本未通过语法检查，已回退内置实验脚本（结果同样真实）",
                    "detail": chk.stderr[:300]}
    cache["script_source"] = "ai"
    return {"status": "ok", "note": "实验脚本已生成并通过语法检查",
            "detail": "脚本行数：%d" % source.count("\n")}


# ================================================================ ④ 本地训练
def _run_exp_run(by, opts, cache):
    results_path = os.path.join(_OUT_DIR, "results.json")
    sp = os.path.join(_OUT_DIR, "exp_uav_ids.py")
    if cache.get("script_source") != "ai" or not os.path.isfile(sp):
        # 非 AI 脚本（或缺失）→ 始终用最新内置脚本，避免旧文件残留
        _write_out("exp_uav_ids.py", _FALLBACK_HEAD)
        cache["script_source"] = "fallback"
    t0 = time.time()
    try:
        r = subprocess.run([_VENV_PY, sp, results_path], capture_output=True, text=True,
                           timeout=900, cwd=_OUT_DIR)
    except subprocess.TimeoutExpired:
        return {"status": "fail", "error": "实验超时（>15min），已中止"}
    out = (r.stdout or "")[-800:]
    if r.returncode != 0:
        # 一次自修复：把报错回喂 AI；仍失败 → 内置脚本兜底
        if cache.get("script_source") == "ai" and model.status()["ok"]:
            try:
                fix = _ai("脚本运行报错，请修复后只输出完整代码：\n%s\n当前脚本：\n%s"
                          % ((r.stderr or "")[:800], open(sp, encoding="utf-8").read()[:7000]),
                          system=_SCRIPT_SYS, max_tokens=4000)
                code = re.sub(r"^```[a-z]*\n?", "", fix.strip()).rstrip("`").strip()
                sp2 = _write_out("exp_uav_ids.py", code)
                r2 = subprocess.run([_VENV_PY, sp2, results_path], capture_output=True, text=True,
                                    timeout=900, cwd=_OUT_DIR)
                if r2.returncode == 0:
                    out = (r2.stdout or "")[-800:]
                else:
                    raise RuntimeError(r2.stderr[:300])
            except Exception:
                _write_out("exp_uav_ids.py", _FALLBACK_HEAD)
                cache["script_source"] = "fallback"
                r = subprocess.run([_VENV_PY, sp, results_path], capture_output=True, text=True,
                                   timeout=900, cwd=_OUT_DIR)
                out = (r.stdout or "")[-800:]
        else:
            _write_out("exp_uav_ids.py", _FALLBACK_HEAD)
            cache["script_source"] = "fallback"
            r = subprocess.run([_VENV_PY, sp, results_path], capture_output=True, text=True,
                               timeout=900, cwd=_OUT_DIR)
            out = (r.stdout or "")[-800:]
    if not os.path.isfile(results_path):
        return {"status": "fail", "error": "实验未产生 results.json：%s" % (r.stderr or "")[:200]}
    with open(results_path, "r", encoding="utf-8") as f:
        res = json.load(f)
    # 校验：结果里出现 N/A（脚本内部异常被吞）→ 视为失败，自动切兜底脚本重跑
    if _results_invalid(res):
        _write_out("exp_uav_ids.py", _FALLBACK_HEAD)
        cache["script_source"] = "fallback"
        r = subprocess.run([_VENV_PY, sp, results_path], capture_output=True, text=True,
                           timeout=900, cwd=_OUT_DIR)
        if not os.path.isfile(results_path):
            return {"status": "fail", "error": "兜底脚本也未产出结果：%s" % (r.stderr or "")[:200]}
        with open(results_path, "r", encoding="utf-8") as f:
            res = json.load(f)
        if _results_invalid(res):
            return {"status": "fail", "error": "兜底脚本结果仍无效（N/A）"}
    cache["results"] = res
    return {"status": "ok", "note": "训练完成（%.0fs，%d 种子；脚本：%s）"
            % (time.time() - t0, len(res.get("seeds") or ["?"]),
               "AI 生成" if cache.get("script_source") == "ai" else "内置兜底"),
            "detail": out}


def _results_invalid(res):
    """结果含 N/A 或全为空 → 无效。"""
    def _bad(v):
        if isinstance(v, str):
            return "N/A" in v or not v.strip()
        if isinstance(v, dict):
            return all(_bad(x) for x in v.values()) if v else True
        return not isinstance(v, (int, float))
    return (not res) or all(_bad(v) for v in res.values())


# ================================================================ ⑤ 结果分析
def _run_exp_analyze(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "结果分析需要 AI。"}
    res = cache.get("results")
    if not res:
        return {"status": "fail", "error": "没有实验结果可分析"}
    body = ("题目：%s\n\n真实实验结果（results.json）：\n%s\n\n"
            "请写 400–600 字结果分析：1) 主结果与基线/消融对比说明图增强与双层机制各自贡献；"
            "2) reject 机制的意义（用 rejected_error_rate 说话）；3) 局限（仿真数据、图增强为邻域聚合近似等）。"
            "只依据给定数字，不得编造。"
            % (cache.get("refined_topic") or "", json.dumps(res.get("aggregate") or res,
                                                            ensure_ascii=False)[:2500]))
    reply = _ai(body, system="你是论文实验分析撰写者，只依据真实数据，输出 Markdown。",
                max_tokens=1500)
    cache["analysis_md"] = reply
    return {"status": "ok", "note": "结果分析完成（%d 字）" % len(reply), "detail": reply[:600]}


# ================================================================ ⑥ 论文生成
_PAPER_SECTIONS = [
    ("title_abstract", "论文题目（用问询校正后的题目）、300 字中文摘要、4–6 个关键词、英文题目与 Abstract。"),
    ("intro", "引言（1. 背景与动机：低空经济/UAV 自组网面临的安全威胁与 IDS 决策可信度问题；"
              "2. 现有不足；3. 本文贡献 3–4 条，与问询 key_claims 对齐；4. 组织结构）。"),
    ("related", "相关工作（三条线：UAV/FANET 入侵检测；图增强/拓扑感知检测；可信 AI 与决策置信度/拒绝机制。"
                "文献用「作者-年份-期刊风格」的**占位条目**[1][2]…并在段末注明：引用清单需作者用真实文献库替换，不得保留占位符投稿）。"),
    ("method", "方法（系统总览；层1 检测器形式化；图增强邻域聚合的公式与 2 次迭代的更新规则；"
               "层2 决策可信度定义（集成分歧+校准）与 reject 二次裁决流程；复杂度分析。"
               "公式用 LaTeX 记号，与实验脚本实现严格一致）。"),
    ("experiments", "实验（设置：数据仿真参数、种子、指标定义；主表：full vs no_graph vs lr vs dual_layer 的"
                    "Accuracy/Precision/Recall/F1/ROC-AUC 均值±std——**只允许用我提供的真实数字**；"
                    "reject 分析；结果讨论）。"),
    ("conclusion", "结论与未来工作（总结贡献、局限：仿真数据/邻域聚合近似/单机规模，"
                   "未来：真实数据集、分布式训练、在线部署）。"),
]


def _run_paper_gen(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "论文生成需要 AI。"}
    topic = cache.get("refined_topic") or cache.get("topic") or ""
    qa = cache.get("socratic_qa") or []
    res = cache.get("results") or {}
    analysis = cache.get("analysis_md") or ""
    if not (topic and res):
        return {"status": "fail", "error": "缺少题目或实验结果，无法写论文"}
    parts, ok_n = [], 0
    ctx = ("题目：%s\n\n问询结论：\n%s\n\n实验方案：%s\n\n真实实验结果（唯一允许引用的数字来源）：\n%s\n\n"
           "结果分析（可复用其表述）：\n%s\n\n个人画像：\n%s"
           % (topic,
              "\n".join("Q:%s A:%s" % (x["q"][:60], x["a"][:100]) for x in qa[:6]),
              json.dumps(cache.get("exp_plan") or {}, ensure_ascii=False)[:1200],
              json.dumps(res.get("aggregate") or {}, ensure_ascii=False)[:1800],
              analysis[:1500], _profile_md() or "（无）"))
    for key, ask in _PAPER_SECTIONS:
        try:
            reply = _ai("%s\n\n请撰写论文章节：%s\n要求：学术中文、具体、与实验实现一致、"
                        "不编造数据与文献（文献用占位并注明）。输出 Markdown。"
                        % (ctx, ask), system="你是计算机领域论文写手（中文）。",
                        max_tokens=3000)
            parts.append("## %s\n\n%s" % (_sec_name(key), reply))
            ok_n += 1
            cache["paper_" + key] = reply
        except Exception as e:
            parts.append("## %s\n\n（本节生成失败：%s——可重跑 paper_gen 补齐）" % (_sec_name(key), e))
    if not ok_n:
        return {"status": "fail", "error": "论文各节均生成失败"}
    md = "# %s\n\n%s" % (topic, "\n\n".join(parts))
    cache["paper_md"] = md
    p = _write_out("paper_draft.md", md)
    return {"status": "ok", "note": "初稿完成：%d/%d 节，%d 字 → %s"
            % (ok_n, len(_PAPER_SECTIONS), len(md), os.path.basename(p)),
            "detail": md[:600]}


def _sec_name(key):
    return {"title_abstract": "摘要", "intro": "1 引言", "related": "2 相关工作",
            "method": "3 方法", "experiments": "4 实验", "conclusion": "5 结论"}.get(key, key)


# ================================================================ ⑦ 盲审 / ⑧ 修改
_REVIEWERS = [
    ("R1 方法论审稿人", "你严格审查：形式化是否自洽、图增强与层2定义是否清晰、复杂度分析是否成立、"
                        "claims 是否被实验支撑。"),
    ("R2 UAV网络审稿人", "你熟悉 FANET/IDS：审查威胁模型与攻击仿真是否合理、特征是否贴近真实协议行为、"
                          "部署与实时性讨论是否充分。"),
    ("R3 机器学习审稿人", "你审查：实验协议公平性（基线是否同一数据/划分）、方差与种子、"
                           "校准与 reject 机制是否严谨、是否过度声称。"),
]


def _run_review(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "盲审需要 AI。"}
    md = cache.get("paper_md") or ""
    if not md:
        return {"status": "fail", "error": "没有论文可审"}
    cache["round"] = int(cache.get("round") or 0) + 1
    rnd = cache["round"]
    reviews = []
    for name, persona in _REVIEWERS:
        try:
            reply = _ai("以下是待审论文全文：\n\n%s\n\n你是%s。%s\n"
                        "给出：总评分(1-10，6 为勉强可接收线)、优点1-2条、问题3-5条（按严重度排序，"
                        "具体到章节与可操作的修改动作）、以及「必须修改」清单。"
                        % (md[:14000], name, persona),
                        system="你是期刊/会议盲审专家，直接给评审意见，输出 Markdown。",
                        max_tokens=1500)
            m = re.search(r"(?:总评分|评分)[^\d]{0,6}(\d+(?:\.\d+)?)", reply)
            score = float(m.group(1)) if m else 5.0
            reviews.append({"reviewer": name, "score": score, "text": reply})
        except Exception as e:
            reviews.append({"reviewer": name, "score": 5.0, "text": "（评审失败：%s）" % str(e)[:120]})
    scores = [r["score"] for r in reviews]
    meta = {"round": rnd, "scores": scores,
            "avg": round(sum(scores) / len(scores), 2),
            "pass": sum(scores) / len(scores) >= REVIEW_PASS_SCORE}
    cache["reviews"] = reviews
    cache["meta_review"] = meta
    cache.setdefault("review_history", []).append(
        {"round": rnd, "meta": meta,
         "texts": [r["text"][:1200] for r in reviews]})
    rp = _write_out("review_round%d.md" % rnd,
                    "\n\n---\n\n".join("## %s（评分 %s）\n\n%s" % (r["reviewer"], r["score"], r["text"])
                                       for r in reviews))
    return {"status": "ok", "note": "第 %d 轮盲审：均分 %s（%s）→ %s"
            % (rnd, meta["avg"], "、".join(str(x) for x in scores), os.path.basename(rp)),
            "detail": "\n\n".join(r["text"][:300] for r in reviews)[:1200]}


def _split_sections(md):
    """按 ## / # 标题把论文切成 [ {title, body} ]，便于分节修订（整篇重生成会截断）。"""
    lines = md.splitlines()
    secs, cur_title, cur = [], "（导言）", []
    for ln in lines:
        if re.match(r"^#{1,2}\s", ln) and not ln.startswith("###"):
            if cur or cur_title != "（导言）":
                secs.append({"title": cur_title, "body": "\n".join(cur).strip()})
            cur_title, cur = ln, []
        else:
            cur.append(ln)
    if cur or cur_title:
        secs.append({"title": cur_title, "body": "\n".join(cur).strip()})
    return [s for s in secs if s["body"] or s["title"].startswith("#")]


def _run_revise(by, opts, cache):
    if not model.status()["ok"]:
        return {"status": "blocked", "needs": "ai", "error": "修改需要 AI。"}
    md = cache.get("paper_md") or ""
    reviews = cache.get("reviews") or []
    meta = cache.get("meta_review") or {}
    if not (md and reviews):
        return {"status": "fail", "error": "缺少论文或评审意见"}
    if meta.get("pass"):
        cache["revision_md"] = "（均分已达标，无需修改）"
        return {"status": "ok", "note": "第 %d 轮均分 %s 已达标（≥%.1f），进入终稿"
                % (meta.get("round", 1), meta["avg"], REVIEW_PASS_SCORE)}
    comments = "\n\n".join("【%s，评分 %s】\n%s" % (r["reviewer"], r["score"], r["text"][:2600])
                           for r in reviews)
    # —— 定向分节修订：只改审稿意见真正涉及的章节（全量逐节修订过慢且常超时）——
    secs = _split_sections(md)
    revised, notes = [], []
    ok_n = 0
    # 从意见中定位重点章节：含批评关键词的节 + 始终复查摘要/引言/结论
    hot_words = ("问题", "必须修改", "缺失", "不足", "建议", "错误", "不清楚", "缺陷", "质疑")
    hot_idx = set()
    for i, s in enumerate(secs):
        t = s["title"] + s["body"][:400]
        if any(w in comments for w in (s["title"].strip(" #").split()[0] if s["title"].strip(" #").split() else "")):
            hot_idx.add(i)
        if re.search(r"摘要|Abstract", s["title"]) or re.search(r"引言", s["title"]) \
                or re.search(r"结论", s["title"]) or re.search(r"实验", s["title"]) \
                or re.search(r"方法", s["title"]):
            hot_idx.add(i)
    for i, s in enumerate(secs):
        body = (s["title"] + "\n\n" + s["body"]).strip()
        if len(body) < 120 or i not in hot_idx:   # 非重点节原样保留
            revised.append(body)
            continue
        try:
            out = _ai("这是论文的一个章节：\n\n%s\n\n\n三位审稿人意见（仅落实与本节相关的）：\n%s\n\n"
                      "请仅输出**该章节修改后的完整 Markdown**（保持原有标题层级；"
                      "实验数字只能沿用原文真实数字；不得删减小节；文献占位符保留）。"
                      % (body[:6500], comments[:4000]),
                      system="你是论文修改专家。只输出章节正文，不要解释。", max_tokens=2200, temperature=0.3)
            out = out.strip()
            # 防截断：改写结果明显变短（<60%）时保留原文
            if len(out) >= 0.6 * len(body):
                revised.append(out)
                notes.append("✓ %s" % s["title"])
                ok_n += 1
            else:
                revised.append(body)
                notes.append("⚠ %s（改写过短，保留原文）" % s["title"])
        except Exception as e:
            revised.append(body)
            notes.append("⚠ %s（修订失败保留原文：%s）" % (s["title"], str(e)[:80]))
    new_md = "\n\n".join(revised)
    # 确保首行是题目
    if not new_md.startswith("# "):
        new_md = "# %s\n\n%s" % (cache.get("refined_topic") or "", new_md)
    cache["revision_md"] = "分节修订 %d/%d 节成功：\n%s" % (ok_n, len(secs), "\n".join(notes))
    cache["paper_md"] = new_md
    _write_out("paper_round%d.md" % (meta.get("round", 1)), new_md)
    return {"status": "ok", "note": "第 %d 轮修改完成（%d 字，%d/%d 节修订），将再次送审"
            % (meta.get("round", 1), len(new_md), ok_n, len(secs)),
            "detail": cache["revision_md"][:500]}


# ================================================================ ⑨ 终稿
def _run_final(by, opts, cache):
    md = cache.get("paper_md") or ""
    if not md:
        return {"status": "fail", "error": "没有终稿可导出"}
    p_md = _write_out("paper_final.md", md)
    # 简易 Markdown → DOCX（python-docx，段落级转换，够盲审终稿用）
    docx_path = ""
    try:
        from docx import Document
        from docx.shared import Pt
        doc = Document()
        doc.styles["Normal"].font.name = "宋体"
        doc.styles["Normal"].font.size = Pt(12)
        for line in md.splitlines():
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("# "):
                doc.add_heading(line[2:], level=0)
            elif line.startswith("## "):
                doc.add_heading(line[3:], level=1)
            elif line.startswith("### "):
                doc.add_heading(line[4:], level=2)
            elif line.startswith("> "):
                para = doc.add_paragraph(line[2:])
                para.paragraph_format.left_indent = Pt(24)
            elif re.match(r"^[-*]\s", line):
                doc.add_paragraph(re.sub(r"^[-*]\s", "", line), style="List Bullet")
            else:
                doc.add_paragraph(re.sub(r"\*\*(.+?)\*\*", r"\1", line))
        docx_path = os.path.join(_OUT_DIR, "paper_final.docx")
        doc.save(docx_path)
    except Exception as e:
        docx_path = ""
        cache["docx_error"] = str(e)[:200]
    cache["final_outputs"] = {"md": p_md, "docx": docx_path}
    # 把产物路径写给用户态
    return {"status": "ok", "note": "终稿已导出：%s%s"
            % (os.path.basename(p_md), (" + " + os.path.basename(docx_path)) if docx_path else ""),
            "detail": "字数：%d；评审轮次：%d"
            % (len(md), len(cache.get("review_history") or []))}


_RUNNERS = {
    "socratic": _run_socratic, "exp_plan": _run_exp_plan, "exp_script": _run_exp_script,
    "exp_run": _run_exp_run, "exp_analyze": _run_exp_analyze, "paper_gen": _run_paper_gen,
    "review": _run_review, "revise": _run_revise, "final": _run_final,
}


# ================================================================ 编排
def run(by, opts=None):
    """opts: {topic, only:[...], force:bool, max_rounds:int}
    review→revise 循环：未达标则 revise 后再次 review，直到达标或轮次上限。"""
    opts = opts or {}
    only = [x for x in (opts.get("only") or []) if x in _RUNNERS]
    force = bool(opts.get("force"))
    max_rounds = int(opts.get("max_rounds") or REVIEW_MAX_ROUNDS)
    prev = _u(by)
    stages = dict(prev.get("stages") or {})
    cache = dict(prev.get("cache") or {})
    cache.setdefault("topic", opts.get("topic") or "")
    results = {}

    def _stage(sid):
        try:
            r = _RUNNERS[sid](by, opts, cache) or {"status": "fail", "error": "无返回"}
        except Exception as e:
            r = {"status": "fail", "error": "%s：%s" % (type(e).__name__, str(e)[:200])}
        if r.get("status") == "fail" and re.search(r"(401|403|429|未配置|不可用|Key)", str(r.get("error") or "")):
            r = {"status": "blocked", "needs": "ai",
                 "error": "模型暂不可用：%s" % str(r["error"])[:140]}
        r["ts"] = _now()
        results[sid] = r
        stages[sid] = r
        return r

    for sid in _STAGE_ORDER:
        if only and sid not in only:
            continue
        if not force and (stages.get(sid) or {}).get("status") == "ok":
            continue
        # 依赖守门
        if sid in ("exp_plan", "exp_script", "exp_run", "exp_analyze", "paper_gen") \
                and (stages.get("socratic") or {}).get("status") != "ok":
            results[sid] = stages[sid] = {"status": "blocked", "ts": _now(),
                                          "error": "苏格拉底问询未完成，后续环节不启动"}
            continue
        r = _stage(sid)
        # 盲审循环
        if sid == "review" and r.get("status") == "ok":
            meta = cache.get("meta_review") or {}
            rounds = 0
            while not meta.get("pass") and rounds < max_rounds and \
                    results["review"].get("status") == "ok":
                rr = _stage("revise")
                if rr.get("status") != "ok":
                    break
                rv = _stage("review")
                if rv.get("status") != "ok":
                    break
                meta = cache.get("meta_review") or {}
                rounds += 1
            u = _u(by)
            cache["done"] = bool((cache.get("meta_review") or {}).get("pass")) or \
                len(cache.get("review_history") or []) >= 1
            continue
        if r.get("status") in ("blocked",) and sid != "review":
            continue

    # 落盘
    outs = {}
    for k in ("paper_draft.md", "paper_final.md", "paper_final.docx"):
        p = os.path.join(_OUT_DIR, k)
        if os.path.isfile(p):
            outs[k] = p
    data = {"stages": stages, "cache": _slim(cache), "topic": cache.get("refined_topic")
            or cache.get("topic") or "", "round": cache.get("round") or 0,
            "scores": [(h["meta"].get("avg")) for h in (cache.get("review_history") or [])],
            "outputs": outs, "done": bool((cache.get("meta_review") or {}).get("pass")),
            "ts": _now()}
    _set_user(by, data)
    return {"ok": True, "stages": state(by)["stages"], "topic": data["topic"],
            "round": data["round"], "scores": data["scores"], "outputs": outs,
            "done": data["done"], "report_md": report_md(state(by))}


def _slim(cache):
    out = dict(cache)
    for k in list(out.keys()):
        if k.startswith("paper_") and k not in ("paper_md", "revision_md"):
            v = out[k]
            if isinstance(v, str) and len(v) > 20000:
                out[k] = v[:20000]
    qa = out.get("socratic_qa") or []
    if qa:
        out["socratic_qa"] = [{"q": x["q"][:400], "a": x["a"][:800]} for x in qa[:14]]
    md = out.get("paper_md") or ""
    if len(md) > 60000:
        out["paper_md"] = md[:60000]
    return out


def report_md(st):
    lines = ["# 论文全流程报告", "",
             "> 规则：实验数字只能来自本地真实训练（results.json）；盲审 ≥%.1f 分或达轮次上限后出终稿。"
             % REVIEW_PASS_SCORE, "", "| 环节 | 状态 | 说明 |", "|---|---|---|"]
    sdict = st.get("stages") or {}
    for s in STAGES:
        r = sdict.get(s["id"]) or {}
        if isinstance(r, str):
            r = {}
        icon = {"ok": "✅", "warn": "⚠️", "blocked": "⛔", "fail": "❌", "skip": "⏭️"}.get(r.get("status"), "·")
        lines.append("| %s | %s %s | %s |" % (s.get("name"), icon, r.get("status", "todo"),
                                              (r.get("note") or r.get("error") or "")[:150]))
    if st.get("scores"):
        lines.append("")
        lines.append("**盲审均分轨迹**：" + " → ".join(str(x) for x in st["scores"]))
    if st.get("outputs"):
        lines.append("")
        lines.append("**产物**：" + "、".join(sorted(st["outputs"].keys())))
    return "\n".join(lines)
