# 跨算力中心的可交付资源时段仓

算力中心调度与跨中心资源交接。时段以同一工作日的 UTC 分钟表示。 资料为本地合成样本。

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

## 资料盘点

运行环境为 Python 3.11，盘点与资料校验仅使用标准库。盘点结果按到达批次显示表名及行数。

```sh
python source_inventory.py
python -m unittest discover -s tests -v
```
