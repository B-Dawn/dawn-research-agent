"""网络与文件系统公共工具。

仅使用标准库。所有对外请求带超时、重试与 User-Agent，
避免在受限网络下卡死整个检索流程。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "research-agent/1.0 (academic literature retrieval; +python-urllib)"


def _ensure_utf8():
    """Windows 控制台默认 GBK，中文输出会炸。强制切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def http_get(url, params=None, headers=None, timeout=25, retries=2, accept=None):
    """GET 请求，返回 bytes。失败返回 None，不抛异常。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    hdrs = {"User-Agent": UA}
    if accept:
        hdrs["Accept"] = accept
    if headers:
        hdrs.update(headers)

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 网络错误一律降级为 None
            last_err = exc
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
    print("[warn] 请求失败: %s (%s)" % (url[:90], last_err), file=sys.stderr)
    return None


def http_json(url, params=None, headers=None, timeout=25, retries=2):
    raw = http_get(url, params, headers, timeout, retries, accept="application/json")
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        print("[warn] JSON 解析失败: %s" % exc, file=sys.stderr)
        return None


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def write_text(path, text):
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_json(path, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_json(path):
    return json.loads(read_text(path))


def slugify(text, maxlen=60):
    """把查询串转成安全文件名，保留中英文字符。"""
    safe = []
    for ch in text.strip():
        if ch.isalnum() or ch in ("-", "_"):
            safe.append(ch)
        elif ch.isspace():
            safe.append("-")
    out = "".join(safe).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return (out[:maxlen] or "query")
