# CMDS.md — qlib 量化研究工程 · 可运行命令

> 规约见 `manu/AGENTS.md`；铁律见 `manu/memory/rules.md`。
> **工作方式**：只改本地 `/media/manu/1TB-Volume/workspace/qlib/`，改完同步到服务器再跑。

## env（服务器）

```bash
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40
source /home/huangzhe/anaconda3/etc/profile.d/conda.sh && conda activate ql
cd /tmp/pycharm_project_658d3fc4
```

> ⚠️ `ssh` 必须加 `-F /dev/null`（本机 ssh_config.d 权限不对）；服务器 sshd 在 **32222**，22 端口关闭。

## 安装 / 复装 qlib（服务器）

```bash
cd /tmp/pycharm_project_658d3fc4
SETUPTOOLS_SCM_PRETEND_VERSION=0.9.8 pip install -e ".[dev]"
```

> 服务器工程目录**无 `.git`** ⇒ 必须给 `SETUPTOOLS_SCM_PRETEND_VERSION=0.9.8`，
> 否则 setuptools_scm 探测不到版本而失败。

## 同步本地改动到服务器

```bash
cd /media/manu/1TB-Volume/workspace/qlib
bash manu/tools/sync_to_server.sh              # 同步 manu/ 全目录
bash manu/tools/sync_to_server.sh manu/memory  # 只同步子路径
bash manu/tools/sync_to_server.sh --dry-run    # 预演
bash manu/tools/sync_to_server.sh --check      # 只做 md5 双侧比对
```

## 冒烟验证

```bash
export PATH=/home/huangzhe/anaconda3/envs/ql/bin:$PATH
cd /tmp/pycharm_project_658d3fc4
python -c "import qlib; print(qlib.__version__, qlib.__file__)"   # → 0.9.8 + 工程目录
qlib.init()                                                        # → successfully initialized
qrun --help                                                        # → 用法
python -c "from qlib.contrib.model import LGBModel; print('ok')"   # → ok
```

## 数据集

```bash
# 官方 cn_data 因数据安全策略暂时下线；社区镜像：
wget https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz
mkdir -p ~/.qlib/qlib_data/cn_data
tar -zxvf qlib_bin.tar.gz -C ~/.qlib/qlib_data/cn_data --strip-components=1
```

> 数据集路径**一律显式指定**（`qlib.init(provider_uri=...)`），不依赖默认值
> `~/.qlib/qlib_data/cn_data`。**下载数据属重活，需用户当轮授权。**

## 跑官方示例

```bash
export PATH=/home/huangzhe/anaconda3/envs/ql/bin:$PATH
cd /tmp/pycharm_project_658d3fc4
python examples/benchmarks/LGBModel/workflow_config_lightgbm_Alpha158.yaml
```

> 🔴 **回测/训练属重活，必须用户当轮明确授权**（铁律一）。
> 引用任何回测数字前必须先填满 `manu/memory/rules.md` §十二·12.1 的六项口径。

## A 股截面打分 + Top-K 选股（本工程自研流水线）

> 代码：`manu/experiments/ashare_topk_selection/`（config.py / run_pipeline.py / export_topk.py）
> 口径与结果：`manu/memory/ashare_topk_20261104.md`
> ⚠️ 训练 + 回测属**重活**，需用户当轮授权；产物落 `/mnt/data/siping/runs/ashare_topk/runs/<recorder_id>/`。

```bash
export PATH=/home/huangzhe/anaconda3/envs/ql/bin:$PATH
cd /tmp/pycharm_project_658d3fc4

# 0) 数据体检（校准区间/股票池/字段，产出 JSON 报告）
python manu/experiments/ashare_topk_selection/inspect_dataset.py \
  --provider-uri /mnt/data/siping/datasets/manu/etf_rotation/datasets/qlib/cn_data

# 1) 训练 + 打分 + 回测（一步产出 label/model/pred + manifest.json）
python manu/experiments/ashare_topk_selection/run_pipeline.py

# 2) 导出「模型打分 Top-N 排名」（kind=model_score_ranking，非策略实际持仓）
python manu/experiments/ashare_topk_selection/export_topk.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id> --top 5

# 2b) test 段每个交易日的 Top-N CSV
python manu/experiments/ashare_topk_selection/export_topk.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id> --top 30 --all-dates
```

> 🔴 `export_topk.py` 的信号只含 **test 段**（`LGBModel.predict` 默认 `segment="test"`）。
> 🔴 引用回测数字前必须先填满 `manu/memory/rules.md` §十二·12.1 的六项口径。

## 换手 / 成本分解（只读产物，不重训）

```bash
# 🔴 回测必须钉 PYTHONHASHSEED=0：qlib 的 Position.get_stock_list 用 set()，
#    迭代序随进程随机 ⇒ 不钉则回测**跨进程不可复现**（实测同 arm 两进程差 3.65pp）。
#    详见 manu/memory/rules.md §12.10。
export PYTHONHASHSEED=0
cd /tmp/pycharm_project_658d3fc4

# 1) 换手率与成本对账（单 arm，基线 topk=30/n_drop=5）
python manu/experiments/ashare_topk_selection/turnover_analysis.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>

# 2) 降换手 sweep（14 组 arm × valid/test 两段；选参只看 valid）
python manu/experiments/ashare_topk_selection/turnover_sweep.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>

# 3) valid 段分年稳定性（不引用 test ⇒ 用于识别跨子区间稳健的参数）
python manu/experiments/ashare_topk_selection/valid_stability.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>
```

> ⚠️ 这三个脚本都会**打确定性补丁**（`determinism.py`，运行期生效，上游源码零改动）。
> ⚠️ 脚本**只输出排序表与推荐**，**不会**自动改 `config.py`。

## 数据集下载（服务器直下，禁走 host 代理）

```bash
# 服务器上执行；GitHub release 的 302 链会挂死 ⇒ 走 api.github.com 取签名直链 + aria2c 多连接
python manu/tools/fetch_qlib_dataset.py --root /mnt/data/siping/datasets/manu/etf_rotation/datasets/qlib
```

> 来源与 md5 留痕在 `<root>/fetch_manifest.json`。

## ETF 轮动线（人工低频，3~5 只 ETF；服务器）

> 代码在 `manu/etf_rotation/`；铁律见 `manu/memory/rules.md §十四`；
> 事实与结论见 `manu/memory/etf_rotation_20261104.md`。
> ⚠️ **akshare 只能用 env `ak`**（`/home/huangzhe/anaconda3/envs/ak/bin/python`），
> **绝不能装进 `ql` env**（会把 numpy 降级、拖垮 qlib）。

```bash
# ---- 服务器（重活一律上服务器）----
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40
cd /tmp/pycharm_project_658d3fc4
export PYTHONHASHSEED=0
PY=/home/huangzhe/anaconda3/envs/ql/bin/python
AK=/home/huangzhe/anaconda3/envs/ak/bin/python

# 1) ETF 数据底座（akshare，服务器直下，禁走 host 代理）—— 顺序不可交换
$AK manu/etf_rotation/data/fetch_etf_universe.py --stage all
$AK manu/etf_rotation/data/fetch_etf_prices.py    --resume
$AK manu/etf_rotation/data/fetch_etf_universe.py --stage finalize
$AK manu/etf_rotation/data/fetch_etf_holdings.py --resume

# 2) 全市场个股打分 S（上游；~35 min，产物 stock_scores.csv）
$PY manu/etf_rotation/qlib_scores.py --mode infer --out-dir /mnt/data/siping/runs/etf_rotation_results

# 3) 当期调仓建议（中文清单 + 金额）
$PY manu/etf_rotation/recommend.py --asof 2026-11-03 \
    --out-dir /mnt/data/siping/runs/etf_rotation_results

# 4) 回测：主口径 / 压力成本 / 消融
$PY manu/etf_rotation/backtest.py --tag base \
    --rebalance-every 2 --alpha-weight 0.5 --momentum-weight 0.5 --hold-buffer 2
$PY manu/etf_rotation/backtest.py --tag stress \
    --rebalance-every 2 --alpha-weight 0.5 --momentum-weight 0.5 --hold-buffer 2 \
    --commission 0.0005 --slippage 0.0020
# 消融：--alpha-weight 0.0（纯动量）/ 1.0（纯 E）；--rebalance-every 1（双周）/ 4（季频）
```

产物：`/mnt/data/siping/runs/etf_rotation_results/`
- `stock_scores.csv`（全市场个股分数，374,793 行 / 69 个信号日）
- `etf_rotation_backtest_<tag>.json`（含 `segment_report` / `n_failed_rebalances` / 偏差披露）
- `etf_rotation_nav_<tag>.csv`（日频 NAV：portfolio / benchmark / excess）
- `rebalance_recommendation.json|.csv`（当期清单 + 金额）

> 🔴 引用回测数字**必须同时给出**口径六项 + 样本量（调仓期数）+ **分段稳定性** + 选择偏差声明。

## ETF 自动化流水线（2026-11-04 新增：qlib → ETF 报告 → TradingAgents → 调仓建议）

# 服务器（重活一律上服务器；host 18:30 关机 ⇒ 夜间链在服务器自主跑）
cd /tmp/pycharm_project_658d3fc4

# 1) 全市场个股打分 S（上游；产物 /mnt/data/siping/runs/etf_rotation_results/stock_scores.csv）
export PYTHONHASHSEED=0
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/qlib_scores.py --mode infer

# 2) ETF 分析报告（= 喂给 TradingAgents 的正文；JSON + MD）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/analyze.py \
    --asof 2026-11-03 --out-dir /mnt/data/siping/runs/etf_rotation_results

# 3) 当期调仓建议（真实持仓口径；--holdings 权重 = account.py::current_weights()）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/recommend.py --asof 2026-11-03 \
    --pool-mode e_covered --entry-ma-window 20 --exit-ma-window 60 \
    --holdings "512800:0.2254,159929:0.1117,513330:0.0760,513770:0.0373,512690:0.0298"

# 3b) 穿透分时间平滑（2026-11-09 新增；**默认已开启**，一般不用显式传）
#     🔴 analyze.py 与 recommend.py 的开关**同名同默认**；两条链必须同组透传，
#        否则规则层与交付层会选出不同的票（2026-11-09 闸门抓到的真缺陷）。
#     · --no-smoothing              关闭平滑（排序退回单日瞬态分）
#     · --smoothing-method ewma|rolling   平滑方法（默认 ewma）
#     · --smoothing-window 5        滚动窗口（**交易日**）
#     · --smoothing-alpha 0.35      EWMA 系数（半衰期 ≈ 1.61 个交易日）
#     · --smoothing-history-days 30 EWMA 预热历史跨度（交易日）
#     例：两条链一起关平滑（仅调试用）：
#     analyze.py ... --no-smoothing ; recommend.py ... --no-smoothing
#     例：换成 10 日滚动均值（两条链必须同时改）：
#     analyze.py ... --smoothing-method rolling --smoothing-window 10
#     recommend.py ... --smoothing-method rolling --smoothing-window 10

# 4) 真实账户口径自检（改数必须抛异常）
cd manu/etf_rotation && /home/huangzhe/anaconda3/envs/ql/bin/python account.py

# 5) TradingAgents（llm env；research_context 注入外部报告）
#    注意：reasoning_effort=max 时 max_tokens 必须 >= 32768，否则正文会被思考 token 吃光
export PATH="$HOME/.local/bin:$PATH"; export CODEX_HOME=/mnt/data/.codex
cd /tmp/pycharm_project_658d3fc4/TradingAgents && /home/huangzhe/anaconda3/envs/llm/bin/python <script>.py

# 同步到服务器（本地改完必跑；md5 双侧比对）
cd /media/manu/1TB-Volume/workspace/qlib && bash manu/tools/sync_to_server.sh manu

# 6) ETF 轮动 · 一键自动化流程（qlib 个股层 → ETF 报告 → TradingAgents → 调仓计划）
#    服务器跑；host 只改代码 + 同步（铁律〇：本地改完 bash manu/tools/sync_to_server.sh manu）
cd /tmp/pycharm_project_658d3fc4 && bash manu/etf_rotation/run_pipeline.sh --asof 2026-11-03
#    日常调仓（复用已有个股分数，不跑重活）：
#    bash manu/etf_rotation/run_pipeline.sh --skip-scores --skip-prices
#    分步：
#      /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/qlib_scores.py --mode infer --out-dir /mnt/data/siping/runs/etf_rotation_results
#      /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/analyze.py --asof 2026-11-03 \
#          --out-dir /mnt/data/siping/runs/etf_rotation_results --stock-prices /mnt/data/siping/runs/etf_rotation_results/stock_prices.csv
#      /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/final_rebalance_plan.py \
#          --out-dir /mnt/data/siping/runs/etf_rotation_results
#    产物：etf_analysis_report.{json,md} / tradingagents_decision.md / final_rebalance_plan.{json,md}

## 仓库整理与版本控制（host 本地，2026-11-05 新增）

> 规约见 `manu/memory/rules.md §十四.93`：**版本库只放「核心代码 + 文档 + 事实」，中间产物一律 ignore**。
> 判定问句：**「这个文件丢了，明天能不能重跑出来？」** 能 ⇒ 不入库；不能 ⇒ 入库。

```bash
cd /media/manu/1TB-Volume/workspace/qlib

# 1) 看当前状态（整理前先看，别盲删）
git status --porcelain
git diff --cached --numstat | wc -l        # 已暂存文件数

# 2) 暂存核心代码 + memory（**不 commit**：commit 需用户授权）
git add -A manu/ CMDS.md .gitignore .gitattributes
git diff --cached --name-only | wc -l       # 期望 90
git diff --cached --name-only | grep -E "__pycache__|\.pyc$|\.ruff_cache|_host_backup|^runs/|\.log$"   # 期望：空（未误收）

# 3) 复核「输入真值确实能入库」（**权威判据是 git add -n，不是 check-ignore 退出码**）
git add -n manu/etf_rotation/data/account_snapshot.csv   # 期望：add 'manu/etf_rotation/data/account_snapshot.csv'
git cat-file blob :manu/etf_rotation/data/account_snapshot.csv | cmp - manu/etf_rotation/data/account_snapshot.csv && echo "逐字节一致 ✓"

# 4) memory 表格结构自检（改了 memory 必跑）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/check_memory_tables.py

# 5) 全量闸门（改了 memory / CMDS.md 必跑：它们在指纹表 35 条登记项里）
#    服务器跑；日志：/mnt/data/siping/runs/etf_rotation_results/_mq3_final4.log
cd /tmp/pycharm_project_658d3fc4 && bash manu/etf_rotation/run_gates.sh

# 6) 生产基线「向前重建」（交付件处于「半更新态」时：先备份 → 重生成 → 只许改值不许增删）
#    ⚠️ 会真调 LLM（TradingAgents 辩论，上轮实测 1525 秒）⇒ 必须用户当轮明确授权
#    ⚠️ 跑完产物根进入「新基线」⇒ 指纹表必须随后重生成（见第 7 条）
cd /tmp/pycharm_project_658d3fc4 && bash manu/etf_rotation/rebuild_baseline.sh --asof 2026-11-03 --label 20261105

# 7) 指纹表安全重生成（改了 memory / CMDS.md / 任何在 35 条登记项里的文件后必跑）
#    先 --dry-run 看漂移，再实跑；口径：先备份 → 从当前良好版重建 → 断言登记项集合不变 → 逐条复验
python3 manu/tools/regen_fingerprint_table.py --artifact-root /mnt/data/siping/runs/etf_rotation_results --dry-run
python3 manu/tools/regen_fingerprint_table.py --artifact-root /mnt/data/siping/runs/etf_rotation_results

# 8) 「不在指纹表 35 条登记项里」的闸门脚本（2026-11-09 新增，见 rules.md §十四.92 第 8 条）
#    ⇒ 它们的漂移不靠指纹表，靠 ① 挂进 run_gates.sh 阶段表（全量闸门每次实跑）② 自带产物级自检
cd /tmp/pycharm_project_658d3fc4 && /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/probes/probe_rolling_increment.py
cd /tmp/pycharm_project_658d3fc4 && /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/probes/probe_snapshot_contract.py
cd /tmp/pycharm_project_658d3fc4 && /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/probes/probe_qlib_provider.py

# 9) qlib provider 单一事实源（2026-11-09 新增；研究链 ↔ 每日链**不分叉**）
#    打印「本次该用哪个 provider + 数据集覆盖末日」（末日 < asof 时告警）；只读、不联网。
cd /tmp/pycharm_project_658d3fc4 && /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/qlib_provider.py --asof 2026-12-01
#    显式覆盖（与 daily/run_qlib_score.py 同一套 env 名）：
cd /tmp/pycharm_project_658d3fc4 && /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/qlib_provider.py \
    --provider-uri /mnt/data/siping/datasets/manu/etf_rotation/datasets/qlib/cn_data_live --asof 2026-12-01
#    研究链显式吃**派生**数据集（阶段 1 自建增量日 ⇒ 覆盖到 2026-11-05）：
#    ⚠️ 会**改变 S 层产物**（stock_scores.csv）⇒ 需用户当轮授权；默认不传 = 上游基座（零回归）
cd /tmp/pycharm_project_658d3fc4 && bash manu/etf_rotation/run_pipeline.sh --asof 2026-11-03 \
    --provider-uri /mnt/data/siping/datasets/manu/etf_rotation/datasets/qlib/cn_data_live
```

> 🔴 **`.gitignore` 顺序陷阱**：上游 `.gitignore:15` 有 `*.csv`（为 qlib 自身大数据文件设的）⇒
> 会**静默忽略** `manu/etf_rotation/data/account_snapshot.csv`（输入真值）⇒ 本工程在**末尾**补
> `!manu/etf_rotation/data/account_snapshot.csv`（白名单必须排在通配之后）。
> 🔴 **`.gitattributes` 陷阱**：该 csv 带 **BOM + CRLF**，没有 `-text` 时 git 会归一化行尾 ⇒
> checkout 后文件字节变了 ⇒ md5 双侧比对**假漂移** ⇒ 已用 `manu/etf_rotation/data/account_snapshot.csv -text` 钉住；
> 改完 `.gitattributes` 若 index 仍是旧版，须 `git rm --cached` 再 `git add`。
> 🔴 **指纹表**：`CMDS.md` 与 memory 四件套在 `/mnt/data/siping/runs/etf_rotation_results/_ms5_md5.txt` 的 35 条登记项里
> ⇒ 改动它们**必然**让闸门 P2 变红 ⇒ 按 `rules.md §十四.92` 第 7 条重生成（先备份 → 从当前良好版重建 → 断言登记项集合不变）。

## 每日 ETF 轮动流水线（2026-11-05 新增；2026-11-09 改单一流水线 + 快照制；服务器侧；**按需分析**）

> 🔴 **交付形态（2026-11-05 用户改口，当前唯一形态）**：**按需分析** —— 用户在窗口喊「分析」⇒ 主 agent 在服务器跑流程 ⇒ **Markdown 报告直接回窗口**。
> ❌ **服务器 crontab 已无本工程任何条目**（5 条定时任务已全部卸载）；❌ **邮件不再是默认动作**（保留为显式 `--send-email` 的 opt-in）。
> 🔴 **架构形态（2026-11-09 架构瘦身）**：**单一流水线 + 快照制** —— **没有**「模式」分诊；
> **每次运行落一个不可变快照目录** `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/`；**副作用只由 `--live-trade` 决定**。
> 六阶段全流程：**盘后取数 → S 层 Alpha 推断 → E=W×S 穿透 + 均线过滤 → TradingAgents 辩论 → 规则层对账 → 执行卡 + 报告**。
> 用法详见 `manu/etf_rotation/daily/README.md`；架构说明见 `manu/docs/snapshot_architecture.md`；铁律见 `manu/memory/rules.md §十五`。

```bash
# ---- 服务器（huangzhe@192.168.99.40:32222）；重活一律上服务器 ----
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40
cd /tmp/pycharm_project_658d3fc4

# 1) 【默认 · 观察态】完整跑一趟：现场拉数 → 内存计算 → 落一个快照目录
#    副作用：无（执行卡挂单禁用、不发信）
#    产物：<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/（自包含、跑完冻结、含 snapshot_manifest.json）
#    报告：快照目录内一份 ＋ manu/report/report_<YYYYMMDD>.md 一份副本（同时打印到 stdout）
#    日志：/mnt/data/siping/runs/etf_rotation_results/logs/daily_run_<YYYYMMDD>.log
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-05

# 1b) 【强制全流程】--full：取数 / S 层刷新 / qlib 数据集刷新 / LLM 辩论 四段全开
      #      ⚠️ 含重活（S 层 ~27min + 辩论 ~15min ⇒ 35~45 分钟）⇒ 用户当轮授权后跑
      #      🟢 阶段 1 还会**真落盘**「自建增量日」（只写派生目录 cn_data_live，基座零改动）
      #      ⇒ S 层因此能打到信号日；只想看计划：--increment-dry-run；不想补：--no-increment
      #      🔴 --full 与任何 skip 开关冲突时**以 --full 为准**；被覆盖的 skip 登记进 manifest 并在报告披露
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-12-01 --full

# 1c) 跳过 LLM 辩论（省 API 配额）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-12-01 --skip-llm

# 1d) 【显式观察态】--dry-run（与 --live-trade 互斥 ⇒ 同时给 ⇒ 退出码 2）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-12-01 --dry-run

# 1e) 【实盘态】--live-trade：执行卡可下单、可发信（🔴 唯一实盘副作用开关）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-12-01 --full --live-trade

# 1f) 指定快照根（缺省 daily_config.SNAPSHOT_ROOT；env ETF_ROTATION_SNAPSHOT_ROOT）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-12-01 \
    --snapshot-root /mnt/data/siping/runs/etf_rotation_results/snapshots

# 1g) 报告落盘到指定路径（默认 manu/report/report_<YYYYMMDD>.md）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-05 \
    --report-md /mnt/data/siping/runs/etf_rotation_results/daily/report_20261105.md

# 1h) 【按需】显式发邮件（默认不发；通道仍通，凭据在 0600 secrets 文件里）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-05 --send-email

# 1i) 【按需补发】只发已持久化的邮件草稿，不重跑任何计算
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-05 --send-only

# 1j) 把服务器跑出来的报告【拉回 host 工程】（PyCharm 里直接看 manu/report/）
bash manu/tools/pull_reports.sh                 # 拉全部报告 + md5 双侧比对
bash manu/tools/pull_reports.sh --check         # 只比对 md5，不落盘
bash manu/tools/pull_reports.sh --list          # 只列服务器上有什么

# 1k) 【阶段 1 兜底】自建增量日：上游快照覆盖不到信号日时，把**派生目录**补到信号日
#     🔴 只写 cn_data_live（基座 cn_data 零改动）；默认 dry-run（真写要 --apply）
#     🔴 快照接口**只能补当天** ⇒ 缺口 > 1 个交易日 ⇒ fail-closed（不写脏数据）
cd /tmp/pycharm_project_658d3fc4
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/stage1_rolling_increment.py \
    --day 2026-12-01                                   # dry-run：只出计划与统计（磁盘零改动）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/stage1_rolling_increment.py \
    --day 2026-12-01 --apply                           # 真写派生目录（重活，需当轮授权）

# 1l) 【实机口径核对】用本配方重算真实数据集已存在的交易日，与上游存值逐位比（只读）
/home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/probes/_probe_inc_recipe.py

# 2) 看日志与产物（🔴 快照目录才是真值）
tail -n 100 /mnt/data/siping/runs/etf_rotation_results/logs/daily_run_20261105.log
ls -l /mnt/data/siping/runs/etf_rotation_results/snapshots/                       # 快照根（可 env 改）
ls -l /mnt/data/siping/runs/etf_rotation_results/snapshots/<YYYYMMDD_HHMMSS>/   # 本轮快照目录
cat /mnt/data/siping/runs/etf_rotation_results/snapshots/<YYYYMMDD_HHMMSS>/snapshot_manifest.json   # 唯一权威索引
cat manu/report/report_20261105.md                                       # Markdown 报告（交付副本）
cat /mnt/data/siping/runs/etf_rotation_results/snapshots/<YYYYMMDD_HHMMSS>/heartbeat_20261105.json
cat /mnt/data/siping/runs/etf_rotation_results/snapshots/<YYYYMMDD_HHMMSS>/execution_card_20261105.json

# 3) 确认服务器上没有本工程的定时任务（应当只有系统默认两行）
crontab -l

# ---- host 本地：同步到服务器（本地改完必跑；判据只能 md5 双侧比对）----
cd /media/manu/1TB-Volume/workspace/qlib
bash manu/tools/sync_to_server.sh manu/etf_rotation/daily
#    预演：bash manu/tools/sync_to_server.sh --dry-run manu/etf_rotation/daily
#    只比对：bash manu/tools/sync_to_server.sh --check  manu/etf_rotation/daily

# ---- host 本地：跑每日链路的单测（只跑单测，不触发任何重活）----
cd /media/manu/1TB-Volume/workspace/qlib
/home/manu/anaconda3/envs/uav/bin/python -m pytest manu/etf_rotation/daily/tests -q
#    服务器侧等价（ql env 未装 pytest 时先在该 env 内 pip install pytest）：
#    /home/huangzhe/anaconda3/envs/ql/bin/python -m pytest manu/etf_rotation/daily/tests -q
```

> 🔴 **老命令会被拒**：`--mode`（连参数本身都已删除）一律**退出码 2** ＋ stderr 迁移提示
> （模式分诊已废除 ⇒ 单一流水线；跑哪几段 = `--full` ＋ skip 开关，要不要动真格 = `--live-trade`）。
> 新旧对照表见 `manu/docs/snapshot_architecture.md` §9。
> 🔴 **重活开关**：`--refresh-scores`（重跑 S 层，约 **27 分钟**）与 `--update-qlib-bin`（刷新 qlib 数据集）
> **默认开启**，`--full` 会强制为开 ⇒ **重活须用户当轮授权**（铁律一）。
> 🔴 **密钥纪律**：126 客户端授权码**只**写进服务器 `/mnt/data/siping/secrets/smtp.env`（权限 **0600**）⇒ **绝不进仓库、不进日志、不进邮件正文**；⚠️ 用**网页登录密码**会 `535 authentication failed`（126 只认客户端授权码）。
> 退出码：**0** 成功（含非交易日静默退出）· **1** 失败 · **2** 用法或环境错误（含 `--mode` 被拒、`--live-trade` 与 `--dry-run` 互斥、锁冲突）。
> ⚠️ **看门狗当前不部署**：`manu/tools/etf_watchdog.py` 保留在仓库（纯标准库），但它的守护对象是 cron ⇒ 取消定时后**没有守护对象**；服务器持久盘 `/mnt/data/siping/apps/etf_watchdog.py` 的旧副本保留未删（无害）。
> 🔴 **`install_schedule.sh` 已删除**（2026-11-05）：它唯一的用途是往用户 crontab 装那 5 条定时任务 ⇒ 取消定时后即死代码 ⇒ 删除，**不要再往 crontab 装本工程任何条目**。
