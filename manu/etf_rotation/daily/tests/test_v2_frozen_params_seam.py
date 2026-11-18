"""🔴 **V2 冻结件 → 每日实盘链的接缝（seam）**必须接得**准**、**断得**干净（2026-11-18）。

【背景：为什么有这条接缝】
    2026-11-18 用户目标文档第三条：把 ``frozen_params_wf.json``
    （``pure_E_native + hold_buffer=2``）正式接入每日执行流水线，
    **替换掉 V1 的短窗口平滑与冗余过滤**。
    审计结论（改造前）：每日链的 ETF 层参数**全部**来自 V1 研究链常量
    （alpha/momentum = 0.5/0.5、E 层 EWMA alpha=0.20 window=5、MA60 入场 + MA60 强制退出）
    ⇒ V2 五折滚动前推选出的冠军参数**压根没进每日链**。

【本文件钉死】
    1. 冻结件能读成**已校验**的参数对象（口径字段齐全、值合法）；
    2. 冻结件 → 命令行的翻译**逐字正确**（两链同一组参数）；
    3. 🔴 **fail-closed**：读不到 / 口径不符 ⇒ **抛异常**，**不许**静默回落 V1 老配置；
    4. 🔴 **不许半支持**：本链复现不了的机制（A 臂半衰期平滑 / 负分空仓闸门）⇒ 明确报错；
    5. 🔴 **不许混口径**：冻结件 cadence / top_k 与每日链不一致 ⇒ 报错；
    6. **变异反证**：把 cadence 校验去掉 ⇒ 对应用例必须转红（证明守卫真的在挡）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_DAILY = Path(__file__).resolve().parents[1]
if str(_DAILY) not in sys.path:
    sys.path.insert(0, str(_DAILY))

import agents_rebalance as AR  # noqa: E402
import daily_config as DC  # noqa: E402
import v2_frozen_params as V2FP  # noqa: E402


# ----------------------------------------------------------------- 夹具
def _frozen_payload(**overrides) -> dict:
    """一份**与服务器上 `frozen_params_wf.json` 同形**的冻结件。"""
    params = {
        "smoothing_arm": "B_native_slow",
        "top_k": 5,
        "hold_buffer": 2,
        "rebalance_every_n_days": 20,
        "alpha_weight": 1.0,
        "momentum_weight": 0.0,
        "entry_ma_window": 0,
        "exit_ma_window": 0,
        "allow_theme_cap": False,
        "neg_score_gate": False,
        "neg_score_gate_stat": "median",
        "neg_score_gate_scale": 0.05,
        "neg_score_gate_min_exposure": 0.0,
    }
    params.update(overrides.pop("params", {}))
    payload = {
        "params": params,
        "model_name": "ablate_roc5",
        "winner_arm": "pure_E_native",
        "winner_arm_desc": "纯 E + 臂 B 原生慢变量 + 无 MA + 同族上限关",
        "rebalance_every_n_days": 20,
        "rebalance_signal_stride": 2,
        "best_score": 0.05386111447920429,
        "frozen_at": "2026-11-18T11:05:53",
    }
    payload.update(overrides)
    return payload


def _write_frozen(tmp_path: Path, payload: dict | None = None) -> Path:
    p = tmp_path / "frozen_params_wf.json"
    p.write_text(json.dumps(payload or _frozen_payload(), ensure_ascii=False), encoding="utf-8")
    return p


# ----------------------------------------------------------------- 1. 加载器
def test_冻结件能读成已校验的参数对象(tmp_path: Path) -> None:
    p = _write_frozen(tmp_path)
    fp = V2FP.load_frozen_params(p)
    assert fp.winner_arm == "pure_E_native"
    assert fp.alpha_weight == 1.0 and fp.momentum_weight == 0.0
    assert fp.smoothing_arm == "B_native_slow" and fp.smoothing_enabled is False
    assert (fp.entry_ma_window, fp.exit_ma_window) == (0, 0)
    assert (fp.top_k, fp.hold_buffer) == (5, 2)
    assert fp.allow_theme_cap is False and fp.neg_score_gate is False
    assert fp.rebalance_every_n_days == 20
    assert len(fp.md5) == 32


def test_命令行翻译逐字正确(tmp_path: Path) -> None:
    """🔴 冻结件 ⇒ analyze.py / recommend.py 的参数（**两链同一组**）。"""
    fp = V2FP.load_frozen_params(_write_frozen(tmp_path))
    args = fp.etf_layer_args()
    assert "--no-smoothing" in args          # 关掉 V1 的 E 层 EWMA
    assert "--smoothing-alpha" not in args   # 不许与 --no-smoothing 同时出现
    pairs = dict(zip(args[::2], args[1::2]))
    assert pairs["--alpha-weight"] == "1"
    assert pairs["--momentum-weight"] == "0"
    assert pairs["--hold-buffer"] == "2"
    assert pairs["--target-n-holdings"] == "5"
    assert pairs["--max-per-theme"] == "0"   # allow_theme_cap=False ⇒ 同族上限不限
    assert pairs["--entry-ma-window"] == "0"
    assert pairs["--exit-ma-window"] == "0"


def test_披露块含来源与md5(tmp_path: Path) -> None:
    p = _write_frozen(tmp_path)
    d = V2FP.load_frozen_params(p).disclosure()
    assert d["enabled"] is True
    assert d["source"] == str(p)
    assert d["source_md5"] == V2FP.load_frozen_params(p).md5
    assert d["winner_arm"] == "pure_E_native"
    assert d["params"]["smoothing_enabled"] is False
    assert "不完全等价" in str(d["caveat"])


# ----------------------------------------------------------------- 2. fail-closed
def test_冻结件读不到时必须抛异常(tmp_path: Path) -> None:
    with pytest.raises(V2FP.V2FrozenParamsError):
        V2FP.load_frozen_params(tmp_path / "不存在.json", required=True)


def test_显式降级模式才允许返回None(tmp_path: Path) -> None:
    assert V2FP.load_frozen_params(tmp_path / "不存在.json", required=False) is None


def test_非法JSON必须抛异常(tmp_path: Path) -> None:
    p = tmp_path / "frozen_params_wf.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(V2FP.V2FrozenParamsError):
        V2FP.load_frozen_params(p)


def test_缺字段必须抛异常(tmp_path: Path) -> None:
    payload = _frozen_payload()
    payload["params"].pop("top_k")
    with pytest.raises(V2FP.V2FrozenParamsError):
        V2FP.load_frozen_params(_write_frozen(tmp_path, payload))


def test_A臂半衰期平滑不可复现必须报错(tmp_path: Path) -> None:
    """🔴 本链是「窗口 + alpha」口径 ⇒ 与 V2 的半衰期口径**不可换算** ⇒ 不许近似。"""
    payload = _frozen_payload(params={"smoothing_arm": "A_half_life_15"})
    with pytest.raises(V2FP.V2FrozenParamsError) as exc:
        V2FP.load_frozen_params(_write_frozen(tmp_path, payload))
    assert "无法复现" in str(exc.value)


def test_负分空仓闸门本链没有必须报错(tmp_path: Path) -> None:
    payload = _frozen_payload(params={"neg_score_gate": True})
    with pytest.raises(V2FP.V2FrozenParamsError) as exc:
        V2FP.load_frozen_params(_write_frozen(tmp_path, payload))
    assert "负分空仓闸门" in str(exc.value)


def test_cadence不是严格月频必须报错(tmp_path: Path) -> None:
    payload = _frozen_payload(params={"rebalance_every_n_days": 90})
    with pytest.raises(V2FP.V2FrozenParamsError) as exc:
        V2FP.load_frozen_params(_write_frozen(tmp_path, payload))
    assert "cadence" in str(exc.value)


def test_权重全零必须报错(tmp_path: Path) -> None:
    payload = _frozen_payload(params={"alpha_weight": 0.0, "momentum_weight": 0.0})
    with pytest.raises(V2FP.V2FrozenParamsError):
        V2FP.load_frozen_params(_write_frozen(tmp_path, payload))


# ----------------------------------------------------------------- 3. 变异反证
def test_变异反证_cadence守卫被改宽就放行90(tmp_path: Path, monkeypatch) -> None:
    """🔴 **变异反证**：把 cadence 守卫改宽 ⇒ ``cadence=90`` 立刻被放过。

    ⇒ 证明「挡住 cadence=90」这件事**确实是那个守卫做的**，不是别的偶然。
    （口径：守卫一旦失效，V2 就会退回那个 2026-11-17 单盲亏 −12.93% 的调仓节奏。）
    """
    payload = _frozen_payload(params={"rebalance_every_n_days": 90})
    p = _write_frozen(tmp_path, payload)

    with pytest.raises(V2FP.V2FrozenParamsError):
        V2FP.load_frozen_params(p)                    # 正式实现：挡住

    monkeypatch.setattr(V2FP, "LOCKED_CADENCE", 90)   # 变异：把守卫改宽
    fp = V2FP.load_frozen_params(p)                   # ⇒ 放行（变异被本用例抓住）
    assert fp.rebalance_every_n_days == 90


def test_变异反证_平滑臂守卫被去掉就放过A臂(tmp_path: Path, monkeypatch) -> None:
    """🔴 **变异反证**：去掉「A 臂不可复现」的守卫 ⇒ A 臂被放过 ⇒ 链条会拿
    **半衰期口径**的参数去喂**窗口 + alpha**口径的链路（口径分叉，且不报错）。"""
    payload = _frozen_payload(params={"smoothing_arm": "A_half_life_15"})
    p = _write_frozen(tmp_path, payload)

    with pytest.raises(V2FP.V2FrozenParamsError):
        V2FP.load_frozen_params(p)

    monkeypatch.setattr(V2FP, "SUPPORTED_SMOOTHING_ARMS",
                        ("B_native_slow", "A_half_life_15", "A_half_life_20"))
    fp = V2FP.load_frozen_params(p)                   # ⇒ 放行（变异被本用例抓住）
    assert fp.smoothing_arm == "A_half_life_15"


# ----------------------------------------------------------------- 4. 每日链接线
def test_接缝开启时每日链用冻结件参数(tmp_path: Path, monkeypatch) -> None:
    p = _write_frozen(tmp_path)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_REQUIRED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_FILE", p)
    args, disclosure = AR._etf_layer_args()
    pairs = dict(zip(args[::2], args[1::2]))
    assert "--no-smoothing" in args
    assert pairs["--alpha-weight"] == "1"
    assert pairs["--entry-ma-window"] == "0"
    assert disclosure.get("enabled") is True
    assert disclosure.get("source") == str(p)


def test_接缝关闭时回落V1老参数(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", False)
    args, disclosure = AR._etf_layer_args()
    pairs = dict(zip(args[::2], args[1::2]))
    assert "--smoothing-alpha" in args
    assert "--no-smoothing" not in args
    assert pairs["--entry-ma-window"] == str(DC.ENTRY_MA_WINDOW)
    assert disclosure.get("enabled") is False


def test_接缝开启但冻结件缺失必须中止(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_REQUIRED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_FILE", tmp_path / "不存在.json")
    with pytest.raises(V2FP.V2FrozenParamsError):
        AR._etf_layer_args()


def test_cadence与每日链部署节奏不一致必须报错(tmp_path: Path, monkeypatch) -> None:
    """🔴 冻结件 cadence=20 与每日链部署节奏不一致 ⇒ 拒绝跑（不许混口径）。"""
    p = _write_frozen(tmp_path, _frozen_payload(params={"rebalance_every_n_days": 20}))
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_REQUIRED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_FILE", p)
    monkeypatch.setattr(AR, "_research_config", lambda: type(
        "Cfg", (), {"DEPLOYED_REBALANCE_EVERY_N_DAYS": 10, "TARGET_N_HOLDINGS": 5})())
    with pytest.raises(V2FP.V2FrozenParamsError) as exc:
        AR._etf_layer_args()
    assert "部署节奏" in str(exc.value)


def test_top_k与账户目标持仓数不一致必须报错(tmp_path: Path, monkeypatch) -> None:
    p = _write_frozen(tmp_path, _frozen_payload(params={"top_k": 5}))
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_REQUIRED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_FILE", p)
    monkeypatch.setattr(DC, "TARGET_POSITIONS", 3)
    monkeypatch.setattr(AR, "_research_config", lambda: type(
        "Cfg", (), {"DEPLOYED_REBALANCE_EVERY_N_DAYS": 20, "TARGET_N_HOLDINGS": 5})())
    with pytest.raises(V2FP.V2FrozenParamsError) as exc:
        AR._etf_layer_args()
    assert "账户目标持仓数" in str(exc.value)


# ----------------------------------------------------------------- 5. 报告字段守卫
def test_分析报告的params字典不许有重复键() -> None:
    """🔴 **重复键守卫**：dict 字面量里同名键**后者覆盖前者** ⇒ 报告里的
    「生效值」会被一行写死的 config 值悄悄顶掉（2026-11-18 接缝施工时真踩到：
    新加的 ``max_per_theme`` 被原有的同名键覆盖 ⇒ 报告恒显 config 值）。

    ⇒ 用 AST 扫 ``analyze.py`` 的**所有** dict 字面量 ⇒ 有重复字符串键 ⇒ 报缺陷。
    """
    import ast

    src_path = Path(__file__).resolve().parents[2] / "analyze.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen: dict[str, int] = {}
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                seen[key.value] = seen.get(key.value, 0) + 1
        for name, cnt in seen.items():
            if cnt > 1:
                bad.append(f"{src_path.name}:{node.lineno} 重复键 `{name}` × {cnt}")
    assert not bad, "字典字面量出现重复键（后者覆盖前者 ⇒ 生效值被顶掉）：\n" + "\n".join(bad)


# =============================================================================
# 6. 🔴 两链同源：分析链 ``analyze.py`` 与规则层 ``recommend.py`` **同一组** ETF 参数
# =============================================================================
#: ETF 层参数的**参数名清单**（用于从整条命令行里挑出可比的那一段）。
_ETF_FLAG_VOCAB = (
    "--alpha-weight", "--momentum-weight", "--hold-buffer", "--target-n-holdings",
    "--max-per-theme", "--entry-ma-window", "--exit-ma-window",
    "--smoothing-method", "--smoothing-window", "--smoothing-alpha",
    "--smoothing-history-days", "--no-smoothing",
)
#: V1 老口径的平滑参数（接缝开着时**不许**出现在命令行里）。
_V1_SMOOTHING_FLAGS = ("--smoothing-method", "--smoothing-window",
                       "--smoothing-alpha", "--smoothing-history-days")


def _etf_flag_slice(cmd: list[str]) -> list[str]:
    """从**整条命令行**里挑出 ETF 层参数（布尔 flag 单独占一个 token）。"""
    out: list[str] = []
    index = 0
    while index < len(cmd):
        token = cmd[index]
        if token in _ETF_FLAG_VOCAB:
            if token == "--no-smoothing":
                out.append(token)
                index += 1
            else:
                out.extend([token, cmd[index + 1]])
                index += 2
        else:
            index += 1
    return out


def _etf_pairs(tokens: list[str]) -> dict:
    """把 ETF 层参数 token 列表解析成 ``{flag: value}``（布尔 flag ⇒ ``True``）。"""
    pairs: dict = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--no-smoothing":
            pairs[token] = True
            index += 1
        else:
            pairs[token] = tokens[index + 1]
            index += 2
    return pairs


class _StopAtStage4(Exception):
    """在阶段 4 的 TA 调用处主动中断（不真跑 LLM）。"""


def _capture_pipeline_etf_args(tmp_path, monkeypatch) -> tuple[list[dict], dict]:
    """跑 ``run_pipeline``（**不执行任何外部命令**）⇒ 抓两链的 ETF 层参数。"""
    calls: list[dict] = []
    captured: dict = {}

    ta_dir = tmp_path / "TradingAgents"
    ta_dir.mkdir(exist_ok=True)
    (ta_dir / ".env").write_text(
        "TRADINGAGENTS_REASONING_EFFORT=max\n", encoding="utf-8"
    )
    monkeypatch.setattr(DC, "TRADINGAGENTS_DIR", ta_dir)
    monkeypatch.delenv("TRADINGAGENTS_REASONING_EFFORT", raising=False)

    def _fake_run_cmd(cmd, *, step, timeout=None, env=None, **_kwargs):
        calls.append({"cmd": list(cmd), "step": step})
        if step == AR.STEP_RULES:
            out_dir = Path(cmd[cmd.index("--out-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "rebalance_recommendation.json").write_text(
                json.dumps({"recommendations": []}), encoding="utf-8")
        if step == AR.STEP_TRADINGAGENTS:
            raise _StopAtStage4
        return ""

    monkeypatch.setattr(AR, "_run_cmd", _fake_run_cmd)
    monkeypatch.setattr(AR, "_refresh_scores", lambda **kw: None)
    monkeypatch.setattr(AR, "_export_stock_prices",
                        lambda asof, notes, out_dir=None: None)
    monkeypatch.setattr(
        AR, "_resolve_analysis_asof",
        lambda **kw: {
            "analysis_asof": "2026-12-01", "scores_max_date": "2026-12-01",
            "grid_last": "2026-12-01", "dataset_max_date": "2026-12-01", "notes": [],
        },
    )
    monkeypatch.setattr(DC, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    # 🔴 2026-11-18 续四（reviewer B1）：``run_pipeline`` 会走到
    #    ``agents_rebalance.py:1268`` 的 ``nav_mod.prepare_daily_nav(...)``，
    #    而该调用**没传 path=** ⇒ ``nav_state.state_path()`` 落到**生产**路径
    #    ``DC.NAV_STATE_FILE``（server 的 ``.../_state/nav_history.json``）
    #    ⇒ 测试净值会被写进生产峰值序列 ⇒ 抬高历史峰值 ⇒ **钝化急跌刹车判定**
    #    （生产刹车会被测试悄悄削弱 ⇒ 这正是铁律「不许拿生产当试验田」）。
    #    ⇒ 本用例必须把状态文件重定向到 tmp_path（与 test_nav_state_brake.py 同做法）。
    monkeypatch.setattr(DC, "NAV_STATE_FILE", tmp_path / "nav_history.json")

    real_rules_layer = AR._run_rules_layer

    def _wrapped_rules_layer(run_path, notes, **kwargs):
        captured["etf_args"] = kwargs.get("etf_args")
        return real_rules_layer(run_path, notes, **kwargs)

    monkeypatch.setattr(AR, "_run_rules_layer", _wrapped_rules_layer)

    with pytest.raises(_StopAtStage4):
        AR.run_pipeline(asof="2026-12-01", run_dir=tmp_path / "run",
                        live_trade=False, refresh_scores=False)
    return calls, captured


def test_分析链与规则层必须共用同一组ETF参数(tmp_path: Path, monkeypatch) -> None:
    """🔴 **运行时断言**：``analyze.py`` 与 ``recommend.py`` 拿到的 ETF 层参数
    **逐字相同**，且 = 冻结件冠军口径。

    🔴 为什么必须有这条：历史上真出过「规则层 11-03 无平滑 vs 交付层 11-05 有平滑」
       的分叉 ⇒ 交付件与规则选出不同的票（**而且不报错**）。
       ⇒ 光有「冻结件能翻译成命令行」的用例**不够** ⇒ 必须钉死**两链拿到的是同一组**。
    """
    p = _write_frozen(tmp_path)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_REQUIRED", True)
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_FILE", p)

    calls, captured = _capture_pipeline_etf_args(tmp_path, monkeypatch)

    analyze = next(c for c in calls if c["step"] == AR.STEP_ANALYZE)
    rules = next(c for c in calls if c["step"] == AR.STEP_RULES)
    analyze_etf = _etf_flag_slice(analyze["cmd"])
    rules_etf = _etf_flag_slice(rules["cmd"])

    assert analyze_etf == rules_etf, (
        "🔴 分析链与规则层的 ETF 层参数**不一致** ⇒ 两链分叉：\n"
        f"  analyze.py  : {analyze_etf}\n"
        f"  recommend.py: {rules_etf}"
    )
    assert captured.get("etf_args") is not None, "规则层必须显式拿到 etf_args"
    assert analyze_etf == _etf_flag_slice(list(captured["etf_args"]))

    pairs = _etf_pairs(analyze_etf)
    assert pairs["--alpha-weight"] == "1"
    assert pairs["--momentum-weight"] == "0"
    assert pairs["--hold-buffer"] == "2"
    assert pairs["--target-n-holdings"] == "5"
    assert pairs["--max-per-theme"] == "0"
    assert pairs["--entry-ma-window"] == "0"
    assert pairs["--exit-ma-window"] == "0"
    assert pairs.get("--no-smoothing") is True
    for flag in _V1_SMOOTHING_FLAGS:
        assert flag not in pairs, f"🔴 接缝开着却仍带 V1 老平滑参数 `{flag}`"


def test_规则层漏传参数在接缝开启时必须中止(monkeypatch) -> None:
    """🔴 **fail-closed**：接缝开着却漏传 ``etf_args`` ⇒ 抛异常（不许静默回落 V1）。"""
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", True)
    with pytest.raises(V2FP.V2FrozenParamsError) as exc:
        AR._resolve_rules_etf_args(None)
    assert "规则层" in str(exc.value)


def test_接缝关闭时规则层仍可回落V1老参数(monkeypatch) -> None:
    """🔴 接缝**关着** ⇒ 回落 V1 老配置是**明确**的老口径（不是漏传）⇒ 允许。"""
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", False)
    args = AR._resolve_rules_etf_args(None)
    assert "--smoothing-alpha" in args
    assert "--no-smoothing" not in args


def test_规则层显式传参时原样透传(monkeypatch) -> None:
    monkeypatch.setattr(DC, "V2_FROZEN_PARAMS_ENABLED", True)
    given = ["--alpha-weight", "1", "--no-smoothing"]
    assert AR._resolve_rules_etf_args(given) == given


def test_两链参数同源AST守卫() -> None:
    """🔴 **AST 守卫**：``run_pipeline`` 里分析链与规则层**必须**用同一个 ``etf_args``。

    🔴 运行时断言只覆盖「今天这一版」；AST 守卫覆盖「以后重构时**手滑漏传**」
       （漏传 ⇒ 规则层回落 V1 ⇒ 两链分叉）。
    """
    import ast

    tree = ast.parse((_DAILY / "agents_rebalance.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_pipeline")

    # ① ``etf_args, ... = _etf_layer_args()`` —— 唯一来源
    sourced = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        callee = getattr(node.value.func, "id", None) or getattr(node.value.func, "attr", None)
        if callee != "_etf_layer_args":
            continue
        for target in node.targets:
            if isinstance(target, ast.Tuple):
                names = [e.id for e in target.elts if isinstance(e, ast.Name)]
                sourced = sourced or ("etf_args" in names)
    assert sourced, "run_pipeline 必须从 `_etf_layer_args()` 取 etf_args"

    # ② 规则层调用**必须**显式传 `etf_args=etf_args`
    rules_calls = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and getattr(n.func, "id", None) == "_run_rules_layer"]
    assert rules_calls, "找不到 `_run_rules_layer` 调用"
    for call in rules_calls:
        kwargs = {k.arg: k.value for k in call.keywords}
        assert "etf_args" in kwargs, "🔴 规则层调用漏传 `etf_args` ⇒ 会回落 V1 ⇒ 两链分叉"
        assert isinstance(kwargs["etf_args"], ast.Name)
        assert kwargs["etf_args"].id == "etf_args", "规则层必须用同一个 etf_args 变量"

    # ③ 分析链的命令行**必须**把 `etf_args` 拼进去
    analyze_calls = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_run_cmd"):
            continue
        for kw in node.keywords:
            if kw.arg != "step":
                continue
            # ``step=STEP_ANALYZE``（模块级 Name）/ ``step=AR.STEP_ANALYZE``（属性）
            is_analyze = (
                isinstance(kw.value, ast.Name) and kw.value.id == "STEP_ANALYZE"
            ) or (
                isinstance(kw.value, ast.Attribute) and kw.value.attr == "STEP_ANALYZE"
            ) or (
                isinstance(kw.value, ast.Constant) and kw.value.value == AR.STEP_ANALYZE
            )
            if is_analyze:
                analyze_calls.append(node)
    assert len(analyze_calls) == 1, "分析链 `_run_cmd` 调用必须唯一"
    used = {n.id for n in ast.walk(analyze_calls[0].args[0]) if isinstance(n, ast.Name)}
    assert "etf_args" in used, "🔴 分析链命令行里没有 etf_args ⇒ 分析链/规则层会分叉"
