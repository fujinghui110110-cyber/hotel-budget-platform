# 数据备份与恢复

当前系统是单机 SQLite 加本地文件存储。项目没有自动备份脚本；备份和恢复由管理员按本页执行，并保留清单、操作者、时间和 SHA-256 证据。

## 备份前停止服务

先停止后台 Web 和 worker，避免复制过程中 SQLite 或上传文件继续变化：

```sh
cd "/path/to/预算系统"
./scripts/maintenance/停止预算统筹系统.command
```

确认服务已停止：

```sh
.venv/bin/python scripts/local_server.py status || true
```

`status` 显示未就绪并不等于某个其他服务已经停止；如果 `8768` 被其他程序占用，应先查明占用者。

## 备份内容

从当前 `.env.production` 或受控运维记录中确认实际路径。下面示例假定数据放在 `$HOME/预算系统数据`，请按实际路径修改：

```sh
DATA_ROOT="$HOME/预算系统数据"
BACKUP_ROOT="$HOME/预算系统备份/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_ROOT"
cp "$DATA_ROOT/db.sqlite3" "$BACKUP_ROOT/db.sqlite3"
ditto "$DATA_ROOT/storage" "$BACKUP_ROOT/storage"
(
  cd "$BACKUP_ROOT"
  find . -type f ! -name 'SHA256SUMS.txt' -print | sort |
    while IFS= read -r file; do shasum -a 256 "$file"; done
) > "$BACKUP_ROOT/SHA256SUMS.txt"
```

备份最小范围是 `DATABASE_PATH` 指向的 SQLite 文件和 `BUDGET_STORAGE_ROOT` 下全部文件，包括原始上传、重算副本、冻结产物和所需 manifest。若数据库或 storage 不在示例目录，必须复制实际路径；不要只备份数据库而遗漏文件，也不要把备份放入 Git checkout。

备份后记录文件数、总字节数、生成时间、源路径和 `SHA256SUMS.txt`。备份本身包含财务底稿，应放在只有运行账号和备份账号可读的受控磁盘中。

## 何时停止并恢复

出现以下情况时先暂停上传和 worker，保存日志与当前清单：

- 数据库记录和 storage 中的文件或 manifest 哈希不一致；
- 上传任务反复失败，或重算结果无法解释；
- 错误版本已经被提交、批准或冻结；
- 备份清单不完整，无法证明原件可恢复。

单个上传版本的问题优先在管理端保留原件并创建新修订，不删除旧版本。只有数据库整体损坏、版本关系整体不可信或数据目录不可读时，才进行整库恢复。

## 临时目录恢复演练

不要直接覆盖当前工作目录。先建立临时恢复目录并复制备份：

```sh
RESTORE_ROOT="$(mktemp -d /tmp/hotel-budget-restore.XXXXXX)"
cp "/path/to/backup/db.sqlite3" "$RESTORE_ROOT/db.sqlite3"
ditto "/path/to/backup/storage" "$RESTORE_ROOT/storage"
```

在当前项目目录执行只读检查时，把配置通过进程环境传给 `manage.py`。`manage.py` 本身不会自动读取 `.env`；如果使用配置文件，先显式导出：

```sh
set -a
. ./.env.production
set +a
DATABASE_PATH="$RESTORE_ROOT/db.sqlite3" \
BUDGET_STORAGE_ROOT="$RESTORE_ROOT/storage" \
.venv/bin/python manage.py check
```

然后核对：

1. 数据库可打开，项目、预算周期、上传版本和任务状态可读；
2. 每个上传版本的 `original_path`、`recalculated_path` 和 SHA-256 与 storage 文件一致；
3. 模板、manifest、规则版本和冻结快照的相互引用一致；
4. `find` 与备份清单的文件数量、大小和哈希一致。

需要做登录和页面读取验证时，在确认当前服务已停止后用临时端口启动恢复副本：

```sh
DATABASE_PATH="$RESTORE_ROOT/db.sqlite3" \
BUDGET_STORAGE_ROOT="$RESTORE_ROOT/storage" \
PORT=8770 .venv/bin/python scripts/local_server.py start
curl -fsS http://127.0.0.1:8770/healthz
DATABASE_PATH="$RESTORE_ROOT/db.sqlite3" \
BUDGET_STORAGE_ROOT="$RESTORE_ROOT/storage" \
PORT=8770 .venv/bin/python scripts/local_server.py stop
```

浏览器验证管理账号登录、项目隔离、预算报表、底稿下载和一个脱敏上传任务。不要在恢复演练中上传真实新数据。若第 1 至第 4 项或页面验证失败，保留临时目录并停止，不要提升为工作目录。

## 提升恢复版本

只有管理者确认临时恢复版本正确后，才执行提升：

1. 再次停止正式服务并保存当前工作目录清单；
2. 将当前 `db.sqlite3` 和 `storage/` 改名为带时间戳的保留目录；
3. 将已核对的恢复副本复制到正式 `DATABASE_PATH` 和 `BUDGET_STORAGE_ROOT`；
4. 以正式根 `.env.production` 导出环境变量，运行 `manage.py check`；
5. 用脱敏文件验证登录、项目隔离、底稿导出和 worker 终态；
6. 确认无误后再运行 `./一键启动.command`。

保留旧目录和所有恢复证据，审计记录中写明恢复依据、备份哈希、操作者和时间。不要用 `rm` 删除旧数据，也不要把本地复制流程当作生产灾备。

## 当前能力边界

MVP 不保证在线热备、跨机房容灾、自动 PITR、对象存储锁定、多人并发冲突解决或一键无损回滚。需要这些能力时，应先确定正式数据库、对象存储、备份频率和恢复目标，再单独设计迁移方案。
