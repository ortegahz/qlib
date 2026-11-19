#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-18（用户目标文档 §二.1）：**TA 陪审团必须看真账户**（单测守卫）。

【钉的是什么】
`build_ta_context.py` 旧口径读**研究链 canonical 账户**（2026-11-04 旧件：
总资产 29,854.67 / 现金 15,518.87 / 5 只持仓 / 权益 48%）
⇒ TA 以为账户是重仓 ⇒ 给出 `Underweight` + 权益上限 20%
⇒ 下游拿真实 23.43% 去比 ⇒ 「一笔不动」⇒ **风控大脑看着假账本做决策**。

【本文件钉三条】
① `--account-json` 给了 ⇒ 上下文里的账户数字**逐字**来自那份 JSON；
② 上下文里**不得**出现 canonical 旧账户的数字 / 旧持仓代码；
③ 给不了 / 给坏了 ⇒ **fail-closed**（抛错），**绝不**回落到 canonical。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LLM_DIR = ROOT / "llm"
if str(LLM_DIR) not in sys.path:
    sys.path.insert(0, str(LLM_DIR))

import build_ta_context as BTA  # noqa: E402

#: 🔴 canonical（研究链）旧账户的数字 —— **不许**出现在用了 `--account-json` 的上下文里
_CANONICAL_CASH = "15,518.87"
_CANONICAL_TOTAL = "29,854.67"
#: canonical 旧持仓（11-04 已清仓的老票）
_CANONICAL_ONLY_CODES = ("159929", "513330", "513770", "512690")

#: 实盘口径基准（2026-11-05 用户口径）：23,367.37 现金 + 6,668.20 市值 = 30,035.57
_LIVE_ACCOUNT = {
    "source": "manu/etf_rotation/daily/account_state.py",
    "snapshot_date": "2026-11-05",
    "currency": "CNY",
    "total_assets": 30035.57,
    "cash": 23367.37,
    "market_value": 6668.20,
    "positions": [
        {
            "code": "512800", "name": "银行ETF华宝", "shares": 7700,
            "price": 0.866, "market_value": 6668.20,
            "cost": 6344.03, "pnl": 324.17, "pnl_pct": 0.0511,
        },
    ],
    "cleared_positions": [{"code": "159929", "name": "医药ETF"}],
    "discrepancy_note": "（夹具）口径披露测试文本",
}


def _write_account_json(tmp_path: Path, payload: dict | None = None) -> Path:
    p = Path(tmp_path) / "account_context.json"
    p.write_text(json.dumps(payload if payload is not None else _LIVE_ACCOUNT,
                             ensure_ascii=False), encoding="utf-8")
    return p


# ───────────────────────── ① 纯函数：解析 + fail-closed ─────────────────────────
def test_账户JSON_解析出与文件逐字一致的数字(tmp_path):
    p = _write_account_json(tmp_path)
    acc = BTA.load_account_from_json(p)
    s = acc["summary"]
    assert abs(s["total_assets"] - 30035.57) < 1e-9, s
    assert abs(s["cash"] - 23367.37) < 1e-9, s
    assert abs(s["market_value"] - 6668.20) < 1e-9, s
    assert abs(s["cash_weight"] - 23367.37 / 30035.57) < 1e-12, s
    assert abs(s["total_pnl"] - 324.17) < 1e-6, s
    assert acc["rows"][0]["code"] == "512800", acc["rows"]
    assert int(acc["rows"][0]["shares"]) == 7700, acc["rows"]
    assert abs(float(acc["rows"][0]["price"]) - 0.866) < 1e-9, acc["rows"]
    assert abs(acc["weights"]["512800"] - 6668.20 / 30035.57) < 1e-12, acc["weights"]


def test_账户JSON_文件不存在_抛错不回落(tmp_path):
    with pytest.raises(FileNotFoundError):
        BTA.load_account_from_json(Path(tmp_path) / "nope.json")


def test_账户JSON_缺字段_抛错(tmp_path):
    bad = {k: v for k, v in _LIVE_ACCOUNT.items() if k != "cash"}
    with pytest.raises(ValueError):
        BTA.load_account_from_json(_write_account_json(tmp_path, bad))


def test_账户JSON_对账不平_抛错不投喂半张账本(tmp_path):
    bad = dict(_LIVE_ACCOUNT)
    bad["total_assets"] = 30000.00          # 与「现金 + 市值」差 35.57 元
    with pytest.raises(ValueError):
        BTA.load_account_from_json(_write_account_json(tmp_path, bad))


# ───────────────────────── ② 端到端：上下文里的账户口径 ─────────────────────────
def _fake_run_root(tmp_path: Path) -> Path:
    run = Path(tmp_path)
    run.mkdir(parents=True, exist_ok=True)
    rep = {
        "meta": {"asof": "2026-11-18"},
        "next_rebalance": {"target_date": "2026-12-01", "trading_days_until": 9},
        "candidates": [
            {"code": "512800", "name": "银行ETF华宝", "final_score": 0.95,
             "theme_id": "银行", "selected": True, "above_ma60": True,
             "entry_ma_ok": True, "broken": False},
        ],
        "stock_scores": {
            "n_stocks": 5571, "unique_values": 3120, "mean": 0.01, "std": 0.20,
            "degenerate_check": {"passed": True, "rule": "唯一值数>1 且 标准差>0"},
        },
        "rank_ic": {},
        "filter_stats": {"stages": []},
        "risk_disclosure": [],
    }
    rec = {
        "pool_mode": "v2",
        "rebalance_every_n_days": 20,
        "defensive_mode": False,
        "brake_mode": "none",
        "relaxed_absolute_momentum_filter": False,
        "holdings": [
            {"code": "512800", "name": "银行ETF华宝", "target_weight": 0.16,
             "final_score": 0.95, "theme_id": "银行", "above_ma60": True},
        ],
        "trades": [
            {"action": "持有", "code": "512800", "name": "银行ETF华宝",
             "target_weight": 0.16, "current_weight": 0.2343, "amount": 0.0},
        ],
        "stats": {"n_entry_ma_reject": 0, "forced_exit_codes": []},
    }
    (run / "etf_analysis_report.json").write_text(
        json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    (run / "rebalance_recommendation.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return run


def test_端到端_上下文账户来自快照JSON_不含canonical旧账(tmp_path, monkeypatch):
    run = _fake_run_root(tmp_path)
    acc_json = _write_account_json(tmp_path)
    # 重合度/拥挤度算不出来 ⇒ 整节省略（不喂假数据）
    monkeypatch.setattr(BTA.overlap_mod, "load_overlap_inputs",
                        lambda **kw: (None, None))
    text, meta = BTA.build(
        run, run / "etf_analysis_report.md", None, None, None, None, None,
        account_json=acc_json,
    )
    # ① 快照口径的数字**逐字**出现
    assert "30,035.57" in text, text[:2000]
    assert "23,367.37" in text, text[:2000]
    assert "6,668.20" in text, text[:2000]
    assert "512800" in text
    # ② canonical 旧账**一个字都不许**出现
    assert _CANONICAL_CASH not in text, "上下文里出现了研究链 canonical 的旧现金数"
    assert _CANONICAL_TOTAL not in text, "上下文里出现了研究链 canonical 的旧总资产"
    for _code in _CANONICAL_ONLY_CODES:
        assert _code not in text, f"上下文里出现了 canonical 旧持仓 {_code}"
    # ③ meta 记录了账户来源（审计用）
    assert meta["account_source_label"] == "snapshot_json", meta
    assert meta["account_source"] == str(acc_json), meta


def test_端到端_不给账户JSON_走canonical口径_向后兼容(tmp_path, monkeypatch):
    """不给 `--account-json` ⇒ 旧口径（研究链 canonical）**逐字不变**。"""
    run = _fake_run_root(tmp_path)
    monkeypatch.setattr(BTA.overlap_mod, "load_overlap_inputs",
                        lambda **kw: (None, None))
    try:
        text, meta = BTA.build(
            run, run / "etf_analysis_report.md", None, None, None, None, None)
    except Exception as exc:  # noqa: BLE001 —— canonical 快照文件缺失时跳过
        pytest.skip(f"研究链 canonical 账户快照不可得：{exc}")
    assert meta["account_source_label"] == "research_chain_canonical", meta
