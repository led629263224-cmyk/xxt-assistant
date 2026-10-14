"""SQLite 存储与增量 diff。

核心职责：
    1. 把采集到的作业/考试/通知落库
    2. 通过 first_seen 判断"哪些是新出现的"（新作业提醒就靠它）
    3. 记录每份作业已经提醒过哪几档，避免重复打扰
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger("xxt.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    key           TEXT PRIMARY KEY,
    course        TEXT,
    courseid      TEXT,
    clazzid       TEXT,
    title         TEXT,
    status        TEXT,
    url           TEXT,
    start_time    TEXT,
    deadline      TEXT,
    deadline_dt   TEXT,
    score         TEXT,
    question_count INTEGER,
    full_score    TEXT,
    done          INTEGER DEFAULT 0,
    detail_at     TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    reminded      TEXT DEFAULT '{}',
    hidden        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exams (
    key           TEXT PRIMARY KEY,
    name          TEXT,
    course        TEXT,
    exam_time     TEXT,
    start_dt      TEXT,
    duration      TEXT,
    exam_status   TEXT,
    answer_status TEXT,
    score         TEXT,
    method        TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    reminded      TEXT DEFAULT '{}',
    hidden        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notices (
    notice_id    TEXT PRIMARY KEY,
    title        TEXT,
    content      TEXT,
    sender       TEXT,
    publish_time TEXT,
    isread       INTEGER DEFAULT 0,
    first_seen   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE INDEX IF NOT EXISTS idx_assign_deadline ON assignments(deadline_dt);
CREATE INDEX IF NOT EXISTS idx_notices_read ON notices(isread, publish_time);
"""

# 老库升级用：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列
_MIGRATIONS = (
    ("assignments", "hidden", "INTEGER DEFAULT 0"),
    ("exams", "hidden", "INTEGER DEFAULT 0"),
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str | None = None):
        config.ensure_dirs()
        self.path = Path(path or config.DB_PATH)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给旧数据库补上后加的列（用户已经跑过一次的库不会重建表）。"""
        for table, column, decl in _MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                log.info("数据库升级：%s 增加列 %s", table, column)
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -------------------------------------------------- 通用

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    def set_meta(self, key: str, value) -> None:
        self._exec("INSERT INTO meta(k, v) VALUES(?, ?) "
                   "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                   (key, json.dumps(value, ensure_ascii=False)))

    def get_meta(self, key: str, default=None):
        rows = self._query("SELECT v FROM meta WHERE k=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["v"])
        except Exception:
            return default

    # -------------------------------------------------- 作业

    def upsert_assignments(self, rows: list[dict]) -> dict:
        """写入本次扫描到的作业，返回 {new: [key], changed: [key], seen: [key]}。"""
        now = _now()
        new_keys, changed_keys, seen = [], [], []
        for row in rows:
            key = row["key"]
            seen.append(key)
            old = self._query("SELECT * FROM assignments WHERE key=?", (key,))
            if not old:
                self._exec(
                    "INSERT INTO assignments(key, course, courseid, clazzid, title,"
                    " status, url, start_time, deadline, deadline_dt, score,"
                    " question_count, full_score, done, first_seen, last_seen)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, row.get("course"), row.get("courseid"), row.get("clazzid"),
                     row.get("title"), row.get("status"), row.get("url"),
                     row.get("start_time"), row.get("deadline"),
                     row.get("deadline_dt"), row.get("score"),
                     row.get("question_count"), row.get("full_score"),
                     1 if row.get("done") else 0, now, now))
                new_keys.append(key)
            else:
                o = old[0]
                changed = (o["status"] or "") != (row.get("status") or "") or \
                          (o["deadline_dt"] or "") != (row.get("deadline_dt") or "") or \
                          (o["done"] or 0) != (1 if row.get("done") else 0)
                self._exec(
                    "UPDATE assignments SET status=?, done=?, last_seen=?,"
                    " deadline=COALESCE(?, deadline),"
                    " deadline_dt=COALESCE(?, deadline_dt),"
                    " score=COALESCE(?, score),"
                    " question_count=COALESCE(?, question_count),"
                    " full_score=COALESCE(?, full_score),"
                    " start_time=COALESCE(?, start_time) WHERE key=?",
                    (row.get("status"), 1 if row.get("done") else 0, now,
                     row.get("deadline"), row.get("deadline_dt"), row.get("score"),
                     row.get("question_count"), row.get("full_score"),
                     row.get("start_time"), key))
                if changed:
                    changed_keys.append(key)
        return {"new": new_keys, "changed": changed_keys, "seen": seen}

    def touch_detail(self, key: str) -> None:
        self._exec("UPDATE assignments SET detail_at=? WHERE key=?", (_now(), key))

    _DETAIL_FIELDS = ("start_time", "deadline", "deadline_dt", "score",
                      "question_count", "full_score", "title", "status", "done")

    def update_assignment_detail(self, key: str, data: dict) -> None:
        """把作业详情页解析出的字段合并进已有记录（只覆盖有值的字段）。"""
        sets, params = [], []
        for name in self._DETAIL_FIELDS:
            value = data.get(name)
            if value not in (None, ""):
                sets.append(f"{name}=?")
                params.append(1 if (name == "done" and value) else value)
        sets.append("detail_at=?")
        params.append(_now())
        params.append(key)
        self._exec(f"UPDATE assignments SET {', '.join(sets)} WHERE key=?", params)

    def assignments_needing_detail(self, older_than_hours: float,
                                   limit: int = 10) -> list[dict]:
        """取出需要（重新）拉详情的未完成作业：没拉过或已过期太久。"""
        rows = self._query(
            "SELECT * FROM assignments WHERE done=0 "
            "ORDER BY CASE WHEN deadline_dt IS NULL OR deadline_dt='' THEN 1 ELSE 0 END,"
            " deadline_dt ASC")
        out = []
        for row in rows:
            age = self.detail_age_hours(row["key"])
            if age is None or age >= older_than_hours:
                out.append(dict(row))
            if len(out) >= limit:
                break
        return out

    def detail_age_hours(self, key: str) -> float | None:
        rows = self._query("SELECT detail_at FROM assignments WHERE key=?", (key,))
        if not rows or not rows[0]["detail_at"]:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(rows[0]["detail_at"])
                    ).total_seconds() / 3600.0
        except Exception:
            return None

    def pending_assignments(self) -> list[dict]:
        """未完成的作业，按截止时间升序（截止未知排最后）。被移除的不返回。"""
        rows = self._query(
            "SELECT * FROM assignments WHERE done=0 AND COALESCE(hidden,0)=0 "
            "ORDER BY CASE WHEN deadline_dt IS NULL OR deadline_dt='' THEN 1 ELSE 0 END,"
            " deadline_dt ASC")
        return [dict(r) for r in rows]

    def all_assignments(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM assignments")]

    def mark_reminded(self, key: str, tag: str, at: str | None = None) -> None:
        rows = self._query("SELECT reminded FROM assignments WHERE key=?", (key,))
        data = {}
        if rows and rows[0]["reminded"]:
            try:
                data = json.loads(rows[0]["reminded"])
            except Exception:
                data = {}
        data[tag] = at or _now()
        self._exec("UPDATE assignments SET reminded=? WHERE key=?",
                   (json.dumps(data, ensure_ascii=False), key))

    def reminded(self, key: str) -> dict:
        rows = self._query("SELECT reminded FROM assignments WHERE key=?", (key,))
        if not rows or not rows[0]["reminded"]:
            return {}
        try:
            return json.loads(rows[0]["reminded"])
        except Exception:
            return {}

    # -------------------------------------------------- 考试

    def upsert_exams(self, rows: list[dict]) -> dict:
        now = _now()
        new_keys, changed_keys, seen = [], [], []
        for row in rows:
            key = row["key"]
            seen.append(key)
            old = self._query("SELECT * FROM exams WHERE key=?", (key,))
            if not old:
                self._exec(
                    "INSERT INTO exams(key, name, course, exam_time, start_dt,"
                    " duration, exam_status, answer_status, score, method,"
                    " first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, row.get("name"), row.get("course"), row.get("exam_time"),
                     row.get("start_dt"), row.get("duration"), row.get("exam_status"),
                     row.get("answer_status"), row.get("score"), row.get("method"),
                     now, now))
                new_keys.append(key)
            else:
                o = old[0]
                changed = (o["answer_status"] or "") != (row.get("answer_status") or "") or \
                          (o["exam_status"] or "") != (row.get("exam_status") or "")
                self._exec(
                    "UPDATE exams SET answer_status=?, exam_status=?, score=?,"
                    " exam_time=?, start_dt=?, last_seen=? WHERE key=?",
                    (row.get("answer_status"), row.get("exam_status"),
                     row.get("score"), row.get("exam_time"), row.get("start_dt"),
                     now, key))
                if changed:
                    changed_keys.append(key)
        return {"new": new_keys, "changed": changed_keys, "seen": seen}

    def unfinished_exams(self) -> list[dict]:
        rows = self._query(
            "SELECT * FROM exams WHERE COALESCE(hidden,0)=0"
            " AND answer_status NOT LIKE '%已提交%'"
            " AND answer_status NOT LIKE '%已交卷%'"
            " AND answer_status NOT LIKE '%已完成%'"
            " ORDER BY CASE WHEN start_dt IS NULL OR start_dt='' THEN 1 ELSE 0 END,"
            " start_dt ASC")
        return [dict(r) for r in rows]

    def all_exams(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM exams")]

    def mark_exam_reminded(self, key: str, tag: str, at: str | None = None) -> None:
        rows = self._query("SELECT reminded FROM exams WHERE key=?", (key,))
        data = {}
        if rows and rows[0]["reminded"]:
            try:
                data = json.loads(rows[0]["reminded"])
            except Exception:
                data = {}
        data[tag] = at or _now()
        self._exec("UPDATE exams SET reminded=? WHERE key=?",
                   (json.dumps(data, ensure_ascii=False), key))

    def exam_reminded(self, key: str) -> dict:
        rows = self._query("SELECT reminded FROM exams WHERE key=?", (key,))
        if not rows or not rows[0]["reminded"]:
            return {}
        try:
            return json.loads(rows[0]["reminded"])
        except Exception:
            return {}

    # -------------------------------------------------- 用户手动移除

    def hide_assignment(self, key: str) -> None:
        """把一条作业从任务清单里移除（只影响本机显示，不动学习通上的数据）。"""
        self._exec("UPDATE assignments SET hidden=1 WHERE key=?", (key,))

    def hide_exam(self, key: str) -> None:
        self._exec("UPDATE exams SET hidden=1 WHERE key=?", (key,))

    def hidden_count(self) -> int:
        rows = self._query(
            "SELECT (SELECT COUNT(*) FROM assignments WHERE COALESCE(hidden,0)=1)"
            " + (SELECT COUNT(*) FROM exams WHERE COALESCE(hidden,0)=1) AS n")
        return int(rows[0]["n"]) if rows else 0

    def restore_hidden(self) -> int:
        """恢复全部被移除的条目，返回恢复条数。"""
        n = self.hidden_count()
        self._exec("UPDATE assignments SET hidden=0 WHERE COALESCE(hidden,0)=1")
        self._exec("UPDATE exams SET hidden=0 WHERE COALESCE(hidden,0)=1")
        return n

    # -------------------------------------------------- 通知

    def upsert_notices(self, rows: list[dict]) -> list[dict]:
        """写入通知，返回本次新入库的通知（用于"有新通知"提醒）。"""
        now = _now()
        fresh = []
        for row in rows:
            nid = row["notice_id"]
            old = self._query("SELECT notice_id, isread FROM notices WHERE notice_id=?",
                              (nid,))
            if not old:
                self._exec(
                    "INSERT INTO notices(notice_id, title, content, sender,"
                    " publish_time, isread, first_seen) VALUES(?,?,?,?,?,?,?)",
                    (nid, row.get("title"), row.get("content"), row.get("sender"),
                     row.get("publish_time"), 1 if row.get("isread") else 0, now))
                if not row.get("isread"):
                    fresh.append(row)
            else:
                self._exec("UPDATE notices SET isread=? WHERE notice_id=?",
                           (1 if row.get("isread") else 0, nid))
        return fresh

    def notices(self, limit: int = 5, unread_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM notices"
        if unread_only:
            sql += " WHERE isread=0"
        sql += " ORDER BY publish_time DESC, first_seen DESC LIMIT ?"
        return [dict(r) for r in self._query(sql, (limit,))]

    def notice_stats(self) -> tuple[int, int]:
        total = self._query("SELECT COUNT(*) AS c FROM notices")[0]["c"]
        unread = self._query("SELECT COUNT(*) AS c FROM notices WHERE isread=0")[0]["c"]
        return total, unread
