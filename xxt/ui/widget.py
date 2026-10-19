"""桌面悬浮任务清单（PySide6）。

纯展示 + 交互层：数据全部从 Store 读，网络动作通过信号交给 main.py 处理。
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta

from PySide6.QtCore import (QEvent, QPoint, QRectF, QSize, Qt, QTimer, Signal,
                            QEasingCurve, QVariantAnimation, Slot)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import config
from ..parse import humanize_delta, nearest_datetime_for
from . import style

log = logging.getLogger("xxt.ui")

# 清单里只剩未完成项，这些状态写出来是废话
_PENDING_WORDS = ("未提交", "未交", "未完成", "待提交", "待完成")

# "10-15 19:38 ~ 10-15 21:38" -> "10-15 19:38~21:38"（同日考试省掉重复日期）
_SAME_DAY_SPAN = re.compile(
    r"(?:\d{4}-)?(\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2})\s*[~～至-]\s*(?:\d{4}-)?\1\s+(\d{1,2}:\d{2})")
# 考试时间区间的结束时间，用来判断这场考试是不是早就结束了
_SPAN_END = re.compile(
    r"[至~～]\s*(\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?|\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?)")
_YEAR_PREFIX = re.compile(r"\b\d{4}-(\d{1,2}-\d{1,2})")


def _short_span(text: str) -> str:
    """把考试时间区间压缩成更短的写法，给标题留位置。"""
    text = " ".join((text or "").split())
    text = _SAME_DAY_SPAN.sub(r"\1 \2~\3", text)
    return _YEAR_PREFIX.sub(r"\1", text)      # 年份占位置又没有信息量


def _span_end(text: str) -> datetime | None:
    """取“… 至 2026-11-13 23:59”里的结束时间；只给到日期就补当天 23:59。"""
    match = _SPAN_END.search(text or "")
    if not match:
        return None
    raw = match.group(1).strip()
    if len(raw) == 5:                          # MM-DD
        raw = f"{datetime.now().year}-{raw} 23:59"
    elif len(raw) == 10:                       # YYYY-MM-DD
        raw += " 23:59"
    elif len(raw) == 11:                       # MM-DD HH:MM
        raw = f"{datetime.now().year}-{raw}"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None

# 一屏最多列几条（作业和考试合并计数）
MAX_ITEMS = 30
MAX_NOTICES = 3

CARD_WIDTH = 340
# 列表区最大高度：超出的部分在卡片里滚动，内容少时卡片自动变矮
MAX_LIST_HEIGHT = 430


class ElidedLabel(QLabel):
    """按宽度省略的标题标签（Qt 的 QLabel 不会自动省略）。

    注意：QLabel 默认的 minimumSizeHint 等于整段文字的宽度，会把所在的
    水平布局撑开、把右边的内容挤出卡片，所以这里必须把最小宽度压到很小。
    """

    MIN_WIDTH = 24

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(self.MIN_WIDTH)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt 命名)
        self._full = text or ""
        self._apply()

    def fullText(self) -> str:  # noqa: N802
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self._full) + 2,
                     metrics.height() + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.MIN_WIDTH, self.fontMetrics().height() + 2)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        width = max(self.MIN_WIDTH, self.width())
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, width)
        QLabel.setText(self, elided)
        self.setToolTip(self._full)


class BackgroundCard(QFrame):
    """按卡片尺寸居中裁剪背景，圆角裁切，并叠加主题蒙层。"""

    def set_background(self, path: str, overlay: int, theme: str) -> None:
        self._background = QPixmap(path) if path else QPixmap()
        self._overlay = overlay
        self._theme = theme
        self.update()

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        pm = getattr(self, "_background", QPixmap())
        if pm.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 12, 12)
        painter.setClipPath(clip)
        scaled = pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((self.width() - scaled.width()) // 2,
                           (self.height() - scaled.height()) // 2, scaled)
        tint = QColor(style.palette(self._theme)["window"])
        tint.setAlpha(round(255 * self._overlay / 100))
        painter.fillRect(rect, tint)
        painter.setPen(QColor(style.palette(self._theme)["border"]))
        painter.drawPath(clip)


class TaskRow(QFrame):
    """一条待办：左标题 + 右倒计时。右键可以把它从列表里划掉。"""

    removeRequested = Signal(str)   # 发出去的 key 形如 "a:<作业key>" / "e:<考试key>"

    def __init__(self, title: str, meta: str, right: str, color: str,
                 tooltip: str = "", key: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Row")
        self._key = key
        self.setToolTip(tooltip or title)
        self.setObjectName("SwipeRow")
        self.body = QFrame(self)
        self.body.setObjectName("Row")
        self._press = None
        self._offset = 0
        self._dismissing = False
        self._effect = QGraphicsOpacityEffect(self.body)
        self._effect.setOpacity(1)
        self.body.setGraphicsEffect(self._effect)
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._move_body)
        self._animation.finished.connect(self._finish_swipe)

        lay = QVBoxLayout(self.body)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title_label = ElidedLabel(title)
        self.title_label.setObjectName("Title")
        self.right_label = QLabel(right)
        self.right_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.right_label.setAlignment(Qt.AlignmentFlag.AlignRight |
                                      Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.title_label, 1)
        top.addWidget(self.right_label, 0)
        lay.addLayout(top)

        self.meta_label = ElidedLabel(meta)
        self.meta_label.setObjectName("Meta")
        lay.addWidget(self.meta_label)
        for child in (self.body, self.title_label, self.right_label, self.meta_label):
            child.installEventFilter(self)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.body.sizeHint()

    def resizeEvent(self, event):  # noqa: N802
        self.body.setGeometry(self._offset, 0, self.width(), self.height())
        super().resizeEvent(event)

    @property
    def swiping(self) -> bool:
        return self._press is not None or self._animation.state() == QVariantAnimation.State.Running

    def _move_body(self, value) -> None:
        self._offset = int(value)
        self.body.move(self._offset, 0)
        self._effect.setOpacity(max(0.12, 1 - abs(self._offset) / max(1, self.width())))

    def _finish_swipe(self) -> None:
        if self._dismissing:
            self.removeRequested.emit(self._key)

    def _gesture(self, event) -> bool:
        if not self._key:
            return False
        if event.type() == QEvent.Type.UngrabMouse and self._press is not None:
            self._press = None
            self._animation.setDuration(220)
            self._animation.setStartValue(self._offset)
            self._animation.setEndValue(0)
            self._animation.start()
            return True
        if event.type() == QEvent.Type.ContextMenu:
            return True
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
            if self._dismissing:
                return True
            self._animation.stop()
            self._press = event.globalPosition().toPoint()
            self._origin = self._offset
            window = self.window()
            self._swipe_bounds = window.frameGeometry()
            self._swipe_bounds.moveTopLeft(window.mapToGlobal(QPoint(0, 0)))
            self.grabMouse()
            return True
        if self._press is not None and event.type() == QEvent.Type.MouseMove:
            position = event.globalPosition().toPoint()
            delta = position - self._press
            self._move_body(self._origin + delta.x())
            return True
        if (self._press is not None and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.RightButton):
            position = event.globalPosition().toPoint()
            delta = position - self._press
            self._move_body(self._origin + delta.x())
            self._press = None
            self.releaseMouse()
            self._dismissing = ((position.x() > self._swipe_bounds.right() and delta.x() > 0)
                                or (position.x() < self._swipe_bounds.left() and delta.x() < 0))
            end = (self.width() if self._offset > 0 else -self.width()) if self._dismissing else 0
            self._animation.setDuration(180 if self._dismissing else 220)
            self._animation.setStartValue(self._offset)
            self._animation.setEndValue(end)
            self._animation.start()
            return True
        return False

    def eventFilter(self, watched, event):  # noqa: N802
        return self._gesture(event) or super().eventFilter(watched, event)

    def event(self, event):
        if hasattr(self, "_animation") and self._gesture(event):
            return True
        return super().event(event)

    def set_right(self, text: str, color: str) -> None:
        self.right_label.setText(text)
        self.right_label.setStyleSheet(f"color: {color}; font-weight: 600;")

class TaskCard(QWidget):
    refreshRequested = Signal()
    settingsRequested = Signal()
    loginRequested = Signal()

    def __init__(self, store, sync, parent=None):
        super().__init__(parent)
        self.store = store
        self.sync = sync
        self._drag_offset: QPoint | None = None
        self._time_rows: list[tuple[TaskRow, datetime | None]] = []
        self._need_login = False          # 登录失效时把「去登录」按钮露出来

        cfg = config.load()
        wcfg = cfg.get("widget", {})
        self.theme = wcfg.get("theme", "light")

        self.setObjectName("TaskCardRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("学习通小助手")
        from .tray import make_icon
        self.setWindowIcon(make_icon())
        self._apply_window_flags()

        self._build_ui()
        self.apply_config()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30_000)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.timeout.connect(self.reload)
        from .desktop import DesktopPresence
        self._desktop_presence = DesktopPresence(self)

    # -------------------------------------------------- 构建

    def _apply_window_flags(self) -> None:
        cfg = config.load()
        # macOS 的 Tool 是 NSPanel，默认失焦隐藏且处于普通窗口之上。
        kind = Qt.WindowType.Window if sys.platform == "darwin" else Qt.WindowType.Tool
        flags = Qt.WindowType.FramelessWindowHint | kind
        if cfg.get("widget", {}).get("always_on_top", False):
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = BackgroundCard(self)
        self.card.setObjectName("Card")
        outer.addWidget(self.card)

        root = QVBoxLayout(self.card)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        # 标题栏
        header = QHBoxLayout()
        header.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color: #9CA3AF; font-size: 10px;")
        self.title = QLabel("学习通待办")
        self.title.setObjectName("Title")
        header.addWidget(self.dot)
        header.addWidget(self.title)
        header.addStretch(1)
        self.badge = QLabel("")
        self.badge.setObjectName("Badge")
        self.badge.hide()
        header.addWidget(self.badge)

        self.btn_refresh = self._tool_button("⟳", "立即同步", self.refreshRequested)
        self.btn_settings = self._tool_button("⚙", "设置", self.settingsRequested)
        self.btn_hide = self._tool_button("✕", "收起到托盘", self.hide_card)
        for btn in (self.btn_refresh, self.btn_settings, self.btn_hide):
            header.addWidget(btn)
        root.addLayout(header)

        # 状态行
        self.status = QLabel("准备同步…")
        self.status.setObjectName("Status")
        root.addWidget(self.status)

        sep = QFrame()
        sep.setObjectName("Sep")
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # 列表
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_host = QWidget()
        self.list_host.setObjectName("ListHost")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        root.addWidget(self.scroll, 1)

        # 底部
        footer = QHBoxLayout()
        self.next_label = QLabel("")
        self.next_label.setObjectName("Status")
        footer.addWidget(self.next_label)
        footer.addStretch(1)
        self.btn_login = QPushButton("去登录")
        self.btn_login.setObjectName("Primary")
        self.btn_login.clicked.connect(self.loginRequested.emit)
        self.btn_login.hide()
        footer.addWidget(self.btn_login)
        root.addLayout(footer)

    def _tool_button(self, text: str, tip: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("Tool")
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        btn.setFixedSize(22, 22)
        # 指定符号字体，避免 ⟳ ⚙ 这类字符被系统回退成彩色 emoji
        font = btn.font()
        font.setFamily("Apple Symbols" if sys.platform == "darwin" else "Segoe UI Symbol")
        btn.setFont(font)
        return btn

    # -------------------------------------------------- 配置

    def apply_config(self) -> None:
        cfg = config.load()
        wcfg = cfg.get("widget", {})
        self.theme = wcfg.get("theme", "light")
        self.card.setStyleSheet("")
        rules = style.stylesheet(self.theme)
        if wcfg.get("background_image"):
            panel = QColor(style.palette(self.theme)["panel"])
            rules += (f"QFrame#Row {{ background: rgba({panel.red()}, {panel.green()}, "
                      f"{panel.blue()}, 180); }}")
        self.setStyleSheet(rules)
        self.setPalette(style.build_palette(self.theme))
        self.card.set_background(wcfg.get("background_image", ""),
                                 int(wcfg.get("background_overlay", 78)), self.theme)
        self._apply_window_flags()
        try:
            self.setWindowOpacity(float(wcfg.get("opacity", 0.97)))
        except Exception:
            self.setWindowOpacity(0.97)
        self.setFixedWidth(CARD_WIDTH)
        self.reload()

    def fit_height(self) -> None:
        """按当前列表内容算高度：内容少就矮一点，超过上限才出现滚动条。"""
        self.list_layout.activate()
        content = max(self.list_layout.sizeHint().height(), self._content_height())
        self.setFixedHeight(self._chrome_height()
                            + max(60, min(content, MAX_LIST_HEIGHT)))

    def _content_height(self) -> int:
        """直接累加子控件高度。

        刚重建完列表时 QLayout 的缓存 sizeHint 还停留在"空布局"的 0
        （invalidate 是延迟生效的），activate() 也叫不醒它，只有等下一轮事件循环
        才对。所以这里自己算一份，取两者较大值，避免卡片瞬间塌成一条。
        """
        lay = self.list_layout
        margins = lay.contentsMargins()
        total = margins.top() + margins.bottom()
        count = 0
        for i in range(lay.count()):
            widget = lay.itemAt(i).widget()
            if widget is not None:
                total += widget.sizeHint().height()
                count += 1
        if count > 1:
            total += lay.spacing() * (count - 1)
        return total

    def showEvent(self, event):  # noqa: N802
        """控件第一次显示前字体/样式还没生效，此刻量出来的高度是错的。

        构造后立刻 fit_height 会算出「只有 60px 的列表区」，卡片会扁成一条。
        显示后补一次，拿到真实高度。
        """
        super().showEvent(event)
        QTimer.singleShot(0, self.fit_height)

    def _chrome_height(self) -> int:
        """标题栏 + 状态行 + 分隔线 + 底部 + 内外边距的总高度。"""
        root = self.card.layout()
        if root is not None:
            root.activate()
        total = self.card.sizeHint().height()
        # QScrollArea 的 sizeHint 是固定默认值，减掉它即得到其余部分的真实高度
        return max(96, total - self.scroll.sizeHint().height())

    def save_position(self) -> None:
        if self.isVisible():
            config.update("widget", x=self.x(), y=self.y())

    def hide_card(self) -> None:
        self._desktop_presence.set_enabled(False)
        self.save_position()
        self.hide()

    def show_card(self) -> None:
        self.apply_config()
        cfg = config.load().get("widget", {})
        x, y = cfg.get("x"), cfg.get("y")
        if x is None or y is None:
            self.move_to_default_corner()
        else:
            self.move(int(x), int(y))
        self.show()
        self.raise_()
        self.reload()
        self._desktop_presence.set_enabled(True)

    def move_to_default_corner(self) -> None:
        """首次运行放到主屏右上角。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.top() + 24)

    # -------------------------------------------------- 数据渲染

    def reload(self) -> None:
        """按当前配置重建列表（数据取自数据库，不联网）。

        只列"未完成且还没过期"的作业 + 未完成的考试，混在一起按时间排序：
        作业看截止时间，考试看开考时间，时间未知的排最后。
        """
        if any(row.swiping for row, _ in self._time_rows):
            self._reload_timer.start(250)
            return
        pending = self.store.pending_assignments()
        exams = self.store.unfinished_exams()
        _total, unread = self.store.notice_stats()
        cfg = config.load()
        now = datetime.now()
        limit = MAX_ITEMS

        items: list[tuple[datetime | None, str, dict]] = []
        for row in pending:
            dl = self._deadline(row, now)
            if dl is not None and dl <= now:
                continue          # 已经过期的不再显示
            items.append((dl, "a", row))
        for exam in exams:
            start = self._parse_dt(exam.get("start_dt"))
            if self._exam_finished(exam, start, now):
                continue          # 早就考完的（比如上学期没交的）不再显示
            items.append((start, "e", exam))
        items.sort(key=lambda it: (it[0] is None, it[0] or datetime.max))

        self._clear_list()
        self._time_rows = []
        show_notices = (unread > 0
                        and cfg.get("widget", {}).get("show_notices", True))
        if not items and not show_notices:
            empty = QLabel("暂无待完成的作业或考试，去休息一下吧 ☕")
            empty.setObjectName("Empty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.list_layout.insertWidget(0, empty)

        shown = items[:limit]
        for when, kind, row in shown:
            if kind == "a":
                self._add_assignment(row, now)
            else:
                self._add_exam(row, now)

        hidden = len(items) - len(shown)
        if hidden:
            self._add_more_hint(hidden, "项", suffix="（滚动查看）")

        if show_notices:
            self._add_section(f"未读通知 · {unread}")
            for notice in self.store.notices(limit=MAX_NOTICES, unread_only=True):
                self._add_notice(notice)

        if items:
            self.badge.setText(f"{len(items)}")
            self.badge.show()
        else:
            self.badge.hide()
        # 本地没有凭据、或上次同步因为登录失效失败时，都把「去登录」露出来
        self.btn_login.setVisible(self._need_login or
                                   not (config.has_credentials() or config.COOKIE_PATH.exists()))
        self._update_next_label()
        # 末尾放一个弹簧：行内容保持紧凑、顶部对齐，多余空间留在底部
        self.list_layout.addStretch(1)
        self.fit_height()
        self.list_host.adjustSize()
        self.list_layout.invalidate()
        QTimer.singleShot(0, self.fit_height)
        self.update()

    def remove_item(self, token: str) -> None:
        """右键菜单里的"从列表移除"：只隐藏本机显示，不同步到学习通。"""
        kind, _, key = token.partition(":")
        if not key:
            return
        if kind == "a":
            self.store.hide_assignment(key)
        else:
            self.store.hide_exam(key)
        self.reload()
        left = self.store.hidden_count()
        self.set_status(f"已从列表移除（共 {left} 项），可在设置·维护 里恢复", "info")

    def _clear_list(self) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                # 先摘掉父子关系，否则 deleteLater 生效前会在原位置留下重影
                widget.setParent(None)
                widget.deleteLater()

    def _add_section(self, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("Section")
        self.list_layout.addWidget(label)

    def _add_more_hint(self, count: int, unit: str, suffix: str = "，点 ▸ 展开查看") -> None:
        hint = QLabel(f"还有 {count} {unit}{suffix}")
        hint.setObjectName("Meta")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_layout.addWidget(hint)

    def _add_assignment(self, row: dict, now: datetime) -> None:
        deadline = self._deadline(row, now)
        color = style.urgency_color(self.theme, deadline, now)
        right = humanize_delta(deadline - now) if deadline else "截止未知"
        meta_bits = [row.get("course") or ""]
        status = row.get("status") or ""
        # 清单里只剩未完成的，"未提交/未交" 这类状态没有信息量，省掉换更多标题位
        if status and not any(m in status for m in _PENDING_WORDS):
            meta_bits.append(status)
        if row.get("deadline"):
            meta_bits.append(f"截止 {row['deadline']}")
        if row.get("question_count"):
            meta_bits.append(f"{row['question_count']} 题")
        tooltip = "\n".join(filter(None, [
            row.get("title"), row.get("course"), f"状态：{row.get('status') or '未知'}",
            f"截止：{row.get('deadline') or '未知'}",
            f"题量：{row.get('question_count') or '未知'}",
            f"满分：{row.get('full_score') or '未知'}",
            f"得分：{row.get('score') or '未公布'}",
            "按住右键，将鼠标拖出卡片左侧或右侧边缘后松手移除；窗口内松手回弹",
        ]))
        widget = TaskRow(row.get("title") or "未命名作业", " · ".join(meta_bits),
                         right, color, tooltip, key=f"a:{row['key']}")
        widget.removeRequested.connect(self.remove_item)
        self.list_layout.addWidget(widget)
        self._time_rows.append((widget, deadline))

    def _add_exam(self, exam: dict, now: datetime) -> None:
        start = self._parse_dt(exam.get("start_dt"))
        color = style.urgency_color(self.theme, start, now)
        right = humanize_delta(start - now) if start else (exam.get("answer_status") or "待考")
        exam_name = exam.get("name") or "未命名考试"
        course = exam.get("course") or ""
        if course and (course == exam_name or exam_name.startswith(course)):
            course = ""       # 课程名和考试名一样时不必重复占一行
        meta_bits = [course, "考试"]
        span = _short_span(exam.get("exam_time") or "")
        if span:
            meta_bits.append(span)
        if exam.get("duration"):
            meta_bits.append(f"{exam['duration']} 分钟")
        tooltip = "\n".join(filter(None, [
            exam.get("name"), f"时间：{exam.get('exam_time') or '未知'}",
            f"考试状态：{exam.get('exam_status') or '未知'}",
            f"作答状态：{exam.get('answer_status') or '未知'}",
            f"时长：{exam.get('duration') or '未知'} 分钟",
            f"分数：{exam.get('score') or '未公布'}",
            "按住右键，将鼠标拖出卡片左侧或右侧边缘后松手移除；窗口内松手回弹",
        ]))
        row = TaskRow(exam_name, " · ".join(filter(None, meta_bits)),
                      right, color, tooltip, key=f"e:{exam['key']}")
        row.removeRequested.connect(self.remove_item)
        self.list_layout.addWidget(row)
        self._time_rows.append((row, start))

    def _add_notice(self, notice: dict) -> None:
        body = (notice.get("content") or "").strip().replace("\n", " ")
        meta = " · ".join(filter(None, [notice.get("sender") or "",
                                        notice.get("publish_time") or "",
                                        body[:40]]))
        row = TaskRow(notice.get("title") or "(无标题)", meta, "未读",
                      style.palette(self.theme)["accent"],
                      body[:300] or notice.get("title") or "")
        self.list_layout.addWidget(row)

    # -------------------------------------------------- 时间刷新

    def _tick(self) -> None:
        """只更新倒计时文字，避免每 30 秒重建整个列表。"""
        now = datetime.now()
        for widget, deadline in self._time_rows:
            if deadline is None:
                continue
            widget.set_right(humanize_delta(deadline - now),
                             style.urgency_color(self.theme, deadline, now))
        self._update_next_label()

    def _update_next_label(self) -> None:
        nxt = getattr(self.sync, "next_run_at", None)
        text = ""
        if nxt:
            remain = int((nxt - datetime.now()).total_seconds())
            if remain > 0:
                mins = max(1, remain // 60)
                text = f"下次自动同步 {mins} 分钟后"
        self.next_label.setText(text)
        self.btn_refresh.setEnabled(not getattr(self.sync, "busy", False))

    # -------------------------------------------------- 状态

    def set_status(self, text: str, level: str = "info") -> None:
        colors = {"info": style.palette(self.theme)["sub"],
                  "ok": style.palette(self.theme)["ok"],
                  "warn": style.palette(self.theme)["warn"],
                  "error": style.palette(self.theme)["danger"]}
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {colors.get(level, colors['info'])};")

    def set_need_login(self, flag: bool) -> None:
        """是否需要露出「去登录」（没登录 / 会话过期 / 刚登录成功）。"""
        self._need_login = bool(flag)
        self.reload()

    def set_syncing(self, syncing: bool) -> None:
        self.dot.setStyleSheet(
            "color: #F59E0B; font-size: 10px;" if syncing
            else "color: #10B981; font-size: 10px;")
        self.btn_refresh.setEnabled(not syncing)
        if syncing:
            self.set_status("正在同步…", "info")

    @Slot(str)
    def show_progress(self, text: str) -> None:
        self.set_syncing(True)
        self.set_status(text)
        if not self._reload_timer.isActive():
            self._reload_timer.start(80)

    def show_report(self, report) -> None:
        if report.ok:
            level = "warn" if report.errors else "ok"
            self.set_status(report.summary(), level)
        else:
            self.set_status("同步失败：" + "；".join(report.errors[:2]), "error")
        # 登录态问题（没登录 / 会话过期）时把「去登录」露出来
        if not report.ok and "未登录" in " ".join(report.errors):
            self._need_login = True
        self.reload()
        self._update_next_label()

    # -------------------------------------------------- 窗口拖动

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            self.save_position()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.hide_card()

    # -------------------------------------------------- 小工具

    @staticmethod
    def _parse_dt(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    @classmethod
    def _deadline(cls, row: dict, now: datetime) -> datetime | None:
        dt = cls._parse_dt(row.get("deadline_dt"))
        if dt:
            return dt
        return nearest_datetime_for(row.get("deadline") or "", now.date())

    @classmethod
    def _exam_finished(cls, exam: dict, start: datetime | None, now: datetime) -> bool:
        """考试结束时间已经过去很久就不再显示（比如上学期没交的）。

        结束时间取"开考 + 时长"和考试时间区间的终点里更晚的那个，
        这样正在进行中的、以及还在作答窗口内的都留着。
        """
        if start is None:
            return False
        try:
            minutes = int(exam.get("duration") or 0)
        except (TypeError, ValueError):
            minutes = 0
        end = start + timedelta(minutes=minutes) if minutes > 0 else start + timedelta(hours=4)
        window_end = _span_end(exam.get("exam_time") or "")
        if window_end and window_end > end:
            end = window_end
        return end <= now
