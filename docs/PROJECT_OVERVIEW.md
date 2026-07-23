# 项目全景说明

## 1. 项目是什么

本项目是一套运行在 Debian VPS 上的 Binance 合约带单员跟单执行系统。它不内置行情策略，
而是把 Binance 公开带单员的最新操作当作外部交易信号，再由本系统完成归一化、独立记账、
资金分配、Testnet 下单、成交恢复、盈亏统计、Telegram 展示和故障处理。

仓库默认只能部署到 Binance USD-M Futures Testnet。正式盘虽有隔离好的代码和部署入口，
但必须使用独立数据库、独立密钥、干净账户和短时激活文件，不能通过替换 API 地址直接启用。

## 2. 当前产品能力

- 10 个带单员槽位：长线 1 个、短线 2 个、自定义 7 个。
- 长线每周日上海时间 00:00 选人，短线每天 00:00 选人。
- 锁定自动槽位后保留现任，只更新备选人；自定义槽位完全由 Telegram 授权用户配置。
- 每 10 秒增量读取已配置带单员的公开操作；首次见到历史只建立基线，不回放旧单。
- 每个带单员拥有独立虚拟仓位、订单、挂单、倍数和盈亏账本。
- 开仓/加仓使用带单员价格的 GTC 保护限价；盘口更优时可立即成交，否则等待到来源退出。
- 减仓/平仓按该带单员在本系统账本中的数量执行，避免影响其他带单员归属。
- 交易所最大允许杠杆、单笔最多 5 U 保证金、共享可配置开仓额度和 30 U 固定保留线。
- Telegram 提供带单员、仓位、待入场、盈亏、资金和系统状态操作，并对写操作鉴权。
- PostgreSQL 事件账本、幂等 client order ID、事务 Outbox、重启恢复和验证备份。
- 30 分钟确定性巡检、每小时 Codex 审查、故障即时报告及受限自动修复。

## 3. 核心数据流

```text
Binance 公开带单操作
        │
        ▼
10 秒增量采集与历史连续性检查
        │
        ▼
规范化信号 ──► 带单员独立虚拟账本 ──► 资金/杠杆/幂等门禁
                                                │
                                                ▼
                                     Binance USD-M Testnet
                                                │
                                                ▼
成交/订单恢复 ──► PostgreSQL 事件与估值 ──► Outbox ──► Telegram
        ▲                       │
        └──── Watchdog / Codex 审查与修复 / 备份 / 事故补发 ────┘
```

同一币种、同一方向来自两个带单员时，Binance 侧仓位可能聚合，但系统仍按带单员分别记录归属。
一个带单员的减仓只扣减自己的虚拟数量，不会从另一个带单员账本挪用。

## 4. 资金与下单语义

默认逻辑资金包络为 150 U，其中共享开仓额度为 120 U、保留线为 30 U。共享额度可由
Telegram 授权用户调整，但每笔订单保证金最多 5 U。系统先以带单员成交名义价值乘以该
带单员专属倍数作为目标，再读取交易所允许的最大初始杠杆；目标超过单笔或共享容量时，
按安全上限缩量，不为追求名义价值突破保证金边界。

开仓限价是“可接受的最差价格”，不是要求必须按该价格成交。因此盘口已经更优时，交易所可以
立即以更优价撮合；盘口较差时订单保持 GTC，直到成交、来源平仓导致取消，或出现明确终态。
提交结果暂不明确时，系统使用原 client order ID 查询，不会盲目重复下单。

## 5. 主要运行组件

| 组件 | 职责 |
| --- | --- |
| `aiq-copy-infra.service` | 启动固定镜像的 PostgreSQL/TimescaleDB |
| `aiq-copy-migrations.service` | 在业务服务前升级到唯一 Alembic head |
| `aiq-copy-poller.service` | 10 秒采集、信号处理、Testnet 下单和估值 |
| `aiq-testnet-user-stream.service` | 保存 Testnet 用户数据流证据并自动续期 |
| `aiq-copy-telegram.service` | 授权操作面板、通知和 Outbox 投递 |
| `aiq-copy-watchdog.timer` | 每 30 分钟确定性健康检查 |
| `aiq-copy-codex-audit.timer` | 每小时脱敏审查，必要时进入修复闭环 |
| 两个 selector timer | 短线每日、长线每周自动选人与备选维护 |
| backup/replay timer | 每日验证备份及每 5 分钟事故通知补发 |

所有常驻服务和定时器均由 systemd 管理并配置开机自启。

## 6. Codex 在系统中的角色

Codex 负责结构化候选复核、小时审查和确认属于代码/配置缺陷后的修复，不直接持有下单权限。
systemd 将 Binance Key、Telegram Token 和生产密钥从 Codex 单元中隔离。固定模型与推理强度
定义在 `src/ai_quant/copy_trading/codex_model.py`，当前为 `gpt-5.6-sol` / `high`。

确定性程序仍是交易状态、幂等、资金边界和健康判断的权威。Codex 不能绕过门禁，也不能把
公开网页内容当作系统指令。

## 7. 仓库目录

| 路径 | 内容 |
| --- | --- |
| `src/ai_quant/copy_trading/` | 跟单领域模型、采集、执行、账本、选人和 Telegram 状态 |
| `src/ai_quant/services/` | systemd 调用的服务入口 |
| `migrations/business/` | PostgreSQL 业务库 Alembic 迁移 |
| `deploy/systemd/` | Testnet systemd 单元及隔离的 production 覆盖 |
| `deploy/*.compose.yaml` | 固定镜像和本地监听的数据库基础设施 |
| `config/`、`contracts/` | 示例策略、JSON Schema 和结构化输出契约 |
| `tests/` | 单元、契约、属性、集成、安全、重放和故障注入测试 |
| `scripts/`、`tools/` | 迁移测试、升级、验证、备份和部署辅助工具 |
| `docs/` | 架构、ADR、部署、故障矩阵和正式盘切换说明 |

## 8. 不在 Git 中的数据

仓库不会保存 Binance/Telegram/OpenAI 密钥、Codex 登录状态、PostgreSQL 数据卷、数据库备份、
浏览器配置、运行日志、证据文件或当前账户状态。这些数据属于每台 VPS 的独立运行环境。
新 VPS 应创建新密钥文件和新数据库，通过首次基线开始观察，不能复制旧 VPS 的运行状态来伪装部署成功。

## 9. 从哪里开始

- 人工从零部署：`docs/deployment/copy-trading-vps.md`
- 交给另一台 VPS 的 Codex：`docs/deployment/codex-vps-handoff.md`
- 精确业务规则：`docs/architecture/copy-trading-testnet.md`
- 故障与恢复预期：`docs/architecture/copy-trading-failure-matrix.md`
- 正式盘边界：`docs/deployment/copy-production-cutover.md`
