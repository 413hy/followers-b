# 跟单系统 Testnet → 正式盘切换手册

本手册是未来切换路径，不代表当前已经启用正式盘。没有专用正式盘 API Key、独立账户和有效
激活文件时，不执行下列切换。正式盘数据库不得从 Testnet 备份恢复。

## 切换前门禁

- 完整测试、Ruff、Mypy、Alembic 单一 head 和 Testnet 故障演练全部通过。
- Testnet 没有 `SUBMITTING/ACKNOWLEDGED/PARTIALLY_FILLED/UNKNOWN` 订单；停止服务后保存验证备份。
- 正式盘使用专用 USD-M Futures 账户，API Key 只授予需要的合约交易权限，不启用提现权限。
- 正式盘账户为 Hedge Mode，切换前零持仓、零普通挂单、零算法挂单且 `canTrade=true`。
- `/root/aiq-user-inputs/copy-trading/production/` 中四个文件均为 root-only：
  `binance_api_key`、`binance_api_secret`、`business_db_password`、`activation.json`。
- `activation.json` 由示例生成；`api_key_sha256` 是当前 API Key 的 SHA-256，授权窗口不超过
  31 天，`expires_at` 必须晚于当前 UTC 时间。它不是永久开关，换 Key 必须重新授权。

## 原子切换顺序

1. 停止 poller、Telegram、选人、巡检、审计和相关 timer。等待正在运行的 oneshot 退出；不在
   Codex 修复或数据库备份中途切换。
2. 停止 `aiq-copy-migrations.service`、`aiq-copy-infra.service`、`aiq-copy-secrets.service`，停止并
   禁用 `aiq-testnet-user-stream.service` 和 `aiq-testnet-secrets.service`。Testnet 容器和数据卷
   保留作只读归档，不删除。
3. 将 `deploy/systemd/production/` 中六个同名完整单元安装到 `/etc/systemd/system/`，安装
   `aiq-production-secrets.service`，并将三个 `.service.d/production.conf` 目录安装到对应
   systemd drop-in 目录。执行 `systemctl daemon-reload` 后先用 `systemd-analyze verify` 检查。
4. 依次启动 `aiq-production-secrets.service`、生产版 `aiq-copy-secrets.service`、生产版
   `aiq-copy-infra.service` 和通用 `aiq-copy-migrations.service`。生产 Compose 固定使用
   `127.0.0.1:55433`、数据库/用户 `aiq_copy_production` 和独立数据卷。
5. 启动生产版 `aiq-copy-poller.service`。它会再次验证激活文件、精确生产端点、账户交易状态、
   Hedge Mode、零持仓、零挂单、新数据库和永久环境绑定。任一条件不满足时启动失败并触发
   事故报告；不得通过删除检查或复用 Testnet 数据库解决。
6. Poller 首次只建立带单员历史基线。生产新库没有运行控制事件时默认
   `PAUSED_NEW_ENTRIES`，所以即使服务在线也不会直接开仓。随后启动 Telegram、选人任务、
   备份 timer、30 分钟 Watchdog 和小时 Codex 审计。
7. 检查环境绑定为 `PRODUCTION`、备份恢复演练成功、Watchdog 无关键发现、Telegram 展示的
   线路/仓位/待入场均为空。最后由授权用户显式恢复新开仓；首笔正式订单另行使用可承受的小额
   人工观察，不扩大 150U 系统边界。

## 回退边界

正式盘一旦出现订单或仓位，不能直接重新安装 Testnet 单元。必须先让正式盘未决订单终态化，
按带单员操作或授权清仓完成，确认交易所与虚拟账本均为零，再停止生产服务。回退时重新安装
`deploy/systemd/` 的 Testnet 单元并恢复 Testnet 密钥物化；生产数据库继续保留，绝不把它绑定
为 Testnet。客户端订单 ID 的 `aqc-p`/`aqc-t` 命名空间和数据库永久绑定可防止两侧互相认领。
