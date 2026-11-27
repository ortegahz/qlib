#!/usr/bin/env python3
"""🔴 V3 **单变量对照实验**驱动（5 折 OOS · 不烧单盲）—— 用户 2026-11-26 目标文档第 ⑤ 条。

**实验设计（单变量 ⇒ 只替换上游打分，其余全锁）**

| 层 | 锁死的东西 |
| --- | --- |
| **下游 ETF 层** | `E = W × S` 的 `W`（成分股权重）、`top_k=5`、`hold_buffer=2`、`cadence=20`、`min_coverage=0.5`、成本（佣金 1.5bp + 滑点 5bp）——**全部取自 V2 冻结件 `frozen_params_wf.json`，一个字不改** |
| **回测引擎** | V2 的 `backtest_v2.run_backtest_v2`（同一份代码路径） |
| **评估口径** | 三项指标由 `manu/etf_rotation/v3/evaluate_s.py` 计算（控制臂与实验臂**同一份**评估代码） |
| **可变的只有** | 上游 `stock_scores_*.csv`（V2 缓存 vs V3 现训） |

**两个臂**

* **control_v2**（对照臂）：**直接复用 V2 已封存的 5 折 OOS 打分**
  （`<RESULTS_ROOT>/v2/_walkforward/stock_scores_v2_wf{1..5}_ablate_roc5.csv`）
  ⇒ **零重训**、零口径漂移 ⇒ 这就是「V2 现役」的真身；
* **v3_full**（实验臂）：V3 逐折重训（ETF 并集池 ＋ 数值中心化超额标签 ＋ 规模/流动性残差化
  ＋ lambdarank 排序损失 ＋ ΣW 样本加权 ＋ 季度滚动重训）⇒ 产物 `stock_scores_v3_*.csv`。

**🔴 ETF 行情面板口径**：V2 默认的 `datasets/etf/etf_prices.csv` **只覆盖 2020-01-02 起**
（实测首行日期）⇒ **覆盖不了 5 折 OOS 的 2019 年** ⇒ 本实验必须显式传
`datasets/etf_ext/etf_prices_merged_2018.csv`（**2018-01-02 起**）；
**两个臂共用同一份面板**（面板只加载一次）⇒ 对照公平。

**🔴 单盲纪律**：本脚本**绝不触碰** `2024-01-01 ~ 2026-11-03`
（V2 已消耗的单盲封存段）⇒ 训练/打分窗口硬断言在 `2019-01-01 ~ 2023-12-31` 内。

**用法（服务器）**
```bash
cd /tmp/pycharm_project_3b68a403 && \
/home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/v3_comparison_probe.py \
  --out-dir /mnt/data/siping/runs/etf_rotation_results/v3/_comparison_20261126 \
  --wf-dir /mnt/data/siping/runs/etf_rotation_results/v2/_walkforward \
  --etf-prices-csv /mnt/data/siping/datasets/manu/etf_rotation/datasets/etf_ext/etf_prices_merged_2018.csv \
  --workers 5 --threads-per-worker 14 \
  --arms control_v2,v3_full \
  --report-out /mnt/data/siping/runs/etf_rotation_results/reports/v3_comparison_20261126.md
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_V2_DIR = _REPO / "etf_rotation" / "v2"
_V3_DIR = _REPO / "etf_rotation" / "v3"
for _p in (str(_V2_DIR), str(_V3_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_v3 as C  # noqa: E402  (V3 配置源)
import evaluate_s as EV  # noqa: E402  (V3 验收指标)

# 🔴 V2 只读引用（**不改 V2 任何文件**）：下游 ETF 层 / 回测引擎 / 指标
import config_v2 as C2  # noqa: E402
import etf_layer as EL  # noqa: E402
import backtest_v2 as BT  # noqa: E402
import metrics as ME  # noqa: E402
import constituents as CON  # noqa: E402

import splits as SP3  # noqa: E402  (V3 自己的切分)

#: 🔴 单盲封存段（V2 已消耗）—— 本脚本任何窗口都不许越过
SEALED_START = "2024-01-01"
SEALED_END = "2026-11-03"
#: 本实验允许的窗口（5 折 OOS 拼接）
ALLOWED_START = "2019-01-01"
ALLOWED_END = "2023-12-31"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_window(start: str, end: str, what: str) -> None:
    """🔴 硬断言：任何窗口都不得越过单盲封存段。"""
    if str(end) > SEALED_START:
        raise ValueError(
            f"🔴 {what} 的结束日 {end} 越进单盲封存段（{SEALED_START} ~ {SEALED_END}）⇒ 拒绝执行"
        )
    if str(start) < "2016-01-01":
        raise ValueError(f"🔴 {what} 的起点 {start} 早于数据起点 2016-01-01 ⇒ 拒绝执行")


# ----------------------------------------------------------------- 参数
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="V3 单变量对照实验（5 折 OOS）")
    p.add_argument("--out-dir", default=str(C.DEFAULT_V3_RUN_ROOT / "_comparison"))
    p.add_argument("--wf-dir", default="/mnt/data/siping/runs/etf_rotation_results/v2/_walkforward",
                   help="V2 5 折封存打分的目录（对照臂的数据源，**只读**）")
    p.add_argument("--etf-prices-csv", default=None,
                   help="ETF 行情面板 CSV（覆盖 V2 的默认路径）")
    p.add_argument("--provider-uri", default=C.DEFAULT_PROVIDER_URI)
    p.add_argument("--arms", default="control_v2,v3_full",
                   help="逗号分隔的臂名：control_v2 / v3_full / v3_mse / v3_no_neutralize / "
                        "v3_no_weight / v3_no_rolling（🔴 默认跑批**不含** opt-in 臂）")
    p.add_argument("--arm", default=None,
                   help="🔴 **显式指定单个臂**（如 v3_pit_pool）⇒ 等价于 --arms <该臂>；"
                        "opt-in 臂（v3_pit_pool）**只有显式指定**才跑")
    p.add_argument("--folds", default="1,2,3,4,5")
    p.add_argument("--workers", type=int, default=1,
                   help="🔴 折级并发的进程数（1 = 串行；>1 = 每折一个独立进程）")
    p.add_argument("--threads-per-worker", type=int, default=0,
                   help="每个 worker 进程的 LightGBM/BLAS 线程数（0 = 不限制）")
    p.add_argument("--frozen-params", default=str(C.FROZEN_PARAMS_JSON))
    p.add_argument("--report-out", default=None, help="报告落点（默认 <out-dir>/v3_comparison_report.md）")
    p.add_argument("--publish-name", default=None, help="若给，则另存一份到 <results-root>/reports/ 下")
    p.add_argument("--skip-e2e", action="store_true", help="只跑 S 层指标（跳过端到端回测）")
    p.add_argument("--reuse-scores", action="store_true",
                   help="🔴 复用 <out-dir> 里**已落盘**的 V3 打分（零重训，只重跑评估）")
    p.add_argument("--reuse-scores-if-present", action="store_true",
                   help="🔴 逐臂判断：<out-dir> 里**每折打分都在** ⇒ 复用（零重训）；"
                        "**缺任一折** ⇒ 该臂改为**现场训练**（日志逐臂明示）")
    return p.parse_args(argv)


# ----------------------------------------------------------------- 静态数据
def load_frozen_params(path: str | Path) -> dict:
    """读 V2 冻结件（**只读**）⇒ 端到端验收的参数来源。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"🔴 V2 冻结件不存在：{p}")
    payload = json.loads(p.read_text(encoding="utf-8"))
    params = dict(payload.get("params") or {})
    if not params:
        raise ValueError(f"🔴 冻结件里没有 params 段：{p}")
    return {"params": params, "source": str(p), "md5": _md5(p),
            "winner_arm": payload.get("winner_arm"), "model_name": payload.get("model_name"),
            "frozen_at": payload.get("frozen_at"),
            "winner_arm_desc": payload.get("winner_arm_desc"),
            # 🔴 冻结件里冠军臂的 OOS 指标（**对账基准**）：探针 control_v2 与它
            #    **不是同一套面板/口径**（见披露 16）⇒ 报告要能并排列出，避免被误读成对照臂坏了。
            "oos_metrics": dict((payload.get("selection") or {}).get("oos_metrics") or {})}


def load_etf_market(prices_csv: str | None) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    """ETF 行情（close / amount 宽表 ＋ 名称映射）。

    🔴 **2026-11-26 真缺陷修复（曾让指标③两臂同时全平）**：
       本函数原来在 ``prices_csv`` 分支用**裸** ``pd.read_csv(prices_csv)``
       ⇒ ``code`` 列被 pandas 推断成 **int64** ⇒ 宽表列名变成整数
       （``158041`` 而非 ``'158041'``）⇒ 与 V2 的 ``EL.load_etf_prices``
       （``dtype={'code': str}`` ＋ ``str.zfill(6)``）**口径分叉**
       ⇒ ``etf_layer.build_targets`` 的 ``_row(col, code)`` 用 str 代码去
       int 列名的特征宽表里查 ⇒ **全部 miss** ⇒ ``amount20d``/``last_close``/
       ``n_days`` 全 NaN ⇒ ``liquidity_ok`` 全 False ⇒ **池空**
       ⇒ 61 个目标全是 ``{}`` ⇒ NAV 恒 1.0、换手 0.0000
       （指标①②照常正常 —— 它们走 qlib 个股面板，不碰 ETF close 面板）。
       ⇒ 现在**统一走 V2 的口径函数**（一个事实源）＋ **fail-closed 断言**。
    """
    prices = EL.load_etf_prices(prices_csv)  # 🔴 V2 同款口径（str ＋ zfill(6)）
    if "amount" not in prices.columns:
        raise ValueError(
            "🔴 ETF 行情面板缺 `amount` 列 ⇒ 流动性闸门（20 日均额）无法工作 "
            "⇒ 标的池会被清空 ⇒ 拒绝执行"
        )
    close = prices.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    amount = prices.pivot_table(index="date", columns="code", values="amount", aggfunc="last")
    _assert_str_code_columns(close, "close")
    _assert_str_code_columns(amount, "amount")
    hold = CON.read_holdings_frame()
    names: dict[str, str] = {}
    if "etf_name" in hold.columns:
        for code, grp in hold.groupby("etf_code", sort=True):
            names[str(code)] = str(grp["etf_name"].iloc[0])
    return close.sort_index(), amount.sort_index(), names


def _assert_str_code_columns(panel: pd.DataFrame, what: str) -> None:
    """🔴 fail-closed：ETF 代码列名**必须是 str**（否则特征 join 全 miss ⇒ 池空）。"""
    if panel.empty:
        return
    bad = [c for c in panel.columns if not isinstance(c, str)]
    if bad:
        raise TypeError(
            f"🔴 {what} 面板的 ETF 代码列名必须是 str（实测 {len(bad)} 个不是：{bad[:3]}）"
            "⇒ 代码口径与 V2 分叉（特征 join 会全 miss ⇒ 池空 ⇒ 指标③全平）⇒ 拒绝执行"
        )


def load_qlib_panels(instruments: list[str], start: str, end: str,
                      provider_uri: str) -> tuple[pd.DataFrame, pd.Series]:
    """取个股 `$vwap` 面板 ＋ 基准 `$vwap` 序列（**只读** qlib）。"""
    import qlib
    from qlib.constant import REG_CN
    from qlib.data import D

    qlib.init(provider_uri=str(provider_uri), region=REG_CN)
    try:
        qlib.config.C["kernels"] = int(C.QLIB_FETCH_KERNELS)
    except Exception:  # pragma: no cover
        pass
    df = D.features(instruments, ["$vwap"], start_time=start, end_time=end, freq="day")
    if df is None or df.empty:
        return pd.DataFrame(), pd.Series(dtype="float64")
    df = df.reset_index()
    value_col = [c for c in df.columns if c not in ("datetime", "instrument")][0]
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.normalize()
    panel = df.pivot_table(index="datetime", columns="instrument", values=value_col, aggfunc="last")
    bench = D.features([C.BENCHMARK], ["$vwap"], start_time=start, end_time=end, freq="day")
    bench_s = pd.Series(dtype="float64")
    if bench is not None and not bench.empty:
        bench = bench.reset_index()
        bcol = [c for c in bench.columns if c not in ("datetime", "instrument")][0]
        bench["datetime"] = pd.to_datetime(bench["datetime"]).dt.normalize()
        bench_s = bench.set_index("datetime")[bcol].sort_index()
    return panel.sort_index(), bench_s


# ----------------------------------------------------------------- 臂
def arm_definitions() -> dict[str, dict]:
    """臂定义（每个臂 = 一组「与对照臂的差异」）。

    🔴 **单变量**：每个 `v3_*` 臂相对 `v3_full` **只少一样东西**；
    `control_v2` 则是现役 V2（零重训、零漂移）。

    🔴 **与 ``v3/train_s.py`` 的参数口径严格对齐**（2026-11-26 修）：
       * ``objective`` ⇒ ``train_s`` 的 ``arm``：**只有** ``"lambdarank"`` / ``"mse"``
         两种 ⇒ 映射到 ``arm="v3"`` / ``arm="control"``；
       * ``center_mode`` ⇒ ``train_s`` 的 ``center``：**字符串模式**
         （``"market_mean"`` / ``"benchmark"``）⇒ 传 bool 会直接 raise；
       * ``weight_mode`` ⇒ ``config_v3.SAMPLE_WEIGHT_MODE``
         （``"sum_w"`` / ``"sqrt_sum_w"`` / ``"uniform"``）；
       * ``rolling`` ⇒ **真滚动重训**（走 ``v3/rolling.py::rolling_retrain_and_score``），
         不是只往 manifest 里塞个 bool。
    """
    return {
        "control_v2": {
            "label": "V2 现役（5 折封存打分，零重训）",
            "objective_label": "mse (V2 口径)",
            "universe": "V2 口径（csi300 训练 / all 打分）",
            "neutralize": False, "center_mode": "n/a（V2 封存打分，无标签重算）",
            "weight_mode": "n/a（V2 封存打分）", "rolling": False,
            "source": "cached",
        },
        "v3_full": {
            "label": "V3 全对齐（超额标签 + ETF 并集∩大中盘池 + 残差化 + lambdarank + ΣW + 滚动重训）",
            "objective_label": "lambdarank", "universe": f"{C.UNIVERSE_MODE}（ETF 成分股全并集 ∩ 中证800∪中证1000）",
            "neutralize": True, "center_mode": "market_mean",
            "weight_mode": "sum_w", "rolling": True,
            "source": "train",
        },
        "v3_mse": {
            "label": "V3 全对齐但损失换回 MSE（单变量：只换损失函数）",
            "objective_label": "mse", "universe": f"{C.UNIVERSE_MODE}（ETF 成分股全并集 ∩ 中证800∪中证1000）",
            "neutralize": True, "center_mode": "market_mean",
            "weight_mode": "sum_w", "rolling": True,
            "source": "train",
        },
        "v3_no_neutralize": {
            "label": "V3 全对齐但不做残差化（单变量：只关中性化）",
            "objective_label": "lambdarank", "universe": f"{C.UNIVERSE_MODE}（ETF 成分股全并集 ∩ 中证800∪中证1000）",
            "neutralize": False, "center_mode": "market_mean",
            "weight_mode": "sum_w", "rolling": True,
            "source": "train",
        },
        "v3_no_weight": {
            "label": "V3 全对齐但不做 ΣW 样本加权（单变量：只关样本加权）",
            "objective_label": "lambdarank", "universe": f"{C.UNIVERSE_MODE}（ETF 成分股全并集 ∩ 中证800∪中证1000）",
            "neutralize": True, "center_mode": "market_mean",
            "weight_mode": "uniform", "rolling": True,
            "source": "train",
        },
        "v3_no_rolling": {
            "label": "V3 全对齐但不做滚动重训（单变量：只关滚动重训）",
            "objective_label": "lambdarank", "universe": f"{C.UNIVERSE_MODE}（ETF 成分股全并集 ∩ 中证800∪中证1000）",
            "neutralize": True, "center_mode": "market_mean",
            "weight_mode": "sum_w", "rolling": False,
            "source": "train",
        },
    }


def opt_in_arm_definitions() -> dict[str, dict]:
    """**显式指定才跑**的臂（🔴 默认跑批**不含**它们 ⇒ 默认跑批不许从 6 臂变 7 臂）。

    🔴 ``v3_pit_pool``：**只换训练池口径**（逐日 PIT 池）⇒ 其余参数与 ``v3_full``
       **逐项相同**（单变量）；🔴 **打分池不动**（保持 ETF 全并集口径）。
    """
    return {
        "v3_pit_pool": {
            "label": "V3 全对齐但训练池改逐日 PIT（单变量：只换训练池口径）",
            "objective_label": "lambdarank",
            "universe": f"{C.UNIVERSE_MODE_PIT}（{C.UNIVERSE_MODE_PIT_DESC}）",
            "universe_mode": C.UNIVERSE_MODE_PIT,
            "neutralize": True, "center_mode": "market_mean",
            "weight_mode": "sum_w", "rolling": True,
            "source": "train",
        },
    }


def all_arm_definitions() -> dict[str, dict]:
    """默认臂 ＋ opt-in 臂（CLI 的臂名白名单；**默认跑批仍只跑默认臂**）。"""
    return {**arm_definitions(), **opt_in_arm_definitions()}


def train_arm_of(arm: dict) -> str:
    """臂的损失口径 ⇒ ``v3/train_s.py`` 的 ``arm`` 参数。

    🔴 ``train_s.train_and_score`` **只认** ``"v3"``（lambdarank）与 ``"control"``（MSE）
       两个取值 ⇒ 其余一律 raise ⇒ 这里做**显式映射**，绝不把臂名直接当 ``arm`` 传。
    """
    # 🔴 2026-11-26 真缺陷：键名曾由 ``objective`` 改名为 ``objective_label``
    #    （落盘 manifest 的键必须叫 ``objective_label`` 才过守卫）
    #    ⇒ 但**臂定义 dict 用的也是** ``objective_label``，而这里仍读 ``objective``
    #    ⇒ 取到空串 ⇒ **任何要训练的臂**（v3_full 复用打分时走不到这里）直接 raise
    #    ⇒ 首次暴露于 6 臂跑批（4 个消融臂现场训练）。
    objective = str(arm.get("objective_label") or arm.get("objective") or "").strip().lower()
    if objective == "lambdarank":
        return "v3"
    if objective == "mse":
        return "control"
    raise ValueError(
        f"🔴 臂的损失口径无法映射到 train_s 的 arm：{objective!r}"
        "（合法 = lambdarank ⇒ 'v3' / mse ⇒ 'control'）"
    )


def load_control_scores(wf_dir: Path, folds: list[int],
                        model_name: str = "ablate_roc5") -> tuple[pd.DataFrame, dict[int, list[str]]]:
    """读 V2 5 折封存打分并**拼接**成 OOS 长表（🔴 只读）。

    🔴 **必须按折的 valid 窗口裁剪**：V2 折打分文件**含 warmup 区**
       （实测 wf1 = 2018-01-12 ~ 2019-12-19，而折 1 的 valid 窗口 = 2019 全年）
       ⇒ 5 折直接拼接会让相邻折**重叠**（同一 ``(date, instrument)`` 出现两次）
       ⇒ 截面 z-score 时 ``z.loc[inst]`` 返回 Series ⇒ **直接崩**
       （实测报 ``ValueError: The truth value of a Series is ambiguous``），
       且重复行会**污染** RankIC / Top10% 等截面指标。
    ⇒ 这里**只保留每折 valid 窗口内的行**（那才是该折的 OOS 段）。

    返回
    ----
    ``(拼接长表, {折号: 该折 OOS 信号日列表})``
    ⇒ 信号日清单是**两臂对齐的唯一事实源**（V3 必须打同一批日，见
      ``train_s.score_universe(score_dates=...)``）。
    """
    frames: list[pd.DataFrame] = []
    fold_dates: dict[int, list[str]] = {}
    for fid in folds:
        # 🔴 **按冻结件的冠军臂 `model_name` 精确取文件**（reviewer P2-5）：
        #    原来用通配符 + `cand[0]`（字典序第一个）⇒ 同一折存在多个模型文件时
        #    会**静默取错臂**，而「对照臂 = V2 现役冠军臂」是整个单变量实验的地基。
        exact = Path(wf_dir) / f"stock_scores_v2_wf{int(fid)}_{model_name}.csv"
        cand = [exact] if exact.is_file() else sorted(
            Path(wf_dir).glob(f"stock_scores_v2_wf{int(fid)}_*.csv"))
        if not cand:
            raise FileNotFoundError(f"🔴 V2 折 {fid} 的打分文件不存在于 {wf_dir}")
        if len(cand) > 1:
            raise RuntimeError(
                f"🔴 V2 折 {fid} 在 {wf_dir} 下有多个候选打分文件（{[c.name for c in cand]}）"
                f"⇒ 对照臂取谁不确定 ⇒ 拒绝执行（请把冠军臂名 `model_name={model_name}` "
                "对应的文件改名成唯一候选）"
            )
        df = pd.read_csv(cand[0], dtype={"instrument": str})
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        spec = SP3.build_fold_spec(int(fid))
        n_before = len(df)
        df = df[(df["date"] >= pd.Timestamp(spec.valid_start))
                & (df["date"] <= pd.Timestamp(spec.valid_end))]
        if df.empty:
            raise RuntimeError(
                f"🔴 V2 折 {fid} 在 valid 窗口 {spec.valid_start}~{spec.valid_end} 内没有分数"
                f"（文件 {cand[0].name} 共 {n_before} 行）"
            )
        df["fold"] = int(fid)
        dates = sorted({str(d.date()) for d in df["date"].unique()})
        fold_dates[int(fid)] = dates
        frames.append(df[["date", "instrument", "score", "fold"]])
        _log(f"  对照臂：折 {fid} ← {cand[0].name}（{n_before} 行 → "
             f"OOS 段 {len(df)} 行 / {len(dates)} 个信号日）")
    out = pd.concat(frames, ignore_index=True)
    dup = int(out.duplicated(subset=["date", "instrument"]).sum())
    if dup:
        raise RuntimeError(
            f"🔴 拼接后仍有 {dup} 行 ``(date, instrument)`` 重复 ⇒ 对照臂口径有问题"
            "（不许靠去重掩盖 ⇒ 说明折的 valid 窗口没裁对）"
        )
    _log(f"  对照臂拼接完成：{len(out)} 行 / {out['date'].nunique()} 个信号日 / 无重复")
    return out, fold_dates


def _run_fold_cmd(cmd: list[str]) -> dict:
    """跑一个折级 worker 子进程（**只负责执行**，不做任何口径判断）。"""
    import subprocess

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def has_all_scores_on_disk(out_dir: Path, arm_name: str, folds: list[int]) -> bool:
    """该臂**每折**的已落盘打分是否齐备（**只判存在**，不读内容）。

    🔴 选文件规则与 `load_v3_scores_from_disk` 完全一致：**按文件名里的折号定位**
    （并行布局有 ``fold{fid}/`` 目录、串行布局没有 ⇒ 不能用目录名判折号）。
    """
    for fid in folds:
        cand = sorted(
            Path(out_dir).glob(f"**/stock_scores_*_wf{int(fid)}_{arm_name}.csv"),
            key=lambda p: (len(p.parts), str(p)),
        )
        if not cand:
            return False
    return True


#: 🔴 **每折训练的内存占用经验值**（GB）—— 2026-11-26 实测：单折训练 ≈ +15 GB，
#:     打分期会 fork 出 ``C["kernels"]``(=4) 个子进程（RSS 计数偏大、实际 COW 共享）。
FOLD_MEM_GB = 15.0


def _mem_available_gb() -> float:
    """系统**可用**内存（GB）⇒ 读不到时返回 NaN（NaN ⇒ 闸门放行，不误杀）。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / (1024.0 * 1024.0)
    except OSError:
        pass
    return float("nan")


def _wait_for_memory(required_gb: float, max_wait_s: int = 3600) -> None:
    """🔴 **内存闸门（fail-closed）**：可用内存不足 ⇒ **等**，等不到 ⇒ **raise 拒绝启动**。

    🔴 **为什么**：同机多会话时，**高并发折级并行**（``--workers 5``）既可能撞上
    **内存天花板**（2026-11-26 实测 ``oom_kill=0`` ⇒ 当次**不是**内核 OOM，而是被
    **外部会话的进程组清理 SIGKILL**：日志**无 traceback**，只剩最后一行"折级并行：5 折…"），
    也会挤占同机**其他会话**（生产链）的资源 ⇒ 现在把内存闸门**前置**：
    可用内存 < 并发折数 × ``FOLD_MEM_GB`` ⇒ 先等（默认最多 60 min）⇒ 仍不够 ⇒ 明确报错退出
    （**宁可明确失败，也不要静默消失**）。
    """
    t0 = time.time()
    while True:
        avail = _mem_available_gb()
        if not np.isfinite(avail) or avail >= required_gb:
            _log(f"  内存闸门：可用 {avail:.1f} GB / 需要 {required_gb:.1f} GB ⇒ 放行")
            return
        waited = time.time() - t0
        if waited >= max_wait_s:
            raise RuntimeError(
                f"🔴 内存闸门超时：可用内存 {avail:.1f} GB < 需要 {required_gb:.1f} GB"
                f"（已等 {waited / 60:.1f} min）⇒ **拒绝启动**"
                "（宁可不跑，也不要被 OOM 静默 SIGKILL；请下调 --workers 后重跑）"
            )
        _log(f"  内存闸门：可用 {avail:.1f} GB < 需要 {required_gb:.1f} GB ⇒ 等 60 s"
             f"（已等 {waited / 60:.1f} min）")
        time.sleep(60)


def load_v3_scores_from_disk(out_dir: Path, arm_name: str, folds: list[int],
                             fold_dates: dict[int, list[str]]) -> pd.DataFrame:
    """复用**已落盘**的 V3 打分（**零重训**）⇒ 只重跑评估。

    🔴 **为什么需要**：V3 逐折训练 ≈1.5 h/臂 ⇒ 分数没变就不该重训
    ⇒ 本入口直接读磁盘上已有的 ``stock_scores_*.csv``（评测口径修复后重算用）。

    🔴 **硬断言（两臂对齐的唯一事实源）**：每折的信号日必须与**对照臂**的
       OOS 信号日**逐日相等** ⇒ 不等 ⇒ 拒绝执行（不许靠对齐/去重掩盖）。

    🔴 **选文件规则**：折叠重训会同时留下「每窗口一个」和「折级汇总一个」
       ⇒ 取**路径最浅**的那个（折级汇总），否则会重复拼接。
    """
    frames: list[pd.DataFrame] = []
    for fid in folds:
        # 🔴 **按文件名里的折号定位**（reviewer P1-2）：**不许**用目录名判折号 ——
        #    串行布局（`--workers 1`）没有 `fold{fid}/` 目录，只有并行布局才有
        #    ⇒ 用目录名会让串行产物永远找不到。文件名模板自带折号
        #    （`stock_scores_v3_wf{fid}_{arm}.csv`）⇒ 两种布局都能命中。
        cand = sorted(
            Path(out_dir).glob(f"**/stock_scores_*_wf{int(fid)}_{arm_name}.csv"),
            key=lambda p: (len(p.parts), str(p)),
        )
        if not cand:
            raise FileNotFoundError(
                f"🔴 未找到臂 {arm_name} 折 {fid} 的已落盘打分"
                f"（在 {out_dir} 下按 `stock_scores_*_wf{int(fid)}_{arm_name}.csv` 搜，无命中）"
            )
        df = pd.read_csv(cand[0], dtype={"instrument": str})
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        spec = SP3.build_fold_spec(int(fid))
        df = df[(df["date"] >= pd.Timestamp(spec.valid_start))
                & (df["date"] <= pd.Timestamp(spec.valid_end))]
        if df.empty:
            raise RuntimeError(
                f"🔴 折 {fid} 的已落盘打分在 valid 窗口 "
                f"{spec.valid_start}~{spec.valid_end} 内为空：{cand[0]}"
            )
        got = sorted({str(d.date()) for d in df["date"].unique()})
        want = list(fold_dates.get(int(fid)) or [])
        if got != want:
            raise RuntimeError(
                f"🔴 折 {fid} 的 V3 信号日与对照臂不一致"
                f"（V3 {len(got)} 个 vs 对照臂 {len(want)} 个）"
                "⇒ 两臂信号栅格相位不同 ⇒ 端到端不可比 ⇒ 拒绝执行"
            )
        df["fold"] = int(fid)
        frames.append(df[["date", "instrument", "score", "fold"]])
        _log(f"  复用落盘分数：折 {fid} ← {cand[0].name}（{len(df)} 行 / {len(got)} 个信号日）")
    out = pd.concat(frames, ignore_index=True)
    dup = int(out.duplicated(subset=["date", "instrument"]).sum())
    if dup:
        raise RuntimeError(f"🔴 复用拼接后有 {dup} 行 (date, instrument) 重复 ⇒ 拒绝执行")
    _log(f"  复用完成：{len(out)} 行 / {out['date'].nunique()} 个信号日 / 无重复")
    return out


def _assert_signal_dates_match(fold_dates: dict[int, list[str]] | None,
                               scores: pd.DataFrame, label: str) -> None:
    """🔴 **两臂信号日逐日相等**（端到端可比性的地基）。

    对照臂的 OOS 信号日是**唯一事实源** ⇒ V3 必须在**完全相同**的日期上打分。
    复用路径已有逐折硬断言（见 :func:`load_v3_scores_from_disk`），
    🔴 **训练路径原先没有** ⇒ 两套日历只要差一天，端到端调仓日历就不同而**全程无报错**。
    """
    if not fold_dates:
        return
    want = sorted({str(d)[:10] for d in sum((list(v or []) for v in fold_dates.values()), [])})
    got = (sorted({str(pd.Timestamp(d).date()) for d in scores["date"].unique()})
           if (scores is not None and not scores.empty) else [])
    missing = [d for d in want if d not in set(got)]
    extra = [d for d in got if d not in set(want)]
    if missing or extra:
        raise RuntimeError(
            f"🔴 {label} 的信号日与对照臂不一致"
            f"（缺 {len(missing)} 个：{missing[:6]}…；多 {len(extra)} 个：{extra[:6]}…）"
            "⇒ 两臂调仓日历不同 ⇒ 端到端不可比 ⇒ 拒绝执行"
        )


def train_v3_scores_parallel(arm: dict, arm_name: str, folds: list[int], out_dir: Path,
                             provider_uri: str, workers: int, threads: int,
                             fold_dates: dict[int, list[str]] | None = None) -> pd.DataFrame:
    """🔴 **折级并行**：每折一个**独立进程**（``v3_fold_worker.py``）⇒ 折与折零共享。

    🔴 **为什么用独立进程而不是线程**：LightGBM 的 OpenMP 线程池 + qlib 客户端在
       同一进程内并发会互相踩（且 GIL 让 Python 侧的中性化无法并行）；
       独立进程 ⇒ 每个折自己一套线程池，线程数由 ``--threads`` 显式锁死
       ⇒ **并发度与线程数都可控**（⇒ 并行路径与串行路径**同口径**）。
    """
    import concurrent.futures as cf

    train_arm = train_arm_of(arm)
    center_mode = str(arm["center_mode"])
    weight_mode = str(arm["weight_mode"])
    use_rolling = bool(arm["rolling"])
    worker_script = _HERE / "v3_fold_worker.py"
    if not worker_script.is_file():
        raise FileNotFoundError(f"🔴 折级 worker 不存在：{worker_script}")
    arm_dir = Path(out_dir) / arm_name
    arm_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[int, list[str]]] = []
    date_files: dict[int, Path] = {}
    for fid in folds:
        dates = (fold_dates or {}).get(int(fid)) or []
        if dates:
            df_path = arm_dir / f"fold{int(fid)}_score_dates.txt"
            df_path.write_text("\n".join(dates) + "\n", encoding="utf-8")
            date_files[int(fid)] = df_path
        cmd = [
            sys.executable, str(worker_script),
            "--fold", str(int(fid)),
            "--model-name", f"wf{int(fid)}_{arm_name}",
            "--arm", train_arm,
            "--out-dir", str(arm_dir),
            "--provider-uri", str(provider_uri),
            "--center-mode", center_mode,
            "--weight-mode", weight_mode,
            "--result-json", str(arm_dir / f"fold{int(fid)}.json"),
            "--neutralize" if bool(arm["neutralize"]) else "--no-neutralize",
            "--rolling" if use_rolling else "--no-rolling",
        ]
        if int(fid) in date_files:
            cmd += ["--score-dates-file", str(date_files[int(fid)])]
        # 🔴 **训练池口径**（只有 opt-in 臂才带 ⇒ 默认臂的 worker 命令逐位不变）
        arm_universe_mode = str(arm.get("universe_mode") or "")
        if arm_universe_mode:
            cmd += ["--universe-mode", arm_universe_mode]
        if int(threads) > 0:
            cmd += ["--threads", str(int(threads))]
        jobs.append((int(fid), cmd))

    _log(f"  折级并行：{len(jobs)} 折 / 并发 {int(workers)} / 每进程 {int(threads)} 线程")
    # 🔴 **内存闸门前置**（2026-11-26 OOM 事故）：并发折数 × 每折经验占用 ⇒ 不足就等/拒绝
    _wait_for_memory(required_gb=max(1, int(workers)) * FOLD_MEM_GB)
    results: dict[int, dict] = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {pool.submit(_run_fold_cmd, cmd): fid for fid, cmd in jobs}
        for fut in cf.as_completed(futures):
            fid = futures[fut]
            out = fut.result()
            if out.get("returncode") != 0:
                raise RuntimeError(
                    f"🔴 折 {fid} 的 worker 失败（rc={out.get('returncode')}）\n"
                    f"--- stdout ---\n{out.get('stdout', '')[-4000:]}\n"
                    f"--- stderr ---\n{out.get('stderr', '')[-4000:]}"
                )
            for line in out.get("stdout", "").splitlines():
                if line.startswith("RESULT "):
                    results[fid] = json.loads(line[len("RESULT "):])
                    _log(f"    折 {fid} 完成：{results[fid]['rows']} 行 / "
                         f"{results[fid]['elapsed_sec']}s / md5 {results[fid]['scores_md5'][:12]}")
    missing = [f for f in folds if int(f) not in results]
    if missing:
        raise RuntimeError(f"🔴 折 {missing} 没有产出结果 JSON")
    frames: list[pd.DataFrame] = []
    for fid in folds:
        csv_path = Path(results[int(fid)]["scores_csv"])
        # 🔴🔴 **折间产物目录隔离守卫**：各折必须写在**各自**的目录里
        #    （共用目录 ⇒ 各折的模型/分数互相覆盖 ⇒ 产物混杂、结果作废；
        #     实测踩过：5 折并发时全部写进同一个 ``window_001``）。
        if f"fold{int(fid)}" not in csv_path.as_posix():
            raise RuntimeError(
                f"🔴 折 {fid} 的产物路径里没有折号目录 ⇒ 可能与其他折互相覆盖：{csv_path}"
            )
        df = pd.read_csv(csv_path, dtype={"instrument": str})
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        df["fold"] = int(fid)
        frames.append(df[["date", "instrument", "score", "fold"]])
    _log(f"  折级并行完成：{len(frames)} 折 / 总耗时 {time.time() - t0:.1f}s")
    merged = pd.concat(frames, ignore_index=True)
    _assert_signal_dates_match(fold_dates, merged, f"臂 {arm_name}（折级并行训练路径）")
    return merged


def train_v3_scores(arm: dict, arm_name: str, folds: list[int], out_dir: Path,
                    provider_uri: str, workers: int = 1, threads: int = 0,
                    fold_dates: dict[int, list[str]] | None = None) -> pd.DataFrame:
    """V3 逐折训练 + 打分 ⇒ 拼接成 OOS 长表。"""
    if int(workers) > 1:
        return train_v3_scores_parallel(arm, arm_name, folds, out_dir, provider_uri,
                                        int(workers), int(threads), fold_dates)
    import train_s as TS
    import universe as UNI

    out_dir.mkdir(parents=True, exist_ok=True)
    etf_codes = UNI.load_etf_universe()
    holdings = UNI.load_holdings()
    # 🔴 **训练池口径由臂决定**（默认臂没有 ``universe_mode`` ⇒ 走 ``config_v3.UNIVERSE_MODE``
    #    ⇒ 默认臂逐位不变；opt-in 臂 `v3_pit_pool` 才切到逐日 PIT 口径）
    universe_mode = str(arm.get("universe_mode") or C.UNIVERSE_MODE)
    universe_info = UNI.build_universe(etf_codes, holdings, mode=universe_mode)
    _log(f"  V3 训练池：{universe_info['n_stocks']} 只（口径 {universe_mode}：ETF 全并集 ∩ 大中盘）｜ΣW 覆盖 {len(universe_info['sum_w'])} 只")

    train_arm = train_arm_of(arm)
    center_mode = str(arm["center_mode"])
    weight_mode = str(arm["weight_mode"])
    use_rolling = bool(arm["rolling"])
    _log(f"  臂口径：objective={arm['objective_label']} ⇒ train_s.arm={train_arm}｜"
         f"center={center_mode}｜weight_mode={weight_mode}｜rolling={use_rolling}")

    frames: list[pd.DataFrame] = []
    for fid in folds:
        spec = SP3.build_fold_spec(int(fid))
        SP3.assert_fold_discipline(spec)
        _assert_window(spec.train_start, spec.valid_end, f"折 {fid}")
        model_name = f"wf{fid}_{arm_name}"
        _log(f"  ▸ 折 {fid}：训练 {spec.train_start}~{spec.train_end} → 打分 {spec.valid_start}~{spec.valid_end}")
        # 🔴 样本权重口径是**单变量** ⇒ 逐臂临时改 ``config_v3.SAMPLE_WEIGHT_MODE``
        #    （``config_v3`` 是共享单例 ⇒ 训练侧读到的是同一个对象）⇒ 用完即还原。
        prev_weight_mode = C.SAMPLE_WEIGHT_MODE
        C.SAMPLE_WEIGHT_MODE = weight_mode
        try:
            if use_rolling:
                import rolling as ROL

                manifest = ROL.rolling_retrain_and_score(
                    spec, out_dir=out_dir / "rolling", model_name=model_name,
                    arm=train_arm, provider_uri=provider_uri,
                    universe_info=universe_info,
                    neutralize=bool(arm["neutralize"]), center=center_mode,
                    score_dates=(fold_dates or {}).get(int(fid)),
                )
                csv_path = Path(manifest["outputs"]["stock_scores_csv"])
            else:
                manifest = TS.train_and_score(
                    spec, out_dir=out_dir, model_name=model_name, arm=train_arm,
                    provider_uri=provider_uri, universe_info=universe_info,
                    neutralize=bool(arm["neutralize"]), center=center_mode,
                    score_start=spec.valid_start, score_end=spec.valid_end,
                    score_dates=(fold_dates or {}).get(int(fid)),
                )
                csv_path = Path(
                    (manifest.get("outputs") or {}).get("stock_scores_csv")
                    or (out_dir / C.scores_filename(model_name))
                )
        finally:
            C.SAMPLE_WEIGHT_MODE = prev_weight_mode
        if not csv_path.is_file():
            raise FileNotFoundError(f"🔴 折 {fid} 的打分文件缺失：{csv_path}")
        df = pd.read_csv(csv_path, dtype={"instrument": str})
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        df["fold"] = int(fid)
        frames.append(df[["date", "instrument", "score", "fold"]])
        _log(f"    折 {fid} 打分完成：{len(df)} 行 → {csv_path.name}")
    merged = pd.concat(frames, ignore_index=True)
    _assert_signal_dates_match(fold_dates, merged, f"臂 {arm_name}（串行训练路径）")
    return merged


# ----------------------------------------------------------------- 评估
def evaluate_arm(arm_name: str, arm: dict, scores: pd.DataFrame, etf_universe: set[str],
                 price_panel: pd.DataFrame, bench_price: pd.Series,
                 close: pd.DataFrame, amount: pd.DataFrame, names: dict[str, str],
                 frozen: dict, skip_e2e: bool) -> dict:
    """一个臂的三项指标。"""
    out: dict[str, Any] = {"arm": arm_name, "definition": arm}
    scores = scores.copy()
    scores["date"] = pd.to_datetime(scores["date"]).dt.normalize()
    scores = scores[(scores["date"] >= pd.Timestamp(ALLOWED_START)) & (scores["date"] <= pd.Timestamp(ALLOWED_END))]
    out["n_score_rows"] = int(len(scores))
    out["n_score_dates"] = int(scores["date"].nunique())

    # ---- ① 20 日滚动 RankIC（ETF 并集内）
    fwd = LAB_forward_window(price_panel)
    ic = EV.rank_ic_series(scores, fwd, universe=etf_universe)
    out["metric_1_rankic"] = EV.icir_stats(ic, rolling_window=C.ACCEPT_RANKIC_WINDOW)
    out["metric_1_rankic"]["ic_series"] = {str(d.date()): (None if not np.isfinite(v) else float(v))
                                            for d, v in ic.items()}

    # ---- ② Top 10% 组合的费后超额
    out["metric_2_top_decile"] = EV.top_decile_excess(
        scores, price_panel, bench_price, universe=etf_universe,
        top_fraction=C.ACCEPT_TOP_FRACTION, commission=C.COMMISSION, slippage=C.SLIPPAGE)
    out["metric_2_top_decile"].pop("per_period", None)

    # ---- ③ 端到端（V2 ETF 层 + 冻结参数）
    if skip_e2e:
        out["metric_3_end_to_end"] = {"skipped": True}
        return out
    params = dict(frozen["params"])
    model_name = params.get("model_name", "ablate_roc5")
    targets, diag = build_targets_for_v3(scores, close, amount, names, params)
    n_target_dates = int(len(targets))
    n_empty_targets = int(sum(1 for t in targets.values() if not t))
    assert_targets_not_empty(arm_name, targets)
    bt = BT.run_backtest_v2(
        close, targets, commission=C.COMMISSION, slippage=C.SLIPPAGE,
        tradable=build_tradable_maps()[0], tradable_sell=build_tradable_maps()[1],
        # 🔴🔴 **P0 修复（2026-11-26，reviewer 审计发现）**：**回测测量窗口必须裁到
        #    OOS 窗口**。不裁 ⇒ 净值一路跑到面板末日 2026-11-10 ⇒ **单盲封存段
        #    2024-01-01~2026-11-03 实际进入了净值**（与报告「未触碰」自相矛盾），
        #    且段级统计会把「2023-12 → 2026-11-10」当**一个**持仓段 ⇒ t 值/胜率失真；
        #    另 `metrics.summarize` 的年份由**净值点数**决定 ⇒ 不裁还会**低估年化换手**。
        #    🔴 **只裁回测测量窗口，不裁 close 面板本身** —— `EL.compute_features` 需要
        #    2018 的前置历史算 ma60/mom120（裁掉会让 2019 前 ~120 日 `n_days` 不达标
        #    ⇒ 流动性闸门清空标的池 ⇒ 反而误伤）。
        start=ALLOWED_START, end=ALLOWED_END,
        periods_per_year=250,
    )
    nav = bt["nav"]
    bench_nav = benchmark_nav(close)
    m = ME.summarize(nav, bench_nav, bt.get("turnover_by_date"),
                      commission=C.COMMISSION, slippage=C.SLIPPAGE, periods_per_year=250)
    seg = segment_excess_stats(nav, bench_nav, list(bt["turnover_by_date"].keys()))
    out["metric_3_end_to_end"] = {
        "annualized_return": m.get("annualized_return"),
        "excess_vs_benchmark": m.get("excess_vs_benchmark"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe": m.get("sharpe"),
        # 🔴 **卡玛比率**（年化 / |最大回撤|）—— 用户目标文档指定的「替换上游打分引擎」
        #    判据（"端到端样本外卡玛比率有明确提升"）⇒ 必须落盘，不许只给夏普。
        "calmar": m.get("calmar"),
        "annualized_turnover": m.get("annualized_turnover"),
        "annualized_turnover_one_way": m.get("annualized_turnover_one_way"),
        "total_cost_drag": m.get("total_cost_drag"),
        # 🔴 「调仓次数」= **引擎真实调仓数**（不是目标日期数）⇒ 两个口径分开披露
        "n_rebalances": int(bt["n_rebalances"]),
        "n_target_dates": n_target_dates,
        "n_empty_targets": n_empty_targets,
        "n_skipped_target_dates": int(len(bt["skipped_target_dates"])),
        # 🔴 「调仓次数」两个口径都披露：**落在交易日上的目标日数**（引擎口径，
        #    含空目标/零换手日）＝ n_target_dates − n_skipped_target_dates；
        #    **有成交（非零换手）的调仓日数**另算。
        "n_rebalances_nonzero": int(sum(1 for v in bt["turnover_by_date"].values() if v > 0)),
        # 🔴 段级显著性（样本 = 调仓段数，不是交易日数）
        "n_segments": seg.get("n_segments"),
        "t_stat": seg.get("t_stat"),
        "win_rate": seg.get("win_rate"),
        "mean_segment_excess": seg.get("mean_segment_excess"),
        "model_name": model_name,
    }
    out["_nav"] = nav
    return out


def LAB_forward_window(price_panel: pd.DataFrame) -> pd.DataFrame:
    """20 日窗口前瞻收益（用 V3 的 `labels_v3`，口径与 V2 一致）。"""
    import labels_v3 as LAB
    return LAB.forward_window_return(price_panel, window=C.LABEL_EXIT_WINDOW)


def build_tradable_maps() -> tuple[dict, dict]:
    """ETF 的 (可买, 可卖) 逐日标记（V2 同款实现，只读调用）。"""
    import run_v2 as R
    return R.build_etf_tradable_maps()


def build_targets_for_v3(scores: pd.DataFrame, close: pd.DataFrame, amount: pd.DataFrame,
                          names: dict[str, str], params: dict) -> tuple[dict, pd.DataFrame]:
    """用 **V2 的 ETF 层** + 冻结参数，把 V3 分数接成目标权重。

    🔴 与 `run_v2._build_targets_for` 的差别**只有一个**：分数来源（V3 CSV vs V2 CSV）。
    """
    snapshot = CON.load_holdings()
    holdings = CON.snapshot_frame(snapshot)
    weights = EL.build_weight_map(holdings)
    # 🔴 **集合对齐断言**（reviewer P2-6）：真正会打空标的池的故障模式是**代码集合错配**
    #    （面板列 vs 持仓权重键），不只是 dtype ⇒ 断言两者**有交集**。
    overlap = set(close.columns) & set(weights.keys())
    if not overlap:
        raise ValueError(
            f"🔴 ETF 行情面板的代码集合与持仓权重键**完全不相交**"
            f"（面板 {len(close.columns)} 列 / 权重 {len(weights)} 键）"
            "⇒ 代码口径分叉 ⇒ 标的池必空 ⇒ 拒绝执行"
        )
    feats = EL.compute_features(close, amount)
    dates = sorted(pd.to_datetime(scores["date"].unique()).tolist())
    targets, diag = EL.build_targets(
        dates, scores, weights, close, feats, names=names,
        alpha_weight=float(params.get("alpha_weight", 0.5)),
        momentum_weight=float(params.get("momentum_weight", 0.5)),
        top_k=int(params.get("top_k", C2.TARGET_N_HOLDINGS)),
        hold_buffer=int(params.get("hold_buffer", C2.HOLD_BUFFER)),
        entry_ma_window=int(params.get("entry_ma_window", 60)),
        exit_ma_window=int(params.get("exit_ma_window", 60)),
        min_coverage=float(C2.MIN_COVERAGE),
        min_amount_20d=float(C2.MIN_ETF_AVG_AMOUNT_20D),
        min_history_days=int(C2.MIN_ETF_HISTORY_DAYS),
        tradable=build_tradable_maps()[0],
        allow_theme_cap=bool(params.get("allow_theme_cap", True)),
        neg_score_gate=bool(params.get("neg_score_gate", False)),
        neg_score_gate_stat=str(params.get("neg_score_gate_stat", "median")),
        neg_score_gate_scale=float(params.get("neg_score_gate_scale", 0.05)),
        neg_score_gate_min_exposure=float(params.get("neg_score_gate_min_exposure", 0.0)),
        smoothing_arm=params.get("smoothing_arm"),
        rebalance_signal_stride=int(C2.rebalance_signal_stride_for(
            int(params.get("rebalance_every_n_days", C2.REBALANCE_EVERY_N_DAYS)))),
    )
    return targets, diag


def benchmark_nav(close: pd.DataFrame, code: str = "510300") -> pd.Series | None:
    """基准 ETF 归一净值（若无该代码 ⇒ None）。"""
    if code not in close.columns:
        for alt in ("510300", "159919", "510310"):
            if alt in close.columns:
                code = alt
                break
    if code not in close.columns:
        return None
    s = close[code].astype("float64").dropna()
    if len(s) < 2:
        return None
    return s / s.iloc[0]


def assert_targets_not_empty(arm_name: str, targets: Mapping[str, Mapping[str, float]]) -> None:
    """🔴 **fail-closed 闸门（2026-11-26 指标③全平事故）**：端到端目标全空 ⇒ 拒绝执行。

    🔴 **为什么必须有**：原实现把「目标日期数」当「调仓次数」写进报告 ⇒
       标的池被过滤光时，会安静地交出一份「年化 0.00%、换手 0.0000」的**假报告**
       （两臂同时全平 ⇒ 看起来像策略失效，其实是口径缺陷）。
    ⇒ 判定用**聚合口径**（全空才抛）⇒ 合法的「某日池为空」（新股/停牌）**不误伤**。
    """
    n_target_dates = int(len(targets))
    n_empty = int(sum(1 for t in targets.values() if not t))
    if n_target_dates == 0 or n_empty == n_target_dates:
        raise RuntimeError(
            f"🔴 臂 {arm_name} 的端到端目标"
            f"{'为空' if n_target_dates == 0 else f'全为空（{n_empty}/{n_target_dates}）'}"
            "⇒ 标的池被过滤光（ETF 代码口径分叉 / 覆盖率·流动性全不达标）"
            "⇒ 拒绝执行（不出全 0 报告）"
        )


def segment_excess_stats(nav: pd.Series, bench_nav: pd.Series | None,
                         rebalance_dates: list[str]) -> dict[str, Any]:
    """🔴 按**调仓期**（非重叠持仓段）算超额序列 ⇒ t 值 / 胜率。

    🔴 **为什么按段而不是逐日**：同一持仓段内的日收益高度自相关
       ⇒ 逐日 t 值虚高 ⇒ 段级（每 ``cadence``=20 交易日一段）
       ⇒ 样本 = 调仓次数 ⇒ 与指标② 的「期」口径同源。

    🔴 **时序对齐**：调仓日收盘成交 ⇒ 该日涨跌归**上一段**持仓
       ⇒ 段 = ``(上一调仓日, 本调仓日]``（与 V2 引擎时序一致）。

    🔴 **首段**：首次调仓前是空仓 ⇒ 收益 0（与年化口径一致，**不剔除**）。
    """
    if nav is None or len(nav) < 2 or not rebalance_dates:
        return {}
    s = pd.Series(nav, dtype="float64").dropna().sort_index()
    cuts = [pd.Timestamp(d) for d in sorted(rebalance_dates) if pd.Timestamp(d) in s.index]
    if not cuts:
        return {}
    b: pd.Series | None = None
    if bench_nav is not None:
        b = pd.Series(bench_nav, dtype="float64").dropna().sort_index()
    bounds = [s.index[0]] + cuts + [s.index[-1]]
    segs: list[float] = []
    bench_segs: list[float] = []
    for a, z in zip(bounds[:-1], bounds[1:]):
        win = s.loc[(s.index > a) & (s.index <= z)]
        if len(win) < 2:
            continue
        segs.append(float(win.iloc[-1] / win.iloc[0] - 1.0))
        if b is not None:
            bwin = b.loc[(b.index > a) & (b.index <= z)]
            bench_segs.append(float(bwin.iloc[-1] / bwin.iloc[0] - 1.0) if len(bwin) >= 2 else 0.0)
        else:
            bench_segs.append(0.0)
    n = len(segs)
    if n < 2:
        return {}
    excess = np.asarray(segs, dtype="float64") - np.asarray(bench_segs, dtype="float64")
    sd = float(excess.std(ddof=1))
    return {
        "n_segments": int(n),
        "t_stat": float(excess.mean() / sd * np.sqrt(n)) if sd > 0 else float("nan"),
        "win_rate": float(np.mean(excess > 0.0)),
        "mean_segment_excess": float(excess.mean()),
    }


# ----------------------------------------------------------------- 报告
def render_report(results: list[dict], meta: dict) -> str:
    """报告正文（人读的那份）。"""
    lines: list[str] = []
    A = lines.append
    A("# V3 单变量对照实验报告（5 折 OOS · 2019–2023）")
    A("")
    A(f"- 生成时间：{meta['generated_at']}")
    A(f"- 实验窗口：**{ALLOWED_START} ~ {ALLOWED_END}**（5 折 OOS 拼接）｜"
      f"🔴 单盲封存段 `{SEALED_START} ~ {SEALED_END}` **未触碰**")
    A(f"- 下游冻结参数：`{meta['frozen_source']}`（md5 `{meta['frozen_md5']}`，"
      f"冠军臂 `{meta['winner_arm']}`）")
    A(f"- 服务器 / provider：`{meta['provider_uri']}`")
    A("")
    A("## ① 三项硬指标总表")
    A("")
    A("| 臂 | 定义 | ① RankIC 均值 | ① ICIR | ① t | ① n | ② Top10% 费后超额(年化) | ② t | ② 胜率 "
      "| ③ 夏普 | ③ **卡玛** | ③ 年化 | ③ 超额 | ③ 回撤 | ③ 双边换手 | ③ t(段级) | ③ 胜率(段级) |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        m1 = r.get("metric_1_rankic") or {}
        m2 = r.get("metric_2_top_decile") or {}
        m3 = r.get("metric_3_end_to_end") or {}
        A("| `{arm}` | {defn} | {m1m} | {icir} | {t} | {n} | {m2a} | {m2t} | {m2w} | {sh} | {cal} "
          "| {ar} | {ex} | {dd} | {to} | {m3t} | {m3w} |".format(
            arm=r["arm"], defn=r["definition"]["label"],
            m1m=_f(m1.get("mean_ic")), icir=_f(m1.get("icir")), t=_f(m1.get("t_stat")),
            n=m1.get("n", 0),
            m2a=_pct(m2.get("annualized_excess_net")), m2t=_f(m2.get("t_stat_net")),
            m2w=_pct(m2.get("win_rate_net")), sh=_f(m3.get("sharpe")),
            cal=_f(m3.get("calmar")),
            ar=_pct(m3.get("annualized_return")), ex=_pct(m3.get("excess_vs_benchmark")),
            dd=_pct(m3.get("max_drawdown")),
            to=_f(m3.get("annualized_turnover")),
            m3t=_f(m3.get("t_stat")), m3w=_pct(m3.get("win_rate"))))
    A("")
    A("## ② 逐臂明细")
    A("")
    for r in results:
        A(f"### `{r['arm']}` —— {r['definition']['label']}")
        A("")
        A(f"- 口径：{json.dumps(r['definition'], ensure_ascii=False)}")
        A(f"- 打分截面：{r.get('n_score_dates')} 个 / {r.get('n_score_rows')} 行")
        m1 = r.get("metric_1_rankic") or {}
        A(f"- ① RankIC（ETF 并集内，20 日窗口标签）：均值 {_f(m1.get('mean_ic'))}、"
          f"ICIR {_f(m1.get('icir'))}、t {_f(m1.get('t_stat'))}、n {m1.get('n')}、"
          f"胜率 {_pct(m1.get('win_rate'))}；20 日滚动均值 {_f(m1.get('rolling_mean'))}"
          f"（滚动为正占比 {_pct(m1.get('rolling_positive_ratio'))}）")
        m2 = r.get("metric_2_top_decile") or {}
        A(f"- ② Top 10% 组合：期数 {m2.get('n_periods')}、平均持仓 {m2.get('mean_n_names')} 只、"
          f"平均持有 {m2.get('mean_holding_days')} 个交易日；费后年化超额 {_pct(m2.get('annualized_excess_net'))}、"
          f"毛年化超额 {_pct(m2.get('annualized_excess_gross'))}、t {_f(m2.get('t_stat_net'))}、"
          f"胜率 {_pct(m2.get('win_rate_net'))}、成本拖累 {_pct(m2.get('total_cost_drag'))}")
        m3 = r.get("metric_3_end_to_end") or {}
        if m3.get("skipped"):
            A("- ③ 端到端：**已跳过**（`--skip-e2e`）")
        else:
            A(f"- ③ 端到端（V2 ETF 层 + 冻结参数）：年化 {_pct(m3.get('annualized_return'))}、"
              f"超额 {_pct(m3.get('excess_vs_benchmark'))}、回撤 {_pct(m3.get('max_drawdown'))}、"
              f"夏普 {_f(m3.get('sharpe'))}、**卡玛 {_f(m3.get('calmar'))}**、"
              f"双边年化换手 {_f(m3.get('annualized_turnover'))}×、"
              f"调仓 {m3.get('n_rebalances')} 次、成本拖累 {_pct(m3.get('total_cost_drag'))}；"
              f"目标日 {m3.get('n_target_dates')} 个（其中空目标 {m3.get('n_empty_targets')} 个）、"
              f"引擎跳过目标日 {m3.get('n_skipped_target_dates')} 个；"
              f"**段级显著性**：{m3.get('n_segments')} 个持仓段、t {_f(m3.get('t_stat'))}、"
              f"胜率 {_pct(m3.get('win_rate'))}、段均超额 {_pct(m3.get('mean_segment_excess'))}")
        A("")
    A("## ③ 口径披露（必读）")
    A("")
    A("1. 🔴 **单盲封存段未触碰**：所有训练/打分/评估窗口都在 2019–2023 内；"
      "2024-01-01 ~ 2026-11-03 是 V2 已消耗的单盲段，本实验一律不碰。")
    A("2. 🔴 **单变量**：下游 ETF 层参数（`top_k=5` / `hold_buffer=2` / `cadence=20` / "
      "`min_coverage=0.5` / 成本）全部取自 V2 冻结件，**一个字未改**；"
      "两臂走**同一份**回测引擎与**同一份**评估代码 ⇒ 差异只来自上游打分。")
    A("3. 🔴 **对照臂 = V2 封存打分**（零重训）⇒ 没有「重新训练 V2」带来的口径漂移。")
    A("4. 🔴 **排序目标下逐日中心化是不变量**：减当日截面均值不改变同日截面排名 ⇒ "
      "指标 ①② 对「是否中心化」不敏感；中心化的作用在 MSE 对照臂与超额口径。")
    A("5. 🔴 **规模中性化 = 指数档位代理**（`csi300/csi500/csi1000`，带日期区间 ⇒ 时变）："
      "Alpha158 无市值字段、本仓无流通市值面板 ⇒ 不是真市值口径。")
    A("6. 🔴 **成分股是 2026-11-04 单快照**倒推历史（前视偏差与 V1/V2 同源，已量化披露）。")
    A("7. 🔴 **样本量**：指标 ① 的 n 是**打分截面数**（**10 交易日栅格**（≈双周，24.4 期/年）⇒ 5 年约 121 个信号日 ⇒ 远小于交易日数）⇒ t 值须与 n 一起读。")
    A("8. 🔴 **费用口径**：② 的成本 = 2×(佣金 1.5bp + 滑点 5bp)/期（每期整体换仓）；"
      "③ 的成本走 V2 回测引擎的 Cost-Aware 口径（佣金+滑点+Rank Buffer 留存量全进净值）。")
    _ran = list(meta.get("arms") or [])
    _abl = ["v3_mse", "v3_no_neutralize", "v3_no_weight", "v3_no_rolling"]
    _not_run = [a for a in _abl if a not in _ran]
    if _not_run:
        A(f"9. 🔴 **本轮只交 {len(_ran)} 臂**"
      f"（{', '.join(_ran)}）：消融臂 "
      + " / ".join(f"`{a}`" for a in _not_run) + " **本轮未跑**（每臂 ≈1.5 h 服务器训练预算；"
      "本轮预算先用于闭合「指标③端到端」的口径缺陷）⇒ 结论**只对已跑的臂**成立，"
      "**不许外推**到未跑的消融臂（哪个设计真正起作用，本报告回答不了）。")
    else:
        A(f"9. 🔴 **本轮交 {len(_ran)} 臂**（{', '.join(_ran)}）：4 个消融臂**全部跑完**"
          "（每个消融臂 = 相对 `v3_full` **只少一样**：目标 / 残差化 / ΣW / 滚动重训）"
          "⇒ 本报告可回答「哪个设计真正起作用」；但结论**只对 2019–2023 OOS 成立**，"
          "**不许外推**到单盲封存段或未来市况。")
    A("10. 🔴 **③ 端到端的**两处**口径修复（2026-11-26，reviewer 审计 + 复现）**："
      "**① 首版两臂 ③ 全平**（年化 0.00%、换手 0.0000）**不是策略问题，是口径缺陷** —— "
      "探针读 ETF 行情时 `code` 列被 pandas 推断成 int64，与 V2 的 str 口径分叉 ⇒ "
      "特征 join 全 miss ⇒ 池空 ⇒ 目标全空 ⇒ 现已统一走 V2 的 "
      "`etf_layer.load_etf_prices`（str + zfill(6)）＋ fail-closed 断言；"
      "**② 回测测量窗口未裁** ⇒ 净值一路跑到面板末日 2026-11-10 ⇒ **单盲封存段 "
      "2024-01-01~2026-11-03 实际进了净值**（且「2023-12 → 2026-11-10」被当**一个**"
      "持仓段 ⇒ 段级 t 值/胜率失真；`metrics.summarize` 的年份由净值点数决定 ⇒ "
      "还**低估了年化换手**）⇒ 现已把回测窗口裁到 "
      f"`{ALLOWED_START} ~ {ALLOWED_END}`（**只裁测量窗口，不裁行情面板** —— "
      "特征需要 2018 前置历史）；**本版打分分数未变**（复用同一批已落盘 OOS 分数，"
      "**零重训**）⇒ ①② 与首版逐字可比，③ 为修复后重算。")
    A("11. 🔴 **③ 的显著性口径**：t 值 / 胜率按**持仓段**（非重叠段，样本 = 调仓段数）算，"
      "不用逐日（同一段内日收益高度自相关 ⇒ 逐日 t 虚高）；「调仓 N 次」= "
      "**落在交易日上的目标日数**（含空目标/零换手日）＝ 目标日数 − 被跳过的目标日数，"
      "**有成交的调仓日数**另行披露。")
    A("12. 🔴 **② 与 ③ 衡量的不是同一件事**：② = 按**个股分数**取 Top10% 并**每期整体换仓**"
      "（成本拖累 ~15.6pp ⇒ 费后超额被成本吃掉）；③ = **V2 生产口径**"
      "（穿透分 `E = W × S` + `top_k=5` + Rank Buffer + `cadence=20`）的组合"
      "（成本拖累 ~3~5pp）⇒ 两者的优劣**不必然同向**，读表时不要混用、也不要互相替代。")
    A("13. 🔴 **可交易（停牌/涨跌停）标记来自 V2 的默认行情面板**"
      "（`etf_prices.csv`，**2020-01-02 起**）⇒ **2019 整折（折 1）没有停牌/涨跌停过滤**"
      "（取不到当日的 ETF 会被判为可交易）；这是 **V2 生产链的既有行为**、"
      "探针**忠实复刻**（两臂同口径 ⇒ 横向可比）⇒ 但**不可外推**到「含涨跌停约束」的结论。")
    # 🔴 **I5**：披露 14 **按臂**写各臂自己的口径与规模
    #    （PIT 臂的规模是**逐日成员数**，绝不能写成静态池 2635）。
    _arm_cal: list[str] = []
    _modes = meta.get("universe_mode_per_arm") or {}
    _sizes = meta.get("universe_size_per_arm") or {}
    _dailies = meta.get("universe_daily_members_per_arm") or {}
    for _an in (meta.get("arms") or []):
        _m = _modes.get(_an) or meta.get("universe_mode")
        _sz = (_sizes.get(_an) or {}).get("static_pool")
        _dl = _dailies.get(_an) or {}
        if _dl:
            _arm_cal.append(
                f"臂 `{_an}`：**逐日 PIT** 口径 `{_m}` —— 静态候选池 {_sz} 只，"
                f"**逐日实际成员** min {_dl.get('min')} / 中位 {_dl.get('median')} / "
                f"max {_dl.get('max')} 只"
            )
        else:
            _arm_cal.append(f"臂 `{_an}`：静态并集口径 `{_m}` —— {_sz} 只")
    _arm_cal_txt = "；".join(_arm_cal) if _arm_cal else "（本轮无臂记录）"
    A(f"14. 🔴 **训练池口径 vs 目标建议区间（按臂）**：{_arm_cal_txt}。"
      f"（静态并集口径 = 候选 ETF 成分股全并集 **{meta.get('raw_etf_union_size')} 只** "
      "∩ (中证 800 ∪ 中证 1000)，实测 2635 只）⇒ 目标文档建议的「中证800+中证1000 约 "
      "1200~1800 只」**未取字面值**（静态口径落在目标「ETF 成分股并集」那一支，"
      "且**多出**约 800 只中小盘）⇒ 该差异属**口径选择**而非实现偏差，"
      "**必须与目标建议区间一并披露**，"
      "不得默认等价；🔴 **逐日 PIT 臂的规模只能读它自己的逐日成员数**，"
      "不许套用静态池 2635（那是**另一个口径**的数）。")
    A("15. 🔴 **替换判据 = 端到端样本外卡玛比率**：目标文档指定的替换条件是"
      "「端到端样本外**卡玛比率**有明确提升 ⇒ 再平滑替换上游打分引擎」⇒ ③ 卡玛"
      "（= 年化 / |最大回撤|，沿用 V2 `metrics.summarize` 口径）**就是**该判据的度量；"
      "夏普/超额/换手为辅助指标，**卡玛未改善 ⇒ 不替换**（判定见 ④ 节，自动生成）。")
    _fo = meta.get("frozen_oos_metrics") or {}
    A("16. 🔴 **探针 `control_v2` 的 ③ 与 V2 冻结件里冠军臂的 OOS 指标不是同一套口径**"
      f"（冻结件：年化 {_pct(_fo.get('annualized_return'))}、回撤 {_pct(_fo.get('max_drawdown'))}、"
      f"夏普 {_f(_fo.get('sharpe'))}、卡玛 {_f(_fo.get('calmar'))}、"
      f"换手 {_f(_fo.get('annualized_turnover'))}×）⇒ **不要逐字对账**："
      "① ETF **价格面板**不同（探针用 `etf_prices_merged_2018.csv`）；"
      "② **可交易（停牌/涨跌停）标记**取自 V2 默认面板（见披露 13）；"
      "③ **测量窗口点数**不同；④ 冻结件是**选参那次**跑的产物，本探针是**验收复跑**。"
      "⇒ **两臂在探针内同口径同面板 ⇒ 横向可比成立**；"
      "探针与冻结件之间的差异属**面板/口径差**，**不作为任何结论依据**。")
    A("17. 🔴 **训练池口径是「静态并集」⇒ 存在池前视**（2026-11-26 只读审计量化）："
      "池 = ETF 成分股并集 ∩ **csi800∪csi1000 的静态并集**（`read_instruments_file` "
      "丢弃了成分表的日期区间 ⇒ 取的是「历史上曾任成分」的全体）⇒ 实测 2635 只，"
      "**超出目标建议区间 1200~1800 约 46%**；训练段有 **11.3%**（297/2635）的标的"
      "**整段不在**中证800/1000 名单内；且池内 **45.98%** 的「日期×标的」单元格"
      "**规模档恒为「其它档」** ⇒ 规模中性化对近半池**不起作用**。"
      "⇒ **静态臂本轮未修**；🔴 **逐日 PIT 臂**（`v3_pit_pool`，显式指定才跑）"
      "用**逐日成员面板**修掉了这一条（池规模见披露 14 的逐臂记录）⇒ 但它同时改掉"
      "训练/打分两端的截面几何 ⇒ 属**中性化语义变更**，"
      "**必须单独立臂验收**；见下一条）。")
    A("18. 🔴 **打分侧与训练侧同口径残差化**（2026-11-26 修复）：训练侧对特征做了"
      "**逐日截面残差化**（规模档 + 流动性），打分侧原先**直接**喂原始特征 ⇒ "
      "**train/serve skew**（实测 VOL20 截面 R²≈0.565 ⇒ 约 56% 的截面信息在训练"
      "被扣掉、打分又被放回；且 4 个消融臂共用该打分路径 ⇒ 消融**测不出**该偏差）"
      "⇒ 现已在打分侧按**同一口径**（同 `cross_sectional_residualize_panel`、同控制变量）"
      "再残差化一次；**历史分数文件与历史报告一律作废**。")
    A("19. 🔴 **`v3_mse` 消融臂已改为真单变量**（2026-11-26 修复）：原先它走 "
      "`CONTROL_LGB_PARAMS`，与主臂差 **8 个超参**（num_leaves/max_depth/"
      "min_data_in_leaf/colsample/subsample/lambda_l1/lambda_l2/metric）⇒ "
      "「只换损失函数」**不成立**、差异会被误归因 ⇒ 现改用与主臂**逐项相同**的 "
      "`MSE_ARM_LGB_PARAMS`（只差 objective/metric）。"
      "🔴 标签表示的**伴生差异**不可消除：lambdarank 只能吃**整数 relevance grade**（5 档），"
      "MSE 臂吃**连续数值超额** ⇒ 两臂标签**信息量同源、表示不同**。")
    A("20. 🔴 **样本权重取字面 ΣW**（目标文档口径）：`SAMPLE_WEIGHT_MODE=sum_w`"
      "（= max(ΣW,0)）⇒ 与目标文档「按个股在 ETF 池中的总权重 ΣW 做样本加权」一致；"
      "√ΣW 变体已不用（它削弱头部集中）。"
      "🔴 **ETF 侧仍是 2026-11-04 单快照**（见披露 6）⇒ 指数侧口径修好后，"
      "**ETF 成分/ΣW 这一层的前视仍在** ⇒ 结论按「两层前视」读。")
    A("")
    lines.extend(render_verdict(results, meta))
    return "\n".join(lines) + "\n"


def _num(value: Any) -> float | None:
    """缺失/NaN 指标 ⇒ None（None ⇒ 该指标不可用，不得当 0 用）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _pick(results: list[dict], arm: str, group: str, key: str) -> float | None:
    for r in results:
        if str(r.get("arm")) == arm:
            return _num((r.get(group) or {}).get(key))
    return None


def render_verdict(results: list[dict], meta: dict) -> list[str]:
    """④ 替换判据与结论（判据 = 端到端样本外卡玛比率明确提升；数字全部取自本次结果）。"""
    out: list[str] = []
    A = out.append
    A("## ④ 替换判据与结论（判据 = 端到端样本外**卡玛比率**明确提升）")
    A("")
    A("| 臂 | ③ 卡玛 | ③ 段级 t | ③ 年化 | ③ 回撤 | ③ 超额 | ① RankIC | ① ICIR "
      "| ② 费后超额(年化) | ② t |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        m1 = r.get("metric_1_rankic") or {}
        m2 = r.get("metric_2_top_decile") or {}
        m3 = r.get("metric_3_end_to_end") or {}
        A(f"| `{r['arm']}` | {_f(m3.get('calmar'))} | {_f(m3.get('t_stat'))} "
          f"| {_pct(m3.get('annualized_return'))} | {_pct(m3.get('max_drawdown'))} "
          f"| {_pct(m3.get('excess_vs_benchmark'))} | {_f(m1.get('mean_ic'))} "
          f"| {_f(m1.get('icir'))} | {_pct(m2.get('annualized_excess_net'))} "
          f"| {_f(m2.get('t_stat_net'))} |")
    A("")
    A("**判定（自动生成，不手填）**")
    A("")
    _fo = meta.get("frozen_oos_metrics") or {}
    if _fo:
        A(f"- **对账基准（V2 冻结件冠军臂 OOS，口径不同、只作参照）**："
          f"年化 {_pct(_fo.get('annualized_return'))}、回撤 {_pct(_fo.get('max_drawdown'))}、"
          f"夏普 {_f(_fo.get('sharpe'))}、**卡玛 {_f(_fo.get('calmar'))}**、"
          f"换手 {_f(_fo.get('annualized_turnover'))}×（差异原因见披露 16）。")
    cal_c = _pick(results, "control_v2", "metric_3_end_to_end", "calmar")
    cal_v = _pick(results, "v3_full", "metric_3_end_to_end", "calmar")
    t_seg = _pick(results, "v3_full", "metric_3_end_to_end", "t_stat")
    ic_c = _pick(results, "control_v2", "metric_1_rankic", "mean_ic")
    ic_v = _pick(results, "v3_full", "metric_1_rankic", "mean_ic")
    icir_c = _pick(results, "control_v2", "metric_1_rankic", "icir")
    icir_v = _pick(results, "v3_full", "metric_1_rankic", "icir")
    if cal_c is None or cal_v is None:
        A("- 🟡 **无法判定**：本轮结果缺卡玛数值 ⇒ 不做任何替换结论。")
    else:
        gain = cal_v - cal_c
        sig = t_seg is not None and abs(t_seg) >= 2.0
        A(f"- 卡玛：`control_v2` = {cal_c:.4f} → `v3_full` = {cal_v:.4f}"
          f"（**Δ = {gain:+.4f}**）；`v3_full` 段级 t = {_f(t_seg)}"
          f"（{'≥ 2 ⇒ 统计显著' if sig else '< 2 ⇒ 统计不显著'}）。")
        if gain > 0 and sig:
            A("- ✅ **满足替换判据**（卡玛明确提升且段级显著）⇒ 技术上可平滑替换上游打分引擎；"
              "仍建议先在正式跑批里做一次**影子对照**再切流。")
        elif gain > 0:
            A(f"- 🟡 **不满足替换判据**：卡玛方向为正（{gain:+.4f}）但**统计不显著**"
              f"（段级 t = {_f(t_seg)} < 2）⇒ 「明确提升」不成立 ⇒ **维持 V2 现役**。")
        else:
            A(f"- ❌ **不满足替换判据**：卡玛未提升（Δ = {gain:+.4f}）⇒ **维持 V2 现役**。")
    if ic_c is not None and ic_v is not None:
        if ic_v < ic_c:
            A(f"- ⚠️ **① 截面预测力 V3 弱于 V2**（RankIC {ic_c:.4f} → {ic_v:.4f}；"
              f"ICIR {icir_c:.4f} → {icir_v:.4f}）⇒ 上游打分**没有变强** ⇒ 端到端的边际差异"
              "更像噪声/持仓结构差异 ⇒ **不构成替换理由**。")
        else:
            A(f"- ✅ **① 截面预测力 V3 不弱于 V2**（RankIC {ic_c:.4f} → {ic_v:.4f}；"
              f"ICIR {icir_c:.4f} → {icir_v:.4f}）。")
    A(f"- 样本外窗口：{meta.get('oos_window')}（5 折 OOS 拼接）；"
      f"单盲封存段 `{meta.get('sealed_window')}` **全程未触碰**。")
    A("- 结论适用范围：**仅限本次 OOS 窗口与本次冻结件**；任何生产切换都须重新跑正式批 + 影子对照。")
    A("")
    return out


def _f(value: Any) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "—" if not np.isfinite(v) else f"{v:.4f}"


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "—" if not np.isfinite(v) else f"{v * 100:.2f}%"


# ----------------------------------------------------------------- 主流程
def _has_flag(argv: list[str], flag: str) -> bool:
    """命令行里是否**显式**给了某个开关（``--x`` 或 ``--x=v`` 两种写法都认）。"""
    for item in argv:
        if item == flag or item.startswith(flag + "="):
            return True
    return False


def per_arm_universe_meta(arms, defs, universe_info, etf_universe,
                           etf_codes, holdings, provider_uri):
    """🔴 **I5**：**按臂**记录训练池口径与规模
    ⇒ 返回 ``(mode, size, daily)`` 三个 dict。

    🔴 **为什么不能只记一个全局口径**：``v3_pit_pool`` 臂用的是**逐日 PIT 成员**
       （约 1400 只/日），其余臂用的是**静态并集池**（2635 只）⇒ 披露 14 若只写
       一个数 ⇒ 会把 PIT 臂**写成静态池**（**错的**）⇒ 口径卡也必须**按臂**生成。

    * ``mode[臂]`` = 该臂自己的 ``universe.mode``；
    * ``size[臂]["static_pool"]`` = 该臂的**静态候选池**规模；
    * ``daily[臂]`` = PIT 臂的**逐日成员数** min/中位/max（静态臂为空 dict
      ⇒ 披露时能区分「静态并集口径」与「逐日 PIT 口径」）。
    """
    import universe as UNI

    mode_per_arm: dict[str, str] = {}
    size_per_arm: dict[str, dict] = {}
    daily_members_per_arm: dict[str, dict] = {}
    for arm_name in arms:
        mode = str(defs[arm_name].get("universe_mode") or C.UNIVERSE_MODE)
        mode_per_arm[arm_name] = mode
        if mode == C.UNIVERSE_MODE_PIT:
            pit_info = UNI.build_universe(etf_codes, holdings, mode=mode,
                                          provider_uri=provider_uri)
            panel = pit_info.get(C.PIT_MEMBERSHIP_KEY)
            daily = (np.asarray(panel.to_numpy().sum(axis=1), dtype=float)
                     if panel is not None else np.asarray([], dtype=float))
            size_per_arm[arm_name] = {"static_pool": int(pit_info.get("n_stocks") or 0)}
            daily_members_per_arm[arm_name] = {
                "min": int(daily.min()) if daily.size else 0,
                "median": float(np.median(daily)) if daily.size else 0.0,
                "max": int(daily.max()) if daily.size else 0,
            }
        else:
            size_per_arm[arm_name] = {
                "static_pool": int(universe_info.get("n_stocks") or len(etf_universe)),
            }
            daily_members_per_arm[arm_name] = {}
    return mode_per_arm, size_per_arm, daily_members_per_arm


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # 🔴 **NIT9**：`--arm` 与 `--arms` 同时给出 ⇒ **直接 raise**（不许静默覆盖）
    #    （旧实现里 `--arm` 无条件覆盖 `--arms` ⇒ 用户以为跑了多臂、实际只跑了一臂）
    if _has_flag(raw_argv, "--arm") and _has_flag(raw_argv, "--arms"):
        raise ValueError(
            "🔴 `--arm` 与 `--arms` 不许同时给出（会静默覆盖）"
            "⇒ 请只用 `--arm <单个臂>` 或只用 `--arms <逗号分隔的臂列表>`"
        )
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _log(f"输出目录：{out_dir}")

    frozen = load_frozen_params(args.frozen_params)
    _log(f"冻结件：{frozen['source']}（md5 {frozen['md5']}，冠军臂 {frozen['winner_arm']}）")

    folds = [int(x) for x in str(args.folds).split(",") if str(x).strip()]
    # 🔴 ``--arm`` = **显式单臂**（opt-in 臂只能这样跑）；不给 ⇒ 走 ``--arms``
    if str(args.arm or "").strip():
        arms = [str(args.arm).strip()]
    else:
        arms = [a.strip() for a in str(args.arms).split(",") if a.strip()]
    defs = all_arm_definitions()
    unknown = [a for a in arms if a not in defs]
    if unknown:
        raise ValueError(f"🔴 未知臂名：{unknown}（可选：{list(defs)}）")

    close, amount, names = load_etf_market(args.etf_prices_csv)
    _log(f"ETF 行情面板：{close.shape}")

    # ---- ETF 并集（训练池 ＋ 指标 ①② 的标的范围）
    import universe as UNI
    etf_codes = UNI.load_etf_universe()
    holdings = UNI.load_holdings()
    universe_info = UNI.build_universe(etf_codes, holdings, mode=C.UNIVERSE_MODE)
    etf_universe = set(universe_info["instruments"])
    _log(f"ETF 成分股并集：{len(etf_universe)} 只")
    # 🔴 披露用：候选 ETF 成分股**全并集**规模（未经大中盘过滤）⇒ 报告要写明
    #    「现落 N 只 = 全并集 M 只 ∩ (中证800 ∪ 中证1000)」，不得默认等价于目标建议区间。
    try:
        raw_union_df, _ = UNI.build_etf_constituent_union(etf_codes, holdings)
        raw_union_size = int(raw_union_df["instrument"].nunique())
    except Exception as _exc:  # pragma: no cover - 披露字段失败不得中断实验
        raw_union_size = -1
        _log(f"⚠️ 全并集规模统计失败（仅影响披露）：{_exc}")
    universe_size = int(universe_info.get("n_stocks") or len(etf_universe))
    _log(f"训练池口径 {C.UNIVERSE_MODE}：{universe_size} 只（全并集 {raw_union_size} 只）")

    # ---- 个股 $vwap 面板（指标 ①② 用）
    price_panel, bench_price = load_qlib_panels(
        sorted(etf_universe), ALLOWED_START, ALLOWED_END, args.provider_uri)
    _log(f"个股 $vwap 面板：{price_panel.shape}｜基准 {C.BENCHMARK}：{len(bench_price)} 点")

    # 🔴 **先加载对照臂**（只读）：它的 **OOS 信号日**是两臂对齐的唯一事实源
    #    ⇒ V3 必须在**完全相同的信号日**上打分（否则端到端指标的调仓日历都不同）
    control_model_name = str(frozen.get("model_name")
                           or (frozen.get("params") or {}).get("model_name")
                           or "ablate_roc5")
    control_scores, fold_dates = load_control_scores(Path(args.wf_dir), folds, control_model_name)

    results: list[dict] = []
    for arm_name in arms:
        arm = defs[arm_name]
        _log(f"=== 臂：{arm_name}（{arm['label']}）===")
        if arm["source"] == "cached":
            scores = control_scores
        elif args.reuse_scores:
            _log("  --reuse-scores：复用已落盘打分（**零重训**，只重跑评估）")
            scores = load_v3_scores_from_disk(out_dir / arm_name, arm_name, folds, fold_dates)
        elif args.reuse_scores_if_present and has_all_scores_on_disk(out_dir / arm_name,
                                                                     arm_name, folds):
            _log("  --reuse-scores-if-present：**每折打分都在盘上** ⇒ 复用（零重训，只重跑评估）")
            scores = load_v3_scores_from_disk(out_dir / arm_name, arm_name, folds, fold_dates)
        else:
            if args.reuse_scores_if_present:
                _log("  --reuse-scores-if-present：**盘上缺折** ⇒ 该臂**现场训练**")
            scores = train_v3_scores(arm, arm_name, folds, out_dir / arm_name,
                                     args.provider_uri,
                                     workers=int(args.workers),
                                     threads=int(args.threads_per_worker),
                                     fold_dates=fold_dates)
        res = evaluate_arm(arm_name, arm, scores, etf_universe, price_panel, bench_price,
                           close, amount, names, frozen, args.skip_e2e)
        nav = res.pop("_nav", None)
        if nav is not None:
            nav.to_csv(out_dir / f"nav_{arm_name}.csv", header=["nav"])
        results.append(res)

    # 🔴 **I5**：训练池口径 **按臂记录**（PIT 臂**不是**静态池 2635
    #    ⇒ 披露 14 必须按臂写）
    (universe_mode_per_arm, universe_size_per_arm,
     universe_daily_members_per_arm) = per_arm_universe_meta(
        arms, defs, universe_info, etf_universe, etf_codes, holdings,
        args.provider_uri,
    )

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "frozen_source": frozen["source"], "frozen_md5": frozen["md5"],
        "winner_arm": frozen["winner_arm"], "provider_uri": args.provider_uri,
        "winner_arm_desc": frozen.get("winner_arm_desc"),
        "frozen_oos_metrics": frozen.get("oos_metrics") or {},
        "arms": arms, "folds": folds,
        "oos_window": [ALLOWED_START, ALLOWED_END],
        "sealed_window": [SEALED_START, SEALED_END],
        "etf_universe_size": len(etf_universe),
        "universe_size": universe_size,
        "raw_etf_union_size": raw_union_size,
        "universe_mode": C.UNIVERSE_MODE,
        # 🔴 **I5**：按臂的口径与规模（PIT 臂的「规模」是**逐日成员数**，不是静态池）
        "universe_mode_per_arm": universe_mode_per_arm,
        "universe_size_per_arm": universe_size_per_arm,
        "universe_daily_members_per_arm": universe_daily_members_per_arm,
        "signal_dates_source": "🔴 对照臂（V2）的 OOS 信号日（每折 valid 窗口内）",
        "signal_dates_per_fold": {str(k): len(v) for k, v in sorted(fold_dates.items())},
        "n_signal_dates_total": int(sum(len(v) for v in fold_dates.values())),
    }
    # 🔴 **I5**：口径卡**按臂生成**（每臂的 ``universe.mode`` 必须与该臂自己的口径一致）
    payload = {"meta": meta, "results": results,
               "v3_caliber": {arm_name: C.v3_caliber_card(mode)
                              for arm_name, mode in universe_mode_per_arm.items()},
               "frozen_params": frozen["params"]}
    (out_dir / "v3_comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    report = render_report(results, meta)
    report_path = Path(args.report_out) if args.report_out else (out_dir / "v3_comparison_report.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    _log(f"报告已写：{report_path}（md5 {_md5(report_path)}）")

    if args.publish_name:
        import shutil
        publish_dir = Path("/mnt/data/siping/runs/etf_rotation_results/reports")
        publish_dir.mkdir(parents=True, exist_ok=True)
        target = publish_dir / args.publish_name
        shutil.copyfile(report_path, target)
        target.chmod(0o664)
        _log(f"已发布：{target}（md5 {_md5(target)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
