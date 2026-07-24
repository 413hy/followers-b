# Debian 12 VPS 上部署 Binance Testnet 跟单系统

本手册面向一台全新 VPS，目标是部署仓库中已实现的 Binance USD-M Futures
Testnet 跟单系统。所有命令默认以 `root` 执行。

如果部署由另一台 VPS 上的 Codex 完成，请先阅读
[Codex VPS 部署交接清单](codex-vps-handoff.md)和仓库根目录的 `AGENTS.md`。

## 1. 重要边界

- 已验证宿主机为 Debian 12 Bookworm/aarch64；其他发行版和架构需自行重新验证。
- systemd 单元固定使用 `/root/quantify/ai-quant-system`，请克隆到该路径。
- 默认仅启用 Binance USD-M Futures Testnet。不要把正式盘 Key 写入 Testnet 文件。
- 全新数据库默认不允许新开仓；首次基线、选人和巡检通过后，由 Telegram 授权用户显式恢复。
- Binance 公开带单页接口不是稳定的官方交易 API，页面升级、限流或地区限制可能导致采集失败。

## 2. 准备账户和软件

需要：

1. Binance Futures Testnet API Key/Secret，账户可交易且已开启 Hedge Mode。
2. Telegram Bot Token、接收通知的 Chat ID 和可操作机器人的 User ID。
3. Docker Engine 和 `docker compose` 插件。
4. Git、curl、OpenSSL、`flock` (`util-linux`) 和 systemd。
5. `uv`，用于按 `uv.lock` 安装 Python 3.12 和依赖。
6. 当前 Node.js/npm、Codex CLI 及可用的 Codex 账户，用于自动选人、小时审查和修复。

建议先安装基础包：

```bash
apt-get update
apt-get install -y ca-certificates curl git openssl util-linux
```

Docker 和 uv 请使用各自官方安装方式。安装后必须满足：

```bash
docker --version
docker compose version
uv --version
systemctl enable --now docker.service
```

## 3. 克隆并安装 Python 环境

```bash
install -d -m 0755 /root/quantify
git clone https://github.com/413hy/followers-b.git /root/quantify/ai-quant-system
cd /root/quantify/ai-quant-system
git rev-parse HEAD
sed -n '1,240p' AGENTS.md
uv sync --frozen --all-groups
.venv/bin/python --version
```

Python 版本必须在 `3.12.x`，`uv sync --frozen` 不应改写 `uv.lock`。

## 4. 安装并登录 Codex CLI

按 [OpenAI Codex CLI 官方文档](https://developers.openai.com/codex/cli/)安装当前支持的 CLI。
Codex 支持使用 ChatGPT OAuth、设备登录或 API Key 等方式认证；本项目不读取仓库内的
OpenAI Key，systemd 使用 root 的 `/root/.codex` 登录状态。

如果尚未安装，可在已准备好当前 Node.js/npm 后安装官方包。不要把 CLI 版本写死为本文
发布时的版本；部署当天先核对官方文档：

```bash
npm install -g @openai/codex
codex --version
codex login
```

项目对 systemd 使用的可执行文件固定路径是 `/root/.local/bin/codex`：

```bash
install -d -m 0755 /root/.local/bin
CODEX_BIN="$(command -v codex)"
if [ "$CODEX_BIN" != /root/.local/bin/codex ]; then
  ln -sfn "$CODEX_BIN" /root/.local/bin/codex
fi
/root/.local/bin/codex --version
```

请在 root 会话中完成登录；如果前面不是以 root 登录，请再次运行
`/root/.local/bin/codex login`。不要把 `/root/.codex`、`auth.json` 或 API Key 复制到项目目录。
代码当前显式固定 `src/ai_quant/copy_trading/codex_model.py` 中的模型和 `high` 推理强度；
部署账户如果无权使用该模型，选人/审查任务会安全失败，不应通过删除结构化输出或安全门禁绕过。

## 5. 创建仓库外密钥

先创建 root-only 目录：

```bash
install -d -m 0700 \
  /root/aiq-user-inputs/testnet/secrets \
  /root/aiq-user-inputs/copy-trading/secrets \
  /root/aiq-user-inputs/notifications/secrets
install -d -m 0700 /root/aiq-user-inputs/notifications
```

生成数据库密码和 Testnet 运行授权文件：

```bash
umask 077
openssl rand -base64 36 | tr -d '\n' \
  > /root/aiq-user-inputs/copy-trading/secrets/business_db_password
printf '\n' >> /root/aiq-user-inputs/copy-trading/secrets/business_db_password
printf '%s\n' 'TESTNET_COPY_TRADING_ARMED' \
  > /root/aiq-user-inputs/copy-trading/secrets/testnet_copy_trading_arm
```

通过隐藏输入写入 Binance 和 Telegram 密钥，避免它们进入 shell history：

```bash
read -rsp 'Binance Testnet API Key: ' VALUE; printf '\n'; \
  printf '%s\n' "$VALUE" > /root/aiq-user-inputs/testnet/secrets/binance_testnet_api_key; \
  unset VALUE
read -rsp 'Binance Testnet API Secret: ' VALUE; printf '\n'; \
  printf '%s\n' "$VALUE" > /root/aiq-user-inputs/testnet/secrets/binance_testnet_api_secret; \
  unset VALUE
read -rsp 'Telegram Bot Token: ' VALUE; printf '\n'; \
  printf '%s\n' "$VALUE" > /root/aiq-user-inputs/notifications/secrets/telegram_bot_token; \
  unset VALUE
read -rp 'Telegram Chat ID: ' VALUE; \
  printf '%s\n' "$VALUE" > /root/aiq-user-inputs/notifications/telegram_chat_ids; \
  unset VALUE
read -rp 'Telegram Authorized User ID: ' VALUE; \
  printf '%s\n' "$VALUE" > /root/aiq-user-inputs/notifications/telegram_authorized_user_ids; \
  unset VALUE
chmod 0400 \
  /root/aiq-user-inputs/testnet/secrets/binance_testnet_api_key \
  /root/aiq-user-inputs/testnet/secrets/binance_testnet_api_secret \
  /root/aiq-user-inputs/copy-trading/secrets/business_db_password \
  /root/aiq-user-inputs/copy-trading/secrets/testnet_copy_trading_arm \
  /root/aiq-user-inputs/notifications/secrets/telegram_bot_token \
  /root/aiq-user-inputs/notifications/telegram_chat_ids \
  /root/aiq-user-inputs/notifications/telegram_authorized_user_ids
```

多个 Chat ID/User ID 时每行写一个纯数字 ID。不要使用用户名或 `@name`。

## 6. 本地验证

在安装 systemd 单元前先确认代码完整：

```bash
cd /root/quantify/ai-quant-system
uv run ruff check src tests tools scripts migrations
uv run mypy src
uv run pytest -q
COPY_BUSINESS_DB_PASSWORD_FILE=/root/aiq-user-inputs/copy-trading/secrets/business_db_password \
  docker compose -f deploy/copy-trading-infra.compose.yaml config --quiet
```

## 7. 安装 systemd 单元

```bash
cd /root/quantify/ai-quant-system
install -m 0644 deploy/systemd/aiq-testnet-secrets.service /etc/systemd/system/
install -m 0644 deploy/systemd/aiq-testnet-user-stream.service /etc/systemd/system/
install -m 0644 deploy/systemd/aiq-copy-*.service /etc/systemd/system/
install -m 0644 deploy/systemd/aiq-copy-*.timer /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/aiq-testnet-secrets.service \
  /etc/systemd/system/aiq-testnet-user-stream.service \
  /etc/systemd/system/aiq-copy-*.service \
  /etc/systemd/system/aiq-copy-*.timer
```

安装命令不会启动交易。先启用开机自启：

```bash
systemctl enable \
  aiq-testnet-secrets.service \
  aiq-copy-secrets.service \
  aiq-copy-infra.service \
  aiq-copy-migrations.service \
  aiq-copy-poller.service \
  aiq-copy-telegram.service \
  aiq-testnet-user-stream.service
```

按依赖顺序首次启动：

```bash
systemctl start aiq-testnet-secrets.service
systemctl start aiq-copy-secrets.service
systemctl start aiq-copy-infra.service
systemctl start aiq-copy-migrations.service
systemctl start aiq-copy-poller.service
systemctl start aiq-copy-telegram.service
systemctl start aiq-testnet-user-stream.service
```

再启用定时巡检、选人、审查、备份和事故补发：

```bash
systemctl enable --now \
  aiq-copy-watchdog.timer \
  aiq-copy-leader-selector.timer \
  aiq-copy-long-leader-selector.timer \
  aiq-copy-leader-status-check.timer \
  aiq-copy-codex-audit.timer \
  aiq-copy-database-backup.timer \
  aiq-copy-incident-replay.timer
```

## 8. 首次基线和选人

Poller 第一次看到带单员时只建立历史基线，不会回放旧操作。可以立即执行一次长线和短线选人：

```bash
systemctl start aiq-copy-long-leader-selector.service
systemctl start aiq-copy-leader-selector.service
systemctl start aiq-copy-leader-status-check.service
```

这两个任务会读取公开候选数据并调用 Codex，可能持续数分钟。自动选人只管理 1 个长线和
2 个短线槽位；自定义 1–7 只能由 Telegram 授权用户输入公开带单员 ID 或详情页链接配置，
也可以保持为空。完成后等待至少一个 10 秒轮询周期，在 Telegram 中检查带单员、仓位、
待入场和系统状态。最后使用授权用户的“恢复新开仓”二次确认操作，不要通过直改数据库跳过该门禁。

## 9. 上线验收

```bash
systemctl is-active \
  aiq-copy-infra.service \
  aiq-copy-migrations.service \
  aiq-copy-poller.service \
  aiq-copy-telegram.service \
  aiq-testnet-user-stream.service \
  aiq-copy-watchdog.timer
systemctl is-enabled \
  aiq-copy-migrations.service \
  aiq-copy-poller.service \
  aiq-copy-telegram.service \
  aiq-testnet-user-stream.service \
  aiq-copy-watchdog.timer \
  aiq-copy-leader-selector.timer \
  aiq-copy-long-leader-selector.timer \
  aiq-copy-leader-status-check.timer \
  aiq-copy-codex-audit.timer \
  aiq-copy-database-backup.timer \
  aiq-copy-incident-replay.timer
docker compose -f deploy/copy-trading-infra.compose.yaml ps
AIQ_BUSINESS_DATABASE_URL_FILE=/run/ai-quant-secrets/copy-business-database-url \
  .venv/bin/alembic -c migrations/business/alembic.ini current
journalctl -u aiq-copy-poller.service -n 50 --no-pager
journalctl -u aiq-copy-telegram.service -n 50 --no-pager
journalctl -u aiq-testnet-user-stream.service -n 50 --no-pager
systemctl start aiq-copy-watchdog.service
journalctl -u aiq-copy-watchdog.service -n 30 --no-pager
systemctl status aiq-copy-leader-status-check.timer --no-pager
systemctl list-timers aiq-copy-leader-status-check.timer --all
```

合格状态应当包括：

- Alembic 只有一个 head，且数据库已在该 head。
- 每个 10 秒周期的带单员轮询成功，没有持续失败。
- Testnet user stream 保持连接或能够明确重连，没有持续认证失败。
- Telegram 能显示 Testnet 环境，并且只接受授权 User ID 的写操作。
- Watchdog 为 `HEALTHY` 且无关键 finding。
- `systemctl --failed` 无与 `aiq-copy-*` 相关的失败单元。

## 10. 日常运维

查看日志：

```bash
journalctl -u aiq-copy-poller.service -f
journalctl -u aiq-copy-telegram.service -f
journalctl -u aiq-copy-watchdog.service -n 100 --no-pager
journalctl -u aiq-copy-codex-audit.service -n 100 --no-pager
```

手动备份：

```bash
systemctl start aiq-copy-database-backup.service
journalctl -u aiq-copy-database-backup.service -n 20 --no-pager
```

备份保存在 `/var/lib/ai-quant/backups/copy-trading`，默认保留 14 天。备份、数据库卷和
`/var/lib/ai-quant/evidence` 都不应提交到 Git。

## 11. 升级

升级前先备份，不在正在执行选人或 Codex 修复时切换：

```bash
cd /root/quantify/ai-quant-system
systemctl start aiq-copy-database-backup.service
git fetch origin
git pull --ff-only origin main
uv sync --frozen --all-groups
uv run pytest -q
systemctl stop aiq-copy-poller.service
systemctl restart aiq-copy-migrations.service
uv run python scripts/upgrade-pending-protected-orders.py \
  --database-url-file /run/ai-quant-secrets/copy-business-database-url \
  --api-key-file /run/ai-quant-secrets/binance-testnet-api-key \
  --api-secret-file /run/ai-quant-secrets/binance-testnet-api-secret \
  --repository-root /root/quantify/ai-quant-system
install -m 0644 deploy/systemd/aiq-testnet-user-stream.service \
  /etc/systemd/system/aiq-testnet-user-stream.service
install -m 0644 deploy/systemd/aiq-copy-*.service /etc/systemd/system/
install -m 0644 deploy/systemd/aiq-copy-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start aiq-copy-poller.service
systemctl restart aiq-copy-telegram.service
systemctl restart aiq-testnet-user-stream.service
systemctl start aiq-copy-watchdog.service
```

`aiq-copy-migrations.service` 在重启时会暂停其依赖服务，这是 systemd 依赖关系的正常现象。升级后必须再次
检查 poller、Telegram 和 Watchdog。`upgrade-pending-protected-orders.py` 只处理升级前仍处于
`SUBMITTED/UNCERTAIN` 且尚未成交的旧 GTD 入场单：它在 poller 停止期间等量换成 GTC，并追加不可修改
的升级证据；全新安装或没有旧挂单时会安全报告 `replaced: 0`。

## 12. 常见故障

### 密钥服务失败

```bash
systemctl status aiq-testnet-secrets.service aiq-copy-secrets.service --no-pager -l
journalctl -u aiq-copy-secrets.service -n 50 --no-pager
find /root/aiq-user-inputs -maxdepth 4 -type f -printf '%m %u:%g %p\n'
```

检查文件是否存在、是否为 root 所有且权限是 `0400`。不要把密钥内容打印到日志。

### PostgreSQL 未启动

```bash
systemctl status aiq-copy-infra.service --no-pager -l
docker compose -f deploy/copy-trading-infra.compose.yaml ps
docker logs --tail 100 aiq-copy-trading-postgres-1
```

### Codex 选人/审查失败

```bash
test -x /root/.local/bin/codex
/root/.local/bin/codex --version
journalctl -u aiq-copy-codex-audit.service -n 100 --no-pager
journalctl -u aiq-copy-leader-selector.service -n 100 --no-pager
```

常见原因是 root 未登录、账户没有指定模型权限、CLI 不在固定路径或网络无法访问 OpenAI。

### 轮询失败或公开带单数据不可见

不要伪造浏览器指纹、绕过验证码或 WAF。先查看精确 reason code、网络、区域限制和 Binance 页面是否变更；
系统在无法确定历史连续性时应当失败关闭，而不是猜测缺失订单。

## 13. 正式盘

不得把 Testnet systemd 单元中的 `--mode testnet` 直接替换为 production。正式盘必须使用：

- `deploy/copy-trading-production-infra.compose.yaml` 中的独立数据库；
- `deploy/systemd/production/` 中的独立单元；
- 专用正式盘 API Key，且不授予提现权限；
- 短时效、绑定 Key 指纹和端点的激活文件；
- 全新、零持仓、零挂单的正式盘数据库和账户。

完整流程见 [Testnet → 正式盘切换手册](copy-production-cutover.md)。
