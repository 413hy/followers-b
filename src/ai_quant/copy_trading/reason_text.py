"""Chinese user-facing descriptions for internal copy-trading reason codes."""

from __future__ import annotations

import re

_REASON_CODE = re.compile(r"(?<![A-Z0-9_])(?:COPY|TELEGRAM)_[A-Z0-9_]+")

_LABELS = {
    "COPY_PNL_PRESENTATION_RESET": (
        "盈亏统计已从本次操作时间重新计为零, 仓位、订单和历史审计记录未修改"
    ),
    "COPY_ACCOUNT_ENVELOPE_RESET": (
        "交易资金净值已恢复到初始操作额度, 此后的盈亏和开仓容量从新基线计算"
    ),
    "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION": (
        "带单员在本系统入场前已减仓或平仓, 未成交的入场限价单已安全取消, 本次不会建立仓位"
    ),
    "COPY_PROTECTED_LIMIT_CANCELLED_BY_CONTROL": (
        "系统已暂停新开仓或正在清仓, 未成交的入场限价单已安全取消"
    ),
    "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_DRAINING": (
        "该带单员正在退出跟单, 未成交的入场限价单已安全取消"
    ),
    "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_SYMBOL_STOP": (
        "该带单员在这个币种已触发10 U净亏损止损, "
        "未成交的入场限价单已撤销, 不会在48小时冷却期内形成新仓位"
    ),
    "COPY_PROTECTED_LIMIT_CANCELLED_EXTERNALLY": ("交易所侧已撤销这笔入场限价单, 系统不会重复下单"),
    "COPY_PROTECTED_LIMIT_EXPIRED": "入场限价单在有效期内未成交, 系统已自动撤销",
    "COPY_PROTECTED_LIMIT_PENDING": "入场限价单已提交, 正在等待成交",
    "COPY_PROTECTED_LIMIT_CANCEL_STATUS_UNKNOWN": "撤单请求已发出, 但交易所状态暂未确认",
    "COPY_ORDER_CANCELED": "交易所确认订单已撤销",
    "COPY_ORDER_EXPIRED": "交易所确认订单已过期",
    "COPY_ORDER_REJECTED": "交易所拒绝了订单",
    "COPY_ORDER_REJECTED_BY_EXCHANGE": "交易所拒绝了本次下单",
    "COPY_TRADIFI_AGREEMENT_REQUIRED": (
        "Binance 账户尚未签署 TradFi 永续合约协议; 交易所在订单进入撮合前已拒绝请求, "
        "因此没有生成订单、没有成交、也没有仓位残留; 登录对应的 Binance 测试盘或正式盘"
        "签署协议后, 才能重新提交这笔跟单"
    ),
    "COPY_EXCHANGE_CODE_4411": "Binance 原始错误码 -4411 (尚未签署 TradFi 永续合约协议)",
    "COPY_TRADIFI_AGREEMENT_RETRY_REQUESTED": (
        "已确认协议签署完成, 正在按原信号、原数量和原限价受控重新提交; "
        "原委托不存在, 本次不会造成重复开仓"
    ),
    "COPY_EXCHANGE_CODE_4016": (
        "提交限价高于 Binance 当时允许的动态价格上限; 订单在进入撮合前已被拒绝, 没有成交或仓位残留"
    ),
    "COPY_ORDER_NOTIONAL_BELOW_EXCHANGE_MINIMUM": "订单金额低于交易所最小下单要求",
    "COPY_ORDER_PARTIAL_PENDING": "订单已部分成交, 剩余数量仍在等待成交",
    "COPY_ORDER_PARTIAL_TERMINAL": "订单部分成交后已经终止, 系统只按实际成交量记账",
    "COPY_ORDER_RECONCILED": "系统已通过交易所查询确认订单成交",
    "COPY_ORDER_STATUS_UNKNOWN": "暂时无法确认交易所订单状态, 系统不会重复下单",
    "COPY_ORDER_STATUS_UNKNOWN_WITH_FILL": "订单已有成交, 但最终状态暂未确认",
    "COPY_ORDER_RESPONSE_INVALID": "交易所返回的订单数据不完整或格式异常",
    "COPY_ORDER_TERMINAL_WITHOUT_FILL": "订单已经终止且没有成交",
    "COPY_ORDER_TERMINAL_WITHOUT_FILL_PRICE": "订单已经成交, 但成交均价暂未同步完成",
    "COPY_FILL_PRICE_PENDING": "订单已成交, 正在补充查询成交均价",
    "COPY_SUBMISSION_STATUS_UNKNOWN": (
        "Binance 测试盘没有在本次下单请求中返回明确结果; "
        "系统正在核对原订单是否实际存在, 这不是带单员信号缺失"
    ),
    "COPY_SUBMISSION_ALREADY_CLAIMED_UNRESOLVED": "订单已提交过, 当前正在核对交易所状态",
    "COPY_SUBMISSION_CLAIM_RACE_UNRESOLVED": "订单提交记录发生并发冲突, 系统已停止重复下单",
    "COPY_SUBMISSION_CLAIM_PARAMETERS_INVALID": "持久化订单参数不完整, 系统已停止继续执行",
    "COPY_SUBMISSION_NOT_FOUND_AFTER_GRACE": "宽限期后仍未在交易所找到这笔订单",
    "PLACE_ORDER_TRANSPORT_FAILED": "向 Binance 提交订单时网络连接失败, 订单结果需要核对",
    "PLACE_ORDER_HTTP_408_CODE_-1007": (
        "Binance 测试盘下单后端等待超时, 本次请求可能成功也可能未生成订单; "
        "系统会先按原订单号核对, 确认不存在后继续跟单"
    ),
    "PLACE_ORDER_INVALID_JSON": (
        "Binance 测试盘下单网关返回了非标准响应, 暂时无法直接判断结果; "
        "系统会先按原订单号核对, 确认不存在后继续跟单"
    ),
    "COPY_PENDING_ENTRY_CLAIM_MISSING": "待入场订单缺少持久化提交记录",
    "COPY_PENDING_ENTRY_CANCEL_UNRESOLVED": "待入场订单的撤销状态暂未确认",
    "COPY_ENTRY_FILL_ATTRIBUTION_FAILED": "成交已发生, 但带单员归属记账没有完成",
    "COPY_ATTRIBUTED_FILL_RECOVERED": "系统恢复后已补齐此前成交的归属记录",
    "COPY_REDUCTION_EXCHANGE_POSITION_INSUFFICIENT": "交易所实际仓位不足以执行本次减仓",
    "COPY_REDUCTION_ORPHAN": (
        "系统虚拟账本中没有该带单员在这个币种、这个方向的已成交仓位, "
        "说明本系统此前没有成功跟入这笔仓位, 因此当前无仓可减; "
        "为避免把平仓误执行成反向开仓, 本次只记录带单员信号, 没有提交订单"
    ),
    "COPY_REDUCTION_BELOW_EXCHANGE_STEP": "可减数量低于交易所最小数量步长",
    "COPY_REDUCTION_PLAN_MISSING": "减仓比例计算记录缺失, 系统已停止执行",
    "COPY_RECOVERED_REDUCTION_PLAN_CHANGED": "恢复时发现减仓目标已变化, 系统正在重新核对",
    "COPY_ONE_WAY_CLOSED_VOLUME_RECLASSIFICATION": (
        "已依据 Binance 平仓数量证据重建该带单员的交易方向"
    ),
    "COPY_FALSE_CROSS_ZERO_ENTRY_NEVER_FILLED": (
        "此前误判的反向开仓从未成交, 已从当前解析纪元隔离"
    ),
    "COPY_NEW_ENTRIES_PAUSED_NEW_ENTRIES": (
        "系统处于暂停新开仓状态, 本次新信号已跳过; 减仓和平仓仍会继续"
    ),
    "COPY_NEW_ENTRIES_REDUCE_ALL": "系统正在执行全部减仓, 本次新开仓信号已跳过",
    "COPY_ENTRY_SKIPPED_DURING_OPERATOR_FLATTEN": (
        "人工清仓期间出现的新开仓已跳过, 清仓完成后只跟随后续新信号"
    ),
    "COPY_LEADER_DRAINING_NO_NEW_ENTRY": "该带单员正在等待退出, 本次不再建立新仓位",
    "COPY_LEADER_SYMBOL_NET_LOSS_LIMIT_REACHED": (
        "该带单员在这个币种当前仍持有的多仓与空仓累计盈亏合计已达到 -10 U止损线"
    ),
    "COPY_LEADER_SYMBOL_ENTRY_COOLDOWN_48H": (
        "仅该带单员的这个币种进入48小时新开仓冷却, 其他带单员和币种不受影响"
    ),
    "COPY_LEADER_SYMBOL_ENTRY_COOLDOWN_ACTIVE": (
        "该带单员的这个币种仍在止损后的48小时冷却期内, "
        "本次新开仓或加仓已跳过; 减仓和平仓仍会执行"
    ),
    "COPY_RECOVERED_LEADER_NO_LONGER_ASSIGNED": "恢复时该带单员已不在槽位中, 本次不再开仓",
    "COPY_EXISTING_POSITION_HISTORY_RECOVERED": (
        "系统重启后检测到该带单员仍有本系统持仓, 已补读停机期间的操作记录并继续同步"
    ),
    "COPY_SIZE_AVAILABLE_BALANCE_RESERVE_REACHED": "账户需保留安全资金, 本次可用余额不足",
    "COPY_SIZE_MARGIN_CAP_REACHED": (
        "当前可分配保证金额度已经用完, 即使使用该币种最高杠杆也无法在额度内完成本次下单"
    ),
    "COPY_SIZE_TOTAL_MARGIN_CAP_REACHED": "所有带单员共享的开仓保证金额度已满",
    "COPY_SIZE_ORDER_MARGIN_CAP_REACHED": "单笔保证金上限不足以满足最小下单量",
    "COPY_SIZE_SYMBOL_MARGIN_CAP_REACHED": "该交易对保证金额度不足以满足最小下单量",
    "COPY_SIZE_BELOW_MINIMUM_NOTIONAL": "按当前额度计算出的订单金额低于交易所最低要求",
    "COPY_SIZE_BELOW_MINIMUM_QUANTITY": "按当前额度计算出的数量低于交易所最低要求",
    "COPY_SIZE_CURRENT_LEVERAGE_ABOVE_POLICY": "该交易对当前杠杆超过系统允许范围",
    "COPY_SIZE_MARKET_PRICE_INVALID": "用于计算下单量的市场价格无效",
    "COPY_SIZE_LEADER_MISMATCH": "信号与带单员资金配置不一致",
    "COPY_ACCOUNT_WARNING_RISK_LINE": (
        "账户低于风险预警参考线; 当前仅记录指标, 不会自动暂停新开仓"
    ),
    "COPY_ACCOUNT_EMERGENCY_RISK_LINE": ("账户低于紧急风险参考线; 当前仅记录指标, 不会自动清仓"),
    "COPY_ACCOUNT_RISK_AUTOMATION_REMOVED_AUTO_RESUME": (
        "已取消账户风险线的自动暂停功能, 系统恢复接收新开仓"
    ),
    "COPY_ACCOUNT_TRADING_DISABLED": "交易所账户当前禁止交易",
    "COPY_ACCOUNT_HEDGE_MODE_REQUIRED": "交易所账户没有启用双向持仓模式",
    "COPY_ACCOUNT_SNAPSHOT_STALE": "账户资金与仓位快照已过期",
    "COPY_ACCOUNT_RISK_DENIED": "账户风险检查未通过, 本次没有下单",
    "COPY_ACCOUNT_POSITIONS_INVALID": (
        "Binance 返回的持仓数据不完整或数值异常, 系统已停止本次交易以避免错单"
    ),
    "COPY_ACCOUNT_POSITION_MARK_INVALID": (
        "Binance 返回的仓位标记价格或名义金额异常, 本次盈亏与风险计算未采用该数据"
    ),
    "COPY_ACCOUNT_BOOLEAN_INVALID": (
        "Binance 返回的账户交易权限或持仓模式字段无效, 系统已停止本次交易"
    ),
    "COPY_ACCOUNT_DECIMAL_INVALID": ("Binance 返回的账户资金数值缺失或无效, 系统已停止本次交易"),
    "COPY_SYMBOL_NOT_AVAILABLE_ON_EXCHANGE": "当前执行环境不支持这个交易对",
    "COPY_SYMBOL_NOT_AVAILABLE_ON_TESTNET": (
        "Binance 测试盘不支持这个交易对, 因此只能记录带单员信号, 无法在测试盘下单; "
        "这不代表 Binance 正式盘也不支持"
    ),
    "COPY_SYMBOL_RULES_INVALID": "交易对的数量、价格或最小下单规则无效",
    "COPY_SYMBOL_LEVERAGE_INVALID": "交易所返回的最高杠杆无效",
    "COPY_SYMBOL_CURRENT_LEVERAGE_INVALID": (
        "Binance 返回的该币种杠杆不是可用值, 系统未提交订单; 这不是带单员信号或余额问题"
    ),
    "COPY_SYMBOL_CONFIG_INVALID": (
        "Binance 返回的该交易对杠杆配置不完整, 系统未提交订单并已触发复核"
    ),
    "COPY_TESTNET_SYMBOL_LEVERAGE_UNINITIALIZED_RETRY": (
        "Testnet 账户重置后该币种尚未初始化杠杆, Binance 返回 0; "
        "系统将先设置该币种允许的最大杠杆, 再重新处理原交易信号"
    ),
    "COPY_MARKET_PRICE_INVALID": "当前市场价格无效",
    "COPY_PROTECTED_ENTRY_PRICE_MISSING": "入场保护限价缺失, 系统没有提交订单",
    "COPY_EXCHANGE_INFO_INVALID": "交易所交易规则数据不完整",
    "COPY_ORDER_REQUEST_MISSING": (
        "持久化订单记录缺少可提交给 Binance 的参数, 系统已停止执行以避免错误下单"
    ),
    "COPY_SIZE_NOT_AN_INCREASE": "当前信号不是开仓或加仓, 不应进入开仓金额分配流程",
    "COPY_REDUCTION_NOT_A_REDUCTION": "当前信号不是减仓或平仓, 不应进入减仓计算流程",
    "COPY_BASELINE_ESTABLISHED": "已建立带单员基线, 不会回放加入前的历史交易",
    "COPY_RECOVERY_BASELINE_NO_OWNED_POSITION": "恢复时未发现本系统归属仓位, 已重新建立基线",
    "COPY_CONTROL_BLOCKED_SIGNAL_REQUEUED_AFTER_VERIFICATION": (
        "信号此前被运行控制状态拦截; 系统复核确认可以继续处理后, 已将它重新放回执行队列"
    ),
    "COPY_OPERATOR_FLATTEN_COMPLETED_AUTO_RESUME": (
        "用户发起的全部清仓已经完成, 系统确认没有剩余归属仓位后自动恢复正常跟单"
    ),
    "COPY_SAFETY_FLATTEN_COMPLETED_REMAINS_PAUSED": (
        "风险清仓已经完成, 但触发清仓的风险条件仍需人工确认, 因此继续暂停新开仓"
    ),
    "COPY_REDUCE_ALL_COMPLETED_AUTO_RESUME": (
        "全部减仓指令已经执行完成, 系统确认没有剩余待处理仓位后自动恢复正常跟单"
    ),
    "COPY_RECONCILIATION_VERIFIED_AUTO_RESUME": (
        "此前状态不明确的订单已经与 Binance 完成核对, 确认不存在未处理风险后自动恢复正常跟单"
    ),
    "COPY_SELECTION_SHORT_WIN_RATE_POOL_INSUFFICIENT": "短线一没有足够的合格候选",
    "COPY_SELECTION_SHORT_INTRADAY_POOL_INSUFFICIENT": "短线二没有足够的合格候选",
    "COPY_SELECTION_ELIGIBLE_POOL_INSUFFICIENT": "本轮没有足够的合格带单员候选",
    "COPY_SELECTION_DIRECTORY_NO_VALID_CANDIDATES": (
        "Binance 本轮返回的候选资料均不完整, 系统未使用不可靠数据并保留当前带单员"
    ),
    "COPY_SELECTION_HISTORY_UNAVAILABLE": "带单员近期操作记录暂时无法读取",
    "COPY_SELECTION_POSITION_SIDE_AMBIGUOUS": "带单员公开记录无法可靠判断多空方向",
    "COPY_SELECTION_EXECUTION_SYMBOL_COMPATIBILITY_LOW": "带单员交易的币种与当前执行环境兼容率过低",
    "COPY_SELECTION_WIN_RATE_LOW": "公开胜率低于选人门槛",
    "COPY_SELECTION_DRAWDOWN_HIGH": "最大回撤超过选人门槛",
    "COPY_SELECTION_AUM_LOW": "带单资金规模低于选人门槛",
    "COPY_SELECTION_FOLLOWER_COUNT_LOW": "当前跟单人数低于自动选人门槛",
    "COPY_SELECTION_RETURN_NONPOSITIVE": "公开收益不是正数",
    "COPY_SELECTION_TRACK_RECORD_SHORT": "公开交易记录时间过短",
    "COPY_SELECTION_CLOSE_SAMPLE_SMALL": "可验证的平仓样本不足",
    "COPY_SELECTION_CLOSE_QUALITY_LOW": "近期有效平仓质量偏低",
    "COPY_SELECTION_PROFIT_FACTOR_LOW": "近期盈亏比低于选人门槛",
    "COPY_SELECTION_PROFIT_CONCENTRATED": "近期盈利过度依赖少数大额盈利单",
    "COPY_SELECTION_ROBUST_PNL_NONPOSITIVE": "剔除最大盈利单后近期收益不为正",
    "COPY_SELECTION_LOSS_STREAK_HIGH": "近期连续亏损次数过多",
    "COPY_SELECTION_ACTIVITY_LOW": "近期交易活跃度不足",
    "COPY_SELECTION_RECENT_PNL_NONPOSITIVE": "最近三天已实现收益不为正, 仅降权观察, 不单独淘汰",
    "COPY_SELECTION_DRAWDOWN_ACCELERATING": "近期最大回撤扩大, 已降权但会结合完整样本判断",
    "COPY_SELECTION_ROI_DROPPING_FAST": "近期公开收益率回落, 已降权但不会因单一短期波动淘汰",
    "COPY_SELECTION_AUM_DROPPING_FAST": "近期带单资金规模明显下降",
    "COPY_SELECTION_PROFIT_FACTOR_ANOMALOUS": "近期利润因子异常偏高, 已降低可信度",
    "COPY_SELECTION_TRACK_RECORD_MATURING": "公开交易历史仍较短, 已限制置信度",
    "COPY_SELECTION_MANUAL_CLEAR_COOLDOWN": "用户今天清空过该带单员仓位, 当天不重新跟入",
    "COPY_SELECTION_LOCKED_INCUMBENT_RETAINED": "当前带单员已锁定, 本轮不会自动替换",
    "COPY_SELECTION_LOCKED_SLOT_BACKUP_SELECTED": (
        "当前带单员保持锁定, 本轮候选只作为备用记录, 不会自动接管"
    ),
    "COPY_SELECTION_LOCKED_SLOT_BACKUP_UNAVAILABLE": (
        "当前带单员保持锁定, 本轮没有通过门槛的备用候选"
    ),
    "COPY_TELEGRAM_LEADER_ALREADY_IN_SLOT": "该带单员已经配置在这条线上",
    "COPY_TELEGRAM_LEADER_ASSIGNED_ELSEWHERE": "该带单员已经配置在另一条线上, 各条线不能重复",
    "COPY_TELEGRAM_LEADER_EVIDENCE_UNAVAILABLE": "没有读到可用于建立跟单基线的近期公开操作",
    "COPY_TELEGRAM_LEADER_SYMBOL_COMPATIBILITY_LOW": "该带单员近期交易品种与当前执行环境兼容率不足",
    "COPY_TELEGRAM_LEADER_DRAINING_WITH_POSITION": "该带单员仍有旧仓位正在排空, 暂时不能重新配置",
    "COPY_TELEGRAM_SHORT_LEADER_ACTIVITY_LOW": "该带单员近期活跃度未达到自动短线门槛",
    "COPY_DRAINING_COMPLETED_RETIRED": "旧带单员仓位与待处理订单均已归零, 已停止额外轮询",
    "COPY_SELECTION_ASSIGNED_TO_OTHER_STRATEGY": "该带单员已分配到另一个槽位, 本轮不会重复占用",
    "COPY_SLOT_REPLACEMENT_BLOCKED_BY_LEADER_LOCK": (
        "当前带单员已被用户锁定, 本轮自动选人不会替换他; 交易跟随继续正常运行"
    ),
    "COPY_SLOT_REPLACEMENT_CANCELLED_BY_LEADER_LOCK": (
        "当前带单员已被用户锁定, 先前排队的自动换人已取消"
    ),
    "TELEGRAM_LEADER_LOCKED": "用户已锁定该带单员, 禁止定时自动替换",
    "TELEGRAM_LEADER_UNLOCKED": "用户已解锁该带单员, 恢复参与后续定时自动选人",
    "COPY_SELECTION_CANDIDATE_POOL_INVALID": (
        "候选带单员数据结构异常, 本轮不更换现有带单员并触发自动排查"
    ),
    "COPY_SELECTION_REVIEW_POOL_INVALID": ("提交给 Codex 复核的候选数据异常, 本轮不更换现有带单员"),
    "COPY_SELECTION_LEADER_COUNT_INVALID": "本轮要求选择的带单员数量配置无效, 未执行更换",
    "COPY_SELECTION_SLOT_COUNT_INVALID": "本轮待更新槽位数量与候选结果不一致, 未执行更换",
    "COPY_SELECTION_EXECUTION_ENVIRONMENT_INVALID": ("选人任务的执行环境配置无效, 本轮未执行更换"),
    "COPY_PUBLIC_TRANSPORT_FAILED": "连接 Binance 公开数据接口失败",
    "COPY_PUBLIC_HISTORY_GAP": (
        "系统恢复后无法从 Binance 公开接口完整覆盖停机期间的带单员操作记录; "
        "为避免漏单或错单, 已停止处理该段不完整历史并触发排查"
    ),
    "COPY_ORDER_HISTORY_WATERMARK_MISSING": (
        "数据库中缺少该带单员上次成功同步位置, 系统无法确认历史是否完整"
    ),
    "COPY_ORDER_HISTORY_WATERMARK_NOT_COVERED": (
        "带单员新增操作过多, 本轮分页仍未追到上次同步位置; 系统不会跳过中间记录"
    ),
    "COPY_ORDER_POSITION_SIDE_UNRESOLVED": (
        "带单员公开记录不足以判断这笔订单属于多仓还是空仓, 系统未据此下单"
    ),
    "COPY_SIGNAL_POSITION_SIDE_AMBIGUOUS": "带单员信号的多空方向存在歧义, 系统未据此下单",
    "COPY_BASELINE_POSITION_SIDE_EVIDENCE_DEFERRED": (
        "首次加入时仅建立历史水位, 历史记录方向不完整不影响手动添加"
    ),
    "COPY_LEADER_LOOKUP_NOT_FOUND": "没有在 Binance 公开目录中找到该带单员",
    "COPY_MANUAL_LEADER_ONE_WAY_EVIDENCE_UNRESOLVED": "公开操作不足以可靠还原该带单员的多空方向",
    "COPY_MANUAL_LEADER_POSITION_SIDE_AMBIGUOUS": "该带单员公开操作的多空方向存在歧义",
    "COPY_CODEX_REPAIR_EXECUTION_FAILED": (
        "Codex CLI 未能启动、执行超时或没有返回结果, 因此本轮自动修复没有完成"
    ),
    "COPY_CODEX_REPAIR_SCHEMA_INVALID": (
        "Codex 返回的修复报告不符合系统要求的结构, 为避免误操作, 本轮结果已拒绝应用"
    ),
    "COPY_CODEX_REPAIR_REQUEST_INVALID": "自动修复请求内容无效, Codex 没有开始修改系统",
    "COPY_CODEX_AUDIT_EXECUTION_FAILED": (
        "Codex CLI 审查未能启动、执行超时或没有返回结果; systemd 会自动重试, "
        "确定性巡检和交易风控仍独立运行"
    ),
    "COPY_CODEX_AUDIT_SCHEMA_INVALID": (
        "Codex 审查结果格式不符合系统约束, 本轮结果已拒绝应用并将自动重试"
    ),
    "COPY_CODEX_AUDIT_ACTIONS_INVALID": (
        "Codex 建议的操作组合不符合安全规则, 系统没有执行这些操作并将自动重试"
    ),
    "COPY_CODEX_AUDIT_REPORTED_FAILURE": "最近一次 Codex 自动审查执行失败, systemd 正在重试",
    "COPY_CODEX_REPAIR_VERIFIED": "Codex 修改已通过完整测试和运行状态复检",
    "COPY_REQUIRED_SERVICE_INACTIVE": "至少一个跟单核心服务没有运行, 已触发自动恢复",
    "COPY_REQUIRED_SERVICE_RECOVERED_AUTO_RESUME": (
        "先前由系统巡检发现的核心服务异常已经消失, 所有实时检查均通过; "
        "系统只撤销本次自动暂停并恢复接收新开仓, 不会撤销用户手动暂停"
    ),
    "COPY_TESTNET_USER_STREAM_INACTIVE": "Binance 测试盘账户事件监听服务没有运行",
    "COPY_TELEGRAM_OUTBOX_STALLED": "Telegram 通知队列长时间未发送完成",
    "COPY_DEAD_TELEGRAM_NOTIFICATIONS": (
        "存在连续五次发送失败的 Telegram 通知, 已保留消息并触发排查"
    ),
    "COPY_NO_ACTIVE_LEADERS": "当前没有可轮询的带单员, 系统不会产生新的跟单信号",
    "COPY_POLL_STALE": "带单员轮询已停止或超过允许延迟, 新开仓将被安全暂停",
    "COPY_POLL_DELAYED": "带单员轮询延迟, 系统仍在运行但信号可能晚于正常周期",
    "COPY_PUBLIC_POLL_FAILURES": "所有带单员公开数据接口在本轮均读取失败",
    "COPY_PUBLIC_POLL_PARTIAL_FAILURE": "部分带单员读取失败, 其他带单员仍继续独立处理",
    "COPY_UNCERTAIN_SUBMISSIONS": (
        "存在 Binance 未明确返回结果的订单; 系统只核对原订单号, 不会重复下单"
    ),
    "COPY_RECENT_EXECUTION_FAILURES": "最近一小时出现多笔跟单执行失败, 已暂停新开仓并排查",
    "COPY_PROTECTED_ENTRY_OVERDUE": "存在超过撤单宽限期仍未确认终态的保护限价单",
    "COPY_POSITION_RECONCILIATION_MISMATCH": (
        "Binance 实际仓位与各带单员虚拟账本合计不一致, 系统已进入安全保护"
    ),
    "COPY_DATABASE_URL_FILE_UNSAFE": (
        "数据库连接配置文件缺失、权限不安全或内容无效, 服务已拒绝启动并触发故障报告"
    ),
}

_SUFFIX_LABELS = (
    ("_ACCESS_DENIED", "接口访问被拒绝"),
    ("_TRANSPORT_FAILED", "网络请求失败"),
    ("_RESPONSE_TOO_LARGE", "接口返回数据过大"),
    ("_INVALID_RESPONSE", "接口返回的数据格式无效"),
    ("_READ_FAILED", "读取系统数据失败"),
    ("_WRITE_FAILED", "写入系统数据失败"),
    ("_UNAVAILABLE", "所需服务或数据暂时不可用"),
    ("_MISSING", "所需数据缺失"),
    ("_STALE", "所需数据已经过期"),
    ("_INVALID", "数据或配置校验未通过"),
    ("_FAILED", "操作执行失败"),
    ("_INSUFFICIENT", "当前条件或可用额度不足"),
)


def reason_code_text(reason_code: str) -> str:
    """Return Chinese operator text while keeping raw codes in durable storage only."""
    label = _LABELS.get(reason_code)
    if label is not None:
        return label
    if reason_code.startswith("COPY_EXCHANGE_CODE_"):
        return f"交易所返回错误代码 {reason_code.removeprefix('COPY_EXCHANGE_CODE_')}"
    for suffix, suffix_label in _SUFFIX_LABELS:
        if reason_code.endswith(suffix):
            return suffix_label
    return (
        "系统遇到尚未配置中文明细的内部异常; 本次不会据此盲目交易, 完整证据已保留并交由 Codex 排查"
    )


def translate_reason_codes_in_text(value: str) -> str:
    """Replace any internal reason-code token before a message reaches Telegram."""
    return _REASON_CODE.sub(lambda match: reason_code_text(match.group(0)), value)
