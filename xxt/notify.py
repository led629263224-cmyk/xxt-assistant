"""提醒规则引擎。

设计目标是"有用但不烦人"：
    * 同一档提醒只发一次（状态记在数据库 assignments.reminded / exams.reminded）
    * 截止提醒按阈值分档（默认 24h / 6h / 1h），进入更近的一档才再提醒一次
    * 已经过期的作业不再提醒（卡片上也不再展示，要看去学习通网站）
    * 深夜免打扰（默认 23:00-07:00），期间只累积不弹窗
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import config
from .parse import humanize_delta

log = logging.getLogger("xxt.notify")

JOURNAL_PATH = config.DATA_DIR / "notify_state.json"


@dataclass
class Reminder:
    kind: str          # homework / exam / notice
    key: str           # 作业/考试主键或通知 id
    tag: str           # 本次提醒的档位标签，用于去重
    title: str
    body: str
    urgent: bool = False
    payload: dict = field(default_factory=dict)


class Notifier:
    def __init__(self, store, toast: Callable[[str, str, bool], None],
                 journal_path: Path | None = None):
        """toast(title, body, urgent) 由托盘层注入。

        journal_path 用于隔离测试，默认写到 data/notify_state.json。
        """
        self.store = store
        self.toast = toast
        self.journal_path = Path(journal_path) if journal_path else JOURNAL_PATH
        self.journal = self._load_journal()

    # -------------------------------------------------- 去重日志

    def _load_journal(self) -> dict:
        if self.journal_path.exists():
            try:
                return json.loads(self.journal_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_journal(self) -> None:
        try:
            config.ensure_dirs()
            cutoff = (datetime.now() - timedelta(days=14)).isoformat()
            self.journal = {k: v for k, v in self.journal.items() if v > cutoff}
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_text(json.dumps(self.journal, ensure_ascii=False,
                                                    indent=2), encoding="utf-8")
        except Exception as exc:  # pragma: no cover
            log.warning("写提醒日志失败：%s", exc)

    def _sent(self, key: str, tag: str) -> bool:
        return f"{key}|{tag}" in self.journal

    def _mark(self, key: str, tag: str) -> None:
        self.journal[f"{key}|{tag}"] = datetime.now().isoformat(timespec="seconds")

    # -------------------------------------------------- 规则

    def scan(self, now: datetime | None = None,
             announce_new: bool = True) -> list[Reminder]:
        cfg = config.load()
        rcfg = cfg.get("remind", {})
        if not rcfg.get("enabled", True):
            return []
        now = now or datetime.now()
        out: list[Reminder] = []
        out += self._near_deadlines(now, rcfg, announce_new)
        out += self._exams(now, rcfg, announce_new)
        out += self._notices(now, rcfg, announce_new)
        return out

    def _near_deadlines(self, now: datetime, rcfg: dict,
                        announce_new: bool) -> list[Reminder]:
        thresholds = sorted({int(h) for h in rcfg.get("threshold_hours", [24, 6, 1])},
                            reverse=True)
        out: list[Reminder] = []
        for row in self.store.pending_assignments():
            key, title = row["key"], row["title"] or "未命名作业"
            course = row["course"] or ""
            age_min = _age_minutes(row.get("first_seen"), now)
            if announce_new and rcfg.get("new_homework", True) and age_min is not None \
                    and age_min <= 20 and not self._sent(key, "new") \
                    and "new" not in self.store.reminded(key):
                out.append(Reminder(
                    "homework", key, "new", f"新作业：{title}",
                    f"{course}\n{humanize_delta(_deadline_dt(row, now) - now)}截止"
                    if row.get("deadline_dt") else f"{course}\n尚未公布截止时间",
                    urgent=False, payload=dict(row)))
            dl = _deadline_dt(row, now)
            if dl is None:
                continue
            remain_h = (dl - now).total_seconds() / 3600.0
            if remain_h <= 0:
                continue
            bucket = next((t for t in reversed(thresholds) if remain_h <= t), None)
            if bucket is None:
                continue
            tag = f"h{bucket}"
            if self._sent(key, tag) or tag in self.store.reminded(key):
                continue
            out.append(Reminder(
                "homework", key, tag,
                f"{bucket} 小时内截止：{title}",
                f"{course}\n{humanize_delta(dl - now)}（{row.get('deadline')}）",
                urgent=bucket <= 1, payload=dict(row)))
        return out

    def _exams(self, now: datetime, rcfg: dict, announce_new: bool) -> list[Reminder]:
        leads = sorted({int(h) for h in rcfg.get("exam_lead_hours", [24, 1])}, reverse=True)
        out: list[Reminder] = []
        for row in self.store.unfinished_exams():
            key, name = row["key"], row["name"] or "未命名考试"
            course = row.get("course") or ""
            if announce_new and rcfg.get("new_exam", True):
                age_min = _age_minutes(row.get("first_seen"), now)
                if age_min is not None and age_min <= 20 and not self._sent(key, "new") \
                        and "new" not in self.store.exam_reminded(key):
                    out.append(Reminder(
                        "exam", key, "new", f"新考试安排：{name}",
                        f"{course}\n{row.get('exam_time') or ''}　{row.get('answer_status') or ''}",
                        urgent=False, payload=dict(row)))
            start = _parse_dt(row.get("start_dt"))
            if start is None:
                continue
            remain_h = (start - now).total_seconds() / 3600.0
            if remain_h <= 0:
                continue
            bucket = next((t for t in reversed(leads) if remain_h <= t), None)
            if bucket is None:
                continue
            tag = f"e{bucket}"
            if self._sent(key, tag) or tag in self.store.exam_reminded(key):
                continue
            out.append(Reminder(
                "exam", key, tag, f"{bucket} 小时后开考：{name}",
                f"{course}\n{row.get('exam_time') or ''}　时长 {row.get('duration') or '?'} 分钟",
                urgent=bucket <= 1, payload=dict(row)))
        return out

    def _notices(self, now: datetime, rcfg: dict, announce_new: bool) -> list[Reminder]:
        if not (announce_new and rcfg.get("new_notice", False)):
            return []
        out: list[Reminder] = []
        for row in self.store.notices(limit=8, unread_only=True):
            nid = row["notice_id"]
            age_min = _age_minutes(row.get("first_seen"), now)
            if age_min is None or age_min > 20:
                continue
            if self._sent(nid, "notice"):
                continue
            body = (row.get("content") or "")[:120].replace("\n", " ")
            out.append(Reminder(
                "notice", nid, "notice", f"新通知：{row['title']}",
                f"{row.get('sender') or ''}\n{body}", urgent=False, payload=dict(row)))
        return out

    # -------------------------------------------------- 发送

    @staticmethod
    def in_quiet_hours(now: datetime, cfg: dict | None = None) -> bool:
        """深夜免打扰（默认 23:00-07:00，可用 remind.quiet_hours 覆盖）。"""
        cfg = cfg or config.load()
        window = cfg.get("remind", {}).get("quiet_hours", [23, 7])
        try:
            start, end = int(window[0]), int(window[1])
        except Exception:
            return False
        if start == end:
            return False
        hour = now.hour
        if start > end:            # 跨零点，如 23 点到次日 7 点
            return hour >= start or hour < end
        return start <= hour < end  # 同一天内，如 13 点到 15 点

    def flush(self, reminders: list[Reminder], force: bool = False,
              now: datetime | None = None) -> int:
        """发送提醒。免打扰时段内只有 urgent 的会立刻弹，其余排队下次再说。

        now 参数只为测试注入固定时间，正常调用不用传。
        """
        if not reminders:
            return 0
        now = now or datetime.now()
        quiet = self.in_quiet_hours(now) and not force
        sent = 0
        for rem in reminders:
            if quiet and not rem.urgent:
                continue
            try:
                self.toast(rem.title, rem.body, rem.urgent)
            except Exception as exc:  # pragma: no cover
                log.warning("弹窗失败：%s", exc)
                continue
            log.info("已弹提醒[%s]：%s", rem.tag, rem.title)
            self._mark(rem.key, rem.tag)
            stamp = now.isoformat(timespec="seconds")
            if rem.kind == "homework":
                self.store.mark_reminded(rem.key, rem.tag, at=stamp)
            elif rem.kind == "exam":
                self.store.mark_exam_reminded(rem.key, rem.tag, at=stamp)
            sent += 1
        if sent:
            self._save_journal()
        return sent


def _deadline_dt(row: dict, now: datetime) -> datetime | None:
    """优先用已解析好的 deadline_dt，其次现场解析 deadline 文本。"""
    dt = _parse_dt(row.get("deadline_dt"))
    if dt:
        return dt
    from .parse import nearest_datetime_for
    return nearest_datetime_for(row.get("deadline") or "", now.date())


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _age_minutes(first_seen, now: datetime) -> float | None:
    dt = _parse_dt(first_seen)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 60.0
