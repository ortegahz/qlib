"""A 股截面打分 + Top-K 选股主流水线。

流程：初始化 → 建 dataset → 训练 LGBModel → 生成 pred.pkl → 回测 → 落盘 manifest。
每一步都按 manu/memory/rules.md 的铁律做断言与留痕：
  - §12.4 数据质量断言先行（行数/NaN/区间/列数交叉核对/覆盖率 M-N）
  - §12.7 fit_* 严格落在 train 段内
  - §12.8 manifest 齐备（commit/配置/种子/数据版本/依赖版本/产物 md5）

用法（服务器）：
    python manu/experiments/ashare_topk_selection/run_pipeline.py
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
from determinism import apply_deterministic_position_ordering  # noqa: E402

# qlib 0.9.8 的合法 data_key（DataHandler.ATTR_MAP 的键）
DK_R, DK_I, DK_L = "raw", "infer", "learn"


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_git_commit() -> dict:
    """commit 只能在**本地 checkout** 取（服务器工程目录无 .git）。

    允许用环境变量 QLIB_LOCAL_REPO 覆盖路径 —— 同步到服务器后可用它把本地
    commit 带进来；取不到就如实记 null，绝不编造。
    """
    # 🔴 rules.md §12.8：服务器工程目录没有 .git，commit 只能从【本地 checkout】取，
    #    再通过环境变量带进来（QLIB_GIT_COMMIT / QLIB_GIT_DIRTY）。
    env_commit = os.environ.get("QLIB_GIT_COMMIT")
    if env_commit:
        return {
            "commit": env_commit,
            "dirty": os.environ.get("QLIB_GIT_DIRTY") == "1",
            "manu_tracked_in_git": os.environ.get("QLIB_GIT_MANU_TRACKED") == "1",
            "source": "env QLIB_GIT_COMMIT（由本地 checkout 取，服务器无 .git）",
            "note": None if os.environ.get("QLIB_GIT_MANU_TRACKED") == "1"
            else "manu/ 未纳入版本控制（commit 不含自研代码）",
        }
    repo = Path("/media/manu/1TB-Volume/workspace/qlib")
    if not (repo / ".git").is_dir():
        return {"commit": None, "note": "本地 checkout 不存在或非 git 仓库（服务器工程目录无 .git）"}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=20
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, timeout=20
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return {"commit": None, "error": f"{type(exc).__name__}: {exc}"}
    manu_tracked = bool(
        subprocess.run(
            ["git", "ls-files", "manu/"], cwd=repo, capture_output=True, text=True, timeout=20
        ).stdout.strip()
    )
    return {
        "commit": commit,
        "dirty": bool(dirty),
        "manu_tracked_in_git": manu_tracked,
        "note": None if manu_tracked else "manu/ 未纳入版本控制（commit 不含自研代码）",
    }


def universe_pairs_count(seg_start: str, seg_end: str, calendar: list) -> int:
    """N = 该段内「按成分股文件本应存在」的 (标的, 交易日) 对数。

    qlib 的 instruments/*.txt 是 TSV，**无表头**，每行 `symbol\tstart\tend`。
    这个 N 是覆盖率的分母：M 是数据集里真实有的样本数。
    """
    inst_file = Path(C.PROVIDER_URI) / "instruments" / f"{C.INSTRUMENTS}.txt"
    if not inst_file.is_file():
        return 0
    import bisect

    total = 0
    for line in inst_file.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        sym, st, en = parts
        lo = max(str(st), str(seg_start))
        hi = min(str(en), str(seg_end))
        if lo > hi:
            continue
        # 日历是已排序的日期串
        i = bisect.bisect_left(calendar, lo)
        j = bisect.bisect_right(calendar, hi)
        total += max(j - i, 0)
    return total


def assert_data_quality(dataset) -> dict:
    """rules.md §12.4：数据质量断言先行，禁止静默空数据。"""
    from qlib.data import D

    stats: dict = {}

    # 特征列数交叉核对：用 loader 解析出的表达式条数，而不是只看 df.shape[1]
    loader = getattr(dataset.handler, "data_loader", None)
    fields = getattr(loader, "fields", None)
    n_expected_cols = None
    if isinstance(fields, dict) and "feature" in fields:
        # loader.fields = {"feature": (exprs, names), "label": (exprs, names)}
        # col_set="feature" 只取 feature 组，所以期望列数 = feature 组的 names 长度
        n_expected_cols = len(fields["feature"][1])
    stats["n_expected_feature_cols"] = n_expected_cols

    # 🔴 日历统一成 "%Y-%m-%d" 字符串：str(Timestamp) 会带 " 00:00:00"，
    #    与 instruments 文件里的日期做字符串比较会错。
    cal = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in D.calendar(freq="day")]
    seg_bounds = {"train": C.TRAIN, "valid": C.VALID, "test": C.TEST}

    for seg in ("train", "valid", "test"):
        df = dataset.prepare(seg, col_set="feature", data_key=DK_I)
        n_rows, n_cols = df.shape
        n_inst = df.index.get_level_values("instrument").nunique()
        nan_cols = [c for c in df.columns if df[c].isna().all()]
        first_dt = str(df.index.get_level_values("datetime").min())
        last_dt = str(df.index.get_level_values("datetime").max())
        stats[seg] = {
            "n_rows": int(n_rows),
            "n_cols": int(n_cols),
            "n_instruments": int(n_inst),
            "n_all_nan_cols": len(nan_cols),
            "all_nan_col_names": nan_cols[:10],
            "first_dt": first_dt,
            "last_dt": last_dt,
        }
        # 🔴 断言：行数 > 0
        assert n_rows > 0, f"[{seg}] 行数为 0 —— 数据为空，拒绝继续"
        # 🔴 断言：特征列数 > 0
        assert n_cols > 0, f"[{seg}] 特征列数为 0 —— handler 配置可能无效"
        # 🔴 断言：不得有全 NaN 列
        assert len(nan_cols) == 0, f"[{seg}] 存在 {len(nan_cols)} 个全 NaN 特征列: {nan_cols[:5]}"
        # 🔴 断言：区间必须覆盖声明的切分（rules.md §12.4「静默的部分失败」）
        want_lo, want_hi = seg_bounds[seg]
        assert first_dt[:10] >= want_lo[:10], f"[{seg}] 实际起点 {first_dt} 晚于声明起点 {want_lo}"
        assert last_dt[:10] <= want_hi[:10], f"[{seg}] 实际终点 {last_dt} 晚于声明终点 {want_hi}"
        # 🔴 断言：特征列数必须等于 loader 解析出的表达式条数（防止静默丢列）
        if n_expected_cols is not None:
            assert n_cols == n_expected_cols, (
                f"[{seg}] 特征列数 {n_cols} != loader 解析出的表达式条数 {n_expected_cols} —— 有列被静默丢弃"
            )
        print(f"[assert] {seg}: rows={n_rows} cols={n_cols} insts={n_inst} "
              f"range={first_dt}~{last_dt} OK")

    # 🔴 覆盖率 M/N（rules.md §12.4）：M = 数据集实有样本数，N = 成分股应有的样本数
    for seg in ("train", "valid", "test"):
        df = dataset.prepare(seg, col_set="feature", data_key=DK_I)
        m = int(df.shape[0])
        n = universe_pairs_count(seg_bounds[seg][0], seg_bounds[seg][1], cal)
        ratio = (m / n) if n else float("nan")
        stats[f"coverage_{seg}"] = {"M": m, "N": n, "ratio": None if n == 0 else round(ratio, 4)}
        print(f"[assert] 覆盖率 M/N [{seg}]: M={m} / N={n} = {ratio:.4f}")
        assert n > 0, f"[{seg}] N（成分股应有样本数）为 0 —— 股票池文件或区间有问题"
        assert ratio > 0.5, f"[{seg}] 覆盖率 {ratio:.4f} 过低 —— 数据不完整，拒绝继续"

    # train/valid 段另报「学习集」行数：DropnaLabel 会删掉标签缺失行，
    # 训练实际用的行数与 infer 行数不同，不能混为一谈。
    for seg in ("train", "valid"):
        df_learn = dataset.prepare(seg, col_set="feature", data_key=DK_L)
        stats[f"{seg}_learn_rows"] = int(df_learn.shape[0])
        print(f"[assert] {seg} 学习集行数(DK_L, 已 DropnaLabel) = {df_learn.shape[0]}")

    return stats


def compute_daily_ic(pred: pd.Series, label: pd.Series) -> dict:
    """日度 RankIC（截面 Spearman）。

    IC 是「打分有没有区分度」的直接度量 —— 每天把预测分和真实收益做截面秩相关，
    再对时间序列取均值。ICIR = mean(IC) / std(IC)。
    """
    label_dates = set(label.index.get_level_values("datetime").unique())
    ics: list[float] = []
    for dt in pred.index.get_level_values("datetime").unique():
        if dt not in label_dates:
            # 末位几个交易日没有未来数据 => 标签为 NaN => 已被 DropnaLabel 剔除
            continue
        p = pred.xs(dt, level="datetime")
        l = label.xs(dt, level="datetime")
        common = p.index.intersection(l.index)
        if len(common) < 20:
            continue
        pc = p.loc[common].rank()
        lc = l.loc[common].rank()
        if float(pc.std()) == 0 or float(lc.std()) == 0:
            continue
        ics.append(float(pc.corr(lc)))
    if not ics:
        return {"mean_ic": None, "icir": None, "n_days": 0}
    arr = np.array(ics)
    return {
        "mean_ic": round(float(arr.mean()), 6),
        "icir": round(float(arr.mean() / arr.std()), 4) if arr.std() > 0 else None,
        "std_ic": round(float(arr.std()), 6),
        "n_days": len(ics),
        "t_stat": round(float(arr.mean() / arr.std() * np.sqrt(len(ics))), 3) if arr.std() > 0 else None,
    }


def main() -> int:
    t_start = time.monotonic()
    random.seed(C.GLOBAL_SEED)
    np.random.seed(C.GLOBAL_SEED)

    C.ensure_exp_root()

    import qlib
    from qlib.constant import REG_CN
    from qlib.utils import init_instance_by_config
    from qlib.workflow import R
    from qlib.workflow.record_temp import SignalRecord, PortAnaRecord

    print("== [1/6] qlib.init ==")
    # 🔴 exp_manager 的 kwargs 必须同时给 uri 和 default_exp_name：
    #    Config.set 是整键浅替换，只传 uri 会让 MLflowExpManager 缺
    #    default_exp_name 形参而 TypeError（ExpManager.__init__ 无默认值）。
    qlib.init(
        provider_uri=C.PROVIDER_URI,
        region=REG_CN,
        exp_manager={
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {"uri": C.RECORDER_URI, "default_exp_name": C.EXPERIMENT_NAME},
        },
    )
    print(f"   provider_uri = {C.PROVIDER_URI}")
    print(f"   recorder uri = {C.RECORDER_URI}")

    task_config = C.build_task_config()

    print("== [2/6] 构建 dataset ==")
    dataset = init_instance_by_config(task_config["dataset"])
    data_stats = assert_data_quality(dataset)

    print("== [3/6] 训练 LGBModel ==")
    model = init_instance_by_config(task_config["model"])

    with R.start(experiment_name=C.EXPERIMENT_NAME, uri=C.RECORDER_URI):
        recorder = R.get_recorder()
        model.fit(dataset)

        print("== [4/6] 生成预测信号 ==")
        sr = SignalRecord(model, dataset, recorder)
        sr.generate()

        print("== [5/6] 回测 ==")
        # 🔴 消除跨进程不确定性（rules.md §12.10）：qlib 的 Position.get_stock_list 返回
        #    set 迭代序（逐进程随机）⇒ 同分标的的卖/买取舍逐进程不同 ⇒ 回测不可逐位复现。
        #    补丁在 manu/ 内实现（运行期打补丁，上游 qlib/ 源码零改动）。
        #    ⚠️ 补丁会改变卖单的执行顺序 ⇒ 可能改变回测数值本身（详见 determinism.py 的说明）。
        determinism_status = apply_deterministic_position_ordering()
        determinism_patched_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        print(f"   确定性补丁 (position ordering): {determinism_status} @ {determinism_patched_at}")
        par = PortAnaRecord(recorder, task_config["port_analysis"])
        par.generate()

        # 🔴 Recorder 没有 save_metrics 方法，只有 log_metrics / log_params / set_tags
        recorder.log_metrics(run_seed=C.GLOBAL_SEED)
        recorder.log_params(
            instruments=C.INSTRUMENTS,
            train=str(C.TRAIN),
            valid=str(C.VALID),
            test=str(C.TEST),
            fit_start=C.FIT_START,
            fit_end=C.FIT_END,
            topk=C.TOPK,
            n_drop=C.N_DROP,
            benchmark=C.BENCHMARK,
        )
        # 🔴 set_tags 的签名是 (**kwargs)，传 dict 会 TypeError
        recorder.set_tags(
            **{
                "instruments": C.INSTRUMENTS,
                "fit_start": C.FIT_START,
                "fit_end": C.FIT_END,
                "topk": C.TOPK,
                "n_drop": C.N_DROP,
                "benchmark": C.BENCHMARK,
                "data_last_date": C.END_TIME,
            }
        )
        # 把训练好的模型也存进产物，否则这次 run 无法复用/复现
        recorder.save_objects(**{"model.pkl": model})
        print(f"   recorder uri: {recorder.uri}")
        recorder_id = recorder.id
        local_dir = Path(recorder.get_local_dir())

        # ---- 采集打分质量指标（valid / test 的日度 RankIC）----
        from qlib.data.dataset.handler import DataHandlerLP

        ic_stats: dict = {}
        for seg in ("valid", "test"):
            label_ser = dataset.prepare(
                seg, col_set="label", data_key=DataHandlerLP.DK_L
            ).iloc[:, 0]
            pred_ser = model.predict(dataset, segment=seg)
            ic_stats[seg] = compute_daily_ic(pred_ser, label_ser)
            print(f"[ic] {seg}: {ic_stats[seg]}")

        # ---- 采集回测指标（PortAnaRecord 记进 mlflow，这里再取出来落进 manifest）----
        try:
            backtest_metrics = {
                k: v for k, v in (recorder.list_metrics() or {}).items()
                if not k.startswith(("train.l2", "valid.l2"))
            }
        except Exception as exc:  # noqa: BLE001
            backtest_metrics = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"[backtest] metrics: {list(backtest_metrics)[:12]}")

    print("== [6/6] 落盘 manifest ==")
    import lightgbm
    import pandas

    # 🔴 标签口径必须记**实际生效**的值，不能读 handler 的类默认值。
    #    Alpha158.get_label_config() 返回的是 **close** 口径
    #    （`qlib/contrib/data/handler.py`：`["Ref($close, -2)/Ref($close, -1) - 1"], ["LABEL0"]`），
    #    而本工程通过 handler 的 `label` 形参**显式覆盖**成 VWAP
    #    （同文件 `kwargs.pop("label", self.get_label_config())`）。
    #    ⇒ 早期版本读 get_label_config() 会把 manifest 记成 close，
    #      与真实训练的模型口径不符（已实证：label.pkl 逐值等于 VWAP 表达式，误差 0）。
    #    真实口径取自 loader 解析后的 fields（DataHandlerLP.data_loader.fields["label"]）。
    try:
        label_exprs, label_names = dataset.handler.data_loader.fields["label"]
    except Exception as exc:  # noqa: BLE001
        label_exprs, label_names = [f"<unavailable: {exc}>"], []

    # 每次运行一个独立目录，避免后一次覆盖前一次的 manifest（rules.md §12.8）
    run_dir = C.RUNS_DIR / recorder_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 把关键产物复制到 run 目录，供 export_topk 直接取用
    artifacts_dir = local_dir / "artifacts"
    artifact_entries: list[dict] = []
    if artifacts_dir.is_dir():
        for p in sorted(artifacts_dir.iterdir()):
            if p.is_file():
                shutil.copy2(p, run_dir / p.name)
                artifact_entries.append(
                    {"name": p.name, "path": str(run_dir / p.name),
                     "bytes": p.stat().st_size, "md5": md5_of(p)}
                )
    # 模型文件通常在 run 目录下一层（pytorch_model.txt / model.txt）
    for p in sorted(local_dir.glob("*.txt")):
        shutil.copy2(p, run_dir / p.name)
        artifact_entries.append(
            {"name": p.name, "path": str(run_dir / p.name),
             "bytes": p.stat().st_size, "md5": md5_of(p)}
        )

    elapsed = time.monotonic() - t_start

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_sec": round(elapsed, 1),
        "host": platform.node(),
        "python": platform.python_version(),
        "recorder_id": recorder_id,
        "recorder_uri": C.RECORDER_URI,
        "run_dir": str(run_dir),
        "code": local_git_commit(),
        "config": {
            "provider_uri": C.PROVIDER_URI,
            "region": C.REGION,
            "instruments": C.INSTRUMENTS,
            "start_time": C.START_TIME,
            "end_time": C.END_TIME,
            "segments": {"train": C.TRAIN, "valid": C.VALID, "test": C.TEST},
            "fit": [C.FIT_START, C.FIT_END],
            "infer_processors": C.INFER_PROCESSORS,
            "learn_processors": C.LEARN_PROCESSORS,
            "label": {
                "expressions": list(label_exprs),
                "names": list(label_names),
                "note": "本工程显式覆盖为 VWAP 口径（T+1 VWAP -> T+2 VWAP）；"
                        "Alpha158 类默认值是 close，Alpha158vwap 才是 vwap",
            },
            "topk": C.TOPK,
            "n_drop": C.N_DROP,
            "account": C.ACCOUNT,
            "benchmark": C.BENCHMARK,
            "strategy_extra_kwargs": C.STRATEGY_EXTRA_KWARGS,
            "exchange_kwargs": C.EXCHANGE_KWARGS,
            "lgb_params": C.LGB_PARAMS,
            "num_boost_round": C.NUM_BOOST_ROUND,
            "early_stopping_rounds": C.EARLY_STOPPING_ROUNDS,
        },
        "seeds": {
            "global_seed": C.GLOBAL_SEED,
            "lightgbm_random_state": C.LGB_PARAMS.get("random_state"),
            "note": "qlib 层无 seed 参数；LightGBM 臂的种子挂在 random_state 上",
        },
        # 🔴 回测的确定性元数据（rules.md §12.10）：本轮回测**打了**确定性补丁，
        #    补丁会改变卖单的执行顺序 ⇒ 可能改变回测数值本身
        #    ⇒ 本 manifest 的换手/收益与「未打补丁那一轮」的数字不可直接比较。
        "determinism": {
            "patched": determinism_status in ("applied", "already-applied"),
            "patched_at": determinism_patched_at,
            "status": determinism_status,
            "target": "qlib.backtest.position.Position.get_stock_list",
            "effect": "把 set 迭代序改为字典序，消除逐进程随机；"
                      "⚠️ 同时会改变卖单的执行顺序 ⇒ 可能改变回测数值本身",
            "note": "上游 qlib/ 源码零改动，运行期打补丁"
                    "（manu/experiments/ashare_topk_selection/determinism.py）",
            "pythonhashseed_required": 0,
        },
        "data_stats": data_stats,
        "ic_stats": ic_stats,
        "backtest_metrics": backtest_metrics,
        "data_version": C.dataset_version(),
        "versions": {
            "qlib": qlib.__version__,
            "numpy": np.__version__,
            "pandas": pandas.__version__,
            "lightgbm": lightgbm.__version__,
        },
        "artifacts": artifact_entries,
        # manu/ 尚未纳入 git（commit 不覆盖自研代码），因此额外记录脚本 md5 以保证可追溯
        "script_md5": {
            p.name: md5_of(p) for p in sorted(Path(__file__).resolve().parent.glob("*.py"))
        },
    }
    out = run_dir / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    print(f"   manifest -> {out} (md5={md5_of(out)})")
    print(f"   artifacts: {[a['name'] for a in artifact_entries]}")
    print("== DONE ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
