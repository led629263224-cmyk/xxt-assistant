"""路径常量与配置读写。

配置分两处存放：
    config.json               普通设置（刷新间隔、提醒阈值、窗口位置主题……）
    data/credentials.bin      账号密码，DPAPI 加密后单独放，避免和设置混在一起
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import crypto


def _base_dir() -> Path:
    """程序目录：源码运行时是项目根，打包成 exe 后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
CREDENTIAL_PATH = DATA_DIR / "credentials.bin"
COOKIE_PATH = DATA_DIR / "session_cookies.json"
DB_PATH = DATA_DIR / "xxt.db"
LOG_PATH = DATA_DIR / "app.log"

DEFAULT_CONFIG: dict[str, Any] = {
    "sync": {
        # 学习通有风控：默认 30 分钟一次，且请求之间限速（见 client._throttle）
        "interval_minutes": 30,
        "jitter_seconds": 90,
        "horizon_days": 120,
        "detail_refresh_hours": 6,
        "request_delay": 0.7,
    },
    "remind": {
        "enabled": True,
        "threshold_hours": [24, 6, 1],
        "new_homework": True,
        "new_exam": True,
        "new_notice": False,
        "exam_lead_hours": [24, 1],
        "sound": True,
        # 深夜免打扰：23 点至次日 7 点只累积不弹窗（紧急的过期作业除外）
        "quiet_hours": [23, 7],
    },
    "widget": {
        "x": None,
        "y": None,
        "theme": "light",
        "always_on_top": False,   # 默认不置顶：启动时不压住用户正在用的窗口
        "opacity": 0.97,
        "show_notices": True,
        "notice_count": 3,
    },
    "notices": {
        "enabled": True,
        "limit": 30,
    },
    "login": {
        "phone": "",
    },
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load(refresh: bool = False) -> dict[str, Any]:
    """读取配置（带内存缓存）。文件不存在或损坏时退回默认值。"""
    global _cache
    with _lock:
        if _cache is not None and not refresh:
            return _cache
        raw: dict = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        _cache = _deep_merge(DEFAULT_CONFIG, raw)
        return _cache


def save(cfg: dict[str, Any] | None = None) -> None:
    """写回配置（原子写：先写临时文件再替换，避免断电写坏）。"""
    global _cache
    with _lock:
        data = cfg if cfg is not None else _cache or DEFAULT_CONFIG
        _cache = data
        ensure_dirs()
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)


def update(section: str, **values) -> dict[str, Any]:
    """局部更新某一节配置并落盘，返回最新配置。"""
    cfg = copy.deepcopy(load(refresh=True))
    cfg.setdefault(section, {}).update(values)
    save(cfg)
    return cfg


# ------------------------------------------------------------------ 凭据

def save_credentials(phone: str, password: str) -> None:
    """DPAPI 加密后保存账号密码。"""
    payload = json.dumps({"phone": phone, "password": password},
                         ensure_ascii=False)
    token = crypto.protect(payload)
    ensure_dirs()
    tmp = CREDENTIAL_PATH.with_suffix(".bin.tmp")
    tmp.write_bytes(token.encode("ascii"))
    os.replace(tmp, CREDENTIAL_PATH)
    update("login", phone=phone)


def load_credentials() -> tuple[str, str] | None:
    """读取账号密码；没有或解不开都返回 None。"""
    if not CREDENTIAL_PATH.exists():
        return None
    try:
        token = CREDENTIAL_PATH.read_text(encoding="ascii").strip()
        payload = json.loads(crypto.unprotect(token))
        phone, password = payload.get("phone"), payload.get("password")
        if phone and password:
            return phone, password
    except Exception:
        return None
    return None


def has_credentials() -> bool:
    return CREDENTIAL_PATH.exists()


def clear_credentials() -> None:
    CREDENTIAL_PATH.unlink(missing_ok=True)
