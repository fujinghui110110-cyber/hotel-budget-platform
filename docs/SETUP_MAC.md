# macOS 安装与启动

本文描述当前 Mac 原生主方案。系统由项目根目录的 `scripts/local_server.py` 管理，默认绑定 `127.0.0.1:8768`，启动后可关闭终端。Docker、Cloudflare Tunnel 和登录后自启都不是本机首次运行的前置条件。

## 1. 前置条件

需要：

- macOS；
- Python 3.13 或更高版本；
- LibreOffice，用于 Excel 公式重算和文件转换；
- 可写的项目目录和项目目录之外的业务数据目录。

检查 Python 和 LibreOffice：

```sh
python3 --version
command -v soffice || true
test -x /Applications/LibreOffice.app/Contents/MacOS/soffice && \
  /Applications/LibreOffice.app/Contents/MacOS/soffice --headless --version
```

没有 LibreOffice 时可用 Homebrew 安装：

```sh
brew install --cask libreoffice
```

## 2. 建立虚拟环境

在实际 checkout 目录执行。当前机器的示例路径是 `/Users/frank/Documents/ChatGPT/预算系统`：

```sh
cd "/Users/frank/Documents/ChatGPT/预算系统"
sh scripts/setup_mac.sh
```

脚本会用系统 `python3` 建立 `.venv`，并安装 `requirements.txt`。`requirements.txt` 与 `pyproject.toml` 的运行时依赖保持一致，包含 Django、openpyxl、Gunicorn 和 WhiteNoise。安装后检查：

```sh
.venv/bin/python --version
.venv/bin/python -m django --version
.venv/bin/python manage.py check
```

## 3. 准备本机配置

开发或本机演示可复制 `.env.example` 为根目录 `.env`。本机正式运行可复制 `.env.production.example` 为根目录 `.env.production`：

```sh
cp .env.production.example .env.production
```

编辑 `.env.production`：

1. 把 `DJANGO_SECRET_KEY=CHANGE_ME` 替换为至少 50 个字符的随机值；
2. 保留 `DJANGO_DEBUG=0`，并将 `DJANGO_ALLOWED_HOSTS` 限制为 `127.0.0.1,localhost`；
3. 本机 HTTP 保持 `TRUST_PROXY=0`、`SECURE_SSL_REDIRECT=0`、`SESSION_COOKIE_SECURE=0`、`CSRF_COOKIE_SECURE=0`；
4. 把 `DATABASE_PATH` 和 `BUDGET_STORAGE_ROOT` 改为 Git checkout 之外的本机目录；
5. 让目录先存在，并确保当前 macOS 账号可读写：

```sh
mkdir -p "$HOME/预算系统数据/storage"
```

启动器会优先读取根目录 `.env.production`，再读取 `.env`，并保留已经存在的进程环境变量。不要同时保存互相冲突的值；正式配置会覆盖本地默认值。

## 4. 初始化数据库和模板

`local_server.py` 会自动读取根目录环境文件，但直接运行 `manage.py` 不会自动读取它们。首次空环境前，请只对自己创建且可信的配置文件执行导出，再运行迁移和管理账号创建：

```sh
set -a
. ./.env.production  # 演示环境可改为 ./.env
set +a
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py createsuperuser
```

不要在正式数据上运行演示数据命令。确认项目账号、项目绑定和预算周期后，再登记指定年度的最终 V3 模板：

```sh
PYTHON_BIN=.venv/bin/python ./deploy/activate_template.sh 2027
```

该命令需要明确的年度参数，并校验 `artifacts/v3/<年度>/` 中的模板与 manifest；它不会创建演示账号，也不会删除数据库。

## 5. 启动、状态和停止

推荐使用根目录启动器：

```sh
./启动预算统筹系统.command
```

它会调用 `scripts/local_server.py start --open`。服务启动后打开：

```text
http://127.0.0.1:8768/
```

命令行检查：

```sh
./启动预算统筹系统.command status
curl -fsS http://127.0.0.1:8768/healthz
./停止预算统筹系统.command
```

`local_server.py` 启动一个 Gunicorn Web 进程和一个 `budget_worker`，上传在生产配置下入队后由 worker 处理。服务日志分别写入 `logs/server.log`、`logs/web.log` 和 `logs/worker.log`；状态文件在 `.runtime/server.json`。重复执行启动命令会复用已健康的本机服务，不会杀掉其他端口服务。

## 6. 管理端底稿导出

管理账号登录后打开 `/management/workpapers/`，先选择预算周期。每个项目可分别下载：

- 当前批准版本的原始 XLSX；
- 当前批准版本的重算 XLSX；
- 最新上传版本的原始 XLSX；
- 最新上传版本的重算 XLSX。

页面顶部还提供按当前周期批量下载 ZIP。ZIP 中有项目分类文件和来源清单，清单记录上传 ID、状态、文件大小和 SHA-256；下载行为会写入审计记录。项目没有可导出的版本、文件缺失或批准指针不明确时，应先处理异常，不用空文件代替。

## 7. LibreOffice 检查

健康检查包含数据库、存储目录和 LibreOffice 路径。需要单独确认版本时执行：

```sh
SOFFICE_BIN=/Applications/LibreOffice.app/Contents/MacOS/soffice
"$SOFFICE_BIN" --headless --version
curl -fsS http://127.0.0.1:8768/healthz
```

重算只能使用上传原件的副本。不要让 LibreOffice 覆盖原始上传文件；如果 Excel/WPS 与 LibreOffice 的结果不同，保留原件、重算副本、日志和哈希并转人工复核。

## 8. 常见问题

- `No such file or directory: .venv/bin/python`：先运行 `sh scripts/setup_mac.sh`。
- 端口 `8768` 被占用：执行 `./启动预算统筹系统.command status`；若健康检查不是本系统，先查明占用者，不要让启动器终止其他服务。
- 页面能开但上传没有完成：查看 `logs/worker.log`，确认 `BUDGET_PROCESS_UPLOAD_INLINE=0` 时 worker 正在运行。
- `soffice` 不存在：安装 LibreOffice，或在 `.env` / `.env.production` 中设置实际的 `SOFFICE_BIN`。
- 正式配置启动失败并提示 `DJANGO_SECRET_KEY`：检查是否仍为 `CHANGE_ME` 或长度不足 50 个字符。

公网接入使用独立进程及安全配置，无需改变本机 HTTP 设置。无域名试用见 [公网接入](公网接入.md)；固定域名方式见 [部署方案](部署方案.md)。登录后自启仍为可选。
