"""程序入口。

用法：
    python -m xxt.main                 正常启动（托盘常驻 + 桌面悬浮清单）
    python -m xxt.main --tray          启动后只留托盘，不弹窗口
    python -m xxt.main --sync-once     不开界面，同步一次并打印结果（排错用）
    python -m xxt.main --dump worklist --course 0
                                       把原始页面存到 data/dump/（页面改版时排错用）
    python -m xxt.main --reset         清除本机保存的 cookie 与账号密码
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

from . import APP_NAME, __version__, config
from .client import ChaoxingClient
from .notify import Notifier
from .store import Store
from .sync import SyncService

log = logging.getLogger("xxt")


# ------------------------------------------------------------------ 日志

def setup_logging(verbose: bool = False) -> None:
    config.ensure_dirs()
    # 首次运行时把默认配置落盘（config.json 在程序目录下），方便用户直接改
    if not config.CONFIG_PATH.exists():
        try:
            config.save()
        except OSError:
            pass
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    # 打包成 -w（无控制台）后 sys.stdout/stderr 是 None，写流会炸，只有源码运行才有控制台
    if sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    # 日志文件任何情况下都要写：打包后没有控制台，出问题只能靠它排查
    try:
        file_handler = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass


# ------------------------------------------------------------------ 无界面命令

def cmd_sync_once(args) -> int:
    store = Store()
    client = ChaoxingClient(request_delay=config.load()["sync"]["request_delay"])
    notifier = Notifier(store, toast=lambda t, b, u: log.info("【提醒】%s：%s", t, b))
    service = SyncService(store, client, notifier)
    report = service.sync_once(refresh_details=not args.no_details)
    print("=" * 60)
    print(report.summary())
    if report.new_assignments:
        print("\n新增作业：")
        for row in report.new_assignments:
            print(f"  · [{row['course']}] {row['title']}　{row.get('status') or ''}")
    if report.new_exams:
        print("\n新增考试：")
        for row in report.new_exams:
            print(f"  · {row['name']}　{row.get('exam_time') or ''}")
    if report.new_notices:
        print(f"\n新通知 {len(report.new_notices)} 条")
    pending = store.pending_assignments()
    print(f"\n当前未完成作业 {len(pending)} 项：")
    for row in pending[:20]:
        print(f"  · [{row['course']}] {row['title']}　截止 {row.get('deadline') or '未知'}"
              f"　状态 {row.get('status') or '未知'}")
    exams = store.unfinished_exams()
    print(f"\n未完成考试 {len(exams)} 场：")
    for row in exams[:20]:
        print(f"  · {row['name']}　{row.get('exam_time') or ''}　{row.get('answer_status') or ''}")
    if report.errors:
        print("\n错误：")
        for err in report.errors:
            print("  ! " + err)
    store.close()
    return 0 if report.ok else 1


def cmd_dump(args) -> int:
    store = Store()
    client = ChaoxingClient(request_delay=config.load()["sync"]["request_delay"])
    course = None
    if args.dump in ("coursepage", "worklist"):
        courses = client.fetch_courses()
        index = args.course or 0
        if not courses:
            print("没有获取到课程")
            return 1
        if index >= len(courses):
            print(f"课程序号超出范围，共 {len(courses)} 门：")
            for i, c in enumerate(courses):
                print(f"  {i}. {c['name']}")
            return 1
        course = courses[index]
        print(f"使用课程：{course['name']}")
    path = client.dump(args.dump, course)
    print("已保存原始响应到：" + path)
    store.close()
    return 0


def cmd_reset(args) -> int:
    config.clear_credentials()
    if config.COOKIE_PATH.exists():
        config.COOKIE_PATH.unlink()
    print("已清除本机保存的账号密码与登录 cookie。")
    return 0


# ------------------------------------------------------------------ 图形界面

def run_gui(args) -> int:
    try:
        from PySide6.QtCore import QObject, QSharedMemory, Signal
        from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
    except ImportError:
        print("未安装 PySide6，无法启动界面。请先运行：\n"
              "  .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 2

    from .ui import style
    from .ui.dialogs import LoginDialog, SettingsDialog
    from .ui.tray import TrayIcon, make_icon
    from .ui.widget import TaskCard

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())          # 对话框、任务栏都用同一个图标
    # 固定用 Fusion + 自己的调色板：本机若开着系统深色模式，默认调色板会让
    # 原生绘制的部件（微调框箭头、消息框、下拉列表）变深，跟样式表打架。
    app.setStyle("Fusion")
    app.setPalette(style.build_palette(config.load().get("widget", {})
                                       .get("theme", "light")))

    # 单实例：已经有一个在跑就退出，避免两个悬浮窗
    guard = QSharedMemory("xxt-assistant-single-instance")
    if not guard.create(1):
        QMessageBox.information(None, APP_NAME, "学习通小助手已经在运行了（看右下角托盘）。")
        return 0

    class Bridge(QObject):
        report = Signal(object)
        syncing = Signal(bool)
        toast = Signal(str, str, bool)

    class Assistant:
        def __init__(self):
            self.bridge = Bridge()
            self.store = Store()
            self.client = ChaoxingClient(
                request_delay=config.load()["sync"]["request_delay"])
            self.notifier = Notifier(self.store,
                                     toast=lambda t, b, u: self.bridge.toast.emit(t, b, u))
            self.sync = SyncService(self.store, self.client, self.notifier,
                                    on_report=self.bridge.report.emit)
            self.card = TaskCard(self.store, self.sync)
            self.tray = TrayIcon(self.card, self.refresh_now, self.open_settings,
                                 self.quit, on_login=self.login)
            self.bridge.report.connect(self.apply_report)
            self.bridge.syncing.connect(self.card.set_syncing)
            self.bridge.toast.connect(self.tray.toast)

        # ---------------- 生命周期
        def start(self, start_hidden: bool) -> None:
            self.tray.show()
            self.card.set_status("正在启动…")
            if not start_hidden:
                self.card.show_card()
            # 首次启动不主动弹登录框：只提示，由用户点卡片上的「去登录」
            # （或托盘菜单的「登录 / 重新登录…」）再弹
            if not config.has_credentials():
                self.card.set_status("尚未登录，点右下角「去登录」", "warn")
            self.sync.start()

        def quit(self) -> None:
            log.info("退出中…")
            self.sync.stop()
            self.card.save_position()
            try:
                self.store.close()
            except Exception:
                pass
            self.tray.hide()
            app.quit()

        # ---------------- 槽函数
        def apply_report(self, report) -> None:
            self.card.set_syncing(False)
            self.card.show_report(report)
            if not report.ok and "未登录" in " ".join(report.errors):
                if config.has_credentials():
                    self.card.set_status("登录已失效，请重新登录", "error")
                else:
                    self.card.set_status("尚未登录，点右下角「去登录」", "warn")

        def refresh_now(self) -> None:
            if self.sync.busy:
                return
            self.bridge.syncing.emit(True)
            threading.Thread(target=self.sync.sync_once, daemon=True,
                             name="xxt-manual-sync").start()

        def login(self) -> None:
            dialog = LoginDialog(
                lambda phone, pwd, remember: self.sync.login(phone, pwd, remember),
                self.card)
            if dialog.exec():
                self.card.set_need_login(False)
                self.card.set_status("登录成功，开始同步…", "ok")
                self.refresh_now()

        def open_settings(self) -> None:
            dialog = SettingsDialog(self.card, store=self.store)
            if dialog.exec():
                dialog.save()
                self.card.apply_config()
                self.card.show_card()

    assistant = Assistant()
    assistant.card.refreshRequested.connect(assistant.refresh_now)
    assistant.card.loginRequested.connect(assistant.login)
    assistant.card.settingsRequested.connect(assistant.open_settings)

    tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
    if not tray_ok:
        log.warning("系统托盘不可用，窗口关闭后将无法唤起")

    assistant.start(start_hidden=args.tray)
    return app.exec()


# ------------------------------------------------------------------ 入口

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xxt.main", description=APP_NAME)
    parser.add_argument("--tray", action="store_true", help="启动后只留托盘图标")
    parser.add_argument("--sync-once", action="store_true", help="不开界面，同步一次")
    parser.add_argument("--no-details", action="store_true",
                        help="配合 --sync-once：跳过作业详情页（更快）")
    parser.add_argument("--dump", choices=["courses", "coursepage", "worklist",
                                           "exams", "notices"],
                        help="保存原始响应到 data/dump/")
    parser.add_argument("--course", type=int, default=0, help="配合 --dump 的课程序号")
    parser.add_argument("--reset", action="store_true", help="清除保存的凭据与 cookie")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    log.info("%s v%s 启动", APP_NAME, __version__)

    if args.reset:
        return cmd_reset(args)
    if args.dump:
        return cmd_dump(args)
    if args.sync_once:
        return cmd_sync_once(args)
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
