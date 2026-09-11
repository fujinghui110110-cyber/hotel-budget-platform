# Windows 电脑作为预算系统服务器

本机入口固定为 **http://127.0.0.1:8768/**。这个地址仅指当前电脑：服务器电脑可以使用它登录；其他项目电脑使用管理端生成的 HTTPS 公网链接。无需域名，公网链接每次重新生成都可能变化，旧链接届时失效。

## 首次安装

1. 从本系统私有 GitHub 仓库下载当前版本并解压到长期保留的目录，例如 `D:\HotelBudget`。不要放进会自动清理的临时目录。
2. 安装 Python 3.13（包含 Python Launcher `py`），以及 LibreOffice Windows 版。LibreOffice 用于 Excel 公式重算。
3. 双击根目录的 `安装预算统筹系统-Windows.bat`。安装程序创建 `.venv`、安装依赖、初始化数据库，并让你设置管理员用户名和强密码（至少 12 位）。输入密码时终端不显示字符是正常的。不会添加演示项目或预算。
4. 安装成功后浏览器自动打开本机地址。使用刚创建的管理员账号登录。

安装程序需要网络下载 Python 依赖；公网组件由系统的公网访问功能准备。下载失败时保留本机数据，恢复网络后可重试。脚本不会覆盖已有 `.env` 或重置已有管理员密码。若已有 `.env.production`，其配置优先于 `.env`，迁移旧电脑时须检查其中路径。

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
- 找不到 LibreOffice：使用官方 Windows 安装包安装至默认目录，再运行安装程序。
- 端口占用：启动程序会报错，不会结束其他软件。检查占用 8768 的应用。
- 启动失败：查看 `logs/server.log`、`logs/web.log`、`logs/worker.log`。
- 本机能打开、公网生成失败：在公网访问页面查看失败原因，检查网络对 Cloudflare 的连通性。Quick Tunnel 是临时公网入口，其可用性取决于网络和 Cloudflare 服务。

Windows 使用 Waitress 提供 Web 服务，Mac 使用 Gunicorn，两个平台均运行独立的预算处理任务。Windows 兼容分支已做自动检查，但当前开发环境为 Mac；交付不包含 Windows 实机验收结论。
