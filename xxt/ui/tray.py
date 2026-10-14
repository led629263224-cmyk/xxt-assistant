"""托盘图标与系统通知。

托盘是"程序的常驻入口"：窗口被关掉后仍在这里活着，右键菜单提供同步/设置/退出。
系统气泡通知复用 QSystemTrayIcon.showMessage，不需要额外的依赖。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import resources

log = logging.getLogger("xxt.ui")

URL_XXT_HOME = "https://i.chaoxing.com"
URL_NOTICE = "https://notice.chaoxing.com/pc/notice/myNotice"


def make_icon(color: str = "#2563EB") -> QIcon:
    """优先用 assets 里的图标；没有就现画一个（免去打包资源也能跑）。"""
    icon = resources.app_icon()
    if icon is not None and not icon.isNull():
        return icon
    return _fallback_icon(color)


def _fallback_icon(color: str = "#2563EB") -> QIcon:
    """用代码画一个图标，免去打包图片资源。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(3, 3, 58, 58, 15, 15)
    painter.setPen(QColor("white"))
    font = QFont("Microsoft YaHei")
    font.setPixelSize(34)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "学")
    painter.end()
    return QIcon(pm)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, card, on_refresh, on_settings, on_quit, parent=None,
                 on_login=None):
        super().__init__(make_icon(), parent)
        self.card = card
        self.setToolTip("学习通小助手")

        menu = QMenu()
        self.act_toggle = QAction("显示任务清单", menu)
        self.act_toggle.triggered.connect(self.toggle_card)
        menu.addAction(self.act_toggle)

        act_refresh = QAction("立即同步", menu)
        act_refresh.triggered.connect(on_refresh)
        menu.addAction(act_refresh)

        # 卡片被收起来、或者会话过期时，登录入口不能只挂在卡片上
        if on_login is not None:
            act_login = QAction("登录 / 重新登录…", menu)
            act_login.triggered.connect(on_login)
            menu.addAction(act_login)

        act_site = QAction("打开学习通网页", menu)
        act_site.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(URL_XXT_HOME)))
        menu.addAction(act_site)

        act_notice = QAction("打开通知中心", menu)
        act_notice.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(URL_NOTICE)))
        menu.addAction(act_notice)

        menu.addSeparator()
        act_settings = QAction("设置…", menu)
        act_settings.triggered.connect(on_settings)
        menu.addAction(act_settings)

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(on_quit)
        menu.addAction(act_quit)

        menu.aboutToShow.connect(self._sync_menu_text)
        self._menu = menu
        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _sync_menu_text(self) -> None:
        self.act_toggle.setText("隐藏任务清单" if self.card.isVisible()
                                else "显示任务清单")

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_card()

    def toggle_card(self) -> None:
        if self.card.isVisible():
            self.card.hide_card()
        else:
            self.card.show_card()

    def toast(self, title: str, body: str, urgent: bool = False) -> None:
        """系统气泡通知（Notifier 的回调）。"""
        icon = (QSystemTrayIcon.MessageIcon.Warning if urgent
                else QSystemTrayIcon.MessageIcon.Information)
        self.showMessage(title, body, icon, 12000 if urgent else 8000)
