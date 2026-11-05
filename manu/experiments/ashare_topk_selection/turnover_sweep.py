"""降换手「组合构造参数」sweep（受控 A/B）：只改组合构造，不重训模型、不动信号/数据/成本。

【这个脚本做什么】
在 **信号 / 模型 / 数据 / 成本 / 基准 / 初始资金全部固定** 的前提下，系统扫描
「组合构造参数」网格（topk / n_drop / hold_thresh / rebalance_every），
找出**换手显著下降**、且**含成本**年化超额明显更好的参数组。

【这个脚本不做什么】
- ❌ 不重训模型：test 段信号直接用 run 目录的 pred.pkl；valid 段信号用 run 目录的
  model.pkl（unpickle 后的 LGBModel）在**同一个 dataset** 上 `predict(segment="valid")`
  取得 —— 全程不调用 fit。
- ❌ 不改任何配置：config.py **只读引用**；脚本不写回任何参数，是否采纳由人决定。
- ❌ 不做自动决策：只输出「按 valid 排序的表 + 推荐第一名」。

【选参与报告严格分离】
- valid 段 = config.VALID (2022-01-01 ~ 2023-12-31) → **唯一**用于排序/选参
- test  段 = config.TEST  (2024-01-01 ~ 2026-11-03) → **只报告**，不参与排序
14 组 arm 两段都跑，是为了在报告里回答「valid 上选出来的参数，样本外站不站得住」。

【口径】
- 回测：qlib 真实回测（SimulatorExecutor, time_per_step=day），exchange_kwargs 取 config
- 策略：manu 自研 TopkRebalanceIntervalStrategy（rebalance_every=1 ⇒ 与原生
  TopkDropoutStrategy 完全等价；risk_degree 保持默认 0.95 不动，否则基线不可比）
- 收益指标：qlib risk_analysis(freq="1day")，日频年化因子 **238**（qlib/contrib/evaluate.py:56）
- 换手：单边 = 0.5 * Σ_i |w_i(t) - w_i(t-1)|；年化 = 日均 × 250 交易日/年
- 建仓日（第一笔建仓）**单列**，不混进稳态统计（与 turnover_analysis.py 同处理）

【断言】
- 控制组 A0_baseline 在 **test 段**必须复现 manifest 的不含成本 / 含成本年化超额
  （相对误差 ≤ 1e-6）与稳态日均单边换手（相对误差 ≤ 5%）
  ⇒ 不满足即 raise 且**不落盘**（防「换了口径还看不出来」的最后一道闸）
- 单组 arm 失败（抛异常）**不中断**整个 sweep：记录该 arm 的 error 继续跑其它 arm；
  但控制组失败必须整体 raise
- 每组 (arm, segment) 的回测交易日数与该段 qlib 日历交易日数对齐
  （允许首日建仓差异，差异必须记录）

【产物】
- <run_dir>/turnover_sweep.json：全部指标 + 按 valid 排序的推荐表 + 断言结果
- <run_dir>/turnover_sweep.csv ：每行一个 (arm, segment)，列拍平

【用法（服务器）】
    python manu/experiments/ashare_topk_selection/turnover_sweep.py \
        --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>

【本地自检（不碰 qlib / 不碰数据）】
    python manu/experiments/ashare_topk_selection/turnover_sweep.py --self-test
"""
from __future__ import annotations

import argparse
import json
import pickle
import platform
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
from determinism import apply_deterministic_position_ordering
from turnover_analysis import (  # noqa: E402
    BACKTEST_FREQ_KEY,
    BACKTEST_TIME_PER_STEP,
    QLIB_DAILY_ANNUALIZATION_DAYS,
    TRADING_DAYS_PER_YEAR,
    AssertionCollector,
    build_yearly_turnover,
    compute_reconciliation,
    compute_turnover_series,
    describe,
    extract_trade_flow,
    extract_weight_frame,
    init_qlib,
    jsonable,
    load_manifest,
    load_signal,
    md5_of,
    resolve_run_dir,
)

# ------------------------------------------------------------------ 常量（显式声明，禁止散落硬编码）

#: 控制组 arm id（必须复现 manifest 的那一条）
CONTROL_ARM_ID = "A0_baseline"

#: 排序并列判定的小数位：先比含成本年化超额，再比 IR，最后比换手更低者
RANK_TIE_DECIMALS = 12

#: 控制组复现 manifest 年化超额的相对误差上限
BASELINE_REPRO_REL_TOL = 1e-6

#: 控制组复现稳态日均单边换手的相对误差上限
BASELINE_TURNOVER_REL_TOL = 0.05

#: 回测交易日数与该段 qlib 日历交易日数的允许差（首日建仓差异）
MAX_TRADING_DAY_DIFF = 1

#: 持仓数相对 topk 的允许偏离（与 turnover_analysis.py 同口径）
HOLDING_MEDIAN_TOL = 5
HOLDING_P05_TOL = 10
HOLDING_MAX_TOL = 20

#: 稳态日均单边换手的量级带（与 turnover_analysis.py 同口径；只对控制组生效）
TURNOVER_DAILY_MIN = 0.01
TURNOVER_DAILY_MAX = 0.60
ANNUALIZED_SINGLE_SIDE_MAX = 150.0

#: 自算双边换手 vs qlib report_normal["turnover"] 的相对误差上限（两者同源，必须对得上）
TURNOVER_CROSSCHECK_REL_TOL = 1e-6

#: 控制组的期望值（来自 run 目录 manifest 的 backtest_metrics，已实测）
CONTROL_EXPECTED = {
    "excess_return_without_cost_annualized": 0.064037723169097,
    "excess_return_with_cost_annualized": 0.0035404311951419986,
    "steady_daily_single_side_turnover": 0.1646,
}


class ArmSpec(NamedTuple):
    """一组组合构造参数（arm）。信号 / 模型 / 数据 / 成本全部固定，只变这五个。"""

    arm_id: str
    topk: int
    n_drop: int
    hold_thresh: int
    rebalance_every: int


#: 14 组 arm（固定网格，脚本里不临时增删）
ARM_GRID: tuple[ArmSpec, ...] = (
    ArmSpec("A0_baseline", 30, 5, 1, 1),  # 现有基线（控制组）
    ArmSpec("A1_ndrop3", 30, 3, 1, 1),
    ArmSpec("A2_ndrop1", 30, 1, 1, 1),
    ArmSpec("A3_ndrop1_hold5", 30, 1, 5, 1),
    ArmSpec("A4_ndrop1_hold10", 30, 1, 10, 1),
    ArmSpec("A5_ndrop1_hold20", 30, 1, 20, 1),
    ArmSpec("A6_ndrop2_hold10", 30, 2, 10, 1),
    ArmSpec("A7_buyhold", 30, 0, 1, 1),  # ⚠️ 不是买入持有！见 ARM_NOTES（n_drop=0 是退化臂）
    ArmSpec("B1_rebal5", 30, 30, 1, 5),
    ArmSpec("B2_rebal10", 30, 30, 1, 10),
    ArmSpec("B3_rebal20", 30, 30, 1, 20),
    ArmSpec("C1_top50_ndrop1", 50, 1, 1, 1),
    ArmSpec("C2_top50_hold10", 50, 1, 10, 1),
    ArmSpec("C3_top50_rebal10", 50, 50, 1, 10),
)

#: 各 arm 的**qlib 源码口径备注**（写进产物 JSON，避免结果被误读；不改变任何参数）
ARM_NOTES: dict[str, str] = {
    "A0_baseline": "控制组：必须复现 manifest 的年化超额与稳态换手，否则整体 raise 且不落盘",
    "A7_buyhold": (
        "⚠️ qlib 0.9.8 源码口径：only_tradable=False 时 get_last_n(li, 0) = list(li)[-0:] "
        "返回**整个列表** ⇒ n_drop=0 并不是「买入持有」，而是「清仓 + 次日全量重建」的极端换手对照；"
        "以实测指标为准，勿按 arm_id 理解"
    ),
    "A3_ndrop1_hold5": "hold_thresh=最短持有 bar 数（只挡卖出、不挡买入 ⇒ 持仓数可能略高于 topk）",
    "A4_ndrop1_hold10": "hold_thresh=最短持有 bar 数（只挡卖出、不挡买入 ⇒ 持仓数可能略高于 topk）",
    "A5_ndrop1_hold20": "hold_thresh=最短持有 bar 数（只挡卖出、不挡买入 ⇒ 持仓数可能略高于 topk）",
    "A6_ndrop2_hold10": "hold_thresh=最短持有 bar 数（只挡卖出、不挡买入 ⇒ 持仓数可能略高于 topk）",
    "C2_top50_hold10": "hold_thresh=最短持有 bar 数（只挡卖出、不挡买入 ⇒ 持仓数可能略高于 topk）",
    "B1_rebal5": "rebalance_every=5 ⇒ 每 5 个交易日做一次「全量刷新到 top-K」（n_drop=topk），其余日零交易",
    "B2_rebal10": "rebalance_every=10 ⇒ 每 10 个交易日做一次「全量刷新到 top-K」（n_drop=topk），其余日零交易",
    "B3_rebal20": "rebalance_every=20 ⇒ 每 20 个交易日做一次「全量刷新到 top-K」（n_drop=topk），其余日零交易",
    "C3_top50_rebal10": "rebalance_every=10 ⇒ 每 10 个交易日做一次「全量刷新到 top-50」，其余日零交易",
}

#: 两段回测：valid 选参、test 只报告
SEGMENT_SPECS: dict[str, dict[str, str]] = {
    "valid": {
        "start": C.VALID[0],
        "end": C.VALID[1],
        "signal_source": "run 目录 model.pkl → predict(segment='valid')（不重训）",
    },
    "test": {
        "start": C.TEST[0],
        "end": C.TEST[1],
        "signal_source": "run 目录 pred.pkl（模型 test 段打分，不重训）",
    },
}

#: 选参只用 valid；test 仅报告
SELECTION_SEGMENT = "valid"
REPORT_SEGMENT = "test"


# ------------------------------------------------------------------ 参数与产物定位


def parse_args() -> argparse.Namespace:
    """命令行参数（全部显式声明，不散落在脚本里）。"""
    p = argparse.ArgumentParser(
        description="降换手组合构造参数 sweep（受控 A/B：只改组合构造，不重训模型；选参只看 valid）",
    )
    p.add_argument(
        "--run-dir",
        default=None,
        help="产物目录 runs/<recorder_id>（含 manifest.json / pred.pkl / model.pkl）；不给则取 config.latest_run_dir()",
    )
    p.add_argument("--out-json", default=None, help="输出 JSON 路径（默认 <run_dir>/turnover_sweep.json）")
    p.add_argument("--out-csv", default=None, help="输出 CSV 路径（默认 <run_dir>/turnover_sweep.csv）")
    p.add_argument(
        "--self-test",
        action="store_true",
        help="只跑纯 pandas 的合成数据单测（不碰 qlib / 不碰数据），用于本地验证",
    )
    return p.parse_args()


def resolve_output_paths(run_dir: Path, out_json: str | None, out_csv: str | None) -> tuple[Path, Path]:
    """默认产物落在 run 目录里，可用 --out-json / --out-csv 覆盖。"""
    json_path = Path(out_json).expanduser() if out_json else run_dir / "turnover_sweep.json"
    csv_path = Path(out_csv).expanduser() if out_csv else run_dir / "turnover_sweep.csv"
    return json_path, csv_path


# ------------------------------------------------------------------ 信号（不重训）


def build_valid_signal(model_path: Path, pred_path: Path | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """用 run 目录的 model.pkl 在 **valid 段**取信号（不重训、不改数据口径）。

    - dataset 用 `init_instance_by_config(config.build_task_config()["dataset"])` 构建，
      train / valid / test 三段都在 config 里；
    - `LGBModel.predict` 走 `data_key=DK_I`（infer 特征），归一化统计量仍取 fit 窗口
      （2016-2021）⇒ 无泄漏；
    - `LGBModel.fit` 只存 `self.model`（booster）、不持 dataset ⇒ 直接 unpickle 即可
      对任意 dataset 调 predict，无需重训。
    """
    from qlib.utils import init_instance_by_config

    dataset = init_instance_by_config(C.build_task_config()["dataset"])
    with model_path.open("rb") as f:
        model = pickle.load(f)
    pred = model.predict(dataset, segment="valid")
    if isinstance(pred, pd.Series):
        pred = pred.to_frame("score")
    if not isinstance(pred, pd.DataFrame) or "score" not in pred.columns:
        raise TypeError(f"valid 信号必须是含 'score' 列的 DataFrame，实际 {type(pred)!r}")
    if not isinstance(pred.index, pd.MultiIndex):
        raise TypeError(f"valid 信号的 index 必须是 MultiIndex，实际 {type(pred.index)!r}")

    # 🔴 同模型交叉校验：用**同一个 model.pkl** 对 test 段再 predict 一次，
    #    与 run 目录里现成的 pred.pkl 逐值比对。
    #    目的：证明 valid 信号（重新推理得到）与 test 信号（原始 pred.pkl）
    #    出自**同一个模型 + 同一套特征处理**，否则两段不可比。
    crosscheck: dict[str, Any] = {"checked": False}
    if pred_path is not None and Path(pred_path).is_file():
        pred_test = model.predict(dataset, segment="test")
        if isinstance(pred_test, pd.Series):
            pred_test = pred_test.to_frame("score")
        ref = load_signal(Path(pred_path))
        joined = pred_test.join(ref, how="inner", rsuffix="_ref")
        if "score_ref" not in joined.columns:
            raise RuntimeError("同模型交叉校验失败：重算的 test 信号与 pred.pkl 没有可对齐的行")
        diff = (joined["score"] - joined["score_ref"]).abs()
        n_bad = int((diff > 1e-9).sum())
        crosscheck = {
            "checked": True,
            "n_common_rows": int(len(joined)),
            "max_abs_diff": float(diff.max()) if len(diff) else None,
            "n_mismatch_gt_1e-9": n_bad,
            "note": "同一 model.pkl 对 test 段重新 predict，与原 pred.pkl 逐值比对 ⇒ "
                    "证明 valid 信号与 test 信号出自同一模型与同一套特征处理",
        }
        if n_bad > 0:
            raise RuntimeError(
                f"同模型交叉校验失败：重算 test 信号与 pred.pkl 有 {n_bad} 行差值 > 1e-9"
                f"（最大差 {crosscheck['max_abs_diff']:.3e}）"
            )
    return pred, crosscheck


def describe_signal(signal: pd.DataFrame) -> dict[str, Any]:
    """信号的行数 / 交易日数 / 日期范围（写进产物 JSON）。"""
    dates = pd.DatetimeIndex(pd.Series(signal.index.get_level_values("datetime")).unique()).sort_values()
    return {
        "n_rows": int(len(signal)),
        "n_dates": int(len(dates)),
        "first_date": str(dates.min().date()) if len(dates) else None,
        "last_date": str(dates.max().date()) if len(dates) else None,
    }


# ------------------------------------------------------------------ 回测（qlib 真实回测）


def run_arm_backtest(
    arm: ArmSpec,
    segment: str,
    signal: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[Any, Any], Any]:
    """用一组 arm 参数在指定区间跑一次 qlib 真实回测，返回 (report, positions, indicator)。

    strategy / executor / exchange_kwargs / account / benchmark 全部对齐
    config.build_task_config()["port_analysis"]，只多一个 rebalance_every。
    """
    from qlib.backtest import backtest as qlib_backtest

    from rebalance_interval_strategy import TopkRebalanceIntervalStrategy

    # 🔴 消除跨进程不确定性：qlib 的 Position.get_stock_list 返回 set 迭代序（逐进程随机），
    #    同分股票的取舍会变 ⇒ 回测不可逐位复现（实测同 arm 两进程 valid 差最大 3.65pp）。
    #    补丁在 manu/ 内实现，上游 qlib/ 源码零改动。详见 determinism.py 的说明。
    apply_deterministic_position_ordering()

    # ⚠️ BaseSignalStrategy 的入参是 keyword-only，必须用关键字传；
    #    risk_degree 不传 ⇒ 保持默认 0.95（改了基线就不可比）。
    strategy = TopkRebalanceIntervalStrategy(
        signal=signal,
        topk=arm.topk,
        n_drop=arm.n_drop,
        hold_thresh=arm.hold_thresh,
        rebalance_every=arm.rebalance_every,
        **C.STRATEGY_EXTRA_KWARGS,
    )
    executor = {
        "class": "SimulatorExecutor",
        "module_path": "qlib.backtest.executor",
        "kwargs": {
            "time_per_step": BACKTEST_TIME_PER_STEP,
            "generate_portfolio_metrics": True,
        },
    }
    portfolio_metric_dict, indicator_dict = qlib_backtest(
        start_time=SEGMENT_SPECS[segment]["start"],
        end_time=SEGMENT_SPECS[segment]["end"],
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


def _risk_block(analysis: pd.DataFrame) -> dict[str, float]:
    """把 risk_analysis 的 DataFrame（index=指标名 / column='risk'）取成扁平 dict。"""
    return {
        "annualized_return": float(analysis.loc["annualized_return", "risk"]),
        "information_ratio": float(analysis.loc["information_ratio", "risk"]),
        "max_drawdown": float(analysis.loc["max_drawdown", "risk"]),
        "mean": float(analysis.loc["mean", "risk"]),
        "std": float(analysis.loc["std", "risk"]),
    }


def compute_arm_metrics(
    arm: ArmSpec,
    segment: str,
    report: pd.DataFrame,
    positions: dict[Any, Any],
    indicator_obj: Any,
    manifest: dict,
) -> dict[str, Any]:
    """把一次回测的产物折算成该 (arm, segment) 的全部指标。"""
    from qlib.contrib.evaluate import risk_analysis

    weight_df = extract_weight_frame(positions)
    trade_flow = extract_trade_flow(indicator_obj)
    turnover = compute_turnover_series(weight_df)
    holdings = (weight_df > 0).sum(axis=1)

    build_mask = holdings > 0
    if not build_mask.any():
        raise RuntimeError("整段回测没有任何持仓 —— 策略没有建仓，信号或区间有问题")
    build_date = pd.Timestamp(build_mask[build_mask].index[0])
    # 建仓日（从空仓到第一笔持仓）单列，不混进稳态统计
    steady_mask = pd.Series(turnover.index > build_date, index=turnover.index)
    steady_mask &= turnover.notna()

    all_turnover = turnover.dropna()
    steady_turnover = turnover[steady_mask].dropna()
    if steady_turnover.empty:
        raise RuntimeError("稳态期没有可用的换手观测")

    flow = trade_flow.reindex(report.index).fillna(0.0)
    steady_flags = steady_mask.reindex(report.index).fillna(False).astype(bool)
    steady_flow = flow[steady_flags]

    holdings = holdings.reindex(steady_mask.index).fillna(0.0)
    steady_holdings = holdings[steady_mask]

    # qlib 的 report["return"] 是不含成本的毛收益 ⇒ 含成本口径要再减一次 report["cost"]
    gross = _risk_block(risk_analysis(report["return"] - report["bench"], freq=BACKTEST_FREQ_KEY))
    net = _risk_block(risk_analysis(report["return"] - report["bench"] - report["cost"], freq=BACKTEST_FREQ_KEY))

    reconciliation = compute_reconciliation(report, trade_flow, manifest)

    return {
        "status": "ok",
        "arm": arm._asdict(),
        "segment": segment,
        "window": {
            "start": SEGMENT_SPECS[segment]["start"],
            "end": SEGMENT_SPECS[segment]["end"],
            "signal_source": SEGMENT_SPECS[segment]["signal_source"],
        },
        "trading_days": {
            "n_backtest_days": int(len(report)),
            "n_weight_snapshot_days": int(len(weight_df)),
            "first_date": str(report.index.min().date()),
            "last_date": str(report.index.max().date()),
            "build_date": str(build_date.date()),
        },
        "returns": {"without_cost": gross, "with_cost": net},
        "turnover": {
            "definition": "单边换手 = 0.5 * Σ_i |w_i(t) - w_i(t-1)|（首行 NaN，无前一日持仓）",
            "annualization_days": TRADING_DAYS_PER_YEAR,
            "daily_mean_all": float(all_turnover.mean()),
            "daily_mean_steady": float(steady_turnover.mean()),
            "p50_steady": float(steady_turnover.quantile(0.50)),
            "p95_steady": float(steady_turnover.quantile(0.95)),
            "annualized_single_side_x250_steady": float(steady_turnover.mean() * TRADING_DAYS_PER_YEAR),
            "annualized_two_side_x250_steady": float(steady_turnover.mean() * 2 * TRADING_DAYS_PER_YEAR),
            "stats_all_days": describe(all_turnover),
            "stats_steady_days": describe(steady_turnover),
            "by_year": build_yearly_turnover(turnover, steady_mask),
            "build_day": {
                "date": str(build_date.date()),
                "turnover_single_side": float(turnover.loc[build_date]),
                "stock_weight_sum": float(weight_df.loc[build_date].sum()),
                "n_holdings": int(holdings.loc[build_date]),
            },
        },
        "trades": {
            "daily_buy_value_mean": float(flow["buy_value"].mean()),
            "daily_sell_value_mean": float(flow["sell_value"].mean()),
            "daily_n_orders_mean": float(flow["n_orders"].mean()),
            "daily_n_filled_orders_mean": float(flow["n_filled_orders"].mean()),
            "total_buy_value": float(flow["buy_value"].sum()),
            "total_sell_value": float(flow["sell_value"].sum()),
            "total_n_orders": int(flow["n_orders"].sum()),
            "total_trade_cost": float(flow["trade_cost"].sum()),
            "steady_daily_buy_value_mean": float(steady_flow["buy_value"].mean()) if len(steady_flow) else None,
            "steady_daily_sell_value_mean": float(steady_flow["sell_value"].mean()) if len(steady_flow) else None,
            "steady_daily_n_orders_mean": float(steady_flow["n_orders"].mean()) if len(steady_flow) else None,
            "steady_total_trade_cost": float(steady_flow["trade_cost"].sum()) if len(steady_flow) else None,
        },
        "holdings": {
            "topk": arm.topk,
            "median_steady": float(steady_holdings.median()),
            "p05_steady": float(steady_holdings.quantile(0.05)),
            "max_steady": int(steady_holdings.max()),
            "stats_all_days": describe(holdings.astype(float)),
            "stats_steady_days": describe(steady_holdings.astype(float)),
        },
        "cost_reconciliation": reconciliation,
    }


# ------------------------------------------------------------------ 断言（不许静默产出）


def check_control_reproduction(
    check: Any,
    metrics: dict[str, Any],
) -> None:
    """控制组复现断言（致命）：A0_baseline 在 test 段必须复现 manifest 的数字。

    这是防「换了口径还看不出来」的最后一道闸 —— 不满足即 raise 且不落盘。
    """
    gross = float(metrics["returns"]["without_cost"]["annualized_return"])
    net = float(metrics["returns"]["with_cost"]["annualized_return"])
    turnover = float(metrics["turnover"]["daily_mean_steady"])

    gross_ref = CONTROL_EXPECTED["excess_return_without_cost_annualized"]
    net_ref = CONTROL_EXPECTED["excess_return_with_cost_annualized"]
    turnover_ref = CONTROL_EXPECTED["steady_daily_single_side_turnover"]

    gross_err = abs(gross - gross_ref) / max(abs(gross_ref), 1e-12)
    net_err = abs(net - net_ref) / max(abs(net_ref), 1e-12)
    turnover_err = abs(turnover - turnover_ref) / max(abs(turnover_ref), 1e-12)

    check(
        "control_gross_excess_return_reproduction",
        gross_err <= BASELINE_REPRO_REL_TOL,
        f"不含成本年化超额 {gross!r} vs manifest {gross_ref!r}，相对误差 {gross_err:.3e}"
        f"（要求 <= {BASELINE_REPRO_REL_TOL}）",
        fatal=True,
    )
    check(
        "control_net_excess_return_reproduction",
        net_err <= BASELINE_REPRO_REL_TOL,
        f"含成本年化超额 {net!r} vs manifest {net_ref!r}，相对误差 {net_err:.3e}"
        f"（要求 <= {BASELINE_REPRO_REL_TOL}）",
        fatal=True,
    )
    check(
        "control_steady_turnover_reproduction",
        turnover_err <= BASELINE_TURNOVER_REL_TOL,
        f"稳态日均单边换手 {turnover:.6f} vs 实测 {turnover_ref!r}，相对误差 {turnover_err:.3e}"
        f"（要求 <= {BASELINE_TURNOVER_REL_TOL}）",
        fatal=True,
    )


def check_arm_metrics(
    check: Any,
    arm: ArmSpec,
    segment: str,
    metrics: dict[str, Any],
    calendar_days: int,
) -> None:
    """单组 (arm, segment) 的口径断言（非致命，记录即可）。"""
    prefix = f"{arm.arm_id}.{segment}"

    n_backtest = int(metrics["trading_days"]["n_backtest_days"])
    n_weight = int(metrics["trading_days"]["n_weight_snapshot_days"])
    diff = n_backtest - calendar_days
    check(
        f"{prefix}.trading_days_vs_qlib_calendar",
        abs(diff) <= MAX_TRADING_DAY_DIFF,
        f"回测 {n_backtest} 天 / qlib 日历 {calendar_days} 天，差 {diff:+d} 天"
        f"（允许 ≤ {MAX_TRADING_DAY_DIFF}，首日建仓差异）",
    )
    check(
        f"{prefix}.report_position_index_consistent",
        n_backtest == n_weight,
        f"report {n_backtest} 天 vs 持仓快照 {n_weight} 天，需一致",
    )

    median_holdings = float(metrics["holdings"]["median_steady"])
    check(
        f"{prefix}.holdings_median",
        abs(median_holdings - arm.topk) <= HOLDING_MEDIAN_TOL,
        f"稳态日持仓数中位数 {median_holdings:.1f}，要求 topk±{HOLDING_MEDIAN_TOL}（topk={arm.topk}）",
    )
    p05_holdings = float(metrics["holdings"]["p05_steady"])
    check(
        f"{prefix}.holdings_p05",
        p05_holdings >= arm.topk - HOLDING_P05_TOL,
        f"稳态日持仓数 p05 {p05_holdings:.1f}，要求 >= topk-{HOLDING_P05_TOL}",
    )
    max_holdings = int(metrics["holdings"]["max_steady"])
    check(
        f"{prefix}.holdings_max",
        max_holdings <= arm.topk + HOLDING_MAX_TOL,
        f"最大日持仓数 {max_holdings}，要求 <= topk+{HOLDING_MAX_TOL}",
    )

    crosscheck = metrics["cost_reconciliation"]["turnover_crosscheck"]
    check(
        f"{prefix}.turnover_crosscheck",
        float(crosscheck["max_rel_diff"]) <= TURNOVER_CROSSCHECK_REL_TOL,
        f"自算双边换手 vs qlib report 的最大相对误差 {float(crosscheck['max_rel_diff']):.3e}"
        f"（要求 <= {TURNOVER_CROSSCHECK_REL_TOL}）",
    )


def check_control_bands(check: Any, metrics: dict[str, Any]) -> None:
    """控制组的量级带（致命）：换手量级必须与原基线同档，说明口径没漂。"""
    daily_mean = float(metrics["turnover"]["daily_mean_steady"])
    check(
        "control_daily_turnover_band",
        TURNOVER_DAILY_MIN <= daily_mean <= TURNOVER_DAILY_MAX,
        f"稳态日均单边换手率 {daily_mean:.4f}，要求 [{TURNOVER_DAILY_MIN}, {TURNOVER_DAILY_MAX}]",
        fatal=True,
    )
    annualized = daily_mean * TRADING_DAYS_PER_YEAR
    check(
        "control_annualized_single_side_turnover_band",
        annualized <= ANNUALIZED_SINGLE_SIDE_MAX,
        f"年化单边换手 {annualized:.1f} 倍（×{TRADING_DAYS_PER_YEAR}），要求 <= {ANNUALIZED_SINGLE_SIDE_MAX}",
        fatal=True,
    )


# ------------------------------------------------------------------ 选参（纯函数，可本地单测）


def build_ranking(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """按 **valid 段**排序，并挂上同一 arm 的 test 段表现（test 只报告，不参与排序）。

    排序口径：含成本年化超额**降序**；并列时依次比 IR **高**、稳态日均单边换手 **低**。
    """
    rows: list[dict[str, Any]] = []
    for arm_id, segments in results.items():
        valid = segments.get(SELECTION_SEGMENT)
        if not valid or valid.get("status") != "ok":
            continue
        row: dict[str, Any] = {
            "arm_id": arm_id,
            "arm": valid["arm"],
            "valid_net_annualized_return": float(valid["returns"]["with_cost"]["annualized_return"]),
            "valid_net_information_ratio": float(valid["returns"]["with_cost"]["information_ratio"]),
            "valid_net_max_drawdown": float(valid["returns"]["with_cost"]["max_drawdown"]),
            "valid_gross_annualized_return": float(valid["returns"]["without_cost"]["annualized_return"]),
            "valid_gross_information_ratio": float(valid["returns"]["without_cost"]["information_ratio"]),
            "valid_daily_turnover_steady": float(valid["turnover"]["daily_mean_steady"]),
            "valid_annualized_turnover_x250": float(valid["turnover"]["annualized_single_side_x250_steady"]),
            "valid_holdings_median": float(valid["holdings"]["median_steady"]),
        }
        test = segments.get(REPORT_SEGMENT)
        if test and test.get("status") == "ok":
            row["test_status"] = "ok"
            row["test_net_annualized_return"] = float(test["returns"]["with_cost"]["annualized_return"])
            row["test_net_information_ratio"] = float(test["returns"]["with_cost"]["information_ratio"])
            row["test_gross_annualized_return"] = float(test["returns"]["without_cost"]["annualized_return"])
            row["test_daily_turnover_steady"] = float(test["turnover"]["daily_mean_steady"])
            row["test_annualized_turnover_x250"] = float(test["turnover"]["annualized_single_side_x250_steady"])
        else:
            row["test_status"] = (test or {}).get("status", "missing")
            row["test_error"] = (test or {}).get("error")
        rows.append(row)

    rows.sort(
        key=lambda r: (
            -round(r["valid_net_annualized_return"], RANK_TIE_DECIMALS),
            -round(r["valid_net_information_ratio"], RANK_TIE_DECIMALS),
            r["valid_daily_turnover_steady"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["valid_rank"] = rank
    return rows


# ------------------------------------------------------------------ CSV（列拍平）


def build_csv_rows(
    results: dict[str, dict[str, Any]],
    ranking: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """每行一个 (arm, segment)，列拍平，便于直接看表。"""
    rank_of = {row["arm_id"]: row["valid_rank"] for row in ranking}
    rows: list[dict[str, Any]] = []
    for arm in ARM_GRID:
        for segment in (SELECTION_SEGMENT, REPORT_SEGMENT):
            metrics = results.get(arm.arm_id, {}).get(segment)
            base: dict[str, Any] = {
                "arm_id": arm.arm_id,
                "topk": arm.topk,
                "n_drop": arm.n_drop,
                "hold_thresh": arm.hold_thresh,
                "rebalance_every": arm.rebalance_every,
                "segment": segment,
                "valid_rank": rank_of.get(arm.arm_id),
            }
            if not metrics or metrics.get("status") != "ok":
                base.update({"status": (metrics or {}).get("status", "missing")})
                base["error"] = (metrics or {}).get("error")
                rows.append(base)
                continue
            recon = metrics["cost_reconciliation"]["recomputed"]
            base.update(
                {
                    "status": "ok",
                    "error": None,
                    "n_trading_days": metrics["trading_days"]["n_backtest_days"],
                    "first_date": metrics["trading_days"]["first_date"],
                    "last_date": metrics["trading_days"]["last_date"],
                    "build_date": metrics["trading_days"]["build_date"],
                    "gross_annualized_return": metrics["returns"]["without_cost"]["annualized_return"],
                    "gross_information_ratio": metrics["returns"]["without_cost"]["information_ratio"],
                    "gross_max_drawdown": metrics["returns"]["without_cost"]["max_drawdown"],
                    "net_annualized_return": metrics["returns"]["with_cost"]["annualized_return"],
                    "net_information_ratio": metrics["returns"]["with_cost"]["information_ratio"],
                    "net_max_drawdown": metrics["returns"]["with_cost"]["max_drawdown"],
                    "turnover_daily_mean_steady": metrics["turnover"]["daily_mean_steady"],
                    "turnover_daily_mean_all": metrics["turnover"]["daily_mean_all"],
                    "turnover_p50_steady": metrics["turnover"]["p50_steady"],
                    "turnover_p95_steady": metrics["turnover"]["p95_steady"],
                    "turnover_annualized_x250_steady": metrics["turnover"]["annualized_single_side_x250_steady"],
                    "daily_buy_value_mean": metrics["trades"]["daily_buy_value_mean"],
                    "daily_sell_value_mean": metrics["trades"]["daily_sell_value_mean"],
                    "daily_n_orders_mean": metrics["trades"]["daily_n_orders_mean"],
                    "total_trade_cost": metrics["trades"]["total_trade_cost"],
                    "holdings_median": metrics["holdings"]["median_steady"],
                    "holdings_p05": metrics["holdings"]["p05_steady"],
                    "holdings_max": metrics["holdings"]["max_steady"],
                    "cost_gap_pp": recon["gap_pp"],
                    "cost_drag_actual_pp": recon["annualized_cost_drag_pp_actual"],
                    "cost_drag_from_turnover_pp": recon["annualized_cost_drag_pp_from_turnover"],
                    "cost_residual_pp": recon["residual_pp"],
                }
            )
            rows.append(base)
    return rows


# ------------------------------------------------------------------ 本地自检（纯 pandas，不碰 qlib / 数据）


def self_test_turnover() -> None:
    """合成数据单测：单边换手 = 0.5 * Σ_i |w_i(t) - w_i(t-1)|，首行 NaN。

    第 1 天 {A:0.50, B:0.50}
    第 2 天 {A:0.50, B:0.00, C:0.50} ⇒ 0.5 * (0 + 0.50 + 0.50) = 0.50
    第 3 天 {A:0.25, B:0.25, C:0.50} ⇒ 0.5 * (0.25 + 0.25 + 0) = 0.25
    """
    index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    weights = pd.DataFrame(
        {"A": [0.50, 0.50, 0.25], "B": [0.50, 0.00, 0.25], "C": [0.00, 0.50, 0.50]},
        index=index,
    )
    got = compute_turnover_series(weights)
    assert np.isnan(got.iloc[0]), f"首行应为 NaN，实际 {got.iloc[0]!r}"
    assert abs(float(got.iloc[1]) - 0.50) < 1e-12, f"day2 期望 0.50，实际 {float(got.iloc[1])!r}"
    assert abs(float(got.iloc[2]) - 0.25) < 1e-12, f"day3 期望 0.25，实际 {float(got.iloc[2])!r}"
    print(
        "[self-test] 单边换手合成数据单测通过："
        f"day1=NaN / day2 期望 0.50 实际 {float(got.iloc[1])} / day3 期望 0.25 实际 {float(got.iloc[2])}"
    )


def self_test_ranking() -> None:
    """合成数据单测：valid 排序 = 含成本年化超额降序 → IR 降序 → 换手升序。"""
    def fake(net: float, ir: float, turnover: float) -> dict[str, Any]:
        return {
            "status": "ok",
            "arm": {"arm_id": "fake"},
            "returns": {
                "without_cost": {"annualized_return": net + 0.01, "information_ratio": ir + 0.1},
                "with_cost": {"annualized_return": net, "information_ratio": ir, "max_drawdown": -0.1},
            },
            "turnover": {
                "daily_mean_steady": turnover,
                "annualized_single_side_x250_steady": turnover * 250,
            },
            "holdings": {"median_steady": 30},
        }

    results = {
        "X_low_net": {"valid": fake(0.01, 1.0, 0.05)},
        "Y_low_ir": {"valid": fake(0.03, 0.5, 0.02)},
        "Z_high_turnover": {"valid": fake(0.03, 0.9, 0.30)},
        "W_best": {"valid": fake(0.03, 0.9, 0.10)},
        "BROKEN": {"valid": {"status": "error", "error": "boom"}},
    }
    ranked = build_ranking(results)
    got = [row["arm_id"] for row in ranked]
    expected = ["W_best", "Z_high_turnover", "Y_low_ir", "X_low_net"]
    assert got == expected, f"排序期望 {expected}，实际 {got}"
    assert [row["valid_rank"] for row in ranked] == [1, 2, 3, 4], "valid_rank 期望 1,2,3,4"
    assert "BROKEN" not in got, "失败的 arm 不应进入排序表"
    print(f"[self-test] arm 排序合成数据单测通过：期望 {expected} == 实际 {got}")


def run_self_tests() -> None:
    """跑全部纯 pandas 合成数据单测。"""
    self_test_turnover()
    self_test_ranking()


# ------------------------------------------------------------------ 主流程


def main() -> int:
    args = parse_args()

    if args.self_test:
        run_self_tests()
        return 0

    t_start = time.monotonic()
    script_path = Path(__file__).resolve()
    run_dir = resolve_run_dir(args.run_dir)
    out_json, out_csv = resolve_output_paths(run_dir, args.out_json, args.out_csv)
    manifest = load_manifest(run_dir)
    pred_path = run_dir / "pred.pkl"
    model_path = run_dir / "model.pkl"
    for path in (pred_path, model_path):
        if not path.is_file():
            raise FileNotFoundError(f"run 目录缺少 {path.name}：{run_dir}")

    print(f"== run_dir : {run_dir}")
    print(f"== out json: {out_json}")
    print(f"== out csv : {out_csv}")

    print("== qlib.init ==")
    init_qlib()

    from qlib.data import D

    calendars = {
        segment: pd.DatetimeIndex(
            D.calendar(
                start_time=SEGMENT_SPECS[segment]["start"],
                end_time=SEGMENT_SPECS[segment]["end"],
                freq=BACKTEST_TIME_PER_STEP,
                future=True,
            )
        )
        for segment in SEGMENT_SPECS
    }

    print("== 信号（不重训）==")
    valid_signal, signal_crosscheck = build_valid_signal(model_path, pred_path)
    signals = {
        REPORT_SEGMENT: load_signal(pred_path),
        SELECTION_SEGMENT: valid_signal,
    }
    signal_stats = {segment: describe_signal(sig) for segment, sig in signals.items()}
    for segment, stats in signal_stats.items():
        print(f"   {segment}: {stats['n_rows']} 行 / {stats['n_dates']} 个交易日 "
              f"[{stats['first_date']} ~ {stats['last_date']}]")

    checks = AssertionCollector()
    fatal_failures: list[str] = []

    def check(name: str, ok: bool, detail: str, fatal: bool = False) -> bool:
        """记录一条断言；fatal=True 的失败进 fatal_failures ⇒ 最终整体 raise 且不落盘。"""
        passed = checks.check(name, ok, detail)
        if fatal and not passed:
            fatal_failures.append(f"{name}: {detail}")
        return passed

    results: dict[str, dict[str, Any]] = {arm.arm_id: {} for arm in ARM_GRID}
    for arm in ARM_GRID:
        for segment in (SELECTION_SEGMENT, REPORT_SEGMENT):
            label = f"{arm.arm_id} / {segment}"
            try:
                report, positions, indicator_obj = run_arm_backtest(arm, segment, signals[segment])
                # 只有控制组才与 manifest 对账；其它 arm 传空 manifest ⇒ 不做 manifest 比对
                recon_manifest = manifest if (arm.arm_id == CONTROL_ARM_ID and segment == REPORT_SEGMENT) else {}
                metrics = compute_arm_metrics(
                    arm, segment, report, positions, indicator_obj, recon_manifest
                )
            except Exception as exc:  # noqa: BLE001 —— 单组失败不中断整个 sweep
                results[arm.arm_id][segment] = {
                    "status": "error",
                    "arm": arm._asdict(),
                    "segment": segment,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
                if arm.arm_id == CONTROL_ARM_ID:
                    raise
                continue

            results[arm.arm_id][segment] = metrics
            check_arm_metrics(check, arm, segment, metrics, int(len(calendars[segment])))
            if arm.arm_id == CONTROL_ARM_ID and segment == REPORT_SEGMENT:
                check_control_bands(check, metrics)
                check_control_reproduction(check, metrics)
                if fatal_failures:
                    raise AssertionError(
                        "控制组断言未通过，拒绝产出（不落盘）：\n  - " + "\n  - ".join(fatal_failures)
                    )
            print(
                f"[OK] {label}: net_ann={metrics['returns']['with_cost']['annualized_return']:.4f} "
                f"IR={metrics['returns']['with_cost']['information_ratio']:.3f} "
                f"turnover_steady={metrics['turnover']['daily_mean_steady']:.4f} "
                f"(年化×250={metrics['turnover']['annualized_single_side_x250_steady']:.1f}) "
                f"days={metrics['trading_days']['n_backtest_days']}"
            )

    ranking = build_ranking(results)
    recommendation: dict[str, Any] | None = None
    if ranking:
        top = ranking[0]
        recommendation = {
            "arm_id": top["arm_id"],
            "arm": top["arm"],
            "selection_segment": SELECTION_SEGMENT,
            "valid_rank": top["valid_rank"],
            "valid_net_annualized_return": top["valid_net_annualized_return"],
            "valid_net_information_ratio": top["valid_net_information_ratio"],
            "valid_daily_turnover_steady": top["valid_daily_turnover_steady"],
            "valid_annualized_turnover_x250": top["valid_annualized_turnover_x250"],
            "test_net_annualized_return": top.get("test_net_annualized_return"),
            "test_daily_turnover_steady": top.get("test_daily_turnover_steady"),
            "test_annualized_turnover_x250": top.get("test_annualized_turnover_x250"),
            "baseline_valid_rank": next(
                (row["valid_rank"] for row in ranking if row["arm_id"] == CONTROL_ARM_ID), None
            ),
            "note": "只给建议 —— 脚本不改 config.py，是否采纳由人决定",
        }

    script_dir = Path(__file__).resolve().parent
    payload = {
        "schema_version": "1.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_sec": round(time.monotonic() - t_start, 1),
        "host": platform.node(),
        "python": platform.python_version(),
        "run_dir": str(run_dir),
        "out_json": str(out_json),
        "out_csv": str(out_csv),
        "semantics": {
            "purpose": "只改组合构造参数（topk / n_drop / hold_thresh / rebalance_every）的受控 A/B sweep",
            "retrain": False,
            "signal_model_data_cost_fixed": True,
            "config_mutated": False,
            "selection_segment": SELECTION_SEGMENT,
            "report_segment": REPORT_SEGMENT,
            "note": "config.py 只读引用；脚本只输出排序表与推荐第一名，不写回任何配置",
        },
        "inputs": {
            "pred_pkl": {
                "path": str(pred_path),
                "md5": md5_of(pred_path),
                "bytes": pred_path.stat().st_size,
            },
            "model_pkl": {
                "path": str(model_path),
                "md5": md5_of(model_path),
                "bytes": model_path.stat().st_size,
            },
            "manifest": {
                "path": str(run_dir / "manifest.json"),
                "md5": md5_of(run_dir / "manifest.json") if (run_dir / "manifest.json").is_file() else None,
            },
            "config_py": {"path": str(Path(C.__file__).resolve()), "md5": md5_of(Path(C.__file__).resolve())},
            "script": {"path": str(script_path), "md5": md5_of(script_path)},
            "turnover_analysis_py": {
                "path": str(script_dir / "turnover_analysis.py"),
                "md5": md5_of(script_dir / "turnover_analysis.py"),
            },
            "rebalance_interval_strategy_py": {
                "path": str(script_dir / "rebalance_interval_strategy.py"),
                "md5": md5_of(script_dir / "rebalance_interval_strategy.py"),
            },
        },
        "signals": {
            segment: {
                "window": {
                    "start": SEGMENT_SPECS[segment]["start"],
                    "end": SEGMENT_SPECS[segment]["end"],
                },
                "signal_source": SEGMENT_SPECS[segment]["signal_source"],
                "calendar_trading_days": int(len(calendars[segment])),
                **signal_stats[segment],
            }
            for segment in SEGMENT_SPECS
        },
        "signal_crosscheck": signal_crosscheck,
        "backtest_config": {
            "strategy": "manu 自研 TopkRebalanceIntervalStrategy（rebalance_every=1 ⇒ 与原生 TopkDropoutStrategy 等价）",
            "risk_degree": "默认 0.95（未改，保持基线可比）",
            "executor": "qlib.backtest.executor.SimulatorExecutor(time_per_step=day)",
            "account": C.ACCOUNT,
            "benchmark": C.BENCHMARK,
            "exchange_kwargs": dict(C.EXCHANGE_KWARGS),
            "strategy_extra_kwargs": dict(C.STRATEGY_EXTRA_KWARGS),
            "provider_uri": C.PROVIDER_URI,
            "region": C.REGION,
        },
        "arm_grid": [{**arm._asdict(), "note": ARM_NOTES.get(arm.arm_id)} for arm in ARM_GRID],
        "results": results,
        "ranking": ranking,
        "recommendation": recommendation,
        "assertions": {
            "thresholds": {
                "baseline_repro_rel_tol": BASELINE_REPRO_REL_TOL,
                "baseline_turnover_rel_tol": BASELINE_TURNOVER_REL_TOL,
                "max_trading_day_diff": MAX_TRADING_DAY_DIFF,
                "holding_median_tol": HOLDING_MEDIAN_TOL,
                "holding_p05_tol": HOLDING_P05_TOL,
                "holding_max_tol": HOLDING_MAX_TOL,
                "turnover_daily_min": TURNOVER_DAILY_MIN,
                "turnover_daily_max": TURNOVER_DAILY_MAX,
                "annualized_single_side_max": ANNUALIZED_SINGLE_SIDE_MAX,
                "turnover_crosscheck_rel_tol": TURNOVER_CROSSCHECK_REL_TOL,
                "qlib_annualization_days": QLIB_DAILY_ANNUALIZATION_DAYS,
                "turnover_annualization_days": TRADING_DAYS_PER_YEAR,
            },
            "control_expected": dict(CONTROL_EXPECTED),
            "results": checks.results,
            "failed_checks": checks.failures,
            "fatal_failures": fatal_failures,
            "all_passed": not fatal_failures,
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(build_csv_rows(results, ranking)).to_csv(out_csv, index=False, encoding="utf-8-sig")

    print("== 按 valid 排序（选参只看 valid；test 只报告）==")
    print(f"{'rank':>4}  {'arm_id':<20} {'valid_net_ann':>13} {'valid_IR':>9} "
          f"{'valid_turn_x250':>16} | {'test_net_ann':>12} {'test_turn_x250':>15}")
    for row in ranking:
        print(
            f"{row['valid_rank']:>4}  {row['arm_id']:<20} {row['valid_net_annualized_return']:>13.4f} "
            f"{row['valid_net_information_ratio']:>9.3f} {row['valid_annualized_turnover_x250']:>16.1f} | "
            f"{row.get('test_net_annualized_return', float('nan')):>12.4f} "
            f"{row.get('test_annualized_turnover_x250', float('nan')):>15.1f}"
        )
    if recommendation:
        print(
            "== 推荐第一名（valid 口径）：{a}（topk={t} n_drop={d} hold_thresh={h} rebalance_every={r}）".format(
                a=recommendation["arm_id"],
                t=recommendation["arm"]["topk"],
                d=recommendation["arm"]["n_drop"],
                h=recommendation["arm"]["hold_thresh"],
                r=recommendation["arm"]["rebalance_every"],
            )
        )
        print(
            "   valid 含成本年化超额 {:.4f} / 稳态日均换手 {:.4f}（年化×250 {:.1f}）；"
            "test 含成本年化超额 {:.4f}".format(
                recommendation["valid_net_annualized_return"],
                recommendation["valid_daily_turnover_steady"],
                recommendation["valid_annualized_turnover_x250"],
                recommendation.get("test_net_annualized_return", float("nan")),
            )
        )
    print(f"== 已写出 : {out_json}")
    print(f"== 已写出 : {out_csv}")
    print(f"== 断言   : {len(checks.results)} 条，失败 {len(checks.failures)} 条，致命 {len(fatal_failures)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
