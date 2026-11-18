"""🔴 V2 特征清单（Alpha158）与**特征消融**（对齐①）。

**这是 V2，与 V1 无关**：本模块只负责「特征名 → 表达式」这一层，
不 import、不改写 V1 的 ``qlib_scores.py`` / ``engine/features.py``
（V1 冻结为 baseline，见 ``v2/__init__.py`` 的硬隔离说明）。

口径（逐条写死，报告里照抄）：

  * **特征源 = qlib 上游** ``qlib/contrib/data/loader.py`` 的
    ``Alpha158DL.get_feature_config()``（**只读 import**）。
    🔴 类名实际叫 ``Alpha158DL``（不是文档里常写的 ``Alpha158``），
    返回值是 ``(fields, names)`` **两个平行 list**（不是 ``(name, expr)`` 元组列表）
    ⇒ 本模块在这里做一次**形状归一**，下游只认 ``[(name, expr), ...]``。
    好处：清单与 qlib 自带 Alpha158 **逐位一致**（158 个），不手抄 ⇒ 不会漂移。
  * 🔴 **「5 日内超短线反转」特征 = ``ROC5``**：
    Alpha158 的 ROC 族表达式是 ``Ref($close, N)/$close``（**倒数型**涨跌幅）
    ⇒ 近期下跌 ⇒ 取值 > 1 ⇒ **高 ROC5 = 超短线反转信号**。
    V2 把它列为**消融对象**：**默认口径 = 消融 ROC5**（``config_v2.DROPPED_FEATURES`` /
    ``ABLATION_WINNER = "ablate_roc5"``），``baseline_full``（全量特征）降为**对照臂**；
    胜者由 **val 段 RankIC** 判定（``run_v2.stage_ablation``）⇒ 默认口径是
    「**val 段实测选出来的**」，不是拍脑袋写死的。
  * 消融 = **按特征名精确剔除** ⇒ ``ROC5/ROC10/ROC20`` 与同窗口的
    ``MA5/MA10/MA20``、``STD5/STD10/STD20`` **互不牵连**
    （消融组已在 ``config_v2.ABLATION_GROUPS`` 里定死，本模块不许改口径）。
  * 🔴 **未知名不许静默忽略**：``drop`` 里出现清单里没有的特征名 ⇒
    ``alpha158_feature_config`` **直接 raise**（静默忽略会让「消融报告」里的
    ``n_kept`` 与真正跑出来的特征数对不上 ⇒ 消融实验结论失真）；
    ``ablation_report`` 作为**披露型**接口则不 raise，而是在 ``dropped_missing``
    里如实列出（两个接口的分工见各自 docstring）。
"""

from __future__ import annotations

from qlib.contrib.data.loader import Alpha158DL

try:  # 扁平导入（v2 目录进 sys.path，与 labels.py / splits.py 同口径）
    import config_v2 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v2 as C


#: qlib 表达式里 ROC 族的**标准写法**（去空格后比较）⇒ 上游若改了 ROC 语义要报错
ROC5_EXPRESSION = "Ref($close,5)/$close"


def _alpha158_baseline() -> list[tuple[str, str]]:
    """qlib Alpha158 的**全量**特征清单（``[(name, expr), ...]``）。

    🔴 每次都**现取**（``get_feature_config`` 是毫秒级纯计算，不值得缓存成模块常量），
    这样上游清单一变，本模块立即跟着变 ⇒ 不会留下「手抄的旧清单」。

    形状归一：qlib 返回 ``(fields, names)`` 两个平行 list ⇒ 这里转成
    ``[(name, expr), ...]``，并断言 **ROC5 必须存在**（它是 V2 的消融锚点）。
    """
    fields, names = Alpha158DL.get_feature_config()
    cfg = [(str(name), str(expr)) for name, expr in zip(names, fields)]
    if len(cfg) != len(names) or len(cfg) != len(fields):
        raise AssertionError(
            f"🔴 Alpha158 清单形状异常：names={len(names)} fields={len(fields)}"
        )
    mapping = dict(cfg)
    if C.REVERSAL_FEATURE not in mapping:
        raise AssertionError(
            f"🔴 Alpha158 清单里没有消融锚点 {C.REVERSAL_FEATURE} ⇒ "
            f"「5 日内超短线反转」消融实验无法执行（上游 Alpha158 已变？）"
        )
    actual = mapping[C.REVERSAL_FEATURE].replace(" ", "")
    if actual != ROC5_EXPRESSION:
        raise AssertionError(
            f"🔴 {C.REVERSAL_FEATURE} 的表达式变成了 {mapping[C.REVERSAL_FEATURE]!r}，"
            f"与预期的 {ROC5_EXPRESSION!r} 不符 ⇒ 消融口径（5 日反转）已失效"
        )
    return cfg


def _select_features(cfg: list[tuple[str, str]], drop: tuple[str, ...]) -> list[tuple[str, str]]:
    """按 ``drop`` **精确剔名**（特征名逐字匹配，命中才剔）。"""
    drop_set = set(drop)
    return [(name, expr) for name, expr in cfg if name not in drop_set]


def _check_drop_names(drop: tuple[str, ...], available: set[str]) -> list[str]:
    """返回 ``drop`` 里**不存在**的特征名（供 raise 时点名）。"""
    return [str(name) for name in drop if str(name) not in available]


def alpha158_feature_config(drop: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """返回 qlib Alpha158 的 ``(name, expression)`` 列表，按 ``drop`` 里的特征名剔除。

    参数
    ----
    drop : 要剔除的特征名（精确匹配；空元组 = **全量基线** = 默认口径）

    返回
    ----
    ``[(name, expression), ...]`` —— 顺序与 qlib 原始清单一致（不打乱）。

    🔴 **未知名 ⇒ raise**（``ValueError``）：消融组名写错时必须当场炸掉，
    不许悄悄少剔一个特征（那会让「消融 ROC 族」的实验结论变成另一个实验）。
    """
    cfg = _alpha158_baseline()
    missing = _check_drop_names(drop, {name for name, _ in cfg})
    if missing:
        raise ValueError(
            f"🔴 要剔除的特征不在 Alpha158 清单里：{missing}（不许静默忽略）"
        )
    return _select_features(cfg, tuple(str(name) for name in drop))


def feature_expressions(drop: tuple[str, ...] = ()) -> list[str]:
    """只返回**表达式字符串**列表（qlib ``D.features`` 可直接吃）。"""
    return [expr for _, expr in alpha158_feature_config(drop)]


def ablation_report(drop: tuple[str, ...] = ()) -> dict:
    """消融口径摘要（给报告用的**披露型**接口，键名固定）。

    返回
    ----
    dict：``dropped``（请求剔除的特征名）、``n_total``（基线特征数）、
    ``n_kept``（剔除后特征数）、``dropped_found``（**真的剔掉**的）、
    ``dropped_missing``（**清单里没有**的）。

    🔴 与 ``alpha158_feature_config`` 的分工：这里**不 raise**，
    而是在 ``dropped_missing`` 里如实点名 ⇒ 报告永远渲染得出来，
    且「消融没生效」这件事**不会**被藏起来。
    真正拿去训练的特征清单仍走 ``alpha158_feature_config``（那里未知名会 raise）。
    """
    cfg = _alpha158_baseline()
    names = {name for name, _ in cfg}
    requested = [str(name) for name in drop]
    found = [name for name in requested if name in names]
    missing = [name for name in requested if name not in names]
    kept = _select_features(cfg, tuple(found))
    return {
        "dropped": requested,
        "n_total": len(cfg),
        "n_kept": len(kept),
        "dropped_found": found,
        "dropped_missing": missing,
    }
