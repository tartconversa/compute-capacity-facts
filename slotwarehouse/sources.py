"""读取并校验本地源交付。

每个文件是一次到达：``intake`` 是到达序号，``table`` 是表名，``records``
里的记录在该表内 id 唯一。这里只做结构校验，不解释业务语义；语义在
:mod:`slotwarehouse.derive` 和 :mod:`slotwarehouse.store`。

校验失败抛 :class:`SourceError`，并带上文件名与记录 id，便于定位补交件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA_FILE = "source-fields.json"

#: 允许为空的字段类型。
_NULLABLE = True


@dataclass(frozen=True)
class Delivery:
    path: Path
    intake: int
    table: str
    records: tuple[dict[str, Any], ...]

    @property
    def name(self) -> str:
        return self.path.name

    def key(self) -> str:
        return f"{self.intake:02d}-{self.table}"


class SourceError(ValueError):
    """源交付格式不合法。"""


def load_schema(root: Path) -> dict[str, dict[str, str]]:
    path = root / SCHEMA_FILE
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - 环境错误
        raise SourceError(f"无法读取 {SCHEMA_FILE}: {exc}") from exc
    if not isinstance(schema, dict) or not schema:
        raise SourceError(f"{SCHEMA_FILE} 必须是非空对象")
    return schema


def _check_value(table: str, record_id: str, name: str, kind: str, value: Any) -> None:
    where = f"{table}/{record_id}.{name}"
    if value is None:
        return
    if kind in {"range", "file_ticks"}:
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            or not value[0] < value[1]
        ):
            raise SourceError(f"{where} 必须是递增的两元整数区间，收到 {value!r}")
    elif kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise SourceError(f"{where} 必须是整数，收到 {value!r}")
    elif kind == "text":
        if not isinstance(value, str):
            raise SourceError(f"{where} 必须是文本，收到 {value!r}")
    elif kind in {"items", "ints"}:
        if not isinstance(value, list):
            raise SourceError(f"{where} 必须是列表，收到 {value!r}")
        inner = str if kind == "items" else int
        for item in value:
            if not isinstance(item, inner) or isinstance(item, bool):
                raise SourceError(f"{where} 含非法元素 {item!r}")
    else:
        raise SourceError(f"{where} 使用未知字段类型 {kind!r}")


def validate_delivery(delivery: Delivery, schema: dict[str, dict[str, str]]) -> None:
    if delivery.table not in schema:
        raise SourceError(f"{delivery.name} 的表名 {delivery.table!r} 不在 {SCHEMA_FILE} 内")
    fields = schema[delivery.table]
    seen: set[str] = set()
    for record in delivery.records:
        if not isinstance(record, dict):
            raise SourceError(f"{delivery.name} 的记录必须是对象")
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise SourceError(f"{delivery.name} 有记录缺少文本 id")
        if record_id in seen:
            raise SourceError(f"{delivery.name} 内记录 id 重复: {record_id}")
        seen.add(record_id)
        missing = set(fields) - set(record)
        extra = set(record) - set(fields)
        if missing or extra:
            raise SourceError(
                f"{delivery.name}/{record_id} 字段不匹配: 缺 {sorted(missing)} 多 {sorted(extra)}"
            )
        for name, kind in fields.items():
            _check_value(delivery.table, record_id, name, kind, record[name])


def iter_deliveries(source_dir: Path) -> Iterator[Delivery]:
    """按文件名顺序产出交付（文件名前两位即到达序号）。"""
    for path in sorted(source_dir.glob("*.json")):
        if path.name == SCHEMA_FILE:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(f"无法读取 {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"{path.name} 顶层必须是对象")
        try:
            intake = payload["intake"]
            table = payload["table"]
            records = payload["records"]
        except KeyError as exc:
            raise SourceError(f"{path.name} 缺少字段 {exc.args[0]}") from exc
        if not isinstance(intake, int) or isinstance(intake, bool) or intake < 0:
            raise SourceError(f"{path.name} 的 intake 必须是非负整数")
        if not isinstance(table, str) or not table:
            raise SourceError(f"{path.name} 的 table 必须是文本")
        if not isinstance(records, list):
            raise SourceError(f"{path.name} 的 records 必须是列表")
        yield Delivery(path=path, intake=intake, table=table, records=tuple(records))


def load_deliveries(source_dir: Path, root: Path | None = None) -> tuple[list[Delivery], dict[str, dict[str, str]]]:
    """读取并校验整份资料目录，返回按到达顺序排列的交付。"""
    source_dir = Path(source_dir)
    if root is not None:
        schema_root = Path(root)
    elif (source_dir / SCHEMA_FILE).exists():
        schema_root = source_dir
    else:
        schema_root = source_dir.parent
    schema = load_schema(schema_root)
    deliveries = list(iter_deliveries(source_dir))
    if not deliveries:
        raise SourceError(f"{source_dir} 内没有 JSON 交付")
    for delivery in deliveries:
        validate_delivery(delivery, schema)
    deliveries.sort(key=lambda d: (d.intake, d.table, d.name))
    return deliveries, schema
