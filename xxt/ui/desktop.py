"""保持独立透明浮窗可绘制，显示桌面时将其放到桌面前方。"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtGui import QGuiApplication

log = logging.getLogger("xxt.ui")


class DesktopPresence(QObject):
    def __init__(self, card):
        super().__init__(card)
        self.card = card
        self.enabled = False
        self._configured_hwnd = None
        self._desktop_raised = False
        self._foreground_at_raise = None
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.check)
        self._api = None
        if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
            api = ctypes.WinDLL("user32", use_last_error=True)
            api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            api.GetClassNameW.restype = ctypes.c_int
            api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            api.GetAncestor.restype = wintypes.HWND
            api.GetForegroundWindow.argtypes = []
            api.GetForegroundWindow.restype = wintypes.HWND
            api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            api.GetWindowRect.restype = wintypes.BOOL
            api.WindowFromPoint.argtypes = [wintypes.POINT]
            api.WindowFromPoint.restype = wintypes.HWND
            api.IsIconic.argtypes = [wintypes.HWND]
            api.IsIconic.restype = wintypes.BOOL
            api.IsWindowVisible.argtypes = [wintypes.HWND]
            api.IsWindowVisible.restype = wintypes.BOOL
            api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            api.SetWindowPos.restype = wintypes.BOOL
            self._get_style = api.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else api.GetWindowLongW
            self._get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            self._get_style.restype = ctypes.c_ssize_t
            self._api = api

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        log.info("桌面卡片%s", "显示" if enabled else "主动收起到托盘")
        if enabled and self._api is not None:
            self._timer.start()
            self.check()
        else:
            self._timer.stop()

    def _class_name(self, hwnd) -> str:
        name = ctypes.create_unicode_buffer(256)
        self._api.GetClassNameW(hwnd, name, len(name))
        return name.value

    def check(self) -> None:
        api = self._api
        if not self.enabled or api is None:
            return
        hwnd = int(self.card.winId())
        if hwnd != self._configured_hwnd:
            try:
                dwm = ctypes.WinDLL("dwmapi")
                dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                     ctypes.c_void_p, wintypes.DWORD]
                value = wintypes.BOOL(True)
                dwm.DwmSetWindowAttribute(hwnd, 12, ctypes.byref(value), ctypes.sizeof(value))
            except OSError:
                log.debug("无法设置卡片的 Aero Peek 属性", exc_info=True)
            self._configured_hwnd = hwnd
        if api.IsIconic(hwnd) or not self.card.isVisible():
            self.card.showNormal()
        if not api.IsWindowVisible(hwnd):
            # 原生 HWND 可能被系统隐藏，但 Qt 仍认为可见；恢复时保留原层级。
            api.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                             0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0040 | 0x0200)
            self.card.update()
            log.info("已恢复被系统隐藏的桌面卡片")
        if self.card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint:
            self._desktop_raised = False
        elif self._desktop_raised and self._foreground_app_exposed(hwnd):
            foreground = api.GetAncestor(api.GetForegroundWindow(), 2)
            after = wintypes.HWND(-2) if self._get_style(foreground, -20) & 0x0008 else foreground
            api.SetWindowPos(hwnd, after, 0, 0, 0, 0,
                             0x0001 | 0x0002 | 0x0010 | 0x0200)
            self._desktop_raised = False
        bounds = wintypes.RECT()
        if not api.GetWindowRect(hwnd, ctypes.byref(bounds)):
            return
        point = wintypes.POINT((bounds.left + bounds.right) // 2,
                               (bounds.top + bounds.bottom) // 2)
        covering = api.GetAncestor(api.WindowFromPoint(point), 2)
        if self._class_name(covering) in ("Progman", "WorkerW"):
            # 显示桌面时 Explorer 可能处于 topmost 层，仅 HWND_TOP 无法越过它。
            # 临时进入该层，用户返回应用时再恢复普通层级，全程不抢焦点。
            api.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0,
                             0x0001 | 0x0002 | 0x0010 | 0x0040 | 0x0200)
            if not self.card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint:
                self._desktop_raised = True
                self._foreground_at_raise = api.GetAncestor(api.GetForegroundWindow(), 2)

    def _foreground_app_exposed(self, hwnd) -> bool:
        api = self._api
        foreground = api.GetAncestor(api.GetForegroundWindow(), 2)
        if not foreground or foreground == hwnd or api.IsIconic(foreground):
            return False
        if self._class_name(foreground) in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False
        if not api.IsWindowVisible(foreground):
            return False
        if foreground != self._foreground_at_raise:
            return True
        # Win+D 过渡中前台 HWND 有时仍指向旧应用；检查它是否真的重新露出。
        rect = wintypes.RECT()
        if not api.GetWindowRect(foreground, ctypes.byref(rect)):
            return False
        for x, y in ((rect.left + 16, rect.top + 16), (rect.right - 16, rect.top + 16),
                     (rect.left + 16, rect.bottom - 16), (rect.right - 16, rect.bottom - 16)):
            if api.GetAncestor(api.WindowFromPoint(wintypes.POINT(x, y)), 2) == foreground:
                return True
        return False
