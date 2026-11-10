"""daily_config.py —— **每日 ETF 轮动流水线**的全部常量（单一事实源）。

【本模块是干什么的】
`manu/etf_rotation/daily/` 是**每日 15:30 自动执行**的流水线（触发 → 日历门 → 增量取数 →
Qlib 穿透打分 → 组合映射 → 邮件推送）。它与 `manu/etf_rotation/` 的**研究链**是两套东西：

| | 研究链（`manu/etf_rotation/*.py`） | 每日流水线（`manu/etf_rotation/daily/`） |
|:--|:--|:--|
| 触发 | 手工 | cron，**每个 A 股交易日 15:30** |
| 产物 | 固定交付件（`<RESULTS_ROOT>/`） | **按日独立**目录（`<RESULTS_ROOT>/daily/<YYYYMMDD>/`） |
| 账户口径 | 研究基准（2026-11-04 快照，29,854.67 元） | **实盘基准**（2026-11-05 快照，30,035.57 元，见 `account_state.py`） |
| LLM | 每次调 | **仅调仓节点日**才调 |

🔴 **口径隔离纪律**（铁律〇之三 + 铁律十四的延伸）：
每日流水线**绝不覆盖**研究链的交付件 ⇒ 研究链的指纹表 / 闸门 / 可复现性全部保持原样。
每日链路的账户口径写在 `account_state.py`，**不写回** `manu/etf_rotation/account.py`
（后者是研究链的历史基准，改它会让已归档的交付件与指纹表**假性失配**）。

【两个落点根（数据集 / 结果分开）】
    服务器 `huangzhe@192.168.99.40` 是**共享训练机** ⇒ 本项目在服务器上产生的
    **一切文件**只允许落在这**两个**项目专属根之下（不许再往 `/mnt/data` 根或
    `/mnt/data/siping/datasets/`、`/mnt/data/siping/runs/` 的公共目录里写）：
      * `ETF_ROTATION_OUTPUT_ROOT`  **数据集根**（默认 /mnt/data/siping/datasets/manu/etf_rotation）
        ⇒ 行情/权重/日历/qlib 数据集（`datasets/etf`、`datasets/qlib`）+ 临时工作区 `_work/`
      * `ETF_ROTATION_RESULTS_ROOT` **结果根**（默认 /mnt/data/siping/runs/etf_rotation_results）
        ⇒ 产物/快照/报告/日志（研究链 canonical、每日链 `daily/`、`snapshots/`、`logs/`）
    🔴 **所有路径都可被环境变量覆盖**（服务器上 cron 的环境极简 ⇒ 必须能注入）
    ETF_ROTATION_REPO_ROOT   工程根（默认 /tmp/pycharm_project_658d3fc4）
    ETF_ROTATION_RUN_ROOT    每日产物根（默认 <RESULTS_ROOT>/daily）
    ETF_ROTATION_LOG_DIR     日志目录（默认 <RESULTS_ROOT>/logs）
    ETF_ROTATION_QL_PY       量化层解释器（默认 /home/huangzhe/anaconda3/envs/ql/bin/python）
    ETF_ROTATION_AK_PY       akshare 解释器（默认 /home/huangzhe/anaconda3/envs/ak/bin/python）
    ETF_ROTATION_LLM_PY      TradingAgents 解释器（默认 /home/huangzhe/anaconda3/envs/llm/bin/python）
    ETF_ROTATION_SMTP_SECRETS  0600 权限的 env 文件（默认 /mnt/data/siping/secrets/smtp.env）
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import timedelta, timezone
from pathlib import Path

# =============================================================================
# 0.0 市场时区（🔴 单一事实源：所有「今天 / 当日 / 收盘时刻」判定都锚这里）
# =============================================================================
#: A 股市场时区（东八区）。🔴 **不许**用机器本地时区代替：
#:   「15:05 收盘定型」「今天是哪一天」是**市场规则**，不是机器规则
#:   ⇒ 机器跑在 UTC 容器里时，本地时区的「今天」会落后一天、「收盘」会差 8 小时
#:   ⇒ 典型后果：`fetch_data.drop_unfinalized_bars` 在 UTC 机器上 07:00 (= 15:00 CST)
#:   仍认为「当日 bar 未定型」⇒ 丢掉**已收盘**的 bar ⇒ 行情底座比信号日差一天
#:   ⇒ 下游 `_data_freshness_guard` fail-closed ⇒ 整轮白跑（2026-11-10 实测事故）。
MARKET_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# =============================================================================
# 0. 路径（全部可用环境变量覆盖）
# =============================================================================
#: 🔴 **数据集落点根**（本项目的**单一事实源 · 数据集侧**）
#:    服务器 `huangzhe@192.168.99.40` 是**共享训练机** ⇒ 本项目在服务器上产生的
#:    **一切文件**只允许落在下面**两个**项目专属根之下（数据集根 + 结果根）。
#:    ⇒ 下面**所有**路径常量都由这两个根派生（不再有第三个字面量）。
#:    可被环境变量 `ETF_ROTATION_OUTPUT_ROOT` 覆盖（cron 环境极简 ⇒ 必须能注入）。
ETF_ROTATION_OUTPUT_ROOT = Path(
    os.environ.get(
        "ETF_ROTATION_OUTPUT_ROOT", "/mnt/data/siping/datasets/manu/etf_rotation"
    )
).expanduser()
#: 🔴 **结果落点根**（本项目的**单一事实源 · 结果侧**）
#:    产物 / 快照 / 报告 / 日志**留在服务器**，不再拷回 host ⇒ 全部落在这个根之下。
#:    可被环境变量 `ETF_ROTATION_RESULTS_ROOT` 覆盖。
ETF_ROTATION_RESULTS_ROOT = Path(
    os.environ.get(
        "ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results"
    )
).expanduser()
#: 工程根（服务器工作副本）
REPO_ROOT = Path(os.environ.get("ETF_ROTATION_REPO_ROOT", "/tmp/pycharm_project_658d3fc4"))
#: 研究链产物根（每日链路**只读**参照，不写）
#:    落点 `<RESULTS_ROOT>`（研究链交付件 + 每日链的父目录）
RESEARCH_RUN_ROOT = ETF_ROTATION_RESULTS_ROOT
#: 每日链路的产物根（按日期分子目录）
#:    落点 `<RESULTS_ROOT>/daily`（按日期分子目录）
RUN_ROOT = Path(
    os.environ.get("ETF_ROTATION_RUN_ROOT", str(RESEARCH_RUN_ROOT / "daily"))
)
#: 日志目录（铁律二：cache 落项目落点根，**不落 /tmp**）
LOG_DIR = Path(
    os.environ.get("ETF_ROTATION_LOG_DIR", str(ETF_ROTATION_RESULTS_ROOT / "logs"))
)
#: **临时工作区**（子进程脚本 / 解压中间产物…）—— 落点 `<OUTPUT_ROOT>/_work`
WORK_ROOT = ETF_ROTATION_OUTPUT_ROOT / "_work"

#: 三个 conda 解释器（**绝对路径** ⇒ cron 不依赖 shell 初始化）
QL_PY = os.environ.get("ETF_ROTATION_QL_PY", "/home/huangzhe/anaconda3/envs/ql/bin/python")
#: akshare 环境（🔴 akshare **绝不能**装进 ql env ⇒ 只能 shell out 到这个解释器）
AK_PY = os.environ.get("ETF_ROTATION_AK_PY", "/home/huangzhe/anaconda3/envs/ak/bin/python")
#: TradingAgents 环境
LLM_PY = os.environ.get("ETF_ROTATION_LLM_PY", "/home/huangzhe/anaconda3/envs/llm/bin/python")

#: ETF 数据集目录（服务器持久盘）
#:    落点 `<OUTPUT_ROOT>/datasets/etf`
ETF_DATA_ROOT = ETF_ROTATION_OUTPUT_ROOT / "datasets" / "etf"
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

#: 个股分数 S（上游产物）
STOCK_SCORES_CSV = RESEARCH_RUN_ROOT / "stock_scores.csv"
#: 🆕 **快照根**（2026-11-09 架构瘦身）：**每次运行落一个不可变快照目录**
#:    `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/` ⇒ 交付件 + 上下文 + manifest 同目录。
#: 🔴 **每日链不再写任何根目录全局文件**（canonical `stock_scores.csv` /
#:    `tradingagents_decision.*` / `rebalance_recommendation.json` 全部只读或不碰）
#:    ⇒ canonical 只在「还没有任何快照」时当**一次性只读种子**用一次。
SNAPSHOT_ROOT = Path(
    os.environ.get(
        "ETF_ROTATION_SNAPSHOT_ROOT", str(ETF_ROTATION_RESULTS_ROOT / "snapshots")
    )
)
#: qlib 二进制数据集根目录（换库时会把**整个目录**改名备份，不逐文件改）
#:    落点 `<OUTPUT_ROOT>/datasets/qlib`
QLIB_DATASET_ROOT = Path(
    os.environ.get(
        "ETF_ROTATION_QLIB_DATASET_ROOT",
        str(ETF_ROTATION_OUTPUT_ROOT / "datasets" / "qlib"),
    )
)
#: qlib 二进制数据集目录（增量写 bar 用）
QLIB_CN_DATA = QLIB_DATASET_ROOT / "cn_data"

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
#: 单实例锁（防重入；锁文件落 <OUTPUT_ROOT>，不落 /tmp）
LOCK_FILE = Path(
    os.environ.get("ETF_ROTATION_LOCK_FILE", str(RUN_ROOT / ".lock"))
)
#: **发信专用**单实例锁（🔴 与 `LOCK_FILE` 分开）
#: 理由：15:30 的分析可能还没跑完（调仓日要跑 TradingAgents LLM），
#: 16:00 的发信进程**不能**被分析锁挡住 ⇒ 两条链各用各的锁。
LOCK_FILE_SEND = Path(
    os.environ.get(
        "ETF_ROTATION_LOCK_FILE_SEND",
        str(RUN_ROOT / ".lock.send"),
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
#: 滚动快照的解压工作区（🔴 **必须落 <OUTPUT_ROOT>**，不许落 /tmp：tmpfiles 会清 /tmp）
STAGE1_WORK_ROOT = Path(
    os.environ.get("ETF_ROTATION_STAGE1_WORK", str(QLIB_DATASET_ROOT / "_stage1_work"))
)
#: 换库前的旧库备份目录（同一个盘 ⇒ 改名是原子操作）
STAGE1_BACKUP_DIR = Path(
    os.environ.get("ETF_ROTATION_STAGE1_BACKUP", str(QLIB_DATASET_ROOT / "_backup"))
)
#: 滚动快照下载落盘目录（复用 manu/tools/fetch_qlib_dataset.py 的 dest_dir）
STAGE1_DEST_DIR = QLIB_DATASET_ROOT
#: 🔴 **自建增量日的派生数据集目录**（上游 `cn_data` 的**派生**，铁律〇之二：只可调用/读取/
#:    派生到新目录）—— 上游快照滞后时用它兜底（见 `stage1_rolling_increment.py`）。
#:    🔴 **绝不能把自建日写进上游 `cn_data`**：那会让 `stage1_dataset.compare_calendars`
#:    的前缀闸门**永远拒绝换库**（fail-closed）。
QLIB_CN_DATA_LIVE = Path(
    os.environ.get(
        "ETF_ROTATION_QLIB_CN_DATA_LIVE", str(QLIB_DATASET_ROOT / "cn_data_live")
    )
)
#: 自建增量日的台账（JSONL：日期 / 来源 / 写入与剔除统计 / 基座与派生日历末日）
QLIB_INCREMENT_LEDGER = Path(
    os.environ.get(
        "ETF_ROTATION_QLIB_INCREMENT_LEDGER",
        str(QLIB_DATASET_ROOT / "_increment_ledger.jsonl"),
    )
)

# =============================================================================
# 5.8 报告落盘（按需分析 ⇒ 报告回窗口）
# =============================================================================
#: **报告归档目录** —— 每次分析的 Markdown 报告都落这里，
#: 文件名带**分析日期**（`report_<YYYYMMDD>.md`）⇒ 便于回看与版本化。
#: 🔴 2026-11-10 铁律十六（落点双根化）：报告**也是「结果」** ⇒ 必须落
#:    **结果根** `<RESULTS_ROOT>/reports/`，**不许**再落仓库内 `manu/report/`
#:    （服务器上那是 `/tmp/pycharm_project_658d3fc4/manu/report` ⇒ 既不在项目
#:    专属根之下，又会把工程目录变成产物堆放点）。
#:    ⇒ 报告与快照 / canonical 同属结果根，**默认不回传 host**。
REPORT_DIR = Path(
    os.environ.get(
        "ETF_ROTATION_REPORT_DIR", str(ETF_ROTATION_RESULTS_ROOT / "reports")
    )
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


# =============================================================================
# 8. provider 路径对账（每日链 ↔ 研究链单一事实源）
# =============================================================================
#: 研究链根（`manu/etf_rotation`）—— provider 单一事实源 `qlib_provider.py` 在这儿
_RESEARCH_ROOT = Path(__file__).resolve().parent.parent


def cross_check_provider_paths() -> dict:
    """每日链 ↔ 研究链的 **provider 路径字面量对账**（防「第二个字面量」分叉）。

    背景（2026-11-09 实测）：每日链原本**自带一份** provider 选择规则，研究链又**另写一份**
    ⇒ 两条链会挑到**不同的数据集**（研究链停在基座末日，每日链用派生）⇒ 口径分叉。
    ⇒ 规则已提到研究链层 `manu/etf_rotation/qlib_provider.py`（单一事实源）；
    但**路径字面量**仍各有各的（`daily_config.QLIB_CN_DATA` vs 个股线 config 的 `PROVIDER_URI`）
    ⇒ 本函数把两边**逐条比对**，不一致 ⇒ `ok=False`（**由探针拦**，不静默放过）。

    返回：`{"ok": bool, "items": [{"name","daily","research","ok"}], "error": str|None}`
    """
    items: list[dict] = []
    error: str | None = None
    try:
        path = _RESEARCH_ROOT / "qlib_provider.py"
        spec = importlib.util.spec_from_file_location(
            "manu_daily_config_qlib_provider", path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 provider 模块：{path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec.name, None)
            raise
        pairs = [
            ("base", QLIB_CN_DATA, module.base_dir()),
            ("live", QLIB_CN_DATA_LIVE, module.live_dir()),
        ]
        for name, daily_path, research_path in pairs:
            items.append(
                {
                    "name": name,
                    "daily": str(daily_path),
                    "research": str(research_path),
                    "ok": str(daily_path) == str(research_path),
                }
            )
    except Exception as exc:  # noqa: BLE001 —— 对账失败本身也是「不一致」
        error = f"{type(exc).__name__}: {exc}"
    return {
        "ok": bool(items) and error is None and all(item["ok"] for item in items),
        "items": items,
        "error": error,
    }


def env_flag(name: str, default: bool = False) -> bool:
    """读环境变量布尔值（`1/true/yes/on` ⇒ True）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
