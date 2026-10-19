"""学习通小助手 —— 只读抓取学习通网页端的作业/考试/通知，桌面悬浮清单提醒。

包内模块：
    crypto    Windows DPAPI 凭据加解密
    config    路径与配置读写
    parse     纯解析函数（无网络，可单测）
    client    学习通 Web 端只读接口客户端
    store     SQLite 存储与增量 diff
    sync      同步编排（抓取 -> 入库 -> 产生事件）
    notify    Windows Toast 提醒与去重
    widget    PySide6 桌面悬浮清单
    cli       命令行入口
"""

__version__ = "1.02"
APP_NAME = "学习通小助手"
BUNDLE_ID = "io.github.led629263224-cmyk.xxt-assistant"
