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
import time
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
    # 以下为有免费额度/免费模型的 OpenAI 兼容端点，注册后领 Key 即可
    "siliconflow": {"label": "SiliconFlow（Qwen 免费模型）", "backend": "openai",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "Qwen/Qwen2.5-7B-Instruct"},
    "zhipu": {"label": "智谱 GLM（Flash 免费）", "backend": "openai",
              "base_url": "https://open.bigmodel.cn/api/paas/v4",
              "model": "glm-4-flash"},
    "modelscope": {"label": "魔搭 ModelScope（每日免费额度）", "backend": "openai",
                   "base_url": "https://api-inference.modelscope.cn/v1",
                   "model": "Qwen/Qwen2.5-7B-Instruct"},
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


def mark_ok(model_name=""):
    """记录一次成功调用（供界面显示"已验证可用"）。"""
    try:
        cur = load_cfg()
        cur["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if model_name:
            cur["last_ok_model"] = model_name
        cur["last_error"] = ""
        _write_cfg(cur)
    except Exception:
        pass
    return True


def mark_error(msg):
    try:
        cur = load_cfg()
        cur["last_error"] = "%s（%s）" % (msg, time.strftime("%Y-%m-%d %H:%M:%S"))
        _write_cfg(cur)
    except Exception:
        pass
    return True


def _write_cfg(cur):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_MODEL_FILE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)


def status():
    """给前端的模型可用性状态。"""
    cfg = load_cfg()
    preset = cfg.get("preset", "")

    def _base():
        return {"preset": preset, "backend_cfg": cfg.get("backend", ""),
                "base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
                "has_key": bool(cfg.get("api_key")),
                # 界面"当前已保存模型"指示所需字段
                "preset_label": PRESETS.get(preset, {}).get("label", preset or "自定义"),
                "key_tail": ("…" + cfg["api_key"][-4:]) if cfg.get("api_key") else "",
                "last_ok": cfg.get("last_ok", ""),
                "last_ok_model": cfg.get("last_ok_model", ""),
                "last_error": cfg.get("last_error", "")}

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
def _make_opener(use_proxy):
    """use_proxy=False 时构造"忽略一切代理配置"的直连 opener。

    必要性：本进程可能从带 HTTP_PROXY/HTTPS_PROXY 的会话里启动（如被其他工具拉起），
    urllib 默认会读环境变量与注册表代理；若本地代理（如 127.0.0.1:7877）未运行，
    就会报 WinError 10061 连接被拒，表现为"模型连不上"。
    """
    if use_proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url, payload, headers=None, timeout=_TIMEOUT):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    last_err = None
    # 先直连；直连不通且确实配置了代理时，再走代理重试（兼容需要代理访问境外端点的场景）
    for use_proxy in (False, True):
        try:
            with _make_opener(use_proxy).open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            if use_proxy or not urllib.request.getproxies():
                break
    raise last_err


def _friendly_http_error(code, raw):
    """把网关返回的错误体翻译成用户能看懂的中文提示。"""
    msg_zh, msg_en, rtype = "", "", ""
    try:
        o = json.loads(raw)
        err = o.get("error") or o
        msg_zh = err.get("message_zh") or ""
        msg_en = err.get("message") or ""
        rtype = err.get("type") or ""
        rid = err.get("request_id") or o.get("request_id") or ""
    except Exception:
        rid = ""
    if code == 429:
        base = "模型服务繁忙或已达容量上限（429 限流）"
    elif code == 401:
        base = "API Key 无效或未授权（401），请检查设置里的 Key"
    elif code == 403:
        base = "无权限访问该模型（403），可能 Key 未开通此模型"
    elif code == 404:
        base = "接口或模型不存在（404），请检查 Base URL 与模型名"
    elif code in (500, 502, 503, 504):
        base = "模型服务端故障（%s），稍后重试" % code
    else:
        base = "模型 API HTTP %s" % code
    extra = msg_zh or msg_en
    if extra:
        base += "：" + extra[:200]
    if rtype and "rate_limit" in rtype and "限流" not in base:
        base += "（限流）"
    if rid:
        base += "  [request_id: %s]" % rid
    return base


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
        try:
            out = _http_json(r["base_url"] + "/api/chat", payload)
            mark_ok(r.get("model") or "")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            raise RuntimeError("Ollama HTTP %s：%s（模型是否已 `ollama pull %s`？）"
                               % (e.code, detail or e.reason, r.get("model") or "llama3"))
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError("无法连接 Ollama（%s）：请确认已启动 `ollama serve`" % e)
        text = (out.get("message") or {}).get("content", "").strip()
        if not text and out.get("error"):
            raise RuntimeError("Ollama 返回错误：%s" % out["error"])
        return text
    # openai 兼容
    url = r["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {"Authorization": "Bearer " + (r.get("api_key") or "")}
    payload = {"model": r.get("model"), "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    # 429/503 属瞬时限流，自动退避重试（共 3 次尝试：0s / 2s / 6s 后）
    out, last = None, None
    for attempt, wait in enumerate((0, 2, 6)):
        if wait:
            time.sleep(wait)
        try:
            out = _http_json(url, payload, headers=headers)
            mark_ok(r.get("model") or "")
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                last = e
                continue
            raise
    if out is None:
        raise last
    try:
        ch = out.get("choices")
        if not ch:
            # 限流、余额不足、模型名错误等情况不会带 choices，直接暴露后端给的原因
            raise RuntimeError("模型返回异常：%s"
                               % (out.get("error", {}).get("message")
                                  or out.get("message") or json.dumps(out, ensure_ascii=False)[:200]))
        return ((ch[0].get("message") or {}).get("content") or "").strip()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:600]
        except Exception:
            pass
        raise RuntimeError(_friendly_http_error(e.code, detail or str(e.reason)))
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("无法连接模型服务：%s" % e)


def test(cfg=None):
    """测试连接：发一条极短消息。返回 {"ok": bool, "reply"?, "error"?}"""
    try:
        reply = chat([{"role": "user", "content": "请只回复：连接成功"}],
                     cfg=cfg, max_tokens=20, temperature=0)
        mark_ok((cfg or load_cfg()).get("model", ""))
        return {"ok": True, "reply": reply}
    except Exception as e:
        msg = str(e)
        mark_error(msg)
        return {"ok": False, "error": msg}


def quick_ask(question, system=None, cfg=None, max_tokens=900, temperature=None):
  msgs = []
  if system:
    msgs.append({"role": "system", "content": system})
  msgs.append({"role": "user", "content": question})
  kwargs = {"cfg": cfg, "max_tokens": max_tokens}
  if temperature is not None:
    kwargs["temperature"] = temperature
  return chat(msgs, **kwargs)
