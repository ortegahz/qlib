"""agents_rebalance.py —— **统一流水线**的执行器（单一入口，无模式分诊）。

【2026-11-09 架构瘦身：三条按用途分诊的链 ⇒ 一条流水线 + 不可变快照】
旧形态有三条按用途分诊的链（日常巡检 / 调仓节点 / 影子推演）⇒ 三套分支、三套产物根、
三套语义 ⇒「忘了加 if」就会漏出新仓位指令，也会污染生产金标（历史事故）。
新形态：

* **一条流水线**：``run_pipeline()`` 走全部六个阶段，**没有模式分支**；
* **一个快照**：每次运行落 ``<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/``
  （自包含、只增不改 ⇒ 可以随时 diff 两份跑）；
* **一个副作用开关**：``live_trade=False``（**缺省**）⇒ 观察态（执行卡挂单禁用、不发信）；
  ``live_trade=True`` ⇒ 实盘态（执行卡可下单）。
* 🔴 **不写根目录全局文件**：canonical ``stock_scores.csv`` 只在「还没有任何快照时」
  当**一次性只读种子**用；每日链自成一链（底座 = **上一份快照**）。

【S 层口径（沿用 2026-11-05 核验结论）】
1. ``analyze.py --asof <信号日>`` 会被 ``data/signal_policy.py::check_signal_feasibility``
   的 **S1 硬拦**挡住（信号日必须落在打分相位栅格上）⇒ 分析日取「**最近一个可用的打分日**」。
2. ``qlib_scores.py`` 的落盘是**整体覆盖**、**不是增量** ⇒ 本模块让它跑在**临时目录**
   + 先备份 + 再按 ``(date, instrument)`` **合并**（keep=last）回**快照目录**。

【组合构造约束（研究链里没有的硬约束，本模块新增）】
* 目标资产**等权 5 份**（权益上限 80% ⇒ 每只 16%），单只标的**金额上限 6,000 元**；
* 买卖数量**向下取整**到 100 股（手）的整数倍；
* 调仓后**必须保留现金**：生效下限 = 「绝对下限 400 元」与「比例下限（总资产 × 20%）」**取大者**；
* **货基碎屑过滤**：场内货基（511880/511990 单价约 100 元/份，一手需 10,000 元）
  超出单标的 6,000 元限额 ⇒ **严禁下单**，自动归入可用现金躺平。

【铁律】
* 只读 ``daily_config.py`` / ``account_state.py``（常量与账户基准的**单一事实源**）；
* 所有数字**来自实际输入**，缺字段 ⇒ 该字段为 ``None`` 并在 ``warnings`` 里点名（**不编数字**）；
* 每一步外部命令失败 ⇒ **把原始输出原样抛出**（不吞错误、不静默降级）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import account_state  # noqa: E402
import daily_config as DC  # noqa: E402
import nav_state as nav_mod  # noqa: E402
import snapshot as snap_mod  # noqa: E402
import run_paths  # noqa: E402
import v2_frozen_params as V2FP  # noqa: E402

#: 研究链根目录（``manu/etf_rotation``）—— 导入研究链模块时按**绝对路径**加载
_RESEARCH_ROOT = Path(__file__).resolve().parents[1]
if str(_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_ROOT))

#: 执行卡固定文案
EXECUTION_BASIS = "信号日 T 收盘出信号 → 执行日 T+1 开盘执行"
PRICE_BASIS_NOTE = "节点日收盘价 ≠ 执行日成交价，实际股数/金额可能不同"

#: 各步骤的名字（报错时要说清是哪一步挂了）—— 对应**六阶段全流程**
STEP_SCORES = "1/8 qlib_scores.py（阶段2 S层重算）"
#: 🆕 **观察态水印 / 免责**（**单一事实源 = `snapshot.py`** ⇒ 报告 / 执行卡 / 终端提示
#:    共用同一句，不许各写各的）
OBSERVATION_WATERMARK = snap_mod.OBSERVATION_WATERMARK
OBSERVATION_DISCLAIMER = snap_mod.OBSERVATION_DISCLAIMER
STEP_RULES = "3b/8 recommend.py（阶段3b 规则层调仓建议）"
STEP_EXPORT_PRICES = "2/8 export_qlib_prices.py（阶段2 个股日线导出）"
STEP_ANALYZE = "3/8 analyze.py（阶段3 E=W×S 穿透 + 均线过滤）"

#: 🔴 2026-11-11（审计缺陷 C）：analyze.py 的 ``--top-n`` 决定报告 JSON 里
#:    ``candidates`` 的长度 ⇒ ``final_rebalance_plan`` 的**顺延池**只能看到前 N 名
#:    ⇒ 池内第 N+1 名以后**不可见** ⇒ 会误报「候选池里没有合格的顺延者」。
#:    🔴 报告 JSON **没有**「完整候选池」字段（只有 ``candidates``（= Top-N）与
#:    ``filter_stats.n_candidate_pool``（= 池子**总数**））⇒ 只能把 ``--top-n`` 调大。
#:    🔴 **数值的单一事实源 = 研究链 `config.ANALYZE_TOP_N`**（不在本文件另写一份）
#:    ⇒ 其余调用 analyze.py 的入口（研究链 / repro 重跑段）必须用同一个值，
#:    否则各链产出的报告不可比。
def _analyze_top_n() -> int:
    """顺延池所需的 ``--top-n``（= 研究链 `config.ANALYZE_TOP_N`；取不到 ⇒ 兜底 100）。"""
    try:
        return int(getattr(_research_config(), "ANALYZE_TOP_N", 100))
    except Exception:  # noqa: BLE001 —— 研究链 config 不可得 ⇒ 用兜底值（不静默失败）
        return 100
STEP_CONTEXT = "4/8 build_ta_context.py（阶段4 上下文）"
STEP_TRADINGAGENTS = "5/8 run_tradingagents.py（阶段4 多智能体辩论）"
STEP_PLAN = "6/8 final_rebalance_plan.py（阶段5 规则层对账）"
STEP_REPRICE = "7/8 reprice_node_day.py（阶段5 节点日重算）"
STEP_CARD = "8/8 执行卡组装（阶段6）"


# =============================================================================
# 通用小工具
# =============================================================================
def _load_by_path(name: str, path: Path) -> Any:
    """按**绝对路径**加载研究链模块。

    为什么不用 ``import config``：``daily/`` 与 ``etf_rotation/`` 同在 ``sys.path`` 上，
    同名模块会互相顶掉 ⇒ 一律走 importlib，**不依赖 sys.path 的命中顺序**。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"研究链模块加载失败：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # 🔴 必须先登记进 sys.modules 再 exec_module（@dataclass + future-annotations ⇒ dataclasses 会反查 sys.modules）
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _research_config() -> Any:
    """加载研究链 `config.py`（拿打分相位步长 / qlib 数据集路径 / 日历起点）。"""
    return _load_by_path("manu_etf_rotation_config", _RESEARCH_ROOT / "config.py")


def _signal_policy() -> Any:
    """加载 `data/signal_policy.py`（打分日栅格口径的**唯一事实源**）。"""
    return _load_by_path("manu_etf_rotation_signal_policy",
                         _RESEARCH_ROOT / "data" / "signal_policy.py")


def _f(value: Any) -> float | None:
    """把任意输入转成 float；转不了 / 非有限数 ⇒ ``None``（**不编数字**）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _i(value: Any) -> int | None:
    """把任意输入转成 int；转不了 ⇒ ``None``。"""
    number = _f(value)
    return round(number) if number is not None else None


def _now_iso() -> str:
    """本地时区的当前时间（秒级 ISO8601）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_prices(prices_df: pd.DataFrame) -> pd.DataFrame:
    """把行情表规范化成 ``code / date / close`` 三列（code 补零 6 位、按日期升序）。

    兼容列名：``code|instrument|symbol`` / ``date|datetime`` / ``close|price``。
    """
    df = pd.DataFrame(prices_df).copy()
    if df.empty:
        return pd.DataFrame(columns=["code", "date", "close"])
    lower = {str(c).lower(): c for c in df.columns}
    code_col = lower.get("code") or lower.get("instrument") or lower.get("symbol")
    date_col = lower.get("date") or lower.get("datetime")
    close_col = lower.get("close") or lower.get("price")
    missing = [
        name for name, col in (("code", code_col), ("date", date_col), ("close", close_col))
        if col is None
    ]
    if missing:
        raise ValueError(f"行情表缺少必需列 {missing}（实际列：{list(df.columns)}）")
    out = pd.DataFrame({
        "code": df[code_col].astype(str).str.strip().str.zfill(6),
        "date": pd.to_datetime(df[date_col], errors="coerce"),
        "close": pd.to_numeric(df[close_col], errors="coerce"),
    })
    out = out.dropna(subset=["date", "close"])
    return out.sort_values(["code", "date"]).reset_index(drop=True)


def _price_of(prices: Any, code: str) -> tuple[float | None, str | None]:
    """从 ``prices`` 里取某只标的的价格与价日期。

    兼容三种形态：``{code: price}`` / ``{code: (price, date)}`` /
    ``{code: {"price":…, "date":…}}``。
    """
    if not prices or code not in prices:
        return None, None
    item = prices[code]
    if isinstance(item, dict):
        return _f(item.get("price")), (str(item["date"]) if item.get("date") else None)
    if isinstance(item, (tuple, list)):
        price = _f(item[0]) if item else None
        date = str(item[1]) if len(item) > 1 and item[1] else None
        return price, date
    return _f(item), None


# =============================================================================
# A. MA 安全垫计算（MA20 / MA60）
# =============================================================================
def compute_ma_levels(
    prices_df: pd.DataFrame,
    code: str,
    *,
    ma_windows: tuple[int, ...] = (20, 60),
    asof: object = None,
    shares: int | None = None,
) -> dict:
    """从价格表算某只 ETF 的 MA20 / MA60。

    🔴 **禁未来函数**：只用 ``asof``（含）为止的数据 ⇒ ``asof`` 之后的极端价格
    **不得**影响本函数的任何输出。

    参数
    ----
    prices_df : 行情表（列 ``code / date / close``，兼容 ``instrument`` / ``price``）
    code      : 标的代码
    ma_windows: 要算的均线窗口（默认 20 / 60）
    asof      : 截止日（含）；``None`` 表示用全部数据
    shares    : 持仓股数（给了才能算**金额**口径的安全垫）

    返回
    ----
    dict，键：``code, close, close_date, ma20, ma60, above_ma20, above_ma60,
    distance_to_ma60_pct, safety_buffer_amount, safety_buffer_pct, n_obs``。

    🔴 **数据不足不许编数字**：样本不足 ``ma60`` 根 ⇒ ``ma60 = None``，
    ``n_obs`` 反映**实际**样本数。
    """
    target = str(code).strip().zfill(6)
    result: dict[str, Any] = {
        "code": target,
        "close": None,
        "close_date": None,
        "ma20": None,
        "ma60": None,
        "above_ma20": None,
        "above_ma60": None,
        "distance_to_ma60_pct": None,
        "safety_buffer_amount": None,
        "safety_buffer_pct": None,
        "n_obs": 0,
    }

    frame = _normalize_prices(prices_df)
    subset = frame[frame["code"] == target]
    if asof is not None:
        cutoff = pd.Timestamp(asof).normalize()
        subset = subset[subset["date"] <= cutoff]
    subset = subset.sort_values("date")
    if subset.empty:
        return result

    closes = subset["close"].to_numpy(dtype=float)
    n_obs = len(closes)
    result["n_obs"] = n_obs
    close = float(closes[-1])
    result["close"] = close
    result["close_date"] = pd.Timestamp(subset["date"].iloc[-1]).date().isoformat()

    mas: dict[int, float | None] = {}
    for window in ma_windows:
        width = int(window)
        mas[width] = float(np.mean(closes[-width:])) if n_obs >= width else None

    ma20 = mas.get(20)
    ma60 = mas.get(60)
    result["ma20"] = ma20
    result["ma60"] = ma60
    result["above_ma20"] = bool(close > ma20) if ma20 is not None else None
    result["above_ma60"] = bool(close > ma60) if ma60 is not None else None
    if ma60:
        distance = close / ma60 - 1.0
        result["distance_to_ma60_pct"] = distance
        result["safety_buffer_pct"] = distance
        if shares is not None:
            result["safety_buffer_amount"] = (close - ma60) * float(shares)
    return result


def _load_price_frame(
    prices_df: pd.DataFrame | None, prices_csv: str | Path | None
) -> tuple[pd.DataFrame, str | None]:
    """取行情表：给了 ``prices_df`` 直接用；否则读 CSV。

    显式给的路径**读不到 ⇒ 抛异常**（调用方的错）；
    缺省路径读不到 ⇒ 返回空表 + 说明（⇒ 降级为 ``insufficient_data``，不编数字）。
    """
    if prices_df is not None:
        return _normalize_prices(prices_df), None
    explicit = prices_csv is not None
    path = Path(prices_csv) if explicit else Path(DC.ETF_PRICES_CSV)
    if not path.exists():
        if explicit:
            raise FileNotFoundError(f"行情文件不存在：{path}（显式指定 ⇒ 拒绝编造数据）")
        return _normalize_prices(pd.DataFrame()), f"行情文件不存在：{path}"
    return _normalize_prices(pd.read_csv(path, dtype={"code": str})), None


def _resolve_account(account: Any) -> tuple[dict, list[str]]:
    """把账户输入归一成「账户字典 + 警告列表」。

    ``account`` 给 ``None`` ⇒ 用 ``account_state.account_summary()``（实盘基准的**单一事实源**）。
    """
    warnings: list[str] = []
    if account is None:
        try:
            account = account_state.account_summary()
        except Exception as exc:  # noqa: BLE001 —— 账户基准读不到就如实说
            warnings.append(f"账户基准读不到（{type(exc).__name__}: {exc}）⇒ 数值字段记为 None")
            account = {}
    if isinstance(account, pd.DataFrame):
        account = {"positions": account.to_dict(orient="records")}
    if not isinstance(account, dict):
        warnings.append(f"账户输入类型不认识（{type(account).__name__}）⇒ 数值字段记为 None")
        account = {}
    return account, warnings


def _account_block(account: dict) -> tuple[dict, list[str]]:
    """把账户字典归一成「总览字典 + 警告列表」。

    缺字段 ⇒ 该字段为 ``None`` 并在警告里点名（**不编数字**）。
    """
    warnings: list[str] = []
    total_assets = _f(account.get("total_assets"))
    cash = _f(account.get("cash"))
    market_value = _f(account.get("market_value"))
    if total_assets is None:
        warnings.append("账户缺 `total_assets` ⇒ 权重类字段记为 None")
    if cash is None:
        warnings.append("账户缺 `cash` ⇒ 现金相关字段记为 None")
    if market_value is None and total_assets is not None and cash is not None:
        market_value = total_assets - cash
    cash_weight = _f(account.get("cash_weight"))
    if cash_weight is None and cash is not None and total_assets:
        cash_weight = cash / total_assets
    block = {
        "snapshot_date": account.get("snapshot_date"),
        "total_assets": total_assets,
        "cash": cash,
        "cash_weight": cash_weight,
        "market_value": market_value,
        "market_value_weight": (
            market_value / total_assets if (market_value is not None and total_assets) else None
        ),
        "discrepancy_note": account.get("discrepancy_note"),
    }
    return block, warnings


def _primary_holding(account: dict) -> dict:
    """从账户里取唯一持仓（``PRIMARY_HOLDING_CODE``）的持仓事实。"""
    code = DC.PRIMARY_HOLDING_CODE
    for pos in account.get("positions") or []:
        if str(pos.get("code", "")).strip().zfill(6) == code:
            return {
                "code": code,
                "name": str(pos.get("name") or DC.PRIMARY_HOLDING_NAME),
                "shares": _i(pos.get("shares")),
                "cost_price": _f(pos.get("cost_price"))
                if _f(pos.get("cost_price")) is not None
                else (
                    (_f(pos.get("cost")) / _f(pos.get("shares")))
                    if (_f(pos.get("cost")) is not None and _f(pos.get("shares")))
                    else None
                ),
                "price": _f(pos.get("price")),
                "market_value": _f(pos.get("market_value")),
                "pnl_pct": _f(pos.get("pnl_pct")),
            }
    return {
        "code": code,
        "name": DC.PRIMARY_HOLDING_NAME,
        "shares": _i(DC.PRIMARY_HOLDING_SHARES),
        "cost_price": _f(DC.PRIMARY_HOLDING_COST),
        "price": None,
        "market_value": _f(DC.MARKET_VALUE),
        "pnl_pct": None,
    }


def _check_holding_discipline(*, asof: str, prices_csv: str | Path | None,
                              account: Any, notes: list[str]) -> dict:
    """**持仓纪律检查**：唯一持仓的现价 vs MA60（安全垫）。

    🔴 为什么保留这一步：旧形态里这条检查住在「日常巡检」路径 ⇒ 该路径被
    废除后它会**一起消失** ⇒ 唯一持仓的止损纪律就没人守了。
    ⇒ 现在它是**统一流水线里的一个环节**：只**披露 + 告警**，**不自动下单**
    （真要紧急平仓，仍需人在实盘态确认）。

    返回 ``{"status", "code", "close", "ma60", "distance_pct", "action"}``；
    ``status`` ∈ ``ok`` / ``broken`` / ``insufficient_data``。
    """
    day = run_paths.normalize_date(asof) or str(asof)
    effective_account, acc_warnings = _resolve_account(account)
    notes.extend(acc_warnings)
    holding = _primary_holding(effective_account)

    frame, price_note = _load_price_frame(None, prices_csv)
    if price_note:
        notes.append(f"⚠️ {price_note} ⇒ 持仓纪律检查走 insufficient_data")

    ma = compute_ma_levels(
        frame, DC.PRIMARY_HOLDING_CODE,
        ma_windows=(DC.ENTRY_MA_WINDOW, DC.EXIT_MA_WINDOW),
        asof=day, shares=holding.get("shares"),
    )
    close = ma["close"]
    ma60 = ma["ma60"]
    action: dict | None = None

    if close is None or ma60 is None:
        status = "insufficient_data"
        notes.append(
            f"⚠️ **样本不足**：MA{DC.EXIT_MA_WINDOW} 需要 {DC.EXIT_MA_WINDOW} 根收盘价，"
            f"实际只有 {ma['n_obs']} 根 ⇒ **不做破位判定**（不编数字），按「趋势完好」披露"
        )
        return {
            "status": status, "code": DC.PRIMARY_HOLDING_CODE, "close": close,
            "ma60": ma60, "distance_pct": None, "action": None,
        }

    distance = ma["distance_to_ma60_pct"]
    if close < ma60 * (1.0 - float(DC.BREAK_CONFIRM_RATIO)):
        status = "broken"
        action = {
            "action": "紧急平仓",
            "kind": "emergency_liquidation",
            "code": DC.PRIMARY_HOLDING_CODE,
            "name": holding.get("name") or DC.PRIMARY_HOLDING_NAME,
            "shares": holding.get("shares"),
            "trigger_price": close,
            "ma60": ma60,
            "break_pct": distance,
            "reason": (
                f"收盘价 {close:.4f} 有效跌破 MA{DC.EXIT_MA_WINDOW}（{ma60:.4f}），"
                f"跌破幅度 {distance:.2%} ⇒ 触发持仓止损纪律"
            ),
            "instruction": (
                f"下一交易时段清掉 {holding.get('shares')} 股 "
                f"{holding.get('name') or DC.PRIMARY_HOLDING_NAME}"
                f"（参考价 {close:.4f} 元/股）；🔴 本卡**不自动下单**，需人工确认"
            ),
        }
        notes.append(
            f"🔴 **紧急避险**：{DC.PRIMARY_HOLDING_CODE} 收盘 {close:.4f} < "
            f"MA{DC.EXIT_MA_WINDOW} {ma60:.4f}（{distance:+.2%}）⇒ 已生成紧急平仓告警"
            "（**不自动下单**）"
        )
    else:
        status = "ok"
        notes.append(
            f"✅ **趋势完好**：收盘 {close:.4f} 高于 MA{DC.EXIT_MA_WINDOW} {ma60:.4f}"
            f"（安全垫 {distance:+.2%}）"
        )
    return {
        "status": status, "code": DC.PRIMARY_HOLDING_CODE, "close": close,
        "ma60": ma60, "distance_pct": distance, "action": action,
    }


# =============================================================================
# B. 统一流水线（六阶段全流程 → 不可变快照）


# =============================================================================
# B. 研究链全链 → 确定性执行卡
# =============================================================================
def _run_cmd(cmd: list, *, step: str, timeout: float | None = None,
             env: dict[str, str] | None = None) -> str:
    """跑一个外部步骤；失败 ⇒ 把**原始输出原样抛出**（不吞错误、不静默降级）。

    「每一步都要能单独失败」是本链的硬要求：哪一步挂了，邮件里必须能一眼看出是哪一步、
    原始报错是什么 ⇒ 这里把 stdout / stderr 原样带进异常消息。
    """
    printable = " ".join(str(c) for c in cmd)
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"第 {step} 步的解释器/脚本不存在：{printable}（{exc}）⇒ 拒绝继续"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"第 {step} 步超时（>{timeout} 秒）：{printable}\n"
            f"--- 部分 stdout ---\n{exc.stdout or ''}\n--- 部分 stderr ---\n{exc.stderr or ''}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"第 {step} 步失败（退出码 {proc.returncode}）\n"
            f"命令：{printable}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return f"{proc.stdout}\n{proc.stderr}"


def _scores_max_date(scores_path: Path) -> str | None:
    """读个股分数文件的**最大日期**（只读现成产物，**不重跑打分**）。"""
    if not scores_path.exists():
        return None
    try:
        df = pd.read_csv(scores_path, usecols=["date"])
    except Exception:  # noqa: BLE001 —— 读不到就如实返回 None
        return None
    if df.empty:
        return None
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return pd.Timestamp(dates.max()).date().isoformat()


#: 🔴 **分数指纹旁挂文件**名（与 ``qlib_scores.py`` 写出的同名）。
#:   单一事实源在 ``qlib_scores.py::SCORES_FINGERPRINT_NAME``（两边必须同名，
#:   否则守卫会因为「读不到指纹」而永远判定为「不可增量」⇒ 白跑全量）。
SCORES_FINGERPRINT_NAME = "stock_scores.fingerprint.json"


def _scores_fingerprint(scores_path: Path) -> dict | None:
    """读个股分数的**模型指纹**（旁挂文件）⇒ 读不到返回 ``None``。"""
    fp = Path(scores_path).parent / SCORES_FINGERPRINT_NAME
    if not fp.is_file():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 指纹坏掉 = 读不到 = 不可增量
        return None


def _current_model_fingerprint() -> dict | None:
    """问 S 层脚本要「**当前**生产模型的指纹」（单一事实源 = 它的默认模型目录）。

    🔴 不许在本文件另写一份生产模型路径常量（两处漂移 = 守卫失信）：
    ⇒ 直接问 ``qlib_scores.py --print-prod-fingerprint``。
    """
    try:
        out = _run_cmd(
            [
                DC.QL_PY,
                str(_RESEARCH_ROOT / "qlib_scores.py"),
                "--print-prod-fingerprint",
            ],
            step=STEP_SCORES,
        )
    except Exception as exc:  # noqa: BLE001 —— 问不到 ⇒ 判定为「不可增量」
        print(f"[WARN] 读不到当前生产模型指纹：{type(exc).__name__}: {exc}")
        return None
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return None


def _fingerprint_matches(base_fp: dict | None, cur_fp: dict | None) -> bool:
    """底座指纹与当前模型指纹**是不是同一套**。

    🔴 读不到任一侧 ⇒ **判为不匹配**（fail-closed）：宁可多跑一次全量，
    也不许把两套模型的分数拼进一份文件。
    """
    if not base_fp or not cur_fp:
        return False
    return (
        base_fp.get("model_md5") == cur_fp.get("model_md5")
        and base_fp.get("fit_start") == cur_fp.get("fit_start")
        and base_fp.get("fit_end") == cur_fp.get("fit_end")
    )


def _read_calendar(policy: Any, cal_path: Path) -> tuple[list[Any], str | None] | None:
    """读 qlib 交易日历；读不到 / 解析不出 ⇒ 返回 ``None``（由调用方换下一个候选）。"""
    try:
        cal = policy.read_calendar_dates(cal_path)
        dataset_max = policy.calendar_max_date(cal_path)
    except Exception:  # noqa: BLE001 —— 日历读不到就如实降级，不编栅格
        return None
    return cal, dataset_max


def _score_grid(calendar_path: str | Path | None = None) -> tuple[list[str] | None, str | None]:
    """算**打分日栅格**（相位口径来自 ``data/signal_policy.py``，唯一事实源）。

    🔴 **相位只能以 qlib 日历为准**：相位 = 交易日历**下标** ``i % every_n == every_n - 1``，
    下标依赖日历的起点 ⇒ 换一个日历（比如含未来日期的 akshare 全量日历）算出来的
    相位就是**错的**（会得出「某天该打分」的错误结论）。
    ⇒ 本函数**优先**用 qlib 数据集的 ``calendars/day.txt``；
    ``calendar_path`` 只在 qlib 日历缺失时作兜底。

    返回 ``(栅格日期列表 | None, qlib 数据集覆盖末日 | None)``。
    栅格算不出来（配置/日历读不到）⇒ 返回 ``(None, None)``，由调用方如实披露。
    """
    try:
        rcfg = _research_config()
        policy = _signal_policy()
    except Exception:  # noqa: BLE001
        return None, None
    every_n = int(getattr(rcfg, "REBALANCE_EVERY_N_DAYS", 10))
    start = str(getattr(rcfg, "SCORE_START_TIME", "") or "")
    provider_uri = str(getattr(rcfg, "PROVIDER_URI", "") or "")
    # 🔴🔴 **先读「选定的 provider」**（2026-11-06 09:2x 修复）：阶段 1 的「自建增量日」把
    #    派生目录 `cn_data_live` 补到了信号日之后 ⇒ 但本函数原来只读**上游基座**
    #    `C.PROVIDER_URI` ⇒ **派生目录对它完全不可见** ⇒ 栅格的「末日例外」停在基座末日
    #    ⇒ 分析日退到基座末日、披露也说「数据集覆盖末日 = 基座末日」⇒ **保守失真**。
    #    ⇒ 现在与打分刷新链（`run_qlib_score.select_provider`）**同源**：谁新用谁。
    candidates: list[Path] = []
    try:
        import run_qlib_score as _RS  # 同目录，避免循环导入（run_qlib_score 不依赖本模块）

        provider, _reason = _RS.select_provider()
        candidates.append(Path(provider) / "calendars" / "day.txt")
    except Exception:  # noqa: BLE001 - 退回基座口径
        pass
    if provider_uri:
        candidates.append(Path(provider_uri) / "calendars" / "day.txt")
    if calendar_path:
        candidates.append(Path(calendar_path))
    for cal_path in candidates:
        if not cal_path.exists():
            continue
        parsed = _read_calendar(policy, cal_path)
        if parsed is None:
            continue
        cal, dataset_max = parsed
        if not cal:
            continue
        if start:
            cal = [d for d in cal if d.isoformat() >= start]
        grid = policy.score_grid_dates(cal, every_n, start=start or None, end=None)
        return grid, dataset_max
    return None, None


def _resolve_analysis_asof(*, signal_date: str, scores_path: Path,
                           calendar_path: str | Path | None = None) -> dict:
    """决定 ``analyze.py`` 该用哪个 ``--asof``（= 最近一个**可用**打分日）。

    🔴 主 agent 2026-11-05 核验：``analyze.py --asof <信号日>`` 会被
    ``data/signal_policy.py::check_signal_feasibility`` 的 **S1 硬拦**挡住
    （信号日必须落在打分相位栅格上）⇒ 分析日**必须**落在栅格上。
    ⇒ 本函数取 **min(分数文件最大日期, 栅格里 ≤ 信号日的最后一天)**（保守取交集）。
    """
    notes: list[str] = []
    scores_max = _scores_max_date(scores_path)
    grid, dataset_max = _score_grid(calendar_path)
    candidates = [d for d in (grid or []) if d <= signal_date]
    grid_last = candidates[-1] if candidates else None

    if scores_max and grid_last:
        analysis_asof = min(scores_max, grid_last)
    elif scores_max:
        analysis_asof = scores_max
    elif grid_last:
        analysis_asof = grid_last
    else:
        raise RuntimeError(
            f"既读不到个股分数文件（{scores_path}）也算不出打分栅格"
            f"（qlib 日历不可用）⇒ 无法确定分析日 ⇒ 拒绝继续（不编日期）"
        )

    if scores_max is None:
        notes.append(f"⚠️ 个股分数文件读不到（{scores_path}）⇒ 分析日取自打分栅格")
    else:
        notes.append(
            f"ℹ️ 个股分数 S：{scores_path}，最大日期 {scores_max}"
        )
    if grid_last is None and grid is not None:
        notes.append("⚠️ 打分栅格里没有 ≤ 信号日的日期 ⇒ 分析日退回分数文件最大日期")

    if analysis_asof < signal_date:
        data_covers = bool(dataset_max) and dataset_max >= signal_date
        if data_covers:
            # 🔴 数据集（派生目录）**已经覆盖到信号日** ⇒ 差的是「S 层没重跑」，不是「数据不够」
            advice = (
                "⇒ **数据已够**（派生目录已覆盖到信号日）⇒ **重跑阶段 2 即可**"
                "（`qlib_scores --end <信号日> --provider-uri <派生目录>`）；"
                "本轮**节点日只重算股数/金额**。"
            )
        else:
            advice = (
                "⇒ **信号日当天的个股 Alpha 拿不到**（数据集没覆盖到信号日 ⇒ "
                "阶段 2 只能打到数据集末日）。本轮**节点日只重算股数/金额**。"
                "要把打分推到信号日，必须先让**阶段 1** 把 qlib 数据集刷新到信号日之后"
                "（滚动快照换库 ＋ **自建增量日**兜底），再重跑阶段 2。"
            )
        notes.append(
            f"🔴 **S 层打分日 = {analysis_asof} ≠ 信号日 {signal_date}**"
            + (f"（打分栅格里 ≤ 信号日的最后一天 = {grid_last}）" if grid_last else "")
            + (f"；当前 qlib 数据集覆盖末日 = {dataset_max}" if dataset_max else "")
            + " " + advice
        )
    else:
        notes.append(
            f"✅ 分析日 = 信号日 {analysis_asof}（**S 层已覆盖到信号日**，"
            "E=W×S 穿透用的是信号日当天的个股 Alpha）"
        )

    return {
        "analysis_asof": analysis_asof,
        "scores_max_date": scores_max,
        "grid_last": grid_last,
        "dataset_max_date": dataset_max,
        "notes": notes,
    }


def _export_stock_prices(analysis_asof: str, notes: list[str],
                         *, out_dir: str | Path) -> Path | None:
    """**阶段 2b**：导出全市场个股日线（供 ``analyze.py`` 的多日 RankIC 用）。

    🔴 为什么必须放在 S 层刷新**之后**：``analyze.py`` 的 N=5 / N=20 交易日 RankIC
    需要「个股前瞻收益」，价格源是 ``stock_prices.csv``；若 S 层刚重算而价格没重导，
    RankIC 会拿**旧价**去配**新分数** ⇒ 口径错配。

    🔴 **只落快照目录**（``out_dir`` = 本轮快照）⇒ 每日链**不写**研究链产物根。
    """
    out = Path(out_dir) / "stock_prices.csv"
    try:
        _run_cmd([
            DC.QL_PY, str(_RESEARCH_ROOT / "data" / "export_qlib_prices.py"),
            "--asof", str(analysis_asof),
            "--out", str(out),
        ], step=STEP_EXPORT_PRICES)
    except Exception as exc:
        notes.append(
            f"⚠️ 个股日线导出失败（**不阻断**主链）：{type(exc).__name__}: {exc}"
            " ⇒ analyze.py 的多日 RankIC 会退化为『样本不足』"
        )
        return None
    notes.append(f"✅ 个股日线已导出（阶段 2b）：{out}（截止 {analysis_asof}）")
    return out


def _refresh_scores(*, scores_path: Path, notes: list[str],
                    end_date: str | None = None) -> None:
    """重跑 S 层（**阶段 2**：全市场个股 Alpha 推断）⇒ **只写快照目录**。

    🔴 ``qlib_scores.py`` 的落盘是 ``scores.to_csv(out_dir/"stock_scores.csv")``
    ⇒ **整体覆盖**、**不是增量** ⇒ 必须「临时目录跑 → 与底座合并 → 写回目标」。

    🔴 **底座口径（链式）**：``scores_path`` 在调用前已经放好底座
       （**上一份快照**的分数，或 canonical 的**一次性只读种子**）
       ⇒ ``start`` 有值 ⇒ **增量**重算 ⇒ 打分窗口与历史一致（不会每轮从 2024 重算）。

    🔴 **canonical 永不被写**：本函数只**读** canonical（算 md5 前后对照做证据），
       只写 ``scores_path``（本轮快照内）。

    🔴 **模型一致守卫（2026-11-19 新增）**：底座分数是**哪套权重**算出来的？
       换了 S 层权重 ⇒ 归一化统计量变了 ⇒ **全部分数都变了**
       ⇒ 拿旧权重的底座增量拼接 = **一份分数里混两套模型** ⇒ 静默劣化。
       ⇒ 指纹（``stock_scores.fingerprint.json``）对不上 ⇒ **整段重算**，
       且**旧底座不并入**（直接丢弃）；指纹读不到 ⇒ 一律判「不可增量」（fail-closed）。
    """
    import hashlib

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = scores_path.parent / f"_scores_refresh_{stamp}"
    backup = scores_path.parent / f"{scores_path.name}.bak-{stamp}"
    canonical = Path(DC.STOCK_SCORES_CSV)
    before_canonical = (
        hashlib.md5(canonical.read_bytes()).hexdigest() if canonical.is_file() else None
    )

    # 🔴 **模型一致守卫**（2026-11-19 新增）：底座分数是**哪套权重**算出来的？
    #   换了 S 层权重 ⇒ 归一化统计量变了 ⇒ **全部分数都变了**
    #   ⇒ 拿旧权重的底座做增量拼接 = 一份分数里混着两套模型 ⇒ 静默劣化。
    #   ⇒ 指纹对不上 ⇒ **整段作废、必须全量重算**（不许增量拼接）。
    base_fp = _scores_fingerprint(scores_path) if scores_path.exists() else None
    cur_fp = _current_model_fingerprint()
    same_model = _fingerprint_matches(base_fp, cur_fp)
    start = _scores_max_date(scores_path) if scores_path.exists() else None
    if not same_model:
        start = None
        notes.append(
            "🔴 **S 层权重已换 ⇒ 底座分数整段作废、必须全量重算**"
            f"（底座指纹 = {json.dumps(base_fp, ensure_ascii=False) if base_fp else '**缺失**'}；"
            f"当前模型指纹 = {json.dumps(cur_fp, ensure_ascii=False) if cur_fp else '**读不到**'}）"
            " ⇒ **不许**把两套模型的分数拼进一份文件"
        )
    print("[WARN] 🔴 即将重跑 S 层：这一步约 27 分钟，属**重活**（铁律一：需用户当轮授权）。")
    notes.append(
        "🔴 **已重跑 S 层**（阶段 2，约 27 分钟的重活）：跑在临时目录 → "
        + ("以**同源底座**增量重算 → 合并落**本快照**" if same_model
           else "**全量重算**（底座整段作废）→ 落**本快照**")
        + "（🔴 **canonical `stock_scores.csv` 零改动**）"
        + (f"；打分截止日 = {end_date}" if end_date else "")
        + (f"；底座最新打分日 = {start}" if start else "；**无底座 ⇒ 全量重算**")
    )
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cmd = [DC.QL_PY, str(_RESEARCH_ROOT / "qlib_scores.py"), "--mode", "infer",
           "--out-dir", str(tmp_dir)]
    # 🔴 **必须显式带 `--provider-uri`**：否则 `qlib_scores` 用**上游基座**的默认
    #    `C.PROVIDER_URI` ⇒ 派生目录 `cn_data_live` 里已补到信号日的数据**用不上**
    #    ⇒ S 层会停在基座末日 ⇒ 拿不到新鲜 Alpha。
    try:
        import run_qlib_score as _RS

        _provider, _reason = _RS.select_provider(end_date)
        cmd += ["--provider-uri", str(_provider)]
        notes.append(f"ℹ️ S 层打分数据源（provider-uri）= {_provider}（{_reason}）")
    except Exception as exc:  # noqa: BLE001 - 退回默认口径
        notes.append(f"⚠️ provider-uri 解析失败（退回默认基座）：{type(exc).__name__}: {exc}")
    if start:
        cmd += ["--start", start]
    if end_date:
        cmd += ["--end", str(end_date)]
    _run_cmd(cmd, step=STEP_SCORES)
    produced = tmp_dir / "stock_scores.csv"
    if not produced.exists():
        raise FileNotFoundError(f"重跑 S 层后产物不存在：{produced}")
    # 🔴 **合并（不是覆盖）**：`qlib_scores.py` 的落盘是**整体覆盖**
    #    ⇒ 只含「本次新增段」的产物若直接写回，会把历史打分日**全部抹掉**。
    #    ⇒ 统一走「旧 + 新 ⇒ 按 (date, instrument) 去重 keep=last ⇒ 写回」。
    if scores_path.exists():
        shutil.copy2(scores_path, backup)
        notes.append(f"ℹ️ 快照内底座分数已备份到 {backup}")
    old = (
        pd.read_csv(scores_path, dtype={"instrument": str})
        if scores_path.exists() and same_model else None
    )
    if scores_path.exists() and not same_model:
        notes.append(
            "🔴 底座分数**不并入**本次结果（它是**另一套权重**算出来的 ⇒ 整段丢弃）"
        )
    new = pd.read_csv(produced, dtype={"instrument": str})
    merged = pd.concat([old, new], ignore_index=True) if old is not None else new
    merged["date"] = pd.to_datetime(merged["date"])
    merged = merged.drop_duplicates(subset=["date", "instrument"], keep="last")
    merged = merged.sort_values(["date", "instrument"]).reset_index(drop=True)
    merged.to_csv(scores_path, index=False)
    # 🔴 **指纹旁挂文件**跟着走（否则下一轮读不到指纹 ⇒ 守卫会永远判「不可增量」）
    produced_fp = tmp_dir / SCORES_FINGERPRINT_NAME
    if produced_fp.is_file():
        shutil.copy2(produced_fp, scores_path.parent / SCORES_FINGERPRINT_NAME)
        notes.append(f"ℹ️ 分数指纹已写入：{scores_path.parent / SCORES_FINGERPRINT_NAME}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    new_max = _scores_max_date(scores_path)
    after_canonical = (
        hashlib.md5(canonical.read_bytes()).hexdigest() if canonical.is_file() else None
    )
    notes.append(
        f"✅ S 层已合并落**本快照**：{scores_path}"
        f"（底座 {0 if old is None else len(old)} 行 + 新增 {len(new)} 行 = {len(merged)} 行）"
        f"｜最新打分日 = {new_max or '—'}"
        f"｜canonical md5 {before_canonical} → {after_canonical}"
        f"（{'**未变 ✅**' if before_canonical == after_canonical else '**变了 ❌ 污染！**'}）"
    )

def _env_with_dotenv(ta_dir: Path, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """把 TradingAgents 的 ``.env`` 载入环境变量后返回环境（供子进程使用）。

    🔴 必须在 **import tradingagents 之前**加载：``tradingagents.default_config``
    在 import 期就把 ``TRADINGAGENTS_*`` 固化进 ``DEFAULT_CONFIG`` ⇒ 晚加载无效。
    （``run_tradingagents.py`` 内部自己也 load 一次，这里是**父进程侧**的同口径兜底。）

    🔴 **合并顺序**（``overrides`` 在最后 ⇒ 优先级最高）：
        ``.env``（只填空位） < **进程已有 env** < ``overrides``（本次显式指定）
    ⇒ 例：``--reasoning-effort medium`` ⇒ 子进程 env 里
    ``TRADINGAGENTS_REASONING_EFFORT=medium`` ⇒ 盖住 ``.env`` 里现役的 ``max``。
    """
    env = dict(os.environ)
    env_path = Path(ta_dir) / ".env"
    if not env_path.exists():
        env.update(overrides or {})
        return env
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        env = dict(os.environ)
    except ImportError:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env.setdefault(key.strip(), value.strip().strip("'\""))
    env.update(overrides or {})
    return env


def _resolve_llm_timeout(llm_timeout: float | None) -> float:
    """阶段 4 的超时上限（秒）：``None`` ⇒ 读 ``daily_config.LLM_TIMEOUT``（默认 5400）。

    🔴 2026-11-05：原默认值写死 **1800** ⇒ MiniMax ``reasoning_effort=max``
    时单次调用实测 185 秒、多智能体图要调十几次 ⇒ 阶段 4 必然超时
    （实测 15:52 起跑 → 16:22 被 1800 秒打断）⇒ 超时**必须可配**且默认够用。
    """
    return float(DC.LLM_TIMEOUT if llm_timeout is None else llm_timeout)


def _holdings_arg(account: dict) -> str:
    """把账户持仓归成 ``recommend.py --holdings`` 要的 ``code:weight`` 串。

    🔴 用**实盘口径**（``account_state``）⇒ 规则层与交付层（执行卡）**同一账户**。
    """
    total = _f(account.get("total_assets"))
    parts: list[str] = []
    for pos in account.get("positions") or []:
        code = str(pos.get("code") or "").strip()
        market_value = _f(pos.get("market_value"))
        if not code or market_value is None or not total:
            continue
        parts.append(f"{code}:{market_value / total:.6f}")
    return ",".join(parts)


def _smoothing_args() -> list[str]:
    """平滑参数（**单一事实源 = 研究链 config.py**）⇒ 分析链与规则层**同一组**。

    🔴 少传一条 ⇒ 两链分叉 ⇒ 交付与规则选出不同的票（且**不报错**）。
    """
    cfg = _research_config()
    return [
        "--smoothing-method", str(getattr(cfg, "SMOOTHING_METHOD", "ewma")),
        "--smoothing-window", str(getattr(cfg, "SMOOTHING_WINDOW", 5)),
        "--smoothing-alpha", str(getattr(cfg, "SMOOTHING_ALPHA", 0.20)),  # 🔴 文档口径 0.20
        "--smoothing-history-days", str(getattr(cfg, "SMOOTHING_HISTORY_SPAN_DAYS", 30)),
    ]

def _load_v2_frozen_params():
    """🔴 读 V2 冻结件（**fail-closed**）⇒ 每日链 ETF 层参数的**唯一来源**。

    🔴 **为什么必须 fail-closed**：接缝开着却读不到冻结件时若静默回落 V1 常量，
       等于「V2 的冠军参数又没进每日链」而且**不会报错** ⇒ 老坑复发且无痕。
    ⇒ 读不到 / 口径不符 ⇒ **抛异常中止**（宁可不产出，不许产出错口径的交付件）。
    """
    if not DC.V2_FROZEN_PARAMS_ENABLED:
        return None
    return V2FP.load_frozen_params(
        DC.V2_FROZEN_PARAMS_FILE, required=DC.V2_FROZEN_PARAMS_REQUIRED
    )


def _etf_layer_args() -> tuple[list[str], dict]:
    """🔴 ETF 层参数（**分析链 ``analyze.py`` 与规则链 ``recommend.py`` 同一组**）。

    🔴 **接缝开着** ⇒ 参数来自 **V2 冻结件**（``frozen_params_wf.json``）：
    纯 E（alpha=1.0 / momentum=0.0）、**关掉** V1 的 E 层 EWMA（``--no-smoothing``）、
    **关掉** V1 的 MA60 入场/退出闸门（``--entry-ma-window 0 / --exit-ma-window 0``）、
    ``top_k`` / ``hold_buffer`` / 同族上限全按冻结件。
    🔴 **接缝关着** ⇒ 回落 V1 老口径（平滑参数取研究链 config，MA 取 ``daily_config``）。

    🔴 **两链必须逐字相同**：少传一条 ⇒ 分析链与规则层分叉 ⇒ 交付件与规则选出不同的票
    （历史上真出过「规则层 11-03 无平滑 vs 交付层 11-05 有平滑」的分叉）。
    """
    fp = _load_v2_frozen_params()
    if fp is None:
        cfg = _research_config()
        args = _smoothing_args() + [
            "--entry-ma-window", str(DC.ENTRY_MA_WINDOW),
            "--exit-ma-window", str(DC.EXIT_MA_WINDOW),
        ]
        return args, {
            "seam": "🔴 **关闭**（回落 V1 老配置：E 层 EWMA + MA60 闸门）",
            "enabled": False,
        }

    # ---- 口径守卫（fail-closed）----
    cfg = _research_config()
    deployed = int(getattr(cfg, "DEPLOYED_REBALANCE_EVERY_N_DAYS", 0) or 0)
    if deployed and deployed != int(fp.rebalance_every_n_days):
        raise V2FP.V2FrozenParamsError(
            f"🔴 V2 冻结件 cadence = {fp.rebalance_every_n_days}，"
            f"每日链部署节奏 = {deployed} ⇒ 两链口径不一致 ⇒ 拒绝跑"
        )
    account_positions = int(getattr(DC, "TARGET_POSITIONS", 0) or 0)
    if account_positions and account_positions != int(fp.top_k):
        raise V2FP.V2FrozenParamsError(
            f"🔴 V2 冻结件 top_k = {fp.top_k}，每日链账户目标持仓数 = {account_positions}"
            " ⇒ 两链口径不一致 ⇒ 拒绝跑"
        )
    return fp.etf_layer_args(), fp.disclosure()


def _sha1_of_file(path: Path) -> str:
    import hashlib

    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _contexts_equivalent(a: Path, b: Path) -> bool:
    """两个 `ta_research_context.md` **内容是否等价**。

    🔴 2026-11-11：context 正文末行嵌了**本快照的绝对路径**
       ⇒ 同一个数据源在两个快照里生成的 context **只差这一行** ⇒ sha1 必然不同。
    ⇒ 比对时**只把各自的快照目录归一化成占位符**，其余**逐字**比。
    ⚠️ 归一化**只限「自己的快照目录」这一段** ⇒ 任何其它差异（分数变了、结论变了……）
       都判不等价。
    """

    def _norm(path: Path) -> str:
        txt = Path(path).read_text(encoding="utf-8")
        return txt.replace(str(Path(path).absolute().parent), "<SNAPSHOT>")

    try:
        return _norm(a) == _norm(b)
    except OSError:
        return False


def _carry_context_trio(src_snap: Path, dst_snap: Path, cur_context: Path,
                        decision_json: Path | None = None) -> tuple[bool, str]:
    """把**来源快照**的 `ta_research_context.md` 搬到本快照（跨快照复用决策件的必要条件）。

    🔴 2026-11-11 修（**跨快照复用决策件 ⇒ sha1 链断裂 ⇒ 闸门 4 阶段假红**）：
       决策件记录的是**它当初吃到的 context** 的 sha1（`meta.context_sha1`）；
       闸门（`verify_artifacts` 第二环 / `probe_llm_stage_repro` P4）校验的是
       「**本快照** context md 的 sha1 == 决策件记录的 context_sha1」。
       ⇒ 只复制决策件（不复制它那份 context）⇒ 链必然断
       ⇒ 交付件被判「LLM 意见已过期」⇒
         `verify_artifacts` / `probe_verify_guards` / `probe_llm_stage_repro` /
         `probe_reproducibility` **4 个阶段假红**（实测快照 `20261111_161126`）。
       ⇒ 修法：**上下文跟着决策件走**（`final_rebalance_plan.py` 早写明这条不变量）。

    🔴 **只搬 context md，不搬 `ta_context_meta.json`**：
       meta 记的是「**本快照**这份报告 → 本快照这份上下文」的链接
       （`report_md_sha1` / `report_content_sha1` / `recommendation_sha1`），
       必须留在本快照 ⇒ 搬来源的会与本快照报告对不上（**第一环**反而断）。
       ⚠️ 报告内容指纹本身**含快照路径**（`current_holdings_diagnosis.snapshot_path`
          在报告**正文**里，不在易变 meta 里）⇒ **跨快照必然不等**
          ⇒ 跨快照的「数据没变」判定**不能**用它 ⇒ 改用**上下文逐字等价**
          （context 由报告生成 ⇒ context 等价 ⇔ 报告等价）。

    ⚠️ 判定三条全过才搬：
      ① 来源 context 存在；
      ② sha1(来源 context) == 来源决策件 `meta.context_sha1`（它自己自洽）；
      ③ 来源 context 与**本快照现算的** context **逐字等价**（仅路径行不同）
         ⇒ 证明来源决策件基于**本轮这份数据**。

    🔴 2026-11-11（审计 P1-2）：③ 必须是 **fail-closed**。
       旧写法 `if Path(cur_context).is_file() and not _contexts_equivalent(...)`：
       **本轮 context 文件不存在**时条件直接为假 ⇒ 跳过 ③ ⇒ 在**没有「本轮数据没变」证明**
       的情况下把来源 context 搬过来（**fail-open**）⇒ 来源决策件可能是**别的数据**的
       ⇒ sha1 链看似接上、实则结论过期。
       ⇒ 现在：**本轮 context 缺失 ⇒ 不搬**（宁可让链保持断口 ⇒ 闸门红 ⇒ 需人工介入）。
       ⚠️ 现网该分支**不可达**（`build_ta_context.py` 先跑，失败即抛 `RuntimeError`
          ⇒ 本轮 context 必然存在）⇒ 本修复**不改变已交付快照的行为**。
    """
    src_ctx = Path(src_snap) / "ta_research_context.md"
    if not src_ctx.is_file():
        return False, "来源快照缺 ta_research_context.md ⇒ 无从让 sha1 链接上 ⇒ 不搬"
    if decision_json is not None and Path(decision_json).is_file():
        try:
            _d = json.loads(Path(decision_json).read_text(encoding="utf-8"))
            _rec = str(((_d.get("meta") or {}).get("context_sha1")) or "")
        except Exception as exc:  # noqa: BLE001
            return False, f"来源决策件不可解析（{type(exc).__name__}: {exc}）⇒ 不搬"
        if not _rec:
            return False, "来源决策件没有 meta.context_sha1 ⇒ 无从证明它吃的是哪份 context ⇒ 不搬"
        _actual = _sha1_of_file(src_ctx)
        if _actual != _rec:
            return False, (
                f"来源快照的 context md 与它自己的决策件**对不上**"
                f"（{_actual[:8]} vs 决策件记录 {_rec[:8]}）⇒ 它自己就是断链的 ⇒ 不搬"
            )
    _cur_ctx = Path(cur_context)
    if not _cur_ctx.is_file():
        return False, (
            "本轮快照缺 ta_research_context.md ⇒ **无法证明**来源决策件基于**本轮数据**"
            "（fail-closed：宁可不搬 ⇒ sha1 链保持断口 ⇒ 闸门会红 ⇒ 需人工介入）"
        )
    if not _contexts_equivalent(src_ctx, _cur_ctx):
        return False, (
            "来源上下文与本轮现算的上下文**不等价**（不只是路径行不同）"
            "⇒ 来源决策件基于**别的数据** ⇒ 不搬（链保持断口 ⇒ 闸门会红 ⇒ 需人工介入）"
        )
    shutil.copy2(src_ctx, Path(dst_snap) / "ta_research_context.md")
    return True, (
        "来源 context 与本轮现算的上下文**逐字等价**（仅快照路径行不同）"
        "⇒ 已随决策件搬入本快照（sha1 链可校验）"
    )


def _brake_args(nav: float | None, nav_peak: float | None) -> list[str]:
    """🔴 2026-11-18：**急跌刹车**参数（``--nav`` / ``--nav-peak``）。

    🔴 事故背景（审计缺陷 ``no_drawdown_brake`` · high 级）：
      研究链的组合级回撤刹车是实现了的（``analyze.py`` / ``recommend.py`` 收
      ``--nav`` / ``--nav-peak`` ⇒ ``engine/portfolio.py`` 的 ``brake_mode``
      ⇒ 整个组合切防守资产），但**每日链从不传** ⇒ ``nav_state`` 恒为 ``None``
      ⇒ 刹车**永远不触发** ⇒ 「急跌无刹车」。

    ⇒ 这里把每日链算出的**账户净值 / 历史峰值**传下去。
      算不出来（净值不可得）⇒ **不传**（保持原行为，绝不编数字）。
    """
    if nav is None or nav_peak is None:
        return []
    return ["--nav", f"{float(nav):.6f}", "--nav-peak", f"{float(nav_peak):.6f}"]


def _resolve_rules_etf_args(etf_args: list[str] | None) -> list[str]:
    """规则层（``recommend.py``）的 ETF 层参数：**接缝开着时必须显式传入**。

    🔴 2026-11-18（续三）：旧写法 ``etf_args if etf_args is not None else <V1 老参数>``
       ⇒ 任何**漏传** ``etf_args`` 的调用点都会**静默**回落 V1 老参数
       （E 层 EWMA + MA60 闸门）⇒ **分析链用 V2 参数、规则层用 V1 参数**
       ⇒ 两链分叉（历史上真出过「规则层 11-03 无平滑 vs 交付层 11-05 有平滑」）。
       ⇒ **接缝开着 + 没传参 ⇒ 直接抛异常**（fail-closed，不许静默回落）。

    🔴 接缝**关着**时仍允许回落 V1 老配置（那是**明确**的老口径，不是漏传）。
    """
    if etf_args is not None:
        return list(etf_args)
    if bool(getattr(DC, "V2_FROZEN_PARAMS_ENABLED", False)):
        raise V2FP.V2FrozenParamsError(
            "🔴 V2 冻结件接缝**已开启** ⇒ 规则层 ETF 层参数必须由 `_etf_layer_args()` 提供"
            "（漏传 ⇒ 分析链用 V2 参数、规则层回落 V1 参数 ⇒ 两链分叉）⇒ 拒绝跑"
        )
    return _smoothing_args() + [
        "--entry-ma-window", str(DC.ENTRY_MA_WINDOW),
        "--exit-ma-window", str(DC.EXIT_MA_WINDOW),
    ]


def _run_rules_layer(run_path: Path, notes: list[str], *, analysis_asof: str,
                     scores_path: Path, prices_path: Path, universe_path: Path,
                     holdings_arg: str = "",
                     nav: float | None = None, nav_peak: float | None = None,
                     etf_args: list[str] | None = None) -> Path | None:
    """**阶段 3b · 规则层**：``recommend.py`` 在**本快照目录内**现跑。

    🔴 为什么不再「从研究链产物根只读复制」：复制来的是**另一次跑**的产物
    （可能用旧分数 / 不同 asof / 不同平滑口径）⇒ 与本轮交付层**口径不一致**
    （历史上真出过「规则层 11-03 无平滑 vs 交付层 11-05 有平滑」的分叉）。
    ⇒ 现在：**同 asof / 同 scores / 同 prices / 同 universe / 同账户权重 / 同平滑参数**，
    在快照里现跑 ⇒ 规则层与交付层**同源**。
    """
    cmd = [
        DC.QL_PY, str(_RESEARCH_ROOT / "recommend.py"),
        "--asof", str(analysis_asof),
        "--scores", str(scores_path),
        "--prices", str(prices_path),
        "--universe", str(universe_path),
        "--holdings-csv", str(DC.ETF_HOLDINGS_CSV),
        "--out-dir", str(run_path),
        "--pool-mode", "e_covered",
    ] + _resolve_rules_etf_args(etf_args) + _brake_args(nav, nav_peak)
    if holdings_arg:
        cmd += ["--holdings", holdings_arg]
    _run_cmd(cmd, step=STEP_RULES)
    target = run_path / "rebalance_recommendation.json"
    if not target.exists():
        notes.append(
            f"⚠️ 规则层推荐件缺失（{target}）⇒ TA 上下文里的规则层段可能为空"
        )
        return None
    notes.append(
        f"✅ 规则层推荐件已在本快照内产出：{target}"
        "（**同源**：同 asof / 同 scores / 同 prices / 同账户权重 / 同平滑参数）"
    )
    return target

def run_pipeline(*, asof: object = None, snapshot: Any = None,
                 run_dir: str | Path | None = None,
                 prices_csv: str | Path | None = None,
                 scores_csv: str | Path | None = None,
                 calendar_path: str | Path | None = None, preflight_only: bool = False,
                 skip_llm: bool = False, llm_timeout: float | None = None,
                 reasoning_effort: str = "", refresh_scores: bool = True,
                 live_trade: bool = False) -> dict:
    """**统一流水线**：六阶段全流程 → 落一个**不可变快照** → 确定性执行卡片。

    参数
    ----
    snapshot:
        ``daily.snapshot.Snapshot`` 实例（**本轮唯一产物根**）。
        缺省 ⇒ 自建一个（落在 ``daily_config.SNAPSHOT_ROOT``）。
    live_trade:
        🔴 **唯一**实盘副作用开关。``False``（**缺省**）⇒ 观察态：
        执行卡**挂单禁用**、**不发信**、节点日重算失败**可降级**（只披露）；
        ``True`` ⇒ 实盘态：执行卡可下单、允许发信、节点日重算失败**整轮崩**
        （fail-closed：拿不到节点日价就不许出节点日清单）。
    preflight_only:
        **前置判定**模式：不取数、不写产物、不调 LLM（只算「能不能跑」）。

        🔴 2026-11-10：**故意不叫 ``dry_run``**。CLI 的 ``--dry-run`` 是另一个意思
        （＝「显式声明观察态」，而观察态**本来就是缺省行为** ⇒ 它不改变任何行为）。
        两个同名不同义的开关并存在一条链上 = 典型「暗门」：哪天有人把 CLI 的
        ``--dry-run`` 接到这个参数上，CLI 承诺的「跑完落快照 ＋ 报告」会**静默变成
        「产物为空」**。⇒ 这里改叫 ``preflight_only``，语义只此一种。

    执行链 = **六阶段全流程**（**每一步都能单独失败，原始报错原样抛出**）：

    0. （阶段 1 由 ``main.py`` 负责：AkShare 盘后取数 + qlib 数据集滚动快照换库。）
    1. **阶段 1b · 有效行情**：canonical ETF 日线 ＋ 当日新抓日线 ⇒ 快照内有效行情。
    2. **阶段 2 · S 层新鲜度**（``refresh_scores=True``）：跑 ``qlib_scores.py --mode infer
       --end <信号日>``（约 27 分钟）⇒ 跑在临时目录 → 与**快照内底座**合并 → 只落快照。
    3. **阶段 2b · 个股日线导出**（``export_qlib_prices.py``）：多日 RankIC 要全市场个股收盘价。
    4. **阶段 3 · ``analyze.py``**：``E = W × S`` 穿透聚合 + MA20/MA60 均线过滤。
    5. **阶段 3b · 规则层 ``recommend.py``**：**在本快照内现跑**（同源）。
    6. **阶段 4 · ``build_ta_context.py``** → ``ta_research_context.md`` + ``ta_context_meta.json``。
    7. **阶段 4 · ``run_tradingagents.py``**：产出 ``tradingagents_decision.md/.json``。
    8. **阶段 5 · ``final_rebalance_plan.py``** → ``final_rebalance_plan.json``。
    9. **阶段 5 · ``reprice_node_day.py``** → ``node_reprice_<日期>.json``。
    10. **阶段 6 · 组装执行卡片**（见 ``build_execution_card``）。

    返回
    ----
    dict，键见模块契约（``live_trade`` / ``snapshot`` / …）。
    """
    signal_date = run_paths.normalize_date(asof) or DC.NEXT_SIGNAL_DATE
    notes: list[str] = []

    # ---- 快照（本轮唯一产物根）--------------------------------------------
    if snapshot is None:
        snapshot = snap_mod.create_snapshot(
            DC.SNAPSHOT_ROOT, asof=signal_date, live_trade=live_trade)
    run_path = Path(snapshot.path)
    # 🔴 2026-11-09（独立审计 P1-6）：原先这几处写死「daily_config 里的默认快照根常量」
    #    ⇒ CLI 的 ``--snapshot-root PATH`` 被**忽略** ⇒ 自定义根下
    #    「底座链」断裂（退回 canonical 一次性种子）、``--skip-llm`` 复用不到上一轮决策。
    #    ⇒ 统一用**本轮快照自己的根**。
    snap_root = Path(snapshot.root)
    prices_path = Path(prices_csv) if prices_csv is not None else Path(DC.ETF_PRICES_CSV)
    scores_path = (
        Path(scores_csv) if scores_csv is not None else (run_path / "stock_scores.csv")
    )
    universe_path = Path(DC.ETF_UNIVERSE_CSV)
    snapshot_path = run_path / "account_snapshot.csv"
    account_json_path = run_path / "account_context.json"
    report_json = run_path / "etf_analysis_report.json"
    plan_json = run_path / "final_rebalance_plan.json"
    node_json = run_path / f"node_reprice_{signal_date.replace('-', '')}.json"
    decision_json = run_path / "tradingagents_decision.json"
    decision_md = run_path / "tradingagents_decision.md"
    execution_date = DC.NEXT_EXECUTION_DATE if signal_date == DC.NEXT_SIGNAL_DATE else None
    llm_timeout = _resolve_llm_timeout(llm_timeout)
    llm_env_overrides: dict[str, str] = {}
    if reasoning_effort:
        llm_env_overrides["TRADINGAGENTS_REASONING_EFFORT"] = str(reasoning_effort)

    # ---- 前置判定（preflight_only）：只判定「能不能跑」------------------------
    if preflight_only:
        base_path, base_note = snap_mod.resolve_scores_base(
            snap_root, DC.STOCK_SCORES_CSV, exclude=run_path, asof=signal_date)
        notes.append(f"ℹ️ S 层底座（只读）：{base_note}")
        analysis = _resolve_analysis_asof(
            signal_date=signal_date,
            scores_path=Path(base_path) if base_path is not None else scores_path,
            calendar_path=calendar_path,
        )
        notes.extend(analysis["notes"])
        notes.append(
            "🧪 **前置判定（preflight_only）**：未取数、未写任何产物、未调 LLM"
            "（快照目录已建但**空**）⇒ 仅做前置判定"
        )
        return {
            "live_trade": bool(live_trade),
            "asof": signal_date,
            "execution_date": execution_date,
            "run_dir": str(run_path),
            "snapshot": {
                "root": str(snapshot.root),
                "path": str(run_path),
                "run_id": str(snapshot.run_id),
            },
            "report_json": None,
            "plan_json": None,
            "node_json": None,
            "card_json": None,
            "card": None,
            "decision_json": str(decision_json),
            "decision_md": str(decision_md),
            "scores_path": str(scores_path),
            "prices_path": str(prices_path),
            "email_kind": "rebalance_card",
            "notes": notes,
            "holding_discipline": None,
            "analysis_asof": analysis["analysis_asof"],
            "scores_max_date": analysis["scores_max_date"],
            "score_grid_last": analysis["grid_last"],
            "qlib_dataset_max_date": analysis["dataset_max_date"],
            # 🔴 前置判定分支**不出卡** ⇒ 这 5 个键仍要占位（值 = 「本轮未跑」）
            #    ⇒ 调用方（`report.py` / `main.py`）读键不会 KeyError。
            "llm_decision_status": "not_run",
            "execution_lag_days": int(DC.EXECUTION_LAG_DAYS),
            "node_reprice_ok": None,
            "node_reprice_error": "",
            "generated_at": _now_iso(),
            "preflight_only": True,
        }

    run_path.mkdir(parents=True, exist_ok=True)
    notes.append(f"📦 本轮快照目录：{run_path}（live_trade={live_trade}）")
    if live_trade:
        notes.append("🔴 **实盘态（--live-trade）**：执行卡**可下单**、允许发信")
    else:
        notes.append(f"🧪 **观察态**（未开 --live-trade）：{snap_mod.OBSERVATION_DISCLAIMER}")

    # ---- 账户快照 + 阶段 4 账户上下文 --------------------------------------
    account_state.write_snapshot_csv(snapshot_path)
    notes.append(
        f"ℹ️ 实盘账户快照已生成：{snapshot_path}"
        f"（口径 = {DC.ACCOUNT_SNAPSHOT_DATE} 用户基准：总资产 {DC.TOTAL_ASSETS} 元 / "
        f"现金 {DC.CASH} 元）"
    )
    account_state.write_account_json(account_json_path)
    notes.append(
        f"✅ 阶段 4 账户上下文（实盘口径）已导出：{account_json_path}"
        f"（总资产 {DC.TOTAL_ASSETS} 元 / 现金 {DC.CASH} 元 / "
        f"唯一底仓 {DC.PRIMARY_HOLDING_CODE} {DC.PRIMARY_HOLDING_SHARES} 股）"
    )
    account_summary = account_state.account_summary()
    holdings_arg = _holdings_arg(account_summary)

    # ---- 持仓纪律检查（唯一持仓 vs MA60）-----------------------------------
    # 🔴 这里必须传**原始** ``prices_csv``（可能是 None），不能传 ``prices_path``：
    #    ``_load_price_frame`` 用「None ⇒ 缺省路径读不到就降级 + 披露」、
    #    「显式路径读不到 ⇒ 抛异常（拒绝编造数据）」区分两种语义；
    #    传 ``prices_path``（永远是个具体 Path）会把「缺省」误判成「显式」
    #    ⇒ 没有该行情文件的机器直接崩（2026-11-09 回归）。
    discipline = _check_holding_discipline(
        asof=signal_date, prices_csv=prices_csv, account=account_summary, notes=notes)

    # ---- 阶段 2：S 层（底座链式 ⇒ 增量重算 ⇒ 只落快照）---------------------
    if not scores_path.exists():
        base_path, base_note = snap_mod.resolve_scores_base(
            snap_root, DC.STOCK_SCORES_CSV, exclude=run_path, asof=signal_date)
        notes.append(f"ℹ️ S 层底座：{base_note}")
        if base_path is not None:
            scores_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base_path, scores_path)
            notes.append(
                f"✅ S 层底座已**只读拷贝**进本快照：{scores_path}（源文件零改动）"
            )
            # 🔴 **指纹必须一起拷**（否则守卫读不到底座指纹 ⇒ 判「不可增量」
            #    ⇒ 每轮都白跑一次全量打分；更糟：万一指纹判成「一致」就是错的口径）
            base_fp_src = base_path.parent / SCORES_FINGERPRINT_NAME
            if base_fp_src.is_file():
                shutil.copy2(base_fp_src, scores_path.parent / SCORES_FINGERPRINT_NAME)
                notes.append(
                    f"ℹ️ 底座**模型指纹**已一并拷贝：{scores_path.parent / SCORES_FINGERPRINT_NAME}"
                )
            else:
                notes.append(
                    "⚠️ 底座**没有模型指纹**旁挂文件（老底座）⇒ 本轮会**整段重算**"
                    "（指纹对不上 ⇒ 不许把两套模型的分数拼在一起）"
                )
    refreshed = bool(refresh_scores)
    if refreshed:
        _refresh_scores(scores_path=scores_path, notes=notes, end_date=signal_date)

    analysis = _resolve_analysis_asof(
        signal_date=signal_date, scores_path=scores_path, calendar_path=calendar_path)
    notes.extend(analysis["notes"])
    analysis_asof = analysis["analysis_asof"]
    if not refreshed:
        notes.append(
            f"⚠️ 本轮**未重跑 S 层**（refresh_scores=False）⇒ 沿用快照底座里的旧分数"
            f"（最新打分日 {analysis.get('scores_max_date') or '—'}）"
        )

    # ---- 2b. export_qlib_prices.py（个股日线，供 analyze.py 的多日 RankIC）----
    stock_prices_path = _export_stock_prices(analysis_asof, notes, out_dir=run_path)

    # ---- 2c. 急跌刹车：净值 / 峰值（**日频防守**的唯一事实源）--------------
    # 🔴 2026-11-18：账户净值按 asof 收盘价重估 ⇒ 跨日持久化 ⇒ 峰值是真峰值。
    #    算不出来 ⇒ 不传 --nav/--nav-peak（保持原行为，不编数字）。
    nav_info = nav_mod.prepare_daily_nav(
        asof=str(analysis_asof),
        account=account_state.account_summary(),
        prices_csv=Path(prices_path),
        notes=notes,
    )
    try:
        (run_path / "nav_state.json").write_text(
            json.dumps(nav_info, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    _nav = nav_info.get("nav")
    _nav_peak = nav_info.get("peak")

    # ---- 3. analyze.py（阶段 3：E=W×S 穿透 + MA20/MA60 过滤）----
    # 🔴 2026-11-18：ETF 层参数走 **V2 冻结件接缝**（分析链 / 规则层**同一组**）。
    etf_args, seam_disclosure = _etf_layer_args()
    try:
        (run_path / "v2_frozen_params.json").write_text(
            json.dumps(seam_disclosure, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    if seam_disclosure.get("enabled", False):
        notes.append(
            "🔴 **V2 冻结件接缝已接通**：ETF 层参数来自 "
            f"`{seam_disclosure.get('source')}`（md5 {str(seam_disclosure.get('source_md5'))[:12]}…"
            f"｜冠军臂 {seam_disclosure.get('winner_arm')}）"
            f"｜alpha={seam_disclosure.get('params', {}).get('alpha_weight')}"
            f" / momentum={seam_disclosure.get('params', {}).get('momentum_weight')}"
            f"｜平滑臂 {seam_disclosure.get('params', {}).get('smoothing_arm')}"
            f"（平滑{'开' if seam_disclosure.get('params', {}).get('smoothing_enabled') else '关'}）"
            f"｜MA 入/出 {seam_disclosure.get('params', {}).get('entry_ma_window')}"
            f"/{seam_disclosure.get('params', {}).get('exit_ma_window')}"
            f"｜top_k={seam_disclosure.get('params', {}).get('top_k')}"
            f"｜hold_buffer={seam_disclosure.get('params', {}).get('hold_buffer')}"
            f"｜同族上限={'关' if not seam_disclosure.get('params', {}).get('allow_theme_cap') else '开'}"
        )
    else:
        notes.append(
            "🔴 **V2 冻结件接缝未启用** ⇒ ETF 层仍用 V1 老配置"
            "（E 层 EWMA + MA60 闸门）⇒ 与 V2 五折冠军口径**不一致**"
        )
    _run_cmd([
        DC.QL_PY, str(_RESEARCH_ROOT / "analyze.py"),
        "--asof", analysis_asof,
        "--total-assets", f"{DC.TOTAL_ASSETS}",
        "--cash", f"{DC.CASH}",
        "--account-snapshot", str(snapshot_path),
        "--prices", str(prices_path),
        "--universe", str(universe_path),
        "--scores", str(scores_path),
        "--out-dir", str(run_path),
        # 🔴 2026-11-11（缺陷 C）：顺延池必须是**完整候选池** ⇒ 不能用默认的 Top-15
        "--top-n", str(_analyze_top_n()),
    ] + (["--stock-prices", str(stock_prices_path)] if stock_prices_path else [])
        + etf_args + _brake_args(_nav, _nav_peak), step=STEP_ANALYZE)
    notes.append(f"✅ 分析报告已产出：{report_json}（分析日 = {analysis_asof}）")
    # 🔴 穿透分平滑口径披露：每日链**不复制**平滑参数（单一事实源在研究链 `config.py`），
    #    但日报必须把**实际生效**的排序口径写出来 ⇒ 读分析报告里的 `score_smoothing` 段。
    smoothing_note = ""
    _smoothing: dict = {}
    try:
        _smoothing = json.loads(report_json.read_text(encoding="utf-8")).get("score_smoothing") or {}
    except Exception:
        _smoothing = {}
    if _smoothing:
        smoothing_note = (
            f"🆕 **穿透分时间平滑**：{_smoothing.get('ranking_basis')}"
            f"｜排序依据 = 平滑分（原始分同时披露）"
            f"｜观测日 {len(_smoothing.get('observation_dates') or [])} 个"
            f"（{', '.join(_smoothing.get('observation_dates') or []) or '无'}）"
            f"｜退化成瞬态分 {_smoothing.get('n_codes_fallback_transient')} 只"
        )
        notes.append(smoothing_note)

    # ---- 3b. recommend.py（规则层：**在本快照内现跑** ⇒ 与交付层同源）----
    _run_rules_layer(
        run_path, notes,
        analysis_asof=analysis_asof,
        scores_path=scores_path,
        prices_path=prices_path,
        universe_path=universe_path,
        holdings_arg=holdings_arg,
        nav=_nav,
        nav_peak=_nav_peak,
        etf_args=etf_args,
    )

    # ---- 3/4. TradingAgents 阶段 ----
    # 🔴 2026-11-11 修（**真缺陷**）：**TA 上下文与 LLM 调用是两件事**
    #    `build_ta_context.py` 是**本地确定性计算**（拥挤度 / 重合度 / 3.5 节），**不调 API**；
    #    旧口径把它塞在 `else`（真 LLM）分支里 ⇒ `--skip-llm` 复跑时快照**缺**
    #    `ta_research_context.md` / `ta_context_meta.json` ⇒ 快照**结构不完整**
    #    ⇒ 闸门「取最新 LLM 完整快照」会**跳过**它 ⇒ 这一轮白跑（实测 2026-11-11 15:08）。
    #    ⇒ 现在上下文**永远**构建；`--skip-llm` 只跳过 **LLM 调用**（决策件复用既有落盘件）。
    _run_cmd([
        DC.QL_PY, str(_RESEARCH_ROOT / "llm" / "build_ta_context.py"),
        "--run-root", str(run_path),
        # 🔴 2026-11-11（审查发现 3.5）：**显式**把与规则层同源的本轮有效行情
        #    喂给 TA 上下文 ⇒ 避免 build_ta_context 回落到 canonical 旧行情，
        #    导致 corr120 / 拥挤度与规则层对不上。
        "--prices", str(prices_path),
        # 🔴 2026-11-18（用户目标文档 §二.1）：**强制**喂实盘账户口径。
        #    旧口径：build_ta_context 读研究链 canonical 账户（2026-11-04 旧件：
        #    总资产 29,854.67 / 现金 15,518.87 / 5 只持仓 / 权益 48%）
        #    ⇒ TA 以为账户是重仓 ⇒ 给出 Underweight + 权益上限 20%
        #    ⇒ 下游拿真实 23.43% 去比 ⇒ 「一笔不动」⇒ 实盘前夜的风控大脑看着假账本。
        #    ⇒ 现在必须用本轮快照导出的实盘口径（`account_context.json`）。
        "--account-json", str(account_json_path),
    ], step=STEP_CONTEXT)
    notes.append(f"✅ TA 上下文已产出：{run_path / 'ta_research_context.md'}")
    llm_status = "skipped"
    if skip_llm:
        reused_from = None
        if not decision_json.exists():
            # 🔴 2026-11-10：按 **asof** 挑底座（不是按目录名＝墙钟创建序）
            #    ⇒ 复用别个信号日的 LLM 决策件 ⇒ 交付件里嵌的是**另一个信号日**的意见
            # 🔴 2026-11-11（真机跑批事故）：旧口径只取「最新一份快照」⇒
            #    若那份是**被中断的不完整快照**（没有决策件）⇒ 复制不到任何决策件
            #    ⇒ 本轮**静默变成「无 TA 意见」**（「本轮无 TA 否决」、合规筛选整节失效）。
            #    ⇒ 改用「**最新且确实有决策件**」的那份（asof 语义不变）。
            # 🔴 2026-11-11（第九轮续）：候选**从新到旧**逐个试，
            #    只接受「决策件 + 它自己的上下文三件套**互相自洽**」的那份
            #    （实测 `20261111_161126`：它自己就是断链的 ⇒ 必须被跳过）。
            _cands = snap_mod.snapshots_with_file(
                snap_root, "tradingagents_decision.json",
                exclude=run_path, asof=signal_date)
            for _cand in _cands:
                _cand_json = _cand / "tradingagents_decision.json"
                _ok, _why = _carry_context_trio(
                    _cand, run_path, run_path / "ta_research_context.md", _cand_json)
                if not _ok:
                    notes.append(f"⚠️ 候选复用快照 {_cand.name} 被跳过：{_why}")
                    continue
                shutil.copy2(_cand_json, decision_json)
                reused_from = _cand_json
                notes.append(
                    f"ℹ️ 跨快照复用决策件（来源快照 {_cand.name}）⇒ "
                    f"上下文三件套已随决策件一起搬入本快照：{_why}"
                )
                break
        if decision_json.exists():
            _run_cmd([
                DC.LLM_PY, str(_RESEARCH_ROOT / "llm" / "run_tradingagents.py"),
                "--ta-dir", str(DC.TRADINGAGENTS_DIR),
                "--run-root", str(run_path),
                "--out-md", str(decision_md),
                "--out-json", str(decision_json),
                "--render-only", "1",
            ], step=STEP_TRADINGAGENTS)
            llm_status = "reused"
            notes.append(
                "ℹ️ skip_llm=True：复用**既有决策件**"
                + (f"（{reused_from} ⇒ 复制进本快照）" if reused_from else "")
                + "（--render-only 重渲 md，零 API 成本）"
            )
        else:
            llm_status = "absent"
            notes.append(
                "ℹ️ skip_llm=True 且**没有**可复用的决策件 ⇒ 本轮无 LLM 第二意见（只有规则层）"
            )
    else:
        _run_cmd([
            DC.LLM_PY, str(_RESEARCH_ROOT / "llm" / "run_tradingagents.py"),
            "--ta-dir", str(DC.TRADINGAGENTS_DIR),
            "--run-root", str(run_path),
            "--context", str(run_path / "ta_research_context.md"),
            "--ticker", DC.PRIMARY_HOLDING_CODE,
            "--date", analysis_asof,
            "--account-json", str(account_json_path),
            "--out-md", str(decision_md),
            "--out-json", str(decision_json),
        ], step=STEP_TRADINGAGENTS, timeout=llm_timeout,
            env=_env_with_dotenv(DC.TRADINGAGENTS_DIR, overrides=llm_env_overrides))
        notes.append(
            f"✅ 阶段 4 超时上限 = {llm_timeout:.0f} 秒"
            + (f"｜reasoning_effort 覆盖 = {reasoning_effort}（子进程 env 下发，.env 未改）"
               if reasoning_effort else "｜reasoning_effort = **不覆盖**（沿用 .env 现役值）")
        )
        payload = json.loads(decision_json.read_text(encoding="utf-8"))
        llm_status = str(payload.get("status") or "")
        if llm_status != "ok":
            raise RuntimeError(
                f"阶段 4 第 {STEP_TRADINGAGENTS} 步失败：决策 json 的 status = {llm_status!r}"
                f"（**权威字段**；md 在失败时也会写占位件，不能只看 md）\n"
                f"错误：{payload.get('error')}\n文件：{decision_json}"
            )
        notes.append(f"✅ TradingAgents 决策已产出（status = ok）：{decision_md}")

    # ---- 5. final_rebalance_plan.py ----
    _run_cmd([
        DC.QL_PY, str(_RESEARCH_ROOT / "final_rebalance_plan.py"),
        "--report", str(report_json),
        "--decision", str(decision_md),
        "--prices", str(prices_path),
        "--universe", str(universe_path),
        "--account", str(snapshot_path),
        "--out-dir", str(run_path),
    ], step=STEP_PLAN)
    notes.append(f"✅ 调仓计划已产出：{plan_json}")

    # ---- 6. reprice_node_day.py（节点日收盘价重算股数） ----
    # 🔴 **实盘态 fail-closed**：节点日重算失败 ⇒ 整轮崩（拿不到节点日价就不许出节点日清单）；
    #    **观察态可降级**：只披露「本轮不给节点日股数/金额」，绝不静默降级、
    #    也不用信号日旧价冒充节点日价。
    node_reprice_error: str | None = None
    try:
        _run_cmd([
            DC.QL_PY, str(_RESEARCH_ROOT / "reprice_node_day.py"),
            "--node-date", signal_date,
            "--plan", str(plan_json),
            "--prices", str(prices_path),
            "--account", str(snapshot_path),
            "--cash", f"{DC.CASH}",
            "--total-assets", f"{DC.TOTAL_ASSETS}",
            "--total-assets-basis", "plan",
            "--out-dir", str(run_path),
        ], step=STEP_REPRICE)
        notes.append(f"✅ 节点日重算已产出：{node_json}")
    except Exception as exc:
        if live_trade:
            raise
        node_reprice_error = f"{type(exc).__name__}: {exc}"
        _head = str(exc).strip().splitlines()[0] if str(exc).strip() else node_reprice_error
        notes.append(
            "🔴 **节点日重算未完成（观察态降级继续）**："
            f"{_head}"
            " ⇒ 本轮**不给节点日股数/金额**（🔴 不用信号日旧价冒充节点日价）"
            " ⇒ 执行卡退化为**只有方向、无数值**的观察卡（**不可下单**）。"
        )

    # ---- 7. 执行卡片 ----
    plan_payload = json.loads(plan_json.read_text(encoding="utf-8"))
    node_payload: dict = {}
    if node_reprice_error is None:
        node_payload = json.loads(node_json.read_text(encoding="utf-8"))
        _actions = node_payload.get("actions") or []
        _exec_date = node_payload.get("meta", {}).get("execution_date") or execution_date
    else:
        _actions = plan_payload.get("actions") or []
        _exec_date = execution_date
    card = build_execution_card(
        actions=_actions,
        account=account_state.account_summary(),
        asof=signal_date,
        execution_date=_exec_date,
        node_json=node_payload or None,
        plan_json=plan_payload,
        live_trade=live_trade,
        node_reprice_ok=(node_reprice_error is None),
        node_reprice_error=node_reprice_error or "",
    )
    notes.append(
        f"✅ 执行卡片已组装：{len(card.get('orders') or [])} 条动作"
        f"｜{PRICE_BASIS_NOTE}"
        + ("｜🔴 **实盘卡（可下单）**" if live_trade else "｜🧪 **观察卡（挂单禁用）**")
    )

    # ---- 执行卡落盘（🔴 审计用：信号日决策必须留一份**可复查**的执行卡） ----
    card_path = persist_execution_card(card, run_path, signal_date)
    notes.append(f"✅ 执行卡片已落盘：{card_path}")

    return {
        "live_trade": bool(live_trade),
        "asof": signal_date,
        "execution_date": card.get("execution_date"),
        "run_dir": str(run_path),
        "snapshot": {
            "root": str(snapshot.root),
            "path": str(run_path),
            "run_id": str(snapshot.run_id),
        },
        "report_json": str(report_json),
        "plan_json": str(plan_json),
        "node_json": str(node_json),
        "card_json": str(card_path),
        "card": card,
        "decision_json": str(decision_json),
        "decision_md": str(decision_md),
        "scores_path": str(scores_path),
        "prices_path": str(prices_path),
        "email_kind": "rebalance_card",
        "notes": notes,
        # 🔴 2026-11-10：把**实际生效的排序口径**透传给交付面报告
        #    ⇒ `daily/report.py::_ranking_basis()` 才不会渲染成「—」
        #    （口径事实源 = 分析报告里的 `score_smoothing` 段，**不复制参数**）。
        "score_smoothing": _smoothing,
        # 🔴 2026-11-10：TA 合规筛选（否决 / 顺延）结果**透传**给交付面报告
        #    ⇒ `daily/report.py` 能渲染「🛡 TA 合规筛选」小节（**只透传，不改数字**）
        "ta_screening": (plan_payload.get("ta_screening") or {})
        if isinstance(plan_payload, dict)
        else {},
        "holding_discipline": discipline,
        "analysis_asof": analysis_asof,
        "scores_max_date": analysis["scores_max_date"],
        "score_grid_last": analysis["grid_last"],
        "qlib_dataset_max_date": analysis["dataset_max_date"],
        "llm_decision_status": llm_status,
        "execution_lag_days": int(DC.EXECUTION_LAG_DAYS),
        "node_reprice_ok": node_reprice_error is None,
        "node_reprice_error": node_reprice_error or "",
        "generated_at": _now_iso(),
        "preflight_only": False,
    }


# =============================================================================
# C. 组合算术 —— 执行卡片（研究链里**没有**的硬约束都在这里）
# =============================================================================
def _est_fee(amount: float, side: str | None) -> float:
    """费用估算：买入 = max(金额×佣金率, 最低佣金)；卖出另加**印花税**（单边千一）。

    🔴 卖出才有印花税 ⇒ 同一金额下卖出费**一定**高于买入费（测试会锁住这条）。
    """
    amt = abs(float(amount))
    fee = max(amt * float(DC.COMMISSION_RATE), float(DC.MIN_COMMISSION))
    if side == "sell":
        fee += amt * float(DC.STAMP_DUTY_RATE)
    return fee


def _buy_cost(amount: float) -> float:
    """一笔买入**占用**的现金 = 金额 + 佣金 + 滑点估算（现金缓冲守卫用）。"""
    amt = abs(float(amount))
    return amt + _est_fee(amt, "buy") + amt * float(DC.SLIPPAGE_RATE)


def merge_actions_by_code(
    actions: list[dict] | None,
    account_positions: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """🔴 **标的级终态归并**（ticker 唯一主键）—— 多来源行动行归并成「每个 ticker 一行」。

    🔴 2026-11-23（真机缺陷：**同标的双行冲突**）
    实测 2026-11-20 的计划件对 **同一标的 `512800`** 产出**两条冲突行**：
    一行「已持仓但未进入目标名额」目标 **0 股**、一行「TA 顺延补位」目标 **1,000 股**，
    两行**都按期初 7,700 股起算** ⇒ 下游按行折算 ⇒ 合计卖出 **14,400 股 > 持仓 7,700 股**
    ⇒ 执行卡 `cash_end = 36,680.60` **超过**总资产 `30,501.07`（虚增 **6,179.53 元**）
    ⇒ `post_trade.positions` 里 512800 重复两次
    ⇒ 逐日台账 `(日期, 代码)` 主键碰撞 ⇒ **重跑不幂等**。

    ⇒ 本函数是**执行层的最后一道闸**（上游 `final_rebalance_plan` 已就地修好源侧，
      这里**再兜一层** ⇒ 任何来源的动作表都先归并、再折算）：
      - **后序合法覆盖前序**：同一 ticker 的**最后一行**是终态（目标权重 / 目标股数）；
      - **期初股数**：动作行自报值与账户快照不一致 ⇒ **如实披露，不静默取其一**
        （动作行的 `current_shares` 本身是研究链算出的期初实际持仓；
          **卖出上限**以账户快照为准 ⇒ 由 `build_execution_card` 的**硬断言①**兜底）；
      - `delta_shares` **只允许**在最后一步算一次 = 终态目标股数 − 期初股数
        （执行卡本来就是这么算的 ⇒ 归并后每个 ticker 只有一行 ⇒ 只算一次）。

    返回
    ----
    ``(merged_actions, disclosure)``：归并后的动作表 ＋ 逐条归并披露（审计要看得到）。
    """
    rows = [dict(a) for a in (actions or [])]
    acct_shares: dict[str, int] = {}
    for pos in (account_positions or []):
        _code = str(pos.get("code") or "").strip().zfill(6)
        _sh = _i(pos.get("shares"))
        if _code and _sh is not None:
            acct_shares[_code] = int(_sh)

    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    unkeyed: list[dict] = []
    for a in rows:
        code = str(a.get("code") or "").strip().zfill(6)
        if not code:
            #: 🔴 无代码行**没有 ticker** ⇒ 谈不上「同标的双行」⇒ 原样保留（不丢数据）
            unkeyed.append(a)
            continue
        if code not in grouped:
            grouped[code] = []
            order.append(code)
        grouped[code].append(a)

    merged: list[dict] = []
    disclosure: list[dict] = []
    for code in order:
        group = grouped[code]
        #: 🔴 **后序合法覆盖前序** ⇒ 终态 = 同一 ticker 的**最后一行**
        terminal = dict(group[-1])
        terminal["code"] = code
        if len(group) > 1:
            _reasons = [str(a.get("reason") or "") for a in group]
            _reasons = [r for r in _reasons if r]
            terminal["reason"] = "｜".join(dict.fromkeys(_reasons))
            terminal["merged_action_rows"] = len(group)
            disclosure.append({
                "code": code,
                "name": str(terminal.get("name") or ""),
                "n_rows": len(group),
                "winning_row_index": len(group) - 1,
                "terminal_target_weight": _f(terminal.get("target_weight")),
                "terminal_target_shares": _i(terminal.get("target_shares")),
                "rule": "后序合法覆盖前序（同一 ticker 只保留终态一行）",
            })
        #: 🔴 **期初股数：动作行自报值与账户快照不一致 ⇒ 如实披露，不静默取其一**
        #    （归并只管「终态」（后序覆盖前序）⇒ **不改写**期初股数：
        #      ① 动作行的 `current_shares` 就是研究链算出的**期初实际持仓**
        #         （生产口径来自 `account_snapshot.csv` ⇒ 与账户快照同源）；
        #      ② 账户快照是**卖出上限**的权威 ⇒ 由下面的**硬断言①**兜底，
        #         不在这里偷偷改写语义（改写会让「封顶不得反向减仓」等既有守卫失真）。
        if code in acct_shares:
            _prev = _i(terminal.get("current_shares"))
            if _prev is not None and int(_prev) != acct_shares[code]:
                terminal["current_shares_conflict"] = {
                    "action_row_value": int(_prev),
                    "account_value": acct_shares[code],
                }
                disclosure.append({
                    "code": code,
                    "name": str(terminal.get("name") or ""),
                    "n_rows": len(group),
                    "winning_row_index": len(group) - 1,
                    "terminal_target_weight": _f(terminal.get("target_weight")),
                    "terminal_target_shares": _i(terminal.get("target_shares")),
                    "rule": (
                        "🔴 期初股数冲突：动作行自报 "
                        f"{int(_prev)} 股 vs 账户快照 {acct_shares[code]} 股"
                        " ⇒ **如实披露，不静默取其一**"
                        "（卖出上限以**账户快照**为准 ⇒ 由硬断言①兜底）"
                    ),
                })
        merged.append(terminal)
    for a in unkeyed:
        merged.append(a)
        disclosure.append({
            "code": "",
            "name": str(a.get("name") or ""),
            "n_rows": 1,
            "winning_row_index": 0,
            "terminal_target_weight": _f(a.get("target_weight")),
            "terminal_target_shares": _i(a.get("target_shares")),
            "rule": "无代码行动行（原样保留，不参与标的级归并）",
        })
    return merged, disclosure


def _aggregate_cash(orders: list[dict]) -> dict[str, float]:
    """把订单表汇总成现金流分量（卖出 / 买入各自合计）。"""
    sells = [o for o in orders if (o["delta_shares"] or 0) < 0]
    buys = [o for o in orders if (o["delta_shares"] or 0) > 0]
    return {
        "sell_proceeds": sum(abs(o["est_amount"] or 0.0) for o in sells),
        "sell_fees": sum(o["est_fee"] or 0.0 for o in sells),
        "sell_slippage": sum(o["est_slippage"] or 0.0 for o in sells),
        "buy_amount": sum(o["est_amount"] or 0.0 for o in buys),
        "buy_fees": sum(o["est_fee"] or 0.0 for o in buys),
        "buy_slippage": sum(o["est_slippage"] or 0.0 for o in buys),
    }


def persist_execution_card(
    card: dict, run_path: str | Path, signal_date: str
) -> Path:
    """把执行卡落盘 ⇒ `<run_path>/execution_card_<YYYYMMDD>.json`。

    🔴 **审计用**：信号日的执行卡必须留一份**可复查**的产物 —— 邮件正文会被
    排版吃掉，原始结构化决策才有追溯价值。
    """
    out_dir = Path(run_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"execution_card_{str(signal_date).replace('-', '')}.json"
    path.write_text(
        json.dumps(card, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def build_execution_card(*, actions: list[dict] | None = None, account: Any = None,
                         asof: object = None, execution_date: object = None,
                         prices: Any = None, node_json: dict | None = None,
                         plan_json: dict | None = None, target_positions: int | None = None,
                         max_amount: float | None = None, lot_size: int | None = None,
                         cash_buffer_min: float | None = None,
                         cash_buffer_max: float | None = None,
                         live_trade: bool = False,
                         node_reprice_ok: bool = True,
                         node_reprice_error: str = "") -> dict:
    """把研究链的动作表折算成**可下单的执行卡片**（含研究链没有的硬约束）。

    硬约束（全部来自 ``daily_config``）：

    * 目标股数**向下取整**到整手（``lot_size``），不能为负 ⇒ 归 0；
    * 单只标的**金额上限**（``max_amount``）⇒ 超了按上限缩减股数（仍取整手），
      并记 ``capped=True`` + ``cap_note``；
    * **货基碎屑过滤**：一手金额 > 上限且当前无持仓 ⇒ ``action="不可建仓"``、
      目标股数归 0、计入 ``cash_flow.unbuildable_slots``、**不进下单清单**；
    * **现金缓冲守卫**：调仓后现金必须 ≥ **生效下限** ⇒ 不够就从**金额最大的
      买入单**开始减；减完仍不够 ⇒ ``feasible=False`` + 明确 reason。
      🔴 2026-11-10：生效下限 = ``max(现金缓冲绝对下限, 总资产 × 现金预留比例)``
      （比例下限 ⇒ **权益上限 = 1 - 预留比例**）⇒ 规则层留现金、执行层也留现金。

    参数
    ----
    actions : 动作表（``node_reprice`` 优先，其次 ``final_rebalance_plan`` 的 ``actions[]``）；
        给 ``None`` ⇒ 从 ``node_json`` / ``plan_json`` 里取。
    account : 账户（总资产 / 现金 / 持仓）；缺字段 ⇒ 该字段为 ``None`` 并在 warnings 点名。
    node_reprice_ok :
        **节点日重算是否成功**。``False`` ⇒ 节点日收盘价拿不到（收盘前 / 当日行情未落库）
        ⇒ 本卡**不给节点日股数/金额**（🔴 绝不用信号日旧价冒充节点日价）
        ⇒ 卡片退化为「**只有方向、无数值**」的观察卡 + 显式告警。

    返回
    ----
    dict，键：``asof, execution_date, execution_lag_days, execution_basis,
    price_basis_note, account, orders, post_trade, cash_flow, warnings, disclaimer``。
    """
    max_amt = float(max_amount) if max_amount is not None else float(DC.MAX_POSITION_AMOUNT)
    lot = int(lot_size) if lot_size is not None else int(DC.LOT_SIZE)
    buf_min = float(cash_buffer_min) if cash_buffer_min is not None else float(DC.CASH_BUFFER_MIN)
    buf_max = float(cash_buffer_max) if cash_buffer_max is not None else float(DC.CASH_BUFFER_MAX)
    n_target = int(target_positions) if target_positions is not None else int(DC.TARGET_POSITIONS)

    warnings: list[str] = []
    if actions is None:
        if node_json and node_json.get("actions"):
            actions = node_json["actions"]
        elif plan_json and plan_json.get("actions"):
            actions = plan_json["actions"]
        else:
            actions = []
            warnings.append("动作列表为空（node_json / plan_json 里都没有 actions）⇒ 执行卡为空")

    acc, acc_warnings = _resolve_account(account)
    warnings.extend(acc_warnings)

    # 🔴 2026-11-23（真机缺陷：**同标的双行冲突** · 根因修复 · 执行层兜底）
    #   动作表可能对**同一 ticker** 给出多条冲突行
    #   （规则层调仓「目标 0 股」 + TA 顺延补位「目标 1,000 股」 + 孤儿持仓保留）
    #   ⇒ 逐行折算 ⇒ 合计卖出 > 持仓 ⇒ `cash_end` 虚增 ⇒ `post_trade.positions` 重复
    #   ⇒ 台账 `(日期, 代码)` 主键碰撞 ⇒ 重跑不幂等。
    #   ⇒ 这里先做**标的级终态归并**（ticker 唯一主键、**后序合法覆盖前序**）
    #     ⇒ 每个 ticker 只剩**一行** ⇒ 下游 `delta_shares` 只算一次
    #     ＝ 终态目标股数 − 期初实际持仓股数（期初股数取**账户快照真值**）。
    _merged_actions, _merge_disp = merge_actions_by_code(actions, acc.get("positions") or [])
    actions = _merged_actions
    for _d in _merge_disp:
        if int(_d.get("n_rows") or 1) > 1:
            warnings.append(
                f"🔴 同一标的 {_d['code']} {_d['name']} 出现 **{_d['n_rows']} 条行动行**"
                f" ⇒ 已按「后序合法覆盖前序」归并为 1 条"
                f"（终态目标股数 {_d['terminal_target_shares']}）"
                f"｜{_d['rule']}"
            )
        elif "期初股数冲突" in str(_d.get("rule") or ""):
            warnings.append(f"🔴 {_d['code']} {_d['name']}：{_d['rule']}")

    total_assets = _f(acc.get("total_assets"))
    cash = _f(acc.get("cash"))
    market_value = _f(acc.get("market_value"))
    if total_assets is None:
        warnings.append("账户缺 `total_assets` ⇒ 权重类字段记为 None（不编数字）")
    if market_value is None and total_assets is not None and cash is not None:
        market_value = total_assets - cash

    # 🔴 2026-11-10：**生效的现金下限** = 「绝对下限」与「比例下限」**取大者**
    #    （比例下限 = 总资产 × 现金预留比例 ⇒ 权益上限 = 1 - 预留比例 = 80%）。
    #    旧口径只看绝对 200 元 ⇒ 允许权益配到 100%（不留现金）⇒ 与规则层不一致。
    reserve_req = (total_assets * float(DC.CASH_RESERVE_RATIO)) if total_assets else None
    buf_min_eff = max(buf_min, reserve_req) if reserve_req is not None else buf_min
    #: 生效下限的**可读说明**（绝对下限 vs 比例下限）⇒ 告警文案里必须能看出是哪一个在起作用
    if reserve_req is None:
        buf_min_note = f"绝对下限 {buf_min:.2f} 元（账户缺 total_assets ⇒ 比例下限不参与）"
    elif reserve_req > buf_min:
        buf_min_note = (
            f"生效下限 {buf_min_eff:.2f} 元"
            f"（比例下限 {reserve_req:.2f} 元 = 总资产 × {DC.CASH_RESERVE_RATIO:.0%}"
            f" > 绝对下限 {buf_min:.2f} 元 ⇒ 取比例下限）"
        )
    else:
        buf_min_note = (
            f"生效下限 {buf_min_eff:.2f} 元"
            f"（绝对下限 {buf_min:.2f} 元 ≥ 比例下限 {reserve_req:.2f} 元 ⇒ 取绝对下限）"
        )

    orders: list[dict] = []
    unbuildable: list[dict] = []

    for act in actions or []:
        code = str(act.get("code") or "").strip().zfill(6)
        name = str(act.get("name") or "")
        current_shares = _i(act.get("current_shares"))
        current_shares = 0 if current_shares is None else current_shares
        price = _f(act.get("price"))
        price_date = act.get("price_date")
        if price is None:
            price, fallback_date = _price_of(prices, code)
            price_date = price_date or fallback_date
        # 🔴 2026-11-11（审计缺陷 A·**缺价角落**）：「维持」行（TA 一票否决但仍在持仓）
        #    **必须**先于「缺价」分支处理：
        #      旧顺序（缺价在前）⇒ 本行落进缺价分支 ⇒ `target_shares=None`
        #      ⇒ positions 循环先 `covered.add(code)` 再 `if shares is None: continue`
        #      ⇒ 该代码**被标记 covered 却不进 positions**；账户持仓兜底循环又因 covered 跳过
        #      ⇒ 它的市值在 `post_trade.equity_amount` 里**彻底消失**（权益口径失真）。
        #    ⇒ 修法：ta_rejected 分支提前；**缺价时用账户快照价/市值兜底**
        #      ⇒ 仍是「维持、delta=0、不下单」，且市值**计入权益**。
        if act.get("ta_rejected"):
            _acc_mv = None
            for _p in (acc.get("positions") or []):
                if str(_p.get("code") or "").strip().zfill(6) == code:
                    _acc_mv = _f(_p.get("market_value"))
                    break
            target_shares = current_shares
            target_amount = (current_shares * price) if price is not None else _acc_mv
            delta_shares = 0
            est_amount = 0.0
            orders.append({
                "code": code, "name": name, "action": "维持",
                "current_shares": current_shares, "price": price,
                "price_date": price_date,
                "target_shares": target_shares, "delta_shares": delta_shares,
                "target_amount": target_amount, "est_amount": est_amount,
                "est_fee": 0.0, "est_slippage": 0.0,
                "capped": False,
                "cap_note": "🛡️ TA 一票否决 ⇒ 保持现仓，**不下单**",
                #: 🔴 缺价时按**账户快照市值**兜底 ⇒ positions 循环据此计入权益（不蒸发）
                "market_value": _acc_mv,
                "reason": str(act.get("reason") or ""),
            })
            continue
        if price is None:
            warnings.append(
                f"{code} {name}：缺价格（动作件与 prices 里都没有）⇒ 该行不参与金额/费用计算"
            )
            orders.append({
                "code": code, "name": name, "action": "无法定价（不执行）",
                "current_shares": current_shares, "price": None, "price_date": price_date,
                "target_shares": None, "delta_shares": None, "target_amount": None,
                "est_amount": None, "est_fee": None, "est_slippage": None,
                "capped": False, "cap_note": "", "reason": str(act.get("reason") or ""),
            })
            continue

        # 🔴 2026-11-17（缺陷：**执行卡 vs 计划件口径不一致** · 根因修复）
        #     研究链的动作行**已经**给出最终股数（``target_shares``）与股数差（``delta_shares``），
        #     执行卡却按 ``target_amount`` **重新推导**股数，再无脑套「单只金额上限」⇒
        #     把研究链明确「本轮不调仓」的持仓
        #     （存量微调 2292.5 元 < 2800 元门槛 ⇒ 维持 7700 股）**凭空砍成 6500 股**
        #     ⇒ 凭空生成 1200 股 / 1100.40 元的卖单
        #     （该笔金额本身也 < 2800 元存量微调门槛 ⇒ 双重违规）。
        #     ⇒ 修法（两条，缺一不可）：
        #       ① 行里有**明确的 ``target_shares``** ⇒ 以它为准
        #          （执行层**不得改写**研究链的股数决策；只做整手取整）；
        #       ② 「单只金额上限」**只约束建仓侧**（目标股数 > 当前股数 ⇒ 才可能触发）
        #          ⇒ 执行层**永远不反向削减存量持仓**（否则等于私自下减仓单）。
        explicit_target = _i(act.get("target_shares"))
        requested = _f(act.get("target_amount"))
        if requested is None and explicit_target is None:
            weight = _f(act.get("target_weight"))
            if weight is not None and total_assets is not None:
                requested = weight * total_assets
        if requested is None and explicit_target is None:
            warnings.append(
                f"{code} {name}：动作件里既没有 target_amount 也没有 target_weight"
                f" ⇒ 目标股数无法确定（记 None，不编数字）"
            )

        target_shares: int | None = None
        target_amount: float | None = None
        capped = False
        cap_note = ""
        if explicit_target is not None or requested is not None:
            if explicit_target is not None:
                target_shares = max(int(explicit_target // lot) * lot, 0)
            else:
                target_shares = max(int((requested / price) // lot) * lot, 0)
            target_amount = target_shares * price
            pre_cap_amount = target_amount
            #: ② 金额上限**只约束建仓侧** ⇒ 目标股数 ≤ 当前股数（维持 / 减仓）时**不封顶**
            if target_shares > current_shares and pre_cap_amount > max_amt + 1e-9:
                capped_shares = max(int((max_amt / price) // lot) * lot, 0)
                #: 封顶后**不得低于现仓** ⇒ 最多「本轮不建仓」，绝不反向变成减仓单
                capped_shares = max(capped_shares, current_shares)
                target_shares = capped_shares
                target_amount = target_shares * price
                capped = True
                cap_note = (
                    f"目标金额 {pre_cap_amount:.2f} 元 > 单只上限 {max_amt:.2f} 元"
                    f" ⇒ 股数按上限缩减到 {target_shares} 股（{target_amount:.2f} 元）"
                )

        one_lot_amount = price * lot
        #: 有效目标金额（显式股数 ⇒ 按股数折算；否则用动作件的 target_amount）
        effective_requested = requested
        if effective_requested is None and target_shares is not None:
            effective_requested = target_shares * price
        if (effective_requested is not None and effective_requested > 0 and current_shares == 0
                and one_lot_amount > max_amt + 1e-9):
            unbuildable.append({
                "code": code,
                "name": name,
                "action": "不可建仓",
                "target_shares": 0,
                "price": price,
                "price_date": price_date,
                "one_lot_amount": round(one_lot_amount, 2),
                "max_amount": max_amt,
                "reason": (
                    f"一手（{lot} 份 × {price:.4f} 元 = {one_lot_amount:.2f} 元）"
                    f"> 单只上限 {max_amt:.2f} 元 ⇒ 整手口径下不可建仓"
                    f" ⇒ 该席位归现金躺平（**不下单**）"
                ),
            })
            continue

        # 🔴 2026-11-25（口径归位：**被最低佣金门槛作废的新开仓** ⇒ 「不可建仓」，不许「维持」）
        #     研究链（`min_trade_gate.py`）已把这一行**就地作废**：
        #     `min_amount_skipped=True` / `min_amount_kind="新开仓"` /
        #     `action="不建仓"`（0 股时）/ `target_shares=0` / `delta_shares=0`。
        #     但执行卡**从不读 `act["action"]`** ⇒ 旧口径只按 `delta_shares` 重写成「持有」
        #     ⇒ 被门槛作废的席位被印进《无动作（维持）》表 ⇒ 读起来像「维持原仓」，
        #        实际是「本轮根本买不进」⇒ **口径说谎**。
        #     ⇒ 修法：命中「**0 股 + 门槛作废 + 新开仓**」⇒ **不进下单清单**，
        #       归入 `cash_flow.unbuildable_slots`（碎屑归现金），
        #       并把**披露**（阈值 / 被作废金额 / 门槛档位 / 原有 `lot_note`）原样带走。
        #     ⚠️ 硬不变量：
        #        ① 现金 / 权益 / 目标权重**零变化**（该行股数差本就为 0、金额为 0
        #           ⇒ 归现金 ⇒ 现金守恒；`shares=0` ⇒ 权益不变）；
        #        ② 「买不进」的标的**不得**出现在 `orders` 里；
        #        ③ `current_shares > 0` 的「持有」行（门槛作废但**有持仓** ⇒ 维持）
        #           **不得**被误分类（判据里的 `current_shares == 0` 就是这条）。
        _min_kind = str(act.get("min_amount_kind") or "")
        #: 判据里的 `delta_shares == 0`：此处股数差**还没算** ⇒ 用等价式
        #: 「目标股数 == 当前股数 == 0」（研究链作废时把 `target_shares` 回退到
        #: `current_shares`，两者都是 0 ⇒ 股数差必为 0）
        _zero_position = (current_shares == 0 and target_shares == 0)
        if (act.get("min_amount_skipped") is True and _min_kind == "新开仓"
                and _zero_position):
            _threshold = _f(act.get("min_amount_threshold"))
            _skipped_amount = _f(act.get("min_amount_skipped_amount"))
            _lot_note = str(act.get("lot_note") or "").strip()
            _why = (
                f"金额 {_skipped_amount:.2f} 元 < 门槛 {_threshold:.2f} 元"
                if (_skipped_amount is not None and _threshold is not None) else ""
            )
            _reason = "；".join([p for p in [
                f"🔴 最低佣金门槛作废（档位：{_min_kind}）",
                _why,
                "⇒ 本轮买不进（0 股建仓）⇒ 该席位归现金躺平（**不下单**）",
                _lot_note,
            ] if p])
            unbuildable.append({
                "code": code,
                "name": name,
                "action": "不可建仓",
                "target_shares": 0,
                "current_shares": current_shares,
                "price": price,
                "price_date": price_date,
                "one_lot_amount": round(one_lot_amount, 2),
                "max_amount": max_amt,
                #: 🔴 **披露不许丢**：门槛阈值 / 被作废金额 / 门槛档位 / 原有 `lot_note`
                "min_amount_threshold": _threshold,
                "min_amount_skipped_amount": _skipped_amount,
                "min_amount_kind": _min_kind,
                "lot_note": _lot_note,
                "reason": _reason,
            })
            continue

        # 🔴 2026-11-25（口径归位：**研究链自判「整手买不起」** ⇒ 「不可建仓」，不许「维持」）
        #     计划件（`final_rebalance_plan.py`）对「目标金额 < 一手金额」的席位**自己**判定：
        #       `action="不可建仓"` / `target_shares=0` / 权重归现金 / `lot_note` 写明原因。
        #     但执行卡**从不读 `act["action"]`** ⇒ 旧口径只按 `delta_shares` 把这一行
        #     重写成「持有」⇒ 被印进《无动作（维持）》表 ⇒ 读起来像「维持原仓」，
        #        实际是「本轮整手都买不起」⇒ **口径说谎**。
        #     ⇒ 修法：命中「**计划件标签=不可建仓 + 0 股 + 目标股数=0 + 有 lot_note**」
        #       ⇒ **不进下单清单**，归入 `cash_flow.unbuildable_slots`（碎屑归现金），
        #       并把**披露**（`lot_note` / 一手金额 / 单只上限）原样带走。
        #     ⚠️ 硬不变量：
        #        ① 现金 / 权益 / 目标权重**零变化**（该行股数差本就为 0、金额为 0
        #           ⇒ 归现金 ⇒ 现金守恒；`shares=0` ⇒ 权益不变）；
        #        ② 「买不进」的标的**不得**出现在 `orders` 里；
        #        ③ `current_shares > 0` 的真实持仓行**不得**被误分类
        #           （判据里的 `current_shares == 0` 就是这条）；
        #        ④ 与分支 A（单只金额上限）/ 分支 B（最低佣金门槛）**互斥**：本分支排在
        #           二者**之后** ⇒ A / B 命中即 `continue` ⇒ 互不覆盖。
        _plan_action = str(act.get("action") or "").strip()
        _lot_note = str(act.get("lot_note") or "").strip()
        if (_plan_action == "不可建仓" and current_shares == 0
                and target_shares == 0 and _lot_note):
            _reason = "；".join([p for p in [
                f"🔴 计划件判定「整手买不起」：{_lot_note}",
                "⇒ 整手口径下本轮买不进 ⇒ 该席位归现金躺平（**不下单**）",
            ] if p])
            unbuildable.append({
                "code": code,
                "name": name,
                "action": "不可建仓",
                "target_shares": 0,
                "current_shares": current_shares,
                "price": price,
                "price_date": price_date,
                "one_lot_amount": round(one_lot_amount, 2),
                "max_amount": max_amt,
                "lot_note": _lot_note,
                "reason": _reason,
            })
            continue

        delta_shares = (target_shares - current_shares) if target_shares is not None else None
        est_amount = delta_shares * price if delta_shares is not None else None
        if delta_shares is None:
            side = None
            label = "无法定量"
        elif delta_shares > 0:
            side = "buy"
            label = "买入"
        elif delta_shares < 0:
            side = "sell"
            label = "卖出"
        else:
            side = "hold"
            label = "持有"
        if est_amount is None:
            est_fee = None
            est_slippage = None
        elif est_amount == 0:
            est_fee = 0.0
            est_slippage = 0.0
        else:
            est_fee = _est_fee(est_amount, side)
            est_slippage = abs(est_amount) * float(DC.SLIPPAGE_RATE)

        orders.append({
            "code": code,
            "name": name,
            "action": label,
            "current_shares": current_shares,
            "price": price,
            "price_date": price_date,
            "target_shares": target_shares,
            "delta_shares": delta_shares,
            "target_amount": target_amount,
            "est_amount": est_amount,
            "est_fee": est_fee,
            "est_slippage": est_slippage,
            "capped": capped,
            "cap_note": cap_note,
            "reason": str(act.get("reason") or ""),
        })

    # ---- 现金流 ----
    scaled: list[dict] = []
    if cash is None:
        cash_flow: dict[str, Any] = {
            "cash_begin": None, "sell_proceeds": None, "sell_fees": None,
            "buy_amount": None, "buy_fees": None, "slippage": None, "cash_end": None,
            "deficit": None, "surplus": None, "available_before_buy": None,
            "headroom": None, "feasible": None, "unbuildable_slots": unbuildable,
            "scaled_down": scaled,
        }
        warnings.append(
            "⚠️ 账户缺 `cash` ⇒ 现金流与可行性全部记为 None（不编数字）")
    else:
        agg = _aggregate_cash(orders)
        available = (cash + agg["sell_proceeds"] - agg["sell_fees"] - agg["sell_slippage"])
        required = agg["buy_amount"] + agg["buy_fees"] + agg["buy_slippage"]
        cash_end = available - required
        if cash_end < buf_min_eff - 1e-9:
            # ---- 现金缓冲守卫：从金额最大的买入单开始减，直到现金 ≥ **生效下限** ----
            budget = max(0.0, available - buf_min_eff)
            for order in sorted(orders, key=lambda o: -(o["est_amount"] or 0.0)):
                if (order["delta_shares"] or 0) <= 0:
                    continue
                lots = int(order["delta_shares"] // lot)
                while lots > 0:
                    if _buy_cost(lots * lot * order["price"]) <= budget + 1e-9:
                        break
                    lots -= 1
                new_delta = lots * lot
                reduced = int(order["delta_shares"]) - new_delta
                if reduced > 0:
                    order["delta_shares"] = new_delta
                    order["target_shares"] = order["current_shares"] + new_delta
                    order["target_amount"] = order["target_shares"] * order["price"]
                    order["est_amount"] = new_delta * order["price"]
                    order["est_fee"] = _est_fee(order["est_amount"], "buy")
                    order["est_slippage"] = abs(order["est_amount"]) * float(DC.SLIPPAGE_RATE)
                    order["action"] = "买入" if new_delta > 0 else "持有"
                    order["reason"] = (
                        order["reason"] + f"；现金缓冲守卫（{buf_min_note}）：本轮缩减买入").strip("；")
                    scaled.append({
                        "code": order["code"], "name": order["name"],
                        "cut": True, "reduced_shares": reduced,
                    })
                budget -= _buy_cost(order["est_amount"] or 0.0)
            agg = _aggregate_cash(orders)
            available = (cash + agg["sell_proceeds"] - agg["sell_fees"] - agg["sell_slippage"])
            required = agg["buy_amount"] + agg["buy_fees"] + agg["buy_slippage"]
            cash_end = available - required
        deficit = max(0.0, required - available)
        surplus = max(0.0, available - required)
        feasible = bool(deficit <= 1e-9 and cash_end >= buf_min_eff - 1e-9)
        if not feasible:
            reasons = []
            if deficit > 1e-9:
                reasons.append(f"现金缺口 {deficit:.2f} 元")
            if cash_end < buf_min_eff - 1e-9:
                reasons.append(
                    f"调仓后现金 {cash_end:.2f} 元 < {buf_min_note}")
            warnings.append("🔴 执行卡**不可行**：" + "；".join(reasons))
        if cash_end > buf_max + 1e-9:
            # 🔴 2026-11-10：现金里**有一部分是规则要求的预留**（不是「多余资金」）
            #    ⇒ 告警必须把这两部分分开说，否则每张卡都在报假警。
            _reserve_note = (
                f"｜其中现金预留 {reserve_req:.2f} 元"
                f"（总资产 × {DC.CASH_RESERVE_RATIO:.0%}）是**规则要求**，不属于多余资金"
                if (reserve_req is not None and cash_end >= reserve_req - 1e-9) else ""
            )
            warnings.append(
                f"ℹ️ 调仓后现金 {cash_end:.2f} 元 > 缓冲上限 {buf_max:.2f} 元"
                f" ⇒ 多余资金躺平（**不为了花完现金而强行建仓**）{_reserve_note}"
            )
        cash_flow = {
            "cash_begin": cash,
            "sell_proceeds": round(agg["sell_proceeds"], 6),
            "sell_fees": round(agg["sell_fees"], 6),
            "buy_amount": round(agg["buy_amount"], 6),
            "buy_fees": round(agg["buy_fees"], 6),
            "slippage": round(agg["sell_slippage"] + agg["buy_slippage"], 6),
            "cash_end": round(cash_end, 6),
            "deficit": round(deficit, 6),
            "surplus": round(surplus, 6),
            "available_before_buy": round(available, 6),
            "headroom": round(surplus, 6),
            "feasible": feasible,
            "unbuildable_slots": unbuildable,
            "scaled_down": scaled,
        }

    # ---- 调仓后持仓 ----
    positions: list[dict] = []
    covered: set[str] = set()
    for order in orders:
        code = order["code"]
        covered.add(code)
        shares = order["target_shares"]
        if shares is None:
            continue
        #: 🔴 2026-11-11（缺陷 A·缺价角落）：「维持」行可能**缺价**
        #    ⇒ 没有行情价 ⇒ 用动作件里带的**账户快照市值**兜底
        #    ⇒ 市值照常计入权益（不许蒸发、也不许编价）。
        _price = order.get("price")
        market = (shares * _price) if _price is not None else (_f(order.get("market_value")) or 0.0)
        positions.append({
            "code": code,
            "name": order["name"],
            "shares": shares,
            "market_value": round(market, 2),
            "weight": (market / total_assets) if total_assets else None,
        })
    for pos in acc.get("positions") or []:
        code = str(pos.get("code") or "").strip().zfill(6)
        if code in covered:
            continue
        market = _f(pos.get("market_value"))
        positions.append({
            "code": code,
            "name": str(pos.get("name") or ""),
            "shares": _i(pos.get("shares")),
            "market_value": market,
            "weight": (market / total_assets) if (market is not None and total_assets) else None,
            "note": "本次动作未覆盖 ⇒ 按当前持仓计入（**未处置**，需人工确认）",
        })
    equity_amount = sum(p["market_value"] or 0.0 for p in positions)

    # ==================================================================
    # 🔴 2026-11-23（真机缺陷：**同标的双行冲突**）—— **两道硬断言（Fail-Closed）**
    #   断言①：**卖出股数 ≤ 期初可用股数**（按 code 汇总）⇒ 超出 ⇒ 直接 raise
    #   断言②：**cash_end ≤ total_assets + 1.0**（容忍 1 元浮点误差）⇒ 超出 ⇒ 直接 raise
    #   ⇒ 目的：从**根源**杜绝「账面虚增」这类畸变被执行卡带进实盘。
    #   ⇒ 落点在**执行卡构造之前** ⇒ 抛异常 ⇒ 执行卡**不予落盘** ⇒ 阶段失败（`--full` 硬失败）。
    #   ⚠️ 前提（运维约定）：跑批**之前**账户快照必须已刷新到**节点日收盘口径**
    #      （`daily_config.ACCOUNT_SNAPSHOT_DATE` / `TOTAL_ASSETS` / `CASH`）
    #      ⇒ 账户价 == 卡片价 ⇒ 断言②不会因隔夜价格漂移而误伤。
    # ==================================================================
    #: 期初可用股数：**账户快照优先**（权威）⇒ 账户没有该标的才回退到行动行自报值
    _acct_shares: dict[str, int] = {}
    for _pos in (acc.get("positions") or []):
        _c = str(_pos.get("code") or "").strip().zfill(6)
        _s = _i(_pos.get("shares"))
        if _c and _s is not None:
            _acct_shares[_c] = int(_s)

    _sell_by_code: dict[str, int] = {}
    _row_shares: dict[str, int] = {}
    for _o in orders:
        _c = str(_o.get("code") or "").strip().zfill(6)
        _d = _i(_o.get("delta_shares"))
        if not _c or _d is None:
            continue
        if int(_d) < 0:
            _sell_by_code[_c] = _sell_by_code.get(_c, 0) + int(_d)
        _cur = _i(_o.get("current_shares"))
        if _cur is not None:
            _row_shares[_c] = max(_row_shares.get(_c, 0), int(_cur))

    for _c in sorted(_sell_by_code):
        _sell_shares = -int(_sell_by_code[_c])
        _avail = _acct_shares.get(_c, _row_shares.get(_c))
        if _avail is None:
            continue
        if _sell_shares > int(_avail):
            raise ValueError(
                f"🔴 Fail-Closed 断言①失败：{_c} 卖出股数 {_sell_shares} > 期初可用股数 "
                f"{int(_avail)}（超卖 {_sell_shares - int(_avail)} 股）"
                f" ⇒ 执行卡**不予落盘**（多半是「同标的双行」没归并干净）"
            )

    _cash_end_check = _f(cash_flow.get("cash_end"))
    if _cash_end_check is not None and total_assets is not None:
        if _cash_end_check > total_assets + 1.0:
            raise ValueError(
                f"🔴 Fail-Closed 断言②失败：调仓后现金 {_cash_end_check:.2f} 元 > 总资产 "
                f"{total_assets:.2f} 元（虚增 {_cash_end_check - total_assets:.2f} 元）"
                f" ⇒ 执行卡**不予落盘**（多半是「同标的双行」重复计卖出）"
            )

    n_built = sum(1 for p in positions if (p["shares"] or 0) > 0)
    if n_built < n_target:
        warnings.append(
            f"ℹ️ 调仓后持仓 {n_built} 只 < 目标 {n_target} 只"
            f" ⇒ 缺口 {n_target - n_built} 席（归现金躺平）"
        )

    for source in (node_json, plan_json):
        if not source:
            continue
        for key in ("warnings", "risk_disclosure"):
            for item in source.get(key) or []:
                text = str(item)
                if text and text not in warnings:
                    warnings.append(text)

    cash_end_value = _f(cash_flow.get("cash_end"))
    card = {
        "asof": run_paths.normalize_date(asof),
        "execution_date": run_paths.normalize_date(execution_date),
        "execution_lag_days": int(DC.EXECUTION_LAG_DAYS),
        "execution_basis": EXECUTION_BASIS,
        "price_basis_note": PRICE_BASIS_NOTE,
        "account": {
            "total_assets": total_assets,
            "cash": cash,
            "cash_weight": (cash / total_assets) if (cash is not None and total_assets) else None,
            "market_value": market_value,
        },
        "orders": orders,
        #: 🔴 2026-11-23：**标的级终态归并**的逐条披露（审计要看得到「哪只被合并过」）
        "merge_disclosure": _merge_disp,
        "post_trade": {
            "cash": cash_end_value,
            "equity_amount": round(equity_amount, 2),
            "equity_weight": (equity_amount / total_assets) if total_assets else None,
            "positions": positions,
            "cash_buffer": cash_end_value,
            "cash_buffer_ok": (
                cash_end_value >= buf_min_eff - 1e-9 if cash_end_value is not None else None
            ),
            # 🔴 2026-11-10：现金**预留**口径（比例 / 比例下限金额 / 是否达标）
            "cash_reserve_ratio": float(DC.CASH_RESERVE_RATIO),
            "cash_reserve_required": (round(reserve_req, 2) if reserve_req is not None else None),
            "cash_reserve_ok": (
                cash_end_value >= reserve_req - 1e-9
                if (cash_end_value is not None and reserve_req is not None) else None
            ),
        },
        "cash_flow": cash_flow,
        "warnings": warnings,
        "disclaimer": DC.DISCLAIMER,
        "constraints": {
            "target_positions": n_target,
            "max_position_amount": max_amt,
            "lot_size": lot,
            "cash_buffer_min": buf_min,
            "cash_buffer_max": buf_max,
            # 🔴 2026-11-10：**生效下限**（绝对 vs 比例取大者）+ 现金预留比例
            "cash_buffer_min_eff": buf_min_eff,
            "cash_reserve_ratio": float(DC.CASH_RESERVE_RATIO),
            "commission_rate": float(DC.COMMISSION_RATE),
            "min_commission": float(DC.MIN_COMMISSION),
            "stamp_duty_rate": float(DC.STAMP_DUTY_RATE),
            "slippage_rate": float(DC.SLIPPAGE_RATE),
        },
    }
    # 🔴 **副作用开关**：``live_trade=False``（缺省）⇒ 观察卡（**挂单禁用**）
    card["live_trade"] = bool(live_trade)
    card["order_enabled"] = bool(live_trade)
    if not live_trade:
        # 🆕 **观察卡**：打上水印 + 禁用标记 ⇒ 任何下游/人工都不会误当实盘挂单
        card["observation_only"] = True
        card["disabled"] = True
        card["watermark"] = snap_mod.OBSERVATION_WATERMARK
        card["notice"] = (
            f"{snap_mod.OBSERVATION_WATERMARK}"
            "本卡仅用于观察当日 Alpha 与 ETF 排序演化，**不可下单**。"
        )
        for _o in card.get("orders") or []:
            _o["disabled"] = True
            _o["order_enabled"] = False
    if not node_reprice_ok:
        # 🔴 **节点日重算未完成 ⇒ 卡片不许带节点日股数/金额**。
        #    节点日重算的作用是「用节点日收盘价把股数与金额重算一遍」；
        #    拿不到节点日价时，剩下的只有**信号日口径**的数字
        #    ⇒ 那些数字**不是节点日数字** ⇒ 留在卡上就会被误读成节点日清单。
        #    ⇒ 处置：**数值全部抹成 None**（只留方向/目标权重），并显著告警。
        card["node_reprice_ok"] = False
        card["node_reprice_error"] = str(node_reprice_error or "")
        card["execution_basis"] = "plan_only（节点日重算未完成）"
        for _o in card.get("orders") or []:
            _o["node_reprice"] = "未完成"
            # 🔴 2026-11-09 补：`price` / `price_date`（**信号日**口径的参考价）一并抹成 None
            #    —— 探针 C11 的口径是「降级卡上**没有任何数字**」⇒ 留着参考价会让读者
            #    把「信号日价格」当成「节点日价格」⇒ 与降级的初衷（不冒充节点日）矛盾。
            for _k in ("target_shares", "delta_shares", "target_amount",
                       "est_amount", "est_fee", "est_slippage",
                       "price", "price_date"):
                _o[_k] = None
        _pt = card.get("post_trade")
        if isinstance(_pt, dict):
            for _k in list(_pt.keys()):
                _pt[_k] = None
        _cf = card.get("cash_flow")
        if isinstance(_cf, dict):
            for _k in list(_cf.keys()):
                _cf[_k] = None
        card["warnings"] = list(card.get("warnings") or []) + [
            "🔴 **节点日重算未完成** ⇒ 本卡**不含节点日股数/金额**"
            "（只剩方向与目标权重；**参考价与价日也一并置空** —— "
            "信号日价格**不是**节点日价格，留着会被误读成节点日清单）"
            + (f"｜原因：{node_reprice_error}" if node_reprice_error else "")
            + " ⇒ **不可下单**、**不得**当作节点日清单使用。"
        ]
    return card
