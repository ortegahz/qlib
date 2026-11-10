"""数据体检：回答「这份数据到底覆盖什么」，不训练、不回测。

只读探查，输出 JSON 到 stdout。目的是让下游的日期切分 / universe 选择
**由实测数据决定**，而不是照抄文档里的 2018-2024。

用法（服务器）：
    python manu/experiments/ashare_topk_selection/inspect_dataset.py \
        --provider-uri <落点根>/datasets/qlib/cn_data
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# ---- 探针口径常量 ----
# qlib 表达式探针用的表达式（必须在 qlib.init() 之后调用 D.features）
PROBE_EXPRESSIONS = ["$close", "Ref($close, 5)", "Mean($close, 20)"]
# 探针窗口（交易日）：必须足够长以覆盖最长回看期，否则序列头部的 rolling 窗口
# 未填满会产生大面积 NaN，导致「大面积 NaN」的假结论
PROBE_WINDOW_DAYS = 120
# 预热区：每只票在探针窗口内的前 N 个交易日，统计 NaN 率时剔除
WARMUP_DAYS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-uri", required=True, help="数据集绝对路径")
    parser.add_argument("--out", default=None, help="体检报告 JSON 落盘路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    uri = str(Path(args.provider_uri).expanduser().resolve())
    report: dict = {"provider_uri": uri}

    root = Path(uri)
    report["dir_exists"] = root.is_dir()
    if not root.is_dir():
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    # ---- 目录结构 ----
    report["subdirs"] = sorted(p.name for p in root.iterdir() if p.is_dir())
    for name in ("calendars", "instruments", "features"):
        report[f"has_{name}"] = (root / name).is_dir()

    # ---- 交易日历 ----
    cal_dir = root / "calendars"
    calendars: dict[str, dict] = {}
    if cal_dir.is_dir():
        for cal_file in sorted(cal_dir.glob("*.txt")):
            lines = [
                ln.strip()
                for ln in cal_file.read_text().splitlines()
                if ln.strip() and not ln.startswith("#")
            ]
            entry = {"n_days": len(lines)}
            if lines:
                entry["first"] = lines[0]
                entry["last"] = lines[-1]
            calendars[cal_file.stem] = entry
    report["calendars"] = calendars

    # ---- universe (instruments) ----
    inst_dir = root / "instruments"
    universes: dict[str, dict] = {}
    if inst_dir.is_dir():
        for f in sorted(inst_dir.glob("*.txt")):
            try:
                first_line = f.read_text().splitlines()[0]
            except (IndexError, OSError):
                first_line = ""
            # qlib instruments 文件首行形如 "csi300\t<start>\t<end>"，后续每行 "<symbol>\t<start>\t<end>"
            n_sym = sum(1 for ln in f.read_text().splitlines() if ln.strip()) - 1
            universes[f.stem] = {"header": first_line, "n_instruments": max(n_sym, 0)}
    report["universes"] = universes

    # ---- features 覆盖 ----
    # qlib 常见两种布局, 这里都兼容:
    #   A) features/<symbol_lower>/<field>.day.bin           (chenditc / SunsetWolf 实际布局)
    #   B) features/<freq>/<symbol_lower>/<field>.day.bin    (部分社区包)
    feat_dir = root / "features"
    feature_info: dict[str, dict] = {}
    if feat_dir.is_dir():
        children = sorted(p for p in feat_dir.iterdir() if p.is_dir())
        flat = bool(children) and any(children[0].glob("*.bin"))

        def _scan(sym_dirs: list[Path], label: str) -> dict:
            fields: set[str] = set()
            n_files = 0
            # 字段名在各标之间是齐的, 抽前 200 个标的统计字段, 避免全量扫 6 万个目录
            for sym_dir in sym_dirs[:200]:
                for bin_file in sym_dir.glob("*.bin"):
                    n_files += 1
                    fields.add(bin_file.name.rsplit(".", 1)[0])
            return {
                "layout": label,
                "n_instrument_dirs": len(sym_dirs),
                "n_bin_files_sampled": n_files,
                "n_distinct_fields": len(fields),
                "fields": sorted(fields),
            }

        if flat:
            feature_info["day"] = _scan(children, "features/<symbol>/<field>.bin")
        else:
            for freq_dir in children:
                sym_dirs = sorted(p for p in freq_dir.iterdir() if p.is_dir())
                feature_info[freq_dir.name] = _scan(sym_dirs, "features/<freq>/<symbol>/<field>.bin")
    report["features"] = feature_info

    # ---- 加载 qlib 做一次真实取数（限量：小样本）----
    try:
        import qlib
        from qlib.constant import REG_CN
    except Exception as exc:  # noqa: BLE001
        report["qlib_import"] = f"FAILED: {type(exc).__name__}: {exc}"
        print(f"❌ 导入 qlib 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        print(text)
        if args.out:
            Path(args.out).write_text(text)
        return 3

    try:
        qlib.init(provider_uri=uri, region=REG_CN)
    except Exception as exc:  # noqa: BLE001
        report["qlib_init"] = f"FAILED: {type(exc).__name__}: {exc}"
        print(f"❌ qlib.init 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        print(text)
        if args.out:
            Path(args.out).write_text(text)
        return 3
    report["qlib_init"] = "OK"

    # 🔴 算子注册表**必须在 qlib.init() 之后**查 —— init 之前 Operators 是空的
    from qlib.data.ops import Operators

    op_names = sorted(getattr(Operators, "_ops", {}).keys())
    required_ops = ["Ref", "Mean", "Std", "Corr"]
    missing_ops = [op for op in required_ops if op not in op_names]
    report["operators"] = {
        "n_registered": len(op_names),
        "required": {op: (op in op_names) for op in required_ops},
        "missing": missing_ops,
        "note": "算子注册表在 qlib.init() 之后才有内容（init 之前查必然是空的）",
    }

    from qlib.data import D

    cal = D.calendar(freq="day")
    if len(cal) == 0:
        report["qlib_calendar"] = {"n_days": 0}
        print("❌ 交易日历为空 —— 数据集里没有 calendars/day.txt 或无交易日", file=sys.stderr)
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        print(text)
        if args.out:
            Path(args.out).write_text(text)
        return 3
    report["qlib_calendar"] = {
        "n_days": len(cal),
        "first": str(cal[0]),
        "last": str(cal[-1]),
    }

    # 各 universe 的实际标的数：首日 / 中间某日 / 末日 三个截面一起看
    # （csi300 是**动态成分**，只看末日会漏掉「成分随时间变化」这件事）
    latest = cal[-1]
    first_day = cal[0]
    middle_day = cal[len(cal) // 2]
    uni_counts: dict[str, dict] = {}
    for uni in ("csi300", "csi500", "csi100", "all"):
        try:
            insts = D.instruments(market=uni)  # qlib 0.9.8: instruments() 无 freq 参数
            counts: dict[str, int] = {}
            sample: list[str] = []
            for label, day in (("first", first_day), ("middle", middle_day), ("latest", latest)):
                syms = list(D.list_instruments(instruments=insts, start_time=str(day), end_time=str(day), as_list=True))
                counts[label] = len(syms)
                if label == "latest":
                    sample = syms[:5]
            uni_counts[uni] = {
                "n_at_first": counts["first"],
                "n_at_middle": counts["middle"],
                "n_at_latest": counts["latest"],
                "n_changes_over_time": len(set(counts.values())) > 1,
                "sample": sample,
            }
        except Exception as exc:  # noqa: BLE001
            uni_counts[uni] = {"error": f"{type(exc).__name__}: {exc}"}
    report["universe_counts"] = uni_counts

    # 最近 5 个交易日 csi300 各有多少标的 —— 确认最后一天不是空/半空截面
    csi300_last5: dict[str, object] = {}
    try:
        insts_csi300 = D.instruments(market="csi300")  # qlib 0.9.8: instruments() 无 freq 参数
        for day in cal[-5:]:
            day_key = str(pd.Timestamp(day).date())
            try:
                syms = list(D.list_instruments(
                    instruments=insts_csi300, start_time=str(day), end_time=str(day), as_list=True
                ))
                csi300_last5[day_key] = len(syms)
            except Exception as exc:  # noqa: BLE001
                csi300_last5[day_key] = {"error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        csi300_last5 = {"error": f"{type(exc).__name__}: {exc}"}
    report["csi300_last_5_days"] = csi300_last5

    # 真实取一次特征，核对列数与 NaN 情况（覆盖 §12.4 的断言要求）
    # 🔴 D.features 的参数顺序是 (instruments, fields, ...) —— 传反了会把 dict 的键
    #    当成表达式（["market", "filter_pipe"]）⇒ ExpressionSyntaxError: Unsupported syntax: Name
    probe_ok = True
    try:
        insts = D.instruments(market="csi300")  # qlib 0.9.8: instruments() 无 freq 参数
        probe_start_idx = max(len(cal) - PROBE_WINDOW_DAYS, 0)
        probe_start = cal[probe_start_idx]
        probe_days = len(cal) - probe_start_idx
        df = D.features(
            insts,
            PROBE_EXPRESSIONS,
            start_time=str(probe_start),
            end_time=str(latest),
            freq="day",
        )
        df = df.sort_index()
        # 预热区 = 每只票在窗口内的前 WARMUP_DAYS 行（rolling 窗口未填满 ⇒ 必 NaN）
        # 用 numpy 布尔掩码（不是布尔 Series），避免 MultiIndex 重复标签 reindex 报错
        seq_pos = df.groupby(level="instrument").cumcount().to_numpy()
        warm_mask = seq_pos < WARMUP_DAYS
        post = df.loc[~warm_mask]
        raw_nan = {c: float(df[c].isna().mean()) for c in df.columns}
        post_nan = {c: float(post[c].isna().mean()) for c in post.columns}
        report["feature_probe"] = {
            "ok": True,
            "expressions": list(PROBE_EXPRESSIONS),
            "window": {
                "start": str(pd.Timestamp(probe_start).date()),
                "end": str(pd.Timestamp(latest).date()),
                "n_trading_days": int(probe_days),
                "min_required_days": PROBE_WINDOW_DAYS,
                "sufficient": bool(probe_days >= PROBE_WINDOW_DAYS),
            },
            "warmup_days": WARMUP_DAYS,
            "n_rows": int(df.shape[0]),
            "n_rows_after_warmup": int(post.shape[0]),
            "n_cols": int(df.shape[1]),
            "columns": list(df.columns),
            "all_nan_cols": [c for c in df.columns if df[c].isna().all()],
            "nan_ratio_raw": raw_nan,
            "nan_ratio_after_warmup": post_nan,
            "n_instruments_in_probe": int(df.index.get_level_values("instrument").nunique()),
        }
        print(f"[probe] 窗口 {report['feature_probe']['window']['start']} ~ "
              f"{report['feature_probe']['window']['end']}（{probe_days} 个交易日，"
              f"要求 ≥ {PROBE_WINDOW_DAYS}），标的 "
              f"{report['feature_probe']['n_instruments_in_probe']} 只", file=sys.stderr)
        for c in df.columns:
            print(f"[probe] {c:<18} 原始 NaN 率 {raw_nan[c]:.4f} | "
                  f"剔除预热区({WARMUP_DAYS} 个交易日)后 NaN 率 {post_nan.get(c, float('nan')):.4f}",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        # 🔴 探查失败必须让体检失败（返回非 0），不能把异常吞进 JSON 还 return 0
        probe_ok = False
        report["feature_probe"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"❌ 特征探针失败: {type(exc).__name__}: {exc}", file=sys.stderr)

    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
        print(f"\n[inspect] 报告已写入 {args.out}", file=sys.stderr)

    # 🔴 体检失败 ⇒ 非 0 退出码（报告仍然落盘，便于排查）
    if missing_ops:
        print(f"❌ 体检失败：qlib 表达式算子缺失 {missing_ops}", file=sys.stderr)
        return 3
    if not probe_ok:
        print("❌ 体检失败：特征探针未通过", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
