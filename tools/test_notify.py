"""提醒功能自测：先离线推演规则，再用真实系统通知弹一遍（人眼可验证）。

阶段一（不弹窗）：造一批覆盖各档位的假数据，检查
    * 新作业 / 新考试是否会被播报
    * 截止 24h / 6h / 1h 三档是否按剩余时间正确落入
    * 已过期、已提交、还早得很的是否安静
    * 同一档是否只提醒一次（第二轮 scan 应为空）
    * 档位递进（20h -> 5h）时是否补一次更近的提醒
    * 深夜免打扰时段是否只放行 urgent 的提醒
阶段二（真弹窗）：用与程序完全相同的路径（QSystemTrayIcon.showMessage）把
    "新作业 / 1 小时内截止 / 即将开考 / 新通知" 四种提醒真的弹到桌面右下角。

全程在一个临时目录里跑，不碰真实数据库、配置和 cookie。
用法：
    .venv\\Scripts\\python.exe tools\\test_notify.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QSystemTrayIcon      # noqa: E402

from xxt import config                                          # noqa: E402
from xxt.notify import Notifier                                 # noqa: E402
from xxt.store import Store                                     # noqa: E402
from xxt.ui.tray import make_icon                               # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + extra if extra else ''}")
    if not cond:
        failures.append(name)


def tags_of(reminders, key: str) -> list[str]:
    return sorted(r.tag for r in reminders if r.key == key)


def pick(reminders, key: str, tag: str):
    for r in reminders:
        if r.key == key and r.tag == tag:
            return r
    return None


def backdate(store: Store, table: str, id_col: str, now: datetime,
             minutes: dict[str, int]) -> None:
    """把 first_seen 改成过去的时间——Store 写入时一律用"现在"，
    而"新作业播报"只认 20 分钟内的记录，所以测试里得手工回拨。"""
    for key, old_min in minutes.items():
        stamp = (now - timedelta(minutes=old_min)).isoformat(timespec="minutes")
        store._exec(f"UPDATE {table} SET first_seen=? WHERE {id_col}=?", (stamp, key))


def seed(store: Store, now: datetime) -> None:
    """造数据：每个 key 代表一个要验证的档位。"""
    def dt(**kw) -> str:
        return (now + timedelta(**kw)).isoformat(timespec="minutes")

    store.upsert_assignments([
        # 刚发现、40 分钟后截止 -> 期望 new + h1（urgent）
        {"key": "a_new1h", "course": "计算机组成原理", "title": "实验四 报告",
         "status": "未提交", "done": False,
         "deadline": dt(minutes=40)[5:16].replace("T", " "),
         "deadline_dt": dt(minutes=40)},
        # 早就发现、5 小时后截止 -> 期望 h6
        {"key": "a_5h", "course": "数据结构与算法", "title": "第 6 章 课后习题",
         "status": "未提交", "done": False, "deadline_dt": dt(hours=5)},
        # 20 小时后截止 -> 期望 h24
        {"key": "a_20h", "course": "计算机网络", "title": "期中论文",
         "status": "未提交", "done": False, "deadline_dt": dt(hours=20)},
        # 还有 3 天 -> 期望安静
        {"key": "a_far", "course": "软件工程", "title": "课程设计开题报告",
         "status": "未提交", "done": False, "deadline_dt": dt(days=3)},
        # 已经过期 -> 期望安静（卡片上也不再显示）
        {"key": "a_overdue", "course": "大学物理", "title": "已经过期的作业",
         "status": "未提交", "done": False, "deadline_dt": dt(hours=-2)},
        # 已经交了 -> 期望安静
        {"key": "a_done", "course": "高等数学", "title": "已经交过的作业",
         "status": "已提交", "done": True},
    ])
    store.upsert_exams([
        # 刚发现、2 小时后开考 -> 期望 new + e24
        {"key": "e_2h", "name": "《数据结构》期末机考", "course": "数据结构",
         "exam_time": "10-15 19:00 ~ 10-15 21:00", "duration": "120",
         "answer_status": "未提交", "start_dt": dt(hours=2)},
        # 30 分钟后开考 -> 期望 e1（urgent）
        {"key": "e_30m", "name": "大学英语单元测试", "course": "大学英语",
         "exam_time": "10-15 10:00 ~ 10-15 11:00", "duration": "60",
         "answer_status": "未提交", "start_dt": dt(minutes=30)},
    ])
    store.upsert_notices([
        {"notice_id": "n1", "title": "关于期中教学检查的通知",
         "content": "各学院：定于第 9-10 周开展期中教学检查。",
         "sender": "教务处", "isread": False},
    ])
    # 除了 a_new1h / e_2h / n1，其余都不是"刚发现"的
    backdate(store, "assignments", "key", now,
             {"a_5h": 2 * 1440, "a_20h": 2 * 1440, "a_far": 2 * 1440,
              "a_overdue": 5 * 1440, "a_done": 5 * 1440})
    backdate(store, "exams", "key", now, {"e_30m": 2 * 1440})


def phase_one(store: Store, notifier: Notifier, now: datetime) -> None:
    print("=" * 68)
    print("阶段一：规则推演（不弹窗）")
    print("=" * 68)
    cfg = config.load()
    print(f"当前 {now:%Y-%m-%d %H:%M}（{now.hour} 点）"
          f"　免打扰 {cfg['remind']['quiet_hours']} 点"
          f"　截止档位 {cfg['remind']['threshold_hours']} 小时"
          f"　考试档位 {cfg['remind']['exam_lead_hours']} 小时")
    print()

    first = notifier.scan(now)
    print(f"本轮算出 {len(first)} 条提醒：")
    for r in first:
        print(f"  [{r.tag:>4}] {r.title}{'   ← 加急' if r.urgent else ''}")
    print()

    check("刚出现的新作业会播报", "new" in tags_of(first, "a_new1h"))
    check("40 分钟后截止 -> h1 且标记加急",
          tags_of(first, "a_new1h") == ["h1", "new"]
          and pick(first, "a_new1h", "h1").urgent)
    check("5 小时后截止 -> h6", tags_of(first, "a_5h") == ["h6"])
    check("20 小时后截止 -> h24", tags_of(first, "a_20h") == ["h24"])
    check("还有 3 天 -> 不打扰", tags_of(first, "a_far") == [])
    check("已过期 -> 不打扰", tags_of(first, "a_overdue") == [])
    check("已提交 -> 不打扰", tags_of(first, "a_done") == [])
    check("刚出现的新考试会播报", "new" in tags_of(first, "e_2h"))
    check("2 小时后开考 -> e24", tags_of(first, "e_2h") == ["e24", "new"])
    check("30 分钟后开考 -> e1 且加急",
          tags_of(first, "e_30m") == ["e1"] and pick(first, "e_30m", "e1").urgent)
    check("新通知默认关闭 -> 不播报", tags_of(first, "n1") == [])

    quiet_from, quiet_to = (int(x) for x in cfg["remind"]["quiet_hours"])
    quiet_now = now.replace(hour=quiet_from, minute=0)   # 固定造一个免打扰时刻
    urgent_n = sum(1 for r in first if r.urgent)
    print(f"免打扰设置：{quiet_from} 点 ~ {quiet_to} 点"
          f"　（现在 {now.hour} 点："
          f"{'在免打扰时段内' if Notifier.in_quiet_hours(now, cfg) else '不在免打扰时段'}）")
    print(f"  免打扰时段内只会弹加急的 {urgent_n} 条，"
          f"其余 {len(first) - urgent_n} 条留到白天再补")
    unsent = _flush_count(notifier, first, quiet_now, force=False)
    check("免打扰时段只放行加急提醒", unsent == urgent_n,
          f"应发 {urgent_n} 条 / 实发 {unsent} 条")
    check("非加急的没有被记账（白天还会补发）",
          pick(notifier.scan(now), "a_5h", "h6") is not None)

    # 强制发完剩下的，验证"同一档只响一次"
    _flush_count(notifier, notifier.scan(now), now, force=True)
    second = notifier.scan(now)
    check("同一档只提醒一次（第二轮为空）", second == [], f"第二轮 {len(second)} 条")

    # 档位递进：20h 的那条日程提前到 5h，应补一次 h6（h24 不重复）
    store.upsert_assignments([{"key": "a_20h", "course": "计算机网络",
                               "title": "期中论文", "status": "未提交",
                               "done": False,
                               "deadline_dt": (now + timedelta(hours=5))
                               .isoformat(timespec="minutes")}])
    third = notifier.scan(now)
    check("进入更近的一档会补提醒（h24 -> h6）",
          tags_of(third, "a_20h") == ["h6"], str(tags_of(third, "a_20h")))
    _flush_count(notifier, third, now, force=True)

    # 打开"新通知提醒"开关，通知应该能播报
    config.update("remind", new_notice=True)
    store.upsert_notices([{"notice_id": "n2", "title": "实验室开放时间调整",
                           "content": "本周六设备检修，暂停开放。",
                           "sender": "实验中心", "isread": False}])
    fourth = notifier.scan(now)
    check("打开开关后新通知会播报", tags_of(fourth, "n2") == ["notice"],
          str(tags_of(fourth, "n2")))
    _flush_count(notifier, fourth, now, force=True)


def _flush_count(notifier: Notifier, reminders, now: datetime, force: bool) -> int:
    """flush 但不真的弹窗（阶段一只看规则）。"""
    real, notifier.toast = notifier.toast, (lambda *a, **k: None)
    try:
        return notifier.flush(reminders, force=force, now=now)
    finally:
        notifier.toast = real


def phase_two(store: Store, now: datetime) -> None:
    print()
    print("=" * 68)
    print("阶段二：真弹窗（走程序的同一条路：QSystemTrayIcon.showMessage）")
    print("=" * 68)
    tmp = Path(tempfile.mkdtemp(prefix="xxt-notify-demo-"))
    config.update("remind", new_notice=True)

    def dt(**kw) -> str:
        return (now + timedelta(**kw)).isoformat(timespec="minutes")

    store.upsert_assignments([
        {"key": "d_new", "course": "计算机组成原理",
         "title": "实验四 整体调试实验报告", "status": "未提交", "done": False,
         "deadline_dt": dt(minutes=38)},
        {"key": "d_urgent", "course": "高等数学", "title": "随堂测验（第 5 周）",
         "status": "未提交", "done": False, "deadline_dt": dt(minutes=25)},
    ])
    store.upsert_exams([
        {"key": "d_exam", "name": "《数据结构》期末机考", "course": "数据结构",
         "exam_time": "10-15 19:00 ~ 10-15 21:00", "duration": "120",
         "answer_status": "未提交", "start_dt": dt(minutes=30)},
    ])
    store.upsert_notices([
        {"notice_id": "d_notice", "title": "关于期中教学检查的通知",
         "content": "各学院：定于第 9-10 周开展期中教学检查。",
         "sender": "教务处", "isread": False},
    ])
    backdate(store, "assignments", "key", now, {"d_urgent": 3 * 1440})
    backdate(store, "exams", "key", now, {"d_exam": 3 * 1440})

    icon = QSystemTrayIcon(make_icon(), None)
    icon.setToolTip("学习通小助手（提醒测试）")
    icon.show()

    app = QApplication.instance()
    for _ in range(10):
        app.processEvents()
        time.sleep(0.1)

    def toast(title: str, body: str, urgent: bool) -> None:
        icon.showMessage(
            "【测试】" + title, body,
            QSystemTrayIcon.MessageIcon.Warning if urgent
            else QSystemTrayIcon.MessageIcon.Information,
            12000 if urgent else 8000)

    notifier = Notifier(store, toast, journal_path=tmp / "notify_state.json")
    found = notifier.scan(now)
    jobs = [r for r in (
        pick(found, "d_new", "new"),        # 新作业
        pick(found, "d_urgent", "h1"),      # 1 小时内截止（加急）
        pick(found, "d_exam", "e1"),        # 30 分钟后开考（加急）
        pick(found, "d_notice", "notice"),  # 新通知
    ) if r is not None]

    print(f"准备弹出 {len(jobs)} 条系统通知，请看屏幕右下角：")
    for rem in jobs:
        print(f"  · {rem.title}{'   ← 加急' if rem.urgent else ''}")
        notifier.flush([rem], force=True, now=now)
        for _ in range(12):
            app.processEvents()
            time.sleep(0.1)

    check("四种提醒都算出来了", len(jobs) == 4, f"实际 {len(jobs)} 条")
    print()
    print("通知已发出，停留 10 秒让你看清（也会留在系统通知中心）…")
    for _ in range(100):
        app.processEvents()
        time.sleep(0.1)
    icon.hide()


def main() -> int:
    now = datetime.now().replace(second=0, microsecond=0)
    tmp = Path(tempfile.mkdtemp(prefix="xxt-notify-"))
    config.CONFIG_PATH = tmp / "config.json"
    config.CREDENTIAL_PATH = tmp / "credentials.bin"
    config.LOG_PATH = tmp / "app.log"
    config.load()
    config.save()

    app = QApplication(sys.argv[:1])

    store = Store(tmp / "notify.db")
    seed(store, now)
    notifier = Notifier(store, lambda *a: None, journal_path=tmp / "state.json")
    phase_one(store, notifier, now)

    try:
        phase_two(store, now)
    finally:
        store.close()

    print()
    print("=" * 68)
    print("全部通过" if not failures else f"失败 {len(failures)} 项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
