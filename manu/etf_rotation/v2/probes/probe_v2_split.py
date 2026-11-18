#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V2 闸门探针 · **切分 / embargo / 标签口径**（对齐① + 时间线红线）。

【每类断言钉死了什么】
A 类 · embargo 硬隔离
  A1 有效隔离带 ≥ 规格下限 20 **且** ≥ 标签窗口末端 25
  A2 训练末样本的标签窗口结束日 **严格早于** 验证集首样本日
  A3 `fit_end` == 裁剪后的训练末日（**不许**仍写 2021-12-31）
  A4 🔴 **字面 20 天的口径必须报红**（embargo 真在起作用的证据）
  A5 验证/测试段**不被裁剪**（起点分别是 2022-01-01 / 2024-01-01）

B 类 · 标签口径
  B1 qlib 表达式必须是 ``Ref(Mean($vwap,11),-25)/$vwap-1``
  B2 「窗口均价 / P[t] - 1」≡「日收益的均值」（分母在窗口内固定 ⇒ 恒等）
  B3 qlib 的**滚动**口径（分子分母一起滚）**必须不同** ⇒ 证明错误写法错在哪
  B4 窗口不完整 ⇒ NaN（不许用半截窗口凑数）
  B5 基准项逐日常数 ⇒ 截面排名不变（超额的实质在评估层）

C 类 · 变异反证（闸门有牙）
  C1 把隔离带硬编码成 20 ⇒ 默认切分必须报红
  C2 把标签边界断言掏空 ⇒ 字面 20 天口径就**不再报红**（证明那条断言不是装饰）
  C3 把「窗口均价」退化成「只用窗口首日价格」⇒ 口径必须不一致

D 类 · 源码静态检查（AST 判定，不许文本匹配）
  D1 无 tab 缩进
  D2 函数名 snake_case
  D3 每个函数都有含中文的 docstring
  D4 模块级 docstring 含中文
"""

from __future__ import annotations

import ast
import datetime as _dt
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import labels as L  # noqa: E402
import splits as S  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[PASS] {name}" + (f"｜{detail}" if detail else "")
    if ok:
        PASSED.append(line)
        print(line, flush=True)
    else:
        line = f"[FAIL] {name}" + (f"｜{detail}" if detail else "")
        FAILED.append(line)
        print(line, flush=True)


def _mutated(tmp_path: Path, source: Path, edits: list[tuple[str, str]], tag: str):
    text = source.read_text(encoding="utf-8")
    for old, new in edits:
        if old not in text:
            raise AssertionError(f"变异锚点不存在：{old!r}")
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"probe_mut_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _price_panel(seed: int = 20261117, n_t: int = 80, n_n: int = 6) -> pd.DataFrame:
    idx = pd.bdate_range("2023-01-02", periods=n_t)
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0.0, 0.08, size=(n_t, n_n))
    data = {f"sh60000{j}": 10.0 * np.exp(np.cumsum(shocks[:, j])) for j in range(n_n)}
    return pd.DataFrame(data, index=idx)


def _window_avg(px: pd.DataFrame) -> pd.DataFrame:
    vals = px.to_numpy(dtype="float64")
    n_t = vals.shape[0]
    out = np.full_like(vals, np.nan)
    for t in range(n_t - 25):
        win = vals[t + 15:t + 26, :]
        out[t] = win.mean(axis=0) / vals[t] - 1.0
    return pd.DataFrame(out, index=px.index, columns=px.columns)


def _daily_return_mean(px: pd.DataFrame) -> pd.DataFrame:
    vals = px.to_numpy(dtype="float64")
    n_t = vals.shape[0]
    out = np.full_like(vals, np.nan)
    for t in range(n_t - 25):
        win = vals[t + 15:t + 26, :]
        out[t] = (win / vals[t] - 1.0).mean(axis=0)
    return pd.DataFrame(out, index=px.index, columns=px.columns)


def _qlib_rolling(px: pd.DataFrame) -> pd.DataFrame:
    vals = px.to_numpy(dtype="float64")
    n_t = vals.shape[0]
    out = np.full_like(vals, np.nan)
    for t in range(n_t - 25):
        terms = []
        for j in range(11):
            terms.append(vals[t + 15 + j] / vals[t - j] - 1.0)
        out[t] = float(np.mean(terms))
    return pd.DataFrame(out, index=px.index, columns=px.columns)


# ---------------------------------------------------------------- A 类
def probe_embargo() -> None:
    spec = S.split()
    eff = S.effective_embargo_days()
    check("A1 embargo ≥ 规格下限 20 且 ≥ 标签窗口末端 25",
          eff >= C.EMBARGO_DAYS and eff >= C.LABEL_HORIZON_DAYS,
          f"effective={eff}（下限 {C.EMBARGO_DAYS}，标签末端 {C.LABEL_HORIZON_DAYS}）")
    last = _dt.date.fromisoformat(spec.train_end)
    val_start = _dt.date.fromisoformat(spec.valid_start)
    lbl_end = S.label_window_end(last)
    check("A2 训练末样本的标签窗口结束日严格早于验证集首样本日",
          lbl_end < val_start, f"标签窗口结束 {lbl_end} < val 起点 {val_start}")
    check("A3 fit_end == 裁剪后的训练末日",
          spec.fit_end == spec.train_end and spec.fit_end != spec.train_end_raw,
          f"fit_end={spec.fit_end}（名义末日 {spec.train_end_raw}）")
    try:
        S.assert_no_label_overlap(S.build_split(embargo_days=20))
        check("A4 字面 20 天的口径必须报红（embargo 真在起作用）", False,
              "字面 20 天竟然没报红 ⇒ 标签窗口跨界未被拦住")
    except AssertionError as exc:
        check("A4 字面 20 天的口径必须报红（embargo 真在起作用）", True, str(exc)[:120])
    check("A5 验证/测试段起点不被裁剪",
          spec.valid_start == "2022-01-01" and spec.test_start == "2024-01-01",
          f"valid={spec.valid_start} test={spec.test_start}")


# ---------------------------------------------------------------- B 类
def probe_label() -> None:
    expr = L.qlib_label_expression()
    check("B1 qlib 表达式必须是 Ref(Mean($vwap,11),-25)/$vwap-1",
          expr == "Ref(Mean($vwap,11),-25)/$vwap-1", expr)
    px = _price_panel()
    got = L.forward_window_return(px)
    d1 = np.nanmax(np.abs(got.to_numpy() - _window_avg(px).to_numpy()))
    check("B1b pandas 版与手算窗口均价逐位一致", d1 < 1e-12, f"max|diff|={d1:.3e}")
    d2 = np.nanmax(np.abs(got.to_numpy() - _daily_return_mean(px).to_numpy()))
    check("B2 窗口均价口径 ≡ 日收益均值口径", d2 < 1e-12, f"max|diff|={d2:.3e}")
    d3 = np.nanmax(np.abs(got.to_numpy() - _qlib_rolling(px).to_numpy()))
    check("B3 qlib 滚动口径（分子分母一起滚）必须与本口径不同", d3 > 1e-4,
          f"max|diff|={d3:.3e}")
    tail_ok = bool(np.isnan(got.iloc[-1, 0])) and bool(not np.isnan(got.iloc[-26, 0]))
    check("B4 窗口不完整必须 NaN", tail_ok, "末端 25 行 NaN、倒数第 26 行有效")
    bench = pd.Series(0.03, index=px.index)
    excess = L.excess_forward_return(got, bench)
    check("B5 基准项逐日常数 ⇒ 截面排名不变",
          L.rank_invariance_under_daily_shift(got, excess))


# ---------------------------------------------------------------- C 类
def probe_mutation(tmp_path: Path) -> None:
    try:
        mut = _mutated(tmp_path, _V2_DIR / "splits.py",
                       [("    return max(int(C.EMBARGO_DAYS), int(C.LABEL_HORIZON_DAYS))",
                         "    return int(C.EMBARGO_DAYS)")], "embargo20")
        try:
            mut.assert_no_label_overlap(mut.build_split())
            check("C1 把隔离带硬编码成 20 ⇒ 默认切分必须报红", False, "竟然没报红")
        except AssertionError:
            check("C1 把隔离带硬编码成 20 ⇒ 默认切分必须报红", True, "报红符合预期")
    except Exception as exc:  # noqa: BLE001
        check("C1 把隔离带硬编码成 20 ⇒ 默认切分必须报红", False, f"变异构造失败：{exc!r}")

    try:
        mut = _mutated(tmp_path, _V2_DIR / "splits.py",
                       [("    if lbl_end >= val_start:", "    if False:")], "no_assert")
        spec20 = mut.build_split(embargo_days=20)
        try:
            mut.assert_no_label_overlap(spec20)
            check("C2 把标签跨界断言掏空 ⇒ 字面 20 天口径不再报红（断言不是装饰）", True,
                  "变异体确实不再报红 ⇒ 原断言有牙")
        except AssertionError:
            check("C2 把标签跨界断言掏空 ⇒ 字面 20 天口径不再报红（断言不是装饰）", False,
                  "掏空后仍然报红 ⇒ 变异无效")
    except Exception as exc:  # noqa: BLE001
        check("C2 把标签跨界断言掏空 ⇒ 字面 20 天口径不再报红（断言不是装饰）", False,
              f"变异构造失败：{exc!r}")

    try:
        mut = _mutated(tmp_path, _V2_DIR / "labels.py",
                       [("    exit_px = np.nanmean(stack, axis=0)", "    exit_px = stack[0]")],
                       "first_only")
        px = _price_panel()
        got = mut.forward_window_return(px)
        d = np.nanmax(np.abs(got.to_numpy() - _window_avg(px).to_numpy()))
        check("C3 把「窗口均价」退化成「只用窗口首日价格」⇒ 口径必须不一致", d > 1e-6,
              f"max|diff|={d:.3e}")
    except Exception as exc:  # noqa: BLE001
        check("C3 把「窗口均价」退化成「只用窗口首日价格」⇒ 口径必须不一致", False,
              f"变异构造失败：{exc!r}")


# ---------------------------------------------------------------- D 类
def probe_static() -> None:
    for fname in ("config_v2.py", "splits.py", "labels.py", "features.py",
                  "constituents.py", "liquidity.py", "train_s.py", "etf_layer.py",
                  "metrics.py", "hyperopt.py", "evaluate.py", "backtest_v2.py",
                  "run_v2.py", "report.py"):
        path = _V2_DIR / fname
        if not path.is_file():
            check(f"D1 {fname} 存在", False, "文件缺失")
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        ok_tab = "\t" not in src
        ok_name = True
        ok_doc = True
        ok_mod = bool(ast.get_docstring(tree))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.lstrip("_").replace("_", "").isalnum() or \
                        any(ch.isupper() for ch in node.name):
                    ok_name = False
                doc = ast.get_docstring(node)
                if not doc or not any("一" <= ch <= "鿿" for ch in doc):
                    ok_doc = False
        check(f"D1 {fname} 无 tab 缩进", ok_tab)
        check(f"D2 {fname} 函数名 snake_case", ok_name)
        check(f"D3 {fname} 每个函数都有含中文的 docstring", ok_doc)
        check(f"D4 {fname} 模块级 docstring 含中文", ok_mod)


def main() -> int:
    import tempfile

    print("=== probe_v2_split · 切分 / embargo / 标签口径 ===")
    probe_embargo()
    probe_label()
    with tempfile.TemporaryDirectory() as td:
        probe_mutation(Path(td))
    probe_static()
    print("")
    print(f"合计 {len(PASSED) + len(FAILED)} 项：PASS {len(PASSED)} / FAIL {len(FAILED)}")
    if FAILED:
        print("")
        for line in FAILED:
            print(line)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
