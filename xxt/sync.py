"""同步编排：拉取 -> 落库 -> 算提醒。

同步是串行 + 限速的，一次完整同步的耗时主要花在作业详情页上（每门课约十几秒），
所以详情只在"没拉过或过期超过 detail_refresh_hours"时才拉，且每次最多若干条。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .client import ChaoxingClient, LoginResult, NotLoggedIn, XxtError
from .notify import Notifier
from .parse import (assignment_key, exam_key, is_exam_unfinished,
                    is_homework_pending, nearest_datetime_for)
from .store import Store

log = logging.getLogger("xxt.sync")

MAX_DETAIL_PER_SYNC = 8


@dataclass
class SyncReport:
    started_at: datetime
    finished_at: datetime | None = None
    ok: bool = True
    courses: int = 0
    scanned: int = 0
    new_assignments: list[dict] = field(default_factory=list)
    new_exams: list[dict] = field(default_factory=list)
    new_notices: list[dict] = field(default_factory=list)
    reminded: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> str:
        when = self.finished_at or datetime.now()
        head = "同步成功" if self.ok else "同步失败"
        text = (f"{head}　{when:%H:%M:%S}　课程 {self.courses}　作业 {self.scanned}"
                f"　新增 {len(self.new_assignments)}　耗时 {self.duration:.0f}s")
        if self.reminded:
            text += f"　提醒 {self.reminded}"
        return text


class SyncService:
    def __init__(self, store: Store, client: ChaoxingClient, notifier: Notifier,
                 on_report=None):
        self.store = store
        self.client = client
        self.notifier = notifier
        self.on_report = on_report
        self.last_report: SyncReport | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = threading.Lock()
        self.next_run_at: datetime | None = None

    # -------------------------------------------------- 登录

    def login(self, phone: str, password: str, remember: bool = True) -> LoginResult:
        res = self.client.login(phone, password)
        if res.ok and remember:
            config.save_credentials(phone, password)
        return res

    def ensure_login(self) -> LoginResult:
        return self.client.ensure_login()

    # -------------------------------------------------- 单次同步

    def sync_once(self, refresh_details: bool = True,
                  announce_new: bool | None = None) -> SyncReport:
        report = SyncReport(started_at=datetime.now())
        if not self._running.acquire(blocking=False):
            report.ok = False
            report.errors.append("已有同步在进行中")
            report.finished_at = datetime.now()
            return report
        try:
            self._do_sync(report, refresh_details, announce_new)
        except NotLoggedIn as exc:
            report.ok = False
            report.errors.append(str(exc))
        except Exception as exc:  # pragma: no cover
            log.exception("同步异常")
            report.ok = False
            report.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            report.finished_at = datetime.now()
            self.last_report = report
            self._persist(report)
            self._running.release()
            if report.ok:
                log.info("%s", report.summary())
            else:
                log.warning("同步失败：%s", "；".join(report.errors[:2]) or "未知原因")
            if self.on_report:
                try:
                    self.on_report(report)
                except Exception:  # pragma: no cover
                    log.exception("同步回调异常")
        return report

    def _do_sync(self, report: SyncReport, refresh_details: bool,
                 announce_new: bool | None) -> None:
        login = self.client.ensure_login()
        if not login.ok:
            raise NotLoggedIn(login.msg)

        cfg = config.load()
        first_run = not self.store.get_meta("sync_count")

        profile = self.client.fetch_profile()
        self.store.set_meta("profile", profile)

        courses = self.client.fetch_courses()
        report.courses = len(courses)
        if not courses:
            report.notes.append("未获取到任何课程")

        all_rows: list[dict] = []
        for course in courses:
            try:
                data = self.client.fetch_homework_list(course)
            except XxtError as exc:
                report.errors.append(f"{course['name']}：{exc}")
                continue
            except Exception as exc:  # pragma: no cover
                report.errors.append(f"{course['name']}：{type(exc).__name__} {exc}")
                continue
            for item in data["items"]:
                key = assignment_key(course["courseid"], course["clazzid"],
                                     item["url"], item["title"])
                all_rows.append({
                    "key": key,
                    "course": course["name"],
                    "courseid": course["courseid"],
                    "clazzid": course["clazzid"],
                    "title": item["title"],
                    "status": item["status"],
                    "url": item["url"],
                    "done": not is_homework_pending(item["status"]),
                })
        report.scanned = len(all_rows)
        diff = self.store.upsert_assignments(all_rows)
        for key in diff["new"]:
            row = self._assignment(key)
            if row:
                report.new_assignments.append(row)
        if refresh_details:
            self._refresh_details(report, cfg)

        self._sync_exams(report)
        self._sync_notices(report, cfg)

        announce = (not first_run) if announce_new is None else announce_new
        reminders = self.notifier.scan(announce_new=announce)
        report.reminded = self.notifier.flush(reminders)
        self.store.set_meta("sync_count", int(self.store.get_meta("sync_count", 0)) + 1)
        # 把这一轮的会话 cookie 与课程签名一起存下来，下次启动直接复用，
        # 省掉重拉 49 个课程页（约一半同步时间）。
        self.client.save_cookies()
        self.client.save_signature_cache()

    def _refresh_details(self, report: SyncReport, cfg: dict) -> None:
        hours = float(cfg.get("sync", {}).get("detail_refresh_hours", 6) or 6)
        targets = self.store.assignments_needing_detail(hours, limit=MAX_DETAIL_PER_SYNC)
        for row in targets:
            try:
                detail = self.client.fetch_homework_detail(row["url"])
            except Exception as exc:
                log.info("详情拉取失败 %s：%s", row["title"], exc)
                continue
            data = {k: v for k, v in detail.items() if k != "title"}
            if detail.get("deadline"):
                dt = nearest_datetime_for(detail["deadline"], datetime.now().date())
                if dt:
                    data["deadline_dt"] = dt.isoformat(timespec="minutes")
            if not data.get("deadline") and not data.get("start_time"):
                # 详情页没有时间信息：标记已尝试，避免每次同步重复拉
                self.store.touch_detail(row["key"])
                continue
            self.store.update_assignment_detail(row["key"], data)

    def _sync_exams(self, report: SyncReport) -> None:
        try:
            exams = self.client.fetch_exams()
        except Exception as exc:
            report.errors.append(f"考试列表：{exc}")
            return
        rows = []
        for exam in exams:
            if not is_exam_unfinished(exam):
                continue
            rows.append({
                "key": exam_key(exam),
                "name": exam["name"],
                "course": exam.get("course"),
                "exam_time": exam.get("exam_time"),
                "start_dt": (exam.get("start_dt").isoformat(timespec="minutes")
                             if exam.get("start_dt") else None),
                "duration": exam.get("duration"),
                "exam_status": exam.get("exam_status"),
                "answer_status": exam.get("answer_status"),
                "score": exam.get("score"),
                "method": exam.get("method"),
            })
        diff = self.store.upsert_exams(rows)
        for key in diff["new"]:
            for row in self.store.unfinished_exams():
                if row["key"] == key:
                    report.new_exams.append(row)
                    break

    def _sync_notices(self, report: SyncReport, cfg: dict) -> None:
        ncfg = cfg.get("notices", {})
        if not ncfg.get("enabled", True):
            return
        try:
            notices = self.client.fetch_notices(limit=int(ncfg.get("limit", 30)))
        except Exception as exc:
            report.errors.append(f"通知中心：{exc}")
            return
        report.new_notices = self.store.upsert_notices(notices)

    def _assignment(self, key: str) -> dict | None:
        rows = self.store._query("SELECT * FROM assignments WHERE key=?", (key,))
        return dict(rows[0]) if rows else None

    def _persist(self, report: SyncReport) -> None:
        self.store.set_meta("last_sync_at", (report.finished_at or datetime.now()
                                            ).isoformat(timespec="seconds"))
        self.store.set_meta("last_sync_ok", report.ok)
        self.store.set_meta("last_sync_summary", report.summary())
        self.store.set_meta("last_errors", report.errors[:5])

    # -------------------------------------------------- 后台循环

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xxt-sync",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def trigger(self) -> None:
        """立刻唤醒一次同步（手动刷新按钮用）。"""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            cfg = config.load()
            interval = max(5, int(cfg.get("sync", {}).get("interval_minutes", 30)))
            jitter = max(0, int(cfg.get("sync", {}).get("jitter_seconds", 90)))
            wait = interval * 60 + random.randint(0, jitter)
            if self.last_report is None:
                wait = 1  # 启动后先跑一次
            self.next_run_at = datetime.fromtimestamp(time.time() + wait)
            if self._wake.wait(timeout=wait):
                self._wake.clear()
                if self._stop.is_set():
                    break
            if self._stop.is_set():
                break
            self.sync_once()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def busy(self) -> bool:
        """当前是否有同步任务在跑（UI 用来禁用刷新按钮）。"""
        return self._running.locked()
