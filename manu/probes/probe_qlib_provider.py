#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""qlib provider **单一事实源**闸门探针。

【用途】
把「两条链（研究链 / 每日链）挑 qlib 数据集的规则」固化成**可执行的闸门**：
provider 路径的唯一性、**谁新用谁**的选择规则、fail-safe 退回、
每日链的**委托**（不再自带一份规则）、以及**变异反证**（闸门有牙）。

【为什么要有这个探针（2026-11-09 实测）】
* 研究链 `qlib_scores.py` 默认吃上游基座 `cn_data`（末日 2026-11-03），
  每日链 `daily/run_qlib_score.py::select_provider()` 却**自带一份**「谁新用谁」规则
  ⇒ 两条链**各写各的** ⇒ 12-01 重建时研究链的穿透打分会**静默停在 11-03 的旧分数**；
* ⇒ 规则已提到研究链层 `manu/etf_rotation/qlib_provider.py`（单一事实源），
  每日链改为**委托**；本探针锁住这个结构（**不许退回两份实现**）。

【纪律】
* **纯函数 + 临时目录夹具**：不联网、不读生产数据集、不写任何生产目录；
* **只用标准库**（pandas 用空模块桩，只为 import 每日链的纯函数）；
* 断言必须带 `[PASS]` / `[FAIL]` 字面量（`run_gates.sh` 按此计数）。

【用法】
    python manu/etf_rotation/probes/probe_qlib_provider.py
    （退出码 0 = 全过，1 = 有 FAIL，2 = 环境错）

【断言清单】
P1  基座目录默认 = 个股线 config 的 `PROVIDER_URI`（单一事实源）
P2  派生目录默认 = <dataset_root>/cn_data_live
P3  env `ETF_ROTATION_QLIB_CN_DATA_LIVE` 覆盖派生目录
P4  env `ETF_ROTATION_QLIB_DATASET_ROOT` 覆盖 ⇒ 派生目录跟着变
P5  env `ETF_ROTATION_QLIB_PROVIDER_URI` ⇒ `provider_uri_override()` 生效
P6  env 全清 ⇒ 无 provider 覆盖（默认基座 ⇒ 零回归）
P7  只有基座（无派生目录）⇒ 选基座
P8  派生末日 **>** 基座 ⇒ **选派生**（谁新用谁）
P9  派生末日 **==** 基座 ⇒ 选基座（不比基座新）
P10 派生末日 **<** 基座 ⇒ 选基座
P11 派生日历空文件 ⇒ 选基座（fail-safe，不猜）
P12 派生目录不存在 ⇒ 选基座，reason 含「派生目录不存在」
P13 基座日历缺失 + 派生可读 ⇒ 选派生
P14 两边都读不到 ⇒ 选基座（退回默认）
P15 asof 未被派生覆盖 ⇒ reason 含「未覆盖」告警，但**仍选派生**（不换源）
P16 asof 被派生覆盖 ⇒ reason 不含「未覆盖」
P17 `calendar_max_date` 对缺失/空日历 ⇒ `None`（**不编数字**）
P18 每日链 `daily_config.cross_check_provider_paths()` ⇒ ok（无第二个字面量）
P19 AST：每日链 `select_provider` **委托**共享模块（不再自带规则）
P20 运行时等价：每日链 `select_provider` 与共享模块**逐字相同**
M1  变异反证：「永远用基座」在 P8 夹具上**必须失败** ⇒ 闸门有牙
M2  变异反证：「派生不比基座新也用派生」在 P9/P10 夹具上**必须失败** ⇒ 闸门有牙
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
while not (REPO_ROOT / "manu" / "etf_rotation" / "qlib_provider.py").is_file():
    if REPO_ROOT.parent == REPO_ROOT:
        break
    REPO_ROOT = REPO_ROOT.parent

RESEARCH_DIR = REPO_ROOT / "manu" / "etf_rotation"
DAILY_DIR = RESEARCH_DIR / "daily"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    """打一条断言结果（🔴 必须带 `[PASS]` / `[FAIL]` 字面量 ⇒ 会被闸门计数）。"""
    (PASS if ok else FAIL).append(name)
    mark = "[PASS]" if ok else "[FAIL]"
    emoji = "✅" if ok else "❌"
    print(f"  {mark} {emoji} {name}" + (f" —— {detail}" if detail else ""))


def _load(name: str, path: Path):
    """按路径加载模块（研究链 / 每日链都不是包）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _make_provider(root: Path, name: str, cal: list[str] | None) -> Path:
    """造一个 provider 目录（只有 `calendars/day.txt`；`cal=None` ⇒ 不建日历文件）。"""
    provider = root / name
    if cal is not None:
        (provider / "calendars").mkdir(parents=True, exist_ok=True)
        (provider / "calendars" / "day.txt").write_text(
            ("\n".join(cal) + "\n") if cal else "", encoding="utf-8"
        )
    provider.mkdir(parents=True, exist_ok=True)
    return provider


# ---------------------------------------------------------------- 变异体（反证用）
def _mutant_always_base(base: Path, live: Path, asof: str | None):
    """变异体 1：**永远用基座**（把「谁新用谁」反向）。"""
    return base, "变异体：永远用基座"


def _mutant_always_live(base: Path, live: Path, asof: str | None):
    """变异体 2：派生目录只要**存在**就用（不比基座新也用）⇒ 会误用过期派生。"""
    return live, "变异体：只要派生存在就用"


def main() -> int:
    qp = _load("manu_probe_qlib_provider", RESEARCH_DIR / "qlib_provider.py")

    # ---------------- P1~P6：路径与单一事实源 ----------------
    stock_cfg = _load(
        "manu_probe_stock_config",
        REPO_ROOT / "manu" / "experiments" / "ashare_topk_selection" / "config.py",
    )
    check(
        "P1 基座目录默认 = 个股线 config 的 PROVIDER_URI（单一事实源）",
        str(qp.base_dir()) == str(stock_cfg.PROVIDER_URI),
        f"{qp.base_dir()} vs {stock_cfg.PROVIDER_URI}",
    )
    check(
        "P2 派生目录默认 = <dataset_root>/cn_data_live",
        str(qp.live_dir()).endswith("/cn_data_live"),
        str(qp.live_dir()),
    )

    env_keys = [
        qp.ENV_CN_DATA_LIVE,
        qp.ENV_DATASET_ROOT,
        qp.ENV_PROVIDER_URI,
    ]
    saved = {key: os.environ.get(key) for key in env_keys}
    try:
        os.environ[qp.ENV_CN_DATA_LIVE] = "/tmp/probe_live_override"
        check(
            "P3 env ETF_ROTATION_QLIB_CN_DATA_LIVE 覆盖派生目录",
            str(qp.live_dir()) == "/tmp/probe_live_override",
            str(qp.live_dir()),
        )
        os.environ.pop(qp.ENV_CN_DATA_LIVE, None)
        os.environ[qp.ENV_DATASET_ROOT] = "/tmp/probe_root_override"
        check(
            "P4 env ETF_ROTATION_QLIB_DATASET_ROOT 覆盖 ⇒ 派生目录跟着变",
            str(qp.live_dir()) == "/tmp/probe_root_override/cn_data_live",
            str(qp.live_dir()),
        )
        os.environ.pop(qp.ENV_DATASET_ROOT, None)
        os.environ[qp.ENV_PROVIDER_URI] = "/tmp/probe_provider_override"
        check(
            "P5 env ETF_ROTATION_QLIB_PROVIDER_URI ⇒ provider_uri_override() 生效",
            str(qp.provider_uri_override()) == "/tmp/probe_provider_override",
            str(qp.provider_uri_override()),
        )
        for key in env_keys:
            os.environ.pop(key, None)
        check(
            "P6 env 全清 ⇒ 无 provider 覆盖（默认基座 ⇒ 零回归）",
            qp.provider_uri_override() is None,
            str(qp.provider_uri_override()),
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---------------- P7~P17：选择规则（临时目录夹具）----------------
    tmp = Path(tempfile.mkdtemp(prefix="probe_qlib_provider_"))
    try:
        base = _make_provider(tmp, "base", ["2026-11-02", "2026-11-03"])
        live_newer = _make_provider(tmp, "live_newer", ["2026-11-02", "2026-11-03", "2026-11-05"])
        live_equal = _make_provider(tmp, "live_equal", ["2026-11-02", "2026-11-03"])
        live_older = _make_provider(tmp, "live_older", ["2026-11-02"])
        live_empty = _make_provider(tmp, "live_empty", [])
        live_absent = tmp / "live_absent"
        base_no_cal = _make_provider(tmp, "base_no_cal", None)

        picked, reason = qp.select_provider(None, base=base, live=live_absent)
        check(
            "P7 只有基座（无派生目录）⇒ 选基座",
            picked == base and "基座" in reason,
            f"{picked.name} / {reason}",
        )

        picked, reason = qp.select_provider(None, base=base, live=live_newer)
        check(
            "P8 派生末日 > 基座 ⇒ 选派生（谁新用谁）",
            picked == live_newer and "派生目录" in reason,
            f"{picked.name} / {reason}",
        )

        picked, _ = qp.select_provider(None, base=base, live=live_equal)
        check(
            "P9 派生末日 == 基座 ⇒ 选基座（不比基座新）",
            picked == base,
            f"{picked.name}",
        )

        picked, _ = qp.select_provider(None, base=base, live=live_older)
        check(
            "P10 派生末日 < 基座 ⇒ 选基座",
            picked == base,
            f"{picked.name}",
        )

        picked, _ = qp.select_provider(None, base=base, live=live_empty)
        check(
            "P11 派生日历空文件 ⇒ 选基座（fail-safe，不猜）",
            picked == base,
            f"{picked.name}",
        )

        picked, reason = qp.select_provider(None, base=base, live=live_absent)
        check(
            "P12 派生目录不存在 ⇒ 选基座且 reason 含「派生目录不存在」",
            picked == base and "派生目录不存在" in reason,
            f"{picked.name} / {reason}",
        )

        picked, _ = qp.select_provider(None, base=base_no_cal, live=live_newer)
        check(
            "P13 基座日历缺失 + 派生可读 ⇒ 选派生",
            picked == live_newer,
            f"{picked.name}",
        )

        picked, _ = qp.select_provider(None, base=base_no_cal, live=live_empty)
        check(
            "P14 两边都读不到 ⇒ 选基座（退回默认）",
            picked == base_no_cal,
            f"{picked.name}",
        )

        picked, reason = qp.select_provider("2026-12-01", base=base, live=live_newer)
        check(
            "P15 asof 未被派生覆盖 ⇒ reason 含「未覆盖」但仍选派生（不换源）",
            picked == live_newer and "未覆盖" in reason,
            f"{picked.name} / {reason}",
        )

        picked, reason = qp.select_provider("2026-11-05", base=base, live=live_newer)
        check(
            "P16 asof 被派生覆盖 ⇒ reason 不含「未覆盖」",
            picked == live_newer and "未覆盖" not in reason,
            f"{picked.name} / {reason}",
        )

        check(
            "P17 calendar_max_date 对缺失/空日历 ⇒ None（不编数字）",
            qp.calendar_max_date(tmp / "nope") is None
            and qp.calendar_max_date(live_empty) is None,
            f"missing={qp.calendar_max_date(tmp / 'nope')} empty={qp.calendar_max_date(live_empty)}",
        )

        # ---------------- M1 / M2：变异反证（闸门有牙）----------------
        correct_newer, _ = qp.select_provider(None, base=base, live=live_newer)
        mutant_newer, _ = _mutant_always_base(base, live_newer, None)
        check(
            "M1 变异反证：「永远用基座」在 P8 夹具上必须失败",
            correct_newer != mutant_newer,
            f"正确 {correct_newer.name} vs 变异 {mutant_newer.name}",
        )

        correct_equal, _ = qp.select_provider(None, base=base, live=live_equal)
        mutant_equal, _ = _mutant_always_live(base, live_equal, None)
        correct_older, _ = qp.select_provider(None, base=base, live=live_older)
        mutant_older, _ = _mutant_always_live(base, live_older, None)
        check(
            "M2 变异反证：「派生不比基座新也用派生」在 P9/P10 夹具上必须失败",
            correct_equal != mutant_equal and correct_older != mutant_older,
            f"正确 {correct_equal.name}/{correct_older.name} vs 变异 {mutant_equal.name}/{mutant_older.name}",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- P18~P20：每日链同源（委托 + 路径对账）----------------
    dc = _load("manu_probe_daily_config", DAILY_DIR / "daily_config.py")
    report = dc.cross_check_provider_paths()
    check(
        "P18 每日链 cross_check_provider_paths() ⇒ ok（无第二个字面量）",
        bool(report.get("ok")),
        json_dumps(report),
    )

    daily_src = (DAILY_DIR / "run_qlib_score.py").read_text(encoding="utf-8")
    tree = ast.parse(daily_src)
    sel_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "select_provider":
            sel_node = node
            break
    delegated = False
    if sel_node is not None:
        called: set[str] = set()
        for node in ast.walk(sel_node):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)
        delegated = "select_provider" in called and "_load_qlib_provider" in called
    check(
        "P19 AST：每日链 select_provider 委托共享模块（不再自带规则）",
        delegated,
        f"找到函数={sel_node is not None} / 委托={delegated}",
    )

    # 🔴 每日链模块只在**函数体**里用 pandas ⇒ 注入空模块桩即可 import（探针只调纯函数）
    if "pandas" not in sys.modules:
        sys.modules["pandas"] = types.ModuleType("pandas")
    daily_mod = _load("manu_probe_daily_run_qlib_score", DAILY_DIR / "run_qlib_score.py")
    same = True
    detail = ""
    for asof in (None, "2026-11-05", "2026-12-01"):
        got = daily_mod.select_provider(asof)
        expect = qp.select_provider(
            asof,
            base=dc.QLIB_CN_DATA,
            live=dc.QLIB_CN_DATA_LIVE,
        )
        if str(got[0]) != str(expect[0]) or got[1] != expect[1]:
            same = False
            detail = f"asof={asof}: {got} vs {expect}"
            break
    check(
        "P20 运行时等价：每日链 select_provider 与共享模块逐字相同",
        same,
        detail or "三组 asof 全部一致",
    )

    print(f"\n=== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    if FAIL:
        print("FAIL：" + ", ".join(FAIL))
        return 1
    return 0


def json_dumps(obj) -> str:
    """轻量 JSON 摘要（避免为一个断言引入 json 依赖顺序问题）。"""
    try:
        import json

        return json.dumps(obj, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"<无法序列化：{exc}>"


if __name__ == "__main__":
    sys.exit(main())
