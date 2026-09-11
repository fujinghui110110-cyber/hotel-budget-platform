# Windows 电脑作为预算系统服务器

本机入口固定为 **http://127.0.0.1:8768/**。这个地址仅指当前电脑：服务器电脑可以使用它登录；其他项目电脑使用管理端生成的 HTTPS 公网链接。无需域名，公网链接每次重新生成都可能变化，旧链接届时失效。

## 首次安装

1. 下载含依赖的部署包并解压到长期保留的目录，例如 `D:\HotelBudget`。支持 Windows 10/11 x64，不支持 ARM64 或 32 位 Windows。
2. 双击 `安装预算统筹系统-Windows.bat`。程序会检测 Python 3.13 x64 和 LibreOffice；已有兼容安装直接使用，缺少时自动安装。LibreOffice 安装可能弹出 Windows 管理员授权窗口，选择允许即可。
3. 按提示设置管理员用户名和强密码，安装完成自动打开固定本机地址。

完整部署包的 `offline/windows/` 包含 Python 3.13.15、LibreOffice 26.2.6 官方安装器，cloudflared 公网客户端，以及系统所需 Python wheel。安装时先验证 SHA256 再执行，Python 库可直接离线安装；离线包缺损时明确报错，不会静默切换联网。仅下载 GitHub 源码时不会带大体积安装器，脚本会从官方地址下载缺失安装器，并联网安装 Python 库。公网链接本身仍需要联网。

Python 是系统运行环境；LibreOffice 在后台重新计算 Excel 公式，补足某些表格缺少或过期的公式缓存，否则读取预算时可能出现“公式有内容但金额为空”。它不要求用户日常打开操作，也不会替代原始预算文件。安装仅发生在服务器电脑，各项目只需浏览器。

已有 `.env` 或 `.env.production` 不会覆盖；其中旧电脑路径须按下方迁移说明核对。初始化只创建缺少的数据库结构和管理员，不插入模拟业务数据。

## 日常使用

双击 `启动预算统筹系统-Windows.bat`，系统在后台运行并打开浏览器。关闭启动窗口或浏览器不会停止后台服务。管理员在系统的“公网访问”页面生成链接，生成过程会重启公网服务；完成后复制新链接发给项目公司。

要停止本机系统，双击 `停止预算统筹系统-Windows.bat`。该文件会先停止公网访问，再停止本机服务；若正在生成链接，会等待操作完成后再停止，最多等待5分钟。失败时窗口显示原因并保留本机服务，方便检查。重新开机后双击启动文件即可。如需登录 Windows 后自动启动，可按 Win+R 输入 `shell:startup`，把启动文件的**快捷方式**放入此目录。此方式在用户登录后启动，不是无人登录的 Windows 系统服务。

服务器运行期间须保持电脑开机、联网，Windows 电源设置中关闭自动睡眠。不要直接把本机端口映射到公网；项目通过系统生成的 HTTPS 链接访问。

## 数据与迁移

代码放 GitHub，预算原件、数据库和密码保存在服务器电脑。全新安装只有空数据库和新管理员；迁移现有系统必须额外带上原数据和模板。

迁移前停止旧电脑的本机与公网服务，安全备份并复制以下内容到新项目目录，保持相对目录不变：

- `db.sqlite3`：业务数据和账号。
- `storage/`：上传原件、重算结果、专项指标原件及归档文件。
- `artifacts/`：基础模板及 manifest，现有基础模板位于 `artifacts/v3/2027/`，不能遗漏。
- `source/`（旧目录存在时）：模板来源材料；如果数据库或 `SOURCE_WORKBOOK` 引用了这里的文件，必须复制。
- `.env`、`.env.production`（存在时）：逐项核对再使用。`DATABASE_PATH`、`BUDGET_STORAGE_ROOT`、`SOURCE_WORKBOOK` 等 Mac 绝对路径须改成新电脑路径；`SOFFICE_BIN` 改成实际 Windows LibreOffice 路径，例如 `C:\Program Files\LibreOffice\program\soffice.exe`。`.env.production` 优先。

不复制旧电脑的 `.venv` 或 `.runtime`，运行 Windows 安装程序创建本机运行环境。若安装程序已启动系统，先双击停止文件，再运行下面的路径迁移命令。此工具只修改数据库中已知的文件引用，不改写预算 Excel 或 manifest 原件；专项指标 FileField 会转换为 storage 内相对路径。默认仅预览：

```bat
.venv\Scripts\python.exe scripts\relocate_data.py --old-root "/Users/frank/Documents/ChatGPT/预算系统" --report "迁移预览.json"
```

确认报告后执行：

```bat
.venv\Scripts\python.exe scripts\relocate_data.py --old-root "/Users/frank/Documents/ChatGPT/预算系统" --apply --report "迁移执行.json"
```

`--old-root` 必须填写旧项目真实根目录。工具默认操作新目录的 `db.sqlite3`；若使用自定义数据库，显式加 `--database "D:\数据\db.sqlite3"`。执行修改前会在数据库旁生成 `.before-relocate-时间.bak` 备份。只更新新位置已存在的文件或目录引用；缺失文件、旧根目录外的引用保留原值并列入报告，退出码为 2。先补齐对应文件再以新的报告文件名重试，勿把退出码 2 当作已完整迁移。工具不自动修改环境文件、审计历史或任意 JSON 内的来源文字。

报告无未解决项后，再双击启动文件，检查模板下载、项目报表和原件导出。不要把数据库、账号清单、环境文件或原始预算上传到 GitHub。

## 排查

- 找不到 `py`：安装 Python 3.13 及 Launcher 后重试。
- LibreOffice 安装失败：检查 Windows 管理员授权是否允许，按退出码处理后重新运行安装批处理。
- 端口占用：启动程序会报错，不会结束其他软件。检查占用 8768 的应用。
- 启动失败：查看 `logs/server.log`、`logs/web.log`、`logs/worker.log`。
- 本机能打开、公网生成失败：在公网访问页面查看失败原因，检查网络对 Cloudflare 的连通性。Quick Tunnel 是临时公网入口，其可用性取决于网络和 Cloudflare 服务。

Windows 使用 Waitress 提供 Web 服务，Mac 使用 Gunicorn，两个平台均运行独立的预算处理任务。Windows 兼容分支已做自动检查，但当前开发环境为 Mac；交付不包含 Windows 实机验收结论。

## 制作含依赖的部署包（维护者）

运行 `python3 scripts/prepare_windows_offline.py`。脚本从官方固定版本地址取得 Python、LibreOffice 安装器和 cloudflared 公网客户端，与 `scripts/windows_dependencies.json` 中官方发布的 SHA256 核对，再下载 Windows Python 3.13 x64 wheels。将生成的 `offline/windows/` 随源码打入部署压缩包，勿提交大体积安装器到 Git。更新版本时必须同步官方校验值。

当前 Mac 已核验下载文件及离线 wheels；Windows 安装和 UAC 流程仍需 Windows 实机验收。
