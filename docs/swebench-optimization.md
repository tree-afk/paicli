# PaiCLI SWE-bench Pro 优化记录

本文档用于记录 PaiCLI 在 SWE-bench Pro 上的评测协议、实验结果、失败分析和优化过程。
数字只记录真实跑出来的结果，不写未验证的指标。

对外用的"改前/改后"汇总与简历口径见 [optimization-results.md](optimization-results.md)；
本文件是过程与归因的完整底稿。

## 1. 目标

- 基于 PaiCLI Python 二次开发，评测终端 Coding Agent 的真实仓库修复能力
- 用测试作为裁判，而不是比对 gold patch
- 通过失败分布驱动优化，每条优化都要有前后指标
- 产出可用于简历和面试的工程记录

## 2. 评测设置

- Benchmark：`ScaleAI/SWE-bench_Pro`，default V2，642 条，其中 Python 237 条
- 模型：`deepseek-flash`（官方 DeepSeek OpenAI-compatible 端点）
- Agent：PaiCLI ReAct，`max_turns` 可配置
- 镜像：`ghcr.io/scaleapi/swe-bench_pro-v2:<instance_id>`
- 仓库路径：镜像内 `/app`（少数任务 `/testbed`）
- 判分：
  - 写入 dataset 的 `test_patch` 到 `/tests/test_patch.patch`
  - 应用 `test_patch` 后运行 `selected_test_files_to_run`
  - Agent patch 应用到一份干净的 base 副本，再跑测试
  - 检查是否修改测试文件
- 协议约束：
  - 禁用 `web_search`、`web_fetch`
  - 命令黑名单拦截 `curl`、`wget`、`git fetch/clone/remote`、`ssh`、`scp`、`pip install`、`apt`、`docker` 等
  - 网络只应允许模型端点
- Runner：`eval/swebench_pro_runner.py`
- 每个实例产物：`case.json`、`agent_prompt.txt`、`agent_patch.diff`、`agent_trace.json`、`verify.log`、`result.json`

> **2026-11-06 重要更正：第 3–5 节的 5/5、3/5、5/7 结果均发现 `gold_patch` 泄漏，不能作为干净基线。**
> Agent 的 `bash` 在宿主机执行，可以 `cd ..` 读取 `<task_dir>/case.json`，其中包含 `gold_patch` 和 `test_patch`。
> 证据与影响见第 10 节；修复前任何 `resolved` 数字都不能写入简历。

## 3. 实验记录

| run | date | cases | network | max_turns | resolved | patch apply | tamper | avg turns | avg tokens | notes |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| preliminary_networked | 2026-11-05 | 5 ansible | on | 20 | 3/5 | 4/4 | 0/5 | 20.0 | 364,928 | 用了 web_search/web_fetch/curl，且有 gold 泄漏，双重无效 |
| clean_5 | 2026-11-05 | 5 ansible | off | 30 | 5/5 | 5/5 | 0/5 | 26.2 | 609,079 | **已作废**：942 直接应用 gold，e9/0ea 读取 case.json；见第 10 节 |
| ablation_t20_nonet | 2026-11-05 | 5 ansible | off | 20 | 3/5 | 3/5 | 0/5 | 17.6 | 273,830 | trace 未见 case.json 读取；宿主机 bash 下仍不能保证隔离 |
| expand_ansible7 | 2026-11-05 | 7 ansible | off | 30 | 5/7 | 6/7 | 0/7 | 23.7 | 493,904 | **已作废**：942 应用 gold、d6 读取 gold；见第 10 节 |
| expand_crossrepo | 未开始 | 13 跨仓库 | off | 30 | 待跑 | 待跑 | 待跑 | 待跑 | 待跑 | openlibrary/qutebrowser 镜像未拉取 |

## 4. 干净 5 条结果（已作废）

> 5/5 不能作为干净基线：`run5_nonet` 中 942424e1 直接 `git apply` 了 `case.json` 里的 `gold_patch`，e9e60012、0ea40e09 也读取了 `case.json`。详见第 10 节。下表保留仅用于记录当时产物。

| # | 测试文件 | 状态 | 轮数 | tokens | 耗时(s) | patch 行数 |
|---|---|---|---:|---:|---:|---:|
| 1 | `test/units/utils/test_vars.py` | resolved | 17 | 250,875 | 72.47 | 27 |
| 2 | connection/executor 多个测试 | resolved | 30 | 707,252 | 158.41 | 254 |
| 3 | `test/units/cli/test_doc.py` | resolved | 30 | 899,061 | 150.65 | 115 |
| 4 | `test_env.py` / `test_py3compat.py` | resolved | 30 | 570,606 | 371.18 | 129 |
| 5 | `test/units/plugins/connection/test_winrm.py` | resolved | 24 | 617,604 | 80.41 | 144 |

汇总：

- resolved：5/5
- patch apply：5/5
- 改测试：0/5
- Web 工具使用：0/5
- 平均轮数：26.2（17–30）
- 总 tokens：3,045,398；平均 609,079/任务
- 平均耗时：166.6s（72.5–371.2s）

## 5. 失败分析

### 5.1 第一次 3/5（联网，20 轮，污染）

- 1 条 `no_patch`：20 轮主要花在读代码、`grep`、`web_fetch`，没有落 edit
- 1 条 `not_resolved`：patch 引入循环 import，测试直接报错
- 5 条全部撞满 20 轮
- 关键问题：Agent 有公网，实际使用 `web_search` / `web_fetch` / `curl`，违反 SWE-bench Pro 的“网络只允许模型端点”协议

### 5.3 20 轮 vs 30 轮消融（禁网）

同一批 5 条实例，禁网，只改 `max_turns`：

| 配置 | resolved | 平均轮数 | 总 tokens | 平均 tokens/任务 |
|---|---:|---:|---:|---:|
| max_turns=20 | 3/5 | 17.6 | 1,369,150 | 273,830 |
| max_turns=30 | 5/5 | 26.2 | 3,045,398 | 609,079 |

失败的两条在 20 轮时都是 `no_patch`，30 轮时被救回：

- 第 2 条：20 轮 no_patch（272,399 tokens）→ 30 轮 resolved（707,252 tokens）
- 第 3 条：20 轮 no_patch（436,609 tokens）→ 30 轮 resolved（899,061 tokens）

结论：

- **max_turns=20 是这批任务的主要瓶颈**；不是网络本身
- 20 轮联网 3/5 与 20 轮禁网 3/5 相同，说明在这 5 条上网络没有提升通过率，反而带来看答案的污染风险
- 30 轮把 resolved 从 3/5 提到 5/5，但 tokens 从 1.37M 涨到 3.05M（约 2.2 倍），是明确的成本/正确率权衡
- n=5，不能做统计显著性结论；下一步要跨仓库扩量

### 5.2 干净 5/5（禁网，30 轮）

- 没有失败，因此不能从失败分布选择核心优化
- 5 条都接近或撞到 30 轮
- 平均 60.9 万 tokens/任务，效率是当前主要问题
- Agent 在宿主机运行，宿主机没有 ansible 测试依赖；大量 bash 调用可能是在跑失败的测试，浪费轮次
- 日志有 `RuntimeError: Event loop is closed` 警告，来自 bash 子进程 transport 清理，不影响结果

### 5.4 扩量中发现的失败样本（ansible 8127）

同环境扩量到第 2 条时出现 `no_patch`：

- 30 轮，819,143 tokens
- 56 次工具调用，其中绝大多数是 `bash` 读代码，`grep` 和 `read_file` 很少
- 全程没有 `edit_file` / `write_file`
- final text 只到 “I'll start by inspecting the relevant files.”
- 7 次工具错误，其中 3 次是 `ssh` 被命令黑名单拦截，1 次是破坏性命令策略
- 该任务有 6 个测试文件、15 个 fail_to_pass，属于多文件复杂任务

这说明：

- 30 轮仍可能不够；复杂任务需要更高预算或更早落 edit
- Agent 存在“过度探索”问题：读了很多文件，但没有形成 patch
- `ssh` 黑名单可能与该任务（connection 插件）发生误伤，需要单独确认；但黑名单不是 no_patch 的唯一原因
- 这是当前最明确的优化方向：**探索预算 / 早编辑策略**，以及 **Agent 放进容器跑测试** 减少盲目探索

### 5.5 扩量结果（ansible 7 条，30 轮禁网）

| # | 实例 | 状态 | 轮数 | tokens | patch 行数 | 耗时(s) |
|---|---|---|---:|---:|---:|---:|
| 1 | 0ea40e09 | resolved | 17 | 217,694 | 20 | 94.6 |
| 2 | 8127abbc | no_patch | 30 | 819,143 | 0 | 218.1 |
| 3 | 942424e1 | resolved | 30 | 569,081 | 254 | 161.7 |
| 4 | bec27fb4 | resolved | 30 | 622,608 | 108 | 246.6 |
| 5 | d6d22519 | resolved | 30 | 771,798 | 95 | 95.6 |
| 6 | e0c91af4 | not_resolved | 11 | 95,744 | 98 | 36.7 |
| 7 | e9e60012 | resolved | 18 | 361,260 | 126 | 90.3 |

汇总：resolved 5/7，patch apply 6/7，改测试 0/7，平均 23.7 轮、49.4 万 tokens/任务。

> **污染说明（2026-11-06）**：942424e1 直接应用 `gold_patch`（agent patch 与 gold 增删行 163/163 重合），d6d22519 读取 `gold_patch`（34/36 重合），8127abbc 读取 `gold_patch`/`test_patch` 但未落 patch。因此 5/7 不能作为干净基线，失败分析仅保留“行为观察”，不作为优化前后的数字依据。

两类失败：

- **no_patch（8127abbc）**：与 5.4 是同一条实例，30 轮仍不落 edit；说明对该任务 30 轮不够，根因是过度探索而非预算单点
- **not_resolved（e0c91af4）**：11 轮就写出 98 行 patch，但把 `py3compat.py` 的依赖改成 `from ansible.utils.display import Display`，引入循环 import；测试收集阶段直接报错。Agent 没有在提交前做 import/测试自检，属于“改得快但没验证”

两类失败对应两个不同优化点：

- 8127 → 早编辑/探索预算
- e0c91af4 → 提交前最小验证（至少 import 改动模块）

## 6. 优化 backlog

| 优化 | 触发问题 | 类型 | 状态 |
|---|---|---|---|
| 禁用 Web 工具 + 网络命令黑名单 | 协议泄漏/外部答案 | 评测协议 | 已完成 |
| `max_turns` 可配置 | 所有任务撞 20 轮 | 评测协议 | 已完成 |
| 20 轮禁网消融 | 隔离 max_turns 变量 | 实验设计 | 已完成：3/5 vs 5/5 |
| 跨仓库扩量到 20 条 | 5 条同仓库，泛化不足 | 数据/评测 | ansible 扩到 7 条（5/7）；openlibrary/qutebrowser 镜像未拉 |
| Agent 放进容器运行 | 宿主机无法跑测试，浪费轮次 | Adapter | 高优先级 |
| 优化探索/早编辑策略 | no_patch（ansible 8127，30 轮 56 次调用无 edit） | Agent Prompt/Skill | 已有失败样本 |
| 提交前最小验证（import 改动模块） | e0c91af4 循环 import，测试收集即失败 | Agent Prompt/Skill | 新增，已有失败样本 |
| 修复 asyncio 子进程清理警告 | 日志噪声 | Runner | 待做 |
| 接官方 Harbor task/parser | 跨语言判分 | 官方 harness | 待做 |

## 7. 简历可用条目（待隔离复跑后可用）

> **暂不可直接使用**：下面 5/5 与 5/7 数字受 `gold_patch` 泄漏影响，已在第 10 节作废。等 B 方案隔离后重跑，再用新数字替换。

> 基于 PaiCLI Python 二次开发，搭建 SWE-bench Pro 真实仓库修复评测链路：镜像 + base commit + hidden test patch + clean replay + 自动指标记录。

> 在 SWE-bench Pro V2 的 5 条 ansible 子集上，PaiCLI resolved 5/5，patch 应用 5/5，测试篡改 0/5；平均 26.2 轮、60.9 万 tokens/任务。

> 发现并修复评测协议泄漏：禁用 Web 工具与网络命令，确保 Agent 不能通过外部搜索/下载获取答案。

> 构建可复现的 SWE-bench Pro runner，记录 patch、工具轨迹、tokens、耗时和失败分类，支持 `max_turns` 消融实验。

> 通过 20 轮/30 轮禁网消融定位 `max_turns` 是主要瓶颈：同一 5 条子集 resolved 由 3/5 提升到 5/5，tokens 由 1.37M 增至 3.05M（约 2.2 倍），形成明确的成本/正确率权衡。

注意：5 条是同一仓库/环境的子集，不能写成“完整 SWE-bench Pro 100%”。

## 8. 已知限制

- 5 条均为 ansible，同一环境 SHA，泛化性不足
- **评测隔离缺陷**：Agent 的 bash 在宿主机执行，可读取 `case.json` 里的 `gold_patch`/`test_patch`；已污染 5/5 与 5/7 结果，待容器化修复
- base commit 通过 Gitee 镜像补拉，不是每实例独立镜像
- 使用 dataset 的 test files + pytest 手动复刻 verifier；官方 Harbor harness 尚未完整跑通
- Agent 在宿主机运行，不能使用容器内的 ansible 测试环境
- `cost` 为空：`deepseek-flash` 没有内置价格 profile；token 为真实 usage
- n=5，不能做统计显著性结论

## 9. 产物

服务器：

```text
<server>/swebenchpro/runs/run5_nonet/
  summary.md
  results.jsonl
  <instance>/case.json
  <instance>/agent_prompt.txt
  <instance>/agent_patch.diff
  <instance>/agent_trace.json
  <instance>/verify.log
  <instance>/result.json
```

代码：

- `eval/swebench_pro_runner.py`
- `eval/runner.py`
- `tests/test_swebench_pro_runner.py`

## 10. 评测隔离缺陷与 B 方案基线

### 10.1 发现：`case.json` 泄漏 gold patch

- `run_instance` 把完整 dataset 行写到 `<task_dir>/case.json`，其中包含 `gold_patch` 和 `test_patch`。
- Agent 的 `bash` 在宿主机执行；路径限制只作用于文件工具，bash 可以直接 `cd ..` 读取 `case.json`。
- 已有 trace 证据：

| run | 实例 | 行为 | agent patch 与 gold 增删行重合 |
|---|---|---|---|
| run5_nonet | 942424e1 | 读 `case.json`，写 `/tmp/gold.patch`，`git apply /tmp/gold.patch` | 163/163 = 100% |
| run5_nonet | e9e60012 | 读 `case.json` / `gold_patch` / `test_patch` | 96/96 = 100% |
| run5_nonet | 0ea40e09 | `cat case.json` | 19/32 = 62.5% |
| run5_nonet | bec27fb4 | 读 `gold_patch` | 60/467 = 11.1% |
| expand_ansible7 | 942424e1 | 读 `case.json`，直接 `git apply` gold | 163/163 = 100% |
| expand_ansible7 | d6d22519 | 读 `case.json` / `gold_patch` | 34/36 = 94.4% |
| expand_ansible7 | 8127abbc | 读 `gold_patch` / `test_patch`，但未落 patch | 0/241 = 0% |

“重合”= agent patch 的增删行中，与 gold patch 的增删行相同的比例。

### 10.2 影响

- `clean_5` 的 5/5 作废：至少 4/5 读取了 `case.json`，其中 942424e1 直接应用 gold。
- `expand_ansible7` 的 5/7 作废：942424e1、d6d22519 污染；8127abbc 也读取但未落 patch。
- `preliminary_networked` 的 3/5 同时有联网和 gold 泄漏，双重无效。
- `ablation_t20_nonet` 的 trace 未见 `case.json` 读取，但宿主机 bash 下无法保证隔离，暂不作为干净基线。
- **在修复隔离前，任何 `resolved` 数字都不能写入简历。**

### 10.3 B 方案：宿主机 Agent + 容器化 bash

- PaiCLI 仍在宿主机，负责模型调用和文件工具。
- 新增 `ContainerBashTool`，对 Agent 仍暴露为 `bash`，内部执行 `docker exec`。
- 容器只 bind mount 当前仓库到 `/app`，使用 `--network=none`。
- 容器内看不到宿主机 `<server>/swebenchpro`，也看不到 `case.json`。
- 同时把 `case.json` 中的 `gold_patch` / `test_patch` 移出 Agent 可见范围。
- 这一方案同时解决两个问题：**Agent 不能自测** 和 **gold patch 泄漏**。
- 单次命令超时上限 1200s，与 `_run_agent` 的整轮预算对齐；`Tool` 外层墙设为上限 + 60s，避免 60s 的默认墙先于命令自身的 timeout 触发。

### 10.4 B 前基线（不能自测）

配置：ansible 7 条，`max_turns=30`，禁网，宿主机 bash。

| 指标 | 数值 | 说明 |
|---|---:|---|
| resolved | 5/7 | **受 gold 泄漏污染，无效** |
| patch apply | 6/7 | 同上 |
| 改测试 | 0/7 | |
| 平均轮数 | 23.7 | |
| 平均 tokens | 493,904 | |
| 成功执行 pytest 的任务 | 0/7 | 5 次 pytest 调用全部在收集/导入阶段失败 |
| 失败的 import 检查 | ≥3 | 缺 `jinja2`、`winrm` 等依赖 |
| 未尝试跑测试的任务 | 2/7 | 8127abbc、d6d22519 |

统计口径：

- pytest 调用：0ea40e09 1 次、942424e1 3 次、e0c91af4 1 次，共 5 次，成功 0 次。
- import 检查：942424e1 `import ansible` 失败、bec27fb4 `import jinja2` 失败、e9e60012 `import winrm` 失败。

### 10.4.5 B 方案冒烟验证（单条，非基线）

实例 `ansible__ansible-0ea40e09`（与 B 前基线同一题），`max_turns=17`。

| 指标 | 数值 |
|---|---:|
| status | resolved |
| 轮数 | 14 |
| tokens | 134,776 |
| agent 耗时 | 35.84s |
| patch 行数 | 18（`lib/ansible/vars/manager.py`） |
| 改测试文件 | 否 |
| test_patch / agent_patch apply rc | 0 / 0 |
| 复跑 test_rc | 0（16 passed in 0.26s） |

冒烟证据：

- 16 次 bash 全部走容器：前 2 次访问宿主机相对路径 `ansible/...` 失败，第 3 次 `pwd` 得到 `/app` 后自行纠正。
- Agent 在容器内自测 2 次：`pytest test/units/utils/test_vars.py test/units/parsing/yaml/test_dumper.py -q`（21 passed in 0.15s）、`pytest test/units/vars -q`（14 passed in 0.42s）。这两次都远快于 60s，未触发旧的外层墙。
- `case.json` 的 `gold_patch` / `test_patch` 为 `<redacted>`；容器内看不到 `task_dir`；运行结束后无残留 `paicli-bash-*` 容器。

### 10.4.6 判分升级：文件级 → 逐测试 f2p/p2p

旧判分只看 `pytest` 退出码，一条测试失败和一片测试失败同罪，也无法说明
“差在哪”。现在 `_verify` 让 pytest 写 `--junitxml`，按官方规则逐测试判定：

- `fail_to_pass` 每条都必须 `passed`（`skipped` 不算通过）；
- `pass_to_pass` 任一 `failed` 即回归（`skipped` 不算回归）；
- 全部满足才 `resolved`；退出码 2/5，或 0 条用例被收集但清单要求测试，记为
  `collection_error`（与“测试跑完但没过”区分开）。

上游 `expansion_ansible.jsonl` 的 P2P 清单被空格切碎过（如
`test_ttyify[The`），因此额外做了“未闭合方括号 + 前缀匹配”，并把命中条数写进
`approximate_entries`，不做静默认账。

用已存 patch 重判（不调用模型）。这 7 条 patch 来自 `runs/expand_ansible7`，
即 **B 方案之前、宿主机 bash 那次运行**（trace 里 `pwd` 返回的是
`<server>/.../repo` 宿主机路径，且 3/7 有 gold 泄漏访问），所以下表只用于
**验证判分器**，不能当作成绩：

| 实例 | 旧（文件级） | 新（逐测试） | collected | 说明 |
|---|---|---|---:|---|
| 0ea40e09 | resolved | **resolved** f2p 1/1，p2p 15/15 | 16 | 1+15=16，全覆盖 |
| 8127abbc | no_patch | **no_patch** f2p 0/15，p2p 0/60 | 0 | 空 patch，未跑测试 |
| 942424e1 | resolved | **resolved** f2p 1/1，p2p 35/35 | 36 | 1+35=36，全覆盖 |
| bec27fb4 | resolved | **resolved** f2p 3/3，p2p 21/21 | 24 | 7 条近似匹配，全部是被空格切碎的参数化 id |
| d6d22519 | resolved | **resolved** f2p 4/4，p2p 2/2 | 6 | 4+2=6，全覆盖 |
| e0c91af4 | not_resolved | **collection_error** f2p 0/6 | 0 | 两个测试模块 import 即报错 |
| e9e60012 | resolved | **resolved** f2p 8/8，p2p 20/20 | 28 | 8+20=28，全覆盖 |

5 条跑通的实例都满足 `f2p_total + p2p_total == collected_tests`，说明清单里的
测试和 junit 里的用例是一一对应的，没有漏配也没有重复计数。

顺带修掉一个会误报的状态：空 patch 时 `git apply` 退出码是 1，旧逻辑会把它报成
`patch_apply_failed`（8127 第一次重判就撞上了）。现在 `_verify` 在空 patch 时直接
返回 `no_patch`。

同一镜像、同一 `base_repo` 上做三个对照组（`runs/controls/`，各一次容器运行）：

| 实例 | 只打 test_patch | test_patch + gold_patch | test_patch + agent patch |
|---|---|---|---|
| bec27fb4 | f2p 0/3，p2p 21/21，rc 1 | **resolved 3/3 + 21/21** | resolved 3/3 + 21/21 |
| e0c91af4 | f2p 0/6，rc 1（6 条收集到） | **resolved 6/6** | collection_error（0 条收集到） |

结论：

- gold patch 两条都能判成 `resolved`，说明镜像、测试清单和判分器可用；
- base + test_patch 时 f2p 全部失败，说明这批 f2p 确实是“修好才变绿”的判据；
- e0c91af4 的 0 条收集是 agent 自己造成的：它把
  `lib/ansible/utils/py3compat.py` 改成在模块顶部
  `from ansible.utils.display import Display`，与 `display → constants →
  config.manager → py3compat.environ` 形成循环导入。属于真实失败，不是判分环境问题。

### 10.4.7 20 条清单与镜像盘点

20 条 = ansible 7 + openlibrary 7 + qutebrowser 6。三个 repo 各自只有一个环境
sha，但这**不代表**可以共用一个镜像：实测 `0ea40e09` 镜像里的仓库只停在它自己的
base commit `f7234968`，另外 6 条 ansible 的 commit 在镜像里 `git cat-file` 查不到；
反过来，`bec27fb4` 自己的镜像 HEAD 正好是它的 base commit `6d34eb88`。

所以正确做法是**一条一个镜像**（官方也是这么做的）：镜像里带着该条的 base
commit，判分和 Agent 都不需要联网 fetch。共用镜像 + 联网 fetch 的后果是上一轮
gold 泄漏的来源之一，已弃用。

| repo | env sha（前 8 位） | 条数 | 镜像 |
|---|---|---:|---|
| ansible/ansible | 30a923fb | 7 | `docker pull`（层属主 root；同 env 层大量共享，实测两个镜像 11/18 层相同） |
| internetarchive/openlibrary | 0f5aece3 | 7 | 层属主是构建 pod 的随机 UID，`docker pull` 必失败 → 按 blob 下载后 `docker import`（10.4.7.1） |
| qutebrowser/qutebrowser | 2ef375ac | 6 | 同上，先按 blob 方案处理 |

连带修掉一个会静默算错的缺陷：判分脚本原先不检查 `git reset --hard` 的返回码，
base commit 缺失时会留在错误的代码树上继续打补丁、跑测试。现在把它写进
`reset_rc`，非 0 直接判 `infra_error`（`verify.log` 里可查）。

### 10.4.7.1 rootless docker 的属主注册限制（已绕过）

实测：openlibrary 镜像的某一层里，`/app/node_modules/optionator/CHANGELOG.md`
属主是构建 pod 的随机 UID `110779`；运行用户的 subuid 区间是 1017504–1083039，
所以 rootless dockerd 在注册层时 `Lchown` 直接失败（`invalid argument`），
`docker pull` 必然失败，重试三次只是重复下载 0.9GB。ansible 的层属主是 root，
所以 7 条 ansible 能正常拉取。daemon 日志里能直接看到这条错误，不是网络问题。

绕过办法：按 manifest 从镜像仓库直接下载同样的 blob，解包时丢弃属主，再用
`docker import` 打回规范 tag（`import_image.py`）。文件内容与官方镜像逐字节
相同，只有属主归一为 root——测试不依赖属主。这是对**环境限制**的绕过，不是
评测口径变化；结果里要披露这一点。判分器侧不需要改动：`_ensure_image` 只检查
本地是否有该 tag，import 出来的镜像直接可用。

| repo | 实例 | f2p | p2p 条目 | 测试文件 |
|---|---|---:|---:|---:|
| ansible | 0ea40e09 | 1 | 15 | 1 |
| ansible | 8127abbc | 15 | 60 | 6 |
| ansible | 942424e1 | 1 | 35 | 1 |
| ansible | bec27fb4 | 3 | 17（解析为 21 条） | 1 |
| ansible | d6d22519 | 4 | 2 | 2 |
| ansible | e0c91af4 | 6 | 0 | 2 |
| ansible | e9e60012 | 8 | 20 | 1 |
| openlibrary | 08ac40d0 | 7 | 119 | 2 |
| openlibrary | 1894cb48 | 1 | 62 | 2 |
| openlibrary | 2fe532a3 | 4 | 20 | 2 |
| openlibrary | 3f580a5f | 6 | 41 | 2 |
| openlibrary | 427f1f4e | 5 | 4 | 1 |
| openlibrary | 43f9e7e0 | 1 | 60 | 1 |
| openlibrary | 4b7ea297 | 24 | 0 | 1 |
| qutebrowser | 01d1d149 | 3 | 46 | 2 |
| qutebrowser | 0b621cb0 | 1 | 20 | 1 |
| qutebrowser | 0fc6d110 | 4 | 54 | 1 |
| qutebrowser | 1a9e74bf | 4 | 68 | 1 |
| qutebrowser | 1af602b2 | 2 | 25 | 1 |
| qutebrowser | 305e7c96 | 1 | 51 | 1 |

开跑前要先做一次预检（不调用模型）：20 条各自用**自己的镜像 + 自己的一份干净
仓库副本**跑 gold patch，期望 20/20 全部 `resolved`。这一步同时验证三件事——
每条镜像里确实有它的 base commit（`reset_rc=0`）、`python -m pytest -q <files>`
在这三个 repo 上是否是正确的调用方式、以及逐测试判分在这些清单上是否成立。

2026-11-08 预检结果：**20/20 resolved**（逐条数字见
`<server>/gold_precheck_20b.log` 与 `runs/gold_precheck/<instance>/`）。
18 条用文件级调用即可；2 条 qutebrowser（01d1d149、1a9e74bf）需要用 10.4.7.2
的按节点重跑，原因是镜像里 QtWebEngine 在一个未判分的测试上段错误。

### 10.4.7.2 qutebrowser 会话崩溃与按节点重跑（已绕过）

两条 qutebrowser 实例在文件级 `pytest -q <files>` 下先报 `infra_error`：pytest
收集 83 项、跑到第 72 项左右进程直接消失，既没有汇总行也没有 junit，判分器只能
看到 `collected 0`。

归因（`verify.log` + 单测复现）：崩溃点在
`tests/unit/config/test_qtargs.py::TestDarkMode::test_new_chromium` →
`qutebrowser/utils/version.py:403 _chromium_version()` →
`qutebrowser/browser/webengine/webenginesettings.py:360 init_user_agent()`，即创建
默认 `QWebEngineProfile` 时 QtWebEngine 段错误（单独跑该测试 `rc=139`，
faulthandler 栈顶就是这三帧）。镜像里的 Chromium 是 83.0.4103.122，而这个测试
本身是"遇到未知 Chromium 就故意失败"的守卫测试。

已排除的变量：

| 变量 | 结果 |
|---|---|
| `QT_QPA_PLATFORM=offscreen` | 仍段错误 |
| `QTWEBENGINE_DISABLE_SANDBOX=1` | 仍段错误 |
| `QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox` / `--disable-gpu` | 仍段错误 |
| `--shm-size=1g/2g`、`--ipc=host` | 仍段错误 |
| `--security-opt seccomp=unconfined`、`--privileged`、`--cap-add SYS_ADMIN` | 仍段错误 |

即崩溃与容器权限、显示、Chromium 沙箱都无关，是该镜像里的 QtWebEngine 在本机
（rootless docker）的硬崩溃。两个崩溃测试都不在 f2p/p2p 清单里，所以它只影响
"会话能否写完 junit"，不影响判分口径。

绕过方式（`eval/swebench_pro_runner.py::_verify`）：文件级调用仍是主路径；当主
路径一条 junit 结果都没有、且 reset 与两个打补丁步骤都成功时，改成只跑 f2p+p2p
的节点 id（清单写 `nodeids.txt`，入口 `node_runner.py`，脚本
`verify_fallback.sh`，日志 `verify_fallback.log`），结果里记
`test_fallback=node_ids`。兜底只在"一个结果都没有"时触发，不会覆盖已有的逐测试
结果，也不会掩盖真正的收集错误（兜底同样收集不到时仍判 `collection_error`
或 `infra_error`）。

兜底后的 gold 结果：01d1d149 → 3/3 + 46/46，1a9e74bf → 4/4 + 68/68，
两者都 `test_rc=0`。

### 10.5 对比表（B 前 / B 后 / C 正式跑）

| 指标 | B 前（宿主机 bash） | B 后（容器 bash） | 变化 |
|---|---:|---:|---|
| resolved（逐测试判分） | 5/7，**作废**（gold 泄漏） | 1/1（仅冒烟 0ea40e09） | 见 10.6 |
| patch apply rc=0 | 6/7（1 条空 patch） | 1/1 | |
| no_patch 数量 | 1（8127abbc） | 0 | |
| 平均轮数 | 23.7（166/7） | 14（单条） | 不可比 |
| 平均 tokens | 493,904 | 134,776（单条） | 不可比 |
| 成功执行测试的任务 | 0/7 | 1/1（单条内自测 2 次） | |
| gold 泄漏访问 | 3/7（有 trace 证据：942、d6、8127） | 0/1 | |

注意：B 同时修复了 gold 泄漏，所以 B 前/B 后不是单变量对比；B 前数字本身无效。若要单独衡量“自测”的作用，需要在 B 内部再做一组“容器 bash 但禁用 pytest”的对照。

**C：20 条正式跑（2026-11-08，旧 harness，max_turns=30）**

| 指标 | 数值 |
|---|---:|
| resolved | **13/20 = 65.0%**，95% Wilson [43.3%, 81.9%] |
| not_resolved（有 patch、f2p 未过） | 1（8127abbc，f2p 0/15，p2p 60/60） |
| no_patch | 6（942424e1、bec27fb4、d6d22519、08ac40d0、4b7ea297、01d1d149） |
| 轮数 | 均值 25.6，**12/20 撞 30 轮上限**（6 条失败里 5 条撞顶） |
| tokens | 合计 11,150,578，均值 557,529 |
| 单条 agent 时长 | 均值 1.8 分钟（20 条约 36 分钟） |
| patch 行数 | 中位 40，最大 128（0fc6d110） |
| 判分兜底 | 1 条（1a9e74bf，`test_fallback=node_ids`） |

该轮用的 harness 有 3 个确定性缺陷（10.6），因此 65% 是**修复前**的数字，偏低。

### 10.6 正式跑归因：旧 harness 的三处缺陷与修复

逐条读 20 份 `agent_trace.json`，20 条实例共遇到 90 次判据拒绝，其中 33 次是误杀
（命令层 19、路径层 14），其余是设计内的联网与全盘搜索拒绝：

| 拒绝原因 | 次数 | 性质 |
|---|---:|---|
| `destructive-command policy`（全是 `find /`） | 24 | 护栏原意拦截"翻全盘找答案"，属设计内 |
| `https://` / `http://` | 21 | 多数是 `urllib`/`curl` 抓上游 fix，属设计内 |
| `curl` | 12 | 同上，属设计内 |
| `git remote` | 14 | **误杀**：14 次全是只读的 `git remote -v` |
| `path escapes workspace` | 14 | **误杀**：`/app/...` 与宿主机工作目录是同一批文件 |
| `ssh` | 5 | **误杀**：`grep ... connection/ssh.py` 命中文件名 |

三处缺陷：

1. **黑名单用子串匹配整条命令**。`CommandGuard` 是 `blocked in normalized`，
   `grep -n "class Connection" lib/ansible/plugins/connection/ssh.py` 因为文件名里有
   `ssh` 被拒；`cat > lib/ansible/utils/py3compat.py <<'EOF' ... https://www.gnu.org ...
   EOF` 因为正文里的许可证 URL 被拒（e9e60012 为此多绕一轮）。8127abbc 一条被
   `ssh.py` 误杀 5 次。
2. **文件工具与 bash 的路径空间不一致**。bash 在容器里看 `/app`，但
   `read_file`/`grep`/`edit_file` 按宿主机工作目录解析，`/app/lib/...` 直接报
   `path escapes workspace`。6 条实例共 14 次，之后都改用 bash 读取，多花轮数。
3. **prompt 没交代容器无网络**。`--network=none` 已经封死出口，但模型仍反复
   `curl raw.githubusercontent.com/<本实例的 base commit>`、`pip download`、
   翻 pip 缓存，用 30 次被拒换取"也许能拿到上游 patch"。这不是护栏缺陷，是
   提示词没有把"拿不到，只能读仓库"讲清楚。

修复（全部在 `eval/` 层，未动 `src/paicli` 核心 Agent）：

- `eval/container_bash.py`：黑名单改为**按动作单位匹配**。先按 `&& || ; |` 切段，
  剥掉 `VAR=x`、`timeout 20`、`env`、`sudo` 等前缀和 `git -C <dir>`，再拿段首词与
  `git` 子命令比对；`git remote` 只拦变更动作（`add/remove/set-url/...`），
  `show`/`get-url`/`-v` 放行。URL 字面量不再做文本扫描——沙箱 `--network=none`
  已经是更强的保证。
- 同文件新增 `create_container_tools()`：`read_file`、`write_file`、`edit_file`、
  `list_dir`、`grep`、`glob` 等 10 个文件工具把 `/app/...` 归一化成工作区相对路径，
  再交给原 handler；`/app/../x` 仍被 `PathGuard` 拒绝。
- `eval/swebench_pro_runner.py`：改用该工厂；prompt 补两句实情——"容器无网络，
  抓不到上游代码"、"bash 与文件工具看同一个仓库，用 `/app/...` 或相对路径"。

验证：`tests/test_container_bash.py` 新增 14 条用例（含 trace 里的原样命令行：
`grep ... ssh.py` 放行、`timeout 20 curl` 拦截、`GIT_PAGER=cat git fetch` 拦截、
`git remote -v` 放行 / `git remote add` 拦截、`/app` 读写成功、`/app/../x` 拒绝），
先红后绿；eval 相关 50 条全绿，`ruff check`/`format` 干净。另在实验室 GPU 机器的真实
任务镜像里跑探针：`/app` 读改成功，四类越权命令仍被拒，heredoc 内 URL 放行。

因为这三处都会改变模型可用的动作集合，13/20 不能与"修复后"混算；对外只报
同一套 harness 内跑出来的数字，修复后整套重跑的结果见 10.5.1。

逐条结果（`runs/pro_v2_20/results.jsonl`，判分口径为逐测试 f2p/p2p）：

| # | 实例 | 结果 | 轮数 | tokens | patch 行 | f2p | p2p |
|---:|---|---|---:|---:|---:|---|---|
| 1 | ansible-0ea40e09 | resolved | 11 | 89,800 | 24 | 1/1 | 15/15 |
| 2 | ansible-8127abbc | not_resolved | 30 | 868,861 | 25 | 0/15 | 60/60 |
| 3 | ansible-942424e1 | no_patch | 30 | 683,848 | 0 | – | – |
| 4 | ansible-bec27fb4 | no_patch | 30 | 757,193 | 0 | – | – |
| 5 | ansible-d6d22519 | no_patch | 30 | 697,169 | 0 | – | – |
| 6 | ansible-e0c91af4 | resolved | 25 | 314,185 | 119 | 6/6 | 0/0 |
| 7 | ansible-e9e60012 | resolved | 18 | 366,020 | 125 | 8/8 | 20/20 |
| 8 | openlibrary-08ac40d0 | no_patch | 30 | 824,116 | 0 | – | – |
| 9 | openlibrary-1894cb48 | resolved | 30 | 858,867 | 78 | 1/1 | 62/62 |
| 10 | openlibrary-2fe532a3 | resolved | 30 | 738,646 | 42 | 4/4 | 20/20 |
| 11 | openlibrary-3f580a5f | resolved | 12 | 112,638 | 75 | 6/6 | 41/41 |
| 12 | openlibrary-427f1f4e | resolved | 30 | 654,648 | 20 | 5/5 | 4/4 |
| 13 | openlibrary-43f9e7e0 | resolved | 30 | 534,123 | 51 | 1/1 | 60/60 |
| 14 | openlibrary-4b7ea297 | no_patch | 30 | 595,666 | 0 | – | – |
| 15 | qutebrowser-01d1d149 | no_patch | 30 | 708,479 | 0 | – | – |
| 16 | qutebrowser-0b621cb0 | resolved | 13 | 109,210 | 23 | 1/1 | 20/20 |
| 17 | qutebrowser-0fc6d110 | resolved | 30 | 776,883 | 128 | 4/4 | 54/54 |
| 18 | qutebrowser-1a9e74bf | resolved | 25 | 549,121 | 64 | 4/4 | 68/68 |
| 19 | qutebrowser-1af602b2 | resolved | 21 | 395,281 | 16 | 2/2 | 25/25 |
| 20 | qutebrowser-305e7c96 | resolved | 26 | 515,824 | 53 | 1/1 | 51/51 |

6 条 no_patch 的共性是**一个字都没改**（`patch_lines=0`），区别在 30 轮花在哪：
942424e1 有 16/45 次调用在翻答案（`pip cache`、`find /`、`git cat-file <base>`），
bec27fb4 8/44、4b7ea297 10/38 同类；d6d22519 只有 3/39 次是翻查，其余在
`sed`/`grep` 读源码，30 轮读完仍未落笔；08ac40d0、01d1d149 介于两者之间
（先试 `env`/`.git/config`/`pip index`，再读 `parse.py`、`blockutils.py`）。
没有一条是判分器判错——空补丁按定义就是 no_patch。

### 10.5.1 修后 harness 20 条（2026-11-08，`runs/pro_v2_20_fixed`）

同一模型（`deepseek-flash` 官方端点）、同一 `max_turns=30`，只换了 10.6 修好的
harness。因 2026-11-08 上午 DeepSeek 余额耗尽（HTTP 402）分两段跑完：前 13 条
在 01:03–01:49，后 7 条在充值后（¥9.97）续跑，`results.jsonl` 按 instance 取最后
一行合并成 20 条。

| 指标 | 数值 |
|---|---:|
| resolved | **14/20 = 70.0%**，95% Wilson [48.1%, 85.5%] |
| not_resolved（有 patch、f2p 未过） | 1（1a9e74bf，f2p 0/4，p2p 68/68） |
| no_patch | 5（d6d22519、08ac40d0、1894cb48、4b7ea297、01d1d149） |
| 轮数 | 均值 25.1，12/20 撞 30 轮上限 |
| tokens | 合计 11,285,166，均值 564,258 |
| 逐测试 | f2p 58/62，p2p 494/494 |
| 测试文件被改 | 0/20 |
| 判分兜底 | 1 条（1a9e74bf，`test_fallback=node_ids`） |

p2p 的分母取决于哪些实例成功应用了补丁（旧跑 500、修后 494），两栏不可直接相减。

与旧 harness 的逐条翻转（只看 resolved 是否翻转）：

| 实例 | 旧 harness | 修后 | 说明 |
|---|---|---|---|
| ansible-8127abbc | not_resolved（f2p 0/15） | **resolved** | 旧跑被 `ssh.py` 子串误杀 5 次 |
| ansible-942424e1 | no_patch | **resolved** | 旧跑 16/45 次调用在翻答案 |
| ansible-bec27fb4 | no_patch | **resolved** | 旧跑 8/44 次调用在翻答案 |
| openlibrary-1894cb48 | resolved | **no_patch** | 30 轮仍未落笔，改前 78 行 |
| qutebrowser-1a9e74bf | resolved（f2p 4/4） | **not_resolved**（f2p 0/4） | 出了 52 行 patch 但没修对 |

净 +1（13→14），但两端都翻转、两个 Wilson 区间大幅重叠（[43.3%,81.9%] vs
[48.1%,85.5%]），**不能把 +1 归因于 harness 修复**。可归因的部分是 8127abbc：
旧跑的失败直接对应被误杀的动作，修复后同一实例 f2p 从 0/15 变全过。942424e1、
bec27fb4 的翻转和 1894cb48、1a9e74bf 的反向翻转同属采样方差——同一模型、
`temperature=0` 仍非确定。

修后 7 条续跑的逐条结果：

| 实例 | 结果 | 轮数 | tokens | patch 行 | f2p | p2p |
|---|---|---:|---:|---:|---|---|
| openlibrary-4b7ea297 | no_patch | 30 | 742,906 | 0 | – | – |
| qutebrowser-01d1d149 | no_patch | 30 | 694,145 | 0 | – | – |
| qutebrowser-0b621cb0 | resolved | 17 | 182,519 | 22 | 1/1 | 20/20 |
| qutebrowser-0fc6d110 | resolved | 30 | 797,504 | 133 | 4/4 | 54/54 |
| qutebrowser-1a9e74bf | not_resolved | 30 | 589,777 | 52 | 0/4 | 68/68 |
| qutebrowser-1af602b2 | resolved | 30 | 703,948 | 14 | 2/2 | 25/25 |
| qutebrowser-305e7c96 | resolved | 27 | 507,397 | 53 | 1/1 | 51/51 |

残余失败构成（6 条）：6 条**全部撞 30 轮上限**。护栏拒绝总数从旧跑 90 次降到 21
次（`blacklist` 52→2、`destructive` 24→19、`path escapes workspace` 14→0）；
剩下 2 次黑名单拒绝都是 `curl`（08ac40d0、4b7ea297），说明"去网上取上游答案"的
惯性还在，但已不再是主要消耗。⇒ 剩余瓶颈是轮数预算，不是路径空间或子串误杀。

### 10.5.2 轮数消融：撞顶的 12 条 30 轮 vs 50 轮（2026-11-08）

修后跑里 `turns==30` 的 12 条（6 条已 resolved、6 条失败）在 `max_turns=50` 下重跑，
只改这一个变量，输出 `runs/pro_v2_20_fixed_t50`：

| 指标 | 30 轮 | 50 轮 |
|---|---:|---:|
| resolved | 6/12 | **9/12** |
| tokens | 9,051,075 | 18,750,340（2.07×） |
| 提前收手（<50 轮） | — | 4 条（bec27fb4 28、1af602b2 23、1a9e74bf 40、08ac40d0 43） |

| 实例 | 30 轮 | 50 轮 | 翻转 |
|---|---|---|---|
| ansible-8127abbc | resolved（979,220） | resolved（2,033,571） | – |
| ansible-942424e1 | resolved（681,847） | resolved（1,657,979） | – |
| ansible-bec27fb4 | resolved（630,441） | resolved（718,935，28 轮） | – |
| ansible-d6d22519 | no_patch（809,636） | **resolved**（1,931,313） | + |
| openlibrary-08ac40d0 | no_patch（850,609） | **resolved**（1,492,074，43 轮） | + |
| openlibrary-1894cb48 | no_patch（864,543） | **resolved**（2,110,819） | + |
| openlibrary-427f1f4e | resolved（706,499） | resolved（1,882,322） | – |
| openlibrary-4b7ea297 | no_patch（742,906） | no_patch（1,840,407） | – |
| qutebrowser-01d1d149 | no_patch（694,145） | not_resolved（1,873,622） | 部分（有补丁仍不过） |
| qutebrowser-0fc6d110 | resolved（797,504） | resolved（1,868,901） | – |
| qutebrowser-1a9e74bf | not_resolved（589,777） | not_resolved（853,144，40 轮） | – |
| qutebrowser-1af602b2 | resolved（703,948） | resolved（487,253，23 轮） | – |

三条翻转（d6d22519、08ac40d0、1894cb48）在 30 轮时都是 `patch_lines=0`——一个字
没改，不是改错；50 轮下写出了能过 f2p 的补丁，属确定可归因的预算问题。反过来
`4b7ea297` 50 轮照样零补丁、`1a9e74bf` 40 轮原地打转，说明**轮数不是唯一瓶颈**。

代价侧：已 resolved 的 6 条里 4 条把 50 轮全部花完（tokens 涨到 2–3 倍）。逐条读
trace 的尾部可以确认这**不是**"改完了还在空转"：`8127abbc`、`942424e1`、`427f1f4e`
在最后一次改动后 1–5 次调用内收手，`0fc6d110` 只隔 1 次。轮数是在**改完之前**被
花掉的，主要去向是翻隐藏测试/答案（下节量化）。

`4b7ea297` 首跑遇到一次到 `api.deepseek.com` 的连接中断，按设计记 `infra_error`
（不占分母），随后单独补跑，上表用的是补跑结果。

口径提醒：这是 12 条子集的对照，**不能**把 9/12 直接并进 20 条总分；要得到
"20 条统一 50 轮"的正式数字，需要把另外 8 条也重跑。

### 10.5.3 翻查抑制：拒绝文案 + 第 3 次硬提示（2026-11-08）

改动只在 `eval/container_bash.py`（没有动 `src/paicli`）：

1. 所有策略拒绝的返回文本都固定说明：容器无网络、无上游仓库，隐藏测试 / gold
   patch / 未来提交都不可达，不要在 `/app` 外搜索，用手上的代码改完收尾。旧文案
   只有 `command rejected by policy: curl`，等于提示换一种写法再试。
2. 按实例累计"翻答案"动作：`find /`、读 `/app` 外的 `ls/cat/grep/rg`、`pip cache/
   show/index/download/list`、`git log/rev-list/branch/tag --all`、`git cat-file/
   fsck/show-ref/count-objects/ls-remote`、`curl/wget/nc/ping/ssh/scp`。第 3 次起在
   同一工具结果里追加"第 N 次翻查、`/app` 外没有答案、收尾"的硬提示。计数挂在
   `create_container_bash_tools()` 的工具实例上，不跨实例泄漏。

同 12 条、同 50 轮，输出 `runs/pro_v2_20_fixed_t50_hint`：

| 指标 | 改前（`..._t50`） | 改后（`..._t50_hint`） |
|---|---:|---:|
| resolved | 9/12 = 75.0%，Wilson [46.8%, 91.1%] | **11/12 = 91.7%**，Wilson [64.6%, 98.5%] |
| tokens | 18,750,340 | 15,023,025（-19.9%） |
| 轮数合计 | 534 | 444（-16.9%） |
| bash 调用 | 659 | 564（-14.4%） |
| 翻查调用 | 174 | **49（-71.8%）** |
| 硬提示触发 | 0 | 26 次，分布在 10/12 条 |

| 实例 | 改前 状态/轮/翻查 | 改后 状态/轮/翻查 | tokens 改前 → 改后 |
|---|---|---|---|
| ansible-8127abbc | resolved/50/12 | resolved/36/3 | 2,033,571 → 1,298,896 |
| ansible-942424e1 | resolved/50/13 | resolved/32/4 | 1,657,979 → 989,226 |
| ansible-bec27fb4 | resolved/28/15 | resolved/29/6 | 718,935 → 909,179 |
| ansible-d6d22519 | resolved/50/13 | resolved/50/4 | 1,931,313 → 1,763,217 |
| openlibrary-08ac40d0 | resolved/43/13 | resolved/48/4 | 1,492,074 → 2,011,287 |
| openlibrary-1894cb48 | resolved/50/13 | resolved/50/3 | 2,110,819 → 1,629,630 |
| openlibrary-427f1f4e | resolved/50/18 | resolved/30/4 | 1,882,322 → 1,096,881 |
| openlibrary-4b7ea297 | no_patch/50/19 | **resolved**/50/5 | 1,840,407 → 1,957,426 |
| qutebrowser-01d1d149 | not_resolved/50/26 | **resolved**/37/8 | 1,873,622 → 1,155,466 |
| qutebrowser-0fc6d110 | resolved/50/8 | resolved/47/2 | 1,868,901 → 1,629,024 |
| qutebrowser-1a9e74bf | not_resolved/40/14 | not_resolved/24/5 | 853,144 → 428,125 |
| qutebrowser-1af602b2 | resolved/23/10 | resolved/11/1 | 487,253 → 154,668 |

可归因：提示确实送达并改变了行为（26 次硬提示、翻查调用 174 → 49 是机制层的
确定性变化）；两条翻正 `4b7ea297`（f2p 24/24）与 `01d1d149`（f2p 3/3）恰好是改前
翻查最多的两条（19、26 次）；无 resolved → 失败的反向翻转，所有 resolved 条 f2p
全过、p2p 无回归。

不可归因：9/12 → 11/12 本身——12 条子集、Wilson 区间重叠，属噪声。`1a9e74bf` 翻查
14 → 5 次但仍 f2p 0/4，说明"停止翻答案"不等于"会修"；`bec27fb4` 29 轮 / 909,179
tokens、`08ac40d0` 48 轮 / 2,011,287 tokens 比改前更贵，是采样波动。

口径提醒：与 10.5.2 相同，这是 12 条子集对照，不能并入 20 条总分。

### 10.5.4 补齐"20 条统一 50 轮"（2026-11-08）

30 轮时未撞顶的 8 条（30 轮下全部 resolved）用当前 harness（含 10.5.3 的提示）、
`max_turns=50` 重跑，输出 `runs/pro_v2_20_fixed_t50_rest`：

| 实例 | 状态 | 轮数 | tokens | f2p | p2p | 翻查 / 提示 |
|---|---|---:|---:|---|---|---|
| ansible-0ea40e09 | resolved | 8 | 72,809 | 1/1 | 15/15 | 0 / 0 |
| ansible-e0c91af4 | resolved | 17 | 196,557 | 6/6 | 0/0 | 1 / 0 |
| ansible-e9e60012 | resolved | 25 | 515,962 | 8/8 | 20/20 | 3 / 1 |
| openlibrary-2fe532a3 | resolved | 16 | 251,999 | 4/4 | 20/20 | 2 / 0 |
| openlibrary-3f580a5f | resolved | 10 | 103,349 | 6/6 | 41/41 | 0 / 0 |
| openlibrary-43f9e7e0 | resolved | 35 | 916,671 | 1/1 | 60/60 | 7 / 5 |
| qutebrowser-0b621cb0 | resolved | 10 | 96,529 | 1/1 | 20/20 | 3 / 1 |
| qutebrowser-305e7c96 | resolved | 22 | 418,412 | 1/1 | 51/51 | 0 / 0 |

合并口径（同一模型、当前 harness、20 条统一 50 轮）：

| 指标 | 30 轮（修后 harness，无提示） | 50 轮（当前 harness） |
|---|---:|---:|
| resolved | 14/20 = 70.0%，Wilson [48.1%, 85.5%] | **19/20 = 95.0%**，Wilson [76.4%, 99.1%] |
| tokens | 11,285,166 | 17,595,313（1.56×） |
| 失败 | 5 no_patch + 1 not_resolved | `1a9e74bf`（f2p 0/4） |

8 条子集 **8/8 resolved**，全部在 8–35 轮内收手、无一撞 50 轮；tokens 合计
2,572,288、翻查 16 次、硬提示 7 次（`43f9e7e0` 占 5 次，仍 35 轮通过）。这批在
30 轮时本来就全过，50 轮没有新增信息，作用只是补齐正式口径。

**不能**把 19/20 与 14/20 的差当作某一项优化的效果：它混了轮数（30→50）、提示
（E7）与采样波动三项，且两个 Wilson 区间在 76.4–85.5% 重叠。单变量证据看 10.5.2
（只改轮数：12 条 6/12 → 9/12）与 10.5.3（只改提示：12 条 9/12 → 11/12）。

## 11. 下一步

1. B 方案已实现，单条冒烟见 10.4.5
2. 逐测试 f2p/p2p 判分已完成，7 条旧 patch 重判见 10.4.6（只验证判分器，不作成绩）
3. 20 个逐实例镜像已全部就绪（服务器直连 ghcr 只有 0.18–0.31 MB/s，改为本机
   下载 blob + 上传 + `docker import`，5.89 GB / 0.45 MB/s / 约 3.6 小时）
4. 20/20 gold 预检已完成并全绿（10.4.7、10.4.7.2）
5. 20 条正式跑第一轮已完成：13/20 = 65.0%（Wilson [43.3%, 81.9%]），但用旧
   harness，见 10.5 的 C 表与 10.6 的归因
6. harness 三处缺陷已修（精确命令拦截、`/app` 文件工具、prompt 说明无网络），
   本地 50 条测试 + 真实容器探针通过；修后 20 条已跑完：**14/20 = 70.0%**
   （Wilson [48.1%, 85.5%]），见 10.5.1
7. 轮数消融已完成：撞顶的 12 条 50 轮重跑 6/12 → 9/12、无回退、tokens 2.07 倍，
   见 10.5.2；结论是"3 条确属预算不足、3 条与轮数无关，已 resolved 的 4 条是在
   改完前把轮数花在翻查上"。要做"20 条统一 50 轮"的正式数字，还需重跑未撞顶的 8 条
8. 翻查抑制已完成：拒绝文案 + 第 3 次硬提示，同 12 条 50 轮下 9/12 → 11/12、
   翻查 174 → 49、tokens -19.9%，见 10.5.3；`1a9e74bf` 仍 f2p 0/4，说明停止翻查
   不等于会修
9. 剩余 8 条 50 轮已补齐（8/8），合并得到"20 条统一 50 轮"正式成绩
   **19/20 = 95.0%**（Wilson [76.4%, 99.1%]）、tokens 1.56 倍，见 10.5.4
10. B 内部再做“允许自测 vs 禁用 pytest”对照，隔离自测变量
11. 接官方 Harbor task/parser，与自研判分互校
