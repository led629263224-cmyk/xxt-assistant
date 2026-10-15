"""配色与样式表：浅色 / 深色两套。"""

from __future__ import annotations

from datetime import datetime, timedelta

LIGHT = {
    "bg": "rgba(255, 255, 255, 245)",
    "window": "#FFFFFF",
    "panel": "#F9FAFB",
    "border": "#E5E7EB",
    "text": "#111827",
    "sub": "#6B7280",
    "accent": "#2563EB",
    "danger": "#DC2626",
    "warn": "#EA580C",
    "ok": "#059669",
    "hover": "#F3F4F6",
}

DARK = {
    "bg": "rgba(31, 41, 55, 246)",
    "window": "#1F2937",
    "panel": "#111827",
    "border": "#374151",
    "text": "#F9FAFB",
    "sub": "#9CA3AF",
    "accent": "#60A5FA",
    "danger": "#F87171",
    "warn": "#FB923C",
    "ok": "#34D399",
    "hover": "#374151",
}


def palette(theme: str) -> dict:
    return DARK if theme == "dark" else LIGHT


# 微调框箭头图标：Qt 对 subcontrol 的 border 三角支持不可靠（会画成两个灰方块），
# 系统原生箭头在自定义样式表下又不画，只能自己生成小 PNG 给样式表用。
_ARROW_CACHE: dict[str, str] = {}


def _arrow_icon(theme: str, direction: str) -> str:
    """生成上/下箭头图标，返回可用于 url() 的路径（失败返回空串）。"""
    key = f"{theme}-{direction}"
    if key in _ARROW_CACHE:
        return _ARROW_CACHE[key]
    path = ""
    try:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor, QPainter, QPixmap, QPolygonF

        from .. import config

        w, h = 9, 6
        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette(theme)["sub"]))
        if direction == "up":
            points = [QPointF(0, h), QPointF(w, h), QPointF(w / 2, 0)]
        else:
            points = [QPointF(0, 0), QPointF(w, 0), QPointF(w / 2, h)]
        painter.drawPolygon(QPolygonF(points))
        painter.end()

        target = config.DATA_DIR / "icons" / f"arrow-{direction}-{theme}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        if pm.save(str(target)):
            path = str(target).replace("\\", "/")
    except Exception:
        path = ""
    _ARROW_CACHE[key] = path
    return path


def build_palette(theme: str):
    """把主题写成 QPalette。

    必要：本机 Windows 处于深色模式时，Qt 默认调色板是深色，原生绘制的部件
    （微调框箭头、滚动条、下拉列表、消息框）会跟着变深，跟我们写死的浅色样式表打架。
    统一按主题设调色板，界面才可预期。
    """
    from PySide6.QtGui import QColor, QPalette

    c = palette(theme)
    p = QPalette()
    role = QPalette.ColorRole
    p.setColor(role.Window, QColor(c["window"]))
    p.setColor(role.WindowText, QColor(c["text"]))
    p.setColor(role.Base, QColor(c["window"]))
    p.setColor(role.AlternateBase, QColor(c["panel"]))
    p.setColor(role.Text, QColor(c["text"]))
    p.setColor(role.Button, QColor(c["panel"]))
    p.setColor(role.ButtonText, QColor(c["text"]))
    p.setColor(role.BrightText, QColor(c["danger"]))
    p.setColor(role.Highlight, QColor(c["accent"]))
    p.setColor(role.HighlightedText, QColor("#FFFFFF"))
    p.setColor(role.ToolTipBase, QColor(c["window"]))
    p.setColor(role.ToolTipText, QColor(c["text"]))
    p.setColor(role.PlaceholderText, QColor(c["sub"]))
    for disabled in (QPalette.ColorGroup.Disabled,):
        p.setColor(disabled, role.Text, QColor(c["sub"]))
        p.setColor(disabled, role.ButtonText, QColor(c["sub"]))
        p.setColor(disabled, role.WindowText, QColor(c["sub"]))
    return p


def urgency_color(theme: str, deadline: datetime | None,
                  now: datetime | None = None) -> str:
    """按剩余时间给颜色：<6h 红、<24h 橙、其余蓝、未知灰。"""
    pal = palette(theme)
    if deadline is None:
        return pal["sub"]
    now = now or datetime.now()
    remain = deadline - now
    if remain <= timedelta(0):
        return pal["danger"]
    if remain <= timedelta(hours=6):
        return pal["danger"]
    if remain <= timedelta(hours=24):
        return pal["warn"]
    return pal["accent"]


def stylesheet(theme: str) -> str:
    c = palette(theme)
    up_icon = _arrow_icon(theme, "up")
    down_icon = _arrow_icon(theme, "down")
    arrow_rules = ""
    if up_icon and down_icon:
        arrow_rules = f"""
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({up_icon}); width: 9px; height: 6px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({down_icon}); width: 9px; height: 6px; }}
    QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
    QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{
        image: none; }}
    """
    return f"""
    QWidget#Card {{
        background: {c['bg']};
        border: 1px solid {c['border']};
        border-radius: 12px;
    }}
    QLabel {{ color: {c['text']}; font-size: 12px; }}
    QLabel#Title {{ font-size: 14px; font-weight: 600; }}
    QLabel#Meta {{ color: {c['sub']}; font-size: 11px; }}
    QLabel#Section {{ color: {c['sub']}; font-size: 11px; font-weight: 600;
                      padding: 6px 2px 2px 2px; }}
    QLabel#Empty {{ color: {c['sub']}; font-size: 12px; padding: 12px; }}
    QLabel#Status {{ color: {c['sub']}; font-size: 11px; }}
    QLabel#Badge {{
        background: {c['accent']}; color: white; border-radius: 8px;
        padding: 1px 6px; font-size: 10px; font-weight: 600;
    }}
    QFrame#Row {{
        background: {c['panel']}; border: 1px solid {c['border']};
        border-radius: 8px;
    }}
    QFrame#Row:hover {{ background: {c['hover']}; }}
    QFrame#Sep {{ background: {c['border']}; max-height: 1px; border: none; }}
    QPushButton {{
        background: transparent; border: none; color: {c['sub']};
        font-size: 13px; padding: 2px 6px; border-radius: 6px;
    }}
    QPushButton:hover {{ background: {c['hover']}; color: {c['text']}; }}
    QPushButton#Primary {{
        background: {c['accent']}; color: white; font-size: 12px;
        padding: 5px 12px; border-radius: 6px;
    }}
    QScrollArea {{ background: transparent; border: none; }}
    /* QScrollArea 的 viewport 是它的孙子控件，必须单独设透明，
       否则会露出默认的深色底色（整条列表后面出现黑带） */
    QScrollArea > QWidget#qt_scrollarea_viewport,
    QScrollArea > QWidget > QWidget,
    QWidget#ListHost {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 6px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 3px;
                                   min-height: 20px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent; }}

    /* ---- 对话框 ----
       Windows 11 深色模式下 Qt 会套用系统深色调色板，而对话框没有自己的
       背景色，会出现"深底 + 深字"看不清的情况，所以这里把底色和控件配色
       全部写死，不再依赖系统主题。 */
    QDialog, QMessageBox, QFileDialog {{ background: {c['window']}; }}
    QDialog QLabel, QMessageBox QLabel, QFileDialog QLabel {{
        color: {c['text']}; font-size: 12px; }}
    QDialog QLabel#Meta, QMessageBox QLabel#Meta {{ color: {c['sub']}; }}
    QGroupBox {{
        border: 1px solid {c['border']}; border-radius: 8px;
        margin-top: 12px; padding: 12px 10px 6px 10px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; subcontrol-position: top left;
        left: 10px; padding: 0 4px; color: {c['sub']}; font-size: 11px;
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
        background: {c['window']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px;
        padding: 4px 6px; font-size: 12px;
        selection-background-color: {c['accent']}; selection-color: white;
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {c['accent']}; }}
    /* 微调框右侧的上下按钮：箭头用自己生成的 PNG（见 _arrow_icon） */
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        background: transparent; border: none; width: 18px; }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {c['hover']};
        border-radius: 4px; }}
    {arrow_rules}
    QCheckBox {{ color: {c['text']}; font-size: 12px; }}
    QCheckBox::indicator {{
        width: 14px; height: 14px; border: 1px solid {c['border']};
        border-radius: 4px; background: {c['window']};
    }}
    QCheckBox::indicator:checked {{
        background: {c['accent']}; border: 1px solid {c['accent']}; }}
    QDialogButtonBox QPushButton, QPushButton {{
        background: {c['panel']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px;
        padding: 5px 14px; font-size: 12px;
    }}
    QDialogButtonBox QPushButton:hover, QPushButton:hover {{
        background: {c['hover']}; }}
    QDialogButtonBox QPushButton:pressed, QPushButton:pressed {{
        background: {c['border']}; }}
    QPushButton#Primary {{
        background: {c['accent']}; color: white; border: none; font-weight: 600; }}
    QPushButton#Primary:hover {{ background: {c['accent']}; }}
    QPushButton#Danger {{ color: {c['danger']}; border: 1px solid {c['danger']}; }}
    /* 卡片右上角的四个小工具按钮 */
    QPushButton#Tool {{
        background: transparent; border: none; padding: 0;
        color: {c['sub']}; font-size: 13px;
    }}
    QPushButton#Tool:hover {{
        color: {c['accent']}; background: {c['hover']}; border-radius: 5px; }}
    QToolTip {{
        background: {c['window']}; color: {c['text']};
        border: 1px solid {c['border']}; padding: 4px;
    }}
    /* ---- 设置页的标签页 ---- */
    QTabWidget::pane {{
        border: 1px solid {c['border']}; border-radius: 8px;
        border-top-left-radius: 0px;
        background: {c['panel']}; top: -1px;
    }}
    QTabBar::tab {{
        background: transparent; color: {c['sub']};
        padding: 8px 17px; margin-right: 0; margin-bottom: -1px; font-size: 12px;
        border: 1px solid transparent; border-top-left-radius: 6px;
        border-top-right-radius: 6px;
    }}
    QTabBar::tab:hover {{ color: {c['text']}; }}
    QTabBar::tab:selected {{
        background: {c['panel']}; color: {c['accent']};
        border: 1px solid {c['border']}; border-bottom-color: {c['panel']};
        font-weight: 600;
    }}
    /* ---- 右键菜单 ---- */
    QMenu {{
        background: {c['window']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 8px; padding: 4px;
    }}
    QMenu::item {{
        padding: 6px 22px 6px 12px; border-radius: 5px; font-size: 12px;
    }}
    QMenu::item:selected {{ background: {c['hover']}; }}
    QMenu::separator {{
        height: 1px; background: {c['border']}; margin: 4px 8px;
    }}
    """
