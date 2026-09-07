#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账户与权限（零依赖）。

- 口令：PBKDF2-HMAC-SHA256 + 每用户随机盐（hashlib 标准库），不存明文。
- 会话：随机 token（os.urandom），存 data/sessions.json，7 天过期。
- 角色：admin（可管理账户/改模型配置） / member（普通成员）。
- 首次使用自动创建超级管理员 admin（初始口令由 DEFAULT_ADMIN 指定，登录后应改）。

公开的 API 由 web_app 决定哪些需要登录、哪些仅限 admin。
"""

import hashlib
import hmac
import json
import os
import time

_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
_USERS = os.path.join(_DATA_DIR, "users.json")
_SESSIONS = os.path.join(_DATA_DIR, "sessions.json")

DEFAULT_ADMIN = ("admin", "Aa123456")
SESSION_DAYS = 7
PBKDF2_ROUNDS = 60000


# ---------------------------------------------------------------- 基础 IO
def _load(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _users():
    return _load(_USERS, {"users": []})


def _save_users(obj):
    _save(_USERS, obj)


def _sessions():
    return _load(_SESSIONS, {"sessions": []})


def _save_sessions(obj):
    _save(_SESSIONS, obj)


# ---------------------------------------------------------------- 口令
def _hash(pwd, salt_hex):
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return dk.hex()


def _new_salt():
    return os.urandom(16).hex()


def verify(pwd, rec):
    return hmac.compare_digest(_hash(pwd, rec["salt"]), rec["hash"])


# ---------------------------------------------------------------- 初始化
def ensure_admin():
    """确保超级管理员存在（首次启动或 users.json 被删后重建）。"""
    obj = _users()
    if any(u.get("name") == DEFAULT_ADMIN[0] for u in obj.get("users", [])):
        return False
    obj.setdefault("users", []).append(_mk_user(DEFAULT_ADMIN[0], DEFAULT_ADMIN[1],
                                                role="admin", display="超级管理员"))
    _save_users(obj)
    return True


def _mk_user(name, pwd, role="member", display="", email=""):
    salt = _new_salt()
    return {"name": name, "display": display or name, "email": email,
            "role": role, "salt": salt, "hash": _hash(pwd, salt),
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "active": True}


def _public(u):
    return {"name": u.get("name", ""), "display": u.get("display", ""),
            "email": u.get("email", ""), "role": u.get("role", "member"),
            "active": u.get("active", True), "created": u.get("created", "")}


# ---------------------------------------------------------------- 注册 / 登录
def register(data):
    name = (data.get("name") or "").strip()
    pwd = data.get("password") or ""
    if len(name) < 2:
        return {"ok": False, "error": "用户名至少 2 个字符"}
    if len(pwd) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    obj = _users()
    if any(u["name"] == name for u in obj.get("users", [])):
        return {"ok": False, "error": "用户名已存在"}
    obj.setdefault("users", []).append(_mk_user(name, pwd, role="member",
                                                display=data.get("display") or "",
                                                email=data.get("email") or ""))
    _save_users(obj)
    return {"ok": True, "user": _public(obj["users"][-1])}


def login(data):
    name = (data.get("name") or "").strip()
    pwd = data.get("password") or ""
    obj = _users()
    for u in obj.get("users", []):
        if u["name"] == name:
            if not u.get("active", True):
                return {"ok": False, "error": "该账户已被停用，请联系管理员"}
            if not verify(pwd, u):
                return {"ok": False, "error": "用户名或密码错误"}
            token = os.urandom(24).hex()
            s = _sessions()
            s.setdefault("sessions", []).append({
                "token": token, "user": name,
                "created": time.strftime("%Y-%m-%d %H:%M"),
                "expire": time.time() + SESSION_DAYS * 86400})
            _save_sessions(s)
            return {"ok": True, "token": token, "user": _public(u)}
    return {"ok": False, "error": "用户名或密码错误"}


def logout(token):
    s = _sessions()
    s["sessions"] = [x for x in s.get("sessions", []) if x.get("token") != token]
    _save_sessions(s)
    return {"ok": True}


def user_by_token(token):
    """返回公开用户信息；无效/过期返回 None。"""
    if not token:
        return None
    s = _sessions()
    now = time.time()
    for x in s.get("sessions", []):
        if x.get("token") == token:
            if x.get("expire", 0) < now:
                return None
            obj = _users()
            for u in obj.get("users", []):
                if u["name"] == x.get("user"):
                    return _public(u)
            return None
    return None


def find(name):
    for u in _users().get("users", []):
        if u["name"] == name:
            return u
    return None


def self_update(data):
    """用户自助：改显示名/邮箱；改自己的密码需验证旧密码。"""
    name = (data.get("_name") or "").strip()
    obj = _users()
    for u in obj.get("users", []):
        if u["name"] != name:
            continue
        if not u.get("active", True):
            return {"ok": False, "error": "账户已停用"}
        new_pwd = data.get("new_password") or ""
        if new_pwd:
            if not verify(data.get("old_password") or "", u):
                return {"ok": False, "error": "旧密码不正确"}
            if len(new_pwd) < 6:
                return {"ok": False, "error": "新密码至少 6 位"}
            salt = _new_salt()
            u["salt"], u["hash"] = salt, _hash(new_pwd, salt)
        if data.get("display") is not None and str(data["display"]).strip():
            u["display"] = str(data["display"]).strip()[:40]
        if data.get("email") is not None:
            u["email"] = str(data["email"]).strip()[:80]
        _save_users(obj)
        return {"ok": True, "user": _public(u)}
    return {"ok": False, "error": "用户不存在"}


# ---------------------------------------------------------------- 管理（admin）
def list_users():
    return [_public(u) for u in _users().get("users", [])]


def _write_user(name, fn):
    obj = _users()
    for u in obj.get("users", []):
        if u["name"] == name:
            fn(u)
            _save_users(obj)
            return _public(u)
    return None


def update_user(data):
    """admin：改角色 / 启用停用 / 改显示名邮箱；也可重置密码。"""
    name = data.get("name")
    if not name:
        return {"ok": False, "error": "缺少用户名"}
    if name == DEFAULT_ADMIN[0] and data.get("role") and data["role"] != "admin":
        return {"ok": False, "error": "不能把超级管理员降级"}
    if name == DEFAULT_ADMIN[0] and data.get("active") is False:
        return {"ok": False, "error": "不能停用超级管理员"}

    def fn(u):
        if data.get("role") in ("admin", "member"):
            u["role"] = data["role"]
        if data.get("active") is not None:
            u["active"] = bool(data["active"])
        if data.get("display"):
            u["display"] = data["display"]
        if data.get("email") is not None:
            u["email"] = data["email"]
        if data.get("password"):
            u["salt"] = _new_salt()
            u["hash"] = _hash(data["password"], u["salt"])
            # 改密后踢掉该用户的所有会话
            s = _sessions()
            s["sessions"] = [x for x in s.get("sessions", []) if x.get("user") != name]
            _save_sessions(s)

    r = _write_user(name, fn)
    if not r:
        return {"ok": False, "error": "用户不存在: %s" % name}
    return {"ok": True, "user": r}


def delete_user(data):
    name = data.get("name")
    if name == DEFAULT_ADMIN[0]:
        return {"ok": False, "error": "不能删除超级管理员"}
    obj = _users()
    before = len(obj.get("users", []))
    obj["users"] = [u for u in obj.get("users", []) if u["name"] != name]
    if len(obj["users"]) == before:
        return {"ok": False, "error": "用户不存在: %s" % name}
    _save_users(obj)
    s = _sessions()
    s["sessions"] = [x for x in s.get("sessions", []) if x.get("user") != name]
    _save_sessions(s)
    return {"ok": True}


def stats():
    obj = _users()
    s = _sessions()
    now = time.time()
    live = [x for x in s.get("sessions", []) if x.get("expire", 0) >= now]
    return {"users": len(obj.get("users", [])), "sessions": len(live)}
