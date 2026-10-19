# macOS 构建与使用

macOS 版本沿用 PySide6 界面，使用 PyInstaller 的目录模式生成 `学习通小助手.app`。
构建脚本生成 Apple Silicon（`arm64`）或 Intel（`x86_64`）产物；不生成混合架构包。

当前采用 **ad-hoc 临时签名，不需要 Apple 开发者证书，未经过 Apple 公证**。
临时签名用于保证包内二进制签名有效，不提供被 Apple 认可的开发者身份。

## 验证状态与系统要求

- 使用 Python **3.13** 构建，运行依赖保持 PySide6 **6.9.3**。
- 最低目标为 **macOS 12**（与 PySide6 安装包要求一致）。在较新系统构建并设置部署目标，并不代表已验证旧系统兼容性。
- GitHub Actions 分别在 macOS 15 的 ARM、Intel 构建机上测试和打包。
- Windows 本机只能验证通用逻辑与模拟 macOS 行为；首次生成安装包后，仍需在真实 Mac 上完成文末验收。

## 使用 GitHub Actions 构建

将本次代码提交到 GitHub；合并到默认分支后，可手动触发：

1. 打开仓库 **Actions → Build macOS → Run workflow**。
2. 等待 `macOS arm64` 和 `macOS x86_64` 两项任务完成。
3. 在该次运行的 **Artifacts** 中下载对应架构的文件。

修改相关文件的 Pull Request、推送 `codex/macos-*` 分支或 `v*` 标签也会触发构建。
工作流仅上传构建产物，不自动发布 GitHub Release。
无需配置签名证书、Apple 账号或学习通账号。离线测试不会访问学习通或真实钥匙串。

每种架构包含：

```text
xxt-assistant-v<版本号>-macos-arm64.dmg
xxt-assistant-v<版本号>-macos-arm64.zip
SHA256SUMS.txt
```

Intel 的文件名使用 `x86_64`。GitHub 的 Artifacts 下载本身也是 ZIP；先解开外层 ZIP，里面才是上述安装包。

## 在 Mac 本机构建

需要 Python 3.13，以及包含 `iconutil`、`codesign`、`hdiutil`、`ditto` 的 macOS 构建环境。
在项目根目录执行：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements-build-macos.txt

# M 系列 Mac，使用 ARM 原生 Python
.venv/bin/python tools/build_macos.py --arch arm64

# Intel Mac，使用 Intel Python
.venv/bin/python tools/build_macos.py --arch x86_64
```

只执行符合当前 Python 架构的一条构建命令。构建脚本会检查架构、创建 `.icns` 图标、
收集应用与钥匙串后端、临时签名、验证签名并执行包内 `--help`，然后生成 DMG 和 ZIP。
ZIP 使用 `ditto` 保留 Qt framework 的符号链接及文件权限。

产物位于 `dist/macos-arm64/` 或 `dist/macos-x86_64/`，不清理已有 Windows 构建产物。
打包范围仅包含程序、依赖和 `assets/`，不包含本地 `config.json`、`storage.json`、`data/` 或账号信息。

## 安装与首次打开

1. M 系列选择 `arm64` 包，Intel 选择 `x86_64` 包。
2. 打开 DMG，将“学习通小助手”拖入“应用程序”；弹出 DMG 后从“应用程序”启动。
3. 如果系统提示无法验证开发者，确认来源可信且包未被改动，再进入 **系统设置 → 隐私与安全性 → 仍要打开**，确认打开。
4. 顶部菜单栏图标可显示/隐藏清单、登录、同步、打开设置和退出。收起卡片不会退出。

Apple 对首次打开的说明：[在 Mac 上安全地打开 App](https://support.apple.com/zh-cn/102445)。

可以在安装包所在目录核对校验值：

```bash
shasum -a 256 -c SHA256SUMS.txt
```

## 数据、账号与自启动

默认目录：

```text
~/Library/Application Support/xxt-assistant/
  config.json
  storage.json                 # 选择其他数据目录后的位置记录
  data/
    xxt.db
    session_cookies.json
    app.log
    crash.log
```

程序不会向 `.app` 包或 DMG 写数据。设置 → 维护可迁移数据目录；位置记录保留在上述用户目录。

“记住账号密码”通过 `keyring` 的原生 macOS 后端存入当前用户登录钥匙串，服务名为
`io.github.led629263224-cmyk.xxt-assistant`。密码不写进配置文件；Cookie 和课程缓存仍在数据目录。
首次访问或应用更新后，系统可能要求授权访问钥匙串。如果拒绝保存，可以取消“记住账号密码”后重新登录。
Windows 的 DPAPI 凭据不能直接迁移到 Mac，需要重新登录。

“清除已保存的账号密码”会删除钥匙串条目；“清除全部本地数据”还会清理 Cookie、缓存和设置。
仅迁移数据目录不会复制或删除钥匙串条目。卸载前如需清理账号，请先在应用内清除。

设置 → 界面中的“登录后自动启动”写入：

```text
~/Library/LaunchAgents/io.github.led629263224-cmyk.xxt-assistant.plist
```

开关在**下次登录 macOS 时生效**，启动后只留菜单栏入口。关闭开关会删除该文件。
启用前请先将应用放入“应用程序”；移动应用后应重新启用，使启动路径更新。
系统若拦截后台登录项，请检查系统设置中的登录项管理。自启动日志写入数据目录的 `launch-error.log`。

## 发布前的 Mac 验收

- 无 Python 环境时，从 DMG 安装并首次打开；核对架构和签名验证结果。
- 登录、钥匙串允许/拒绝、退出后重新启动、清除凭据及清除全部数据。
- 切换其他应用时卡片保持可见；分别检查普通层级和置顶，以及多屏、全屏、Spaces。
- 菜单栏打开菜单不切换卡片；“显示/隐藏任务清单”正确生效；退出后不残留进程。
- 背景图、数据目录迁移，以及升级替换 `.app` 后数据保留。
- 系统通知权限和提醒实际到达（当前复用 Qt 的系统通知接口，需实机验证）。
- 开启和关闭登录自启后分别重新登录；检查休眠唤醒后的同步。
- 在 macOS 12 和较新系统分别验证后，再确定正式公布的支持范围。

正式分发若需要 Apple 认可的开发者身份，再接入 Developer ID 签名和公证；
当前脚本不上传任何内容到 Apple，也不要求购买证书。
