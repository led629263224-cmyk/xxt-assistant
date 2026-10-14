"""登录对话框与设置对话框。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import config, startup
from . import style

log = logging.getLogger("xxt.ui")


class LoginDialog(QDialog):
    """学号/手机号 + 密码登录。登录动作由外部注入，便于测试。"""

    def __init__(self, login_fn, parent=None):
        super().__init__(parent)
        self.login_fn = login_fn
        self.setWindowTitle("登录学习通")
        self.setModal(True)
        self.setFixedWidth(340)
        theme = config.load().get("widget", {}).get("theme", "light")
        self.setStyleSheet(style.stylesheet(theme))
        self.setPalette(style.build_palette(theme))

        root = QVBoxLayout(self)
        root.setSpacing(10)

        tip = QLabel("账号密码仅用 Windows DPAPI 加密后保存在本机，"
                     "不会上传到任何服务器。抓取全部为只读请求。")
        tip.setWordWrap(True)
        tip.setObjectName("Meta")
        root.addWidget(tip)

        form = QFormLayout()
        form.setSpacing(8)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("手机号 / 学号")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("密码")
        saved = config.load_credentials()
        if saved:
            self.phone.setText(saved[0])
        form.addRow("账号", self.phone)
        form.addRow("密码", self.password)
        root.addLayout(form)

        self.remember = QCheckBox("记住账号密码（DPAPI 加密）")
        self.remember.setChecked(True)
        root.addWidget(self.remember)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("Meta")
        root.addWidget(self.hint)

        buttons = QDialogButtonBox()
        self.ok_btn = buttons.addButton("登录", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_btn.setObjectName("Primary")
        cancel = buttons.addButton("稍后", QDialogButtonBox.ButtonRole.RejectRole)
        cancel.clicked.connect(self.reject)
        self.ok_btn.clicked.connect(self._do_login)
        root.addWidget(buttons)

    def _do_login(self) -> None:
        phone = self.phone.text().strip()
        password = self.password.text()
        if not phone or not password:
            self.hint.setText("账号和密码都要填哦")
            return
        self.ok_btn.setEnabled(False)
        self.hint.setText("正在登录…")
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QGuiApplication.processEvents()
        try:
            res = self.login_fn(phone, password, self.remember.isChecked())
        finally:
            QGuiApplication.restoreOverrideCursor()
            self.ok_btn.setEnabled(True)
        if res.ok:
            self.accept()
        else:
            self.hint.setText(res.msg)


class SettingsDialog(QDialog):
    """设置：同步 / 提醒 / 界面 / 维护 四个标签页。"""

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.store = store
        # 标题栏不写文字（四个标签页已经说明这是什么窗口）。
        # 注意：title 置空时 Windows 会退回显示可执行文件名（"python"），
        # 用一个空格才能真正空着。
        self.setWindowTitle(" ")
        self.setModal(True)
        self.setFixedWidth(430)
        cfg = config.load()
        theme = cfg.get("widget", {}).get("theme", "light")
        self.setStyleSheet(style.stylesheet(theme))
        self.setPalette(style.build_palette(theme))

        root = QVBoxLayout(self)
        root.setSpacing(10)

        tabs = QTabWidget()
        tabs.addTab(self._tab_sync(cfg), "同步")
        tabs.addTab(self._tab_remind(cfg), "提醒")
        tabs.addTab(self._tab_ui(cfg), "界面")
        tabs.addTab(self._tab_misc(), "维护")
        # 固定最小高度：切页时对话框不要跟着内容忽高忽低
        tabs.setMinimumHeight(360)
        self.tabs = tabs
        root.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("保存")
        ok_btn.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ---------------- 同步

    def _tab_sync(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(10)
        s = cfg.get("sync", {})

        self.interval = QSpinBox()
        self.interval.setRange(5, 720)
        self.interval.setSuffix(" 分钟")
        self.interval.setValue(int(s.get("interval_minutes", 30)))
        form.addRow("自动同步间隔", self.interval)

        self.delay = QDoubleSpinBox()
        self.delay.setRange(0.2, 5.0)
        self.delay.setSingleStep(0.1)
        self.delay.setSuffix(" 秒/请求")
        self.delay.setValue(float(s.get("request_delay", 0.7)))
        form.addRow("请求限速", self.delay)

        self.detail_hours = QSpinBox()
        self.detail_hours.setRange(1, 72)
        self.detail_hours.setSuffix(" 小时")
        self.detail_hours.setValue(int(s.get("detail_refresh_hours", 6)))
        form.addRow("作业详情刷新", self.detail_hours)

        tip = QLabel("间隔越短越及时，但请求越频繁。建议不低于 15 分钟。")
        tip.setObjectName("Meta")
        tip.setWordWrap(True)
        form.addRow(tip)
        form.addRow(self._stretch())
        return page

    # ---------------- 提醒

    def _tab_remind(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(10)
        r = cfg.get("remind", {})

        self.remind_enabled = QCheckBox("启用桌面提醒（右下角弹通知）")
        self.remind_enabled.setChecked(bool(r.get("enabled", True)))
        form.addRow(self.remind_enabled)

        self.thresholds = QLineEdit(",".join(str(x) for x in
                                             r.get("threshold_hours", [24, 6, 1])))
        self.thresholds.setPlaceholderText("例如 24,6,1")
        form.addRow("截止提醒档位(小时)", self.thresholds)

        self.exam_leads = QLineEdit(",".join(str(x) for x in
                                             r.get("exam_lead_hours", [24, 1])))
        self.exam_leads.setPlaceholderText("例如 24,1")
        form.addRow("考试提醒档位(小时)", self.exam_leads)

        quiet = QHBoxLayout()
        q0, q1 = (r.get("quiet_hours") or [23, 7])[:2]
        self.quiet_start = QSpinBox()
        self.quiet_start.setRange(0, 23)
        self.quiet_start.setValue(int(q0))
        self.quiet_end = QSpinBox()
        self.quiet_end.setRange(0, 23)
        self.quiet_end.setValue(int(q1))
        quiet.addWidget(self.quiet_start)
        quiet.addWidget(QLabel("点 至"))
        quiet.addWidget(self.quiet_end)
        quiet.addWidget(QLabel("点"))
        quiet.addStretch(1)
        form.addRow("免打扰时段", quiet)

        self.new_homework = QCheckBox("发现新作业时提醒")
        self.new_homework.setChecked(bool(r.get("new_homework", True)))
        self.new_exam = QCheckBox("发现新考试时提醒")
        self.new_exam.setChecked(bool(r.get("new_exam", True)))
        self.new_notice = QCheckBox("发现新通知时提醒（通知较多，默认关）")
        self.new_notice.setChecked(bool(r.get("new_notice", False)))
        form.addRow(self.new_homework)
        form.addRow(self.new_exam)
        form.addRow(self.new_notice)
        form.addRow(self._stretch())
        return page

    # ---------------- 界面

    def _tab_ui(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(10)
        w = cfg.get("widget", {})

        self.always_on_top = QCheckBox("窗口始终置顶")
        self.always_on_top.setChecked(bool(w.get("always_on_top", False)))
        self.dark = QCheckBox("深色主题")
        self.dark.setChecked(w.get("theme") == "dark")

        self.opacity = QSpinBox()
        self.opacity.setRange(60, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(int(round(float(w.get("opacity", 0.97)) * 100)))

        self.show_notices = QCheckBox("在列表底部显示未读通知")
        self.show_notices.setChecked(bool(w.get("show_notices", True)))
        self.autostart = QCheckBox("开机自动启动")
        self.autostart.setChecked(startup.is_enabled())

        for row in (self.always_on_top, self.dark):
            form.addRow(row)
        form.addRow("不透明度", self.opacity)
        for row in (self.show_notices, self.autostart):
            form.addRow(row)
        form.addRow(self._stretch())
        return page

    # ---------------- 维护

    def _tab_misc(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(12, 14, 12, 12)
        box.setSpacing(10)

        acc = QGroupBox("账号")
        acc_lay = QVBoxLayout(acc)
        acc_lay.setSpacing(8)
        creds = config.load_credentials()
        self.account_label = QLabel(f"{creds[0]}（已用 DPAPI 加密保存）" if creds
                                    else "尚未保存账号")
        self.account_label.setObjectName("Meta")
        acc_lay.addWidget(self.account_label)
        clear_btn = QPushButton("清除已保存的账号密码")
        clear_btn.clicked.connect(self._clear_credentials)
        acc_lay.addWidget(clear_btn)
        box.addWidget(acc)

        item_box = QGroupBox("任务清单")
        item_lay = QVBoxLayout(item_box)
        item_lay.setSpacing(8)
        self.hidden_label = QLabel("")
        self.hidden_label.setObjectName("Meta")
        item_lay.addWidget(self.hidden_label)
        self.restore_btn = QPushButton("恢复被移除的条目")
        self.restore_btn.clicked.connect(self._restore_hidden)
        item_lay.addWidget(self.restore_btn)
        open_btn = QPushButton("打开数据目录")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(config.DATA_DIR))))
        item_lay.addWidget(open_btn)
        box.addWidget(item_box)
        self._refresh_hidden_label()
        box.addStretch(1)
        return page

    def _stretch(self) -> QWidget:
        """占位：让表单内容顶部对齐，不要被纵向拉伸。"""
        holder = QWidget()
        holder.setFixedHeight(1)
        return holder

    # ---------------- 动作

    def _refresh_hidden_label(self) -> None:
        count = self.store.hidden_count() if self.store else 0
        if count:
            self.hidden_label.setText(f"已从列表移除 {count} 项（不影响学习通上的数据）")
        else:
            self.hidden_label.setText("没有已移除的条目")
        self.restore_btn.setEnabled(bool(count))

    def _restore_hidden(self) -> None:
        if not self.store:
            return
        n = self.store.restore_hidden()
        self._refresh_hidden_label()
        QMessageBox.information(self, "已恢复", f"恢复了 {n} 项，关闭设置后即可看到。")

    def _clear_credentials(self) -> None:
        if QMessageBox.question(self, "确认", "确定要清除本机保存的账号密码吗？") \
                == QMessageBox.StandardButton.Yes:
            config.clear_credentials()
            self.account_label.setText("尚未保存账号")

    @staticmethod
    def _parse_hours(text: str, fallback: list[int]) -> list[int]:
        out = []
        for part in (text or "").replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return sorted(set(out), reverse=True) or fallback

    def save(self) -> None:
        config.update("sync",
                      interval_minutes=self.interval.value(),
                      request_delay=round(self.delay.value(), 2),
                      detail_refresh_hours=self.detail_hours.value())
        config.update("remind",
                      enabled=self.remind_enabled.isChecked(),
                      threshold_hours=self._parse_hours(self.thresholds.text(),
                                                        [24, 6, 1]),
                      exam_lead_hours=self._parse_hours(self.exam_leads.text(),
                                                        [24, 1]),
                      quiet_hours=[self.quiet_start.value(), self.quiet_end.value()],
                      new_homework=self.new_homework.isChecked(),
                      new_exam=self.new_exam.isChecked(),
                      new_notice=self.new_notice.isChecked())
        config.update("widget",
                      always_on_top=self.always_on_top.isChecked(),
                      theme="dark" if self.dark.isChecked() else "light",
                      opacity=round(self.opacity.value() / 100.0, 2),
                      show_notices=self.show_notices.isChecked())
        if self.autostart.isChecked() != startup.is_enabled():
            if not startup.set_enabled(self.autostart.isChecked()):
                QMessageBox.warning(self, "提示", "开机自启设置失败，可稍后重试。")
