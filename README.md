# 酒店预算统筹管理系统

## 日常使用只需双击一个文件

- **Mac：`一键启动.command`**
- **Windows：`一键启动-Windows.bat`**

服务准备好后会自动打开系统网页，默认地址 `http://127.0.0.1:8768/`。再次双击会打开已运行的系统。Windows首次使用也用这个入口。停止、管理员修复及登录自启工具收纳在 `scripts/maintenance/`；公网访问在登录后的管理页面操作。


这是一个面向酒店管理公司财务管理端的预算统筹系统。管理端维护预算周期、统一 Excel 模板、项目版本、汇总报表、预算测算、审核问答和冻结快照；项目端用自己的项目账号下载模板、填报、上传并查看校验结果。

系统按预算年度 `T` 组织数据。为编制下一年度预算，默认同时保留两年前和三年前的实际、上一年度的预测以及 `T` 年预算：

| 口径 | 年度 | 含义 |
|---|---:|---|
| `T-3` | `T - 3` | 实际 |
| `T-2` | `T - 2` | 实际 |
| `T-1` | `T - 1` | 本年度预测 |
| `T` | `T` | 下一年度预算 |

年度由预算周期决定，不在代码或页面中硬编码为 2027 年。金额、比率、来源工作表、单元格、公式和文件 SHA-256 均保留可追溯关系。

## 日常使用与 Windows 安装

Windows 与 Mac 的本机入口统一为 **http://127.0.0.1:8768/**。在服务器电脑上登录管理员账号，进入 **系统设置 → 公网访问**，点击 **生成公网链接**。系统会重启独立的公网服务，自动显示可复制的新链接；原公网链接失效，本机服务保持可用。项目公司使用公网链接和各自账号登录。

Windows 首次使用请阅读 [Windows 部署与迁移](docs/WINDOWS部署.md)，双击 `一键启动-Windows.bat`；以后双击 `一键启动-Windows.bat`。代码可以保存在 GitHub，数据库和预算原件保存在服务器电脑。下载代码不会带入现有项目数据，换电脑时须按文档迁移。

`127.0.0.1` 始终指当前电脑，不是跨电脑通用的服务器地址。无需域名的公网链接是临时地址；服务器需保持开机、联网且不休眠。

## 在 Mac 上启动

Mac 本机服务只监听 `127.0.0.1:8768`，数据保存在本机。需要外部访问时，在登录后的“公网访问”页面启动官方 Cloudflare 隧道。更多说明见 [公网接入](docs/公网接入.md)。

在终端执行：

```sh
cd "/path/to/预算系统"
sh scripts/setup_mac.sh
./一键启动.command
```

`setup_mac.sh` 会建立 `.venv` 并安装 `requirements.txt`。启动器随后由 `scripts/local_server.py` 执行迁移、Django 检查、静态文件收集，并在后台启动 Gunicorn 和 `budget_worker`；可以关闭启动终端。浏览器地址为：

```text
http://127.0.0.1:8768/
```

状态与停止：

```sh
.venv/bin/python scripts/local_server.py status
curl -fsS http://127.0.0.1:8768/healthz
./scripts/maintenance/停止预算统筹系统.command
```

日志在 `logs/server.log`、`logs/web.log` 和 `logs/worker.log`，运行状态在 `.runtime/server.json`。启动器不会初始化演示账号、重置密码或删除已有数据。直接运行 `manage.py` 不会自动读取 `.env`；首次空数据库前，请只对自己创建且可信的配置文件执行导出：

```sh
set -a
. ./.env.production  # 本机正式配置；演示环境可改为 ./.env
set +a
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py createsuperuser
```

本机和公网分别提供启动/关闭入口；启用公网前须更换演示密码。隧道运行时暂时防止系统空闲休眠，合盖、手动休眠、关机或断网仍会中断访问。

## 配置与数据边界

启动管理器会优先读取项目根目录的 `.env.production`，再读取 `.env`；同一变量不要在两个文件中写不同值，正式配置会覆盖本地默认值。本机正式配置可由 [.env.production.example](.env.production.example) 复制为 `.env.production` 后填写。正式配置必须使用至少 50 个字符的随机 `DJANGO_SECRET_KEY`，并明确填写 `DJANGO_ALLOWED_HOSTS`。

本机 HTTP 配置应保持 `SECURE_SSL_REDIRECT=0`、`SESSION_COOKIE_SECURE=0` 和 `CSRF_COOKIE_SECURE=0`；这只适用于回环地址。`BUDGET_PROCESS_UPLOAD_INLINE=0` 时上传由 `budget_worker` 异步处理，避免请求线程执行 LibreOffice。LibreOffice 的实际路径通过 `SOFFICE_BIN` 配置。

数据库、上传原件、重算文件、冻结产物、日志、备份和环境文件都属于业务数据，不提交 GitHub。私有 GitHub 只保存源代码、部署文件和经过确认的标准模板；它不是数据库或业务文件存储。建议把 `DATABASE_PATH` 和 `BUDGET_STORAGE_ROOT` 放在 Git checkout 之外的本机受控目录，并先创建目录：

```sh
mkdir -p "$HOME/预算系统数据/storage"
```

## 管理端和项目端

管理端常用入口：

| 功能 | 地址 |
|---|---|
| 预算总览 | `/management/planning/` |
| 预算周期 | `/management/cycles/` |
| 项目预算 | `/management/project-budgets/` |
| 模板管理 | `/management/templates/` |
| 汇总报表 | `/management/reports/` |
| 预算测算 | `/management/scenarios/` |
| 审核问答 | `/questions/` |
| 调整与冻结 | `/management/adjustments/`、`/management/freeze/` |
| 预算底稿导出 | `/management/workpapers/` |

项目端从 `/project/` 开始：下载 `/project/template/` 的统一模板，上传到 `/project/uploads/new`，在上传详情页修复校验问题并提交管理端复核。只有批准版本才进入管理汇总；未批准、拒绝或处理中版本不会替换当前正式版本。

管理端的“预算底稿导出”页面支持按预算周期查看各项目，并分别下载当前批准版本或最新上传版本的原始 XLSX、重算 XLSX；页面也提供批量 ZIP。批量包包含来源和 SHA-256 清单，下载行为写入审计记录。缺少文件或版本不明确时，系统拒绝该文件或在清单中标明未导出原因，不用空文件代替。

## 模板和年度口径

最终 V3 模板与 manifest 位于 `artifacts/v3/<预算年度>/`。正式空环境登记模板前执行：

```sh
PYTHON_BIN=.venv/bin/python ./deploy/activate_template.sh 2027
```

命令中的年度必须明确指定；系统会校验 manifest、公式指纹和文件路径，不会自动从真实源工作簿重建模板。编制下一年度时使用对应年度的模板，并继续从前两年实际和本年度预测取数。

业务勾稽关系必须以模板和经确认的业务口径为准，不能使用“其他支出”作为平衡项。当前合成导入和浏览器走查证据只用于功能验证，不等同于真实项目预算或正式审批。

## 外部访问和备用方案

没有域名时可按 [公网接入](docs/公网接入.md) 使用 Quick Tunnel 获得临时 HTTPS 地址，重启会变化。需要长期固定地址时，可参考 [deploy/cloudflared/config.yml.example](deploy/cloudflared/config.yml.example) 建立命名 Tunnel，将外部 HTTPS 域名转发到 Mac 的 `http://127.0.0.1:8768`。命名隧道配置示例不会自动创建隧道；不要提交 Cloudflare JSON 凭据。

外部 HTTPS 验收完成后，应用配置才可以切换为 `TRUST_PROXY=1`、`SECURE_SSL_REDIRECT=1`、`SESSION_COOKIE_SECURE=1`、`CSRF_COOKIE_SECURE=1`，并填写实际域名的 `DJANGO_ALLOWED_HOSTS` 和 `CSRF_TRUSTED_ORIGINS`。本机生产示例保持 HTTP cookie 设置，不应直接拿到公网使用。

Docker 与 Caddy 文件保留在 `compose.yaml`、`Dockerfile` 和 `deploy/` 中，作为未来迁移或隔离运行的备用方案；Mac 原生 `scripts/local_server.py` 是当前主方案。Docker 的数据卷仍需备份，SQLite 不支持通过增加多个 Web 实例横向扩展。

## 备份、恢复与限制

备份至少包含 `DATABASE_PATH` 指向的 SQLite 文件和 `BUDGET_STORAGE_ROOT` 下的全部业务文件，并保存文件清单和 SHA-256。恢复时先停止服务，在临时目录校验数据库、上传版本、来源文件和 manifest，再由管理者确认后替换工作数据。具体检查顺序见 [docs/RECOVERY.md](docs/RECOVERY.md)。

当前 MVP 是单机 SQLite 加本地文件存储，不提供在线热备、跨机房容灾、自动 PITR 或多人并发冲突解决。生产使用前仍需完成真实业务数据、Excel 与 LibreOffice 黄金样本逐单元格对账，以及正式上线验收。

更多操作说明见 [docs/SETUP_MAC.md](docs/SETUP_MAC.md)、[docs/部署方案.md](docs/部署方案.md) 和 [docs/RECOVERY.md](docs/RECOVERY.md)。

## GitHub 自动检查

代码仓库为私有仓库。当前 GitHub 登录授权不含 `workflow` 权限，因此自动检查配置保存在 `deploy/github-actions-ci.yml.example`，尚未启用 GitHub Actions。账号获得相应权限后，可将该文件放到 `.github/workflows/ci.yml` 并提交。当前版本已在本机的独立发布副本中运行测试。

专项指标模板导入、预算自动对应及图表使用见 [专项指标对比](docs/专项指标对比.md)。

### Windows 离线安装包

Windows x64 首次安装可直接解压完整离线包，双击 `一键启动-Windows.bat`。系统自动检测 Python 3.13 和 LibreOffice，缺失时使用包内安装器安装；Python 依赖和公网连接程序也已随包提供。LibreOffice 安装可能弹出 Windows 管理员确认。详见 [Windows 部署说明](docs/WINDOWS部署.md)。

LibreOffice 用于重新计算上传 Excel 的公式，避免公式缓存为空或过期导致汇总数据缺失；项目人员无需手动打开它。生成公网链接时仍需要联网。
