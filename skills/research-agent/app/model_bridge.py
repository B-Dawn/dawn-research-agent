#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型桥接层（零依赖）。

让科研智能体可以"真正调用大模型做判断"，而不是只靠关键词规则。
支持两类后端：

1. OpenAI 兼容 API（通义/DeepSeek/智谱/Moonshot/SiliconFlow/本地 vLLM/中转 等）
   POST {base_url}/chat/completions, Authorization: Bearer {api_key}
   例：base_url = https://api.deepseek.com/v1, model = deepseek-chat
2. Ollama 本地模型（原生 /api/chat）

配置存于 data/model.json：
    {"backend": "auto|openai|ollama|none",
     "base_url": "", "api_key": "", "model": ""}

backend=auto 时：优先用已保存的 openai 配置；否则自动探测本机 Ollama/LM Studio；
都没有 → none（AI 功能禁用，但规则引擎照常工作）。

隐私说明：选择 openai 类远端后端会把问题文本（含画像资料）发送到该服务；
本地 ollama 则完全不出本机。调用方应在界面上向用户披露这一点。
"""

import json
import os
import urllib.request
import urllib.error

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
_MODEL_FILE = os.path.join(_DATA_DIR, "model.json")
_TIMEOUT = 90  # AI 判断允许长一点

# 预设后端：默认腾讯混元（OpenAI 兼容接口，需自备 API Key；新用户通常有免费额度）
PRESETS = {
    "hunyuan": {"label": "腾讯混元（默认 · 免费额度）", "backend": "openai",
                "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
                "model": "hunyuan-turbos-latest"},
    "deepseek": {"label": "DeepSeek", "backend": "openai",
                 "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "ollama": {"label": "本地 Ollama", "backend": "ollama",
               "base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b"},
    "custom": {"label": "自定义 OpenAI 兼容", "backend": "openai", "base_url": "", "model": ""},
}


# ---------------------------------------------------------------- 配置读写
def _default_cfg():
    # 默认即混元：base_url/模型预置好，用户只需在设置里填 API Key
    p = PRESETS["hunyuan"]
    return {"preset": "hunyuan", "backend": p["backend"],
            "base_url": p["base_url"], "api_key": "", "model": p["model"]}


def load_cfg():
    if os.path.isfile(_MODEL_FILE):
        try:
            with open(_MODEL_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            d = _default_cfg()
            d.update({k: v for k, v in cfg.items() if k in d})
            return d
        except Exception:
            pass
    return _default_cfg()


def apply_preset(name):
    """切到某个预设：返回该预设对应的 base_url/model（不落盘，由 save_cfg 落盘）。"""
    p = PRESETS.get(name)
    if not p:
        return {}
    return {"preset": name, "backend": p["backend"],
            "base_url": p["base_url"], "model": p["model"]}


def cfg_for_preset(name):
    """按预设派生一份临时配置（不落盘）：沿用已存的 api_key，切换 base_url/model/backend。
    用于对话中按需选择模型。name 无效或为空返回 None（用全局配置）。"""
    if not name or name not in PRESETS:
        return None
    cfg = load_cfg()
    cfg.update(apply_preset(name))
    return cfg


def save_cfg(cfg):
    os.makedirs(_DATA_DIR, exist_ok=True)
    cur = load_cfg()
    preset = (cfg.get("preset") or "").strip()
    if preset and preset in PRESETS:
        # 切预设时，未手填的字段用预设值补全（api_key 由用户自己填）
        cur.update(apply_preset(preset))
        if cfg.get("api_key"):
            cur["api_key"] = str(cfg["api_key"]).strip()
    for k in ("backend", "base_url", "api_key", "model", "preset"):
        if cfg.get(k) not in (None, ""):
            cur[k] = str(cfg[k]).strip()
    with open(_MODEL_FILE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    return cur


# ---------------------------------------------------------------- 探测
def _probe(url, timeout=1.5):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def probe_ollama():
    return _probe("http://127.0.0.1:11434/api/tags")


def probe_lmstudio():
    return _probe("http://127.0.0.1:1234/v1/models")


def resolve(cfg=None):
    """返回实际生效的后端：{"backend","base_url","api_key","model","reason"}"""
    cfg = cfg or load_cfg()
    backend = cfg.get("backend", "auto")
    if backend == "auto":
        if cfg.get("api_key") or cfg.get("base_url"):
            return {"backend": "openai", "base_url": cfg.get("base_url", ""),
                    "api_key": cfg.get("api_key", ""), "model": cfg.get("model", ""),
                    "reason": "使用已保存的 OpenAI 兼容配置"}
        if probe_ollama():
            return {"backend": "ollama", "base_url": "http://127.0.0.1:11434",
                    "api_key": "", "model": cfg.get("model") or "", "reason": "检测到本地 Ollama"}
        if probe_lmstudio():
            return {"backend": "openai", "base_url": "http://127.0.0.1:1234/v1",
                    "api_key": "", "model": cfg.get("model") or "", "reason": "检测到本地 LM Studio"}
        return {"backend": "none", "reason": "未配置 API 也未检测到本地模型"}
    return {"backend": backend, "base_url": cfg.get("base_url", ""),
            "api_key": cfg.get("api_key", ""), "model": cfg.get("model", ""),
            "reason": "手动指定 %s" % backend}


def status():
    """给前端的模型可用性状态。"""
    cfg = load_cfg()
    preset = cfg.get("preset", "")

    def _base():
        return {"preset": preset, "backend_cfg": cfg.get("backend", ""),
                "base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
                "has_key": bool(cfg.get("api_key"))}

    r = resolve(cfg)
    ok = r["backend"] in ("openai", "ollama")
    if not ok:
        d = _base()
        d.update({"ok": False, "backend": "none",
                  "reason": r.get("reason", "模型未配置") + "。去「设置 → 模型设置」选预设（默认腾讯混元）并填入 OpenAI 兼容 API 或安装 Ollama。",
                  "tips": ["openai 兼容：选预设后填 api_key + 模型名（base_url 自动填好）",
                           "本地：安装 Ollama 后运行 `ollama pull qwen2.5:7b`，切到「本地 Ollama」预设即可免 Key"]})
        return d
    if r["backend"] == "openai" and (not r.get("base_url") or not r.get("model")):
        d = _base()
        d.update({"ok": False, "backend": "openai",
                  "reason": "OpenAI 兼容配置不完整：需要 base_url + model（本地网关可不填 api_key）。",
                  "tips": []})
        return d
    if r["backend"] == "openai" and not r.get("api_key"):
        local = ("127.0.0.1" in (r.get("base_url") or "")) or ("localhost" in (r.get("base_url") or ""))
        if not local:
            d = _base()
            d.update({"ok": False, "backend": "openai",
                      "reason": "模型后端已就绪（%s），但还没填 API Key。去「设置 → 模型设置」粘贴 Key 后点测试连接。"
                                % PRESETS.get(preset, {}).get("label", r.get("base_url", "")),
                      "tips": ["腾讯混元：控制台 → API Key 管理 → 创建，新用户通常有免费额度",
                               "本地 Ollama：安装后 `ollama pull qwen2.5:7b`，切到「本地 Ollama」预设即可免 Key"]})
            return d
    d = _base()
    d.update({"ok": True, "backend": r["backend"], "model": r.get("model") or "（自动）",
              "reason": r.get("reason", "")})
    return d


# ---------------------------------------------------------------- 调用
def _http_json(url, payload, headers=None, timeout=_TIMEOUT):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(messages, cfg=None, max_tokens=1200, temperature=0.4):
    """调模型。messages: [{"role": "user"/"system"/"assistant", "content": str}]
    返回纯文本；出错抛异常（由调用方转成友好提示）。"""
    r = resolve(cfg)
    if r["backend"] == "none":
        raise RuntimeError("模型不可用：" + r.get("reason", ""))
    if r["backend"] == "ollama":
        payload = {"model": r.get("model") or "llama3", "messages": messages,
                   "stream": False, "options": {"temperature": temperature,
                                                "num_predict": max_tokens}}
        out = _http_json(r["base_url"] + "/api/chat", payload)
        return (out.get("message") or {}).get("content", "").strip()
    # openai 兼容
    url = r["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {"Authorization": "Bearer " + (r.get("api_key") or "")}
    payload = {"model": r.get("model"), "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    try:
        out = _http_json(url, payload, headers=headers)
        return (out["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise RuntimeError("模型 API HTTP %s：%s" % (e.code, detail or e.reason))
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("无法连接模型服务：%s" % e)


def test(cfg=None):
    """测试连接：发一条极短消息。返回 {"ok": bool, "reply"?, "error"?}"""
    try:
        reply = chat([{"role": "user", "content": "请只回复：连接成功"}],
                     cfg=cfg, max_tokens=20, temperature=0)
        return {"ok": True, "reply": reply}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def quick_ask(question, system=None, cfg=None, max_tokens=900):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": question})
    return chat(msgs, cfg=cfg, max_tokens=max_tokens)
