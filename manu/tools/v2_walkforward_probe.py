#!/usr/bin/env python3
"""🔴 V2 **5 折滚动前推（expanding window）验证**探针（2026-11-18）

【问的问题】
  原验证段只有 **2022–2023 两年**（484 个交易日）⇒ 按月频只有 **24 个调仓点**
  ⇒ 拿 24 个样本点去网格搜 240 组超参 = **必然过拟合**；且这两年是单边熊市
  ⇒ 「躺平装死」永远优于「积极轮动」⇒ 搜出来必然是「蜗牛参数」。

【处方（用户 2026-11-18 授权）】
  方法一：**5 折 expanding window（滚动前推）**
      折 1 = 训练 2016~2018 → 验证 2019；折 2 = 训练 2016~2019 → 验证 2020；
      折 3 = 训练 2016~2020 → 验证 2021；折 4 = 训练 2016~2021 → 验证 2022；
      折 5 = 训练 2016~2022 → 验证 2023
      ⇒ 5 折 OOS **拼成 2019–2023**（5 年、~1,200 个交易日、~60 个月频调仓点）
      ⇒ 样本里**同时含牛市 / 熊市 / 震荡市**；**2024–2026 单盲保持封存**。
  方法二：**压缩自由度**（先验规则替代暴力搜参）
      ``cadence=20`` 焊死、``top_k=5`` 锁死、``hold_buffer ∈ {2,3,5}``
      ⇒ 网格规模从 240 降到个位数。

【五条铁律（脚本自证，不靠嘴说）】
  ① **绝不触碰测试段**：每折都调 ``splits.assert_fold_only()``（硬断言
     ``valid_end < 2024-01-01``）；诊断用的前瞻收益窗口也逐折裁剪
     （末 25 个交易日的标签窗口伸进下一年 ⇒ 该截面剔除）。
  ② **不重跑单盲**：不 import ``evaluate``、不写 token / 登记表；
     跑前跑后**比对已封存单盲产物的 md5** ⇒ 被改动直接 raise。
  ③ **不覆盖正式产物**：产物只写 ``v2/_walkforward/``。
  ④ **重构自证（折 4 = 原 val 段的同构复现）**：折 4 的训练段与原 2016~2021
     训练段**逐日相同**（embargo 倒推后同为 2021-11-26）⇒ 折 4 的 S 层分数
     必须与**已封存** ``stock_scores_v2_ablate_roc5.csv`` **逐位一致**；
     且用冻结参数重建的目标权重必须与已封存 ``blind_diagnostics.csv``
     的 val 段选中集合**逐日一致** ⇒ 不一致直接 raise。
  ⑤ **仓库零改动**：所有变体都在**内存里**（参数 dict）⇒ 仓库与已封存产物保持同代。

【用法（服务器）】
    export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NUMEXPR_NUM_THREADS=4
    python manu/tools/v2_walkforward_probe.py --out-subdir _walkforward
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

V2_DIR_CANDIDATES = (
    Path(os.environ["ETF_ROTATION_REPO_ROOT"]) / "manu" / "etf_rotation" / "v2"
    if os.environ.get("ETF_ROTATION_REPO_ROOT")
    else None,
    Path(__file__).resolve().parents[2] / "manu" / "etf_rotation" / "v2",
    Path("/tmp/pycharm_project_3b68a403/manu/etf_rotation/v2"),
)
V2_DIR = next(
    (p for p in V2_DIR_CANDIDATES if p is not None and (p / "config_v2.py").is_file()),
    V2_DIR_CANDIDATES[-1],
)
if str(V2_DIR) not in sys.path:
    sys.path.insert(0, str(V2_DIR))

#: 🔴 已封存的产物（探针**不得**触碰 ⇒ 跑前跑后比 md5）
SEALED = (
    "single_blind_test_2024_2026.json",
    "_single_blind_registry.json",
    "frozen_params.json",
    "arm_search_val.json",
    "val_search.json",
    "blind_diagnostics.csv",
    "stock_scores_v2_ablate_roc5.csv",
)

#: 🔴 **ETF 层构造方案矩阵**（= 用户「方法二」的先验规则：把物理意义明确的参数锁死）
#:
#:   方案名                 alpha  动量   入场MA  出场MA  平滑臂            同族上限
#:   ---------------------  -----  -----  ------  ------  ----------------  --------
#:   pure_E_native           1.0   0.0      0       0   B_native_slow     关
#:   pure_E_themecap         1.0   0.0      0       0   B_native_slow     开
#:   E_mom_native            0.5   0.5      0       0   B_native_slow     开
#:   v2_frozen_ma120_80      0.5   0.5    120      80   A_half_life_15    开（V2 现状基线）
#:
#: 依据（2026-11-18 val 归因证据，见 ``v2/_probe_attribution/``）：
#:   * 纯 E（alpha=1.0）把 ETF 层 RankIC 从 0.0550 提到 **0.1016**、超额从 −10.06% 提到 **+7.06%**；
#:   * 下游 EWMA 臂 A15 让排序**反向**（RankIC → **−0.0383**）⇒ 臂 B（原生慢变量）为首选；
#:   * 同族上限吃掉约 9pp 超额 ⇒ 保留「关」这一档；
#:   * MA120/80 小幅恶化（MA60 更差）⇒ 只保留「V2 现状」这一档做对照。
ARMS: tuple[dict, ...] = (
    {
        "name": "pure_E_native",
        "desc": "纯 E + 臂 B 原生慢变量 + 无 MA + 同族上限关",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 0, "exit_ma_window": 0,
        "smoothing_arm": "B_native_slow", "allow_theme_cap": False,
    },
    {
        "name": "pure_E_themecap",
        "desc": "纯 E + 臂 B 原生慢变量 + 无 MA + **保留**同族上限",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 0, "exit_ma_window": 0,
        "smoothing_arm": "B_native_slow", "allow_theme_cap": True,
    },
    {
        "name": "E_mom_native",
        "desc": "E+动量 0.5/0.5 + 臂 B 原生慢变量 + 无 MA + 同族上限开",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 0, "exit_ma_window": 0,
        "smoothing_arm": "B_native_slow", "allow_theme_cap": True,
    },
    {
        "name": "v2_frozen_ma120_80",
        "desc": "**V2 现状基线**：E+动量 + A15 平滑 + MA120/80 + 同族上限开",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 120, "exit_ma_window": 80,
        "smoothing_arm": "A_half_life_15", "allow_theme_cap": True,
    },
)


def _md5(path: Path) -> str:
    if not path.is_file():
        return "<不存在>"
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_csv(path: Path, rows: list[dict]) -> Path:
    import csv as _csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = _csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in keys})
    return path


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-subdir", default="_walkforward")
    ap.add_argument("--out-dir", default="", help="产物目录**覆盖**（host 只读挂载时落 /tmp）")
    ap.add_argument("--folds", default="1,2,3,4,5", help="要跑的折（冒烟用：1,2）")
    ap.add_argument("--arms", default="", help="只跑这些方案（逗号分隔；留空 = 全跑）")
    ap.add_argument("--buffer-grid", default="", help="hold_buffer 网格（默认 2,3,5）")
    ap.add_argument("--gate-grid", default="off,0.05:0.0",
                    help="负分空仓保护网格：``off`` 或 ``scale:min_exposure``（逗号分隔）")
    ap.add_argument("--ablation", default="", help="S 层消融组（默认 = config 里的胜者）")
    ap.add_argument("--skip-train", action="store_true", help="跳过训练/打分阶段（只跑评估）")
    ap.add_argument("--skip-regression", action="store_true",
                    help="跳过「折 4 重构自证」（**正式跑不许跳**）")
    ap.add_argument("--chunk-months", type=int, default=12)
    ap.add_argument("--etf-prices-csv", default="",
                    help="扩展 ETF 行情面板（长表 CSV）；留空 = 用 config_v2 的默认面板")
    ap.add_argument("--warmup-days", type=int, default=240,
                    help="每折**平滑臂预热**长度（交易日）：打分窗口向前延伸这么多天，"
                         "但 OOS 指标只算 [valid_start, valid_end]")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import config_v2 as C
    import splits as SP

    fold_ids = [int(x) for x in args.folds.split(",") if x.strip()]
    for fid in fold_ids:
        SP.assert_fold_only(fid)          # 🔴 铁律①：折级红线（含「绝不触碰 2024+」）
    buffers = ([int(x) for x in args.buffer_grid.split(",") if x.strip()]
               if args.buffer_grid else list(C.WALK_FORWARD_BUFFER_GRID))
    arms = [a for a in ARMS if (not args.arms) or (a["name"] in args.arms.split(","))]
    if not arms:
        raise SystemExit(f"🔴 没有方案匹配 {args.arms!r}")

    gate_grid: list[dict] = []
    for raw in args.gate_grid.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if raw.lower() == "off":
            gate_grid.append({"enabled": False, "scale": 0.05, "min_exposure": 0.0,
                              "label": "off"})
        elif ":" in raw:
            scale_s, min_s = raw.split(":", 1)
            gate_grid.append({"enabled": True, "scale": float(scale_s),
                              "min_exposure": float(min_s),
                              "label": f"gate(scale={scale_s},min_exp={min_s})"})
        else:
            raise SystemExit(f"🔴 闸门网格格式非法：{raw!r}（应为 off 或 scale:min_exposure）")

    ablation = args.ablation or C.ABLATION_WINNER
    drop = tuple(C.ABLATION_GROUPS.get(ablation, ()))

    v2_root = Path(C.DEFAULT_V2_RUN_ROOT)
    out_dir = Path(args.out_dir) if args.out_dir else (v2_root / args.out_subdir)
    _log(f"正式产物根：{v2_root}")
    _log(f"探针产物根：{out_dir}")
    _log(f"折 = {fold_ids}｜方案 = {[a['name'] for a in arms]}｜buffer = {buffers}")
    _log(f"闸门网格 = {[g['label'] for g in gate_grid]}")
    _log(f"🔴 OOS 窗口 = {list(C.WALK_FORWARD_OOS_WINDOW)}（测试段 {list(C.TEST)} **封存**）")
    if args.dry_run:
        _log("--dry-run ⇒ 未开跑")
        return 0

    before = {name: _md5(v2_root / name) for name in SEALED}

    import numpy as np
    import pandas as pd

    import attribution as AT
    import etf_layer as EL
    import metrics as ME
    import run_v2 as R
    import train_s as TS

    out_dir.mkdir(parents=True, exist_ok=True)
    wf_dir = out_dir                       # 折的模型/分数也落这里（不污染正式根）

    # ----------------------------------------------------------------- 取数
    _log("=== 取数：ETF 行情 / 成分股权重 ===")
    if args.etf_prices_csv:
        import constituents as CON
        _prices = EL.load_etf_prices(args.etf_prices_csv)
        close = _prices.pivot_table(index="date", columns="code", values="close", aggfunc="last")
        amount = _prices.pivot_table(index="date", columns="code", values="amount", aggfunc="last")
        _hold = CON.read_holdings_frame()
        names = {}
        if "etf_name" in _hold.columns:
            for _c, _g in _hold.groupby("etf_code", sort=True):
                names[str(_c)] = str(_g["etf_name"].iloc[0])
        close, amount = close.sort_index(), amount.sort_index()
        _log(f"⚠️ 使用**扩展**行情面板：{args.etf_prices_csv}"
             f"（{close.shape}，{close.index[0].date()} ~ {close.index[-1].date()}）"
             f"；🔴 涨跌停/停牌标记仍来自默认面板（2020+）⇒ 覆盖期外的年份**无**流动性约束")
    else:
        close, amount, names = R.load_market_data()
    feats = EL.compute_features(close, amount)
    bench_full = R._benchmark_nav(close)
    fwd20 = AT.etf_forward_return_panel(close, horizon=20)

    import constituents as CON
    holdings = CON.snapshot_frame(CON.load_holdings())
    weights = EL.build_weight_map(holdings)
    _log(f"成分股 ETF 数 = {len(weights)}｜ETF 行情面板 = {close.shape}")

    # ---- 全局信号栅格（**必须**与 ``train_s.score_universe`` 同锚：日历起点）----
    TS.init_qlib(str(C.DEFAULT_PROVIDER_URI))          # 🔴 qlib 必须先 init 才能取日历
    from qlib.data import D
    cal = D.calendar(start_time=C.START_TIME, end_time=C.TEST[1], freq="day")
    global_grid = list(cal[:: C.SIGNAL_STRIDE_N_DAYS])
    grid_pos = {pd.Timestamp(d): i for i, d in enumerate(global_grid)}
    stride = int(C.rebalance_signal_stride_for(int(C.WALK_FORWARD_CADENCE)))
    _log(f"全局信号栅格 = {len(global_grid)} 个（步长 {C.SIGNAL_STRIDE_N_DAYS} 交易日）"
         f"｜调仓步长 = {stride}（cadence={C.WALK_FORWARD_CADENCE}）")

    def fold_signal_days(spec, k: int, start: str | None = None) -> tuple[list, list]:
        """该折的**信号日**（含至多 1 个锚点，保证与全局 cadence 的奇偶对齐）。

        🔴 **锚点必须按「实际使用的 stride」算**：锚点是「全局栅格上、位于本折信号日
           之前的那一个信号日」，加上它之后 ``list[k::]`` 才与 ``global_grid[::k]``
           在窗口内**逐日一致**（stride 换了 ⇒ 锚点也得换 ⇒ 2026-11-18 冒烟里
           cadence=90（stride 9）配 stride=2 的锚点 ⇒ 复现失败，已修）。
        """
        vs = pd.Timestamp(spec.valid_start)
        ve = pd.Timestamp(spec.valid_end)
        lo = pd.Timestamp(start) if start else vs
        # 🔴 钳制到 **ETF 行情面板**的首日：行情面板早于它 ⇒ ``px.loc[px.index <= day]``
        #    为空 ⇒ ``build_targets`` 抛 IndexError（2026-11-18 全量跑实测踩到）。
        px_first = pd.Timestamp(close.index[0])
        if lo < px_first:
            lo = px_first
        in_range = [pd.Timestamp(d) for d in global_grid if lo <= pd.Timestamp(d) <= ve]
        if not in_range:
            raise RuntimeError(f"🔴 折 {spec.fold_id}：{lo}~{ve} 内无信号日")
        first_idx = grid_pos[in_range[0]]
        anchor = None
        if first_idx % int(k) != 0:
            anchor = pd.Timestamp(global_grid[first_idx - 1])
        signal = ([anchor] if anchor is not None else []) + in_range
        picked = signal[::int(k)]
        expect = [pd.Timestamp(d) for d in global_grid[::int(k)] if vs <= pd.Timestamp(d) <= ve]
        got = [d for d in picked if vs <= d <= ve]
        if [str(x.date()) for x in got] != [str(x.date()) for x in expect]:
            raise RuntimeError(
                f"🔴 折 {spec.fold_id} 调仓栅格与全局 cadence 不一致（stride={k}）：\n"
                f"  期望 {len(expect)} 个：{[str(x.date()) for x in expect][:6]} …\n"
                f"  实得 {len(got)} 个：{[str(x.date()) for x in got][:6]} …"
            )
        # 🔴 2026-11-18：把**行情面板起点之前**的信号日剔掉再交给 ``build_targets``。
        #    起因：上面为了对齐全局 cadence 会带进一个「锚点日」，而锚点可能落在
        #    面板起点之前（例：折 2 验证 2020，面板起点 2020-01-02，锚点落在 2019）
        #    ⇒ ``px.loc[px.index <= day]`` 为空 ⇒ ``build_targets`` 抛 IndexError
        #    （2026-11-18 10:04 全量跑实测崩在这）。
        #    剔掉的只是「面板之前的那几天」：它们**没有任何价格**，本来也构造不出
        #    候选池；且**不参与 OOS 指标**（回测窗口 = 折的验证年），
        #    栅格对齐校验已在上面用**未剔除**的 signal 做过 ⇒ 不影响口径。
        signal = [d for d in signal if d >= px_first]
        if not signal:
            raise RuntimeError(
                f"🔴 折 {spec.fold_id}：剔除面板起点前的信号日后为空"
                f"（面板首日 {px_first.date()}）"
            )
        return signal, expect

    # ----------------------------------------------------------------- 阶段 A：训练 + 打分
    fold_specs: dict[int, object] = {}
    fold_models: dict[int, str] = {}
    fold_scores: dict[int, Path] = {}
    for fid in fold_ids:
        spec = SP.assert_fold_only(fid)
        fold_specs[fid] = spec
        model_name = f"wf{fid}_{ablation}"
        fold_models[fid] = model_name
        model_path = wf_dir / f"model_{model_name}.pkl"
        scores_csv = wf_dir / f"stock_scores_v2_{model_name}.csv"
        fold_scores[fid] = scores_csv
        if args.skip_train:
            _log(f"[跳过训练] 折 {fid}：{model_path.name} / {scores_csv.name}")
            continue
        if model_path.is_file() and scores_csv.is_file():
            _log(f"[续跑] 折 {fid}：模型与分数已存在 ⇒ 跳过训练/打分")
            continue
        _log(f"=== 折 {fid}：训练（{spec.train_start} ~ {spec.train_end}，"
             f"名义末日 {spec.train_end_raw}）===")
        TS.train_s_layer(
            universe=C.TRAIN_UNIVERSE, drop=drop, out_dir=wf_dir,
            model_name=model_name, spec=spec,
        )
        warmup_start = SP.trading_days_before(
            SP._d(spec.valid_start), int(args.warmup_days)).isoformat()
        _log(f"=== 折 {fid}：打分（{warmup_start} ~ {spec.valid_end}；"
             f"前 {args.warmup_days} 个交易日 = **平滑臂预热**，不计入 OOS 指标）===")
        TS.score_universe(
            model_path=model_path,
            frozen_path=wf_dir / f"frozen_norm_{model_name}.pkl",
            drop=drop, universe=C.SCORE_UNIVERSE, out_dir=wf_dir,
            score_start=warmup_start, score_end=spec.valid_end,
            rebalance_every=C.SIGNAL_STRIDE_N_DAYS,
            chunk_months=int(args.chunk_months),
            model_name=model_name,
        )

    # ----------------------------------------------------------------- 铁律④：折 4 重构自证
    regression: dict = {"checked": False}
    if 4 in fold_specs and not args.skip_regression:
        _log("=== 铁律④：折 4 重构自证（训练段与原 2016~2021 逐日相同 ⇒ 必须复现封存产物）===")
        spec4 = fold_specs[4]
        sealed_scores = v2_root / f"stock_scores_v2_{ablation}.csv"
        wf_scores = fold_scores[4]
        reg: dict = {"fold_id": 4, "train_end": spec4.train_end}
        if sealed_scores.is_file() and wf_scores.is_file():
            a = pd.read_csv(sealed_scores, dtype={"instrument": str})
            b = pd.read_csv(wf_scores, dtype={"instrument": str})
            for df in (a, b):
                df["date"] = pd.to_datetime(df["date"])
            vs, ve = pd.Timestamp(spec4.valid_start), pd.Timestamp(spec4.valid_end)
            a = a[(a["date"] >= vs) & (a["date"] <= ve)]
            b = b[(b["date"] >= vs) & (b["date"] <= ve)]
            m = a.merge(b, on=["date", "instrument"], suffixes=("_sealed", "_wf"))
            if len(m) == 0:
                raise SystemExit("🔴🔴 折 4 重构自证失败：与封存分数**零行重合**")
            max_abs = float((m["score_sealed"] - m["score_wf"]).abs().max())
            reg["n_rows_compared"] = int(len(m))
            reg["n_rows_sealed"] = int(len(a))
            reg["n_rows_wf"] = int(len(b))
            reg["max_abs_score_diff"] = max_abs
            reg["scores_bit_identical"] = bool(max_abs <= 1e-9)
            _log(f"  分数比对：{len(m)} 行｜max|Δscore| = {max_abs:.3e}"
                 f" ⇒ {'✅ 逐位一致' if reg['scores_bit_identical'] else '⚠️ 不一致'}")
            if not reg["scores_bit_identical"]:
                raise SystemExit("🔴🔴 折 4 重构自证失败：S 层分数与封存产物不一致")
        else:
            _log("  ⚠️ 未找到封存/折 4 分数文件 ⇒ 跳过分数逐位比对")

        # ---- 目标权重逐日复现（用冻结参数）----
        frozen_path = v2_root / "frozen_params.json"
        sealed_csv = v2_root / "blind_diagnostics.csv"
        if frozen_path.is_file() and sealed_csv.is_file():
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
            fp = dict(frozen.get("params") or {})
            fp["_model_name"] = fold_models[4]
            fp.setdefault("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)
            # 🔴 复现必须用**封存运行当时那份信号日列表**（2022~2023 的 val 段列表）：
            #    列表的**首日与奇偶**决定 [::stride] 选出的调仓日 ⇒ 换成折内子集就会漂。
            sealed_dates_csv = v2_root / f"stock_scores_v2_{ablation}.csv"
            if sealed_dates_csv.is_file():
                _sd = pd.read_csv(sealed_dates_csv, usecols=["date"])
                _sd["date"] = pd.to_datetime(_sd["date"])
                _vals = sorted(set(pd.Timestamp(d) for d in _sd["date"].unique()
                                   if pd.Timestamp(C.VALID[0]) <= pd.Timestamp(d) <= pd.Timestamp(C.VALID[1])))
                signal4 = R.val_score_dates_without_label_leak(_vals)
            else:
                signal4, _ = fold_signal_days(spec4, 1)
            fstride = int(C.rebalance_signal_stride_for(
                int(fp.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS))))
            _log(f"  复现用信号日 {len(signal4)} 个｜冻结 cadence="
                 f"{fp.get('rebalance_every_n_days')}（stride={fstride}）")
            ns = argparse.Namespace()
            tg, dg = R._build_targets_for(ns, wf_dir, fp, close, amount, names, signal4)
            sd = pd.read_csv(sealed_csv, dtype={"code": str})
            sd["date"] = sd["date"].astype(str)
            sd = sd[(sd["date"] >= str(spec4.valid_start)) & (sd["date"] <= str(spec4.valid_end))]
            bad: list[str] = []
            for day, grp in sd.groupby("date"):
                want = sorted(grp[grp["selected"].astype(bool)]["code"].astype(str).tolist())
                got = sorted(dg[(dg["date"].astype(str) == day) &
                                (dg["selected"].astype(bool))]["code"].astype(str).tolist())
                if want != got:
                    bad.append(f"{day}: 封存={want} 重建={got}")
            reg["n_rebalance_days_compared"] = int(sd["date"].nunique())
            reg["targets_identical"] = bool(not bad)
            if bad:
                _log("  🔴 目标权重不一致：")
                for line in bad[:5]:
                    _log(f"     {line}")
                raise SystemExit("🔴🔴 折 4 重构自证失败：目标权重与封存 blind_diagnostics 不一致")
            _log(f"  ✅ 目标权重逐日一致（{sd['date'].nunique()} 个调仓日）")
        regression = {"checked": True, **reg}
    elif not args.skip_regression:
        _log("⚠️ 本次不含折 4 ⇒ 跳过重构自证（正式跑必须含折 4）")

    # ----------------------------------------------------------------- 阶段 B：逐折评估
    ns = argparse.Namespace()
    results: dict[tuple, dict] = {}
    per_fold_rows: list[dict] = []
    oos_panels: dict[tuple, pd.DataFrame] = {}
    void_folds: list[dict] = []

    combos = [(arm, buf, gate) for arm in arms for buf in buffers for gate in gate_grid]
    _log(f"=== 阶段 B：{len(combos)} 组参数 × {len(fold_ids)} 折 = "
         f"{len(combos) * len(fold_ids)} 次回测 ===")

    for arm, buf, gate in combos:
        key = (arm["name"], buf, gate["label"])
        nav_list: list[pd.Series] = []
        bench_list: list[pd.Series] = []
        turn_map: dict[str, float] = {}
        rank_panels: list[pd.DataFrame] = []
        fold_metrics: list[dict] = []
        for fid in fold_ids:
            spec = fold_specs[fid]
            vs_ = pd.Timestamp(spec.valid_start)
            ve_ = pd.Timestamp(spec.valid_end)
            n_px = int(((close.index >= vs_) & (close.index <= ve_)).sum())
            if n_px < 2:
                _log(f"  ⚠️ 折 {fid}（{spec.valid_start}~{spec.valid_end}）："
                     f"**ETF 行情面板无覆盖**（{n_px} 个交易日）⇒ 该折**作废**（不计入 OOS）")
                void_folds.append({"fold_id": fid, "valid_start": spec.valid_start,
                                   "valid_end": spec.valid_end, "n_price_days": n_px,
                                   "reason": "🔴 ETF 行情面板无覆盖（数据起点晚于该折验证年）"})
                continue
            p = {
                "_model_name": fold_models[fid],
                "alpha_weight": arm["alpha_weight"],
                "momentum_weight": arm["momentum_weight"],
                "entry_ma_window": arm["entry_ma_window"],
                "exit_ma_window": arm["exit_ma_window"],
                "smoothing_arm": arm.get("smoothing_arm"),
                "allow_theme_cap": arm["allow_theme_cap"],
                "top_k": int(C.WALK_FORWARD_TOPK_LOCKED),
                "hold_buffer": int(buf),
                "rebalance_every_n_days": int(C.WALK_FORWARD_CADENCE),
                "neg_score_gate": bool(gate["enabled"]),
                "neg_score_gate_stat": str(C.NEG_SCORE_GATE_STAT),
                "neg_score_gate_scale": float(gate["scale"]),
                "neg_score_gate_min_exposure": float(gate["min_exposure"]),
            }
            warmup_start = SP.trading_days_before(
                SP._d(spec.valid_start), int(args.warmup_days)).isoformat()
            signal, rebal = fold_signal_days(spec, stride, warmup_start)
            targets, diag = R._build_targets_for(ns, wf_dir, p, close, amount, names, signal)
            if not targets:
                _log(f"  ⚠️ 折 {fid}｜{arm['name']} b={buf} {gate['label']}：无目标 ⇒ 跳过")
                continue
            vs = pd.Timestamp(spec.valid_start)
            ve = pd.Timestamp(spec.valid_end)
            close_fold = close.loc[(close.index >= vs) & (close.index <= ve)]
            bt = R._backtest(close_fold, targets, p)
            nav = bt["nav"]
            nav_list.append(nav)
            for k, v in (bt.get("turnover_by_date") or {}).items():
                turn_map[str(k)] = float(v)
            if bench_full is not None:
                b = bench_full.loc[(bench_full.index >= vs) & (bench_full.index <= ve)]
                bench_list.append(b / b.iloc[0] if len(b) else b)
            # ---- ETF 层排序分面板（诊断用；末尾 25 个交易日裁掉，标签窗口不许越年）----
            rank_panel = pd.DataFrame(
                {pd.Timestamp(d): g.set_index("code")["final_score"]
                 for d, g in diag.groupby("date")},
            ).T
            rank_panel.index = pd.DatetimeIndex(rank_panel.index)
            rank_panel = rank_panel.loc[(rank_panel.index >= vs) & (rank_panel.index <= ve)]
            keep = [d for d in rank_panel.index
                    if pd.Timestamp(d) in set(R.val_score_dates_without_label_leak(
                        list(rank_panel.index), valid_end=str(spec.valid_end)))]
            rank_panels.append(rank_panel.loc[keep])
            mfold = ME.summarize(nav, None, bt.get("turnover_by_date"),
                                 commission=float(C.COMMISSION), slippage=float(C.SLIPPAGE),
                                 periods_per_year=250)
            fold_metrics.append({
                "fold_id": fid, "year": str(spec.valid_start)[:4],
                "annualized_return": mfold["annualized_return"],
                "max_drawdown": mfold["max_drawdown"],
                "annualized_turnover": mfold["annualized_turnover"],
                # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
                "annualized_turnover_one_way": C.one_way_turnover(mfold["annualized_turnover"]),
                "excess_vs_benchmark": None,
                "n_rebalances": int(len(targets)),
            })
            per_fold_rows.append({
                "arm": arm["name"], "hold_buffer": buf, "gate": gate["label"],
                "fold_id": fid, "year": str(spec.valid_start)[:4],
                "train_start": spec.train_start, "train_end": spec.train_end,
                "valid_start": spec.valid_start, "valid_end": spec.valid_end,
                "annualized_return": mfold["annualized_return"],
                "max_drawdown": mfold["max_drawdown"],
                "annualized_turnover": mfold["annualized_turnover"],
                # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
                "annualized_turnover_one_way": C.one_way_turnover(mfold["annualized_turnover"]),
                "total_turnover": mfold["total_turnover"],
                "n_rebalances": int(len(targets)),
            })
        if not nav_list:
            continue
        # ---- 拼接 5 折 OOS ----
        rets = pd.concat([n.pct_change().dropna() for n in nav_list])
        nav_oos = (1.0 + rets).cumprod()
        nav_oos.index = pd.DatetimeIndex(nav_oos.index)
        bench_oos = None
        if bench_list and len(bench_list) == len(nav_list):
            brets = pd.concat([b.pct_change().dropna() for b in bench_list])
            bench_oos = (1.0 + brets).cumprod()
            bench_oos.index = pd.DatetimeIndex(bench_oos.index)
        to_series = pd.Series(turn_map, dtype="float64").sort_index()
        m = ME.summarize(nav_oos, bench_oos, to_series,
                         commission=float(C.COMMISSION), slippage=float(C.SLIPPAGE),
                         periods_per_year=250)
        turn = float(m.get("annualized_turnover") or 0.0)
        exc = m.get("excess_vs_benchmark")
        excess = float(exc) if exc is not None and pd.notna(exc) else float(m["annualized_return"])
        # 🔴 2026-11-18 用户目标文档修正：惩罚项**必须有上限**（唯一实现见
        #    `config_v2.turnover_penalty`）。旧口径 `0.05 * max(0, turn - 4.0)`
        #    在 cadence=20 下恒 ≈ 0.416 ⇒ 把超额（~0.1）完全盖住
        #    ⇒ 目标函数退化成「最小化换手」⇒ 选出「蜗牛参数」。
        penalty = C.turnover_penalty(turn, float(C.TURNOVER_TARGET))
        score = excess - penalty
        panel = pd.concat(rank_panels) if rank_panels else pd.DataFrame()
        ic = AT.etf_rank_ic(panel, fwd20) if len(panel) else {"ic": None}
        results[key] = {
            "arm": arm["name"], "arm_desc": arm["desc"], "hold_buffer": int(buf),
            "gate": gate["label"], "gate_params": {k: v for k, v in gate.items() if k != "label"},
            "params": {k: v for k, v in p.items() if k != "_model_name"},
            "oos_metrics": {
                "annualized_return": m.get("annualized_return"),
                "max_drawdown": m.get("max_drawdown"),
                "sharpe": m.get("sharpe"),
                "calmar": m.get("calmar"),
                "annualized_turnover": m.get("annualized_turnover"),
                # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
                "annualized_turnover_one_way": C.one_way_turnover(m.get("annualized_turnover")),
                "total_turnover": m.get("total_turnover"),
                "total_cost_drag": m.get("total_cost_drag"),
                "excess_vs_benchmark": m.get("excess_vs_benchmark"),
            },
            "score": float(score), "turnover_penalty": float(penalty),
            "etf_rank_ic": ic,
            "n_oos_days": int(len(nav_oos)),
            "per_fold": fold_metrics,
            "_nav": nav_oos, "_bench": bench_oos, "_panel": panel,
        }
        _log(f"  {arm['name']} b={buf} {gate['label']}｜OOS 年化 {_fmt(m.get('annualized_return'))}"
             f"｜回撤 {_fmt(m.get('max_drawdown'))}｜换手 {_fmt(m.get('annualized_turnover'))}"
             f"｜超额 {_fmt(m.get('excess_vs_benchmark'))}"
             f"｜ETF层RankIC {_fmt(ic.get('ic'))}｜score {_fmt(score)}")

    # ----------------------------------------------------------------- 阶段 C：汇总落盘
    rows: list[dict] = []
    for key, r in results.items():
        rows.append({
            "arm": r["arm"], "hold_buffer": r["hold_buffer"], "gate": r["gate"],
            "annualized_return": r["oos_metrics"]["annualized_return"],
            "max_drawdown": r["oos_metrics"]["max_drawdown"],
            "sharpe": r["oos_metrics"]["sharpe"],
            "annualized_turnover": r["oos_metrics"]["annualized_turnover"],
            # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
            "annualized_turnover_one_way": C.one_way_turnover(r["oos_metrics"]["annualized_turnover"]),
            "total_turnover": r["oos_metrics"]["total_turnover"],
            "excess_vs_benchmark": r["oos_metrics"]["excess_vs_benchmark"],
            "etf_rank_ic": (r["etf_rank_ic"] or {}).get("ic"),
            "etf_rank_ic_n": (r["etf_rank_ic"] or {}).get("n_cross_sections"),
            "score": r["score"],
            "n_oos_days": r["n_oos_days"],
        })
    rows.sort(key=lambda x: (x["score"] is None, -(x["score"] or -1e18)))
    _write_csv(out_dir / "oos_grid.csv", rows)
    _write_csv(out_dir / "per_fold.csv", per_fold_rows)

    best = None
    if rows:
        best_key = (rows[0]["arm"], rows[0]["hold_buffer"], rows[0]["gate"])
        best = results[best_key]
    # 🔴 2026-11-18：**实际**覆盖的 OOS 窗口（折 1 若因行情面板起点而作废，
    #    实际窗口就是 2020 起 ⇒ 名义窗口 2019-2023 是**假的**，不许照抄）。
    _void_ids = {int(v["fold_id"]) for v in void_folds}
    _survived = [fid for fid in fold_ids if fid not in _void_ids]
    _oos_actual = (
        (min(str(fold_specs[fid].valid_start) for fid in _survived),
         max(str(fold_specs[fid].valid_end) for fid in _survived))
        if _survived else None
    )
    _n_oos_folds = len(_survived)

    summary = {
        "mode": "walk_forward_5fold_expanding_window",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "split_summary": SP.walk_forward_summary(),
        "grid": {
            "arms": [a["name"] for a in arms],
            "hold_buffer": buffers,
            "gate": [g["label"] for g in gate_grid],
            "locked": {"rebalance_every_n_days": C.WALK_FORWARD_CADENCE,
                       "top_k": C.WALK_FORWARD_TOPK_LOCKED},
            "n_configs": len(combos),
        },
        "objective": C.OBJECTIVE_DESCRIPTION,
        "regression_check": regression,
        "void_folds": void_folds,
        "oos_window_nominal": list(C.WALK_FORWARD_OOS_WINDOW),
        "oos_window_actual": (list(_oos_actual) if _oos_actual else None),
        "n_folds_scored": _n_oos_folds,
        "n_folds_requested": len(fold_ids),
        "price_panel_coverage": {
            "first_date": str(pd.Timestamp(close.index[0]).date()),
            "last_date": str(pd.Timestamp(close.index[-1]).date()),
            "n_days": int(len(close.index)),
            "note": ("🔴 ETF 行情面板的起点决定了**哪几折可评**：起点之前的折**作废**"
                     "（不是模型不行，是没有可交易的价格）"),
        },
        "best": None if best is None else {
            "arm": best["arm"], "arm_desc": best["arm_desc"],
            "hold_buffer": best["hold_buffer"], "gate": best["gate"],
            "params": best["params"], "oos_metrics": best["oos_metrics"],
            "score": best["score"], "etf_rank_ic": best["etf_rank_ic"],
        },
        "ranking": rows,
        "disclosure": [
            "🔴 训练段起点 = 2016-01-01（用户规格写 2015；数据本身从 2016 起）⇒ 折 1 训练段少 1 年。",
            "🔴 embargo = 25 个交易日（= max(规格下限 20, 标签窗口末端 25)）⇒ 满足「≥20」上界。",
            "🔴 每折从**空仓**起算（不做跨折持仓结转）⇒ 拼接 OOS 在折边界有轻微跳空。",
            f"🔴 平滑臂在每折有**冷启动**：打分窗口向前延伸 {int(args.warmup_days)} 个交易日做 EWMA 预热，"
            "但该预热段可能落在该折的**训练段内**（样本内）⇒ 只影响折内前 ~25% 的 EWMA 状态，"
            "且随半衰期（15 日）指数衰减；臂 B（原生慢变量）完全不受影响。",
            "🔴 拼接 OOS 的年化按（净值点数−1）/250 计 ⇒ 5 年窗口被记为约 4.84 年，年化略被高估。",
            "🔴 **测试段（2024–2026）单盲已消耗**：本探针产出的候选参数**不得**回头用它验收。",
            f"🔴 **换手惩罚有上限**（`config_v2.TURNOVER_PENALTY_CAP`={C.TURNOVER_PENALTY_CAP}）："
            f"无上限时惩罚项（~0.42）会把超额（~0.1）完全盖住 ⇒ 选出的是「蜗牛参数」（cadence=90）。",
            f"🔴 **换手 ≤4× 红线在 cadence={C.WALK_FORWARD_CADENCE} 下不可达**（结构性，不是搜索不到）"
            f"⇒ 降换手只能靠「月频 + Rank Buffer（宽出严进）」，**不是**拉长调仓周期。",
        ],
    }
    (out_dir / "walkforward_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- 逐折 OOS 净值图数据（供报告/画图）----
    if best is not None:
        nav_df = pd.DataFrame({
            "nav": best["_nav"],
            "benchmark": best["_bench"] if best["_bench"] is not None else np.nan,
        })
        nav_df.to_csv(out_dir / "oos_nav_best.csv", encoding="utf-8-sig")
        best_panel = best.get("_panel")
        if best_panel is not None and len(best_panel) > 0:
            best_panel.to_csv(out_dir / "oos_rank_score_best.csv", encoding="utf-8-sig")

    # ---- Markdown 报告 ----
    lines: list[str] = []
    _act = summary.get("oos_window_actual")
    _nom = list(C.WALK_FORWARD_OOS_WINDOW)
    _win_txt = (f"{_act[0]} ~ {_act[1]}" if _act else f"{_nom[0]} ~ {_nom[1]}（无折实际评分）")
    lines.append(f"# V2 · 5 折滚动前推验证报告（{_win_txt} OOS）\n")
    lines.append(f"- 生成时间：{summary['generated_at']}")
    lines.append(f"- 产物目录：`{out_dir}`")
    if _act:
        lines.append(f"- OOS 窗口（**实际**）：**{_act[0]} ~ {_act[1]}**"
                     f"（{summary.get('n_folds_scored')} / {summary.get('n_folds_requested')} 折实际评分）")
        if list(_act) != _nom:
            lines.append(f"- 🔴 **名义窗口 = {_nom[0]} ~ {_nom[1]}**，但**实际**只有 "
                         f"{_act[0]} ~ {_act[1]} ⇒ 见「作废折」一节（行情面板起点决定）")
    else:
        lines.append(f"- OOS 窗口：**{_nom[0]} ~ {_nom[1]}**（**无任何折实际评分**）")
    lines.append(f"- 网格：{len(combos)} 组（cadence={C.WALK_FORWARD_CADENCE} 焊死、"
                 f"top_k={C.WALK_FORWARD_TOPK_LOCKED} 锁死）\n")
    lines.append("## 1. 折定义\n")
    lines.append("| 折 | 训练段（embargo 裁剪后） | 训练末样本标签窗口结束日 | 验证年 | 交易日 |")
    lines.append("|---|---|---|---|---|")
    for f in summary["split_summary"]["folds"]:
        lines.append(f"| {f['fold_id']} | {f['train_start']} ~ {f['train_end']} | "
                     f"{f['last_train_label_window_end']} | {f['valid_start']} ~ {f['valid_end']} | "
                     f"{f['n_valid_days']} |")
    lines.append("")
    lines.append("## 2. 拼接 OOS 网格结果（按目标函数排序）\n")
    # 🔴 2026-11-18 换手口径正名：``annualized_turnover`` = **双边**（Σ|Δw|），
    #    ``annualized_turnover_one_way`` = **单边**（= 0.5 × 双边，同一行的并列键，
    #    落盘见上方 rows）⇒ 表头标口径并**新增**单边列（**排序 / 统计逻辑一字未改**）。
    lines.append("| # | 方案 | buffer | 闸门 | 年化 | 回撤 | 年化换手（双边） | "
                 "年化换手（单边） | 超额 | ETF层RankIC | score |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | {r['arm']} | {r['hold_buffer']} | {r['gate']} | "
                     f"{_fmt(r['annualized_return'])} | {_fmt(r['max_drawdown'])} | "
                     f"{_fmt(r['annualized_turnover'])} | "
                     f"{_fmt(r['annualized_turnover_one_way'])} | "
                     f"{_fmt(r['excess_vs_benchmark'])} | "
                     f"{_fmt(r['etf_rank_ic'])} | {_fmt(r['score'])} |")
    lines.append("")
    if best is not None:
        lines.append("## 3. 最优候选\n")
        lines.append(f"- 方案：**{best['arm']}**（{best['arm_desc']}）")
        lines.append(f"- hold_buffer = **{best['hold_buffer']}**，闸门 = `{best['gate']}`")
        lines.append(f"- 拼接 OOS：年化 {_fmt(best['oos_metrics']['annualized_return'])}｜"
                     f"回撤 {_fmt(best['oos_metrics']['max_drawdown'])}｜"
                     f"换手 {_fmt(best['oos_metrics']['annualized_turnover'])}｜"
                     f"超额 {_fmt(best['oos_metrics']['excess_vs_benchmark'])}")
        lines.append(f"- ETF 层 20 日 RankIC：{_fmt((best['etf_rank_ic'] or {}).get('ic'))}"
                     f"（n = {(best['etf_rank_ic'] or {}).get('n_cross_sections')} 个截面）")
    lines.append("\n## 4. 披露（不许藏）\n")
    for d in summary["disclosure"]:
        lines.append(f"- {d}")
    lines.append("")
    (out_dir / "walkforward_report.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- 已封存产物 md5 自检 ----
    after = {name: _md5(v2_root / name) for name in SEALED}
    changed = [k for k in before if before[k] != after[k]]
    if changed:
        raise SystemExit(f"🔴🔴 已封存产物被改动：{changed}")
    _log("✅ 已封存产物 md5 全部未变（单盲 / 冻结 / 正式搜索 / 单盲诊断 / 封存分数）")
    _log(f"[OK] 探针产物目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
