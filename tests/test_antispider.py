# -*- coding: utf-8 -*-
"""反爬拦截识别、易失 cookie 硬化、以及"被拦"不再被误记成"已尝试"。全部离线。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from xxt import config
from xxt.client import AntispiderBlocked, ChaoxingClient, VOLATILE_COOKIES
from xxt.parse import is_homework_pending
from xxt.store import Store
from xxt.sync import SyncReport, SyncService

ANTISPIDER_URL = "https://mooc1.chaoxing.com/antispiderShowVerify.ac"
ANTISPIDER_HTML = ('<html><body><div>提示页面 【9010】操作异常，请输入图片中的验证码</div>'
                   '<img src="verify.png"></body></html>')
DETAIL_HTML = ('<div class="tit">作业详情第三章 作业</div><div>题量: 10</div>'
               '<div>作答时间: 03-20 08:00 至 03-27 23:59</div>')
DETAIL_URL = "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?workId=999"


def resp(text, url="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork"):
    r = Mock()
    r.text = text
    r.url = url
    r.status_code = 200
    r.cookies = []
    return r


class IsolatedDataDir(unittest.TestCase):
    """把 config 的数据路径指到临时目录，避免动到真实 data/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xxt-anti-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = {name: getattr(config, name) for name in
                    ("DATA_DIR", "CONFIG_PATH", "CREDENTIAL_PATH", "COOKIE_PATH",
                     "DB_PATH", "LOG_PATH", "STORAGE_PATH", "_cache")}
        config._set_data_paths(self.root / "data")
        config.STORAGE_PATH = self.root / "storage.json"

    def tearDown(self):
        for name, value in self.old.items():
            setattr(config, name, value)

    def make_client(self, cookies=None):
        c = ChaoxingClient(request_delay=0)
        self.addCleanup(c.session.close)
        for k, v in (cookies or {}).items():
            c.session.cookies.set(k, v, domain=".chaoxing.com", path="/")
        return c


class TestAntispiderDetect(unittest.TestCase):
    """验证码页的识别：只认 URL 和正文开头，不整页扫"验证码"。"""

    def test_url_marker(self):
        self.assertTrue(ChaoxingClient._looks_like_antispider(resp("", ANTISPIDER_URL)))

    def test_text_marker(self):
        self.assertTrue(ChaoxingClient._looks_like_antispider(resp(ANTISPIDER_HTML)))

    def test_normal_detail_page_is_not_blocked(self):
        self.assertFalse(ChaoxingClient._looks_like_antispider(resp(DETAIL_HTML)))

    def test_verification_code_word_inside_homework_body_is_not_blocked(self):
        # 题干里出现"验证码"三个字不能误判（issue 里点名的坑）
        html = "<div>作业详情</div>" + "<p>题目说明：</p>" * 60 + "<p>请在答案里填写验证码</p>"
        self.assertFalse(ChaoxingClient._looks_like_antispider(resp(html)))


class TestCookieHardening(IsolatedDataDir):
    """易失票据既不落盘、也不参与指纹，但签名缓存仍能跨重启复用。"""

    def test_volatile_cookies_are_not_persisted(self):
        c = self.make_client({"JSESSIONID": "s1", "k8s": "t1", "route": "r1",
                              "k8sexam": "e1", "jrose": "j1"})
        c.save_cookies()
        saved = json.loads(config.COOKIE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("JSESSIONID"), "s1")
        for name in VOLATILE_COOKIES:
            self.assertNotIn(name, saved)

    def test_old_cookie_file_with_volatile_is_filtered_on_load(self):
        # 旧版本（v1.01 及以前）存过易失票据，升级上来不能继续回放
        config.ensure_dirs()
        config.COOKIE_PATH.write_text(json.dumps(
            {"JSESSIONID": "s1", "k8s": "old-ticket", "route": "old-route"}),
            encoding="utf-8")
        c = self.make_client()
        names = {cookie.name for cookie in c.session.cookies}
        self.assertIn("JSESSIONID", names)
        self.assertNotIn("k8s", names)
        self.assertNotIn("route", names)

    def test_fingerprint_ignores_volatile_tickets(self):
        a = self.make_client({"JSESSIONID": "s1", "k8s": "ticket-old"})
        b = self.make_client({"JSESSIONID": "s1", "k8s": "ticket-new"})
        self.assertEqual(a._cookie_fingerprint(), b._cookie_fingerprint())

    def test_signature_cache_survives_restart(self):
        a = self.make_client({"JSESSIONID": "s1", "k8s": "ticket-1"})
        a._work_enc_cache[("1001", "2001")] = (0.0, {"workEnc": "w1", "enc": "e1"})
        a.save_cookies()
        a.save_signature_cache()

        b = self.make_client()                      # 重启：重新读存盘的 cookie
        self.assertEqual(b.load_signature_cache(), 1)
        self.assertEqual(b._work_enc_cache[("1001", "2001")][1]["workEnc"], "w1")


class TestAntispiderRetry(IsolatedDataDir):
    """命中验证码页时丢易失票据重试一次；仍被拦就抛专门异常。"""

    def test_retry_without_volatile_then_success(self):
        c = self.make_client({"JSESSIONID": "s1", "k8s": "t1", "route": "r1"})
        c._raw_get = Mock(side_effect=[resp(ANTISPIDER_HTML, ANTISPIDER_URL), resp(DETAIL_HTML)])
        r = c._get(DETAIL_URL)
        self.assertIn("作答时间", r.text)
        self.assertEqual(c._raw_get.call_count, 2)
        self.assertNotIn("k8s", {cookie.name for cookie in c.session.cookies})

    def test_still_blocked_raises_antispider(self):
        c = self.make_client({"JSESSIONID": "s1"})
        c._raw_get = Mock(return_value=resp(ANTISPIDER_HTML, ANTISPIDER_URL))
        with self.assertRaises(AntispiderBlocked):
            c._get(DETAIL_URL)
        self.assertEqual(c._raw_get.call_count, 2)


class TestDetailRefreshOnBlock(IsolatedDataDir):
    """被拦 ≠ 页面里没有时间信息：不能盖 detail_at，而且要在报告里露出来。"""

    def make_store(self, count=3):
        store = Store()
        self.addCleanup(store.close)
        store.upsert_assignments([
            {"key": f"c1:cl1:w{i}", "course": "某课程", "courseid": "c1", "clazzid": "cl1",
             "title": f"第{i}章 作业", "status": "未交",
             "url": DETAIL_URL + str(i), "done": False}
            for i in range(1, count + 1)])
        return store

    def run_refresh(self, store, client):
        svc = SyncService(store, client, Mock())
        report = SyncReport(started_at=datetime.now())
        svc._refresh_details(report, {"sync": {"detail_refresh_hours": 6}})
        return report

    def test_blocked_detail_is_not_marked_as_tried(self):
        store = self.make_store()
        client = Mock()
        client.fetch_homework_detail.side_effect = AntispiderBlocked("被拦")
        report = self.run_refresh(store, client)

        self.assertEqual(store.detail_age_hours("c1:cl1:w1"), None)   # 没盖时间戳
        self.assertEqual(len(store.assignments_needing_detail(6)), 3)  # 下一轮还会重试
        self.assertTrue(report.errors)
        self.assertIn("反爬", report.errors[0])
        self.assertEqual(client.fetch_homework_detail.call_count, 1)   # 被拦就停手，不再连发

    def test_page_without_time_is_still_marked_as_tried(self):
        # 原有行为要保留：确实没有时间的页面记成"已尝试"，避免每轮白拉
        store = self.make_store(count=1)
        client = Mock()
        client.fetch_homework_detail.return_value = {
            "title": None, "question_count": None, "full_score": None,
            "start_time": None, "deadline": None, "score": None}
        self.run_refresh(store, client)
        self.assertIsNotNone(store.detail_age_hours("c1:cl1:w1"))
        self.assertEqual(len(store.assignments_needing_detail(6)), 0)


class TestDoneMarks(unittest.TestCase):
    def test_reviewed_peer_homework_counts_as_done(self):
        self.assertFalse(is_homework_pending("已互评"))

    def test_waiting_for_my_review_still_shows_up(self):
        self.assertTrue(is_homework_pending("待互评"))
