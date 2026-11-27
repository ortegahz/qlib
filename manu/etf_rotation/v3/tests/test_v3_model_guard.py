"""🔴 V3 模型层（lambdarank / MSE 两条臂）的**守卫测试**。

**本文件钉的判定**
  ① **输入契约先于依赖**：分组行数 / grades 越界 / 权重长度错位 ⇒ **先报错**，
     再谈训练（不许「先 import lightgbm 再校验」⇒ 契约错位会被依赖缺失掩盖）；
  ② **不许 early_stopping**：qlib 会把 [train, valid] 一起塞进 ``valid_sets``
     ⇒ 早停 = 隐性泄漏 ⇒ 模型层源码里不许出现 ``early_stopping`` / ``valid_sets``；
  ③ **排序信号真的进了模型**（变异反证）：把「有信息的 grade」换成**全常数**
     ⇒ 预测必须**塌成常数**（退化行为确定）⇒ 反证「grade 确实在驱动排序」；
  ④ 训练/预测的形状契约（100 行 × 3 特征 / 2 个组）。

🔴 **不依赖 qlib、不读服务器数据、不联网**；🔴 **不许跑长训练**（``num_boost_round=5``）。
⛔ 本机若**没装 lightgbm** ⇒ 依赖训练的用例 **skip**（不静默通过、不伪造结果）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_V3_DIR = Path(__file__).resolve().parents[1]
if str(_V3_DIR) not in sys.path:
    sys.path.insert(0, str(_V3_DIR))

import config_v3 as C  # noqa: E402
import model as M  # noqa: E402

#: 合成数据规模（🔴 小到秒级；不许拖成真训练）
N_ROWS = 100
N_FEATURES = 3
N_GROUPS = 2
ROUNDS = 5


def _frame(seed: int = 7) -> pd.DataFrame:
    """合成特征矩阵（randn，100 行 × 3 特征）。"""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.standard_normal((N_ROWS, N_FEATURES)),
                        columns=[f"f{i}" for i in range(N_FEATURES)])


def _groups() -> list[int]:
    """两个交易日组（各 50 行）。"""
    size = N_ROWS // N_GROUPS
    return [size] * (N_GROUPS - 1) + [N_ROWS - size * (N_GROUPS - 1)]


def _informative_grades(X: pd.DataFrame) -> np.ndarray:
    """**有信息**的 relevance（与第 1 个特征同向）⇒ 排序信号确实存在。"""
    signal = X.iloc[:, 0].to_numpy(dtype=float)
    ranks = pd.Series(signal).rank(method="average").to_numpy()
    percentile = (ranks - 1.0) / float(len(ranks))
    return np.floor(percentile * int(C.RANK_GRADE_COUNT)).astype(np.int64)


# ----------------------------------------------------------------- ① 输入契约
class TestInputContract:
    def test_默认label_gain是线性档位(self) -> None:
        assert M.default_label_gain() == list(range(int(C.RANK_GRADE_COUNT)))
        assert M.default_label_gain() == [0, 1, 2, 3, 4]

    def test_分组行数与特征行数不符必须报错(self) -> None:
        with pytest.raises(ValueError, match="分组行数合计"):
            M.build_rank_dataset(_frame(), np.zeros(N_ROWS, dtype=np.int64), [10, 10])

    def test_空分组必须报错(self) -> None:
        with pytest.raises(ValueError, match="每个交易日组"):
            M.build_rank_dataset(_frame(), np.zeros(N_ROWS, dtype=np.int64), [0, N_ROWS])

    def test_grade越界必须报错(self) -> None:
        bad = np.full(N_ROWS, int(C.RANK_GRADE_COUNT), dtype=np.int64)
        with pytest.raises(ValueError, match="grades 超出"):
            M.build_rank_dataset(_frame(), bad, _groups())

    def test_权重长度错位必须报错(self) -> None:
        with pytest.raises(ValueError, match="weights 长度"):
            M.build_rank_dataset(_frame(), np.zeros(N_ROWS, dtype=np.int64), _groups(),
                                 weights=np.ones(N_ROWS - 1))

    def test_负权重必须报错(self) -> None:
        weights = np.ones(N_ROWS)
        weights[0] = -1.0
        with pytest.raises(ValueError, match="weights 不得为负"):
            M.build_rank_dataset(_frame(), np.zeros(N_ROWS, dtype=np.int64), _groups(),
                                 weights=weights)

    def test_标签与特征行数不符必须报错(self) -> None:
        with pytest.raises(ValueError, match="标签长度"):
            M.train_regressor(_frame(), np.zeros(N_ROWS - 1))


# ----------------------------------------------------------------- ② 泄漏守卫
class TestNoEarlyStopping:
    def test_模型层不许把valid_sets或early_stopping传给lightgbm(self) -> None:
        """🔴 早停 = 隐性泄漏 ⇒ 用 **AST 关键字参数**扫描（文档里提到不算违规）。"""
        tree = ast.parse((_V3_DIR / "model.py").read_text(encoding="utf-8"))
        banned = {"early_stopping", "valid_sets", "callbacks", "valid_names"}
        hits = [
            f"{node.func.attr}#{kw.arg}"
            for node in ast.walk(tree) if isinstance(node, ast.Call)
            for kw in node.keywords if kw.arg in banned
        ]
        assert not hits, f"🔴 模型层把 {hits} 传给了 LightGBM（早停 ⇒ 隐性泄漏）"

    def test_早停常量恒为None(self) -> None:
        assert C.EARLY_STOPPING_ROUNDS is None

    def test_超参取自config_v3(self) -> None:
        src = (_V3_DIR / "model.py").read_text(encoding="utf-8")
        assert "LGB_PARAMS" in src and "CONTROL_LGB_PARAMS" in src


# ----------------------------------------------------------------- ③④ 训练与预测
try:  # 🔴 本机可能没装 lightgbm ⇒ 只有「依赖训练」的用例 skip，其余照跑
    import lightgbm  # noqa: F401

    _HAS_LGB = True
except ModuleNotFoundError:  # pragma: no cover - 取决于运行环境
    _HAS_LGB = False


@pytest.mark.skipif(not _HAS_LGB,
                    reason="本机未装 lightgbm（服务器 conda 环境 `ql` 有 4.7.0）")
class TestTrainAndPredict:
    def test_排序臂训得出booster且预测长度正确(self) -> None:
        X = _frame()
        grades = _informative_grades(X)
        params = {**C.LGB_PARAMS, "learning_rate": 0.3,
                   # 🔴 **小样本必须下调叶最小样本数**：生产 `min_data_in_leaf=200`
                   #    （每折 ~1.18e6 行）是对的，但本合成集只有 **100 行**
                   #    ⇒ LightGBM 一个叶子都分裂不出来 ⇒ 预测**恒 0**
#    （实测 `min_data_in_leaf=1` 会触发 LightGBM 内部断言 `left_count > 0`
#     ⇒ 选 **5**；服务器实测 ptp=0.54 ⇒ 预测确实分散）
                   #    （2026-11-26 服务器实测：唯一真红就是这个 ⇒ 已定性为
                   #     **测试构造问题**，**不是** lambdarank 退化；生产超参不动）。
                   "min_data_in_leaf": 5, "min_sum_hessian_in_leaf": 0}
        booster = M.train_ranker(X, grades, _groups(), params=params, num_boost_round=ROUNDS)
        assert booster is not None
        assert booster.num_trees() >= 1
        pred = M.predict_scores(booster, X)
        assert isinstance(pred, np.ndarray)
        assert pred.shape == (N_ROWS,)
        assert np.isfinite(pred).all()

    def test_对照臂训得出booster且预测长度正确(self) -> None:
        X = _frame()
        y = X.iloc[:, 0].to_numpy(dtype=float)
        params = {**C.CONTROL_LGB_PARAMS, "learning_rate": 0.3}
        booster = M.train_regressor(X, y, params=params, num_boost_round=ROUNDS)
        assert booster is not None
        pred = M.predict_scores(booster, X)
        assert pred.shape == (N_ROWS,)
        assert np.isfinite(pred).all()

    def test_变异反证_常数grade必须让预测塌成常数(self) -> None:
        """把有信息的 grade 换成**全常数** ⇒ 排序信号消失 ⇒ 预测必须退化成常数。"""
        X = _frame()
        params = {**C.LGB_PARAMS, "learning_rate": 0.3,
                   # 🔴 **小样本必须下调叶最小样本数**：生产 `min_data_in_leaf=200`
                   #    （每折 ~1.18e6 行）是对的，但本合成集只有 **100 行**
                   #    ⇒ LightGBM 一个叶子都分裂不出来 ⇒ 预测**恒 0**
#    （实测 `min_data_in_leaf=1` 会触发 LightGBM 内部断言 `left_count > 0`
#     ⇒ 选 **5**；服务器实测 ptp=0.54 ⇒ 预测确实分散）
                   #    （2026-11-26 服务器实测：唯一真红就是这个 ⇒ 已定性为
                   #     **测试构造问题**，**不是** lambdarank 退化；生产超参不动）。
                   "min_data_in_leaf": 5, "min_sum_hessian_in_leaf": 0}
        booster = M.train_ranker(X, np.zeros(N_ROWS, dtype=np.int64), _groups(),
                                 params=params, num_boost_round=ROUNDS)
        pred = M.predict_scores(booster, X)
        assert float(np.ptp(pred)) == 0.0, (
            f"常数 grade 下预测仍分散（ptp={float(np.ptp(pred)):.3e}）"
            "⇒ 排序信号没有真正来自 grade（守卫失效）"
        )

    def test_有信息grade时预测不得是常数(self) -> None:
        """对照组：grade 带信息 ⇒ 预测必须分散（否则上面的变异反证没有意义）。"""
        X = _frame()
        grades = _informative_grades(X)
        params = {**C.LGB_PARAMS, "learning_rate": 0.3,
                   # 🔴 **小样本必须下调叶最小样本数**：生产 `min_data_in_leaf=200`
                   #    （每折 ~1.18e6 行）是对的，但本合成集只有 **100 行**
                   #    ⇒ LightGBM 一个叶子都分裂不出来 ⇒ 预测**恒 0**
#    （实测 `min_data_in_leaf=1` 会触发 LightGBM 内部断言 `left_count > 0`
#     ⇒ 选 **5**；服务器实测 ptp=0.54 ⇒ 预测确实分散）
                   #    （2026-11-26 服务器实测：唯一真红就是这个 ⇒ 已定性为
                   #     **测试构造问题**，**不是** lambdarank 退化；生产超参不动）。
                   "min_data_in_leaf": 5, "min_sum_hessian_in_leaf": 0}
        booster = M.train_ranker(X, grades, _groups(), params=params, num_boost_round=ROUNDS)
        pred = M.predict_scores(booster, X)
        assert float(np.ptp(pred)) > 0.0

    def test_权重参与训练且不改变预测形状(self) -> None:
        X = _frame()
        grades = _informative_grades(X)
        weights = np.linspace(0.5, 2.0, N_ROWS)
        params = {**C.LGB_PARAMS, "learning_rate": 0.3,
                   # 🔴 **小样本必须下调叶最小样本数**：生产 `min_data_in_leaf=200`
                   #    （每折 ~1.18e6 行）是对的，但本合成集只有 **100 行**
                   #    ⇒ LightGBM 一个叶子都分裂不出来 ⇒ 预测**恒 0**
#    （实测 `min_data_in_leaf=1` 会触发 LightGBM 内部断言 `left_count > 0`
#     ⇒ 选 **5**；服务器实测 ptp=0.54 ⇒ 预测确实分散）
                   #    （2026-11-26 服务器实测：唯一真红就是这个 ⇒ 已定性为
                   #     **测试构造问题**，**不是** lambdarank 退化；生产超参不动）。
                   "min_data_in_leaf": 5, "min_sum_hessian_in_leaf": 0}
        booster = M.train_ranker(X, grades, _groups(), weights=weights, params=params,
                                 num_boost_round=ROUNDS)
        assert M.predict_scores(booster, X).shape == (N_ROWS,)
