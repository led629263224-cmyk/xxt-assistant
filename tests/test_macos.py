"""macOS 适配回归：路径、钥匙串生命周期、LaunchAgent 与窗口交互。

平台 API 使用替身，在 Windows/macOS 均可运行，不访问真实钥匙串或登录项。
"""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from xxt import BUNDLE_ID, config, startup
from tests.test_features import IsolatedConfig


class MemoryKeychain:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        del self.values[service, account]


class TestMacCredentials(IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.keychain = MemoryKeychain()
        self.enterContext(patch("sys.platform", "darwin"))
        self.enterContext(patch("xxt.config._mac_keychain", return_value=self.keychain))

    def test_password_is_in_keychain_and_survives_directory_migration(self):
        config.save_credentials("test-user", "private-password")
        self.assertTrue(config.has_credentials())
        self.assertFalse(config.CREDENTIAL_PATH.exists())
        self.assertNotIn("private-password", config.CONFIG_PATH.read_text(encoding="utf-8"))
        config.select_data_directory(self.root / "chosen")
        self.assertEqual(config.load_credentials(), ("test-user", "private-password"))
        config.clear_credentials()
        self.assertFalse(config.has_credentials())
        self.assertFalse(self.keychain.values)

    def test_clear_all_also_removes_keychain_credentials(self):
        config.save_credentials("test-user", "private-password")
        config.COOKIE_PATH.write_text("cookie")
        config.clear_all_data()
        self.assertFalse(self.keychain.values)
        self.assertFalse(config.COOKIE_PATH.exists())
        self.assertFalse(config.has_credentials())

    def test_denied_keychain_does_not_fall_back_to_plaintext(self):
        with patch.object(self.keychain, "set_password", side_effect=RuntimeError("denied")):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                config.save_credentials("test-user", "private-password")
        self.assertFalse(config.CREDENTIAL_PATH.exists())
        self.assertFalse(config.has_credentials())

    def test_keychain_failure_prevents_partial_clear(self):
        config.COOKIE_PATH.write_text("keep")
        with patch.object(self.keychain, "get_password", side_effect=RuntimeError("locked")):
            with self.assertRaises(RuntimeError):
                config.clear_all_data()
        self.assertEqual(config.COOKIE_PATH.read_text(), "keep")

    def test_missing_or_invalid_credentials_do_not_count_as_saved(self):
        config.clear_credentials()
        self.keychain.values[BUNDLE_ID, "credentials"] = "invalid-json"
        self.assertIsNone(config.load_credentials())
        self.assertFalse(config.has_credentials())


class TestMacPathsAndStartup(IsolatedConfig):
    def test_frozen_mac_data_is_outside_application_bundle(self):
        with patch("sys.platform", "darwin"), patch.object(Path, "home", return_value=self.root), \
                patch.object(sys, "frozen", True, create=True):
            self.assertEqual(config._base_dir(),
                             self.root / "Library" / "Application Support" / "xxt-assistant")

    def test_windows_keeps_portable_data_directory(self):
        executable = self.root / "xxt-assistant.exe"
        with patch("sys.platform", "win32"), patch.object(sys, "executable", str(executable)), \
                patch.object(sys, "frozen", True, create=True):
            self.assertEqual(config._base_dir(), self.root)

    def test_agent_enable_disable_roundtrip_with_spaces_in_path(self):
        agent_path = self.root / "LaunchAgents" / f"{BUNDLE_ID}.plist"
        executable = str(self.root / "Applications with spaces" / "学习通小助手.app"
                         / "Contents" / "MacOS" / "xxt-assistant")
        with patch("sys.platform", "darwin"), patch("xxt.startup._agent_path", return_value=agent_path), \
                patch.object(sys, "executable", executable), \
                patch.object(sys, "frozen", True, create=True):
            self.assertFalse(startup.is_enabled())
            self.assertTrue(startup.set_enabled(True))
            self.assertTrue(startup.is_enabled())
            agent = plistlib.loads(agent_path.read_bytes())
            self.assertEqual(agent["ProgramArguments"], [executable, "--tray"])
            self.assertNotIn("KeepAlive", agent)
            self.assertEqual(agent["StandardErrorPath"], str(config.DATA_DIR / "launch-error.log"))
            self.assertTrue(startup.set_enabled(False))
            self.assertFalse(startup.is_enabled())
            self.assertTrue(startup.set_enabled(False))

    def test_source_login_item_uses_launcher_outside_data_directory(self):
        with patch("sys.platform", "darwin"), patch.object(sys, "frozen", False, create=True):
            arguments = startup._mac_arguments()
        self.assertEqual(Path(arguments[1]), Path(startup.__file__).resolve().parents[1] / "run.py")
        self.assertEqual(arguments[-1], "--tray")

    def test_invalid_agent_and_unsupported_platform_are_handled(self):
        agent_path = self.root / "broken.plist"
        agent_path.write_bytes(plistlib.dumps([]))
        with patch("sys.platform", "darwin"), patch("xxt.startup._agent_path", return_value=agent_path):
            self.assertFalse(startup.is_enabled())
            agent_path.write_text("broken")
            self.assertFalse(startup.is_enabled())
        with patch("sys.platform", "linux"):
            self.assertFalse(startup.is_enabled())
            self.assertFalse(startup.set_enabled(True))


class TestMacWidget(IsolatedConfig):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_card_is_normal_window_and_menu_open_does_not_toggle_it(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QSystemTrayIcon
        from xxt.store import Store
        from xxt.ui.tray import RoundedMenu, TrayIcon
        from xxt.ui.widget import TaskCard

        store = Store()
        self.addCleanup(store.close)
        with patch("sys.platform", "darwin"):
            card = TaskCard(store, Mock(busy=False, next_run_at=None))
            self.addCleanup(card.hide_card)
            self.assertEqual(card.windowType(), Qt.WindowType.Window)
            self.assertFalse(card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            card.show_card()
            tray = TrayIcon(card, lambda: None, lambda: None, lambda: None)
            self.assertNotIsInstance(tray._menu, RoundedMenu)
            tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
            self.assertTrue(card.isVisible())
            tray.act_toggle.trigger()
            self.assertFalse(card.isVisible())
            tray.act_toggle.trigger()
            self.assertTrue(card.isVisible())
            config.update("widget", always_on_top=True)
            card.apply_config()
            self.assertTrue(card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)


class TestMacBuild(IsolatedConfig):
    def test_generated_spec_contains_metadata_and_only_public_assets(self):
        import ast
        from tools.build_macos import ROOT, _write_spec

        spec = _write_spec(self.root, self.root / "icon.icns", "arm64")
        source = spec.read_text(encoding="utf-8")
        tree = ast.parse(source)
        strings = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertIn(BUNDLE_ID, source)
        self.assertIn("target_arch='arm64'", source)
        self.assertIn("codesign_identity='-'", source)
        self.assertIn(str(ROOT / "assets"), strings)
        self.assertNotIn(str(config.DATA_DIR), strings)
        self.assertNotIn("credentials.bin", source)
