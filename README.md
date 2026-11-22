# 跨算力中心的可交付资源时段仓

算力中心调度与跨中心资源交接。时段以同一工作日的 UTC 分钟表示。资料为本地合成样本。

本仓库提供三样东西：

1. **可持续导入的时段仓**：源交付只追加登记，推导结果可重建、可对账；
2. **窗口查找**：给安排下一批窗口的人，回答"哪个中心、哪段时段能整段承诺"；
3. **预留交接入口与履约视图**：给核对既有承诺的人，回答"已确认的承诺现在
   实际能交付多少、缺口在哪、补安排有哪些候选"。

## 源交付

`sources/` 存放 JSON 原件，文件名前两位是到达序号，同一序号内的各表属于同次交付。每个文件有 `intake`、`table`、`records`，记录 `id` 在所属表内唯一。`source-fields.json` 列出各表字段类型，空值用 `null`，空集合用 `[]`。业务时间按下方约定解释。

| 数据表 | 说明 |
| --- | --- |
| `topology` | 机架通信域与卡分片配置。 |
| `usage` | 实际已录得的作业运行凭证。 |
| `coverage` | 占用记录来源完整范围。 |
| `holds` | 运维禁止接受新工作或预留的范围。 |
| `reservations` | 预约请求与确切时段。 |
| `handoffs` | 预约交接。allowed_to 为最初明确接受的替代区域。 |
| `placements` | 先前已签发的具体资源承诺凭证。 |

## 来源约定

span 采用左闭右开，作业以运行区间占用；采集 coverage 声明该流在给定卡上的范围完整。资源号在区域内唯一。config 的 members 为空表示整卡模式，多实例成员形如设备号:规格。规格 fit7 和 fit3 分别对应物理容量的 7/7 和 3/7；配置中空余份额不能直接当成可独占的实例，整卡和子实例不能同时承诺。跨卡连用条件是同 center、同 fabric，且每个请求的 count 个资源可用满全 span。
holds 的 excluded 为禁止新承诺，drain 也不抢占正在执行的已有作业。reservation 行的 location 是承诺交付地，finish 前保持承诺，hole 可以转为已确认缺口。交接凭证的 from 和 to 对应同一预约的两地，交接接受只在 confirmed_min 开始生效；切换前重试只以一笔预约解释。record 的 replaces 对应被更正记录，保留之前的接收事实。

## 推导口径

所有结果行都带 `source_table`、`source_id`、`intake`、`source_file`，可从任何
视图追回原件。口径版本记在 `slotwarehouse.derive.SEMANTICS_VERSION`，历史窗口
通知会一并入库。

| 规则 | 落地表 |
| --- | --- |
| 分片配置只在自身 `span` 内生效；后到配置覆盖同时段先到配置 | `topology_segment` |
| 整卡占用时，同段子实例用量不重复计入 | `occupancy_fact`（`usage_suppressed`） |
| 分片被占时整卡也不可承诺；配置空余份额单列 | `availability_piece`、`unusable_share` |
| 采集未覆盖的分钟不算可交付，单独列出 | `coverage_gap`、`occupancy_fact`（`uncovered`） |
| `excluded`/`drain` 都禁止新承诺；drain 允许已有作业跑完 | `hold_piece`、`maintenance` 事实 |
| 交接从 `confirmed_min` 起在接手侧生效，让出侧承诺到该点为止 | `commitment_piece.side` |
| 交接期双侧物理占用只计一份承诺 | `commitment_piece.dual_side`、`handoff_view` |
| `replaces` 更正链：旧凭证保留但标 `superseded` | `usage_fact.status` |
| 新资料可缩小可兑现范围，但不改写已签发 `placement` | `fulfillment`、`commitment_gap` |

## 两个查询视图

### 窗口查找（安排下一批窗口）

```python
from slotwarehouse import Warehouse, find_window

with Warehouse("slotwarehouse.sqlite3") as warehouse:
    warehouse.ingest("sources")
    finding = find_window(
        warehouse, center="edge-west", span=(160, 200),
        resource_type="fit7", count=2,
    )
```

`verdict` 为 `deliverable` 时，`groups` 给出同中心、同通信域、整段空闲且有
采集覆盖的组合；`partial`/`undeliverable` 时，`candidates` 逐个资源列出
`free_minutes`、`blocked_minutes` 与按原因归类的 `causes_minutes`
（`usage_whole_card`、`usage_member`、`hold`、`commitment`、`uncovered`），
`coverage_gaps` 单列采集空档。零碎空闲或跨通信域的卡不会拼成组合。

### 履约视图（核对既有承诺）

```python
from slotwarehouse import fulfillment_report

report = fulfillment_report(warehouse, party="alpha")
```

每笔预约给出 `commitment_minutes`、`delivered_minutes`、`gap_minutes`、
`pending_minutes` 与 `evidence_state`：

* `fulfilled` —— 承诺时段全部可交付；
* `confirmed_gap` —— 有明确证据的缺口（维护封锁、其他合作方作业占用、
  承诺冲突）；
* `pending_gap` —— 尚待确认：采集空档或交接任一侧缺少凭证；
* `pending_missing_placement` 等 —— 结构性的证据缺失。

`confirmed_gaps` / `pending_gaps` 分列，`replenishment` 是补安排候选（同中心、
同通信域、覆盖缺口整段），只作建议、`requires_approval=1`，原承诺与确认依据
保持不变。

## 入口与状态

写入口先追加到 `api_*` 表，再按同一套推导重建视图：

| 调用 | 作用 |
| --- | --- |
| `submit_request(...)` | 登记待确认请求；完全相同的重提复用原请求 |
| `confirm_request(...)` | 复核当前快照后签发 placement；同一空闲组合已被有效确认时拒绝后到者 |
| `open_handoff(...)` | 登记换中心交接，接手侧从 `confirmed_min` 起生效 |
| `reserve_window(...)` | 查窗口 + 登记 + 确认一步完成 |

源交付里的预约状态不会被改写；应用推进过的状态存在 `api_reservation_state`，
推导时叠加并保留 `source_state`。已签发通知存 `window_notice`，记录快照哈希、
口径版本、资料水位与 API 水位，可用 `reproduce_notice(warehouse, id)` 原样复算。

## 对账

`derived_reconciliation` 逐卡比较前后两份快照，把可承诺分钟拆成
`capacity / covered / uncovered / usage / maintenance / commitment /
deliverable` 七个分量，并给出 `causes_json`：变化来自物理事实、拓扑/覆盖、
交接还是承诺本身一目了然；`party`、`center` 维度单列承诺分钟变化。

## 命令行

```sh
python -m slotwarehouse --db slotwarehouse.sqlite3 ingest
python -m slotwarehouse --db slotwarehouse.sqlite3 window \
    --center edge-west --start 160 --end 200 --count 2
python -m slotwarehouse --db slotwarehouse.sqlite3 fulfillment --party alpha
python -m slotwarehouse --db slotwarehouse.sqlite3 reconcile
python -m slotwarehouse --db slotwarehouse.sqlite3 notice --id 1
```

## 资料盘点与验证

运行环境为 Python 3.11，盘点、校验与时段仓仅使用标准库。

```sh
python source_inventory.py
python -m unittest discover -s tests -v
```

`tests/test_slot_warehouse.py` 覆盖：分片配置变更与跨中心接替相遇、交接期
双侧占用只计一次承诺、缺少证据时区分待确认与已确认缺口、重提请求、两份请求
竞争同一组合、确认后新到维护封锁产生缺口但不改写承诺、历史通知可复算。
