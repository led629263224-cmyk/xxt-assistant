"""渲染悬浮清单的预览图（不出网、不读真实数据，结果写到 docs/）。

用途：改完 UI 后快速看一眼长什么样，也可用于开发时回归截图。

用法：
    .venv\\Scripts\\python.exe tools\\preview_card.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication          # noqa: E402

from xxt import config                              # noqa: E402
from xxt.store import Store                         # noqa: E402
from xxt.ui.widget import TaskCard                  # noqa: E402

OUT_DIR = ROOT / "docs"


class FakeSync:
    """只提供卡片需要的几个属性，避免预览时联网。"""

    running = True
    busy = False

    def __init__(self) -> None:
        self.next_run_at = datetime.now() + timedelta(minutes=12)

    def trigger(self) -> None:
        pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:00")


def _short(dt: datetime) -> str:
    return dt.strftime("%m-%d %H:%M")


def seed(store: Store) -> None:
    now = datetime.now().replace(second=0, microsecond=0)
    raw = [
        ("实验四 整体调试实验报告", "计算机组成原理", now + timedelta(minutes=38)),
        ("第 6 章 课后习题", "数据结构与算法", now + timedelta(hours=4, minutes=20)),
        ("期中论文：网络协议分析", "计算机网络", now + timedelta(hours=21)),
        ("课程设计开题报告", "软件工程", now + timedelta(days=3, hours=6)),
        ("随堂测验（第 5 周）", "高等数学", now - timedelta(hours=9)),
    ]
    rows = []
    for i, (title, course, dl) in enumerate(raw):
        rows.append({
            "key": f"demo-{i}",
            "course": course,
            "courseid": "1000",
            "clazzid": "2000",
            "title": title,
            "status": "未提交" if dl > now else "待提交",
            "url": "https://mooc1.chaoxing.com/",
            "start_time": _short(dl - timedelta(days=10)),
            "deadline": _short(dl),
            "deadline_dt": _iso(dl),
            "score": None,
            "question_count": 12,
            "full_score": 100,
            "done": False,
        })
    store.upsert_assignments(rows)

    store.upsert_exams([
        {
            "key": "exam-demo-1",
            "name": "《数据结构》期末机考",
            "course": "数据结构与算法",
            "exam_time": f"{_short(now + timedelta(hours=19))} ~ "
                         f"{_short(now + timedelta(hours=21))}",
            "start_dt": _iso(now + timedelta(hours=19)),
            "duration": "120",
            "exam_status": "已发布",
            "answer_status": "未提交",
            "score": None,
            "method": "在线考试",
        },
        {
            "key": "exam-demo-2",
            "name": "大学英语单元测试（第 7 单元）",
            "course": "大学英语",
            "exam_time": f"{_short(now + timedelta(days=4))} ~ "
                         f"{_short(now + timedelta(days=4, hours=2))}",
            "start_dt": _iso(now + timedelta(days=4)),
            "duration": "60",
            "exam_status": "已发布",
            "answer_status": "未提交",
            "score": None,
            "method": "在线考试",
        },
    ])

    # 演示右键"从列表移除"：这场考试只在本机隐藏，仍保留在库里
    store.upsert_exams([{
        "key": "exam-demo-hidden",
        "name": "《线性代数》阶段测验",
        "course": "线性代数",
        "exam_time": _short(now + timedelta(days=6)),
        "start_dt": _iso(now + timedelta(days=6)),
        "duration": "45",
        "exam_status": "已发布",
        "answer_status": "未提交",
        "method": "在线考试",
    }])
    store.hide_exam("exam-demo-hidden")

    store.upsert_notices([
        {
            "notice_id": "notice-demo-1",
            "title": "关于 2026 年春季学期期中教学检查的通知",
            "content": "各学院：定于第 9-10 周开展期中教学检查，请按时提交自查材料。",
            "sender": "教务处",
            "publish_time": _short(now - timedelta(hours=2)),
            "isread": False,
        },
        {
            "notice_id": "notice-demo-2",
            "title": "实验室开放时间调整",
            "content": "因设备检修，本周六实验室开放时间调整为 9:00-15:00。",
            "sender": "实验中心",
            "publish_time": _short(now - timedelta(hours=20)),
            "isread": False,
        },
        {
            "notice_id": "notice-demo-3",
            "title": "关于开展网络安全知识竞赛的通知",
            "content": "面向全校学生，报名截止本周五。",
            "sender": "信息中心",
            "publish_time": _short(now - timedelta(days=2)),
            "isread": False,
        },
    ])


def render(app: QApplication, widget, path: Path) -> None:
    """把控件渲染成 PNG：show 一下让布局生效，截图后立刻隐藏。"""
    if hasattr(widget, "reload"):
        widget.reload()
    widget.show()
    for _ in range(12):
        app.processEvents()
    widget.grab().save(str(path))
    widget.hide()
    print(f"已生成 {path}")


def render_dialogs(app: QApplication, store: Store) -> None:
    from xxt.ui.dialogs import LoginDialog, SettingsDialog

    def fake_login(phone, password, remember):
        from xxt.client import LoginResult
        return LoginResult(False, "演示：用户名或密码错误")

    login = LoginDialog(fake_login)
    login.data_directory.setText(r"D:\XXT-Data")
    render(app, login, OUT_DIR / "dialog-login.png")
    settings = SettingsDialog(store=store)
    settings.data_directory.setText(r"D:\XXT-Data")
    for i, name in enumerate(("sync", "remind", "ui", "misc")):
        settings.tabs.setCurrentIndex(i)
        render(app, settings, OUT_DIR / f"dialog-settings-{name}.png")
    render(app, settings, OUT_DIR / "dialog-settings.png")


def main() -> int:
    app = QApplication(sys.argv[:1])
    # 与真实运行保持一致（main.run_gui 里也这么设）
    app.setStyle("Fusion")
    from xxt.ui import style as ui_style
    app.setPalette(ui_style.build_palette("light"))
    tmp = Path(tempfile.mkdtemp(prefix="xxt-preview-"))

    # 预览必须完全隔离：TaskCard.save_position 会写配置，
    # 不隔离的话跑一次预览就把用户真实的 config.json 改了（曾经踩过）。
    config._set_data_paths(tmp)
    config.CONFIG_PATH = tmp / "config.json"
    config._cache = None
    config.CREDENTIAL_PATH = tmp / "credentials.bin"
    config.LOG_PATH = tmp / "app.log"

    store = Store(tmp / "preview.db")
    seed(store)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config.load()
    config.save()

    card = TaskCard(store, FakeSync())
    card.set_status("上次同步 12 分钟前 · 5 门课程", "ok")

    for theme in ("light", "dark"):
        config.load()["widget"]["theme"] = theme
        config.load()["widget"]["x"] = None
        config.load()["widget"]["y"] = None
        card.apply_config()
        render(app, card, OUT_DIR / f"preview-{theme}.png")

    config.load()["widget"]["theme"] = "light"
    render_dialogs(app, store)

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
