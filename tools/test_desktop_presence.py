"""Windows 常驻浮窗实测，使用临时数据和独立进程的普通应用窗口。

会短暂按 Win+D 显示桌面 / 恢复窗口，最后恢复原状态。无需登录或联网。
若 Windows 不允许测试窗口获得前台，会明确跳过原生交互验证。
运行：.venv/Scripts/python.exe tools/test_desktop_presence.py
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from xxt import config
from xxt.store import Store
from xxt.ui.widget import TaskCard
from xxt.ui.dialogs import SettingsDialog


def cover_app() -> int:
    app = QApplication([])
    window = QWidget()
    window.setWindowTitle("学习通小助手 · 独立应用测试窗口")
    window.showMaximized()
    window.raise_()
    window.activateWindow()
    print(int(window.winId()), flush=True)
    return app.exec()


def run() -> int:
    if sys.platform != "win32":
        print("SKIP Windows-only native desktop test")
        return 0
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix="xxt-desktop-") as directory:
        config._set_data_paths(Path(directory))
        config.save()
        store = Store()
        card = TaskCard(store, Mock(busy=False, next_run_at=None))
        card.show_card()
        card.move(80, 80)
        presence = card._desktop_presence
        api = presence._api
        if api is None:
            store.close()
            raise RuntimeError("需要使用 Windows Qt 平台运行此实测")
        api.SetForegroundWindow.argtypes = [wintypes.HWND]
        api.SetForegroundWindow.restype = wintypes.BOOL
        api.GetForegroundWindow.argtypes = []
        api.GetForegroundWindow.restype = wintypes.HWND
        api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        api.GetWindowThreadProcessId.restype = wintypes.DWORD
        api.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        api.AttachThreadInput.restype = wintypes.BOOL
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        api.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        api.keybd_event.restype = None
        api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        api.ShowWindow.restype = wintypes.BOOL
        cover = subprocess.Popen([sys.executable, "-u", __file__, "--cover"],
                                  stdout=subprocess.PIPE, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        failures = []
        skipped = []
        state = {}
        toggles = 0
        passes = 0

        def toggle():
            nonlocal toggles
            for key, flags in ((0x5B, 0), (0x44, 0), (0x44, 2), (0x5B, 2)):
                api.keybd_event(key, 0, flags, 0)
            toggles += 1

        def hit_at_card():
            rect = wintypes.RECT()
            assert api.GetWindowRect(int(card.winId()), ctypes.byref(rect))
            return api.WindowFromPoint(wintypes.POINT((rect.left + rect.right) // 2,
                                                      (rect.top + rect.bottom) // 2))

        def finish():
            try:
                if toggles % 2:
                    toggle()
            finally:
                card.hide_card()
                cover.terminate()
                cover.wait(timeout=5)
                app.quit()

        def stage(fn):
            def wrapped():
                try:
                    fn()
                except Exception as exc:
                    failures.append(f"{fn.__name__}: {exc}")
                    finish()
            return wrapped

        cover_hwnd = int(cover.stdout.readline().strip())

        def activate(hwnd):
            current = kernel.GetCurrentThreadId()
            active_thread = api.GetWindowThreadProcessId(api.GetForegroundWindow(), None)
            attached = current != active_thread and api.AttachThreadInput(current, active_thread, True)
            try:
                return bool(api.SetForegroundWindow(hwnd))
            finally:
                if attached:
                    api.AttachThreadInput(current, active_thread, False)

        @stage
        def from_other_app():
            state["focus_attempts"] = state.get("focus_attempts", 0) + 1
            activate(cover_hwnd)
            QTimer.singleShot(400, check_other_app)

        @stage
        def check_other_app():
            foreground = api.GetForegroundWindow()
            if foreground != cover_hwnd and state["focus_attempts"] < 3:
                QTimer.singleShot(200, from_other_app)
                return
            if foreground != cover_hwnd:
                skipped.append("Windows 前台锁阻止测试应用获得前台，未执行 Win+D 验证")
                finish()
                return
            state["focus_attempts"] = 0
            hit = hit_at_card()
            root = api.GetAncestor(hit, 2)
            assert root == foreground, ("卡片没有处于前台应用下方："
                f"hit={presence._class_name(hit)}, root={presence._class_name(root)}")
            assert api.GetAncestor(int(card.winId()), 2) == int(card.winId())
            print("PASS independent foreground application covers desktop card", flush=True)
            toggle()
            QTimer.singleShot(600, on_desktop)

        @stage
        def on_desktop():
            nonlocal passes
            hit = hit_at_card()
            foreground = api.GetForegroundWindow()
            assert hit == int(card.winId()), ("回到桌面后卡片未显示："
                f"hit={presence._class_name(hit)}, "
                f"foreground={presence._class_name(foreground)}, foreground_minimized={bool(api.IsIconic(foreground))}, "
                f"visible={card.isVisible()}, iconic={bool(api.IsIconic(int(card.winId())))}")
            assert not api.IsIconic(int(card.winId()))
            assert api.GetAncestor(int(card.winId()), 2) == int(card.winId())
            passes += 1
            print(f"PASS return from other application to desktop ({passes}/3)", flush=True)
            toggle()
            if passes < 3:
                QTimer.singleShot(500, from_other_app)
            else:
                QTimer.singleShot(500, settings_and_drag)

        @stage
        def settings_and_drag():
            before = card.mapToGlobal(QPoint(0, 0))
            card.move(card.pos() + QPoint(40, 20))
            assert card.mapToGlobal(QPoint(0, 0)) == before + QPoint(40, 20), "浮窗移动坐标错误"
            dialog = SettingsDialog(card, store=store)
            state["dialog"] = dialog
            dialog.show()
            state["dialog_activation_allowed"] = activate(int(dialog.winId()))
            QTimer.singleShot(400, settings_visible)

        @stage
        def settings_visible():
            dialog = state["dialog"]
            assert dialog.isVisible()
            assert api.GetAncestor(int(dialog.winId()), 2) == int(dialog.winId()), "设置被错误挂载到桌面层"
            if state["dialog_activation_allowed"]:
                assert api.GetForegroundWindow() == int(dialog.winId()), "设置未保持获得的前台状态"
            else:
                print("SKIP forced settings focus: Windows foreground lock", flush=True)
            dialog.reject()
            print("PASS desktop card movement and normal settings dialog", flush=True)
            config.update("widget", always_on_top=True)
            card.show_card()
            QTimer.singleShot(400, check_modes)

        @stage
        def check_modes():
            assert card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
            assert api.GetAncestor(int(card.winId()), 2) == int(card.winId())
            config.update("widget", always_on_top=False)
            card.show_card()
            assert api.GetAncestor(int(card.winId()), 2) == int(card.winId())
            print("PASS both display modes retain an independent renderable window", flush=True)
            api.ShowWindow(int(card.winId()), 0)
            QTimer.singleShot(500, recovered)

        @stage
        def recovered():
            assert api.IsWindowVisible(int(card.winId())), "系统隐藏原生窗口后未自动恢复"
            assert card.isVisible()
            print("PASS native-hidden desktop card automatically becomes visible", flush=True)
            card.hide_card()
            toggle()
            QTimer.singleShot(600, hidden)

        @stage
        def hidden():
            assert not card.isVisible()
            assert not presence.enabled
            print("PASS tray-hidden card stays hidden", flush=True)
            finish()

        QTimer.singleShot(700, from_other_app)
        QTimer.singleShot(20000, app.quit)
        try:
            app.exec()
            if passes != 3 and not skipped:
                failures.append(f"completed {passes}/3 desktop cycles")
        finally:
            if cover.poll() is None:
                finish()
            store.close()
        for failure in failures:
            print("FAIL", failure)
        for reason in skipped:
            print("SKIP", reason)
        return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(cover_app() if "--cover" in sys.argv else run())
