"""🔴 2026-11-27 新增：**「强制退出」是闸门决策，不是破位诊断** —— 交付文案不许说谎。

【缺陷来源】
探针 H2 修复（`analyze.py`）把持仓诊断的 `ma60_ok` 从 **exit 闸门 map** 改成
**恒定 MA60 口径实算的诊断值**（原先绑在闸门上 ⇒ V2 冻结件 `exit_ma_window=0`
⇒ 恒为 `None` ⇒ 探针答不出「这只持仓破没破 MA60」）。

⚠️ 副作用（**真缺陷**）：`final_rebalance_plan.py` 里有一行
``if p["current_shares"] and p["ma60_ok"] is False:`` ⇒ 追加文案
「**已跌破 MA60 ⇒ 持仓强制退出（不看排名）**」。
改之前 V2 下 `ma60_ok` 恒 `None` ⇒ 分支**永不触发** ⇒ 不会说谎；
改之后 `ma60_ok` 变成**真值** ⇒ V2 下**可能触发** ⇒ 而 V2 冻结件
`exit_ma_window = 0`（**强制退出闸门关闭**）⇒ 跌破 MA60 的持仓**根本不会被
无条件卖出** ⇒ 交付件在**说谎**（读者会以为必须卖）。

⇒ 修法：文案里的「强制退出」**同源**取报告 `meta.params.exit_ma_window`
   （`analyze.py` 写入的那一个）：
     · `> 0` ⇒ 闸门开着 ⇒ 保留「强制退出」措辞；
     · `= 0` ⇒ 闸门关闭 ⇒ 只披露「破位诊断」，**不许**声称强制退出；
     · 字段缺失 ⇒ **不声称**（宁可少说一句，也不许写一句可能不成立的操作指令）。

【本文件钉死四件事（自带夹具，不依赖服务器产物）】
  ① 闸门关闭 ⇒ 文案**不许**出现「强制退出」，且必须出现「破位诊断」；
  ② 闸门开启 ⇒ 文案**必须**出现「强制退出（不看排名）」；
  ③ 闸门字段缺失 ⇒ 文案**不许**出现「强制退出」；
  ④ **变异反证**：把修法改回「无条件声称强制退出」⇒ ①③ 必须转红（证明本测试有牙）。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

import final_rebalance_plan as frp  # noqa: E402

_HELD = "512800"
_SIGNAL_DATE = "2026-11-09"
_EXEC_DATE = "2026-12-01"


def _load_module(name: str, path: pathlib.Path):
    """按**路径**加载模块（变异反证用：只加载副本，**不碰真源码**）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载不了模块：{path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: pathlib.Path, exit_window: object = 0) -> types.SimpleNamespace:
    """合成一份「账户持有 512800 且 `ma60_ok=False`（跌破 MA60）」的报告 + 决策件。"""
    params = {"commission_bp": 1.5, "slippage_bp": 5.0, "pool_mode": "e_covered"}
    if exit_window is not _MISSING:
        params["exit_ma_window"] = exit_window
    report = {
        "meta": {"asof": _SIGNAL_DATE, "params": params},
        "current_holdings_diagnosis": {
            "total_assets": 30035.57,
            "cash": 23367.37,
            "holdings": [{
                "code": _HELD, "name": "银行ETF华宝", "shares": 7700,
                "market_value": 6668.20, "cost": 1.0, "e_score": 0.4177,
                # 🔴 本测试的**被检事实源**：跌破 MA60 ⇒ 诊断值为 False
                "ma20_ok": True, "ma60_ok": False, "mom120": 0.1288,
            }],
        },
        "next_rebalance": {"signal_date": _SIGNAL_DATE, "target_date": _EXEC_DATE,
                           "execution_date": _EXEC_DATE},
        "recommendation": {"actions": [{
            "code": _HELD, "name": "银行ETF华宝", "action": "持有",
            "target_weight": 0.16, "reason": "规则层：银行主题唯一入选",
        }]},
        "candidates": [{
            "code": _HELD, "name": "银行ETF华宝", "final_score": 0.9,
            "e_score": 0.4177, "coverage": 1.0, "theme_id": "银行",
            "mom20": 0.05, "mom60": 0.06, "mom120": 0.1288, "broken": False,
        }],
        "filter_stats": {"n_candidate_pool": 1, "universe": 1664, "liquidity": 292},
    }
    (tmp_path / "etf_analysis_report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "etf_prices.csv").write_text(
        f"code,date,close\n512800,{_SIGNAL_DATE},0.866\n512800,2026-11-06,0.860\n",
        encoding="utf-8")
    # 🔴 决策件**不许**否决 512800（被否决 ⇒ 走「维持」分支 ⇒ 提前 `continue`
    #    ⇒ 根本走不到被检的那段代码 ⇒ 断言会**真空通过**）。
    (tmp_path / "tradingagents_decision.json").write_text(json.dumps({
        "status": "ok", "signal": "Underweight",
        "final_trade_decision": "## 最终交易决定\n\n维持现有配置。\n",
        "meta": {"context_sha1": "0" * 40},
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "tradingagents_decision.md").write_text(
        "# 决策\n\n## 最终交易决定\n\n维持现有配置。\n", encoding="utf-8")
    return types.SimpleNamespace(
        report=str(tmp_path / "etf_analysis_report.json"),
        decision=str(tmp_path / "tradingagents_decision.md"),
        out_dir=str(tmp_path),
        prices=str(tmp_path / "etf_prices.csv"),
        universe=str(tmp_path / "etf_universe.csv"),
        account=None,
    )


#: 哨兵：**区分「字段缺失」与「字段值为 None/0」**
_MISSING = object()


def _held_reason(plan: dict) -> str:
    """计划里 512800 那一行的 `reason`（找不到 ⇒ 断言失败，不许静默跳过）。"""
    rows = {str(a.get("code")): a for a in (plan.get("actions") or [])}
    assert _HELD in rows, f"计划里没有 {_HELD}（夹具失效）：{list(rows)}"
    return str(rows[_HELD].get("reason") or "")


def test_闸门关闭_文案不许声称强制退出(tmp_path: pathlib.Path):
    """① V2 冻结件口径（`exit_ma_window = 0`）⇒ 只披露破位诊断，**不许**说强制退出。"""
    plan = frp.build_plan(_fixture(tmp_path, exit_window=0))
    reason = _held_reason(plan)
    assert "强制退出（不看排名）" not in reason, (
        f"闸门关闭却声称「强制退出（不看排名）」（口径说谎）：{reason}"
    )
    assert "破位诊断" in reason, f"闸门关闭时必须披露「破位诊断」：{reason}"


def test_闸门开启_文案保留强制退出措辞(tmp_path: pathlib.Path):
    """② 闸门开着（`exit_ma_window = 60`）⇒ 保留「强制退出（不看排名）」措辞。"""
    plan = frp.build_plan(_fixture(tmp_path, exit_window=60))
    reason = _held_reason(plan)
    assert "强制退出（不看排名）" in reason, (
        f"闸门开着却没写「强制退出（不看排名）」（丢了真事实）：{reason}"
    )


def test_闸门字段缺失_文案不许声称强制退出(tmp_path: pathlib.Path):
    """③ 闸门字段缺失 ⇒ **不声称**强制退出（宁可少说一句，也不许写可能不成立的指令）。"""
    plan = frp.build_plan(_fixture(tmp_path, exit_window=_MISSING))
    reason = _held_reason(plan)
    assert "强制退出（不看排名）" not in reason, (
        f"闸门状态不明却声称「强制退出（不看排名）」（口径说谎）：{reason}"
    )
    assert "破位诊断" in reason, f"闸门状态不明时必须披露「破位诊断」：{reason}"


def test_变异反证_改回无条件声称强制退出_闸门关闭与缺失必须转红(tmp_path: pathlib.Path):
    """④ **变异反证**：把文案改回「无条件声称强制退出」⇒ ①③ 必须转红（证明有牙）。

    🔴 变异体只在**临时副本**里做（复制源码到 tmp 再改）⇒ **不碰生产文件**。
    """
    src = pathlib.Path(frp.__file__)
    text = src.read_text(encoding="utf-8")
    old = 'if _exit_gate_enabled(meta)'
    new = 'if True'
    assert old in text, "变异点没找到（源码已变？）"
    mutated_path = tmp_path / "mutated_final_rebalance_plan.py"
    mutated_path.write_text(text.replace(old, new, 1), encoding="utf-8")
    mutated = _load_module("mutated_frp", mutated_path)

    for label, window in (("闸门关闭", 0), ("字段缺失", _MISSING)):
        sub = tmp_path / label
        sub.mkdir()
        plan = mutated.build_plan(_fixture(sub, exit_window=window))
        reason = _held_reason(plan)
        assert "强制退出（不看排名）" in reason, (
            f"变异体（{label}）竟然**没**声称强制退出 ⇒ 变异没生效 ⇒ 反证无效：{reason}"
        )
    # 🔴 反证成立 ⇒ 原实现（闸门关闭 / 字段缺失时）必须**不**声称强制退出。
    for label, window in (("闸门关闭", 0), ("字段缺失", _MISSING)):
        sub = tmp_path / f"orig_{label}"
        sub.mkdir()
        plan = frp.build_plan(_fixture(sub, exit_window=window))
        assert "强制退出（不看排名）" not in _held_reason(plan), (
            f"{label}：原实现竟然声称强制退出"
        )
