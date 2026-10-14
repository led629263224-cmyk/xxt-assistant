"""打包成单文件 exe（给同学双击就能用，不需要装 Python）。

    .venv\\Scripts\\python.exe tools\\build_exe.py

产物：dist\\学习通小助手.exe
说明：
    * --onefile 单文件，首次启动要解包到临时目录，会慢 3~5 秒，之后正常
    * --noconsole 不弹黑窗口，日志写在同目录的 data\\app.log
    * 数据（数据库/配置/凭据）放在 exe 所在目录的 data\\ 下，卸载就是删文件夹
    * assets 打进包里（--add-data），运行期由 xxt/resources.py 定位
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "学习通小助手"
ICON = ROOT / "assets" / "icon.ico"
DIST = ROOT / "dist"
WORK = ROOT / "build"

# 只用到 QtCore/QtGui/QtWidgets，其余的 Qt 模块（尤其 QtWebEngine 上百 MB）都排掉
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtBluetooth", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtNfc", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtXml",
    "tkinter", "unittest", "pydoc_data", "PIL", "numpy", "pandas", "test",
]


def main() -> int:
    if not ICON.exists():
        print(f"缺少图标 {ICON}，先跑一次 tools/make_icon.py")
        return 1

    for folder in (DIST, WORK):
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", NAME,
        f"--icon={ICON}",
        f"--add-data={ROOT / 'assets'}{';'}assets",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
    ]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    args.append(str(ROOT / "run.py"))

    print("执行：", " ".join(f'"{a}"' if " " in a else a for a in args[1:8]), "…")
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode != 0:
        print("打包失败")
        return result.returncode

    exe = DIST / f"{NAME}.exe"
    if not exe.exists():
        print(f"打包命令成功但没找到 {exe}")
        return 1
    size = exe.stat().st_size / 1024 / 1024
    print()
    print(f"完成：{exe}　{size:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
