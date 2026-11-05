"""daily_config.py —— **每日 ETF 轮动流水线**的全部常量（单一事实源）。

【本模块是干什么的】
`manu/etf_rotation/daily/` 是**每日 15:30 自动执行**的流水线（触发 → 日历门 → 增量取数 →
Qlib 穿透打分 → 组合映射 → 邮件推送）。它与 `manu/etf_rotation/` 的**研究链**是两套东西：

| | 研究链（`manu/etf_rotation/*.py`） | 每日流水线（`manu/etf_rotation/daily/`） |
|:--|:--|:--|
| 触发 | 手工 | cron，**每个 A 股交易日 15:30** |
| 产物 | 固定交付件（`/mnt/data/siping/runs/etf_rotation/`） | **按日独立**目录（`.../daily/<YYYYMMDD>/`） |
| 账户口径 | 研究基准（2026-11-04 快照，29,854.67 元） | **实盘基准**（2026-11-05 快照，30,035.57 元，见 `account_state.py`） |
| LLM | 每次调 | **仅调仓节点日**才调（模式 B） |

🔴 **口径隔离纪律**（铁律〇之三 + 铁律十四的延伸）：
每日流水线**绝不覆盖**研究链的交付件 ⇒ 研究链的指纹表 / 闸门 / 可复现性全部保持原样。
每日链路的账户口径写在 `account_state.py`，**不写回** `manu/etf_rotation/account.py`
（后者是研究链的历史基准，改它会让已归档的交付件与指纹表**假性失配**）。

【所有路径都可被环境变量覆盖】（服务器上 cron 的环境极简 ⇒ 必须能注入）
    ETF_ROTATION_REPO_ROOT   工程根（默认 /tmp/pycharm_project_658d3fc4）
    ETF_ROTATION_RUN_ROOT    每日产物根（默认 /mnt/data/siping/runs/etf_rotation/daily）
    ETF_ROTATION_LOG_DIR     日志目录（默认 <RUN_ROOT>/logs）
    ETF_ROTATION_QL_PY       量化层解释器（默认 /home/huangzhe/anaconda3/envs/ql/bin/python）
    ETF_ROTATION_AK_PY       akshare 解释器（默认 /home/huangzhe/anaconda3/envs/ak/bin/python）
    ETF_ROTATION_LLM_PY      TradingAgents 解释器（默认 /home/huangzhe/anaconda3/envs/llm/bin/python）
    ETF_ROTATION_SMTP_SECRETS  0600 权限的 env 文件（默认 /mnt/data/siping/secrets/smtp.env）
"""
from __future__ import annotations

import os
from pathlib import Path

# =============================================================================
# 0. 路径（全部可用环境变量覆盖）
# =============================================================================
#: 工程根（服务器工作副本）
REPO_ROOT = Path(os.environ.get("ETF_ROTATION_REPO_ROOT", "/tmp/pycharm_project_658d3fc4"))
#: 每日链路的产物根（按日期分子目录）
RUN_ROOT = Path(
    os.environ.get("ETF_ROTATION_RUN_ROOT", "/mnt/data/siping/runs/etf_rotation/daily")
)
#: 日志目录（铁律二：cache 落 /mnt/data，**不落 /tmp**）
LOG_DIR = Path(
    os.environ.get("ETF_ROTATION_LOG_DIR", str(RUN_ROOT / "logs"))
)

#: 三个 conda 解释器（**绝对路径** ⇒ cron 不依赖 shell 初始化）
QL_PY = os.environ.get("ETF_ROTATION_QL_PY", "/home/huangzhe/anaconda3/envs/ql/bin/python")
#: akshare 环境（🔴 akshare **绝不能**装进 ql env ⇒ 只能 shell out 到这个解释器）
AK_PY = os.environ.get("ETF_ROTATION_AK_PY", "/home/huangzhe/anaconda3/envs/ak/bin/python")
#: TradingAgents 环境
LLM_PY = os.environ.get("ETF_ROTATION_LLM_PY", "/home/huangzhe/anaconda3/envs/llm/bin/python")

#: ETF 数据集目录（服务器持久盘）
ETF_DATA_ROOT = Path("/mnt/data/siping/datasets/etf")
#: ETF 日线行情（含 ETF 收盘价）
ETF_PRICES_CSV = ETF_DATA_ROOT / "etf_prices.csv"
#: ETF 现货快照
ETF_SPOT_CSV = ETF_DATA_ROOT / "etf_spot.csv"
#: ETF 候选池
ETF_UNIVERSE_CSV = ETF_DATA_ROOT / "etf_universe.csv"
#: **W（成分股权重）缓存** —— 每日链路**只读**这个文件
ETF_HOLDINGS_CSV = ETF_DATA_ROOT / "etf_holdings.csv"
#: A 股交易日历（含未来交易日）
TRADE_CALENDAR_CSV = ETF_DATA_ROOT / "trade_calendar.csv"

#: 研究链产物根（每日链路**只读**参照，不写）
RESEARCH_RUN_ROOT = Path("/mnt/data/siping/runs/etf_rotation")
#: 个股分数 S（上游产物）
STOCK_SCORES_CSV = RESEARCH_RUN_ROOT / "stock_scores.csv"
#: qlib 二进制数据集目录（增量写 bar 用）
QLIB_CN_DATA = Path("/mnt/data/siping/datasets/qlib/cn_data")

#: TradingAgents 工作副本（llm env 侧）
TRADINGAGENTS_DIR = Path(os.environ.get("ETF_ROTATION_TA_DIR", str(REPO_ROOT / "TradingAgents")))

#: SMTP 授权码 env 文件（🔴 **不进仓库**；须 0600）
SMTP_SECRETS_FILE = Path(
    os.environ.get("ETF_ROTATION_SMTP_SECRETS", "/mnt/data/siping/secrets/smtp.env")
)

# =============================================================================
# 1. 调度
# =============================================================================
#: 触发时刻（每个**工作日** 15:30）
TRIGGER_TIME = "15:30"
#: cron 表达式（周一~周五；**日历门在进程内** ⇒ 非交易日进程自己静默退出 0）
CRON_EXPR = "30 15 * * 1-5"
#: cron 任务名（安装 / 卸载都用它）
CRON_TAG = "etf_rotation_daily"
#: cron 落盘位置
CRON_FILE = Path(os.environ.get("ETF_ROTATION_CRON_FILE", "/etc/cron.d/etf_rotation_daily"))
#: 单实例锁（防重入；锁文件落 /mnt/data，不落 /tmp）
LOCK_FILE = Path(
    os.environ.get("ETF_ROTATION_LOCK_FILE", "/mnt/data/siping/runs/etf_rotation/daily/.lock")
)
#: **发信专用**单实例锁（🔴 与 `LOCK_FILE` 分开）
#: 理由：15:30 的分析可能还没跑完（调仓日要跑 TradingAgents LLM），
#: 16:00 的发信进程**不能**被分析锁挡住 ⇒ 两条链各用各的锁。
LOCK_FILE_SEND = Path(
    os.environ.get(
        "ETF_ROTATION_LOCK_FILE_SEND",
        "/mnt/data/siping/runs/etf_rotation/daily/.lock.send",
    )
)

# =============================================================================
# 2. 实盘账户（2026-11-05 用户口径；**推导值见 account_state.py**）
# =============================================================================
#: 总资产（元）= 现金 23,367.37 + 持仓市值 6,668.20
TOTAL_ASSETS = 30_035.57
#: 可用现金（元）
CASH = 23_367.37
#: ETF 持仓市值（元）
MARKET_VALUE = 6_668.20
#: 账户快照日（用户持仓截图日）
ACCOUNT_SNAPSHOT_DATE = "2026-11-05"
#: 唯一在保底仓
PRIMARY_HOLDING_CODE = "512800"
PRIMARY_HOLDING_NAME = "银行ETF华宝"
PRIMARY_HOLDING_SHARES = 7_700
PRIMARY_HOLDING_COST = 0.8239

# =============================================================================
# 3. 组合构造约束（用户 2026-11-05 口径）
# =============================================================================
#: 目标只数（等权 5 份）
TARGET_POSITIONS = 5
#: 单只标的**金额上限**（元）—— 🔴 现有研究链**没有**这个硬上限，本流水线新增
MAX_POSITION_AMOUNT = 6_000.0
#: 最小交易单位（手）；**必须向下取整**到它的整数倍
LOT_SIZE = 100
#: 调仓后**必须保留**的现金缓冲下限 / 上限（元）
CASH_BUFFER_MIN = 200.0
CASH_BUFFER_MAX = 300.0
#: 佣金（万分之；买卖各算一次，下限 5 元 —— 与研究链一致）
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
#: 印花税（**卖出**单边，千分之一）
STAMP_DUTY_RATE = 0.001
#: 滑点（万分之一，估算用）
SLIPPAGE_RATE = 0.0001

# =============================================================================
# 4. 均线安全带（与研究链 config 口径一致，**独立复制**以免研究链改动传导到线上）
# =============================================================================
#: 入场门槛：收盘价未站上 MA20 ⇒ **严禁**纳入新开仓推荐
ENTRY_MA_WINDOW = 20
#: 持仓止损：收盘价有效跌破 MA60 ⇒ 触发紧急避险
EXIT_MA_WINDOW = 60
#: 「有效跌破」的容忍幅度（跌破超过该比例才算有效破位）
BREAK_CONFIRM_RATIO = 0.0

# =============================================================================
# 5. 调仓节点（用户锁死口径）
# =============================================================================
#: 下一正式调仓**信号日**（收盘出信号）
NEXT_SIGNAL_DATE = "2026-12-01"
#: 执行日（12-02 **开盘**）—— 🔴 研究链 `config.EXECUTION_LAG_DAYS=1` 写的是「T+1 **收盘**」
#:    ⇒ 两者差一个盘口；执行卡里**必须显式标注**这个差异，不许混称
NEXT_EXECUTION_DATE = "2026-12-02"
#: 静默防守窗口（含端点）：期间**严禁**生成任何买入指令
SILENT_DEFENSE_START = "2026-11-06"
SILENT_DEFENSE_END = "2026-11-30"
#: 信号日 → 执行日的交易日滞后（1 个交易日）
EXECUTION_LAG_DAYS = 1

# =============================================================================
# 5.5 阶段 1（qlib 数据集滚动快照换库）
# =============================================================================
#: qlib 数据集根目录（换库时会把**整个目录**改名备份，不逐文件改）
QLIB_DATASET_ROOT = Path(os.environ.get("ETF_ROTATION_QLIB_DATASET_ROOT", "/mnt/data/siping/datasets/qlib"))
#: 滚动快照的解压工作区（🔴 **必须落 /mnt/data**，不许落 /tmp：tmpfiles 会清 /tmp）
STAGE1_WORK_ROOT = Path(
    os.environ.get("ETF_ROTATION_STAGE1_WORK", str(QLIB_DATASET_ROOT / "_stage1_work"))
)
#: 换库前的旧库备份目录（同一个 /mnt/data 盘 ⇒ 改名是原子操作）
STAGE1_BACKUP_DIR = Path(
    os.environ.get("ETF_ROTATION_STAGE1_BACKUP", str(QLIB_DATASET_ROOT / "_backup"))
)
#: 滚动快照下载落盘目录（复用 manu/tools/fetch_qlib_dataset.py 的 dest_dir）
STAGE1_DEST_DIR = QLIB_DATASET_ROOT

# =============================================================================
# 5.8 报告落盘（按需分析 ⇒ 报告回窗口）
# =============================================================================
#: **报告归档目录**（仓库内 `manu/report/`）—— 每次分析的 Markdown 报告都落这里，
#: 文件名带**分析日期**（`report_<YYYYMMDD>.md`）⇒ 便于回看与版本化。
#: 🔴 用**模块自身位置**推导（不依赖 REPO_ROOT 环境变量）⇒ host / 服务器都指向同一个
#: 仓库内的 `manu/report/`；REPO_ROOT 在服务器上默认是 `/tmp/pycharm_project_658d3fc4`，
#: 在 host 上不存在 ⇒ 用它推导会把报告写到错误位置。
_MANU_ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = Path(
    os.environ.get("ETF_ROTATION_REPORT_DIR", str(_MANU_ROOT / "report"))
)

# =============================================================================
# 6. 邮件（🔴 **按需 opt-in**：默认不发信，报告直接回窗口）
# =============================================================================
#: 收件邮箱
MAIL_TO = os.environ.get("ETF_ROTATION_MAIL_TO", "zxthz@126.com")
#: 发件账号（默认与收件同号；126 必须用**授权码**，不能用登录密码）
MAIL_FROM = os.environ.get("ETF_ROTATION_MAIL_FROM", MAIL_TO)
#: SMTP 主机 / 端口（126）
SMTP_HOST = os.environ.get("ETF_ROTATION_SMTP_HOST", "smtp.126.com")
SMTP_PORT = int(os.environ.get("ETF_ROTATION_SMTP_PORT", "465"))
#: 是否走隐式 SSL（465 ⇒ True；587 ⇒ False + STARTTLS）
SMTP_USE_SSL = os.environ.get("ETF_ROTATION_SMTP_USE_SSL", "1") not in ("0", "false", "False")
#: 超时（秒）
SMTP_TIMEOUT = float(os.environ.get("ETF_ROTATION_SMTP_TIMEOUT", "20"))
#: 邮件里统一的免责声明（**所有模板必须带**）
DISCLAIMER = (
    "本邮件由本地自动化流水线生成，内容来自 Qlib 个股打分（E=W×S）与规则过滤，"
    "**不构成投资建议**，也不保证任何收益；历史统计不代表未来表现，"
    "下单与否请自行判断并自行承担全部风险。"
)

# =============================================================================
# 7. 数据新鲜度 / 守卫阈值
# =============================================================================
#: W（成分股权重）快照允许的最大滞后（交易日）—— 🔴 **只披露不拦**（重爬会被 IP 封禁）
HOLDINGS_STALE_DAYS_DISCLOSE = 5
#: 公司行为守卫：复权序列与现价推算的日收益差超过该阈值 ⇒ 该股**剔除**并上报
FACTOR_DIVERGENCE_THRESHOLD = 0.02
#: 单次运行最长墙钟（秒）；超时 ⇒ 中止并告警
MAX_WALL_SECONDS = float(os.environ.get("ETF_ROTATION_MAX_WALL_SECONDS", "3600"))

# =============================================================================
# 7.1 阶段 4（TradingAgents 多智能体辩论）
# =============================================================================
#: 阶段 4 的**墙钟超时**（秒）—— 🔴 2026-11-05 实测事故的直接修复项：
#:    MiniMax-M3.1-Flash + `reasoning_effort=max` 时单次 LLM 调用 **185 秒**
#:    （同一份 2484-token 上下文；high 116s / medium 91s）
#:    ⇒ 多智能体图要调十几次、后段上下文更长 ⇒ **1800 秒根本不够**
#:    （实测：阶段 4 从 15:52 跑到 16:22 被 1800 秒超时打断）。
#:    ⇒ 默认给到 **5400 秒（90 分钟）**，宁可慢也别把辩论打断在半截。
LLM_TIMEOUT = float(os.environ.get("ETF_ROTATION_LLM_TIMEOUT", "5400"))


def env_flag(name: str, default: bool = False) -> bool:
    """读环境变量布尔值（`1/true/yes/on` ⇒ True）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
