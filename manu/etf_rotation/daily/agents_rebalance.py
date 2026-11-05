"""agents_rebalance.py —— **双轨模式**的执行器（A 日常巡检 / B 调仓节点）。

【两条轨道是什么】
* **模式 A（日常巡检，2026-11-06 ~ 2026-11-30 静默防守）**：
  **只**检验唯一持仓 512800 现价与 MA60 的安全垫距离。
  正常 ⇒ 「趋势完好，守纪律躺平」；破位 ⇒ 紧急平仓指令。
  🔴 **模式 A 绝不跑 qlib 打分、绝不跑 TradingAgents、绝不产出任何新仓位指令** ——
  这是**构造性保证**：`run_mode_a` 的调用路径里根本没有打分 / 选股 / LLM 的入口
  （打分与 LLM 只在 `run_mode_b` 里被调用），所以「忘了加 if」也不会漏出新仓位指令。
* **模式 B（调仓信号日 2026-12-01 收盘出信号，2026-12-02 开盘执行）**：
  走研究链全链（分析报告 → TradingAgents 辩论 → 调仓计划 → 节点日重算）
  ⇒ 产出**确定性买卖执行卡片**。

【🔴 模式 B 的 S 层口径（主 agent 2026-11-05 核验后写入）】
1. `analyze.py --asof <信号日>` 会被 `data/signal_policy.py::check_signal_feasibility`
   的 **S1 硬拦**挡住：**信号日必须落在打分相位栅格上**（相位 = qlib 交易日历下标
   `i % every_n == every_n - 1`，`every_n = config.REBALANCE_EVERY_N_DAYS = 10`），
   而栅格末日 = qlib 数据集覆盖末日 ⇒ **2026-11-04 及以后（含 12-01）都不在栅格上**。
   ⇒ 模式 B 的分析日取「**最近一个可用的打分日**」= min(`stock_scores.csv` 的最大日期,
   栅格里 ≤ 信号日的最后一天)，**不写死**。
2. `qlib_scores.py` 的落盘是 `scores.to_csv(out_dir/"stock_scores.csv")`
   ⇒ **整体覆盖**、**不是增量**（拿旧的 69 个打分日当增量会全部丢失）
   ⇒ **默认路径绝对不跑它**（跑一次 ≈27 分钟且破坏 canonical 文件，属重活 + 破坏性）。
   本模块只**读** `stock_scores.csv`；是否需要刷新写进返回值的 `notes`。
   保留的 `refresh_scores=True` 开关必须满足：默认关、跑在**临时目录**、
   **先备份**再**合并**（不是覆盖）回 canonical、且跑前明确提示「约 27 分钟，属重活」。

【组合构造约束（研究链里没有的硬约束，本模块新增）】
* 目标资产**等权 5 份**，单只标的**金额上限 6,000 元**；
* 买卖数量**向下取整**到 100 股（手）的整数倍；
* 调仓后**必须保留 200~300 元现金缓冲**；
* **货基碎屑过滤**：场内货基（511880/511990 单价约 100 元/份，一手需 10,000 元）
  超出单标的 6,000 元限额 ⇒ **严禁下单**，自动归入可用现金躺平。

【铁律】
* 只读 `daily_config.py` / `account_state.py`（常量与账户基准的**单一事实源**）；
* 所有数字**来自实际输入**，缺字段 ⇒ 该字段为 `None` 并在 `warnings` 里点名（**不编数字**）；
* 每一步外部命令失败 ⇒ **把原始输出原样抛出**（不吞错误、不静默降级）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import account_state  # noqa: E402
import daily_config as DC  # noqa: E402
import mode_router  # noqa: E402

#: 研究链根目录（``manu/etf_rotation``）—— 导入研究链模块时按**绝对路径**加载
_RESEARCH_ROOT = Path(__file__).resolve().parents[1]
if str(_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_ROOT))

#: 模式 B 的执行卡固定文案
EXECUTION_BASIS = "信号日 T 收盘出信号 → 执行日 T+1 开盘执行"
PRICE_BASIS_NOTE = "节点日收盘价 ≠ 执行日成交价，实际股数/金额可能不同"

#: 模式 B 各步骤的名字（报错时要说清是哪一步挂了）—— 对应**六阶段全流程**
STEP_SCORES = "1/8 qlib_scores.py（阶段2 S层重算）"
STEP_EXPORT_PRICES = "2/8 export_qlib_prices.py（阶段2 个股日线导出）"
STEP_ANALYZE = "3/8 analyze.py（阶段3 E=W×S 穿透 + 均线过滤）"
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
# A. 模式 A —— 日常巡检（只查 512800 与 MA60 的距离）
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
    缺省路径读不到 ⇒ 返回空表 + 说明（⇒ 模式 A 走 ``insufficient_data``，不编数字）。
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


def run_mode_a(
    *,
    asof: object = None,
    prices_df: pd.DataFrame | None = None,
    prices_csv: str | Path | None = None,
    account: Any = None,
    run_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """模式 A：日常巡检 —— **只**看 512800 现价与 MA60 的距离。

    🔴 **构造性保证**：本函数**不会**调用打分 / 选股 / LLM（那些只在 `run_mode_b` 里），
    也不存在任何开仓分支 ⇒ 物理上不可能产出新仓位指令。

    返回
    ----
    dict，键：``mode, asof, status, holding, account, ma, action, email_kind, notes``
    （外加 ``run_dir`` / ``artifact``）。

    * ``status``：``"ok"``（趋势完好）/ ``"broken"``（破位）/ ``"insufficient_data"``；
    * ``status == "ok"`` ⇒ ``action`` **必须**是 ``None``；
    * ``status == "broken"`` ⇒ ``action`` 是紧急平仓指令（含股数 / 触发价 / MA60 / 跌破幅度）；
    * ``email_kind``：``"daily_ok"`` / ``"emergency_break"``。
    """
    day = mode_router.normalize_date(asof) or date.today().isoformat()
    notes: list[str] = []

    effective_account, acc_warnings = _resolve_account(account)
    notes.extend(acc_warnings)
    account_block, block_warnings = _account_block(effective_account)
    notes.extend(block_warnings)
    holding = _primary_holding(effective_account)

    frame, price_note = _load_price_frame(prices_df, prices_csv)
    if price_note:
        notes.append(f"⚠️ {price_note} ⇒ 无法计算均线 ⇒ 走 insufficient_data")

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
        email_kind = "daily_ok"
        notes.append(
            f"⚠️ **样本不足**：MA{DC.EXIT_MA_WINDOW} 需要 {DC.EXIT_MA_WINDOW} 根收盘价，"
            f"实际只有 {ma['n_obs']} 根 ⇒ **不做破位判定**（不编数字），"
            f"按「趋势完好」处理并如实披露"
        )
    elif close < ma60 * (1.0 - float(DC.BREAK_CONFIRM_RATIO)):
        status = "broken"
        email_kind = "emergency_break"
        distance = ma["distance_to_ma60_pct"]
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
                f"（参考价 {close:.4f} 元/股），成交后资金归集，"
                f"下一次敞口由 {DC.NEXT_SIGNAL_DATE} 的正式调仓决定"
            ),
        }
        notes.append(
            f"🔴 **紧急避险**：{DC.PRIMARY_HOLDING_CODE} 收盘 {close:.4f} < "
            f"MA{DC.EXIT_MA_WINDOW} {ma60:.4f}（{distance:+.2%}）⇒ 已生成紧急平仓指令"
        )
    else:
        status = "ok"
        email_kind = "daily_ok"
        distance = ma["distance_to_ma60_pct"]
        buffer_amount = ma["safety_buffer_amount"]
        buffer_txt = (
            f"、金额口径安全垫 {buffer_amount:,.2f} 元"
            if buffer_amount is not None else ""
        )
        notes.append(
            f"✅ **趋势完好，守纪律躺平**：收盘 {close:.4f} 高于 "
            f"MA{DC.EXIT_MA_WINDOW} {ma60:.4f}（安全垫 {distance:+.2%}{buffer_txt}）"
            f" ⇒ 本轮不做任何仓位调整"
        )

    notes.append(
        "🔴 模式 A 是**构造性保证**：本函数路径内没有打分 / 选股 / LLM 调用"
        " ⇒ 不会产出任何新仓位指令"
    )
    if DC.SILENT_DEFENSE_START <= day <= DC.SILENT_DEFENSE_END:
        notes.append(
            f"🔴 **静默防守窗口**（{DC.SILENT_DEFENSE_START} ~ {DC.SILENT_DEFENSE_END}）："
            f"下一个正式调仓信号日 = {DC.NEXT_SIGNAL_DATE}，此前只巡检不动手"
        )

    result: dict[str, Any] = {
        "mode": "A",
        "asof": day,
        "status": status,
        "holding": holding,
        "account": account_block,
        "ma": ma,
        "action": action,
        "email_kind": email_kind,
        "notes": notes,
        "generated_at": _now_iso(),
    }

    if run_dir is not None:
        out_dir = Path(run_dir)
        artifact = out_dir / f"mode_a_{day.replace('-', '')}.json"
        result["run_dir"] = str(out_dir)
        result["artifact"] = str(artifact)
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
    return result


# =============================================================================
# B. 模式 B —— 调仓节点（研究链全链 → 确定性执行卡）
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
            f"模式 B 第 {step} 步的解释器/脚本不存在：{printable}（{exc}）⇒ 拒绝继续"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"模式 B 第 {step} 步超时（>{timeout} 秒）：{printable}\n"
            f"--- 部分 stdout ---\n{exc.stdout or ''}\n--- 部分 stderr ---\n{exc.stderr or ''}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"模式 B 第 {step} 步失败（退出码 {proc.returncode}）\n"
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
    candidates = [Path(provider_uri) / "calendars" / "day.txt"]
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
        notes.append(
            f"🔴 **S 层打分日 = {analysis_asof} ≠ 信号日 {signal_date}**"
            + (f"（打分栅格里 ≤ 信号日的最后一天 = {grid_last}）" if grid_last else "")
            + (f"；当前 qlib 数据集覆盖末日 = {dataset_max}" if dataset_max else "")
            + " ⇒ **信号日当天的个股 Alpha 拿不到**（数据集没覆盖到信号日 ⇒ "
            "阶段 2 只能打到数据集末日）。本轮**节点日只重算股数/金额**。"
            "要把打分推到信号日，必须先让**阶段 1** 把 qlib 数据集刷新到信号日之后"
            "（滚动快照换库），再重跑阶段 2。"
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


def _export_stock_prices(analysis_asof: str, notes: list[str]) -> Path | None:
    """**阶段 2b**：导出全市场个股日线（供 ``analyze.py`` 的多日 RankIC 用）。

    🔴 为什么必须放在 S 层刷新**之后**：``analyze.py`` 的 N=5 / N=20 交易日 RankIC
    需要「个股前瞻收益」，价格源是 ``stock_prices.csv``；若 S 层刚重算而价格没重导，
    RankIC 会拿**旧价**去配**新分数** ⇒ 口径错配。
    """
    out = Path(DC.RESEARCH_RUN_ROOT) / "stock_prices.csv"
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


def _refresh_scores(*, scores_path: Path, notes: list[str], end_date: str | None = None) -> None:
    """重跑 S 层（**阶段 2**：全市场个股 Alpha 推断）。

    🔴 主 agent 2026-11-05 核验：``qlib_scores.py`` 的落盘是
    ``scores.to_csv(out_dir/"stock_scores.csv")`` ⇒ **整体覆盖**、**不是增量**
    ⇒ 拿旧的 69 个打分日当增量会把它们**全部抹掉**。
    ⇒ 本函数必须做到四件事：① 跑在**临时目录**；② 跑之前**备份** canonical；
    ③ 跑完**合并**（不是覆盖）回 canonical；④ 明确提示「约 27 分钟，属重活」。

    🔴 **2026-11-05 新增 ``end_date``**：不传 ``--end`` 时 ``qlib_scores.py`` 会用
    ``config.SCORE_END_TIME``（数据集末日）**静默截断** ⇒ 调仓日拿到的仍是旧分数。
    传了 ``--end <信号日>`` 才会打到信号日；脚本内部还会按**数据集真实覆盖末日**
    封顶并在超限时**显式告警**（不静默）。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = scores_path.parent / f"_scores_refresh_{stamp}"
    backup = scores_path.parent / f"{scores_path.name}.bak-{stamp}"
    start = _scores_max_date(scores_path)
    print("[WARN] 🔴 即将重跑 S 层：这一步约 27 分钟，属**重活**（铁律一：需用户当轮授权）。")
    notes.append(
        "🔴 **已重跑 S 层**（阶段 2，约 27 分钟的重活）：跑在临时目录 → "
        "备份 canonical → **合并**（不是覆盖）回 canonical"
        + (f"；打分截止日 = {end_date}" if end_date else "")
    )
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cmd = [DC.QL_PY, str(_RESEARCH_ROOT / "qlib_scores.py"), "--mode", "infer",
           "--out-dir", str(tmp_dir)]
    if start:
        cmd += ["--start", start]
    if end_date:
        cmd += ["--end", str(end_date)]
    _run_cmd(cmd, step=STEP_SCORES)
    produced = tmp_dir / "stock_scores.csv"
    if not produced.exists():
        raise FileNotFoundError(f"重跑 S 层后产物不存在：{produced}")
    if scores_path.exists():
        shutil.copy2(scores_path, backup)
        notes.append(f"ℹ️ canonical 个股分数已备份到 {backup}")
    old = (pd.read_csv(scores_path, dtype={"instrument": str})
           if scores_path.exists() else None)
    new = pd.read_csv(produced, dtype={"instrument": str})
    merged = pd.concat([old, new], ignore_index=True) if old is not None else new
    merged["date"] = pd.to_datetime(merged["date"])
    merged = merged.drop_duplicates(subset=["date", "instrument"], keep="last")
    merged = merged.sort_values(["date", "instrument"]).reset_index(drop=True)
    merged.to_csv(scores_path, index=False)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    new_max = _scores_max_date(scores_path)
    notes.append(
        f"✅ S 层已合并回 canonical：最新打分日 = {new_max or '—'}"
        f"（合并前 {start or '—'}；按 (date, instrument) 去重，keep=last）"
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


def _stage_recommendation(run_path: Path, notes: list[str]) -> None:
    """把研究链的 ``rebalance_recommendation.json`` **只读复制**到本次运行目录。

    为什么需要：``build_ta_context.py`` 读 ``<run-root>/rebalance_recommendation.json``
    （规则层推荐件，由 ``recommend.py`` 产出）⇒ 每日运行目录里没有它 ⇒ 步骤 3 会失败。
    该件是**研究链的规则层事实** ⇒ 直接复用（只读），不重跑（重跑 = 另一条重活）。
    ⚠️ 口径披露：推荐件里的账户数字是**研究链基准**（2026-11-04），
    不是实盘基准（2026-11-05）⇒ TA 上下文里的账户段以执行卡的 ``account`` 块为准。
    """
    target = run_path / "rebalance_recommendation.json"
    if target.exists():
        return
    source = Path(DC.RESEARCH_RUN_ROOT) / "rebalance_recommendation.json"
    if not source.exists():
        notes.append(f"⚠️ 研究链规则层推荐件不存在（{source}）⇒ build_ta_context 可能失败")
        return
    shutil.copy2(source, target)
    notes.append(
        f"ℹ️ 规则层推荐件从 {source} **只读复制**到 {target}（供 build_ta_context 消费）；"
        f"⚠️ 该件的账户数字是**研究链基准**（2026-11-04），不是实盘基准（2026-11-05）"
        f" ⇒ 辩论里的账户口径以执行卡的 account 块为准"
    )


def run_mode_b(*, asof: object = None, run_dir: str | Path | None = None,
               prices_csv: str | Path | None = None, scores_csv: str | Path | None = None,
               calendar_path: str | Path | None = None, dry_run: bool = False,
               skip_llm: bool = False, llm_timeout: float | None = None,
               reasoning_effort: str = "",
               refresh_scores: bool = True) -> dict:
    """模式 B：调仓节点 —— 研究链全链 + 确定性执行卡片。

    参数
    ----
    llm_timeout:
        阶段 4（``run_tradingagents.py``）的墙钟超时（秒）。
        **缺省读 `daily_config.LLM_TIMEOUT`**（默认 5400 秒）
        ⇒ 🔴 2026-11-05 实测：``reasoning_effort=max`` 时 MiniMax 单次调用 185 秒，
        多智能体图要调十几次 ⇒ 原 1800 秒**必然超时**（实测 15:52→16:22 被打断）。
    reasoning_effort:
        阶段 4 的 ``reasoning_effort`` 覆盖（``max`` / ``high`` / ``medium`` / ``low``）。
        **缺省空串 = 不覆盖** ⇒ 沿用 ``TradingAgents/.env`` 里现役的 ``max``。
        非空时通过子进程 env ``TRADINGAGENTS_REASONING_EFFORT`` 下发（**不改 .env**）。

    执行链 = **六阶段全流程**（**每一步都能单独失败，原始报错原样抛出**）：

    0. （阶段 1 由 ``main.py`` 负责：AkShare 盘后取数 + qlib 数据集滚动快照换库。）
    1. **阶段 2 · S 层新鲜度**：``refresh_scores=True``（**默认**）⇒ 跑
       ``qlib_scores.py --mode infer --end <信号日>``（约 27 分钟）；
       它是**整体覆盖**、不是增量 ⇒ 本函数让它跑在**临时目录** + 先备份 canonical +
       再按 ``(date, instrument)`` **合并**（keep=last）回 canonical。
       ``refresh_scores=False`` 时**只读** ``stock_scores.csv``（沿用旧分数，
       并在 ``notes`` 里**显式披露**分数末日 ≠ 信号日）。
    2. **阶段 2b · 个股日线导出**（``export_qlib_prices.py``）：``analyze.py`` 的多日
       RankIC（N=5 / N=20）要全市场个股收盘价 ⇒ S 层刷新后**必须重导**，否则用的是旧价。
    3. **阶段 3 · ``analyze.py``**：``E = W × S`` 穿透聚合 + MA20/MA60 均线过滤；
       注入**实盘账户基准**（``--total-assets`` / ``--cash`` / ``--account-snapshot``），
       分析日 = **最近可用打分日**（见 `_resolve_analysis_asof`）。
    4. **阶段 4 · ``build_ta_context.py``** → ``ta_research_context.md`` + ``ta_context_meta.json``。
    5. **阶段 4 · ``run_tradingagents.py``**（**llm 解释器**）：产出
       ``tradingagents_decision.md/.json``；🔴 **json 的 ``status`` 字段才是权威**
       （失败时 md 也会写占位件）。
    6. **阶段 5 · ``final_rebalance_plan.py``**：产出 ``final_rebalance_plan.json``。
    7. **阶段 5 · ``reprice_node_day.py``**：用**节点日收盘价**重算股数 ⇒
       ``node_reprice_<日期>.json``。
    8. **阶段 6 · 组装执行卡片**（见 `build_execution_card`）。

    返回
    ----
    dict，键：``mode, asof, execution_date, run_dir, report_json, plan_json, node_json,
    card, email_kind, notes``（外加 ``analysis_asof`` / ``scores_max_date`` /
    ``score_grid_last`` / ``qlib_dataset_max_date`` / ``llm_decision_status`` /
    ``execution_lag_days`` 等审计键）。
    """
    signal_date = mode_router.normalize_date(asof) or DC.NEXT_SIGNAL_DATE
    notes: list[str] = []
    run_path = Path(run_dir) if run_dir is not None else (
        Path(DC.RUN_ROOT) / signal_date.replace("-", "")
    )
    prices_path = Path(prices_csv) if prices_csv is not None else Path(DC.ETF_PRICES_CSV)
    scores_path = Path(scores_csv) if scores_csv is not None else Path(DC.STOCK_SCORES_CSV)
    universe_path = Path(DC.ETF_UNIVERSE_CSV)
    snapshot_path = run_path / "account_snapshot.csv"
    account_json_path = run_path / "account_context.json"
    report_json = run_path / "etf_analysis_report.json"
    plan_json = run_path / "final_rebalance_plan.json"
    node_json = run_path / f"node_reprice_{signal_date.replace('-', '')}.json"
    decision_json = run_path / "tradingagents_decision.json"
    decision_md = run_path / "tradingagents_decision.md"
    execution_date = DC.NEXT_EXECUTION_DATE if signal_date == DC.NEXT_SIGNAL_DATE else None
    # 🔴 阶段 4 超时缺省读 `daily_config.LLM_TIMEOUT`（默认 5400 秒）
    #    ⇒ 不再写死 1800（2026-11-05 实测：effort=max 时单次调用 185 秒 ⇒ 1800 必超时）
    llm_timeout = _resolve_llm_timeout(llm_timeout)
    # 🔴 阶段 4 的 reasoning_effort 覆盖：空串 = **不覆盖**（沿用 .env 现役值）
    llm_env_overrides: dict[str, str] = {}
    if reasoning_effort:
        llm_env_overrides["TRADINGAGENTS_REASONING_EFFORT"] = str(reasoning_effort)

    if dry_run:
        analysis = _resolve_analysis_asof(
            signal_date=signal_date, scores_path=scores_path, calendar_path=calendar_path)
        notes.extend(analysis["notes"])
        notes.append("🧪 **dry-run**：未执行任何外部命令、未写入任何产物（仅做前置判定）")
        return {
            "mode": "B",
            "asof": signal_date,
            "execution_date": execution_date,
            "run_dir": str(run_path),
            "report_json": None,
            "plan_json": None,
            "node_json": None,
            "card": None,
            "email_kind": "rebalance_card",
            "notes": notes,
            "analysis_asof": analysis["analysis_asof"],
            "scores_max_date": analysis["scores_max_date"],
            "score_grid_last": analysis["grid_last"],
            "qlib_dataset_max_date": analysis["dataset_max_date"],
            "dry_run": True,
        }

    run_path.mkdir(parents=True, exist_ok=True)
    account_state.write_snapshot_csv(snapshot_path)
    notes.append(
        f"ℹ️ 实盘账户快照已生成：{snapshot_path}"
        f"（口径 = {DC.ACCOUNT_SNAPSHOT_DATE} 用户基准：总资产 {DC.TOTAL_ASSETS} 元 / "
        f"现金 {DC.CASH} 元）"
    )
    # 🔴 阶段 4 的账户口径（**实盘**）：导出成 JSON 传给 `run_tradingagents.py --account-json`
    #    ⇒ 修「Portfolio Manager 拿研究链的 5 只持仓组合做推理」这个正确性 bug。
    #    🔴 研究链的 `data/account_snapshot.csv` **不动**（那是研究链的输入真值）。
    account_state.write_account_json(account_json_path)
    notes.append(
        f"✅ 阶段 4 账户上下文（实盘口径）已导出：{account_json_path}"
        f"（总资产 {DC.TOTAL_ASSETS} 元 / 现金 {DC.CASH} 元 / "
        f"唯一底仓 {DC.PRIMARY_HOLDING_CODE} {DC.PRIMARY_HOLDING_SHARES} 股）"
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
            f"⚠️ 本轮**未重跑 S 层**（refresh_scores=False）⇒ 沿用 canonical 里的旧分数"
            f"（最新打分日 {analysis.get('scores_max_date') or '—'}）"
        )

    # ---- 2b. export_qlib_prices.py（阶段 2b：个股日线，供 analyze.py 的多日 RankIC）----
    stock_prices_path = _export_stock_prices(analysis_asof, notes)

    # ---- 3. analyze.py（阶段 3：E=W×S 穿透 + MA20/MA60 过滤）----
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
    ] + (["--stock-prices", str(stock_prices_path)] if stock_prices_path else []), step=STEP_ANALYZE)
    notes.append(f"✅ 分析报告已产出：{report_json}（分析日 = {analysis_asof}）")

    # ---- 3/4. TradingAgents 阶段 ----
    llm_status = "skipped"
    if skip_llm:
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
                "ℹ️ skip_llm=True：复用既有决策件（--render-only 重渲 md，零 API 成本）"
            )
        else:
            llm_status = "absent"
            notes.append(
                "ℹ️ skip_llm=True 且**没有**既有决策件 ⇒ 本轮无 LLM 第二意见（只有规则层）"
            )
    else:
        _stage_recommendation(run_path, notes)
        _run_cmd([
            DC.QL_PY, str(_RESEARCH_ROOT / "llm" / "build_ta_context.py"),
            "--run-root", str(run_path),
        ], step=STEP_CONTEXT)
        notes.append(f"✅ TA 上下文已产出：{run_path / 'ta_research_context.md'}")
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
                f"模式 B 第 {STEP_TRADINGAGENTS} 步失败：决策 json 的 status = {llm_status!r}"
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

    # ---- 7. 执行卡片 ----
    node_payload = json.loads(node_json.read_text(encoding="utf-8"))
    plan_payload = json.loads(plan_json.read_text(encoding="utf-8"))
    card = build_execution_card(
        actions=node_payload.get("actions") or [],
        account=account_state.account_summary(),
        asof=signal_date,
        execution_date=(node_payload.get("meta", {}).get("execution_date") or execution_date),
        node_json=node_payload,
        plan_json=plan_payload,
    )
    notes.append(
        f"✅ 执行卡片已组装：{len(card.get('orders') or [])} 条动作"
        f"｜{PRICE_BASIS_NOTE}"
    )

    # ---- 执行卡落盘（🔴 审计用：信号日决策必须留一份**可复查**的执行卡） ----
    card_path = persist_execution_card(card, run_path, signal_date)
    notes.append(f"✅ 执行卡片已落盘：{card_path}")

    return {
        "mode": "B",
        "asof": signal_date,
        "execution_date": card.get("execution_date"),
        "run_dir": str(run_path),
        "report_json": str(report_json),
        "plan_json": str(plan_json),
        "node_json": str(node_json),
        "card_json": str(card_path),
        "card": card,
        "email_kind": "rebalance_card",
        "notes": notes,
        "analysis_asof": analysis_asof,
        "scores_max_date": analysis["scores_max_date"],
        "score_grid_last": analysis["grid_last"],
        "qlib_dataset_max_date": analysis["dataset_max_date"],
        "llm_decision_status": llm_status,
        "execution_lag_days": int(DC.EXECUTION_LAG_DAYS),
        "generated_at": _now_iso(),
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
                         cash_buffer_max: float | None = None) -> dict:
    """把研究链的动作表折算成**可下单的执行卡片**（含研究链没有的硬约束）。

    硬约束（全部来自 ``daily_config``）：

    * 目标股数**向下取整**到整手（``lot_size``），不能为负 ⇒ 归 0；
    * 单只标的**金额上限**（``max_amount``）⇒ 超了按上限缩减股数（仍取整手），
      并记 ``capped=True`` + ``cap_note``；
    * **货基碎屑过滤**：一手金额 > 上限且当前无持仓 ⇒ ``action="不可建仓"``、
      目标股数归 0、计入 ``cash_flow.unbuildable_slots``、**不进下单清单**；
    * **现金缓冲守卫**：调仓后现金必须 ≥ ``cash_buffer_min`` ⇒ 不够就从**金额最大的
      买入单**开始减；减完仍不够 ⇒ ``feasible=False`` + 明确 reason。

    参数
    ----
    actions : 动作表（``node_reprice`` 优先，其次 ``final_rebalance_plan`` 的 ``actions[]``）；
        给 ``None`` ⇒ 从 ``node_json`` / ``plan_json`` 里取。
    account : 账户（总资产 / 现金 / 持仓）；缺字段 ⇒ 该字段为 ``None`` 并在 warnings 点名。

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
    total_assets = _f(acc.get("total_assets"))
    cash = _f(acc.get("cash"))
    market_value = _f(acc.get("market_value"))
    if total_assets is None:
        warnings.append("账户缺 `total_assets` ⇒ 权重类字段记为 None（不编数字）")
    if market_value is None and total_assets is not None and cash is not None:
        market_value = total_assets - cash

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

        requested = _f(act.get("target_amount"))
        if requested is None:
            weight = _f(act.get("target_weight"))
            if weight is not None and total_assets is not None:
                requested = weight * total_assets
        if requested is None:
            warnings.append(
                f"{code} {name}：动作件里既没有 target_amount 也没有 target_weight"
                f" ⇒ 目标股数无法确定（记 None，不编数字）"
            )

        target_shares: int | None = None
        target_amount: float | None = None
        capped = False
        cap_note = ""
        if requested is not None:
            target_shares = int((requested / price) // lot) * lot
            target_shares = max(target_shares, 0)
            target_amount = target_shares * price
            if target_amount > max_amt + 1e-9:
                capped_shares = max(int((max_amt / price) // lot) * lot, 0)
                target_shares = capped_shares
                target_amount = target_shares * price
                capped = True
                cap_note = (
                    f"目标金额 {requested:.2f} 元 > 单只上限 {max_amt:.2f} 元"
                    f" ⇒ 股数按上限缩减到 {target_shares} 股（{target_amount:.2f} 元）"
                )

        one_lot_amount = price * lot
        if (requested is not None and requested > 0 and current_shares == 0
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
        if cash_end < buf_min - 1e-9:
            # ---- 现金缓冲守卫：从金额最大的买入单开始减，直到现金 ≥ 下限 ----
            budget = max(0.0, available - buf_min)
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
                        order["reason"] + "；现金缓冲守卫：本轮缩减买入").strip("；")
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
        feasible = bool(deficit <= 1e-9 and cash_end >= buf_min - 1e-9)
        if not feasible:
            reasons = []
            if deficit > 1e-9:
                reasons.append(f"现金缺口 {deficit:.2f} 元")
            if cash_end < buf_min - 1e-9:
                reasons.append(
                    f"调仓后现金 {cash_end:.2f} 元 < 缓冲下限 {buf_min:.2f} 元")
            warnings.append("🔴 执行卡**不可行**：" + "；".join(reasons))
        if cash_end > buf_max + 1e-9:
            warnings.append(
                f"ℹ️ 调仓后现金 {cash_end:.2f} 元 > 缓冲上限 {buf_max:.2f} 元"
                f" ⇒ 多余资金躺平（**不为了花完现金而强行建仓**）"
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
        market = shares * order["price"]
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
    return {
        "asof": mode_router.normalize_date(asof),
        "execution_date": mode_router.normalize_date(execution_date),
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
        "post_trade": {
            "cash": cash_end_value,
            "equity_amount": round(equity_amount, 2),
            "equity_weight": (equity_amount / total_assets) if total_assets else None,
            "positions": positions,
            "cash_buffer": cash_end_value,
            "cash_buffer_ok": (
                cash_end_value >= buf_min - 1e-9 if cash_end_value is not None else None
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
            "commission_rate": float(DC.COMMISSION_RATE),
            "min_commission": float(DC.MIN_COMMISSION),
            "stamp_duty_rate": float(DC.STAMP_DUTY_RATE),
            "slippage_rate": float(DC.SLIPPAGE_RATE),
        },
    }
