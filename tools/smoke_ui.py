"""无头交互自检：不需要人点，验证卡片上的交互链路真的通。

覆盖：
  1. 右键移除 → Store 真的把人隐藏了，行也从界面消失
  2. 标题栏没有展开按钮、未读通知直接显示在列表底部
  3. 过期作业 → 不出现在清单里
  4. 恢复被移除 → 行回来
用法：
    .venv\\Scripts\\python.exe tools\\smoke_ui.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QToolButton            # noqa: E402

from xxt import config                                          # noqa: E402
from xxt.store import Store                                     # noqa: E402
from xxt.ui import style as ui_style                            # noqa: E402
from xxt.ui.widget import TaskRow, TaskCard                     # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + extra if extra else ''}")
    if not cond:
        failures.append(name)


class FakeSync:
    running = True
    busy = False

    def __init__(self) -> None:
        self.next_run_at = datetime.now() + timedelta(minutes=20)

    def trigger(self) -> None:
        pass


def rows(card: TaskCard) -> list[TaskRow]:
    return [card.list_layout.itemAt(i).widget()
            for i in range(card.list_layout.count())
            if isinstance(card.list_layout.itemAt(i).widget(), TaskRow)]


def task_rows(card: TaskCard) -> list[TaskRow]:
    """只要作业/考试行（通知行的 key 是空的）。"""
    return [r for r in rows(card) if r._key]


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setPalette(ui_style.build_palette("light"))

    tmp = Path(tempfile.mkdtemp(prefix="xxt-smoke-"))
    config._set_data_paths(tmp)
    config.load()
    config.save()

    store = Store(tmp / "smoke.db")
    now = datetime.now().replace(second=0, microsecond=0)
    store.upsert_assignments([
        {"key": "a1", "course": "数字电路", "title": "第三章 作业", "status": "未提交",
         "deadline": (now + timedelta(hours=3)).strftime("%m-%d %H:%M"),
         "deadline_dt": (now + timedelta(hours=3)).isoformat(timespec="minutes"),
         "done": False, "url": "https://mooc1.chaoxing.com/"},
        {"key": "a2", "course": "高等数学", "title": "已经交过的作业", "status": "已提交",
         "done": True},
        {"key": "a3", "course": "大学物理", "title": "已经过期的作业", "status": "未提交",
         "deadline_dt": (now - timedelta(hours=2)).isoformat(timespec="minutes"),
         "done": False},
    ])
    store.upsert_exams([{"key": "e1", "name": "《数据结构》期末机考", "course": "数据结构",
                         "exam_time": "10-15 19:00 ~ 10-15 21:00",
                         "start_dt": (now + timedelta(hours=18)).isoformat(timespec="minutes"),
                         "duration": "120", "answer_status": "未提交"}])
    store.upsert_notices([{"notice_id": "n1", "title": "通知一", "isread": False}])

    card = TaskCard(store, FakeSync())
    card.show()
    for _ in range(6):
        app.processEvents()

    titles = [r.toolTip().splitlines()[0] for r in task_rows(card)]
    check("列出未完成作业与考试", titles == ["第三章 作业", "《数据结构》期末机考"],
          str(titles))
    check("不显示已提交的作业", "已经交过的作业" not in titles)
    check("不显示已过期的作业", "已经过期的作业" not in titles)
    check("未读通知显示在列表底部", "通知一" in [r.toolTip() for r in rows(card)])

    # 过期/已提交的即使时间最近也不该混进来
    check("按截止时间排序（近的在前）", titles[0] == "第三章 作业")
    check("角标数量 = 条目数", card.badge.text() == "2", card.badge.text())

    # 右键移除
    target = task_rows(card)[0]
    target.removeRequested.emit(target._key)
    for _ in range(4):
        app.processEvents()
    check("移除后库里标记为隐藏", store.hidden_count() == 1,
          f"hidden_count={store.hidden_count()}")
    check("移除后行消失", [r.toolTip().splitlines()[0] for r in task_rows(card)]
          == ["《数据结构》期末机考"])
    check("移除后角标减一", card.badge.text() == "1", card.badge.text())

    # 同步再跑一遍也不能复活
    store.upsert_assignments([{"key": "a1", "course": "数字电路", "title": "第三章 作业",
                               "status": "未提交", "done": False}])
    check("再次同步不会复活被移除的作业", store.hidden_count() == 1)

    # 恢复
    check("恢复被移除的条目", store.restore_hidden() == 1)
    card.reload()
    for _ in range(4):
        app.processEvents()
    check("恢复后行回来了", "第三章 作业" in [r.toolTip().splitlines()[0] for r in rows(card)])

    # 标题栏工具按钮：只剩同步 / 设置 / 收起，不再有展开按钮
    buttons = [b for b in card.findChildren(QToolButton)]
    check("标题栏没有展开按钮", all(b.text() not in ("▸", "▾") for b in buttons),
          "".join(b.text() for b in buttons))

    # 未读通知直接显示在列表底部（不再需要展开）
    check("列表底部显示未读通知",
          any("通知一" in (r.toolTip() or "") for r in rows(card)))

    card.hide()
    store.close()
    print()
    print("全部通过" if not failures else f"失败 {len(failures)} 项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    with patch("xxt.config._mac_keychain",
               return_value=Mock(get_password=Mock(return_value=None))):
        raise SystemExit(main())
