#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V2 闸门探针 · **时间线红线**（超参只在 val + 单盲只跑一次）。

【每类断言钉死了什么】
A 类 · 超参搜索只许在 val 段
  A1 val 段窗口必须**放行**
  A2 触碰测试段（end ≥ 2024-01-01）必须**被拒**
  A3 越出 val 段（起点早于 2022-01-01 / 终点晚于 2023-12-31）必须**被拒**
  A4 误用训练段（2016–2021）来选参必须**被拒**
  A5 窗口倒挂（start > end）必须**被拒**

B 类 · 单盲只跑一次（fail-closed）
  B1 首次跑成功并**写出** token
  B2 第二次跑必须抛 ``SingleBlindViolation``（**不许**静默重跑）
  B3 ``allow_rerun=True`` 但**没写理由** ⇒ 必须抛错（**在跑之前**就拒）
  B4 token 必须**只读**（0444）⇒ 物理上不易被悄悄改掉
  B5 token 里必须留有**指纹**（哪一版参数跑出来的）

C 类 · 变异反证（闸门有牙）
  C1 把测试段起点挪到 2024 之后 ⇒ 「2024 窗口被放行」⇒ 原闸门有牙
  C2 把 ``allow_rerun`` 的理由校验去掉 ⇒ ``allow_rerun=True`` 不写理由就会被放行
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import evaluate as EV  # noqa: E402
import hyperopt as HO  # noqa: E402

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


# ---------------------------------------------------------------- A 类
def probe_val_only() -> None:
    try:
        HO.assert_val_only(C.VALID[0], C.VALID[1])
        check("A1 val 段窗口必须放行", True, f"{C.VALID[0]} ~ {C.VALID[1]}")
    except Exception as exc:  # noqa: BLE001
        check("A1 val 段窗口必须放行", False, repr(exc))

    for label, window in (
        ("A2 触碰测试段必须被拒", ("2024-01-01", "2024-12-31")),
        ("A3a 起点早于 val 必须被拒", ("2021-06-01", "2022-06-30")),
        ("A3b 终点晚于 val 必须被拒", ("2022-06-01", "2024-06-30")),
        ("A4 误用训练段选参必须被拒", ("2016-01-01", "2021-12-31")),
        ("A5 窗口倒挂必须被拒", ("2023-12-31", "2022-01-01")),
    ):
        try:
            HO.assert_val_only(*window)
            check(label, False, f"{window} 竟然被放行")
        except ValueError as exc:
            check(label, True, str(exc)[:110])


# ---------------------------------------------------------------- A2 类（2026-11-17 21:20 审计新增）
def probe_metric_window() -> None:
    """🔴 **A2 类：超参搜索的「指标窗口」必须落在 val 段**。

    🔴 **为什么要有这一类**（2026-11-17 21:20 独立审计）：A 类的 ``assert_val_only``
    只校验**窗口标签**，管不住 objective 实际用哪段数据算指标。旧实现把**整段历史净值**
    （2020-01-02 ~ 2026-11-10，含完整测试段）交给 ``metrics.summarize()``
    ⇒ 年化收益 / 年化换手**混进测试段已实现收益** ⇒ 超参其实是在「看过测试段」的情况下
    选出来的 ⇒ 红线「超参搜索只许在 val 段做」被实质违反，且下游的「单盲」被掏空。

    ⇒ 现在有三道闸：
      ① ``run_v2.val_window_metrics`` 遇到非 val 窗口**直接抛错**；
      ② val 窗口内的指标与「全史指标」**必然不同**（证明确实切了窗）；
      ③ **变异反证**：把①的守卫改坏 ⇒ ②/① 必须能检出（证明断言有牙）。
    """
    import numpy as np
    import pandas as pd

    import metrics as ME
    import run_v2 as RV

    # ---- ① 非 val 窗口必须被拒 ----
    for label, window in (
        ("A6a 指标窗口触碰测试段必须被拒", ("2024-01-01", "2026-11-03")),
        ("A6b 指标窗口起点早于 val 必须被拒", ("2021-06-01", "2022-06-30")),
        ("A6c 指标窗口终点晚于 val 必须被拒", ("2022-06-01", "2024-06-30")),
    ):
        try:
            RV.val_window_metrics(None, None, None, window=window)
            check(label, False, f"{window} 竟然被放行")
        except RuntimeError as exc:
            check(label, True, str(exc)[:110])

    # ---- ② val 窗口指标必须与全史指标不同 ----
    idx = pd.bdate_range("2020-01-01", periods=800)
    nav = pd.Series(1.0 + np.linspace(0.0, 0.8, len(idx)), index=idx, name="nav")
    bench = pd.Series(1.0 + np.linspace(0.0, 0.2, len(idx)), index=idx, name="bench")
    try:
        m_val = RV.val_window_metrics(nav, None, bench)
        m_all = ME.summarize(nav, bench, None, commission=0.0, slippage=0.0, periods_per_year=250)
        diff = abs(float(m_val["annualized_return"]) - float(m_all["annualized_return"]))
        check("A7 指标确实只算 val 段（与全史口径必然不同）", diff > 1e-6,
              f"val={float(m_val['annualized_return']):.6f} vs 全史={float(m_all['annualized_return']):.6f}")
    except Exception as exc:  # noqa: BLE001
        check("A7 指标确实只算 val 段（与全史口径必然不同）", False, repr(exc))

    # ---- ③ 变异反证：守卫改坏 ⇒ 必须能被检出 ----
    with tempfile.TemporaryDirectory() as td:
        try:
            mut = _mutated(
                Path(td), _V2_DIR / "run_v2.py",
                [("    if got != expect:\n        raise RuntimeError(",
                  "    if False:\n        raise RuntimeError(")],
                "no_window_guard",
            )
            raised = False
            try:
                # 用**有效**的 nav/bench 调用 ⇒ 变异体若没有守卫就应当**正常返回**（不抛错）
                mut.val_window_metrics(nav, None, bench, window=("2024-01-01", "2026-11-03"))
            except RuntimeError:
                raised = True
            check("A8 指标窗口守卫有牙（改坏后不再抛错 ⇒ 变异生效）", not raised,
                  "🔴 变异后仍然抛错 ⇒ 守卫不在这条路径上")
        except AssertionError as exc:
            check("A8 指标窗口守卫有牙（改坏后不再抛错 ⇒ 变异生效）", False,
                  f"变异构造失败：{exc}")


# ---------------------------------------------------------------- B 类
def probe_single_blind() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            return {"ok": True, "value": 42}

        try:
            EV.run_single_blind(root, "test_probe", _fn, fingerprint="fp-1")
            check("B1 首次跑成功并写出 token", calls["n"] == 1 and
                  EV.single_blind_token_path(root, "test_probe").is_file(),
                  f"fn 调用 {calls['n']} 次")
        except Exception as exc:  # noqa: BLE001
            check("B1 首次跑成功并写出 token", False, repr(exc))
            return

        try:
            EV.run_single_blind(root, "test_probe", _fn, fingerprint="fp-2")
            check("B2 第二次跑必须抛 SingleBlindViolation", False, "竟然没抛错")
        except EV.SingleBlindViolation as exc:
            check("B2 第二次跑必须抛 SingleBlindViolation", True, str(exc)[:110])
        except Exception as exc:  # noqa: BLE001
            check("B2 第二次跑必须抛 SingleBlindViolation", False, repr(exc))

        try:
            EV.run_single_blind(root, "test_probe2", _fn, allow_rerun=True)
            check("B3 allow_rerun=True 不写理由必须抛错", False, "竟然没抛错")
        except ValueError as exc:
            check("B3 allow_rerun=True 不写理由必须抛错", True, str(exc)[:110])
        except Exception as exc:  # noqa: BLE001
            check("B3 allow_rerun=True 不写理由必须抛错", False, repr(exc))

        path = EV.single_blind_token_path(root, "test_probe")
        mode = stat.S_IMODE(path.stat().st_mode)
        check("B4 token 必须只读（0444）", mode == 0o444, f"mode={oct(mode)}")
        record = json.loads(path.read_text(encoding="utf-8"))
        check("B5 token 必须留有参数指纹", bool(record.get("fingerprint")),
              f"fingerprint={record.get('fingerprint')}")


# ---------------------------------------------------------------- C 类
def probe_mutation(tmp_path: Path) -> None:
    # 🔴 差分变异（两步）：把 VAL_WINDOW 放宽到含 2024，**只**保留「测试段检查」能拒它；
    #    再把 TEST_WINDOW 挪到 2099 ⇒ 同一窗口必须被放行
    #    ⇒ 证明上一步的拒绝**确实来自测试段检查**（而不是别的原因）。
    widen_val = [('VAL_WINDOW: tuple[str, str] = ("2022-01-01", "2023-12-31")',
                  'VAL_WINDOW: tuple[str, str] = ("2022-01-01", "2026-12-31")')]
    move_test = [('TEST_WINDOW: tuple[str, str] = ("2024-01-01", "2026-11-03")',
                  'TEST_WINDOW: tuple[str, str] = ("2099-01-01", "2099-12-31")')]
    try:
        mut1 = _mutated(tmp_path, _V2_DIR / "hyperopt.py", widen_val, "wide_val")
        try:
            mut1.assert_val_only("2024-01-01", "2024-12-31")
            rejected_by_test = False
            why = "放宽 val 窗后 2024 窗口被放行 ⇒ 测试段检查没生效"
        except ValueError as exc:
            rejected_by_test = True
            why = f"放宽 val 窗后仍被拒（{str(exc)[:60]}）⇒ 拒绝来自测试段检查"
        mut2 = _mutated(tmp_path, _V2_DIR / "hyperopt.py", widen_val + move_test, "both")
        try:
            mut2.assert_val_only("2024-01-01", "2024-12-31")
            released = True
        except ValueError:
            released = False
        check("C1 测试段检查有牙（放宽 val 窗后仍拒 2024；再挪走 test 窗才放行）",
              rejected_by_test and released,
              f"拒={rejected_by_test}（{why}）／放={released}")
    except Exception as exc:  # noqa: BLE001
        check("C1 测试段检查有牙（放宽 val 窗后仍拒 2024；再挪走 test 窗才放行）", False,
              f"变异构造失败：{exc!r}")

    try:
        mut = _mutated(tmp_path, _V2_DIR / "evaluate.py",
                       [('    if allow_rerun and not str(reason).strip():', "    if False:")],
                       "no_reason_check")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mut.run_single_blind(root, "probe_x", lambda: {"ok": 1}, fingerprint="f")
            try:
                mut.run_single_blind(root, "probe_x", lambda: {"ok": 2},
                                     allow_rerun=True, fingerprint="f")
                check("C2 去掉理由校验 ⇒ allow_rerun 不写理由就会被放行", True,
                      "变异体确实放行 ⇒ 原校验有牙")
            except Exception:  # noqa: BLE001
                check("C2 去掉理由校验 ⇒ allow_rerun 不写理由就会被放行", False,
                      "变异体仍然报错 ⇒ 变异无效")
    except Exception as exc:  # noqa: BLE001
        check("C2 去掉理由校验 ⇒ allow_rerun 不写理由就会被放行", False,
              f"变异构造失败：{exc!r}")


def main() -> int:
    print("=== probe_v2_redline · 超参只在 val + 单盲只跑一次 ===")
    probe_val_only()
    probe_metric_window()
    probe_single_blind()
    with tempfile.TemporaryDirectory() as td:
        probe_mutation(Path(td))
    print("")
    print(f"合计 {len(PASSED) + len(FAILED)} 项：PASS {len(PASSED)} / FAIL {len(FAILED)}")
    if FAILED:
        print("")
        for line in FAILED:
            print(line)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
