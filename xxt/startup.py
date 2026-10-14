"""开机自启：写 HKCU 的 Run 键（不需要管理员权限）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("xxt.startup")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "XxtAssistant"


def _command() -> str:
    """打包成 exe 时直接用 exe；源码运行时用 pythonw 跑项目根目录的 run.py。

    注意不能写成 `pythonw -m xxt.main`：开机自启的工作目录是 system32 之类，
    模块搜索路径里没有本项目，`-m xxt.main` 会直接 ImportError。跑 run.py 时
    Python 会把脚本所在目录放进 sys.path，所以 import xxt 一定成功。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    from . import config
    launcher = config.BASE_DIR / "run.py"
    return f'"{exe}" "{launcher}" --tray'


def is_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover
        log.info("读取开机自启失败：%s", exc)
        return False


def set_enabled(enabled: bool) -> bool:
    """返回是否操作成功。写入失败不抛异常，只返回 False。"""
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:  # pragma: no cover
        log.warning("设置开机自启失败：%s", exc)
        return False
