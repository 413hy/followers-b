# 交给另一台 VPS Codex 的部署清单

这份清单用于让一台全新 VPS 上的 Codex 部署本仓库。它是执行顺序和验收约束，
具体命令以 [完整 VPS 部署手册](copy-trading-vps.md)为准。

## 1. 直接交给 Codex 的任务说明

可以把下面这段原样交给新 VPS 上的 Codex：

```text
请把 https://github.com/413hy/followers-b 部署为 Binance USD-M Futures Testnet
跟单系统。仓库当前 main 是代码唯一基准。

先完整阅读仓库根目录 AGENTS.md、README.md、docs/PROJECT_OVERVIEW.md、
docs/deployment/codex-vps-handoff.md 和 docs/deployment/copy-trading-vps.md。
严格按文档部署到 /root/quantify/ai-quant-system。

只部署 Testnet，不启用正式盘，不复制其他 VPS 的数据库或运行状态，不把任何密钥写进
仓库、聊天输出或日志。先安装锁定依赖并跑完整测试，再创建仓库外密钥、安装 systemd、
迁移数据库、建立首次历史基线。所有服务和定时器验收正常后，等待我在 Telegram 中
显式恢复新开仓。最后给我提交版本、Alembic head、服务 active/enabled、定时器、
Watchdog、Telegram 和 Testnet 连接的验收报告；报告不得包含密钥。
```

## 2. 需要用户在 VPS 上准备的内容

Codex 可以安装软件和配置服务，但以下内容必须由账户所有者提供：

- Binance Futures Testnet API Key 和 Secret，且账户已开启 Hedge Mode。
- Telegram Bot Token、通知 Chat ID、允许操作机器人的数字 User ID。
- 可使用项目固定模型的 Codex 账户登录。
- 对该 VPS 的 root 权限和可访问 GitHub、Binance Testnet、Telegram、OpenAI 的网络。

不要在对话里直接发送密钥。由用户在 VPS 的隐藏输入提示中写入
`/root/aiq-user-inputs`，具体文件名和权限见完整部署手册。

## 3. Codex 必须分阶段执行

### 阶段 A：宿主机与源码

1. 核对 Debian 12、架构、磁盘、时间同步、systemd 和 Docker 状态。
2. 安装 Git、Docker Compose、uv、Python 3.12 环境和当前 Codex CLI。
3. 克隆 `main` 到固定路径 `/root/quantify/ai-quant-system`。
4. 记录 `git rev-parse HEAD`，阅读 `AGENTS.md`，确认工作树干净。
5. 运行 `uv sync --frozen --all-groups`，不得改写 `uv.lock`。

### 阶段 B：离线验证

至少运行：

```bash
uv run ruff check src tests tools scripts migrations
uv run mypy src
uv run pytest -q
uv run bandit -q -r src
uv run python scripts/validate/secret_scan.py
make test-migrations
```

任一必需检查失败都应先查明并修复根因，不得为了“部署成功”删除测试、安全门禁或迁移。

### 阶段 C：密钥和基础设施

1. 让用户通过隐藏输入创建仓库外 root-only 密钥。
2. 先用 `docker compose ... config --quiet` 检查 Compose。
3. 安装仓库内 systemd 单元并运行 `systemd-analyze verify`。
4. 按 secrets → infra → migrations → poller/user-stream/Telegram 的顺序启动。
5. 启用 Watchdog、Codex 审查、长短线选人、备份和事故补发 timer。

### 阶段 D：首次基线与验收

1. 确认数据库只有一个 Alembic head，迁移已到 head。
2. 确认 poller、user stream、Telegram、数据库和全部 timer 均 active/enabled。
3. 首次公开历史只建立基线，不回放旧订单。
4. 手动运行 Watchdog，必须得到新的健康结果。
5. 验证 Telegram 只允许配置的 User ID 执行写操作。
6. 检查 10 个槽位页面；长线、短线可自动选择，自定义 1–7 可以留空或由用户配置。
7. 在用户明确确认前保持“暂停新开仓”，不要直接修改数据库解除门禁。

## 4. 最终验收报告模板

Codex 完成后应给用户一份不含密钥的简短报告：

```text
代码: <commit SHA>，工作树 clean
环境: Debian/架构/Python/uv/Codex 版本
测试: ruff、mypy、pytest、security scan、migration test
数据库: 当前 Alembic head，PostgreSQL healthy
服务: poller、user-stream、Telegram active/enabled
定时器: watchdog、Codex audit、长短线选人、备份、事故补发 active/enabled
运行验收: 最近轮询成功、Watchdog 状态、Telegram 授权检查
交易门禁: TESTNET；新开仓保持暂停/已由授权用户恢复
未完成项: <没有则写“无”>
```

## 5. 禁止做的事

- 不得把生产 Key 写入 Testnet 文件，或通过改 URL 启用正式盘。
- 不得提交 `/root/.codex`、`/root/aiq-user-inputs`、`/run/ai-quant-secrets`、
  `/var/lib/ai-quant`、Docker volume、日志或数据库转储。
- 不得伪造浏览器指纹、绕过验证码/WAF，或在数据缺口时猜测带单员操作。
- 不得因订单暂不明确而生成第二个 client order ID 重复下单。
- 不得清空事件历史来“初始化”盈亏；初始化应追加新的展示/资金基线。
- 不得把测试通过描述为正式盘安全或获批。

## 6. 后续升级

升级前创建并验证数据库备份，然后只接受 `main` 的快进更新。重新同步锁定依赖、跑完整测试、
执行迁移并刷新 systemd 单元，最后重复上线验收。若远端历史被仓库所有者有意重写，
停止自动升级并让用户确认新基准；不要在有运行数据的 VPS 上擅自 hard reset。

Codex CLI 的安装和认证方式可能随版本更新，部署时应核对
[OpenAI Codex CLI 官方文档](https://developers.openai.com/codex/cli/)；仓库级长期指导使用
`AGENTS.md`，Codex 登录状态保留在 root 的 Codex 主目录而不是项目目录。
