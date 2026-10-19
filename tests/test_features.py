"""同步渐进更新、数据迁移与清理、真实鼠标手势的离线回归。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from xxt import config
from xxt.client import ChaoxingClient, LoginResult
from xxt.store import Store
from xxt.sync import SyncService


class IsolatedConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xxt-features-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # macOS 测试同样只使用临时数据，不读取/删除真实登录钥匙串。
        self.enterContext(patch("xxt.config._mac_keychain",
                                return_value=Mock(get_password=Mock(return_value=None))))
        self.old = {name: getattr(config, name) for name in
                    ("DATA_DIR", "CONFIG_PATH", "CREDENTIAL_PATH", "COOKIE_PATH",
                     "DB_PATH", "LOG_PATH", "STORAGE_PATH", "_cache")}
        config._set_data_paths(self.root / "original")
        config.STORAGE_PATH = self.root / "storage.json"
        config.save(copy.deepcopy(config.DEFAULT_CONFIG))

    def tearDown(self):
        for name, value in self.old.items():
            setattr(config, name, value)


class TestDataLocation(IsolatedConfig):
    def test_migrate_then_clear_preserves_unrelated_files_and_location(self):
        source = config.DATA_DIR
        config.COOKIE_PATH.write_text('{"session": "test"}')
        config.CREDENTIAL_PATH.write_bytes(b"encrypted-test")
        (source / "background.png").write_bytes(b"picture-test")
        (source / "unrelated.txt").write_text("keep")
        config.update("widget", background_image=str(source / "background.png"))
        store = Store()
        store.upsert_assignments([{"key": "one", "title": "test", "done": False}])
        store.close()
        target = self.root / "chosen"
        target.mkdir()
        (target / "personal.txt").write_text("keep")
        config.select_data_directory(target)
        self.assertEqual(config.DATA_DIR, target)
        self.assertEqual(config.load()["widget"]["background_image"], str(target / "background.png"))
        self.assertTrue(config.COOKIE_PATH.exists())
        self.assertFalse((source / "credentials.bin").exists())
        self.assertTrue((source / "unrelated.txt").exists())
        restored = Store()
        self.assertEqual(len(restored.pending_assignments()), 1)
        restored.close()
        config.clear_all_data()
        self.assertFalse(config.DB_PATH.exists())
        self.assertFalse(config.COOKIE_PATH.exists())
        self.assertFalse(config.CREDENTIAL_PATH.exists())
        self.assertFalse((target / "background.png").exists())
        self.assertEqual((target / "personal.txt").read_text(), "keep")
        self.assertEqual(json.loads(config.STORAGE_PATH.read_text())["data_dir"], str(target))
        self.assertTrue(config.load()["storage"]["selected"])

    def test_existing_target_is_not_overwritten(self):
        target = self.root / "occupied"
        target.mkdir()
        (target / "config.json").write_text("existing")
        original = config.DATA_DIR
        with self.assertRaises(OSError):
            config.select_data_directory(target)
        self.assertEqual((target / "config.json").read_text(), "existing")
        self.assertEqual(config.DATA_DIR, original)

    def test_copy_failure_keeps_original_and_does_not_commit_pointer(self):
        config.COOKIE_PATH.write_text("original")
        original = config.DATA_DIR
        with patch("xxt.config.shutil.copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.select_data_directory(self.root / "failed")
        self.assertEqual(config.DATA_DIR, original)
        self.assertEqual(config.COOKIE_PATH.read_text(), "original")
        self.assertFalse(config.STORAGE_PATH.exists())

    def test_reject_nested_directory(self):
        with self.assertRaises(OSError):
            config.select_data_directory(config.DATA_DIR / "nested")


class TestProgressiveSync(IsolatedConfig):
    def test_courses_are_visible_before_whole_sync_finishes(self):
        store = Store()
        self.addCleanup(store.close)
        client = Mock()
        client.ensure_login.return_value = LoginResult(True)
        client.fetch_profile.return_value = {}
        client.fetch_courses.return_value = [
            {"courseid": "1", "clazzid": "1", "name": "first"},
            {"courseid": "2", "clazzid": "2", "name": "second"},
        ]
        seen_during_second_course = []

        def fetch(course):
            if course["courseid"] == "2":
                seen_during_second_course.extend(store.pending_assignments())
            return {"items": [{"url": "https://example.org/" + course["courseid"],
                               "title": course["name"], "status": "未提交"}]}

        client.fetch_homework_list.side_effect = fetch
        client.fetch_exams.return_value = []
        client.fetch_notices.return_value = []
        notifier = Mock()
        notifier.flush.return_value = 0
        progress = []
        service = SyncService(store, client, notifier, on_progress=progress.append)
        report = service.sync_once(refresh_details=False)
        self.assertTrue(report.ok)
        self.assertEqual(report.scanned, 2)
        self.assertEqual([row["title"] for row in seen_during_second_course], ["first"])
        self.assertTrue(any("1/2" in text for text in progress))
        self.assertEqual(len(report.new_assignments), 2)

    def test_login_does_not_race_with_sync(self):
        store = Store()
        self.addCleanup(store.close)
        client = Mock()
        service = SyncService(store, client, Mock())
        service._running.acquire()
        try:
            self.assertFalse(service.login("test", "test").ok)
            client.login.assert_not_called()
        finally:
            service._running.release()

    def test_base_page_shared_between_profile_and_courses(self):
        client = ChaoxingClient(request_delay=0)
        self.addCleanup(client.session.close)
        client._get = Mock(return_value=Mock(text='var fid = "427"'))
        client.fetch_profile()
        client.fetch_courses()
        from xxt.client import URL_BASE
        self.assertEqual(sum(call.args[0] == URL_BASE for call in client._get.call_args_list), 1)


class TestDesktopState(unittest.TestCase):
    def make_presence(self):
        from PySide6.QtCore import QObject, Qt
        from xxt.ui.desktop import DesktopPresence

        class Card(QObject):
            def winId(self):
                return 1

            def isVisible(self):
                return True

            def showNormal(self):
                pass

            def windowFlags(self):
                return Qt.WindowType.Tool

            def update(self):
                pass

        class DesktopApi:
            def __init__(self):
                self.desktop = True
                self.visible = True
                self.raised = False
                self.positions = []

            def GetForegroundWindow(self):
                return 3  # Win+D 过渡期仍指向旧应用。

            def GetAncestor(self, hwnd, kind):
                return hwnd

            def GetClassNameW(self, hwnd, output, size):
                output.value = {1: "QtWindow", 2: "Progman", 3: "Chrome_WidgetWin_1"}[hwnd]

            def GetWindowRect(self, hwnd, output):
                values = (400, 100, 740, 300) if hwnd == 1 else (0, 0, 1200, 800)
                output._obj.left, output._obj.top, output._obj.right, output._obj.bottom = values
                return True

            def IsIconic(self, hwnd):
                return False

            def IsWindowVisible(self, hwnd):
                return self.visible if hwnd == 1 else True

            def WindowFromPoint(self, point):
                on_card = 400 <= point.x <= 740 and 100 <= point.y <= 300
                if self.raised and on_card:
                    return 1
                return 2 if self.desktop else 3

            def SetWindowPos(self, hwnd, after, x, y, width, height, flags):
                after = after.value if hasattr(after, "value") else after
                self.positions.append((after, flags))
                if flags & 0x0040:
                    self.visible = True
                if not flags & 0x0004:
                    self.raised = after == -1 or after == 2 ** 64 - 1
                return True

        card = Card()
        presence = DesktopPresence(card)
        presence._api = DesktopApi()
        presence._get_style = lambda hwnd, index: 0
        presence._configured_hwnd = 1
        presence.enabled = True
        return card, presence

    def test_stale_foreground_does_not_prevent_desktop_display_or_app_restore(self):
        card, presence = self.make_presence()
        api = presence._api
        presence.check()
        self.assertTrue(api.raised)
        presence.check()
        self.assertTrue(api.raised, "前台 HWND 尚未变化时不能把桌面卡片降下去")
        api.desktop = False
        presence.check()
        self.assertFalse(api.raised)
        self.assertEqual(api.positions[-1][0], 3, "恢复应用时卡片须排在该应用后方")

    def test_native_hide_recovers_without_reviving_tray_hidden_card(self):
        card, presence = self.make_presence()
        api = presence._api
        api.desktop = False
        api.visible = False
        presence.check()
        self.assertTrue(api.visible)
        self.assertTrue(api.positions[0][1] & 0x0004, "恢复可见性时应保留层级")
        presence.enabled = False
        api.visible = False
        presence.check()
        self.assertFalse(api.visible)


class TestSwipe(IsolatedConfig):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_short_swipe_returns_and_long_swipe_hides_then_restores(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from xxt.ui.widget import TaskCard, TaskRow
        store = Store()
        self.addCleanup(store.close)
        store.upsert_assignments([{"key": "gesture", "title": "Swipe me", "done": False}])
        card = TaskCard(store, Mock(busy=False, next_run_at=None))
        self.addCleanup(card.hide)
        card.show()
        QTest.qWait(80)
        row = card.findChildren(TaskRow)[0]
        point = QPoint(80, row.height() // 2)
        QTest.mousePress(row, Qt.MouseButton.RightButton, pos=point)
        QTest.mouseRelease(row, Qt.MouseButton.RightButton, pos=point + QPoint(15, 0))
        QTest.qWait(260)
        self.assertEqual(store.hidden_count(), 0)
        self.assertEqual(row._offset, 0)
        # 从标题子控件开始，验证实际用户点到文字时也能触发手势。
        QTest.mousePress(row.title_label, Qt.MouseButton.RightButton, pos=QPoint(15, 5))
        release = row.mapFromGlobal(row.title_label.mapToGlobal(QPoint(15, 5))) + QPoint(130, 0)
        QTest.mouseRelease(row, Qt.MouseButton.RightButton, pos=release)
        QTest.qWait(260)
        self.assertEqual(store.hidden_count(), 0, "长滑仍在窗口内时不能移除")
        self.assertEqual(row._offset, 0)
        # 拖过行边缘但没越过窗口边缘，也要回弹。
        edge_inside = row.mapFromGlobal(card.mapToGlobal(QPoint(card.width() - 2, 20)))
        QTest.mousePress(row, Qt.MouseButton.RightButton, pos=point)
        QTest.mouseRelease(row, Qt.MouseButton.RightButton, pos=edge_inside)
        QTest.qWait(260)
        self.assertEqual(store.hidden_count(), 0)
        outside = row.mapFromGlobal(card.mapToGlobal(QPoint(card.width() + 12, 20)))
        QTest.mousePress(row.title_label, Qt.MouseButton.RightButton, pos=QPoint(15, 5))
        QTest.mouseRelease(row, Qt.MouseButton.RightButton, pos=outside)
        QTest.qWait(260)
        self.assertEqual(store.hidden_count(), 1)
        self.assertEqual(len(card.findChildren(TaskRow)), 0)
        store.restore_hidden()
        card.reload()
        QTest.qWait(80)
        self.assertEqual(len(card.findChildren(TaskRow)), 1)

    def test_tray_hide_disables_desktop_presence(self):
        from xxt.ui.widget import TaskCard
        store = Store()
        self.addCleanup(store.close)
        card = TaskCard(store, Mock(busy=False, next_run_at=None))
        card.show_card()
        self.assertTrue(card._desktop_presence.enabled)
        card.hide_card()
        card._desktop_presence.check()
        self.assertFalse(card._desktop_presence.enabled)
        self.assertFalse(card.isVisible())

    def test_tray_double_click_does_not_hide_card_again(self):
        from PySide6.QtWidgets import QSystemTrayIcon
        from xxt.ui.tray import TrayIcon
        from xxt.ui.widget import TaskCard
        store = Store()
        self.addCleanup(store.close)
        card = TaskCard(store, Mock(busy=False, next_run_at=None))
        self.addCleanup(card.hide_card)
        card.show_card()
        tray = TrayIcon(card, lambda: None, lambda: None, lambda: None)
        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        self.assertTrue(card.isVisible())
        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertTrue(card.isVisible())

    def test_background_is_copied_then_can_be_removed(self):
        from PySide6.QtGui import QColor, QImage
        from xxt.ui.dialogs import SettingsDialog
        original = self.root / "original-picture.png"
        image = QImage(120, 80, QImage.Format.Format_RGB32)
        image.fill(QColor("#39bfa5"))
        self.assertTrue(image.save(str(original)))
        with patch("xxt.startup.is_enabled", return_value=False):
            dialog = SettingsDialog()
            dialog.background_path.setText(str(original))
            dialog.background_overlay.setValue(50)
            dialog.save()
            managed = config.DATA_DIR / "background.png"
            self.assertTrue(managed.exists())
            self.assertEqual(config.load()["widget"]["background_image"], str(managed))
            original.unlink()
            self.assertFalse(QImage(str(managed)).isNull())
            # 再次保存同一张托管背景时也不能因文件占用失败。
            second = SettingsDialog()
            second.save()
            second.background_path.clear()
            second.save()
            self.assertFalse(managed.exists())
            self.assertEqual(config.load()["widget"]["background_image"], "")

    def test_login_runs_in_background_and_returns_result_to_gui(self):
        import time
        from xxt.ui.dialogs import LoginDialog
        locations = []

        def slow_login(*args):
            time.sleep(.15)
            return LoginResult(False, "模拟登录失败")

        dialog = LoginDialog(slow_login, directory_fn=locations.append)
        dialog.phone.setText("test-user")
        dialog.password.setText("test-password")
        dialog._do_login()
        self.assertTrue(dialog._logging_in)
        self.assertFalse(dialog.ok_btn.isEnabled())
        until = time.monotonic() + 2
        while dialog._logging_in and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(dialog._logging_in)
        self.assertEqual(dialog.hint.text(), "模拟登录失败")
        self.assertTrue(dialog.ok_btn.isEnabled())
        self.assertEqual(locations, [str(config.DATA_DIR)])


if __name__ == "__main__":
    unittest.main()
