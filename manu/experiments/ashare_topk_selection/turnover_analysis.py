"""换手率 / 交易成本分解：量化 TopkDropoutStrategy(topk=30, n_drop=5) 的换手，
并解释 manifest 里「不含成本年化超额 +6.40% vs 含成本年化超额 +0.35%」这 6.05pp 落差的来源。

【口径（与 config.py 完全一致；回测用 qlib 真实 TopkDropoutStrategy + Exchange）】
  - 信号：run 目录里的 pred.pkl（模型在 test 段的打分）—— **不重训**，直接当 signal
  - 区间：config.TEST = (2024-01-01, 2026-11-03)，account / benchmark / exchange_kwargs 全部取 config
  - 换手定义：
        w_i(t)      = 该标的市值(t) / 组合总资产(t)          （股票市值 + 现金）
        单边换手率(t) = 0.5 * Σ_i |w_i(t) - w_i(t-1)|
    首日没有前一日持仓 ⇒ 不参与换手差分；建仓日（第一笔建仓）单独标注。
  - 年化：单边换手率 × 250 交易日/年（A 股口径）
  - 成本对账：qlib risk_analysis 的日频年化因子是 **238**（qlib/contrib/evaluate.py:56
    NORM_FREQ_DAY=238），manifest 的两个年化超额就是用它算的 ⇒ 与 manifest 对账**必须**用 238，
    用 250 对不上是口径错，不是数字错。

【产物】<run_dir>/turnover.json：换手定义式 + 统计量 + 分年 + 成本对账 + 输入/脚本 md5 + 生成时间。
【断言】交易日数、每日持仓数、换手率量级、回测复现 manifest 的年化超额 —— 任一不成立即 raise，
退出码非 0，**不落盘**（不许静默产出）。

用法（服务器）：
    python manu/experiments/ashare_topk_selection/turnover_analysis.py \
        --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
from determinism import apply_deterministic_position_ordering  # noqa: E402

# ------------------------------------------------------------------ 常量（显式声明，禁止散落硬编码）

#: A 股年化换手口径：一年 250 个交易日
TRADING_DAYS_PER_YEAR = 250

#: qlib risk_analysis 的日频年化因子（qlib/contrib/evaluate.py:56, NORM_FREQ_DAY=238）。
#: manifest 的「年化超额」用它算 ⇒ 对账 6.05pp 必须用 238。
QLIB_DAILY_ANNUALIZATION_DAYS = 238

#: 回测复现 manifest 年化超额时允许的相对误差（同一份 pred.pkl + 同一套 config ⇒ 应当完全一致）
MANIFEST_REPRO_REL_TOL = 1e-6

#: 自算双边换手 vs qlib report_normal["turnover"] 的相对误差上限（两者应来自同一笔累计器）
TURNOVER_CROSSCHECK_REL_TOL = 1e-6

#: 断言阈值（可审计；超出即 raise）
MIN_TRADING_DAYS = 600          # test 段约 685 个交易日，下界留 12% 余量
MAX_TRADING_DAYS = 800
HOLDING_MEDIAN_TOL = 5          # 每日持仓数中位数相对 topk 的允许偏离
HOLDING_P05_TOL = 10            # p05 允许低于 topk 的幅度
HOLDING_MAX_TOL = 20            # 允许高于 topk 的幅度（碎股残留）
TURNOVER_DAILY_MIN = 0.01       # 稳态日均单边换手率下界
TURNOVER_DAILY_MAX = 0.60       # 稳态日均单边换手率上界
ANNUALIZED_SINGLE_SIDE_MAX = 150.0   # 年化单边换手倍数上限

#: 回测频次 key（portfolio_metric_dict / indicator_dict 的键 = "{count}{freq}"，
#: 由 qlib.backtest.backtest.collect_data_loop 用 Freq.parse(time_per_step) 生成 ⇒ 日频是 "1day"）
BACKTEST_FREQ_KEY = "1day"
BACKTEST_TIME_PER_STEP = "day"

#: 确定性补丁生效状态（运行期记录，写进产物 JSON 供审计）
_DETERMINISM_STATUS: dict[str, Any] = {}


# ------------------------------------------------------------------ 通用工具


def md5_of(path: Path) -> str:
    """文件 md5（铁律：同步判据只能靠 md5 双侧比对）。"""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jsonable(obj: Any) -> Any:
    """把 numpy / pandas / Timestamp 转成可 JSON 序列化的原生类型。"""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return jsonable(obj.tolist())
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TopkDropoutStrategy 换手率 / 交易成本分解（不含成本 vs 含成本年化超额对账）",
    )
    p.add_argument(
        "--run-dir",
        default=None,
        help="产物目录 runs/<recorder_id>（含 manifest.json / pred.pkl）；不给则取 config.latest_run_dir()",
    )
    p.add_argument("--out", default=None, help="输出 JSON 路径（默认 <run_dir>/turnover.json）")
    p.add_argument(
        "--self-test",
        action="store_true",
        help="只跑纯 pandas 的换手率合成数据单测（不碰 qlib / 数据），用于本地验证",
    )
    return p.parse_args()


def resolve_run_dir(explicit: str | None) -> Path:
    """定位产物目录：优先 --run-dir，否则 config.latest_run_dir()。"""
    if explicit:
        run_dir = Path(explicit).expanduser()
        if not run_dir.is_dir():
            raise FileNotFoundError(f"--run-dir 指定的目录不存在或不是目录: {run_dir}")
        return run_dir

    latest_fn = getattr(C, "latest_run_dir", None)
    if latest_fn is None:
        raise RuntimeError("config 里没有 latest_run_dir() —— 请确认 config.py 已是最新版本")
    run_dir = latest_fn()
    if run_dir is None:
        runs_dir = Path(getattr(C, "RUNS_DIR", Path(C.EXP_ROOT) / "runs"))
        raise FileNotFoundError(f"{runs_dir} 下没有含 manifest.json 的 run 目录 —— 先跑 run_pipeline.py")
    return Path(run_dir)


def resolve_pred_pkl(run_dir: Path) -> Path:
    """在 run 目录里取 pred.pkl；缺失时给出可行动的报错。"""
    pred_path = run_dir / "pred.pkl"
    if pred_path.is_file():
        return pred_path
    if (run_dir / "manifest.json").is_file():
        raise FileNotFoundError(f"{run_dir} 有 manifest.json 但没有 pred.pkl —— run_pipeline 尚未产出信号")
    raise FileNotFoundError(f"{run_dir} 下既没有 pred.pkl 也没有 manifest.json —— 该目录不是产物目录")


def load_manifest(run_dir: Path) -> dict:
    """读取 manifest.json（缺失时返回空 dict）。"""
    path = run_dir / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 换手计算（纯 pandas，可本地单测）


def compute_turnover_series(weight_df: pd.DataFrame) -> pd.Series:
    """逐日单边换手率 = 0.5 * Σ_i |w_i(t) - w_i(t-1)|。

    输入 weight_df：index = 交易日(datetime)，columns = 标的(instrument)，
    值 = 该标的市值 / 组合总资产（股票市值 + 现金）。缺列 / 缺行一律视为 0。
    输出：与输入同 index 的 Series；**首行为 NaN**（没有前一日持仓，差分无定义）。

    🔴 该函数不依赖 qlib —— 纯 pandas 实现，便于用合成数据单测。
    """
    if weight_df.empty:
        raise ValueError("weight_df 为空，无法计算换手率")
    weights = weight_df.sort_index().astype(float).fillna(0.0)
    prev = weights.shift(1).fillna(0.0)
    turnover = 0.5 * (weights - prev).abs().sum(axis=1)
    turnover.iloc[0] = np.nan
    turnover.name = "turnover_single_side"
    return turnover


def self_test_turnover() -> None:
    """合成数据单测：2 天持仓变动，换手率必须等于手算期望值。

    第 1 天：{A: 0.50, B: 0.50}；第 2 天：{A: 0.50, C: 0.50}
    手算 day2：0.5 * (|0.50-0.50| + |0.00-0.50| + |0.50-0.00|) = 0.5 * 1.00 = 0.50
    """
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    weights = pd.DataFrame(
        {"A": [0.50, 0.50], "B": [0.50, 0.00], "C": [0.00, 0.50]},
        index=index,
    )
    got = compute_turnover_series(weights)
    assert np.isnan(got.iloc[0]), f"首行应为 NaN，实际 {got.iloc[0]!r}"
    expected = 0.50
    assert abs(float(got.iloc[1]) - expected) < 1e-12, f"day2 换手率期望 {expected}，实际 {got.iloc[1]!r}"
    print(f"[self-test] 单边换手率合成数据单测通过：day2 期望 {expected} == 实际 {float(got.iloc[1])}")


# ------------------------------------------------------------------ 回测（qlib 真实 TopkDropoutStrategy）


def init_qlib() -> None:
    """初始化 qlib（与 run_pipeline.py 同口径：provider_uri / region / exp_manager）。"""
    import qlib

    qlib.init(
        provider_uri=C.PROVIDER_URI,
        region=C.REGION,
        exp_manager={
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {"uri": C.RECORDER_URI, "default_exp_name": C.EXPERIMENT_NAME},
        },
    )


def load_signal(pred_path: Path) -> pd.DataFrame:
    """读 pred.pkl 当 signal。

    🔴 qlib 的 SignalWCache 会自己做 convert_index_format（datetime 在 level 1 时自动 swap），
    所以这里**原样**把 DataFrame 交给策略，与 PortAnaRecord 的 "<PRED>" 替换完全一致。
    """
    pred = pd.read_pickle(pred_path)
    if isinstance(pred, pd.Series):
        pred = pred.to_frame("score")
    if not isinstance(pred, pd.DataFrame):
        raise TypeError(f"pred.pkl 必须是 DataFrame/Series，实际是 {type(pred)!r}")
    if "score" not in pred.columns:
        raise ValueError(f"pred.pkl 里没有 'score' 列，实际列: {list(pred.columns)}")
    if not isinstance(pred.index, pd.MultiIndex):
        raise TypeError(f"pred.pkl 的 index 必须是 MultiIndex，实际是 {type(pred.index)!r}")
    for level in ("datetime", "instrument"):
        if level not in pred.index.names:
            raise ValueError(f"pred.pkl 的 index 缺少 level '{level}'，实际 names: {list(pred.index.names)}")
    return pred


def run_backtest(signal: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any], Any]:
    """用 qlib 真实 TopkDropoutStrategy 跑一次回测，返回 (report, positions, indicator)。

    strategy / executor / exchange_kwargs 全部对齐 config.build_task_config()["port_analysis"]
    与 qlib.workflow.record_temp.PortAnaRecord 的默认 executor。
    """
    from qlib.backtest import backtest as qlib_backtest

    # 确定性补丁：qlib 的 Position.get_stock_list 返回 set 迭代序（逐进程随机），
    # 会让同分股票的卖/买取舍逐进程不同 ⇒ 回测不可逐位复现。改成字典序。
    _DETERMINISM_STATUS["position_ordering"] = apply_deterministic_position_ordering()
    print(f"== 确定性补丁 (position ordering): {_DETERMINISM_STATUS['position_ordering']}")

    strategy = {
        "class": "TopkDropoutStrategy",
        "module_path": "qlib.contrib.strategy",
        "kwargs": {
            "signal": signal,
            "topk": C.TOPK,
            "n_drop": C.N_DROP,
            **C.STRATEGY_EXTRA_KWARGS,
        },
    }
    executor = {
        "class": "SimulatorExecutor",
        "module_path": "qlib.backtest.executor",
        "kwargs": {
            "time_per_step": BACKTEST_TIME_PER_STEP,
            "generate_portfolio_metrics": True,
        },
    }
    portfolio_metric_dict, indicator_dict = qlib_backtest(
        start_time=C.TEST[0],
        end_time=C.TEST[1],
        strategy=strategy,
        executor=executor,
        benchmark=C.BENCHMARK,
        account=C.ACCOUNT,
        exchange_kwargs=dict(C.EXCHANGE_KWARGS),
    )
    if BACKTEST_FREQ_KEY not in portfolio_metric_dict:
        raise RuntimeError(
            f"回测没有产出 {BACKTEST_FREQ_KEY} 频率的组合指标，实际频次: {list(portfolio_metric_dict)}"
        )
    report, positions = portfolio_metric_dict[BACKTEST_FREQ_KEY]
    _, indicator_obj = indicator_dict[BACKTEST_FREQ_KEY]
    return report, positions, indicator_obj


# ------------------------------------------------------------------ 从回测产物里抽数


def extract_weight_frame(positions: dict[Any, Any]) -> pd.DataFrame:
    """把 qlib 的 hist_positions（Timestamp -> Position）摊平成 (日期 x 标的) 权重表。

    🔴 hist_positions[t] 是**第 t 个交易 bar 收盘时**的持仓（Account.update_hist_positions
    在成交 + 价格更新到收盘价之后才 deepcopy），所以它就是真实的日末持仓路径。
    w_i = 该标的市值 / 组合总资产（Position.get_stock_weight_dict(only_stock=False)，
    分母 = 股票市值 + 现金 + cash_delay）。
    """
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for dt, pos in positions.items():
        rows[pd.Timestamp(dt)] = {str(k): float(v) for k, v in pos.get_stock_weight_dict(only_stock=False).items()}
    if not rows:
        raise ValueError("回测没有产出任何持仓快照 —— 检查回测区间是否为空")
    weight_df = pd.DataFrame(rows).T.sort_index()
    return weight_df.astype(float).fillna(0.0)


def _metric_to_series(order_indicator: Any, metric: str) -> pd.Series:
    """把 qlib 的 SingleData 指标安全地转成 pandas Series（index = 标的代码）。

    🔴 qlib 0.9.8 的 SingleData.to_series() 会把它自己的 Index 对象直接丢给
    pd.Series(data, index=qlib_Index) —— 在 pandas 2.3 上抛
    TypeError: Index(...) must be called with a collection of some kind。
    所以这里绕开 to_series()，直接取 SingleData.data / SingleData.index 自己拼 Series。
    """
    single = order_indicator.data.get(metric)
    if single is None:
        return pd.Series(dtype=float)
    ids = [str(item) for item in list(single.index)]
    values = np.asarray(single.data, dtype=float)
    return pd.Series(values, index=ids)


def extract_trade_flow(indicator_obj: Any) -> pd.DataFrame:
    """从 qlib 的 order indicator 还原每日的买/卖成交额、成本、订单数（**精确值，非推算**）。

    qlib 内部（qlib/backtest/report.py::_update_order_trade_info）：
        trade_value = 成交金额 * order.sign   （买入 sign=+1、卖出 sign=-1）
        trade_cost  = max(成交金额 * cost_ratio, min_cost)
        trade_dir   = 0 卖出 / 1 买入
    """
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for dt, order_indicator in indicator_obj.order_indicator_his.items():
        value = _metric_to_series(order_indicator, "trade_value")
        cost = _metric_to_series(order_indicator, "trade_cost")
        direction = _metric_to_series(order_indicator, "trade_dir")
        # 防御性对齐：三个指标理论上同 index，真不一致时宁可当成未知方向也不能崩
        direction = direction.reindex(value.index).fillna(-1.0)
        buy_value = float(value[direction == 1].sum()) if len(value) else 0.0
        sell_value = float(-value[direction == 0].sum()) if len(value) else 0.0
        filled = value[value.abs() > 1e-5]
        rows[pd.Timestamp(dt)] = {
            "buy_value": buy_value,
            "sell_value": sell_value,
            "trade_cost": float(cost.sum()) if len(cost) else 0.0,
            "n_orders": int(len(value)),
            "n_filled_orders": int(len(filled)),
        }
    if not rows:
        raise ValueError("回测没有产出任何成交指标 —— 检查回测区间是否为空")
    return pd.DataFrame(rows).T.sort_index().astype(float)


# ------------------------------------------------------------------ 统计与对账


def describe(series: pd.Series) -> dict[str, float]:
    """一列数值的基本统计（带 n）。"""
    clean = pd.Series(series).dropna()
    if clean.empty:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "mean": float(clean.mean()),
        "std": float(clean.std(ddof=1)) if clean.size > 1 else 0.0,
        "min": float(clean.min()),
        "p05": float(clean.quantile(0.05)),
        "p50": float(clean.quantile(0.50)),
        "p95": float(clean.quantile(0.95)),
        "max": float(clean.max()),
        "sum": float(clean.sum()),
    }


def build_yearly_turnover(turnover: pd.Series, steady_mask: pd.Series) -> dict[str, dict[str, float]]:
    """分年给出年化单边换手率（×250）。"""
    frame = pd.DataFrame({"turnover": turnover, "steady": steady_mask}).dropna(subset=["turnover"])
    out: dict[str, dict[str, float]] = {}
    for year, group in frame.groupby(frame.index.year):
        steady = group[group["steady"]]
        out[str(year)] = {
            "n_days": int(len(group)),
            "n_steady_days": int(len(steady)),
            "daily_mean_all": float(group["turnover"].mean()),
            "daily_mean_steady": float(steady["turnover"].mean()) if len(steady) else None,
            "annualized_single_side_x250_steady": (
                float(steady["turnover"].mean() * TRADING_DAYS_PER_YEAR) if len(steady) else None
            ),
            "annualized_two_side_x250_steady": (
                float(steady["turnover"].mean() * 2 * TRADING_DAYS_PER_YEAR) if len(steady) else None
            ),
        }
    return out


def compute_reconciliation(
    report: pd.DataFrame,
    trade_flow: pd.DataFrame,
    manifest: dict,
) -> dict[str, Any]:
    """成本对账：把「不含成本 vs 含成本年化超额」的落差拆成 换手×成本 + 其它。

    qlib 的 report_normal["return"] 是**不含成本**的毛收益（Account.update_portfolio_metrics
    里 return_rate = (now_earning + now_cost) / last_account_value，把成本加回来了），
    所以 risk_analysis 里 "excess_return_with_cost" 要再减一次 report["cost"]。
    ⇒ 落差 = mean(report["cost"]) × 238（qlib 的日频年化因子）。
    """
    from qlib.contrib.evaluate import risk_analysis

    # risk_analysis 的 freq 与 portfolio_metric_dict 的 key 同格式（"1day"）
    freq = BACKTEST_FREQ_KEY
    excess_gross = risk_analysis(report["return"] - report["bench"], freq=freq)
    excess_net = risk_analysis(report["return"] - report["bench"] - report["cost"], freq=freq)

    # 🔴 risk_analysis 返回的是「index=指标名 / column='risk'」的 DataFrame，
    #    所以取值是 loc[指标名, 'risk']，不是 loc['risk', 指标名]。
    ann_gross = float(excess_gross.loc["annualized_return", "risk"])
    ann_net = float(excess_net.loc["annualized_return", "risk"])
    gap_pp = (ann_gross - ann_net) * 100.0

    # 实际成本（qlib 自己记的）与换手推算成本（按 open_cost / close_cost）逐日对比
    account = report["account"].astype(float)
    prev_account = account.shift(1)
    prev_account.iloc[0] = float(C.ACCOUNT)   # 首日分母是初始资金
    flow = trade_flow.reindex(report.index).fillna(0.0)
    cost_rate_actual = report["cost"].astype(float)
    rate_cost = flow["buy_value"] * float(C.EXCHANGE_KWARGS["open_cost"]) + flow["sell_value"] * float(
        C.EXCHANGE_KWARGS["close_cost"]
    )
    cost_rate_est = rate_cost / prev_account
    residual_rate = cost_rate_actual - cost_rate_est

    # 双边换手（成交额口径）：自算 vs qlib report_normal["turnover"]（同一笔累计器，必须对得上）
    traded_ratio = (flow["buy_value"] + flow["sell_value"]) / prev_account
    qlib_turnover = report["turnover"].astype(float)
    turn_diff = (traded_ratio - qlib_turnover).abs()
    rel_series = turn_diff / qlib_turnover.abs().replace(0.0, np.nan)
    max_turn_rel = float(rel_series.max()) if rel_series.notna().any() else 0.0

    manifest_metrics = manifest.get("backtest_metrics", {}) if manifest else {}
    manifest_gross = manifest_metrics.get(f"{freq}.excess_return_without_cost.annualized_return")
    manifest_net = manifest_metrics.get(f"{freq}.excess_return_with_cost.annualized_return")
    manifest_gap_pp = None
    if manifest_gross is not None and manifest_net is not None:
        manifest_gap_pp = (float(manifest_gross) - float(manifest_net)) * 100.0

    return {
        "qlib_annualization_days": QLIB_DAILY_ANNUALIZATION_DAYS,
        "note": (
            "manifest 的年化超额由 qlib risk_analysis(freq='1day') 算出，日频年化因子是 238 "
            "（qlib/contrib/evaluate.py:56）；与它对账必须用 238，不能用 250。"
        ),
        "recomputed": {
            "excess_return_without_cost_annualized": ann_gross,
            "excess_return_with_cost_annualized": ann_net,
            "gap_pp": gap_pp,
            "mean_daily_cost_rate": float(cost_rate_actual.mean()),
            "mean_daily_cost_rate_from_turnover": float(cost_rate_est.mean()),
            "mean_daily_residual_cost_rate": float(residual_rate.mean()),
            "annualized_cost_drag_pp_actual": float(cost_rate_actual.mean() * QLIB_DAILY_ANNUALIZATION_DAYS * 100),
            "annualized_cost_drag_pp_from_turnover": float(
                cost_rate_est.mean() * QLIB_DAILY_ANNUALIZATION_DAYS * 100
            ),
            "residual_pp": float(residual_rate.mean() * QLIB_DAILY_ANNUALIZATION_DAYS * 100),
        },
        "manifest": {
            "excess_return_without_cost_annualized": manifest_gross,
            "excess_return_with_cost_annualized": manifest_net,
            "gap_pp": manifest_gap_pp,
        },
        "turnover_crosscheck": {
            "qlib_report_turnover_mean": float(qlib_turnover.mean()),
            "derived_traded_ratio_mean": float(traded_ratio.mean()),
            "max_abs_diff": float(turn_diff.max()) if len(turn_diff) else 0.0,
            "max_rel_diff": max_turn_rel,
            "note": "qlib report_normal['turnover'] 是双边成交额/上日总资产；自算值应与之逐日一致",
        },
        "daily_cost_components": {
            "open_cost": float(C.EXCHANGE_KWARGS["open_cost"]),
            "close_cost": float(C.EXCHANGE_KWARGS["close_cost"]),
            "min_cost": float(C.EXCHANGE_KWARGS["min_cost"]),
            "impact_cost": float(C.EXCHANGE_KWARGS.get("impact_cost", 0.0)),
            "mean_buy_value": float(flow["buy_value"].mean()),
            "mean_sell_value": float(flow["sell_value"].mean()),
            "mean_trade_cost_cny": float(flow["trade_cost"].mean()),
            "mean_filled_orders": float(flow["n_filled_orders"].mean()),
            "total_trade_cost_cny": float(flow["trade_cost"].sum()),
            "note": (
                "residual（实际成本 − 费率×成交额）= min_cost 兜底 + 整手取整；"
                "impact_cost=0 ⇒ 冲击成本缺口为 0。"
            ),
        },
    }


# ------------------------------------------------------------------ 断言


class AssertionCollector:
    """收集断言结果；任一不成立都记下来，最后统一 raise（不静默产出）。"""

    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.results.append({"name": name, "passed": bool(ok), "detail": detail})
        if not ok:
            self.failures.append(f"{name}: {detail}")
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        return bool(ok)

    def raise_if_failed(self) -> None:
        if self.failures:
            raise AssertionError("断言未通过，拒绝产出：\n  - " + "\n  - ".join(self.failures))


def run_assertions(
    report: pd.DataFrame,
    weight_df: pd.DataFrame,
    turnover: pd.Series,
    steady_mask: pd.Series,
    reconciliation: dict[str, Any],
    manifest: dict,
    expected_calendar: pd.DatetimeIndex,
) -> AssertionCollector:
    """量级断言：交易日数 / 每日持仓数 / 换手率 / 回测复现 manifest。"""
    collector = AssertionCollector()

    n_days = int(len(report))
    collector.check(
        "trading_days_in_range",
        MIN_TRADING_DAYS <= n_days <= MAX_TRADING_DAYS,
        f"回测交易日数 {n_days}，要求 [{MIN_TRADING_DAYS}, {MAX_TRADING_DAYS}]",
    )

    # 回测交易日必须与 qlib 日历在 test 段内的交易日**完全一致**（不多不少）
    missing = set(expected_calendar) - set(report.index)
    extra = set(report.index) - set(expected_calendar)
    collector.check(
        "trading_days_match_qlib_calendar",
        not missing and not extra,
        f"回测交易日与 qlib 日历不一致：日历 {len(expected_calendar)} 天 / 回测 {n_days} 天，"
        f"缺 {len(missing)} 天、多 {len(extra)} 天",
    )

    collector.check(
        "report_position_index_consistent",
        set(report.index) == set(weight_df.index),
        f"report 索引 {len(report.index)} 天 vs 持仓快照索引 {len(weight_df.index)} 天，需完全一致",
    )

    start_ok = report.index.min() >= pd.Timestamp(C.TEST[0])
    end_ok = report.index.max() <= pd.Timestamp(C.TEST[1])
    collector.check(
        "trading_days_within_test_window",
        bool(start_ok and end_ok),
        f"回测区间 [{report.index.min().date()}, {report.index.max().date()}]，要求落在 {C.TEST}",
    )

    holdings = (weight_df > 0).sum(axis=1)
    steady_holdings = holdings[steady_mask]
    median_holdings = float(steady_holdings.median())
    collector.check(
        "holdings_count_median",
        abs(median_holdings - C.TOPK) <= HOLDING_MEDIAN_TOL,
        f"稳态日持仓数中位数 {median_holdings:.1f}，要求 topk±{HOLDING_MEDIAN_TOL}（topk={C.TOPK}）",
    )
    p05_holdings = float(steady_holdings.quantile(0.05))
    collector.check(
        "holdings_count_p05",
        p05_holdings >= C.TOPK - HOLDING_P05_TOL,
        f"稳态日持仓数 p05 {p05_holdings:.1f}，要求 >= topk-{HOLDING_P05_TOL}",
    )
    max_holdings = int(holdings.max())
    collector.check(
        "holdings_count_max",
        max_holdings <= C.TOPK + HOLDING_MAX_TOL,
        f"最大日持仓数 {max_holdings}，要求 <= topk+{HOLDING_MAX_TOL}",
    )

    steady_turnover = turnover[steady_mask].dropna()
    daily_mean = float(steady_turnover.mean())
    collector.check(
        "daily_turnover_mean_band",
        TURNOVER_DAILY_MIN <= daily_mean <= TURNOVER_DAILY_MAX,
        f"稳态日均单边换手率 {daily_mean:.4f}，要求 [{TURNOVER_DAILY_MIN}, {TURNOVER_DAILY_MAX}]",
    )
    ann_single = daily_mean * TRADING_DAYS_PER_YEAR
    collector.check(
        "annualized_single_side_turnover_band",
        ann_single <= ANNUALIZED_SINGLE_SIDE_MAX,
        f"年化单边换手 {ann_single:.1f} 倍（×{TRADING_DAYS_PER_YEAR}），要求 <= {ANNUALIZED_SINGLE_SIDE_MAX}",
    )

    crosscheck = reconciliation["turnover_crosscheck"]
    collector.check(
        "turnover_crosscheck",
        crosscheck["max_rel_diff"] <= TURNOVER_CROSSCHECK_REL_TOL,
        f"自算双边换手 vs qlib report 的最大相对误差 {crosscheck['max_rel_diff']:.3e}，"
        f"要求 <= {TURNOVER_CROSSCHECK_REL_TOL}",
    )

    manifest_metrics = reconciliation["manifest"]
    recomputed = reconciliation["recomputed"]
    if manifest_metrics["gap_pp"] is None:
        collector.check(
            "manifest_reproduction",
            False,
            "manifest 里没有 backtest_metrics 的年化超额，无法验证回测复现",
        )
    else:
        gross_ref = float(manifest_metrics["excess_return_without_cost_annualized"])
        net_ref = float(manifest_metrics["excess_return_with_cost_annualized"])
        gross_err = abs(recomputed["excess_return_without_cost_annualized"] - gross_ref) / max(abs(gross_ref), 1e-12)
        net_err = abs(recomputed["excess_return_with_cost_annualized"] - net_ref) / max(abs(net_ref), 1e-12)
        gap_err = abs(recomputed["gap_pp"] - manifest_metrics["gap_pp"])
        collector.check(
            "manifest_reproduction",
            gross_err <= MANIFEST_REPRO_REL_TOL and net_err <= MANIFEST_REPRO_REL_TOL and gap_err <= 1e-4,
            f"不含成本年化超额相对误差 {gross_err:.3e}、含成本 {net_err:.3e}、落差绝对误差 {gap_err:.3e}pp，"
            f"要求 <= {MANIFEST_REPRO_REL_TOL}",
        )

    return collector


# ------------------------------------------------------------------ 主流程


def main() -> int:
    args = parse_args()

    if args.self_test:
        self_test_turnover()
        return 0

    t_start = time.monotonic()
    script_path = Path(__file__).resolve()
    run_dir = resolve_run_dir(args.run_dir)
    pred_path = resolve_pred_pkl(run_dir)
    out_path = Path(args.out).expanduser() if args.out else run_dir / "turnover.json"
    manifest = load_manifest(run_dir)

    print(f"== run_dir  : {run_dir}")
    print(f"== pred.pkl : {pred_path} (md5={md5_of(pred_path)})")
    print(f"== out      : {out_path}")

    signal = load_signal(pred_path)
    pred_dates = pd.DatetimeIndex(pd.Series(signal.index.get_level_values("datetime")).unique()).sort_values()
    print(
        f"== pred 信号 : {len(signal)} 行 / {len(pred_dates)} 个交易日 "
        f"[{pred_dates.min().date()} ~ {pred_dates.max().date()}]"
    )

    print("== qlib.init ==")
    init_qlib()

    # qlib 日历在 test 段内的交易日（回测的 trade_calendar 就是它 ⇒ 用来断言交易日数）
    from qlib.data import D

    expected_calendar = pd.DatetimeIndex(
        D.calendar(start_time=C.TEST[0], end_time=C.TEST[1], freq=BACKTEST_TIME_PER_STEP, future=True)
    )

    print("== 回测（qlib 真实 TopkDropoutStrategy）==")
    report, positions, indicator_obj = run_backtest(signal)

    weight_df = extract_weight_frame(positions)
    trade_flow = extract_trade_flow(indicator_obj)
    turnover = compute_turnover_series(weight_df)

    holdings = (weight_df > 0).sum(axis=1)
    build_mask = holdings > 0
    if not build_mask.any():
        raise AssertionError("整段回测没有任何持仓 —— 策略没有建仓，信号或区间有问题")
    build_date = build_mask[build_mask].index[0]
    # 建仓日（从空仓到第一笔持仓）单独标注，不混进稳态统计
    steady_mask = pd.Series(turnover.index > build_date, index=turnover.index)
    steady_mask &= turnover.notna()
    steady_holdings = holdings[steady_mask]

    daily = pd.DataFrame(
        {
            "n_holdings": holdings,
            "stock_weight_sum": weight_df.sum(axis=1),
            "turnover_single_side": turnover,
            "buy_value": trade_flow["buy_value"].reindex(turnover.index).fillna(0.0),
            "sell_value": trade_flow["sell_value"].reindex(turnover.index).fillna(0.0),
            "trade_cost": trade_flow["trade_cost"].reindex(turnover.index).fillna(0.0),
            "n_filled_orders": trade_flow["n_filled_orders"].reindex(turnover.index).fillna(0.0),
        },
        index=turnover.index,
    )
    daily["qlib_cost_rate"] = report["cost"].astype(float).reindex(daily.index)
    daily["qlib_turnover_two_side"] = report["turnover"].astype(float).reindex(daily.index)

    reconciliation = compute_reconciliation(report, trade_flow, manifest)

    print("== 断言 ==")
    collector = run_assertions(
        report, weight_df, turnover, steady_mask, reconciliation, manifest, expected_calendar
    )
    collector.raise_if_failed()

    steady_turnover = turnover[steady_mask].dropna()
    all_turnover = turnover.dropna()
    daily_mean_steady = float(steady_turnover.mean())

    payload = {
        "schema_version": "1.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_sec": round(time.monotonic() - t_start, 1),
        "host": platform.node(),
        "python": platform.python_version(),
        "run_dir": str(run_dir),
        "out_path": str(out_path),
        "inputs": {
            "pred_pkl": {
                "path": str(pred_path),
                "md5": md5_of(pred_path),
                "bytes": pred_path.stat().st_size,
                "n_rows": int(len(signal)),
                "n_dates": int(len(pred_dates)),
                "first_date": str(pred_dates.min().date()),
                "last_date": str(pred_dates.max().date()),
            },
            "manifest": {
                "path": str(run_dir / "manifest.json"),
                "md5": md5_of(run_dir / "manifest.json") if (run_dir / "manifest.json").is_file() else None,
            },
        },
        "script": {
            "path": str(script_path),
            "md5": md5_of(script_path),
            "note": "脚本自身 md5 —— 便于复现时确认版本",
        },
        "determinism": {
            "position_ordering": _DETERMINISM_STATUS.get("position_ordering", "not-applied"),
            "note": "Position.get_stock_list 改为字典序（上游 qlib/ 源码零改动，运行期打补丁）",
            "pythonhashseed_required": 0,
        },
        "backtest_config": {
            "strategy": "qlib.contrib.strategy.TopkDropoutStrategy",
            "topk": C.TOPK,
            "n_drop": C.N_DROP,
            "strategy_extra_kwargs": dict(C.STRATEGY_EXTRA_KWARGS),
            "executor": "qlib.backtest.executor.SimulatorExecutor(time_per_step=day)",
            "start_time": C.TEST[0],
            "end_time": C.TEST[1],
            "account": C.ACCOUNT,
            "benchmark": C.BENCHMARK,
            "exchange_kwargs": dict(C.EXCHANGE_KWARGS),
            "provider_uri": C.PROVIDER_URI,
            "region": C.REGION,
            "signal": "run 目录的 pred.pkl（模型 test 段打分，未重训）",
        },
        "turnover_definition": {
            "formula": "turnover_single_side(t) = 0.5 * sum_i |w_i(t) - w_i(t-1)|",
            "w_i": "该标的市值 / 组合总资产（股票市值 + 现金）",
            "daily_turnover_two_side_multiple": "双边换手倍数 = 2 × 单边换手率",
            "annualization": f"年化换手 = 日均单边换手率 × {TRADING_DAYS_PER_YEAR} 交易日/年",
            "sample": "逐交易日连续两日之间的持仓权重差分；首日无前一日持仓，不参与差分",
            "build_day": f"建仓日 {pd.Timestamp(build_date).date()}（第一笔建仓）单独标注，未计入稳态统计",
            "source": "qlib Account.hist_positions（bar 收盘时、成交后、已按收盘价重估的持仓）",
        },
        "trading_days": {
            "n_days": int(len(report)),
            "first_date": str(report.index.min().date()),
            "last_date": str(report.index.max().date()),
            "pred_n_dates": int(len(pred_dates)),
            "qlib_calendar_n_days": int(len(expected_calendar)),
            "build_date": str(pd.Timestamp(build_date).date()),
        },
        "holdings": {
            "topk": C.TOPK,
            "stats_all_days": describe(holdings.astype(float)),
            "stats_steady_days": describe(steady_holdings.astype(float)),
        },
        "turnover": {
            "daily_single_side_all_days": describe(all_turnover),
            "daily_single_side_steady_days": describe(steady_turnover),
            "daily_two_side_steady_days": describe(steady_turnover * 2.0),
            "annualized_single_side_x250": daily_mean_steady * TRADING_DAYS_PER_YEAR,
            "annualized_two_side_x250": daily_mean_steady * 2.0 * TRADING_DAYS_PER_YEAR,
            "initial_build": {
                "date": str(pd.Timestamp(build_date).date()),
                "turnover_single_side": float(turnover.loc[build_date]),
                "stock_weight_sum": float(daily.loc[build_date, "stock_weight_sum"]),
            },
            "by_year": build_yearly_turnover(turnover, steady_mask),
        },
        "cost_reconciliation": reconciliation,
        "assertions": {
            "thresholds": {
                "min_trading_days": MIN_TRADING_DAYS,
                "max_trading_days": MAX_TRADING_DAYS,
                "holding_median_tol": HOLDING_MEDIAN_TOL,
                "holding_p05_tol": HOLDING_P05_TOL,
                "holding_max_tol": HOLDING_MAX_TOL,
                "turnover_daily_min": TURNOVER_DAILY_MIN,
                "turnover_daily_max": TURNOVER_DAILY_MAX,
                "annualized_single_side_max": ANNUALIZED_SINGLE_SIDE_MAX,
                "manifest_repro_rel_tol": MANIFEST_REPRO_REL_TOL,
                "turnover_crosscheck_rel_tol": TURNOVER_CROSSCHECK_REL_TOL,
            },
            "results": collector.results,
            "all_passed": True,
        },
        "daily_detail": {
            "note": "逐日明细（换手 / 持仓数 / 成交额 / qlib 口径成本率），CSV 另存",
            "csv_path": str(out_path.with_suffix(".daily.csv")),
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    daily.to_csv(out_path.with_suffix(".daily.csv"), encoding="utf-8-sig")

    print(f"== 已写出 : {out_path}")
    print(f"== 已写出 : {out_path.with_suffix('.daily.csv')}")
    print(
        "== 结论: 稳态日均单边换手 {d:.4f}（年化 ×{n} = {a:.1f} 倍），"
        "manifest 落差 {g:.4f}pp，其中换手×成本解释 {e:.4f}pp，残差 {r:.4f}pp".format(
            d=daily_mean_steady,
            n=TRADING_DAYS_PER_YEAR,
            a=daily_mean_steady * TRADING_DAYS_PER_YEAR,
            g=reconciliation["recomputed"]["gap_pp"],
            e=reconciliation["recomputed"]["annualized_cost_drag_pp_from_turnover"],
            r=reconciliation["recomputed"]["residual_pp"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
