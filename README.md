# 临床研究与生产来源资料工作台

本目录包含研究同意范围、站点提交、设备批次和评价记录的合成资料，以及一个
把它们持续接收、统一解析并按角色投影成三个视图的工作台。样例用虚构编号表示
人员、站点、器械和批次，**不含真实患者资料或商业产品信息**。

## 设计目标

同一份来源变化，必须让三类使用者看到**一致但各自有意义**的结果：

| 视图 | 关注点 | 入口 |
| --- | --- | --- |
| 资料接收（ingestion） | 收进了什么、拒收了什么、哪些来源被改写或撤回 | `ingestion_view` |
| 研究判断（research） | 纳入依据、缺失项、结论状态、证据版本 | `research_view` |
| 生产追溯（production） | 批次身份与更正、关联证据、证据版本是否完整 | `production_view` |

三个视图都读取**同一个** `Snapshot`，因此它们对同一次来源变化必然一致；差异
只在展示的侧面，而不在底层事实。

## 核心规则

1. **同意范围与有效时间决定纳入。**
   - 提交引用的 `consent_revision` 必须对应到某个 `study_scope` 版本。
   - 声明的 `consent_scope` 与报告期必须落在该版本的范围内。
   - 报告期只与有效期部分重叠时标记 `partial`，不会静默截断。
   - 结果：`included` / `out_of_scope` / `insufficient_basis`。

2. **缺失不等于零事件。**
   - 只有随访明确给出 `events_observed: 0` 才是 `no_events_observed`。
   - 没有随访、随访未给事件数、或随访不完整，一律是 `insufficient`。
   - 汇总里 `submissions_with_no_events_observed` 与
     `submissions_with_insufficient_data` 分列，永不合并。

3. **更正不改写既有评价依据。**
   - 解析可按任意时间点 `cutoff` 重建当时的状态。
   - 评价记录以其 `decided_at` 时刻冻结依据；后续更正只改变“当前版本”，
     不改变“当时依据”。

4. **器械/批次只按精确身份关联。**
   - 只有 `device_use` 的 `device_ref` 与 `lot_ref` 同时精确匹配某个批次时，
     关联才算可证明（`provable`）。
   - 名称相近、产品名相似、批次号前缀相同都**不会**被自动归并。
   - 批次被更正（例如改名）后，仍指向旧标识的使用记录变为
     `association_not_provable`，需要站点补交才能恢复。

5. **关键来源撤回的影响范围可追溯。**
   - 撤回可指向具体来源、提交或批次；工作台解析出受影响的研究记录与批次。
   - 未发布的评价不再能作为完整证据（`usable_as_complete_evidence = false`）。
   - 已发布的结论**保留原文与历史版本**，同时标记 `needs_review` 并给出需
     复核的提交与批次范围（`review_scope`）。

## 快速开始

需要 Python 3.11 或更新版本，无第三方依赖。

```sh
python3 -m unittest discover -s tests
```

### 命令行

```sh
# 总体概览（含需复核的评价）
python3 -m clinical_workbench summary fixtures/sources.json

# 研究判断：纳入依据、缺失项、结论
python3 -m clinical_workbench research fixtures/sources.json

# 生产追溯：批次身份、关联证据、证据版本
python3 -m clinical_workbench production fixtures/sources.json

# 连续接收：多个批次文档一起解析
python3 -m clinical_workbench production \
  fixtures/scenarios/lot-chain-batch-1.json \
  fixtures/scenarios/lot-chain-batch-2.json

# 重建某一时刻的状态
python3 -m clinical_workbench as-of 2026-07-26T00:00:00Z \
  fixtures/scenarios/withdrawal-batch-1.json \
  fixtures/scenarios/withdrawal-batch-2.json

# 所有命令都支持 --json 输出
python3 -m clinical_workbench research fixtures/sources.json --json
```

### Python API

```python
from clinical_workbench import (
    Workbench, load_document,
    ingestion_view, research_view, production_view,
)

workbench = Workbench()
workbench.ingest(load_document("fixtures/scenarios/withdrawal-batch-1.json"), label="basis")
workbench.ingest(load_document("fixtures/scenarios/withdrawal-batch-2.json"), label="withdrawal")

snapshot = workbench.snapshot()              # 最新状态
frozen = workbench.snapshot(cutoff="2026-07-26T00:00:00Z")  # 撤回前状态

research_view(snapshot)["records"]           # 纳入依据与结论
production_view(snapshot)["lots"]            # 批次证据版本
snapshot.evaluations["EV-WD-1"].integrity    # withdrawn
```

## 来源记录类型

| `record_kind` | 用途 |
| --- | --- |
| `study_scope` | 研究同意范围的一个版本（`revision` 单调增加） |
| `site_submission` | 医院提交的病例摘要 |
| `site_follow_up` | 随访；`events_observed` 与 `data_complete` 决定结论 |
| `device_use` | 某次提交所用器械与批次（精确关联的依据） |
| `production_lot` | 生产批次主记录 |
| `lot_correction` | 批次更正（可改 `lot_ref`、`device_ref` 等） |
| `site_correction` | 病例更正（不回溯改写已决定的评价） |
| `source_withdrawal` | 撤回某来源、提交或批次 |
| `evaluation_record` | 评价结论（`status`、`decided_at`、`evidence_refs`） |

未知的 `record_kind` 会被保留并标记，但不参与判断——这让前向兼容的资料流
仍然可接收。

## 一致性保证

- 所有证据版本由内容哈希（`evidence_version`）确定，同一输入永远得到同一版本。
- 研究视图与生产视图对同一条提交引用**完全相同**的 `evidence_version`。
- 输入文件顺序不影响结果；解析按 `collected_at` 排序并归一化到 UTC。
- 同一 `source_id` 的补交保留全部历史版本，以便 `cutoff` 重建。

## 目录

```
clinical_workbench/
  model.py       信封校验、来源解析、版本历史
  util.py        规范化 JSON、哈希、日期/周期解析
  resolver.py    核心解析：范围、提交、批次、撤回、结论、评价依据
  workbench.py   连续接收与快照（含 cutoff 重建）
  views.py       面向接收 / 研究 / 生产的投影
  cli.py         命令行
fixtures/
  sources.json   初始演练资料
  scenarios/     完整链路、超窗、撤回、批次更正等合成场景
tests/           79 个单元测试
```
