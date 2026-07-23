# Binance 合约带单员跟单系统

这是一套运行在 Debian VPS 上的、事件源驱动的 Binance USD-M Futures
跟单系统。系统每 10 秒读取公开带单员操作，为每个带单员维护独立虚拟仓位账本，
并在 Binance Testnet 上执行对应的开仓、加仓、减仓和平仓。

> 默认部署只允许 Testnet。正式盘入口有独立数据库和激活门禁，不能通过单纯替换 API URL
> 启用。这是高风险交易软件，使用者必须自行承担交易、杠杆、清算、API 变更和公开数据源不稳定风险。

## 主要功能

- 共 10 个槽位：1 个长线、2 个短线和 7 个手动自定义槽位；未配置槽位可保持为空。
- 长线每周、短线每日自动选人，支持锁定带单员和备选人。
- 自定义槽位只接受 Telegram 授权用户输入的公开带单员 ID 或详情页链接，不参与自动换人。
- 每个带单员独立仓位、订单、盈亏和跟单倍数，不因同币种同方向而混账。
- 开仓/加仓按带单员成交价提交持续有效的保护限价；当前盘口更优时由交易所立即按更优价格撮合，盘口更差时持续等待至带单员退出该仓位。减仓/平仓使用市价。
- 150 U 逻辑资金边界、30 U 实际可用余额保留线、所有带单员共享的可配置开仓额度（默认/最高 120 U）、单笔最多 5 U 保证金；盈亏不重复缩减共享额度，也不设置单币种累计上限。授权用户可在 Telegram 资金页用快捷值或自定义数值二次确认修改。
- 自动读取交易所允许的最大初始杠杆，不足时按剩余容量缩量。
- Telegram 面板、交易通知、带单员管理、指定平仓、盈亏和健康状态查询。
- PostgreSQL 事件源账本、幂等下单、事务 Outbox、订单状态恢复和每日验证备份。
- 30 分钟确定性巡检、每小时 Codex 审查、故障即时唤醒与自动修复闭环。

## 数据流

```text
Binance 公开带单数据
          ↓ 10 秒轮询
归一化信号 → 带单员虚拟账本 → 资金/杠杆/幂等门禁
          ↓
Binance USD-M Testnet → 成交与盈亏事件 → PostgreSQL Outbox → Telegram
          ↑
Watchdog / Codex 审查 / 故障报告 / 备份
```

更完整的业务规则见
[跟单子系统架构](docs/architecture/copy-trading-testnet.md)。

首次阅读建议先看 [项目全景说明](docs/PROJECT_OVERVIEW.md)，其中说明了组件边界、
数据流、资金语义、故障恢复方式和仓库目录。

## 部署

新 VPS 请按 [Debian 12 Testnet 完整部署手册](docs/deployment/copy-trading-vps.md)
从零部署。手册包含：

- Docker、Python 3.12/uv 和 Codex CLI 准备；
- Binance Testnet、Telegram 和 PostgreSQL 密钥文件；
- Alembic 迁移、systemd 开机自启与定时任务；
- 首次选人、Telegram 恢复新开仓和健康验证；
- 升级、备份、日志和常见故障排查。

如果由另一台 VPS 上的 Codex 执行部署，请同时把
[Codex VPS 部署交接清单](docs/deployment/codex-vps-handoff.md)交给它。仓库根目录
的 [AGENTS.md](AGENTS.md) 是 Codex 在本项目中必须遵守的长期安全和验证约束。

正式盘只能按
[Testnet → 正式盘切换手册](docs/deployment/copy-production-cutover.md)
执行，不得复用 Testnet 数据库、密钥或客户端订单 ID 命名空间。

## 验证

```bash
uv sync --frozen --all-groups
uv run ruff check src tests tools scripts migrations
uv run mypy src
uv run pytest -q
AIQ_BUSINESS_DATABASE_URL_FILE=/run/ai-quant-secrets/copy-business-database-url \
  uv run alembic -c migrations/business/alembic.ini current
```

实际 Binance 订单测试只能在专用 Testnet 账户上运行。单元测试通过不代表正式盘已获批。

## 密钥与运行数据

仓库不包含 API Key、Telegram Token、Codex 登录状态、数据库密码、备份、日志、
浏览器配置或 VPS 取证数据。密钥必须放在仓库外的 `/root/aiq-user-inputs`，
由 systemd 一次性服务物化到 `/run/ai-quant-secrets`。

如果任何密钥曾经进入 Git，仅删除文件不够：应当立即在 Binance/Telegram/OpenAI 侧撤销并轮换。

## 许可说明

`pyproject.toml` 当前将项目标记为 Proprietary。未附带开源许可的情况下，公开可见不等于
授予复制、修改或商业使用权。如需开源，仓库所有者应当另行选择并添加 LICENSE。
