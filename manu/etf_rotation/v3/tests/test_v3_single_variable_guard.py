"""🔴 V3 **单变量对照纪律**的回归测试（消融臂能不能归因）。

【为什么必须有这个文件】
  2026-11-26 只读审计发现：``v3_mse`` 消融臂被标成「单变量：只换损失函数」，
  但它实际走的是 ``config_v3.CONTROL_LGB_PARAMS`` ⇒ 与主臂 ``LGB_PARAMS``
  **差 8 个超参**（num_leaves / max_depth / min_data_in_leaf / colsample /
  subsample / lambda_l1 / lambda_l2 / metric）
  ⇒ 差异**无法归因到损失函数**（可能是正则强度）⇒ 消融结论会被误判。
  修法：新增 ``config_v3.MSE_ARM_LGB_PARAMS``（与 ``LGB_PARAMS`` 逐项相同，
  只差 objective/metric）⇒ 本文件钉住「以后不许再漂移」。

【本文件钉的判定】
  ① ``MSE_ARM_LGB_PARAMS`` 与 ``LGB_PARAMS`` 的差异**只在** objective / metric
     （主臂独有的 ``ndcg_eval_at`` 不许出现在 MSE 臂）；
  ② MSE 臂的每个超参值都与主臂**逐项相等**（不许偷改 max_depth 之类）；
  ③ ``train_s`` 的 MSE 分支**必须**用 ``MSE_ARM_LGB_PARAMS``；
  ④ ``model.train_regressor`` 的默认回退**必须**是 ``MSE_ARM_LGB_PARAMS``；
  ⑤ 🔴 **同源派生**：``MSE_ARM_LGB_PARAMS`` 必须**由** ``LGB_PARAMS`` 派生
     （源码层断言 ⇒ 不许各写一份、以后漂移）。

【口径提醒】纯常量/源码断言，不 import qlib、不联网、不读 /mnt/data。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_v3 as C  # noqa: E402

#: 两个臂之间**允许**不同的键（objective / metric 是「要换的那个变量」本身）
ALLOWED_DIFF_KEYS = {"objective", "metric"}
#: 主臂独有、MSE 臂**不许**有的键（ndcg 是排序专用）
MAIN_ONLY_KEYS = {"ndcg_eval_at"}


def test_mse_arm_differs_only_in_objective_and_metric():
    """① 差异键集合 ⊆ {objective, metric}。"""
    main = dict(C.LGB_PARAMS)
    mse = dict(C.MSE_ARM_LGB_PARAMS)
    diff = {k for k in set(main) | set(mse)
            if main.get(k) != mse.get(k)} - MAIN_ONLY_KEYS
    assert diff <= ALLOWED_DIFF_KEYS, f"🔴 MSE 臂多出了非单变量差异：{sorted(diff)}"
    for key in MAIN_ONLY_KEYS:
        assert key not in mse, f"🔴 MSE 臂不许带排序专用参数 {key}"


def test_mse_arm_params_are_itemwise_equal_to_main_arm():
    """② 逐项相等（数值/列表/元组都逐字比）。"""
    main = dict(C.LGB_PARAMS)
    mse = dict(C.MSE_ARM_LGB_PARAMS)
    for key, value in main.items():
        if key in ALLOWED_DIFF_KEYS or key in MAIN_ONLY_KEYS:
            continue
        assert mse[key] == value, f"🔴 超参 {key} 在两臂之间不一致：{value!r} vs {mse[key]!r}"
    assert mse["objective"] == "mse"
    assert mse["metric"] == "l2"
    assert main["objective"] == "lambdarank"


def test_train_s_mse_branch_uses_mse_arm_params():
    """③ ``train_s`` 的 MSE 分支必须用 ``MSE_ARM_LGB_PARAMS``。"""
    src = (Path(__file__).resolve().parents[1] / "train_s.py").read_text(encoding="utf-8")
    assert "params=dict(C.MSE_ARM_LGB_PARAMS)" in src
    assert "params=dict(C.CONTROL_LGB_PARAMS)" not in src


def test_model_regressor_fallback_is_mse_arm_params():
    """④ ``model.train_regressor`` 的默认回退 = ``MSE_ARM_LGB_PARAMS``。"""
    src = (Path(__file__).resolve().parents[1] / "model.py").read_text(encoding="utf-8")
    assert "_base_params(params, dict(C.MSE_ARM_LGB_PARAMS))" in src
    assert "_base_params(params, dict(C.CONTROL_LGB_PARAMS))" not in src


def test_mse_arm_params_are_derived_from_main_arm():
    """⑤ 🔴 同源派生（源码断言）：不许各写一份常量。"""
    src = (Path(__file__).resolve().parents[1] / "config_v3.py").read_text(encoding="utf-8")
    assert "MSE_ARM_LGB_PARAMS = {" in src
    assert "LGB_PARAMS.items()" in src
