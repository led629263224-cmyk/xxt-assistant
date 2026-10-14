"""用真实数据库渲染一张卡片快照（只读，绝不改用户的配置/数据库）。

跟 preview_card.py 的区别：那个用假数据验证样式，这个用你真实抓到的数据看一眼
实际长什么样（长课程名、缺截止时间、真实条数都跑一遍）。

用法：
    .venv\\Scripts\\python.exe tools\\snapshot_live.py
输出：
    docs\\live-card.png（收起态）、docs\\live-card-expanded.png（展开态）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication                     # noqa: E402

from xxt import config                                        # noqa: E402
from xxt.store import Store                                   # noqa: E402
from xxt.ui import style as ui_style                          # noqa: E402
from xxt.ui.widget import TaskCard                            # noqa: E402

OUT_DIR = ROOT / "docs"
REAL = {
    "DB_PATH": config.DB_PATH,
    "CONFIG_PATH": config.CONFIG_PATH,
}


class RealSync:
    """只暴露卡片用到的状态，不联网。"""

    running = True
    busy = False

    def __init__(self) -> None:
        from datetime import datetime, timedelta
        self.next_run_at = datetime.now() + timedelta(minutes=17)

    def trigger(self) -> None:
        pass


def main() -> int:
    if not REAL["DB_PATH"].exists():
        print(f"没有数据库：{REAL['DB_PATH']}")
        return 1

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    # 真实配置只读一份到临时目录，避免顺手改到用户的设置
    tmp = Path(tempfile.mkdtemp(prefix="xxt-live-"))
    cfg_copy = tmp / "config.json"
    if REAL["CONFIG_PATH"].exists():
        shutil.copy2(REAL["CONFIG_PATH"], cfg_copy)
    config.CONFIG_PATH = cfg_copy
    config.CREDENTIAL_PATH = tmp / "credentials.bin"
    config.LOG_PATH = tmp / "app.log"
    cfg = config.load(refresh=True)
    app.setPalette(ui_style.build_palette(cfg.get("widget", {}).get("theme", "light")))

    # 数据库用只读方式打开一份拷贝，绝不写用户的数据
    db_copy = tmp / "xxt.db"
    shutil.copy2(REAL["DB_PATH"], db_copy)
    store = Store(db_copy)

    total, unread = store.notice_stats()
    pending = store.pending_assignments()
    exams = store.unfinished_exams()
    print(f"真实数据：待完成作业 {len(pending)} 项、未完成考试 {len(exams)} 项、"
          f"通知 {total} 条（未读 {unread}）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    card = TaskCard(store, RealSync())
    card.set_status(store.get_meta("last_sync_summary") or "尚未同步过", "ok")

    card.reload()
    card.show()
    for _ in range(12):
        app.processEvents()
    out = OUT_DIR / "live-card.png"
    card.grab().save(str(out))
    card.hide()
    print(f"已生成 {out}")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
