"""打包 / 直接运行用的入口（等价于 python -m xxt.main）。

PyInstaller 从这里开始分析依赖：
    .venv\\Scripts\\python.exe tools\\build_exe.py

打包成 -w（无控制台）后，未捕获的异常会悄无声息地让程序消失，所以这里兜一层：
写 data/crash.log，并弹一个对话框告诉用户日志在哪。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _report(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(text, file=sys.stderr)
    log_path = None
    try:
        from xxt import config

        config.ensure_dirs()
        log_path = config.DATA_DIR / "crash.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n" + "-" * 60 + "\n")
    except Exception:
        pass

    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv[:1])
        where = f"\n\n详细信息已写入：\n{log_path}" if log_path else ""
        QMessageBox.critical(None, "学习通小助手启动失败",
                             f"{type(exc).__name__}: {exc}{where}")
    except Exception:
        pass


def main() -> int:
    from xxt.main import main as run

    return run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:                      # noqa: BLE001
        _report(exc)
        raise SystemExit(1)
