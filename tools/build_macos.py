"""在 macOS 上构建临时签名的 .app、ZIP 和 DMG，无需开发者证书。

    python tools/build_macos.py --arch arm64
    python tools/build_macos.py --arch x86_64
"""
from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xxt import APP_NAME, BUNDLE_ID, __version__
from tools.build_exe import EXCLUDES


def _make_icon(work: Path) -> Path:
    from PIL import Image

    iconset = work / "app.iconset"
    iconset.mkdir()
    with Image.open(ROOT / "assets" / "icon.png") as source:
        source = source.convert("RGBA")
        side = min(source.size)
        left, top = (source.width - side) // 2, (source.height - side) // 2
        source = source.crop((left, top, left + side, top + side))
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                source.resize((size * scale, size * scale), Image.Resampling.LANCZOS).save(
                    iconset / f"icon_{size}x{size}{suffix}.png")
    icon = work / "app.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icon)], check=True)
    return icon


def _write_spec(work: Path, icon: Path, arch: str) -> Path:
    spec = work / "macos.spec"
    plist = {
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": ".".join(str(int(part)) for part in __version__.split(".")),
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    spec.write_text(
        "from PyInstaller.utils.hooks import copy_metadata\n"
        f"a = Analysis([{str(ROOT / 'run.py')!r}], pathex=[{str(ROOT)!r}],\n"
        f"    datas=[({str(ROOT / 'assets')!r}, 'assets')] + copy_metadata('keyring'),\n"
        "    hiddenimports=['keyring.backends.macOS', 'keyring.backends.macOS.api'],\n"
        f"    excludes={EXCLUDES!r})\n"
        "pyz = PYZ(a.pure)\n"
        "exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='xxt-assistant',\n"
        f"    console=False, argv_emulation=False, target_arch={arch!r}, codesign_identity='-')\n"
        "coll = COLLECT(exe, a.binaries, a.datas, name='xxt-assistant')\n"
        f"app = BUNDLE(coll, name={APP_NAME + '.app'!r}, icon={str(icon)!r},\n"
        f"    bundle_identifier={BUNDLE_ID!r}, info_plist={plist!r})\n",
        encoding="utf-8",
    )
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("arm64", "x86_64"), default=platform.machine())
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("请在 macOS 或 GitHub Actions 的 macOS runner 中构建。")
    if platform.machine() != args.arch:
        parser.error("请使用与 --arch 相同架构的 Python，或选择对应的 GitHub Actions 构建机。")

    dist = ROOT / "dist" / f"macos-{args.arch}"
    build = ROOT / "build" / "macos"
    dist.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)
    stem = f"xxt-assistant-v{__version__}-macos-{args.arch}"
    with tempfile.TemporaryDirectory(prefix=f"{args.arch}-", dir=build) as temporary:
        work = Path(temporary)
        icon = _make_icon(work)
        spec = _write_spec(work, icon, args.arch)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(dist), "--workpath", str(work / "pyinstaller"), str(spec),
        ], cwd=ROOT, check=True)
        app = dist / f"{APP_NAME}.app"
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        # 直接执行包内入口，检查冻结后的 Python 及核心依赖可以正常加载。
        subprocess.run([str(app / "Contents" / "MacOS" / "xxt-assistant"), "--help"], check=True)

        stage = work / "dmg"
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        shutil.copy2(ROOT / "docs" / "macos.md", stage / "macOS-使用说明.md")
        dmg = dist / f"{stem}.dmg"
        subprocess.run([
            "hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(stage),
            "-ov", "-format", "UDZO", str(dmg),
        ], check=True)
        archive = dist / f"{stem}.zip"
        # 使用 ditto 保留 Qt frameworks 的符号链接和应用权限。
        archive.unlink(missing_ok=True)
        subprocess.run([
            "ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive),
        ], check=True)

    checksums = []
    for artifact in (dmg, archive):
        with artifact.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{checksum}  {artifact.name}\n")
        print(f"完成：{artifact}")
    (dist / "SHA256SUMS.txt").write_text("".join(checksums), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
