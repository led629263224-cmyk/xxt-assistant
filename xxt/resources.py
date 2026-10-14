"""图标与静态资源定位：源码运行和 PyInstaller 打包后都能找到 assets/。

打包时用 `--add-data "assets;assets"`，PyInstaller 会把它解到 sys._MEIPASS；
源码运行时就是项目根目录下的 assets/。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["resource_dir", "icon_path", "app_icon"]


def resource_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def icon_path() -> Path | None:
    """按优先级找一个能当图标用的文件，找不到就返回 None（调用方回退到画图标）。"""
    for name in ("icon-256.png", "icon.png", "icon.ico"):
        candidate = resource_dir() / name
        if candidate.exists():
            return candidate
    return None


def app_icon():
    """QIcon；没有资源文件或 Qt 不可用时返回空图标。"""
    try:
        from PySide6.QtGui import QIcon

        path = icon_path()
        return QIcon(str(path)) if path else QIcon()
    except Exception:
        return None
