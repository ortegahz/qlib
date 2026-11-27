#!/usr/bin/env python3
"""🔴 V3 **单折**训练 + 打分 worker（**独立进程** ⇒ 支持折级并行）。

**为什么独立进程**：折与折之间**完全独立**（各自训练、各自打分）⇒ 并行是最省事的提速；
用独立进程（而不是线程）可以避开 GIL、LightGBM/OpenMP 线程池争抢与 qlib 客户端的 fork 风险。

**用法（服务器）**
```bash
/home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/v3_fold_worker.py \
  --fold 1 --model-name wf1_v3_full --arm v3 \
  --out-dir <results>/v3/_comparison_<ts>/v3_full \
  --provider-uri <qlib provider> \
  --neutralize --center-mode market_mean --weight-mode sum_w --rolling \
  --threads 16 --result-json /tmp/v3_fold_worker/fold1.json
```

**输出**：`<out-dir>/stock_scores_v3_<model>.csv`（滚动口径 = 各窗口合并件）
＋ ``--result-json`` 落一行 JSON（折号 / CSV 路径 / 行数 / md5 / 耗时）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    # 🔴 口径名**从常量现取**（V3 目录已在 ``sys.path`` 上 ⇒ 不许手写死字符串）
    import config_v3 as _C

    p = argparse.ArgumentParser(description="V3 单折训练 + 打分 worker")
    p.add_argument("--fold", type=int, required=True)
    p.add_argument("--model-name", required=True)
    p.add_argument("--arm", choices=["v3", "control"], default="v3",
                   help="v3 = lambdarank（实验臂）；control = mse（对照臂）")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--provider-uri", default=None)
    p.add_argument("--universe-mode", default=None,
                   help="训练池口径（默认 = config_v3.UNIVERSE_MODE；"
                        f"逐日 PIT 口径 = {_C.UNIVERSE_MODE_PIT}）")
    p.add_argument("--center-mode", default="market_mean")
    p.add_argument("--weight-mode", default="sum_w",
                   help="样本权重口径：sum_w / sqrt_sum_w / uniform")
    p.add_argument("--neutralize", dest="neutralize", action="store_true", default=True)
    p.add_argument("--no-neutralize", dest="neutralize", action="store_false")
    p.add_argument("--rolling", dest="rolling", action="store_true", default=True)
    p.add_argument("--no-rolling", dest="rolling", action="store_false")
    p.add_argument("--threads", type=int, default=0,
                   help="本进程的 LightGBM / BLAS 线程数（0 = 不限制）")
    p.add_argument("--score-dates-file", default=None,
                   help="🔴 显式打分日清单（一个日期一行）⇒ 与对照臂同一批信号日")
    p.add_argument("--result-json", default=None)
    return p


def main() -> int:
    # 🔴 V3 目录**先**挂上 ``sys.path``（口径常量要从 ``config_v3`` 现取）
    here = Path(__file__).resolve().parent
    v3_dir = here.parent / "etf_rotation" / "v3"
    if str(v3_dir) not in sys.path:
        sys.path.insert(0, str(v3_dir))

    args = _build_parser().parse_args()
    t0 = time.time()

    # 🔴 线程数**必须在 import numpy / lightgbm 之前**设进环境变量
    #    （BLAS 与 LightGBM 的 OpenMP 池在 import/首次调用时读它）。
    if int(args.threads) > 0:
        n = str(int(args.threads))
        os.environ["OMP_NUM_THREADS"] = n
        os.environ["OPENBLAS_NUM_THREADS"] = n
        os.environ["MKL_NUM_THREADS"] = n

    import pandas as pd  # noqa: E402

    import config_v3 as C  # noqa: E402
    import rolling as ROL  # noqa: E402
    import splits as SP3  # noqa: E402
    import train_s as TS  # noqa: E402
    import universe as UNI  # noqa: E402

    if int(args.threads) > 0:
        n = int(args.threads)
        C.LGB_PARAMS = {**C.LGB_PARAMS, "num_threads": n}
        C.MSE_ARM_LGB_PARAMS = {**C.MSE_ARM_LGB_PARAMS, "num_threads": n}
        C.CONTROL_LGB_PARAMS = {**C.CONTROL_LGB_PARAMS, "num_threads": n}
    C.SAMPLE_WEIGHT_MODE = str(args.weight_mode)

    explicit_dates: list[str] | None = None
    if args.score_dates_file:
        sdf = Path(args.score_dates_file)
        if not sdf.is_file():
            raise FileNotFoundError(f"🔴 显式打分日清单不存在：{sdf}")
        explicit_dates = [ln.strip()[:10] for ln in
                          sdf.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not explicit_dates:
            raise ValueError(f"🔴 显式打分日清单为空：{sdf}")

    spec = SP3.build_fold_spec(int(args.fold))
    SP3.assert_fold_discipline(spec)

    provider_uri = str(args.provider_uri or C.DEFAULT_PROVIDER_URI)
    etf_codes = UNI.load_etf_universe()
    holdings = UNI.load_holdings()
    # 🔴 **I3**：PIT 面板的日期轴必须与训练/打分侧**同源**（provider 优先的交易日历）
    #    ⇒ 把 provider_uri 传进去（否则面板只认静态日历 ⇒ 日期轴可能对不上）。
    universe_info = UNI.build_universe(etf_codes, holdings, mode=args.universe_mode,
                                        provider_uri=provider_uri)

    # 🔴🔴 **每折必须有自己的产物目录**：
    #    ``rolling.rolling_retrain_and_score`` 会在 ``out_dir/rolling/window_XXX/``
    #    里写模型与分数 ⇒ 若多折共用同一个 ``out_dir``，**各折会互相覆盖**
    #    （实测 5 折并发时全部写进同一个 ``window_001`` ⇒ 产物混杂、结果作废）。
    #    ⇒ 这里强制按折号分目录（``<out-dir>/fold<f>/``）。
    out_dir = Path(args.out_dir) / f"fold{int(args.fold)}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.rolling:
        manifest = ROL.rolling_retrain_and_score(
            spec, out_dir=out_dir / "rolling", model_name=args.model_name,
            arm=args.arm, provider_uri=provider_uri, universe_info=universe_info,
            neutralize=bool(args.neutralize), center=str(args.center_mode),
            score_dates=explicit_dates,
        )
    else:
        manifest = TS.train_and_score(
            spec, out_dir=out_dir, model_name=args.model_name, arm=args.arm,
            provider_uri=provider_uri, universe_info=universe_info,
            neutralize=bool(args.neutralize), center=str(args.center_mode),
            score_start=spec.valid_start, score_end=spec.valid_end,
            score_dates=explicit_dates,
        )
    csv_path = Path(manifest["outputs"]["stock_scores_csv"])
    if not csv_path.is_file():
        raise FileNotFoundError(f"🔴 折 {args.fold} 的打分文件缺失：{csv_path}")

    df = pd.read_csv(csv_path, dtype={"instrument": str})
    result = {
        "fold": int(args.fold),
        "model_name": args.model_name,
        "arm": args.arm,
        "universe_mode": str(universe_info.get("mode") or ""),
        "pit_mode": bool(C.PIT_MEMBERSHIP_KEY in (universe_info or {})),
        "scores_csv": str(csv_path),
        "rows": int(len(df)),
        "n_score_dates": int(pd.to_datetime(df["date"]).nunique()),
        "scores_md5": TS.md5_of(csv_path),
        "n_samples": manifest.get("n_samples"),
        "n_features": manifest.get("n_features"),
        "objective": manifest.get("objective"),
        "sample_weight_mode": str(C.SAMPLE_WEIGHT_MODE),
        "score_dates_source": ("explicit" if explicit_dates else "grid"),
        "n_score_dates_explicit": (len(explicit_dates) if explicit_dates else 0),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    if args.result_json:
        rp = Path(args.result_json)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
