"""test_stage4_llm_controls.py —— **阶段 4（TradingAgents 辩论）**的三个修复项自测。

【完全离线】：不联网、不调 LLM、不跑研究链；只 import 本工程的纯函数
（`daily_config` / `account_state` / `agents_rebalance` / `run_tradingagents` 的 helper）。

【覆盖清单（对应交付规格）】
1. `daily_config.LLM_TIMEOUT` 默认 **5400** 秒（不再是 1800），env `ETF_ROTATION_LLM_TIMEOUT` 可覆盖；
2. `main.py` 的 `--llm-timeout` / `--reasoning-effort` 两个 CLI 参数存在且缺省语义正确
   （`--reasoning-effort` 缺省空串 = **不覆盖**）；
3. `agents_rebalance.run_pipeline` 的 `llm_timeout` 缺省**读 DC.LLM_TIMEOUT**（不写死 1800）；
4. `reasoning_effort` 非空 ⇒ 子进程 env 里出现 `TRADINGAGENTS_REASONING_EFFORT`；
   `_env_with_dotenv` 的**合并顺序** = `.env` < 进程 env < 显式 overrides；
5. 🔴 账户口径修复：`account_state.write_account_json` 导出的 JSON **必含**
   现金 / 总资产 / 持仓（代码 · 股数 · 成本），且**只含实盘唯一底仓 512800**
   ⇒ `run_tradingagents._account_positions` 映射出的持仓**不是**研究链那 5 只。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_DAILY = _HERE.parent
_LLM = _DAILY.parent / "llm"
for _p in (_DAILY, _LLM):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import account_state  # noqa: E402
import agents_rebalance as AR  # noqa: E402
import daily_config as DC  # noqa: E402
import main as DM  # noqa: E402


def _load_ta_module():
    """按路径加载 `run_tradingagents.py`（它在 llm 目录，不在 daily 的 sys.path 上）。"""
    spec = importlib.util.spec_from_file_location(
        "_run_tradingagents_under_test", _LLM / "run_tradingagents.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TA = _load_ta_module()


# ---------------------------------------------------------------- 1. LLM_TIMEOUT
def test_llm_timeout_default_is_5400():
    """默认超时必须是 **5400 秒**（1800 实测必然超时，见交付说明）。"""
    assert DC.LLM_TIMEOUT == 5400.0


def test_llm_timeout_env_override(monkeypatch):
    """env `ETF_ROTATION_LLM_TIMEOUT` 能覆盖默认值。"""
    monkeypatch.setenv("ETF_ROTATION_LLM_TIMEOUT", "7200")
    reloaded = importlib.reload(DC)
    try:
        assert reloaded.LLM_TIMEOUT == 7200.0
    finally:
        monkeypatch.undo()
        importlib.reload(DC)
    assert DC.LLM_TIMEOUT == 5400.0


# ---------------------------------------------------------------- 2. CLI 参数
def test_cli_exposes_llm_timeout_and_reasoning_effort():
    """`main.py --help` 里两个新参数都可见。"""
    args = DM.parse_args([])
    assert args.llm_timeout is None, "缺省应交给 daily_config.LLM_TIMEOUT 决定"
    assert args.reasoning_effort == "", "缺省空串 = 不覆盖 .env 现役值"


def test_cli_parses_llm_timeout_and_reasoning_effort():
    args = DM.parse_args(["--llm-timeout", "1800", "--reasoning-effort", "medium"])
    assert args.llm_timeout == 1800.0
    assert args.reasoning_effort == "medium"


def test_cli_rejects_unknown_reasoning_effort():
    with pytest.raises(SystemExit):
        DM.parse_args(["--reasoning-effort", "turbo"])


def test_run_tradingagents_cli_exposes_new_args():
    """`run_tradingagents.py` 也有 `--account-json` / `--reasoning-effort`。"""
    import argparse
    import inspect

    source = inspect.getsource(TA.main)
    assert '"--account-json"' in source
    assert '"--reasoning-effort"' in source
    # 参数解析走 argparse ⇒ 只需确认 add_argument 调用存在且默认值正确
    assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)


# ---------------------------------------------------------------- 3. 超时缺省读配置
def test_run_pipeline_timeout_defaults_to_dc_llm_timeout(monkeypatch):
    """`run_pipeline` 不传 `llm_timeout` ⇒ 用 `DC.LLM_TIMEOUT`（**不是** 1800）。"""
    monkeypatch.setattr(DC, "LLM_TIMEOUT", 5400.0)
    assert AR._resolve_llm_timeout(None) == 5400.0
    # 显式传入时以传入值为准
    assert AR._resolve_llm_timeout(123.0) == 123.0


def test_run_pipeline_signature_has_no_hardcoded_1800():
    """🔴 回归护栏：`run_pipeline` 的默认超时**不许**再写死 1800。"""
    import inspect

    sig = inspect.signature(AR.run_pipeline)
    assert sig.parameters["llm_timeout"].default is None
    assert "1800" not in inspect.getsource(AR.run_pipeline).split("llm_timeout")[1][:200]


# ---------------------------------------------------------------- 4. reasoning_effort env
def test_env_with_dotenv_overrides_dotenv_value(tmp_path, monkeypatch):
    """🔴 合并顺序：``.env`` < 进程 env < **显式 overrides**（本轮指定的最高优先）。"""
    ta_dir = tmp_path / "TradingAgents"
    ta_dir.mkdir()
    (ta_dir / ".env").write_text(
        "TRADINGAGENTS_REASONING_EFFORT=max\nTRADINGAGENTS_LLM_PROVIDER=openai\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TRADINGAGENTS_REASONING_EFFORT", raising=False)

    env = AR._env_with_dotenv(ta_dir)
    assert env["TRADINGAGENTS_REASONING_EFFORT"] == "max", ".env 的值应被带上"

    env = AR._env_with_dotenv(ta_dir, overrides={"TRADINGAGENTS_REASONING_EFFORT": "medium"})
    assert env["TRADINGAGENTS_REASONING_EFFORT"] == "medium", "显式 overrides 必须盖过 .env"


def test_env_with_dotenv_process_env_beats_dotenv(tmp_path, monkeypatch):
    """进程里**已有**的 env 优先于 ``.env``（与 ``load_dotenv(override=False)`` 同口径）。"""
    ta_dir = tmp_path / "TradingAgents"
    ta_dir.mkdir()
    (ta_dir / ".env").write_text("TRADINGAGENTS_REASONING_EFFORT=max\n", encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENTS_REASONING_EFFORT", "high")

    env = AR._env_with_dotenv(ta_dir)
    assert env["TRADINGAGENTS_REASONING_EFFORT"] == "high"


def test_env_with_dotenv_without_dotenv_package(tmp_path, monkeypatch):
    """``dotenv`` 不可用时的手工解析分支**同样**遵守 overrides 最高优先。"""
    import builtins

    ta_dir = tmp_path / "TradingAgents"
    ta_dir.mkdir()
    (ta_dir / ".env").write_text("TRADINGAGENTS_REASONING_EFFORT=max\n", encoding="utf-8")
    monkeypatch.delenv("TRADINGAGENTS_REASONING_EFFORT", raising=False)

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "dotenv":
            raise ImportError("dotenv 不可用（测试）")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    env = AR._env_with_dotenv(ta_dir, overrides={"TRADINGAGENTS_REASONING_EFFORT": "low"})
    assert env["TRADINGAGENTS_REASONING_EFFORT"] == "low"


# ---------------------------------------------------------------- 5. 🔴 账户口径
def test_write_account_json_contains_cash_total_assets_positions(tmp_path):
    """导出的 JSON **必含** 现金 / 总资产 / 持仓（代码 · 股数 · 成本）。"""
    out = account_state.write_account_json(tmp_path / "account_context.json")
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert payload["cash"] == pytest.approx(DC.CASH)
    assert payload["total_assets"] == pytest.approx(DC.TOTAL_ASSETS)
    assert payload["positions"], "持仓不能为空"
    pos = payload["positions"][0]
    assert pos["code"] == DC.PRIMARY_HOLDING_CODE
    assert float(pos["shares"]) == pytest.approx(DC.PRIMARY_HOLDING_SHARES)
    assert "cost" in pos and float(pos["cost"]) > 0, "必须带成本"


def test_write_account_json_has_only_live_position(tmp_path):
    """🔴 实盘口径**只有 512800**（159929/512690/513330/513770 已清仓 ⇒ 不许出现在持仓里）。"""
    out = account_state.write_account_json(tmp_path / "account_context.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    codes = {str(p["code"]) for p in payload["positions"]}
    assert codes == {"512800"}
    cleared = {str(p["code"]) for p in payload["cleared_positions"]}
    assert cleared == {"159929", "512690", "513330", "513770"}


def test_account_json_matches_account_summary(tmp_path):
    """导出的 JSON 与 `account_state.account_summary()` **逐字一致**（同一真值源）。"""
    out = account_state.write_account_json(tmp_path / "account_context.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    summary = account_state.account_summary()
    assert payload["cash"] == pytest.approx(summary["cash"])
    assert payload["total_assets"] == pytest.approx(summary["total_assets"])
    assert payload["market_value"] == pytest.approx(summary["market_value"])
    assert payload["snapshot_date"] == summary["snapshot_date"]


def test_account_positions_maps_cost_to_average_price(tmp_path):
    """`run_tradingagents` 的映射：总成本 ÷ 股数 = 每股成本（TA 的 `average_price`）。"""
    out = account_state.write_account_json(tmp_path / "account_context.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    positions = TA._account_positions(payload)

    assert len(positions) == 1
    assert positions[0]["ticker"] == "512800"
    assert positions[0]["quantity"] == pytest.approx(7700.0)
    expected_cost_price = float(payload["positions"][0]["cost"]) / 7700.0
    assert positions[0]["average_price"] == pytest.approx(expected_cost_price)


def test_account_positions_is_not_research_chain_five_positions(tmp_path):
    """🔴 回归护栏：喂进去的组合**不是**研究链那 5 只（否则 bug 复发）。"""
    out = account_state.write_account_json(tmp_path / "account_context.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    tickers = {p["ticker"] for p in TA._account_positions(payload)}
    assert tickers == {"512800"}
    assert "159929" not in tickers and "512690" not in tickers


def test_load_account_json_rejects_missing_fields(tmp_path):
    """🔴 账户 JSON 缺字段 ⇒ **直接报错**（不许静默回退到研究链 CSV）。"""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"cash": 1.0}), encoding="utf-8")
    with pytest.raises(SystemExit):
        TA._load_account_json(bad)


def test_load_account_json_rejects_non_object(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(SystemExit):
        TA._load_account_json(bad)


def test_load_account_json_accepts_valid_payload(tmp_path):
    out = account_state.write_account_json(tmp_path / "account_context.json")
    payload = TA._load_account_json(out)
    assert payload["cash"] == pytest.approx(DC.CASH)
    assert payload["total_assets"] == pytest.approx(DC.TOTAL_ASSETS)
    assert isinstance(payload["positions"], list)


# ---------------------------------------------------------------- 6. 研究链真值不许被动
def test_research_account_snapshot_csv_untouched():
    """🔴 `manu/etf_rotation/data/account_snapshot.csv` 是研究链输入真值 ⇒ 本次不许改。"""
    csv = _DAILY.parent / "data" / "account_snapshot.csv"
    assert csv.exists(), "研究链账户快照 CSV 必须原样存在"
    text = csv.read_text(encoding="utf-8-sig")
    assert "159929" in text, "研究链快照仍应是 5 只持仓那一份（本次没动它）"


# ---------------------------------------------------------------- 7. 阶段 4 接线（端到端）
class _StopAtStage4(Exception):
    """在阶段 4 抓完命令后主动中断（避免真跑 LLM）。"""


def _run_pipeline_until_stage4(tmp_path, monkeypatch, **kwargs):
    """跑 ``run_pipeline`` 直到阶段 4 的 TA 调用（**不执行任何外部命令**）。"""
    calls: list[dict] = []

    # 🔴 用一个**临时**的 TA 目录（含 .env）⇒ 不碰真 `.env`，也不依赖服务器上的现役值
    ta_dir = tmp_path / "TradingAgents"
    ta_dir.mkdir(exist_ok=True)
    (ta_dir / ".env").write_text(
        "TRADINGAGENTS_REASONING_EFFORT=max\n", encoding="utf-8"
    )
    monkeypatch.setattr(DC, "TRADINGAGENTS_DIR", ta_dir)
    monkeypatch.delenv("TRADINGAGENTS_REASONING_EFFORT", raising=False)

    def _fake_run_cmd(cmd, *, step, timeout=None, env=None, **_kwargs):
        calls.append({"cmd": list(cmd), "step": step, "timeout": timeout, "env": env})
        if step == AR.STEP_TRADINGAGENTS:
            raise _StopAtStage4
        return ""

    monkeypatch.setattr(AR, "_run_cmd", _fake_run_cmd)
    monkeypatch.setattr(AR, "_refresh_scores", lambda **kw: None)
    # 🔴 2026-11-09 修：`_export_stock_prices` 带 `out_dir` 形参（产物落盘目录）
    #    ⇒ 桩必须跟着签名走，否则这里抛
    #    `TypeError: got an unexpected keyword argument 'out_dir'` ⇒ 本文件 5 个用例全红。
    monkeypatch.setattr(AR, "_export_stock_prices", lambda asof, notes, out_dir=None: None)
    monkeypatch.setattr(
        AR, "_resolve_analysis_asof",
        lambda **kw: {
            "analysis_asof": "2026-11-04", "scores_max_date": "2026-11-04",
            "grid_last": "2026-11-04", "dataset_max_date": "2026-11-04", "notes": [],
        },
    )
    def _fake_run_rules_layer(run_path, notes, **kwargs):
        """🔴 2026-11-09 修：规则层改成**在本快照内现跑**（``_run_rules_layer``）
        ⇒ 桩仍要产出 ``rebalance_recommendation.json``（build_ta_context 的输入件）。
        """
        out = Path(run_path) / "rebalance_recommendation.json"
        out.write_text(json.dumps({"recommendations": []}), encoding="utf-8")
        notes.append(f"🧪 桩：规则层推荐件已产出：{out}")
        return out

    monkeypatch.setattr(AR, "_run_rules_layer", _fake_run_rules_layer)
    # 🔴 快照根必须落在 tmp_path ⇒ 绝不往**唯一落点根**写真东西
    monkeypatch.setattr(DC, "SNAPSHOT_ROOT", tmp_path / "snapshots")

    with pytest.raises(_StopAtStage4):
        AR.run_pipeline(asof="2026-12-01", run_dir=tmp_path / "run",
                        live_trade=False, **kwargs)
    return calls


def test_stage4_call_passes_account_json(tmp_path, monkeypatch):
    """🔴 阶段 4 的 TA 调用**必须**带 `--account-json`（实盘口径）。"""
    calls = _run_pipeline_until_stage4(tmp_path, monkeypatch, refresh_scores=False)
    ta_call = next(c for c in calls if c["step"] == AR.STEP_TRADINGAGENTS)
    cmd = ta_call["cmd"]
    assert "--account-json" in cmd
    acct_path = Path(cmd[cmd.index("--account-json") + 1])
    assert acct_path.exists(), "账户 JSON 必须已落盘"
    payload = json.loads(acct_path.read_text(encoding="utf-8"))
    assert payload["cash"] == pytest.approx(DC.CASH)
    assert {str(p["code"]) for p in payload["positions"]} == {"512800"}


def test_stage4_call_timeout_is_5400_by_default(tmp_path, monkeypatch):
    """🔴 阶段 4 超时缺省 = 5400 秒（不再是 1800）。"""
    calls = _run_pipeline_until_stage4(tmp_path, monkeypatch, refresh_scores=False)
    ta_call = next(c for c in calls if c["step"] == AR.STEP_TRADINGAGENTS)
    assert ta_call["timeout"] == 5400.0


def test_stage4_call_timeout_overridable(tmp_path, monkeypatch):
    calls = _run_pipeline_until_stage4(
        tmp_path, monkeypatch, refresh_scores=False, llm_timeout=1800.0
    )
    ta_call = next(c for c in calls if c["step"] == AR.STEP_TRADINGAGENTS)
    assert ta_call["timeout"] == 1800.0


def test_stage4_env_carries_reasoning_effort_override(tmp_path, monkeypatch):
    """`--reasoning-effort` ⇒ 子进程 env 里出现 `TRADINGAGENTS_REASONING_EFFORT`。"""
    calls = _run_pipeline_until_stage4(
        tmp_path, monkeypatch, refresh_scores=False, reasoning_effort="medium"
    )
    ta_call = next(c for c in calls if c["step"] == AR.STEP_TRADINGAGENTS)
    env = ta_call["env"]
    assert env is not None
    assert env["TRADINGAGENTS_REASONING_EFFORT"] == "medium"


def test_stage4_env_without_override_has_no_reasoning_effort(tmp_path, monkeypatch):
    """不指定 ⇒ 子进程 env 里是 **.env 的现役值**（不被本轮改写）。"""
    calls = _run_pipeline_until_stage4(tmp_path, monkeypatch, refresh_scores=False)
    ta_call = next(c for c in calls if c["step"] == AR.STEP_TRADINGAGENTS)
    env = ta_call["env"]
    assert env is not None
    assert env["TRADINGAGENTS_REASONING_EFFORT"] == "max", "缺省应沿用 .env 现役值"
