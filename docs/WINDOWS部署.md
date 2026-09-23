# Windows 电脑作为预算系统服务器

本机入口固定为 **http://127.0.0.1:8768/**。这个地址仅指当前电脑：服务器电脑可以使用它登录；其他项目电脑使用管理端生成的 HTTPS 公网链接。无需域名，公网链接每次重新生成都可能变化，旧链接届时失效。

## 首次安装

1. 下载含依赖的部署包并解压到长期保留的目录，例如 `D:\HotelBudget`。支持 Windows 10/11 x64，不支持 ARM64 或 32 位 Windows。
2. 双击 `一键启动-Windows.bat`。程序会检测 Python 3.13 x64 和 LibreOffice；已有兼容安装直接使用，缺少时自动安装。LibreOffice 安装可能弹出 Windows 管理员授权窗口，选择允许即可。
3. 按提示设置管理员用户名和强密码，安装完成自动打开固定本机地址。

完整部署包的 `offline/windows/` 包含 Python 3.13.15、LibreOffice 26.2.6 官方安装器，cloudflared 公网客户端，以及系统所需 Python wheel。安装时先验证 SHA256 再执行，Python 库可直接离线安装；离线包缺损时明确报错，不会静默切换联网。仅下载 GitHub 源码时不会带大体积安装器，脚本会从官方地址下载缺失安装器，并联网安装 Python 库。公网链接本身仍需要联网。

Python 是系统运行环境；LibreOffice 在后台重新计算 Excel 公式，补足某些表格缺少或过期的公式缓存，否则读取预算时可能出现“公式有内容但金额为空”。它不要求用户日常打开操作，也不会替代原始预算文件。安装仅发生在服务器电脑，各项目只需浏览器。

已有 `.env` 或 `.env.production` 不会覆盖；其中旧电脑路径须按下方迁移说明核对。初始化只创建缺少的数据库结构和管理员，不插入模拟业务数据。

## 日常使用

双击 `一键启动-Windows.bat`，系统在后台运行并打开浏览器。关闭启动窗口或浏览器不会停止后台服务。管理员在系统的“公网访问”页面生成链接，生成过程会重启公网服务；完成后复制新链接发给项目公司。

要停止本机系统，双击 `scripts/maintenance/停止预算统筹系统-Windows.bat`。该文件会先停止公网访问，再停止本机服务；若正在生成链接，会等待操作完成后再停止，最多等待5分钟。失败时窗口显示原因并保留本机服务，方便检查。重新开机后双击启动文件即可。如需登录 Windows 后自动启动，可按 Win+R 输入 `shell:startup`，把启动文件的**快捷方式**放入此目录。此方式在用户登录后启动，不是无人登录的 Windows 系统服务。

服务器运行期间须保持电脑开机、联网，Windows 电源设置中关闭自动睡眠。不要直接把本机端口映射到公网；项目通过系统生成的 HTTPS 链接访问。

## 数据与迁移

代码放 GitHub，预算原件、数据库、已使用模板和密码保存在服务器电脑。源码和在线更新包只带源代码、部署文件及发布校验清单，不带业务数据库、上传底稿或业务模板；全新安装或更新完成后，由管理员在“模板管理”中按实际预算年度和预算版本发布空白模板。迁移现有系统时才复制数据库、storage、历史模板和配置。

迁移前停止旧电脑的本机与公网服务，安全备份并复制以下内容到新项目目录，保持相对目录不变：

- `db.sqlite3`：业务数据和账号。
- `storage/`：上传原件、重算结果、专项指标原件及归档文件。
- `artifacts/` 或业务数据目录中的模板文件：旧系统已经使用的模板、manifest 及其历史引用，迁移时不能遗漏；这些文件不从源码或在线更新包补齐。
- `source/`（旧目录存在时）：模板来源材料；如果数据库或 `SOURCE_WORKBOOK` 引用了这里的文件，必须复制。
- `.env`、`.env.production`（存在时）：逐项核对再使用。`DATABASE_PATH`、`BUDGET_STORAGE_ROOT`、`SOURCE_WORKBOOK` 等 Mac 绝对路径须改成新电脑路径；`SOFFICE_BIN` 改成实际 Windows LibreOffice 路径，例如 `C:\Program Files\LibreOffice\program\soffice.exe`。`.env.production` 优先。

不复制旧电脑的 `.venv` 或 `.runtime`，运行 Windows 安装程序创建本机运行环境。若安装程序已启动系统，先双击停止文件，再运行下面的路径迁移命令。此工具只修改数据库中已知的文件引用，不改写预算 Excel 或 manifest 原件；专项指标 FileField 会转换为 storage 内相对路径。默认仅预览：

```bat
set "OLD_ROOT=D:\OldHotelBudget"
.venv\Scripts\python.exe scripts\relocate_data.py --old-root "%OLD_ROOT%" --report "迁移预览.json"
```

确认报告后执行：

```bat
set "OLD_ROOT=D:\OldHotelBudget"
.venv\Scripts\python.exe scripts\relocate_data.py --old-root "%OLD_ROOT%" --apply --report "迁移执行.json"
```

`--old-root` 必须填写旧项目真实根目录。工具默认操作新目录的 `db.sqlite3`；若使用自定义数据库，显式加 `--database "D:\数据\db.sqlite3"`。执行修改前会在数据库旁生成 `.before-relocate-时间.bak` 备份。只更新新位置已存在的文件或目录引用；缺失文件、旧根目录外的引用保留原值并列入报告，退出码为 2。先补齐对应文件再以新的报告文件名重试，勿把退出码 2 当作已完整迁移。工具不自动修改环境文件、审计历史或任意 JSON 内的来源文字。

报告无未解决项后，再双击启动文件，检查模板下载、项目报表和原件导出。不要把数据库、账号清单、环境文件或原始预算上传到 GitHub。

## 旧版在线升级

旧版服务器必须先在系统更新页面安装 `2026.09.23.0` 过渡版，再重新检查并安装 `2026.09.23.1` 等现代版本。`2026.09.23.0` 只更新在线升级器，保留旧业务代码和数据库 schema；不要让旧版直接安装需要新 schema 的现代版本。升级前仍须备份数据库、上传文件、历史模板和配置。

过渡版 `v2026.09.23.0` 继续保持 GitHub `latest`，供旧客户端桥接。现代版本发布时使用 `--latest=false`，避免移动旧客户端依赖的 `latest`；现代升级器读取 GitHub `/releases`，过滤稳定、非 draft、非 prerelease 且带完整校验资产的版本，并选择最高版本。只有正式 Release 以及对应提交的 CI/provenance 证据齐全时，版本才可作为在线更新来源；GitHub Actions 或隔离升级记录不等于 Windows 实机验收，当前文档不把 Windows 实机验收写成已完成。

私有仓库的更新凭据仅需本仓库的 `Contents: Read-only` 和 `Attestations: Read-only` 权限，不授予写权限。即使仓库公开，当前系统更新页面仍要求配置凭据；按 [GitHub 更新凭据说明](GitHub更新凭据一步一步操作.md) 操作。服务器缺少 `gh` 时，更新器会通过 `scripts/github_cli.py` 下载官方固定版本并校验固定 SHA-256，失败或校验不通过就拒绝更新。

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

## 首次管理员登录与修复

首次安装不再要求在命令行手动创建用户。没有管理员时，系统自动创建独立的强密码管理员，并打开“本机账号信息”文件夹中的登录信息文件。按文件中的用户名与密码登录；不是其他电脑的旧密码。已有管理员时再次安装不会改密码。

旧安装包创建账号失败或忘记密码时，双击系统文件夹内“scripts/maintenance/修复管理员登录-Windows.bat”，选择管理员后输入 Y，复制新文件中的登录信息重新登录。只重置所选管理员密码，保留所有项目和预算；不要删除 db.sqlite3 或 storage。
