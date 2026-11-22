"""SQLite 时段仓：只追加地登记到达，再重建可追溯的推导视图。

表分三层：

* ``intake_batch`` / ``source_record`` —— 到达原件，永不覆盖；
* ``derived_*`` —— 由 :mod:`slotwarehouse.derive` 全量重建的事实与视图；
* ``warehouse_state`` —— 当前快照、口径版本与已签发通知，供历史追溯。

重建使用临时表 + 事务交换，读方不会看到半份推导。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import derive as derive_module
from .derive import SEMANTICS_VERSION, Derived, derive
from .sources import Delivery, load_deliveries

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS intake_batch (
    intake        INTEGER NOT NULL,
    table_name    TEXT    NOT NULL,
    file_name     TEXT    NOT NULL,
    record_count  INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,
    received_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (intake, table_name)
);

CREATE TABLE IF NOT EXISTS source_record (
    intake        INTEGER NOT NULL,
    table_name    TEXT    NOT NULL,
    source_id     TEXT    NOT NULL,
    payload_json  TEXT    NOT NULL,
    PRIMARY KEY (intake, table_name, source_id),
    FOREIGN KEY (intake, table_name) REFERENCES intake_batch (intake, table_name)
);

CREATE TABLE IF NOT EXISTS derived_batch (
    intake        INTEGER NOT NULL,
    table_name    TEXT    NOT NULL,
    file_name     TEXT    NOT NULL,
    record_count  INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,
    PRIMARY KEY (intake, table_name)
);

CREATE TABLE IF NOT EXISTS derived_topology_segment (
    id             TEXT PRIMARY KEY,
    config_id      TEXT NOT NULL,
    card           TEXT NOT NULL,
    center         TEXT NOT NULL,
    fabric         TEXT NOT NULL,
    span_start     INTEGER NOT NULL,
    span_end       INTEGER NOT NULL,
    mode           TEXT NOT NULL,
    members_json   TEXT NOT NULL,
    capacity_units INTEGER NOT NULL,
    unusable_units INTEGER NOT NULL,
    intake         INTEGER NOT NULL,
    source_file    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_coverage_piece (
    card                  TEXT NOT NULL,
    span_start            INTEGER NOT NULL,
    span_end              INTEGER NOT NULL,
    scope                 TEXT NOT NULL,
    source_ids_json       TEXT NOT NULL,
    source_resources_json TEXT NOT NULL,
    intake                INTEGER NOT NULL,
    source_file           TEXT NOT NULL,
    source_table          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_coverage_gap (
    card        TEXT NOT NULL,
    center      TEXT NOT NULL,
    fabric      TEXT NOT NULL,
    scope       TEXT NOT NULL,
    span_start  INTEGER NOT NULL,
    span_end    INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    intake      INTEGER NOT NULL,
    source_file TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_usage_fact (
    usage_id      TEXT PRIMARY KEY,
    usage_key     TEXT NOT NULL,
    card          TEXT,
    resource      TEXT NOT NULL,
    member        TEXT,
    resource_type TEXT,
    span_start    INTEGER NOT NULL,
    span_end      INTEGER NOT NULL,
    party         TEXT NOT NULL,
    job           TEXT NOT NULL,
    replaces      TEXT,
    status        TEXT NOT NULL,
    intake        INTEGER NOT NULL,
    source_id     TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    source_table  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_usage_piece (
    card          TEXT NOT NULL,
    center        TEXT NOT NULL,
    fabric        TEXT NOT NULL,
    resource      TEXT NOT NULL,
    member        TEXT,
    resource_type TEXT NOT NULL,
    whole_card    INTEGER NOT NULL,
    span_start    INTEGER NOT NULL,
    span_end      INTEGER NOT NULL,
    party         TEXT NOT NULL,
    job           TEXT NOT NULL,
    usage_id      TEXT NOT NULL,
    severity      TEXT NOT NULL,
    intake        INTEGER NOT NULL,
    source_file   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_usage_feature (
    usage_id   TEXT NOT NULL,
    resource   TEXT NOT NULL,
    member     TEXT,
    feature    TEXT NOT NULL,
    value      REAL NOT NULL,
    source_table TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    intake     INTEGER NOT NULL,
    source_file TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_hold_piece (
    card        TEXT NOT NULL,
    center      TEXT NOT NULL,
    fabric      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    member      TEXT,
    scope       TEXT NOT NULL,
    span_start  INTEGER NOT NULL,
    span_end    INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    intake      INTEGER NOT NULL,
    source_file TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_commitment_piece (
    reservation     TEXT NOT NULL,
    party           TEXT,
    center          TEXT NOT NULL,
    resource        TEXT NOT NULL,
    card            TEXT NOT NULL,
    member          TEXT,
    resource_type   TEXT NOT NULL,
    span_start      INTEGER NOT NULL,
    span_end        INTEGER NOT NULL,
    effective_start INTEGER NOT NULL,
    effective_end   INTEGER NOT NULL,
    side            TEXT NOT NULL,
    confirmation    TEXT NOT NULL,
    placement_id    TEXT NOT NULL,
    intake          INTEGER NOT NULL,
    source_file     TEXT NOT NULL,
    origin          TEXT NOT NULL,
    dual_side       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_occupancy_fact (
    card          TEXT NOT NULL,
    center        TEXT NOT NULL,
    fabric        TEXT NOT NULL,
    config_id     TEXT NOT NULL,
    config_mode   TEXT NOT NULL,
    span_start    INTEGER NOT NULL,
    span_end      INTEGER NOT NULL,
    covered       INTEGER NOT NULL,
    resource      TEXT,
    resource_type TEXT,
    fact_kind     TEXT NOT NULL,
    party         TEXT,
    job           TEXT,
    reservation   TEXT,
    share_units   INTEGER NOT NULL,
    source_table  TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    intake        INTEGER NOT NULL,
    source_file   TEXT NOT NULL,
    severity      TEXT NOT NULL,
    note          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_availability_piece (
    card          TEXT NOT NULL,
    center        TEXT NOT NULL,
    fabric        TEXT NOT NULL,
    config_id     TEXT NOT NULL,
    config_mode   TEXT NOT NULL,
    resource      TEXT NOT NULL,
    member        TEXT,
    resource_type TEXT NOT NULL,
    units         INTEGER NOT NULL,
    span_start    INTEGER NOT NULL,
    span_end      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    reasons_json  TEXT NOT NULL,
    source_table  TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    intake        INTEGER NOT NULL,
    source_file   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_reservation (
    id             TEXT PRIMARY KEY,
    party          TEXT NOT NULL,
    location       TEXT NOT NULL,
    span_start     INTEGER NOT NULL,
    span_end       INTEGER NOT NULL,
    resource_type  TEXT NOT NULL,
    count          INTEGER NOT NULL,
    purpose        TEXT NOT NULL,
    state          TEXT NOT NULL,
    source_state   TEXT NOT NULL,
    origin         TEXT NOT NULL,
    intake         INTEGER NOT NULL,
    source_id      TEXT NOT NULL,
    source_file    TEXT NOT NULL,
    source_table   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_handoff (
    id             TEXT PRIMARY KEY,
    reservation    TEXT NOT NULL,
    from_center    TEXT NOT NULL,
    to_center      TEXT NOT NULL,
    confirmed_min  INTEGER NOT NULL,
    allowed_to_json TEXT NOT NULL,
    origin         TEXT NOT NULL,
    intake         INTEGER NOT NULL,
    source_id      TEXT NOT NULL,
    source_file    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_placement (
    id             TEXT PRIMARY KEY,
    reservation    TEXT NOT NULL,
    party          TEXT,
    center         TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    span_start     INTEGER NOT NULL,
    span_end       INTEGER NOT NULL,
    confirmation   TEXT NOT NULL,
    origin         TEXT NOT NULL,
    intake         INTEGER NOT NULL,
    source_file    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_fulfillment (
    reservation       TEXT PRIMARY KEY,
    party             TEXT NOT NULL,
    location          TEXT NOT NULL,
    span_start        INTEGER NOT NULL,
    span_end          INTEGER NOT NULL,
    resource_type     TEXT NOT NULL,
    count             INTEGER NOT NULL,
    purpose           TEXT NOT NULL,
    state             TEXT NOT NULL,
    origin            TEXT NOT NULL,
    has_handoff       INTEGER NOT NULL,
    confirmed_min     INTEGER,
    handoff_to        TEXT,
    allowed_to_json   TEXT NOT NULL,
    evidence_state    TEXT NOT NULL,
    commitment_minutes INTEGER NOT NULL,
    delivered_minutes INTEGER NOT NULL,
    gap_minutes       INTEGER NOT NULL,
    pending_minutes   INTEGER NOT NULL,
    note              TEXT NOT NULL,
    source_table      TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    intake            INTEGER NOT NULL,
    source_file       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_commitment_gap (
    reservation   TEXT NOT NULL,
    party         TEXT NOT NULL,
    center        TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    span_start    INTEGER NOT NULL,
    span_end      INTEGER NOT NULL,
    gap_kind      TEXT NOT NULL,
    cause         TEXT NOT NULL,
    detail        TEXT NOT NULL,
    suggestion_id TEXT,
    source_id     TEXT NOT NULL,
    intake        INTEGER NOT NULL,
    source_file   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_replenishment_option (
    id             TEXT PRIMARY KEY,
    reservation    TEXT NOT NULL,
    party          TEXT NOT NULL,
    center         TEXT NOT NULL,
    fabric         TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    span_start     INTEGER NOT NULL,
    span_end       INTEGER NOT NULL,
    resource_type  TEXT NOT NULL,
    count          INTEGER NOT NULL,
    requires_approval INTEGER NOT NULL,
    note           TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    intake         INTEGER NOT NULL,
    source_file    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_handoff_view (
    handoff_id        TEXT PRIMARY KEY,
    reservation       TEXT NOT NULL,
    party             TEXT NOT NULL,
    from_center       TEXT NOT NULL,
    to_center         TEXT NOT NULL,
    confirmed_min     INTEGER NOT NULL,
    allowed_to_json   TEXT NOT NULL,
    from_placement_json TEXT NOT NULL,
    to_placement_json TEXT NOT NULL,
    from_evidence_state TEXT NOT NULL,
    to_evidence_state TEXT NOT NULL,
    from_release_min  INTEGER NOT NULL,
    dual_physical_start INTEGER,
    dual_physical_end INTEGER,
    counted_once      INTEGER NOT NULL,
    note              TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    intake            INTEGER NOT NULL,
    source_file       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_competition (
    left_reservation  TEXT NOT NULL,
    right_reservation TEXT NOT NULL,
    center            TEXT NOT NULL,
    resource_type     TEXT NOT NULL,
    span_start        INTEGER NOT NULL,
    span_end          INTEGER NOT NULL,
    combined_count    INTEGER NOT NULL,
    pool_json         TEXT NOT NULL,
    verdict           TEXT NOT NULL,
    note              TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    intake            INTEGER NOT NULL,
    source_file       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_reconciliation (
    dimension    TEXT NOT NULL,
    key          TEXT NOT NULL,
    before_json  TEXT NOT NULL,
    after_json   TEXT NOT NULL,
    delta_json   TEXT NOT NULL,
    causes_json  TEXT NOT NULL,
    cause        TEXT NOT NULL,
    note         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_unresolved (
    kind        TEXT NOT NULL,
    resource    TEXT,
    detail      TEXT,
    span_start  INTEGER,
    span_end    INTEGER,
    source_id   TEXT NOT NULL,
    intake      INTEGER NOT NULL,
    source_file TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouse_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS window_notice (
    notice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    issued_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    snapshot_hash TEXT NOT NULL,
    semantics_version TEXT NOT NULL,
    intake_max    INTEGER NOT NULL,
    center        TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    window_end    INTEGER NOT NULL,
    request_json  TEXT NOT NULL,
    result_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_reservation (
    id            TEXT PRIMARY KEY,
    party         TEXT NOT NULL,
    location      TEXT NOT NULL,
    span_start    INTEGER NOT NULL,
    span_end      INTEGER NOT NULL,
    resource_type TEXT NOT NULL,
    count         INTEGER NOT NULL,
    purpose       TEXT NOT NULL,
    state         TEXT NOT NULL,
    created_seq   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_reservation_state (
    reservation   TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    updated_seq   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_handoff (
    id            TEXT PRIMARY KEY,
    reservation   TEXT NOT NULL,
    from_center   TEXT NOT NULL,
    to_center     TEXT NOT NULL,
    confirmed_min INTEGER NOT NULL,
    allowed_to_json TEXT NOT NULL,
    created_seq   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_placement (
    id             TEXT PRIMARY KEY,
    reservation    TEXT NOT NULL,
    center         TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    span_start     INTEGER NOT NULL,
    span_end       INTEGER NOT NULL,
    confirmation   TEXT NOT NULL,
    created_seq    INTEGER NOT NULL
);

"""

#: 视图表名 -> Derived 属性名。
DERIVED_TABLES: dict[str, str] = {
    "derived_batch": "batches",
    "derived_topology_segment": "topology_segments",
    "derived_coverage_piece": "coverage_pieces",
    "derived_coverage_gap": "coverage_gaps",
    "derived_usage_fact": "usage_facts",
    "derived_usage_piece": "usage_pieces",
    "derived_hold_piece": "hold_pieces",
    "derived_commitment_piece": "commitment_pieces",
    "derived_occupancy_fact": "occupancy_facts",
    "derived_availability_piece": "availability_pieces",
    "derived_reservation": "reservations",
    "derived_handoff": "handoffs",
    "derived_placement": "placements",
    "derived_fulfillment": "fulfillments",
    "derived_commitment_gap": "commitment_gaps",
    "derived_replenishment_option": "replenishment_options",
    "derived_handoff_view": "handoff_view",
    "derived_competition": "competitions",
    "derived_reconciliation": "reconciliation",
    "derived_unresolved": "unresolved",
}



class Warehouse:
    """时段仓入口。"""

    def __init__(self, path: str | Path = ":memory:", *, source_dir: str | Path | None = None):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        default_sources = Path(__file__).resolve().parents[1] / "sources"
        self.source_dir = Path(source_dir) if source_dir is not None else default_sources
        self._last_derived: Derived | None = None
        if self._state("snapshot_hash") is not None:
            self.rebuild()

    # -- 基础 -------------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    def _state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM warehouse_state WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO warehouse_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- 导入 -------------------------------------------------------------

    def ingest(self, source_dir: str | Path | None = None) -> list[str]:
        """只追加地登记尚未见过的到达，然后重建视图。返回新到达的名字。"""
        source_dir = Path(source_dir) if source_dir is not None else self.source_dir
        schema_root = (
            source_dir
            if (source_dir / "source-fields.json").exists()
            else self.source_dir
            if (self.source_dir / "source-fields.json").exists()
            else Path(__file__).resolve().parents[1]
        )
        deliveries, _schema = load_deliveries(source_dir, root=schema_root)
        known = {
            (row["intake"], row["table_name"]): row["content_hash"]
            for row in self.conn.execute("SELECT intake, table_name, content_hash FROM intake_batch")
        }
        accepted: list[str] = []
        with self.transaction():
            for delivery in deliveries:
                key = (delivery.intake, delivery.table)
                content_hash = derive_module._digest(
                    [derive_module._json(record) for record in delivery.records]
                )
                previous = known.get(key)
                if previous is not None:
                    if previous == content_hash:
                        continue
                    raise ValueError(
                        f"到达 {delivery.name} 与已登记的同批次内容不同；补交请使用新的到达序号"
                    )
                self.conn.execute(
                    "INSERT INTO intake_batch(intake, table_name, file_name, record_count, content_hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (delivery.intake, delivery.table, delivery.name, len(delivery.records), content_hash),
                )
                self.conn.executemany(
                    "INSERT INTO source_record(intake, table_name, source_id, payload_json) VALUES (?, ?, ?, ?)",
                    [
                        (delivery.intake, delivery.table, str(record["id"]), derive_module._json(record))
                        for record in delivery.records
                    ],
                )
                accepted.append(delivery.name)
        self.rebuild()
        return accepted

    def stored_deliveries(self) -> list[Delivery]:
        rows = self.conn.execute(
            "SELECT intake, table_name, file_name FROM intake_batch ORDER BY intake, table_name"
        ).fetchall()
        deliveries: list[Delivery] = []
        for row in rows:
            records = [
                json.loads(record["payload_json"])
                for record in self.conn.execute(
                    "SELECT payload_json FROM source_record WHERE intake = ? AND table_name = ? ORDER BY source_id",
                    (row["intake"], row["table_name"]),
                )
            ]
            deliveries.append(
                Delivery(
                    path=Path(row["file_name"]),
                    intake=int(row["intake"]),
                    table=row["table_name"],
                    records=tuple(records),
                )
            )
        return deliveries

    def rebuild(self) -> Derived:
        """从已登记原件与前次快照重建全部推导视图。"""
        deliveries = self.stored_deliveries()
        if not deliveries:
            raise RuntimeError("仓库里还没有任何到达；先调用 ingest()")
        previous = self._load_previous_snapshot()
        derived = derive(
            deliveries,
            extra_reservations=self._api_rows("api_reservation", "reservations"),
            extra_handoffs=self._api_rows("api_handoff", "handoffs"),
            extra_placements=self._api_rows("api_placement", "placements"),
            reservation_states={
                row["reservation"]: row["state"]
                for row in self.conn.execute("SELECT reservation, state FROM api_reservation_state")
            },
            previous=previous,
        )
        with self.transaction():
            # 先清空再写入，保证读方不会看到新旧混杂；整个过程在一个事务里。
            for table in DERIVED_TABLES:
                self.conn.execute(f"DELETE FROM {table}")
            for table, attribute in DERIVED_TABLES.items():
                self._insert_rows(table, getattr(derived, attribute))
            self._set_state("snapshot_hash", derived.snapshot_hash)
            self._set_state("semantics_version", SEMANTICS_VERSION)
            self._set_state("clock_min", str(derived.clock_min))
            self._set_state(
                "intake_max",
                str(max((d.intake for d in deliveries), default=0)),
            )
            self._set_state("derived_at", _now())
            self._set_state(
                "counts_json",
                json.dumps(
                    {table: len(getattr(derived, attribute)) for table, attribute in DERIVED_TABLES.items()},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        self._last_derived = derived
        return derived

    def _insert_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        columns = [info[1] for info in self.conn.execute(f"PRAGMA table_info({table})")]
        payload = []
        for row in rows:
            values = []
            for column in columns:
                if column in row:
                    values.append(row[column])
                elif column == "resources_json":
                    values.append(json.dumps(row.get("resources"), ensure_ascii=False))
                else:
                    values.append(None)
            payload.append(values)
        placeholders = ", ".join("?" for _ in columns)
        self.conn.executemany(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})",
            payload,
        )

    def _api_rows(self, table: str, kind: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY created_seq")]
        if kind == "reservations":
            return [
                {
                    **row,
                    "span": (row["span_start"], row["span_end"]),
                    "span_start": row["span_start"],
                    "span_end": row["span_end"],
                }
                for row in rows
            ]
        if kind == "handoffs":
            return [{**row, "allowed_to": json.loads(row["allowed_to_json"])} for row in rows]
        return [
            {
                **row,
                "resources": json.loads(row["resources_json"]),
                "span": (row["span_start"], row["span_end"]),
            }
            for row in rows
        ]

    def _load_previous_snapshot(self) -> Derived | None:
        if not self.conn.execute("SELECT 1 FROM derived_topology_segment LIMIT 1").fetchone():
            return None
        # 只需上一轮用于对账的那部分事实。
        topology = [dict(r) for r in self.conn.execute("SELECT * FROM derived_topology_segment")]
        coverage = [dict(r) for r in self.conn.execute("SELECT * FROM derived_coverage_piece")]
        occupancy = [dict(r) for r in self.conn.execute("SELECT * FROM derived_occupancy_fact")]
        return Derived(
            snapshot_hash=self._state("snapshot_hash") or "",
            batches=[],
            topology_segments=topology,
            coverage_pieces=coverage,
            coverage_gaps=[],
            usage_facts=[],
            usage_pieces=[],
            hold_pieces=[],
            commitment_pieces=[],
            occupancy_facts=occupancy,
            availability_pieces=[],
            reservations=[],
            handoffs=[],
            placements=[],
            fulfillments=[],
            commitment_gaps=[],
            replenishment_options=[],
            handoff_view=[],
            competitions=[],
            reconciliation=[],
            unresolved=[],
            clock_min=int(self._state("clock_min") or 0),
        )

    # -- 便捷读取 ---------------------------------------------------------

    def current(self) -> Derived:
        if self._last_derived is None:
            raise RuntimeError("仓库尚未导入资料；先调用 ingest() 或 rebuild()")
        return self._last_derived

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(sql, tuple(params))]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else dict(row)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
