"""把 assets/icon.png 转成 Windows 需要的多尺寸 assets/icon.ico（以及 256px 的 PNG）。

用 DSH 自带的 python 跑（venv 里没装 Pillow）：
    "C:\\Users\\ASUS\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe" tools\\make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "icon.png"
ICO = ROOT / "assets" / "icon.ico"
SMALL = ROOT / "assets" / "icon-256.png"
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    im = Image.open(SRC).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    im = im.crop((left, top, left + side, top + side))   # 裁成正方形，避免拉伸变形
    im.save(ICO, format="ICO", sizes=SIZES)
    im.resize((256, 256), Image.LANCZOS).save(SMALL, format="PNG")
    print(f"源图 {SRC.name} {w}x{h} -> 裁剪为 {im.size}")
    print(f"已生成 {ICO.name}（{', '.join(f'{a}x{b}' for a, b in SIZES)}）")
    print(f"已生成 {SMALL.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
