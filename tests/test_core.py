"""配置与解析层的单元测试（不需要联网、不需要 GUI）。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xxt import crypto  # noqa: E402
from xxt.parse import (aes_decrypt, aes_encrypt, assignment_key,  # noqa: E402
                       humanize_delta, is_exam_unfinished, is_homework_pending,
                       nearest_datetime_for, parse_exams, parse_full_datetime,
                       parse_homework_detail, parse_homework_list, parse_notices,
                       strip_tags)


class TestAes(unittest.TestCase):
    def test_roundtrip(self):
        for text in ("13800138000", "p@ssw0rd!中文", "a" * 32):
            self.assertEqual(aes_decrypt(aes_encrypt(text)), text)

    def test_blocksize(self):
        # AES-CBC + PKCS7：密文长度应为 16 的整数倍
        import base64
        raw = base64.b64decode(aes_encrypt("1234"))
        self.assertEqual(len(raw) % 16, 0)

    def test_known_shape(self):
        token = aes_encrypt("13800138000")
        self.assertEqual(len(token), 24)
        self.assertNotIn("13800138000", token)


class TestStripTags(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(strip_tags("<p>你好&nbsp;<b>世界</b></p>"), "你好 世界")

    def test_script_removed(self):
        self.assertEqual(strip_tags("<script>var a=1;</script>正文"), "正文")


class TestCoursesAndWorkList(unittest.TestCase):
    HTML = """
    <div class="course">
      <a href="/visit/stucoursemiddle?courseid=111&clazzid=222&cpi=333&ismooc2=1">
        <span class="course-name" title="数字电路">数字电路</span></a>
      <p class="overHidden2 fl">王老师</p>
    </div>
    """

    def test_parse_courses(self):
        from xxt.parse import parse_courses
        courses = parse_courses(self.HTML)
        self.assertEqual(len(courses), 1)
        c = courses[0]
        self.assertEqual((c["courseid"], c["clazzid"], c["cpi"]), ("111", "222", "333"))
        self.assertEqual(c["name"], "数字电路")
        self.assertEqual(c["teacher"], "王老师")

    def test_parse_courses_empty(self):
        from xxt.parse import parse_courses
        self.assertEqual(parse_courses(""), [])

    WORK_HTML = """
    <div class="work-total"><span>1/3</span></div></div>
    <ul>
      <li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc2/work/view?courseId=1&amp;workId=999&amp;enc=abc">
        <p class="overHidden2 fl">第三章 作业</p>
        <p class="status fl">未交</p>
      </li>
      <li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc2/work/view?courseId=1&amp;workId=1000">
        <p class="overHidden2 fl">第二章 作业</p>
        <p class="status fl">已完成</p>
      </li>
    </ul>
    """

    def test_parse_work_list(self):
        data = parse_homework_list(self.WORK_HTML)
        self.assertEqual(data["progress"], (1, 3))
        self.assertEqual(len(data["items"]), 2)
        first = data["items"][0]
        self.assertEqual(first["title"], "第三章 作业")
        self.assertEqual(first["status"], "未交")
        self.assertIn("workId=999", first["url"])
        self.assertNotIn("&amp;", first["url"])

    def test_pending_flag(self):
        items = parse_homework_list(self.WORK_HTML)["items"]
        self.assertTrue(is_homework_pending(items[0]["status"]))
        self.assertFalse(is_homework_pending(items[1]["status"]))
        self.assertTrue(is_homework_pending(""))          # 状态未知按未完成处理
        self.assertFalse(is_homework_pending("待批阅"))

    def test_assignment_key_stable(self):
        url = "https://mooc1.chaoxing.com/mooc2/work/view?courseId=1&workId=999"
        k1 = assignment_key("1", "2", url, "第三章 作业")
        k2 = assignment_key("1", "2", url, "第三章 作业")
        self.assertEqual(k1, k2)
        self.assertEqual(k1, "1:2:999")
        # 没有 workId 时退化为标题摘要
        k3 = assignment_key("1", "2", "https://x/", "第三章 作业")
        self.assertTrue(k3.startswith("1:2:t"))


class TestHomeworkDetail(unittest.TestCase):
    HTML = """
    <div class="tit">作业详情第三章 作业</div>
    <div>题量: 10 满分: 100.0</div>
    <div>作答时间: 03-20 08:00 至 03-27 23:59</div>
    <div>智能分析 88.5 分</div>
    """

    def test_parse_detail(self):
        d = parse_homework_detail(self.HTML)
        self.assertEqual(d["question_count"], 10)
        self.assertEqual(d["full_score"], "100.0")
        self.assertEqual(d["start_time"], "03-20 08:00")
        self.assertEqual(d["deadline"], "03-27 23:59")
        self.assertEqual(d["score"], "88.5")
        self.assertEqual(d["title"], "第三章 作业")

    def test_parse_detail_empty(self):
        d = parse_homework_detail("<html>没有权限</html>")
        self.assertIsNone(d["deadline"])
        self.assertIsNone(d["question_count"])


class TestTime(unittest.TestCase):
    def test_nearest_year_current(self):
        today = date(2026, 3, 1)
        self.assertEqual(nearest_datetime_for("03-27 23:59", today),
                         datetime(2026, 3, 27, 23, 59))

    def test_nearest_year_wrap(self):
        # 12 月看到次年 1 月的截止时间，应落到明年
        today = date(2026, 12, 28)
        self.assertEqual(nearest_datetime_for("01-05 23:59", today),
                         datetime(2027, 1, 5, 23, 59))
        # 1 月看到 12 月的截止时间，应落到去年
        today = date(2026, 1, 3)
        self.assertEqual(nearest_datetime_for("12-28 10:00", today),
                         datetime(2025, 12, 28, 10, 0))

    def test_nearest_year_bad(self):
        self.assertIsNone(nearest_datetime_for("", date(2026, 1, 1)))
        self.assertIsNone(nearest_datetime_for("不是时间", date(2026, 1, 1)))

    def test_parse_full_datetime(self):
        self.assertEqual(parse_full_datetime("2026-03-20 14:00~16:00"),
                         datetime(2026, 3, 20, 14, 0))
        self.assertIsNone(parse_full_datetime("待定"))

    def test_humanize(self):
        self.assertEqual(humanize_delta(timedelta(days=3, hours=5)), "还有 3 天 5 小时")
        self.assertEqual(humanize_delta(timedelta(hours=2)), "还有 2 小时")
        self.assertEqual(humanize_delta(timedelta(minutes=30)), "还有 30 分钟")
        self.assertTrue(humanize_delta(timedelta(hours=-2)).startswith("已过期"))
        self.assertEqual(humanize_delta(None), "截止未知")


class TestExams(unittest.TestCase):
    HTML = """
    <table><tr class="dataTr">
      <td>1</td><td>《数字电路》期中考试</td><td>2026-04-10 14:00~16:00</td>
      <td>120</td><td>未开始</td><td>未提交</td><td></td><td>线上</td>
    </tr><tr class="dataTr">
      <td>2</td><td>《大学英语》期末考试</td><td>2026-01-05 09:00~11:00</td>
      <td>120</td><td>已结束</td><td>已提交</td><td>90</td><td>线上</td>
    </tr></table>
    """

    def test_parse(self):
        rows = parse_exams(self.HTML)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "《数字电路》期中考试")
        self.assertEqual(rows[0]["course"], "数字电路")
        self.assertEqual(rows[0]["start_dt"], datetime(2026, 4, 10, 14, 0))
        self.assertEqual(rows[0]["duration"], "120")

    def test_unfinished(self):
        rows = parse_exams(self.HTML)
        self.assertTrue(is_exam_unfinished(rows[0]))
        self.assertFalse(is_exam_unfinished(rows[1]))

    def test_no_rows(self):
        self.assertEqual(parse_exams("<table></table>"), [])


class TestNotices(unittest.TestCase):
    PAYLOAD = {
        "status": True,
        "notices": {
            "list": [{"title": "关于选课", "content": "详情正文", "createrName": "教务处",
                      "completeTime": "2026-03-01 10:00", "isread": False}],
            "lastGetId": "abc",
            "lastPage": True,
        },
    }

    def test_parse(self):
        out = parse_notices(self.PAYLOAD)
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["title"], "关于选课")
        self.assertEqual(out["items"][0]["sender"], "教务处")
        self.assertTrue(out["last_page"])
        self.assertTrue(out["items"][0]["notice_id"])

    def test_empty(self):
        out = parse_notices({"status": False})
        self.assertEqual(out["items"], [])


class TestCrypto(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "DPAPI 仅 Windows 可用")
    def test_roundtrip(self):
        self.assertTrue(crypto.available())
        token = crypto.protect("13800138000:secret")
        self.assertNotIn("secret", token)
        self.assertEqual(crypto.unprotect(token), "13800138000:secret")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI 仅 Windows 可用")
    def test_bad_token(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.unprotect("这不是base64!!!")


class TestStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        from xxt.store import Store
        self.tmp = tempfile.mkdtemp()
        self.store = Store(Path(self.tmp) / "t.db")

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_assignment_diff(self):
        row = {"key": "c1:1:100", "course": "数字电路", "title": "第三章 作业",
               "status": "未交", "url": "u", "done": False}
        first = self.store.upsert_assignments([row])
        self.assertEqual(first["new"], ["c1:1:100"])
        second = self.store.upsert_assignments([row])
        self.assertEqual(second["new"], [])
        self.assertEqual(second["changed"], [])
        # 状态变化要被识别出来
        row2 = dict(row, status="已完成", done=True)
        third = self.store.upsert_assignments([row2])
        self.assertEqual(third["changed"], ["c1:1:100"])
        self.assertEqual(self.store.pending_assignments(), [])

    def test_detail_not_wiped_by_list_sync(self):
        row = {"key": "k", "title": "t", "status": "未交", "done": False}
        self.store.upsert_assignments([row])
        self.store.update_assignment_detail("k", {"deadline": "03-27 23:59",
                                                  "deadline_dt": "2026-03-27T23:59"})
        self.store.upsert_assignments([row])
        pending = self.store.pending_assignments()
        self.assertEqual(pending[0]["deadline"], "03-27 23:59")

    def test_remind_tags(self):
        self.store.upsert_assignments([{"key": "k", "title": "t", "done": False}])
        self.assertEqual(self.store.reminded("k"), {})
        self.store.mark_reminded("k", "h24")
        self.assertIn("h24", self.store.reminded("k"))

    def test_hide_and_restore(self):
        """右键移除只在本机隐藏，再次同步不会被重新拉回来。"""
        row = {"key": "k1", "title": "t", "done": False}
        self.store.upsert_assignments([row])
        self.store.upsert_exams([{"key": "e1", "name": "期中", "answer_status": "未提交"}])
        self.store.hide_assignment("k1")
        self.store.hide_exam("e1")
        self.assertEqual(self.store.pending_assignments(), [])
        self.assertEqual(self.store.unfinished_exams(), [])
        self.assertEqual(self.store.hidden_count(), 2)
        # 同步回来也不能复活
        self.store.upsert_assignments([row])
        self.assertEqual(self.store.pending_assignments(), [])
        self.assertEqual(self.store.restore_hidden(), 2)
        self.assertEqual(self.store.hidden_count(), 0)
        self.assertEqual(len(self.store.pending_assignments()), 1)
        self.assertEqual(len(self.store.unfinished_exams()), 1)

    def test_exams_and_notices(self):
        self.store.upsert_exams([{"key": "e1", "name": "期中", "answer_status": "未提交"}])
        self.store.upsert_exams([{"key": "e2", "name": "期末", "answer_status": "已提交"}])
        names = [e["name"] for e in self.store.unfinished_exams()]
        self.assertEqual(names, ["期中"])
        fresh = self.store.upsert_notices([{"notice_id": "n1", "title": "通知",
                                            "isread": False}])
        self.assertEqual(len(fresh), 1)
        self.assertEqual(self.store.upsert_notices([{"notice_id": "n1", "title": "通知",
                                                     "isread": False}]), [])
        self.assertEqual(self.store.notice_stats(), (1, 1))
        self.store.upsert_notices([{"notice_id": "n1", "title": "通知", "isread": True}])
        self.assertEqual(self.store.notice_stats(), (1, 0))


class TestNotifyRules(unittest.TestCase):
    def setUp(self):
        import tempfile
        from xxt.notify import Notifier
        from xxt.store import Store
        self.tmp = tempfile.mkdtemp()
        self.store = Store(Path(self.tmp) / "t.db")
        self.sent = []
        self.notifier = Notifier(self.store,
                                 lambda t, b, u: self.sent.append((t, b, u)),
                                 journal_path=Path(self.tmp) / "notify.json")

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, key="k", deadline_dt=None, status="未交"):
        self.store.upsert_assignments([{"key": key, "course": "数字电路",
                                        "title": "第三章 作业", "status": status,
                                        "done": False}])
        if deadline_dt:
            self.store.update_assignment_detail(key, {"deadline_dt": deadline_dt})

    def test_bucket_escalates_once(self):
        from xxt import config
        now = datetime(2026, 3, 20, 12, 0)
        # 20 小时后截止 -> 落在 24 小时档
        self._add(deadline_dt=(now + timedelta(hours=20)).isoformat(timespec="minutes"))
        rem = self.notifier.scan(now=now, announce_new=False)
        self.assertEqual([r.tag for r in rem], ["h24"])
        self.assertEqual(self.notifier.flush(rem, now=now), 1)
        # 同一档不重复
        self.assertEqual(self.notifier.scan(now=now, announce_new=False), [])
        # 进入 6 小时档再提醒一次
        later = now + timedelta(hours=15)
        rem2 = self.notifier.scan(now=later, announce_new=False)
        self.assertEqual([r.tag for r in rem2], ["h6"])

    def test_no_reminder_when_far(self):
        now = datetime(2026, 3, 20, 12, 0)
        self._add(deadline_dt=(now + timedelta(days=5)).isoformat(timespec="minutes"))
        self.assertEqual(self.notifier.scan(now=now, announce_new=False), [])

    def test_overdue_not_reminded(self):
        """过期作业不再提醒（卡片也不展示，用户可从学习通网站自行查看）。"""
        now = datetime(2026, 3, 20, 12, 0)
        self._add(deadline_dt=(now - timedelta(hours=3)).isoformat(timespec="minutes"))
        self.assertEqual(self.notifier.scan(now=now, announce_new=False), [])
        self.assertEqual(self.notifier.scan(now=now + timedelta(hours=7),
                                            announce_new=False), [])

    def test_new_homework_announced_once(self):
        noon = datetime(2026, 3, 20, 12, 0)
        self._add()
        rem = self.notifier.scan(now=noon, announce_new=True)
        self.assertIn("new", [r.tag for r in rem])
        self.notifier.flush(rem, now=noon)
        self.assertEqual(self.notifier.scan(now=noon, announce_new=True), [])

    def test_new_homework_with_deadline_text(self):
        """带截止时间的新作业也要能播报（曾经把 datetime 当 timedelta 传给
        humanize_delta，一有新作业就整轮 scan 抛 AttributeError）。"""
        noon = datetime(2026, 3, 20, 12, 0)
        self._add(deadline_dt=(noon + timedelta(days=2)).isoformat(timespec="minutes"))
        rem = self.notifier.scan(now=noon, announce_new=True)
        new = [r for r in rem if r.tag == "new"]
        self.assertEqual(len(new), 1)
        self.assertIn("还有 2 天", new[0].body)
        self.assertTrue(new[0].title.startswith("新作业"))

    def test_new_homework_skipped_on_first_run(self):
        self._add()
        self.assertEqual(self.notifier.scan(announce_new=False), [])

    def test_quiet_hours(self):
        from xxt import config
        cfg = {"remind": {"quiet_hours": [23, 7]}}
        self.assertTrue(self.notifier.in_quiet_hours(datetime(2026, 3, 20, 23, 30), cfg))
        self.assertTrue(self.notifier.in_quiet_hours(datetime(2026, 3, 20, 3, 0), cfg))
        self.assertFalse(self.notifier.in_quiet_hours(datetime(2026, 3, 20, 12, 0), cfg))


class TestSignatureCache(unittest.TestCase):
    """课程页签名落盘：重启后第一趟同步不用重拉 49 个课程页。"""

    def setUp(self):
        import tempfile
        from xxt import client as client_mod
        self.mod = client_mod
        self.tmp = Path(tempfile.mkdtemp(prefix="xxt-sig-"))
        self._old = client_mod.config.DATA_DIR
        client_mod.config.DATA_DIR = self.tmp

    def tearDown(self):
        self.mod.config.DATA_DIR = self._old

    def _blank(self, cookie_value: str):
        c = object.__new__(self.mod.ChaoxingClient)
        c._work_enc_cache = {}
        import requests
        c.session = requests.Session()
        c.session.cookies.set("JSESSIONID", cookie_value)
        return c

    def test_roundtrip_and_fingerprint_guard(self):
        a = self._blank("sess-1")
        a._work_enc_cache[("1001", "2001")] = (0.0, {"workEnc": "w1", "enc": "e1"})
        a._work_enc_cache[("1002", "2002")] = (0.0, {"workEnc": "", "enc": "e2"})
        a.save_signature_cache()

        b = self._blank("sess-1")
        self.assertEqual(b.load_signature_cache(), 1)      # 空 workEnc 的不落盘
        self.assertEqual(b._work_enc_cache[("1001", "2001")][1]["workEnc"], "w1")

        other = self._blank("sess-2")                      # 换了会话就不能复用
        self.assertEqual(other.load_signature_cache(), 0)


class TestCardTimeHelpers(unittest.TestCase):
    """卡片上的时间显示与"过期就不显示"的判定。"""

    def setUp(self):
        from xxt.ui.widget import TaskCard
        self.card = TaskCard

    def test_short_span(self):
        from xxt.ui.widget import _short_span
        self.assertEqual(_short_span("2025-11-19 00:00 至 2025-11-19 02:00"),
                         "11-19 00:00~02:00")
        self.assertEqual(_short_span("10-15 19:38 至 10-15 21:38"), "10-15 19:38~21:38")
        # 跨天的不压缩，只去掉年份
        self.assertEqual(_short_span("2026-11-07 00:00 至 2026-11-13 23:59"),
                         "11-07 00:00 至 11-13 23:59")
        self.assertEqual(_short_span(""), "")

    def test_span_end(self):
        from xxt.ui.widget import _span_end
        self.assertEqual(_span_end("2026-11-07 00:00 至 2026-11-13 23:59"),
                         datetime(2026, 11, 13, 23, 59))
        self.assertEqual(_span_end("2026-11-07 至 2026-11-13"),
                         datetime(2026, 11, 13, 23, 59))
        self.assertIsNone(_span_end("2026-11-07 00:00"))

    def test_exam_finished(self):
        now = datetime(2026, 10, 15, 12, 0)
        old = {"exam_time": "2025-11-19 00:00 至 2025-11-20 23:59", "duration": "100"}
        self.assertTrue(self.card._exam_finished(old, datetime(2025, 11, 19), now))
        # 昨天考完的
        done = {"exam_time": "2026-10-14 14:00 至 2026-10-14 16:00", "duration": "120"}
        self.assertTrue(self.card._exam_finished(done, datetime(2026, 10, 14, 14, 0), now))
        # 正在考
        running = {"exam_time": "2026-10-15 11:30 至 2026-10-15 13:30", "duration": "120"}
        self.assertFalse(self.card._exam_finished(running, datetime(2026, 10, 15, 11, 30), now))
        # 长周期考试：开考时间已过但还在作答窗口内，要留着
        span = {"exam_time": "2026-10-10 00:00 至 2026-10-20 23:59", "duration": "100"}
        self.assertFalse(self.card._exam_finished(span, datetime(2026, 10, 10), now))
        # 没有开始时间的不敢判断，留着
        self.assertFalse(self.card._exam_finished({}, None, now))


if __name__ == "__main__":
    unittest.main(verbosity=2)
