"""路径常量与配置读写。

配置分两处存放：
    config.json               普通设置（刷新间隔、提醒阈值、窗口位置主题……）
    data/credentials.bin      Windows 账号密码，DPAPI 加密后单独放
    macOS 账号密码存放在系统钥匙串中，不随数据目录迁移
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from . import crypto
from . import BUNDLE_ID


def _base_dir() -> Path:
    """默认配置目录；macOS 的应用包只存资源，不写入运行数据。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "xxt-assistant"
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
STORAGE_PATH = BASE_DIR / "storage.json"
try:
    _location = json.loads(STORAGE_PATH.read_text(encoding="utf-8"))
    DATA_DIR = Path(_location["data_dir"]).resolve()
    CONFIG_PATH = DATA_DIR / "config.json"
except (OSError, ValueError, KeyError, TypeError):
    pass
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
        "background_image": "",
        "background_overlay": 78,
    },
    "notices": {
        "enabled": True,
        "limit": 30,
    },
    "login": {
        "phone": "",
    },
    "storage": {"selected": False},
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

def credential_storage_name() -> str:
    return "macOS 钥匙串" if sys.platform == "darwin" else "Windows DPAPI"


def _mac_keychain():
    # 显式选用系统后端，避免自动发现到其他密码存储插件。
    from keyring.backends.macOS import Keyring
    return Keyring()


def save_credentials(phone: str, password: str) -> None:
    """Windows 用 DPAPI，macOS 用当前用户的登录钥匙串。"""
    payload = json.dumps({"phone": phone, "password": password},
                         ensure_ascii=False)
    if sys.platform == "darwin":
        _mac_keychain().set_password(BUNDLE_ID, "credentials", payload)
    else:
        token = crypto.protect(payload)
        ensure_dirs()
        tmp = CREDENTIAL_PATH.with_suffix(".bin.tmp")
        tmp.write_bytes(token.encode("ascii"))
        os.replace(tmp, CREDENTIAL_PATH)
    update("login", phone=phone)


def load_credentials() -> tuple[str, str] | None:
    """读取账号密码；没有或解不开都返回 None。"""
    try:
        if sys.platform == "darwin":
            value = _mac_keychain().get_password(BUNDLE_ID, "credentials")
            if value is None:
                return None
        else:
            if not CREDENTIAL_PATH.exists():
                return None
            token = CREDENTIAL_PATH.read_text(encoding="ascii").strip()
            value = crypto.unprotect(token)
        payload = json.loads(value)
        phone, password = payload.get("phone"), payload.get("password")
        if phone and password:
            return phone, password
    except Exception:
        return None
    return None


def has_credentials() -> bool:
    if sys.platform == "darwin":
        return load_credentials() is not None
    return CREDENTIAL_PATH.exists()


def clear_credentials() -> None:
    if sys.platform == "darwin":
        keychain = _mac_keychain()
        if keychain.get_password(BUNDLE_ID, "credentials") is not None:
            keychain.delete_password(BUNDLE_ID, "credentials")
    CREDENTIAL_PATH.unlink(missing_ok=True)


def _set_data_paths(directory: Path, config_path: Path | None = None) -> None:
    global DATA_DIR, CONFIG_PATH, CREDENTIAL_PATH, COOKIE_PATH, DB_PATH, LOG_PATH, _cache
    DATA_DIR = directory
    CONFIG_PATH = config_path or directory / "config.json"
    CREDENTIAL_PATH = directory / "credentials.bin"
    COOKIE_PATH = directory / "session_cookies.json"
    DB_PATH = directory / "xxt.db"
    LOG_PATH = directory / "app.log"
    _cache = None


def managed_files() -> list[Path]:
    """只枚举程序拥有的文件，不删除用户选择的目录或其中的其他文件。"""
    names = ("xxt.db", "xxt.db-wal", "xxt.db-shm", "session_cookies.json",
             "credentials.bin", "credentials.bin.tmp", "signatures.json", "signatures.tmp",
             "background.png", "background.tmp", "app.log", "crash.log", "launch-error.log")
    files = [CONFIG_PATH, CONFIG_PATH.with_suffix(".json.tmp")] + [DATA_DIR / name for name in names]
    for folder in (DATA_DIR / "icons", DATA_DIR / "dump"):
        if folder.is_symlink():
            raise OSError("数据子目录不能是符号链接")
        if folder.exists():
            for path in folder.rglob("*"):
                if path.is_symlink():
                    raise OSError("数据目录不能包含符号链接")
                if path.is_file():
                    files.append(path)
    for path in files:
        if path.is_symlink():
            raise OSError("数据文件不能是符号链接")
    return [path for path in files if path.is_file()]


def select_data_directory(directory: str | Path) -> None:
    """调用前必须关闭同步、数据库和日志。先复制成功再切换位置。"""
    target = Path(directory).expanduser().resolve()
    source = DATA_DIR.resolve()
    if target == source:
        update("storage", selected=True)
        return
    if source in target.parents or target in source.parents:
        raise OSError("请选择与当前数据目录互不包含的文件夹")
    target.mkdir(parents=True, exist_ok=True)
    if (target / "config.json").exists():
        raise OSError("所选文件夹中已有 config.json，请选择空文件夹")
    cfg = copy.deepcopy(load())
    copies = []
    for path in managed_files():
        if path in (CONFIG_PATH, CONFIG_PATH.with_suffix(".json.tmp")):
            relative = Path(path.name)
        else:
            relative = path.relative_to(DATA_DIR)
        destination = target / relative
        if destination.exists():
            raise OSError(f"所选文件夹中已有 {relative}，请选择空文件夹")
        copies.append((path, destination))
    created = []
    try:
        for path, destination in copies:
            destination.parent.mkdir(parents=True, exist_ok=True)
            created.append(destination)
            shutil.copy2(path, destination)
        if cfg["widget"].get("background_image") == str(source / "background.png"):
            cfg["widget"]["background_image"] = str(target / "background.png")
        cfg["storage"]["selected"] = True
        config_target = target / "config.json"
        config_target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if config_target not in created:
            created.append(config_target)
        pointer = STORAGE_PATH.with_suffix(".json.tmp")
        pointer.write_text(json.dumps({"data_dir": str(target)}, ensure_ascii=False), encoding="utf-8")
        os.replace(pointer, STORAGE_PATH)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    _set_data_paths(target)
    for path, _destination in copies:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # 新位置已提交，清理失败不能回滚到一半迁移的旧位置。
            pass


def clear_all_data() -> None:
    """关闭后台与文件句柄后清理本地数据，保留所选位置。"""
    files = managed_files()
    clear_credentials()
    for path in files:
        path.unlink(missing_ok=True)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["storage"]["selected"] = True
    save(cfg)
