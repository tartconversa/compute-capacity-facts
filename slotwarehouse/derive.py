"""把到达的原始资料推导成可对账的容量事实。

推导是确定性的：同一批到达、同一套口径（:data:`SEMANTICS_VERSION`）永远得到
同一份结果。所有结果行都带 ``source_table`` / ``source_id`` / ``intake`` /
``source_file``，以便从任何视图追回到原件。

关键口径（与 README 一致）：

* 卡的分片配置只在它的 ``span`` 内生效；整卡模式与分片模式互斥。后到的配置
  覆盖同时段先到的配置。
* 整卡被占用时，同一时段的分片用量不再重复计入；分片被占用时整卡也不能
  算作可承诺。
* 采集 coverage 没覆盖的分钟不算可交付，单独列为未覆盖。
* holds 的 excluded 与 drain 都禁止新承诺；区别是 drain 允许已有作业跑完。
* 交接接受从 ``confirmed_min`` 起生效：之前承诺在原地，之后在接手地。交接
  期两侧可以同时有物理占用，但同一预约只计一份承诺。
* 新到的资料可以缩小可兑现范围，但不会改写已签发的 placement。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .intervals import intersect, subtract, union
from .reconcile import diff_summaries, snapshot_summary
from .sources import Delivery

#: 口径版本。视图与通知都会记录它；改变解释规则时必须递增。
SEMANTICS_VERSION = "2026-11-22.1"

#: 卡的总容量，用 7 等份表示（fit7 = 7/7，fit3 = 3/7）。
CARD_UNITS = 7
TYPE_UNITS = {"fit7": 7, "fit3": 3}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(parts: Iterable[str]) -> str:
    hasher = hashlib.sha256()
    for part in sorted(parts):
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _cuts(*groups: Iterable[tuple[int, int]]) -> list[int]:
    points: set[int] = set()
    for group in groups:
        for start, end in group:
            points.add(start)
            points.add(end)
    return sorted(points)


def _elementary(span: tuple[int, int], boundaries: Iterable[int]) -> list[tuple[int, int]]:
    points = sorted({span[0], span[1]} | {p for p in boundaries if span[0] < p < span[1]})
    return [(a, b) for a, b in zip(points, points[1:]) if a < b]


def _touch(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return max(a[0], b[0]) < min(a[1], b[1])


def _minutes(spans: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in union(spans))


@dataclass(frozen=True)
class Annotated:
    table: str
    record: Mapping[str, Any]
    intake: int
    file: str

    @property
    def id(self) -> str:
        return str(self.record["id"])

    @property
    def span(self) -> tuple[int, int]:
        raw = self.record.get("span") or (0, 0)
        return (int(raw[0]), int(raw[1]))

    def get(self, key: str, default: Any = None) -> Any:
        return self.record.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.record[key]


# ---------------------------------------------------------------------------
# 拓扑
# ---------------------------------------------------------------------------


@dataclass
class TopologySegment:
    id: str
    card: str
    center: str
    fabric: str
    start: int
    end: int
    members: dict[str, str]
    intake: int
    file: str

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def mode(self) -> str:
        return "split" if self.members else "whole"

    @property
    def capacity_units(self) -> int:
        return CARD_UNITS if not self.members else sum(TYPE_UNITS[k] for k in self.members.values())

    @property
    def unusable_units(self) -> int:
        return CARD_UNITS - self.capacity_units

    def resources(self) -> list[dict[str, Any]]:
        if not self.members:
            return [{"resource": self.card, "member": None, "type": "fit7", "units": CARD_UNITS, "whole": 1}]
        return [
            {"resource": member, "member": member, "type": kind, "units": TYPE_UNITS[kind], "whole": 0}
            for member, kind in sorted(self.members.items())
        ]

    def to_row(self) -> dict[str, Any]:
        return {
            # 同一配置被后到配置切段后会产生多条记录，用生效起点做键；
            # config_id 保留原始来源记录 id。
            "id": f"{self.id}@{self.start}",
            "config_id": self.id,
            "card": self.card,
            "center": self.center,
            "fabric": self.fabric,
            "span_start": self.start,
            "span_end": self.end,
            "mode": self.mode,
            "members_json": _json(sorted(self.members.items())),
            "capacity_units": self.capacity_units,
            "unusable_units": self.unusable_units,
            "intake": self.intake,
            "source_file": self.file,
        }


def _parse_members(raw: Sequence[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str) or ":" not in item:
            raise ValueError(f"无法解析分片成员 {item!r}，应为 设备号:规格")
        member, kind = item.rsplit(":", 1)
        if kind not in TYPE_UNITS:
            raise ValueError(f"未知规格 {kind!r}（成员 {item!r}）")
        if not member:
            raise ValueError(f"分片成员缺少设备号: {item!r}")
        if member in parsed:
            raise ValueError(f"分片成员在同一配置里重复: {member}")
        parsed[member] = kind
    return parsed


def build_topology(
    rows: Sequence[Annotated],
    unparsed: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[TopologySegment]], dict[str, tuple[str, str]]]:
    """按卡展开配置段；后到的配置覆盖同时段先到的配置。

    无法解析的成员配置不进入拓扑，同时登记到 ``unparsed``，避免静默丢弃。
    """
    unparsed = unparsed if unparsed is not None else []
    by_card: dict[str, list[TopologySegment]] = defaultdict(list)
    member_index: dict[str, tuple[str, str]] = {}
    ordered = sorted(rows, key=lambda row: (row.intake, row.id))

    for row in ordered:
        card = str(row["card"])
        start, end = row.span
        try:
            members = _parse_members(row.get("members") or [])
        except ValueError as exc:
            unparsed.append(
                {
                    "kind": "topology_bad_members",
                    "resource": card,
                    "span_start": start,
                    "span_end": end,
                    "detail": str(exc),
                    "source_id": row.id,
                    "intake": row.intake,
                    "source_file": row.file,
                }
            )
            continue
        if not start < end:
            raise ValueError(f"拓扑 {row.id} 的 span 非法")
        # 分片成员号属于本次配置的生效时段；不能拿旧配置的分片来解析。
        for member in members:
            member_index[member] = (card, members[member])

        survivors: list[TopologySegment] = []
        for seg in by_card[card]:
            for piece in subtract([seg.span], [(start, end)]):
                survivors.append(
                    TopologySegment(
                        id=seg.id,
                        card=seg.card,
                        center=seg.center,
                        fabric=seg.fabric,
                        start=piece[0],
                        end=piece[1],
                        members=dict(seg.members),
                        intake=seg.intake,
                        file=seg.file,
                    )
                )
        survivors.append(
            TopologySegment(
                id=row.id,
                card=card,
                center=str(row["center"]),
                fabric=str(row["fabric"]),
                start=start,
                end=end,
                members=members,
                intake=row.intake,
                file=row.file,
            )
        )
        by_card[card] = sorted(survivors, key=lambda seg: (seg.start, seg.end, seg.id))
    return by_card, member_index


# ---------------------------------------------------------------------------
# 推导结果容器
# ---------------------------------------------------------------------------


@dataclass
class Derived:
    snapshot_hash: str
    batches: list[dict[str, Any]]
    topology_segments: list[dict[str, Any]]
    coverage_pieces: list[dict[str, Any]]
    coverage_gaps: list[dict[str, Any]]
    usage_facts: list[dict[str, Any]]
    usage_pieces: list[dict[str, Any]]
    hold_pieces: list[dict[str, Any]]
    commitment_pieces: list[dict[str, Any]]
    occupancy_facts: list[dict[str, Any]]
    availability_pieces: list[dict[str, Any]]
    reservations: list[dict[str, Any]]
    handoffs: list[dict[str, Any]]
    placements: list[dict[str, Any]]
    fulfillments: list[dict[str, Any]]
    commitment_gaps: list[dict[str, Any]]
    replenishment_options: list[dict[str, Any]]
    handoff_view: list[dict[str, Any]]
    competitions: list[dict[str, Any]]
    reconciliation: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    clock_min: int
    stats: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 主推导
# ---------------------------------------------------------------------------


def derive(
    deliveries: Sequence[Delivery],
    *,
    extra_reservations: Sequence[Mapping[str, Any]] = (),
    extra_handoffs: Sequence[Mapping[str, Any]] = (),
    extra_placements: Sequence[Mapping[str, Any]] = (),
    reservation_states: Mapping[str, str] | None = None,
    previous: Derived | None = None,
) -> Derived:
    """对完整到达集合做一次推导。

    ``extra_*`` 是应用层登记、不属于源到达的请求/交接/承诺凭证；它们与源
    凭证一并参与推导，但保留 ``origin='api'``。
    """
    batches = [
        {
            "intake": d.intake,
            "table_name": d.table,
            "file_name": d.name,
            "record_count": len(d.records),
            "content_hash": _digest([_json(r) for r in d.records]),
        }
        for d in deliveries
    ]

    annotated: dict[str, list[Annotated]] = defaultdict(list)
    for delivery in deliveries:
        for record in delivery.records:
            annotated[delivery.table].append(
                Annotated(table=delivery.table, record=record, intake=delivery.intake, file=delivery.name)
            )

    unresolved: list[dict[str, Any]] = []
    by_card, member_index = build_topology(annotated["topology"], unresolved)
    topology_segments = [seg.to_row() for segs in by_card.values() for seg in segs]
    card_ids = set(by_card)
    clock_min = max((max(row.span) for row in annotated["topology"]), default=0)

    card_meta: dict[str, dict[str, Any]] = {}
    for card, segs in by_card.items():
        first = min(segs, key=lambda s: (s.start, s.id))
        card_meta[card] = {
            "card": card,
            "center": first.center,
            "fabric": first.fabric,
            "centers_json": _json(sorted({s.center for s in segs})),
            "fabrics_json": _json(sorted({s.fabric for s in segs})),
        }

    def resolve(resource: str) -> tuple[str | None, str | None, str | None]:
        if resource in member_index:
            card, kind = member_index[resource]
            return card, resource, kind
        if resource in card_ids:
            return resource, None, "fit7"
        return None, None, None

    coverage, coverage_pieces = _coverage(annotated["coverage"], resolve, unresolved)
    coverage_gaps = _coverage_gaps(by_card, coverage)
    usage_facts, usage_pieces = _usage(annotated["usage"], resolve, by_card, card_meta, unresolved)
    hold_pieces = _holds(annotated["holds"], resolve, by_card, card_meta, unresolved)

    reservations = _reservations(annotated["reservations"], extra_reservations, reservation_states or {})
    handoffs = _handoffs(annotated["handoffs"], extra_handoffs)
    handoff_by_reservation = _latest_handoffs(handoffs)
    placements = _placements(annotated["placements"], extra_placements, reservations)
    commitment_pieces = _commitments(placements, handoff_by_reservation, resolve, unresolved)

    occupancy_facts, availability_pieces = _capacity_facts(
        by_card, coverage, usage_pieces, hold_pieces, commitment_pieces
    )

    fulfillments, commitment_gaps, replenishment, handoff_view = _fulfillment(
        reservations,
        handoff_by_reservation,
        by_card,
        coverage,
        usage_pieces,
        hold_pieces,
        commitment_pieces,
        placements,
    )

    competitions = _competition(reservations, by_card)
    reconciliation = _reconcile(previous, topology_segments, coverage_pieces, occupancy_facts)

    parts = [
        _json(row)
        for rows in (
            batches,
            topology_segments,
            coverage_pieces,
            coverage_gaps,
            usage_facts,
            usage_pieces,
            hold_pieces,
            commitment_pieces,
            reservations,
            handoffs,
            placements,
        )
        for row in rows
    ]
    snapshot_hash = _digest(parts)

    stats = {
        "cards": len(by_card),
        "topology_segments": len(topology_segments),
        "coverage_gap_minutes": sum(g["span_end"] - g["span_start"] for g in coverage_gaps),
        "usage_effective": sum(1 for f in usage_facts if f["status"] == "effective"),
        "usage_superseded": sum(1 for f in usage_facts if f["status"] == "superseded"),
        "reservations": len(reservations),
        "placements": len(placements),
        "handoffs": len(handoffs),
        "unresolved": len(unresolved),
    }

    return Derived(
        snapshot_hash=snapshot_hash,
        batches=batches,
        topology_segments=topology_segments,
        coverage_pieces=coverage_pieces,
        coverage_gaps=coverage_gaps,
        usage_facts=usage_facts,
        usage_pieces=usage_pieces,
        hold_pieces=hold_pieces,
        commitment_pieces=commitment_pieces,
        occupancy_facts=occupancy_facts,
        availability_pieces=availability_pieces,
        reservations=reservations,
        handoffs=handoffs,
        placements=placements,
        fulfillments=fulfillments,
        commitment_gaps=commitment_gaps,
        replenishment_options=replenishment,
        handoff_view=handoff_view,
        competitions=competitions,
        reconciliation=reconciliation,
        unresolved=unresolved,
        clock_min=clock_min,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# 采集覆盖
# ---------------------------------------------------------------------------


@dataclass
class CoverageMap:
    """按卡与按资源的采集覆盖。

    整卡行覆盖该卡的全部资源；成员行的覆盖只作用于那个成员。资源在配置里
    是否可见由拓扑决定，coverage 只回答"这个流在这段是否有人看"。
    """

    by_card: dict[str, list[tuple[int, int]]]
    by_resource: dict[str, list[tuple[int, int]]]

    def spans(self, card: str, resource: str | None = None) -> list[tuple[int, int]]:
        result = list(self.by_card.get(card, []))
        if resource is not None:
            result.extend(self.by_resource.get(resource, []))
        return union(result)

    def covered(self, card: str, resource: str | None, interval: tuple[int, int]) -> bool:
        return bool(intersect([interval], self.spans(card, resource)))

    def gaps(self, card: str, resource: str | None, span: tuple[int, int]) -> list[tuple[int, int]]:
        return subtract([span], self.spans(card, resource))


def _coverage(rows, resolve, unresolved) -> tuple[CoverageMap, list[dict[str, Any]]]:
    """拆整卡覆盖与成员覆盖；成员覆盖不再冒充整卡覆盖。"""
    by_card: dict[str, list[tuple[int, int]]] = defaultdict(list)
    by_resource: dict[str, list[tuple[int, int]]] = defaultdict(list)
    sources: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    member_card: dict[str, str] = {}
    for row in rows:
        card, member, _kind = resolve(str(row["resource"]))
        if card is None:
            unresolved.append(_unresolved_row("coverage_unknown_resource", row))
            continue
        scope = card if member is None else member
        if member is None:
            by_card[card].append(row.span)
        else:
            by_resource[member].append(row.span)
            member_card[member] = card
        sources[(card, scope)].append(
            {
                "source_id": row.id,
                "source_file": row.file,
                "intake": row.intake,
                "resource": row["resource"],
                "span": row.span,
            }
        )
    coverage = CoverageMap(
        by_card={card: union(spans) for card, spans in by_card.items()},
        by_resource={member: union(spans) for member, spans in by_resource.items()},
    )
    pieces: list[dict[str, Any]] = []
    for card, spans in sorted(coverage.by_card.items()):
        for start, end in spans:
            contributing = sorted(
                {
                    (s["source_id"], s["source_file"], s["intake"], s["resource"])
                    for s in sources[(card, card)]
                    if _touch((start, end), s["span"])
                }
            )
            pieces.append(
                {
                    "card": card,
                    "span_start": start,
                    "span_end": end,
                    "scope": "card",
                    "source_ids_json": _json(sorted({item[0] for item in contributing})),
                    "source_resources_json": _json(sorted({item[3] for item in contributing})),
                    "intake": min(item[2] for item in contributing),
                    "source_file": contributing[0][1],
                    "source_table": "coverage",
                }
            )
    for member, spans in sorted(coverage.by_resource.items()):
        card = member_card[member]
        for start, end in spans:
            contributing = sorted(
                {
                    (s["source_id"], s["source_file"], s["intake"], s["resource"])
                    for s in sources[(card, member)]
                    if _touch((start, end), s["span"])
                }
            )
            pieces.append(
                {
                    "card": card,
                    "span_start": start,
                    "span_end": end,
                    "scope": member,
                    "source_ids_json": _json(sorted({item[0] for item in contributing})),
                    "source_resources_json": _json(sorted({item[3] for item in contributing})),
                    "intake": min(item[2] for item in contributing),
                    "source_file": contributing[0][1],
                    "source_table": "coverage",
                }
            )
    return coverage, pieces


def _coverage_gaps(by_card, coverage: CoverageMap) -> list[dict[str, Any]]:
    """按资源列采集空档：整卡配置用卡号，分片配置用成员号。

    成员级 coverage 只覆盖它声明的那个成员；同一分片配置里没有覆盖的成员
    会单独列出，不会被整卡口径含糊带过。
    """
    gaps: list[dict[str, Any]] = []
    for card, segs in sorted(by_card.items()):
        union_spans = union([seg.span for seg in segs])
        horizon = (min(seg.start for seg in segs), max(seg.end for seg in segs))
        for start, end in subtract([horizon], union_spans):
            meta = card_meta_of(segs)
            gaps.append(
                {
                    "card": card,
                    "center": meta["center"],
                    "fabric": meta["fabric"],
                    "scope": card,
                    "span_start": start,
                    "span_end": end,
                    "reason": "no_topology_config",
                    "source_id": segs[0].id,
                    "intake": segs[0].intake,
                    "source_file": segs[0].file,
                }
            )
        for seg in segs:
            for resource in seg.resources():
                member = resource["member"]
                for start, end in coverage.gaps(card, member, seg.span):
                    gaps.append(
                        {
                            "card": card,
                            "center": seg.center,
                            "fabric": seg.fabric,
                            "scope": resource["resource"],
                            "span_start": start,
                            "span_end": end,
                            "reason": "no_coverage",
                            "source_id": seg.id,
                            "intake": seg.intake,
                            "source_file": seg.file,
                        }
                    )
    gaps.sort(key=lambda row: (row["card"], row["span_start"], row["span_end"], row["scope"]))
    return gaps


def card_meta_of(segs: Sequence[TopologySegment]) -> dict[str, str]:
    first = min(segs, key=lambda s: (s.start, s.id))
    return {"center": first.center, "fabric": first.fabric}


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------


def _usage(rows, resolve, by_card, card_meta, unresolved):
    rows = sorted(rows, key=lambda row: (row.intake, row.id))
    intake_by_id = {row.id: row.intake for row in rows}
    # 只有不早于被更正记录的后续凭证才算更正；早到的引用不追溯。
    replaced_ids = {
        str(row["replaces"])
        for row in rows
        if row.get("replaces")
        and row.intake >= intake_by_id.get(str(row["replaces"]), row.intake)
    }
    facts: list[dict[str, Any]] = []
    pieces: list[dict[str, Any]] = []

    for row in rows:
        effective = row.id not in replaced_ids
        resource = str(row["resource"])
        card, member, kind = resolve(resource)
        facts.append(
            {
                "usage_id": row.id,
                "usage_key": f"{row.intake:02d}:{row.id}",
                "card": card,
                "resource": resource,
                "member": member,
                "resource_type": kind,
                "span_start": row.span[0],
                "span_end": row.span[1],
                "party": row["party"],
                "job": row["job"],
                "replaces": row.get("replaces"),
                "status": "effective" if effective else "superseded",
                "intake": row.intake,
                "source_id": row.id,
                "source_file": row.file,
                "source_table": "usage",
            }
        )
        if not effective:
            continue
        if card is None:
            unresolved.append(_unresolved_row("usage_unknown_resource", row))
            continue
        segs = by_card.get(card, [])
        matched = False
        for seg in segs:
            for start, end in intersect([row.span], [seg.span]):
                matched = True
                whole = member is None
                severity = "ok"
                if member is not None:
                    if seg.mode == "whole":
                        whole, severity = True, "member_without_split_config"
                    elif member not in seg.members:
                        whole, severity = True, "member_not_in_config"
                elif seg.mode == "split":
                    severity = "whole_card_in_split_config"
                pieces.append(
                    {
                        "card": card,
                        "center": seg.center,
                        "fabric": seg.fabric,
                        "resource": resource,
                        "member": None if whole else member,
                        "resource_type": "fit7" if whole else seg.members[member],
                        "whole_card": 1 if whole else 0,
                        "span_start": start,
                        "span_end": end,
                        "party": row["party"],
                        "job": row["job"],
                        "usage_id": row.id,
                        "severity": severity,
                        "intake": row.intake,
                        "source_file": row.file,
                    }
                )
        if not matched:
            pieces.append(
                {
                    "card": card,
                    "center": card_meta[card]["center"],
                    "fabric": card_meta[card]["fabric"],
                    "resource": resource,
                    "member": None,
                    "resource_type": "fit7",
                    "whole_card": 1,
                    "span_start": row.span[0],
                    "span_end": row.span[1],
                    "party": row["party"],
                    "job": row["job"],
                    "usage_id": row.id,
                    "severity": "outside_config",
                    "intake": row.intake,
                    "source_file": row.file,
                }
            )
    pieces.sort(key=lambda p: (p["card"], p["span_start"], p["span_end"], p["resource"]))
    return facts, pieces


# ---------------------------------------------------------------------------
# 运维封锁
# ---------------------------------------------------------------------------


def _holds(rows, resolve, by_card, card_meta, unresolved) -> list[dict[str, Any]]:
    pieces: list[dict[str, Any]] = []
    for row in rows:
        resource = str(row["resource"])
        card, member, _kind = resolve(resource)
        if card is None:
            unresolved.append(_unresolved_row("hold_unknown_resource", row))
            continue
        segs = by_card.get(card, [])
        matched = False
        for seg in segs:
            for start, end in intersect([row.span], [seg.span]):
                matched = True
                pieces.append(_hold_piece(row, card, seg.center, seg.fabric, member, start, end))
        if not matched:
            meta = card_meta[card]
            pieces.append(
                _hold_piece(row, card, meta["center"], meta["fabric"], member, row.span[0], row.span[1])
            )
    pieces.sort(key=lambda p: (p["card"], p["span_start"], p["span_end"]))
    return pieces


def _hold_piece(row, card, center, fabric, member, start, end) -> dict[str, Any]:
    return {
        "card": card,
        "center": center,
        "fabric": fabric,
        "resource": row["resource"],
        "member": member,
        "scope": member or card,
        "span_start": start,
        "span_end": end,
        "kind": str(row["kind"]),
        "source_id": row.id,
        "intake": row.intake,
        "source_file": row.file,
    }


# ---------------------------------------------------------------------------
# 预约、交接、承诺
# ---------------------------------------------------------------------------


def _reservations(rows, extra, states: Mapping[str, str]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (r.intake, r.id)):
        by_id[row.id] = {
            "id": row.id,
            "party": row["party"],
            "location": row["location"],
            "span_start": row.span[0],
            "span_end": row.span[1],
            "resource_type": row["resource_type"],
            "count": int(row["count"]),
            "purpose": row["purpose"],
            "state": row["state"],
            "source_state": row["state"],
            "origin": "source",
            "intake": row.intake,
            "source_id": row.id,
            "source_file": row.file,
            "source_table": "reservations",
        }
    for row in extra:
        by_id[str(row["id"])] = {
            "id": str(row["id"]),
            "party": row["party"],
            "location": row["location"],
            "span_start": int(row["span"][0]),
            "span_end": int(row["span"][1]),
            "resource_type": row["resource_type"],
            "count": int(row["count"]),
            "purpose": row["purpose"],
            "state": row["state"],
            "source_state": row["state"],
            "origin": "api",
            "intake": 0,
            "source_id": str(row["id"]),
            "source_file": "",
            "source_table": "windows",
        }
    # 应用层推进过的状态叠加在原始状态之上；源行本身不改写。
    for reservation_id, state in states.items():
        if reservation_id in by_id:
            by_id[reservation_id]["state"] = state
    return sorted(by_id.values(), key=lambda r: (r["span_start"], r["id"]))


def _handoffs(rows, extra) -> list[dict[str, Any]]:
    result = [
        {
            "id": row.id,
            "reservation": row["reservation"],
            "from_center": row["from"],
            "to_center": row["to"],
            "confirmed_min": int(row["confirmed_min"]),
            "allowed_to_json": _json(row.get("allowed_to") or []),
            "allowed_to": list(row.get("allowed_to") or []),
            "origin": "source",
            "intake": row.intake,
            "source_id": row.id,
            "source_file": row.file,
        }
        for row in sorted(rows, key=lambda r: (r.intake, r.id))
    ]
    for row in extra:
        result.append(
            {
                "id": str(row["id"]),
                "reservation": str(row["reservation"]),
                "from_center": str(row["from_center"]),
                "to_center": str(row["to_center"]),
                "confirmed_min": int(row["confirmed_min"]),
                "allowed_to_json": _json(row.get("allowed_to") or []),
                "allowed_to": list(row.get("allowed_to") or []),
                "origin": "api",
                "intake": 0,
                "source_id": str(row["id"]),
                "source_file": "",
            }
        )
    return result


def _latest_handoffs(handoffs) -> dict[str, dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    for handoff in handoffs:
        current = chosen.get(handoff["reservation"])
        if current is None or (current["intake"], current["id"]) <= (handoff["intake"], handoff["id"]):
            chosen[handoff["reservation"]] = handoff
    return chosen


def _placements(rows, extra, reservations) -> list[dict[str, Any]]:
    party_by_reservation = {r["id"]: r["party"] for r in reservations}
    result = [
        {
            "id": row.id,
            "reservation": row["reservation"],
            "party": party_by_reservation.get(row["reservation"]),
            "center": row["center"],
            "resources_json": _json(list(row["resources"])),
            "resources": list(row["resources"]),
            "span_start": row.span[0],
            "span_end": row.span[1],
            "span": row.span,
            "confirmation": row["confirmation"],
            "origin": "source",
            "intake": row.intake,
            "source_file": row.file,
        }
        for row in sorted(rows, key=lambda r: (r.intake, r.id))
    ]
    for row in extra:
        result.append(
            {
                "id": str(row["id"]),
                "reservation": str(row["reservation"]),
                "party": row.get("party") or party_by_reservation.get(str(row["reservation"])),
                "center": str(row["center"]),
                "resources_json": _json(list(row["resources"])),
                "resources": list(row["resources"]),
                "span_start": int(row["span"][0]),
                "span_end": int(row["span"][1]),
                "span": (int(row["span"][0]), int(row["span"][1])),
                "confirmation": str(row.get("confirmation") or "recorded"),
                "origin": "api",
                "intake": 0,
                "source_file": "",
            }
        )
    return result


def _commitments(placements, handoff_by_reservation, resolve, unresolved) -> list[dict[str, Any]]:
    """把 placement 拆成交接前后两侧的有效承诺，同一预约只计一次。"""
    pieces: list[dict[str, Any]] = []
    for placement in placements:
        handoff = handoff_by_reservation.get(placement["reservation"])
        side = "none"
        effective_start, effective_end = placement["span"]
        if handoff:
            if placement["center"] == handoff["from_center"]:
                side = "from"
                effective_end = min(effective_end, handoff["confirmed_min"])
            elif placement["center"] == handoff["to_center"]:
                side = "to"
                effective_start = max(effective_start, handoff["confirmed_min"])
            else:
                side = "other"
        if effective_start >= effective_end:
            continue
        for resource in placement["resources"]:
            card, member, rtype = resolve(str(resource))
            if card is None:
                unresolved.append(
                    {
                        "kind": "placement_unknown_resource",
                        "resource": resource,
                        "span_start": placement["span"][0],
                        "span_end": placement["span"][1],
                        "source_id": placement["id"],
                        "intake": placement["intake"],
                        "source_file": placement["source_file"],
                    }
                )
                continue
            pieces.append(
                {
                    "reservation": placement["reservation"],
                    "party": placement["party"],
                    "center": placement["center"],
                    "resource": str(resource),
                    "card": card,
                    "member": member,
                    "resource_type": rtype,
                    "span_start": placement["span"][0],
                    "span_end": placement["span"][1],
                    "effective_start": effective_start,
                    "effective_end": effective_end,
                    "side": side,
                    "confirmation": placement["confirmation"],
                    "placement_id": placement["id"],
                    "intake": placement["intake"],
                    "source_file": placement["source_file"],
                    "origin": placement["origin"],
                    "dual_side": 0,
                }
            )

    # 交接期间两侧可能同时存在物理占用，但同一预约只算一份承诺：若两侧
    # 凭证在同一资源上都有物理占用，让出侧标为重复。判断不依赖具体哪一侧
    # 先在时间上结束，避免让出侧作业延后时重复计承诺。
    by_reservation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for piece in pieces:
        by_reservation[piece["reservation"]].append(piece)
    for group in by_reservation.values():
        to_assets = {
            (right["resource"], right["span_start"], right["span_end"])
            for right in group
            if right["side"] == "to"
        }
        from_assets = {
            (left["resource"], left["span_start"], left["span_end"])
            for left in group
            if left["side"] == "from"
        }
        if to_assets & from_assets:
            for left in group:
                if left["side"] == "from" and (
                    left["resource"], left["span_start"], left["span_end"]
                ) in (to_assets & from_assets):
                    left["dual_side"] = 1
    pieces.sort(key=lambda p: (p["reservation"], p["center"], p["span_start"], p["resource"]))
    return pieces


# ---------------------------------------------------------------------------
# 容量事实：占用 / 可用
# ---------------------------------------------------------------------------


def _capacity_facts(by_card, coverage, usage_pieces, hold_pieces, commitment_pieces):
    facts: list[dict[str, Any]] = []
    availability: list[dict[str, Any]] = []

    usage_by_card: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    holds_by_card: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    commits_by_card: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in usage_pieces:
        usage_by_card[row["card"]].append(row)
    for row in hold_pieces:
        holds_by_card[row["card"]].append(row)
    for row in commitment_pieces:
        commits_by_card[row["card"]].append(row)

    for card, segs in sorted(by_card.items()):
        for seg in sorted(segs, key=lambda s: (s.start, s.id)):
            relevant_usage = [
                r for r in usage_by_card.get(card, [])
                if _touch(seg.span, (r["span_start"], r["span_end"]))
            ]
            relevant_holds = [
                r for r in holds_by_card.get(card, [])
                if _touch(seg.span, (r["span_start"], r["span_end"]))
            ]
            relevant_commits = [
                r for r in commits_by_card.get(card, [])
                if _touch(seg.span, (r["effective_start"], r["effective_end"]))
            ]
            boundaries = _cuts(
                [seg.span],
                [(r["span_start"], r["span_end"]) for r in relevant_usage],
                [(r["span_start"], r["span_end"]) for r in relevant_holds],
                [(r["effective_start"], r["effective_end"]) for r in relevant_commits],
                coverage.spans(card),
            )
            for start, end in _elementary(seg.span, boundaries):
                interval = (start, end)
                covered = coverage.covered(card, None, interval)
                whole_usage = [
                    r for r in relevant_usage
                    if r["whole_card"] and _touch(interval, (r["span_start"], r["span_end"]))
                ]
                member_usage = [
                    r for r in relevant_usage
                    if not r["whole_card"] and _touch(interval, (r["span_start"], r["span_end"]))
                ]
                holds_here = [
                    r for r in relevant_holds
                    if _touch(interval, (r["span_start"], r["span_end"]))
                ]
                commits_here = [
                    r for r in relevant_commits
                    if not r.get("dual_side")
                    and _touch(interval, (r["effective_start"], r["effective_end"]))
                ]
                base = {
                    "card": card,
                    "center": seg.center,
                    "fabric": seg.fabric,
                    "config_id": seg.id,
                    "config_mode": seg.mode,
                    "span_start": start,
                    "span_end": end,
                    "covered": 1 if covered else 0,
                }

                # 每个资源单独判定覆盖与占用：成员级 coverage 只惠及该成员。
                for res in seg.resources():
                    resource_covered = coverage.covered(card, res["member"], interval)
                    if not resource_covered:
                        facts.append(
                            {
                                **base,
                                "resource": res["resource"],
                                "resource_type": res["type"],
                                "fact_kind": "uncovered",
                                "party": None,
                                "job": None,
                                "reservation": None,
                                "share_units": 0,
                                "source_table": "coverage",
                                "source_id": seg.id,
                                "intake": seg.intake,
                                "source_file": seg.file,
                                "severity": "uncovered",
                                "note": "调度采集未覆盖此时段，不可据此承诺",
                            }
                        )

                for row in whole_usage:
                    facts.append(
                        {
                            **base,
                            "resource": row["resource"],
                            "resource_type": "fit7",
                            "fact_kind": "usage",
                            "party": row["party"],
                            "job": row["job"],
                            "reservation": None,
                            "share_units": CARD_UNITS,
                            "source_table": "usage",
                            "source_id": row["usage_id"],
                            "intake": row["intake"],
                            "source_file": row["source_file"],
                            "severity": row["severity"],
                            "note": "整卡占用",
                        }
                    )
                for row in member_usage:
                    suppressed = bool(whole_usage)
                    facts.append(
                        {
                            **base,
                            "resource": row["resource"],
                            "resource_type": row["resource_type"],
                            "fact_kind": "usage_suppressed" if suppressed else "usage",
                            "party": row["party"],
                            "job": row["job"],
                            "reservation": None,
                            "share_units": 0 if suppressed else TYPE_UNITS.get(row["resource_type"], 0),
                            "source_table": "usage",
                            "source_id": row["usage_id"],
                            "intake": row["intake"],
                            "source_file": row["source_file"],
                            "severity": row["severity"],
                            "note": "整卡占用期间子实例用量不重复计入" if suppressed else "子实例占用",
                        }
                    )
                for row in holds_here:
                    member_scoped = row["member"] is not None
                    facts.append(
                        {
                            **base,
                            "resource": row["scope"],
                            "resource_type": "fit3" if member_scoped else "fit7",
                            "fact_kind": "maintenance",
                            "party": None,
                            "job": None,
                            "reservation": None,
                            "share_units": 0,
                            "source_table": "holds",
                            "source_id": row["source_id"],
                            "intake": row["intake"],
                            "source_file": row["source_file"],
                            "severity": "blocked_new",
                            "note": "drain：已有作业继续跑完，不接新承诺"
                            if row["kind"] == "drain"
                            else "excluded：禁止新承诺",
                        }
                    )
                for row in commits_here:
                    facts.append(
                        {
                            **base,
                            "resource": row["resource"],
                            "resource_type": row["resource_type"],
                            "fact_kind": "commitment",
                            "party": row["party"],
                            "job": None,
                            "reservation": row["reservation"],
                            "share_units": TYPE_UNITS.get(row["resource_type"], 0),
                            "source_table": "placements",
                            "source_id": row["placement_id"],
                            "intake": row["intake"],
                            "source_file": row["source_file"],
                            "severity": "ok",
                            "note": f"{row['side']} 侧承诺",
                        }
                    )

                # 每个资源在这个基础时段是否仍可承诺。
                for res in seg.resources():
                    resource_covered = coverage.covered(card, res["member"], interval)
                    reasons: list[dict[str, Any]] = []
                    if not resource_covered:
                        reasons.append({"code": "uncovered", "detail": "采集未覆盖"})
                    if whole_usage:
                        reasons.append(
                            {
                                "code": "usage_whole_card",
                                "detail": "整卡被运行作业占用",
                                "usage_id": whole_usage[0]["usage_id"],
                            }
                        )
                    for m in member_usage:
                        if m["whole_card"] or res["whole"] or m["resource"] == res["resource"]:
                            reasons.append(
                                {
                                    "code": "usage_member",
                                    "detail": "子实例被占用",
                                    "usage_id": m["usage_id"],
                                    "party": m["party"],
                                    "job": m["job"],
                                }
                            )
                    for h in holds_here:
                        if h["member"] is None or res["whole"] or h["member"] == res["resource"]:
                            reasons.append(
                                {"code": "hold", "detail": h["kind"], "source_id": h["source_id"]}
                            )
                    for c in commits_here:
                        if c["member"] is None or res["whole"] or c["member"] == res["resource"]:
                            reasons.append(
                                {
                                    "code": "commitment",
                                    "detail": "已签发承诺",
                                    "reservation": c["reservation"],
                                    "placement_id": c["placement_id"],
                                }
                            )
                    status = (
                        "free"
                        if not reasons
                        else ("uncovered" if not resource_covered else "blocked")
                    )
                    availability.append(
                        {
                            "card": card,
                            "center": seg.center,
                            "fabric": seg.fabric,
                            "config_id": seg.id,
                            "config_mode": seg.mode,
                            "resource": res["resource"],
                            "member": res["member"],
                            "resource_type": res["type"],
                            "units": res["units"],
                            "span_start": start,
                            "span_end": end,
                            "status": status,
                            "reasons_json": _json(reasons),
                            "source_table": "topology",
                            "source_id": seg.id,
                            "intake": seg.intake,
                            "source_file": seg.file,
                        }
                    )
                    if status == "free":
                        facts.append(
                            {
                                **base,
                                "resource": res["resource"],
                                "resource_type": res["type"],
                                "fact_kind": "idle",
                                "party": None,
                                "job": None,
                                "reservation": None,
                                "share_units": 0,
                                "source_table": "topology",
                                "source_id": seg.id,
                                "intake": seg.intake,
                                "source_file": seg.file,
                                "severity": "ok",
                                "note": "配置内可用份额",
                            }
                        )
                if seg.mode == "split" and seg.unusable_units and covered:
                    facts.append(
                        {
                            **base,
                            "resource": seg.card,
                            "resource_type": "unallocated",
                            "fact_kind": "unusable_share",
                            "party": None,
                            "job": None,
                            "reservation": None,
                            "share_units": seg.unusable_units,
                            "source_table": "topology",
                            "source_id": seg.id,
                            "intake": seg.intake,
                            "source_file": seg.file,
                            "severity": "info",
                            "note": "配置空余份额不能当独占实例",
                        }
                    )

    facts.sort(key=lambda r: (r["card"], r["span_start"], r["span_end"], r["fact_kind"], str(r["resource"])))
    availability.sort(key=lambda r: (r["center"], r["fabric"], r["card"], r["resource"], r["span_start"]))
    return facts, availability


# ---------------------------------------------------------------------------
# 履约、缺口、补安排
# ---------------------------------------------------------------------------


def _fulfillment(
    reservations,
    handoff_by_reservation,
    by_card,
    coverage,
    usage_pieces,
    hold_pieces,
    commitment_pieces,
    placements,
):
    fulfillments: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    replenishment: list[dict[str, Any]] = []
    handoff_rows: list[dict[str, Any]] = []
    reservation_by_id = {r["id"]: r for r in reservations}

    for reservation in reservations:
        rid = reservation["id"]
        handoff = handoff_by_reservation.get(rid)
        confirmed_min = handoff["confirmed_min"] if handoff else None
        own_pieces = [
            p for p in commitment_pieces
            if p["reservation"] == rid and p["effective_start"] < p["effective_end"]
        ]
        row = {
            "reservation": rid,
            "party": reservation["party"],
            "location": reservation["location"],
            "span_start": reservation["span_start"],
            "span_end": reservation["span_end"],
            "resource_type": reservation["resource_type"],
            "count": reservation["count"],
            "purpose": reservation["purpose"],
            "state": reservation["state"],
            "origin": reservation["origin"],
            "has_handoff": 1 if handoff else 0,
            "confirmed_min": confirmed_min,
            "handoff_to": handoff["to_center"] if handoff else None,
            "allowed_to_json": _json(handoff["allowed_to"]) if handoff else "[]",
            "evidence_state": "unconfirmed",
            "commitment_minutes": 0,
            "delivered_minutes": 0,
            "gap_minutes": 0,
            "pending_minutes": 0,
            "note": "",
            "source_table": reservation["source_table"],
            "source_id": reservation["source_id"],
            "intake": reservation["intake"],
            "source_file": reservation["source_file"],
        }

        if reservation["state"] == "requested":
            row["evidence_state"] = "requested"
            fulfillments.append(row)
            continue
        if reservation["state"] not in {"confirmed", "handoff"}:
            row["evidence_state"] = f"unknown_state:{reservation['state']}"
            row["note"] = f"预约状态 {reservation['state']!r} 不在已解释的范围内"
            fulfillments.append(row)
            continue
        if not own_pieces:
            row["evidence_state"] = "pending_missing_placement"
            row["pending_minutes"] = reservation["span_end"] - reservation["span_start"]
            gaps.append(
                _gap(
                    reservation,
                    reservation["location"],
                    [],
                    reservation["span_start"],
                    reservation["span_end"],
                    "pending",
                    "missing_placement",
                    "预约已确认但没有 placement 凭证",
                    None,
                    reservation["source_id"],
                    reservation["intake"],
                    reservation["source_file"],
                )
            )
            fulfillments.append(row)
            continue

        if handoff:
            _check_handoff_coverage(
                reservation, handoff, own_pieces, gaps, confirmed_min
            )

        delivered = 0
        gap_minutes = 0
        pending = 0
        for piece in own_pieces:
            active_start = max(piece["effective_start"], reservation["span_start"])
            active_end = min(piece["effective_end"], reservation["span_end"])
            if active_start >= active_end:
                continue
            active = (active_start, active_end)
            reasons, uncovered = _resource_blocks(
                piece["card"],
                piece["resource"],
                piece["member"] is None,
                active,
                usage_pieces,
                hold_pieces,
                coverage,
            )
            for other in commitment_pieces:
                if other is piece or other["reservation"] == rid or other.get("dual_side"):
                    continue
                if other["card"] != piece["card"] or other["resource"] != piece["resource"]:
                    continue
                if _touch(active, (other["effective_start"], other["effective_end"])):
                    reasons.append(
                        {
                            "code": "commitment_conflict",
                            "reservation": other["reservation"],
                            "span": [other["effective_start"], other["effective_end"]],
                        }
                    )
            boundaries = set()
            for reason in reasons:
                boundaries.update(reason.get("span", []))
            for u_start, u_end in uncovered:
                boundaries.update((u_start, u_end))
            for start, end in _elementary(active, boundaries):
                sub = (start, end)
                sub_reasons = [
                    r
                    for r in reasons
                    if "span" not in r or _touch(sub, (r["span"][0], r["span"][1]))
                ]
                sub_uncovered = any(_touch(sub, u) for u in uncovered)
                if not sub_reasons and not sub_uncovered:
                    delivered += end - start
                    continue
                physical = [r for r in sub_reasons if r["code"] in {"usage_whole_card", "usage_member"}]
                maintenance = [r for r in sub_reasons if r["code"] == "hold"]
                conflict = [r for r in sub_reasons if r["code"] == "commitment_conflict"]
                same_party = [r for r in physical if r.get("party") == reservation["party"]]
                if (
                    physical
                    and len(same_party) == len(physical)
                    and not conflict
                    and not sub_uncovered
                ):
                    # 同一合作方自己的作业占用，说明承诺正在被使用；维护封锁
                    # 不抢占这个作业，所以这段仍算可交付。
                    delivered += end - start
                    continue
                if sub_uncovered:
                    kind, cause, detail = "pending", "coverage_missing", "采集未覆盖，无法确认交付"
                    pending += end - start
                elif maintenance:
                    kind = "confirmed"
                    cause = "maintenance"
                    detail = f"维护封锁 {maintenance[0].get('detail')}"
                    gap_minutes += end - start
                elif conflict:
                    kind, cause, detail = "confirmed", "commitment_conflict", "同一资源被其他预约承诺"
                    gap_minutes += end - start
                elif physical:
                    parties = sorted({str(r.get("party")) for r in physical})
                    kind = "confirmed"
                    cause = "foreign_usage"
                    detail = f"其他合作方作业占用: {'/'.join(parties)}"
                    gap_minutes += end - start
                else:
                    kind, cause, detail = "confirmed", "unknown", "无法解释的阻断"
                    gap_minutes += end - start
                gaps.append(
                    _gap(
                        reservation,
                        piece["center"],
                        [piece["resource"]],
                        start,
                        end,
                        kind,
                        cause,
                        detail,
                        None,
                        piece["placement_id"],
                        piece["intake"],
                        piece["source_file"],
                    )
                )

        row["commitment_minutes"] = sum(
            p["effective_end"] - p["effective_start"] for p in own_pieces
        )
        row["delivered_minutes"] = delivered
        row["gap_minutes"] = gap_minutes
        row["pending_minutes"] = pending
        if not row["evidence_state"].startswith("pending"):
            own_gaps = [g for g in gaps if g["reservation"] == rid]
            structural = [
                g
                for g in own_gaps
                if g["gap_kind"] == "pending"
                and g["cause"]
                in {
                    "missing_placement",
                    "missing_evidence_to_side",
                    "missing_evidence_from_side",
                    "unauthorized_location",
                }
            ]
            if structural:
                # 任一侧缺少凭证时，先把结论记为尚待确认；已有侧的真实缺口
                # 仍保留在 confirmed 列表里，二者不相互覆盖。
                row["evidence_state"] = "pending_gap"
            elif any(g["gap_kind"] == "confirmed" for g in own_gaps):
                row["evidence_state"] = "confirmed_gap"
            elif any(g["gap_kind"] == "pending" for g in own_gaps):
                row["evidence_state"] = "pending_gap"
            else:
                row["evidence_state"] = "fulfilled"
        if handoff:
            handoff_rows.append(
                _handoff_row(
                    handoff,
                    reservation,
                    own_pieces,
                    confirmed_min,
                    usage_pieces,
                )
            )
        fulfillments.append(row)

    replenishment = _replenishment(gaps, reservation_by_id, by_card)
    suggestion_index: dict[tuple[str, int, int], str] = {}
    for option in replenishment:
        suggestion_index.setdefault(
            (option["reservation"], option["span_start"], option["span_end"]), option["id"]
        )
    for gap in gaps:
        gap["suggestion_id"] = suggestion_index.get(
            (gap["reservation"], gap["span_start"], gap["span_end"])
        )
    return fulfillments, gaps, replenishment, handoff_rows


def _check_handoff_coverage(reservation, handoff, pieces, gaps, confirmed_min):
    rid = reservation["id"]
    to_pieces = [p for p in pieces if p["side"] == "to"]
    from_pieces = [p for p in pieces if p["side"] == "from"]
    if handoff["to_center"] not in handoff["allowed_to"]:
        gaps.append(
            _gap(
                reservation,
                handoff["to_center"],
                [],
                max(confirmed_min, reservation["span_start"]),
                reservation["span_end"],
                "pending",
                "unauthorized_location",
                f"接手地 {handoff['to_center']} 不在 allowed_to {handoff['allowed_to']} 内",
                None,
                handoff["id"],
                handoff["intake"],
                handoff["source_file"],
            )
        )
    if not to_pieces:
        gaps.append(
            _gap(
                reservation,
                handoff["to_center"],
                [],
                max(confirmed_min, reservation["span_start"]),
                reservation["span_end"],
                "pending",
                "missing_evidence_to_side",
                "交接已接受但接手侧没有 placement 凭证",
                None,
                handoff["id"],
                handoff["intake"],
                handoff["source_file"],
            )
        )
    if not from_pieces and confirmed_min > reservation["span_start"]:
        gaps.append(
            _gap(
                reservation,
                handoff["from_center"],
                [],
                reservation["span_start"],
                min(confirmed_min, reservation["span_end"]),
                "pending",
                "missing_evidence_from_side",
                "交接前让出侧没有 placement 凭证",
                None,
                handoff["id"],
                handoff["intake"],
                handoff["source_file"],
            )
        )


def _handoff_row(handoff, reservation, pieces, confirmed_min, usage_pieces):
    from_side = [p for p in pieces if p["side"] == "from"]
    to_side = [p for p in pieces if p["side"] == "to"]
    from_resources = {p["resource"] for p in from_side}
    release_ends = [
        row["span_end"]
        for row in usage_pieces
        if row["resource"] in from_resources
        and row["span_end"] > confirmed_min
        and row["span_start"] < reservation["span_end"]
    ]
    release_min = max(release_ends) if release_ends else confirmed_min
    dual_physical = (
        [confirmed_min, release_min] if from_side and to_side and release_min > confirmed_min else None
    )
    return {
        "handoff_id": handoff["id"],
        "reservation": reservation["id"],
        "party": reservation["party"],
        "from_center": handoff["from_center"],
        "to_center": handoff["to_center"],
        "confirmed_min": confirmed_min,
        "allowed_to_json": _json(handoff["allowed_to"]),
        "from_placement_json": _json(sorted({p["placement_id"] for p in from_side})),
        "to_placement_json": _json(sorted({p["placement_id"] for p in to_side})),
        "from_evidence_state": "evidenced" if from_side else "pending_missing_evidence",
        "to_evidence_state": "evidenced" if to_side else "pending_missing_evidence",
        "from_release_min": release_min,
        "dual_physical_start": dual_physical[0] if dual_physical else None,
        "dual_physical_end": dual_physical[1] if dual_physical else None,
        "counted_once": 1,
        "note": "同一预约在交接期只计一份承诺；让出侧物理占用到实际作业结束",
        "source_id": handoff["id"],
        "intake": handoff["intake"],
        "source_file": handoff["source_file"],
    }


def _gap(
    reservation,
    center,
    resources,
    start,
    end,
    kind,
    cause,
    detail,
    suggestion_id,
    source_id,
    intake,
    source_file,
):
    return {
        "reservation": reservation["id"],
        "party": reservation["party"],
        "center": center,
        "resources_json": _json(resources),
        "span_start": start,
        "span_end": end,
        "gap_kind": kind,
        "cause": cause,
        "detail": detail,
        "suggestion_id": suggestion_id,
        "source_id": source_id,
        "intake": intake,
        "source_file": source_file,
    }


def _resource_blocks(card, resource, whole_card, span, usage_pieces, hold_pieces, coverage):
    reasons: list[dict[str, Any]] = []
    for row in usage_pieces:
        if row["card"] != card or not _touch(span, (row["span_start"], row["span_end"])):
            continue
        if row["whole_card"]:
            reasons.append(
                {
                    "code": "usage_whole_card",
                    "usage_id": row["usage_id"],
                    "party": row["party"],
                    "job": row["job"],
                    "span": [row["span_start"], row["span_end"]],
                }
            )
        elif row["resource"] == resource:
            reasons.append(
                {
                    "code": "usage_member",
                    "usage_id": row["usage_id"],
                    "party": row["party"],
                    "job": row["job"],
                    "span": [row["span_start"], row["span_end"]],
                }
            )
    for row in hold_pieces:
        if row["card"] != card or not _touch(span, (row["span_start"], row["span_end"])):
            continue
        if row["member"] is None or whole_card or row["member"] == resource:
            reasons.append(
                {
                    "code": "hold",
                    "detail": row["kind"],
                    "source_id": row["source_id"],
                    "span": [row["span_start"], row["span_end"]],
                }
            )
    uncovered = coverage.gaps(card, resource, span)
    return reasons, uncovered


def _replenishment(gaps, reservation_by_id, by_card):
    options: list[dict[str, Any]] = []
    seq = 0
    for gap in gaps:
        if gap["gap_kind"] != "confirmed":
            continue
        reservation = reservation_by_id[gap["reservation"]]
        resources = json.loads(gap["resources_json"]) if gap["resources_json"] else []
        count = max(1, len(resources))
        for candidate in _candidate_groups(
            by_card,
            reservation["location"],
            reservation["resource_type"],
            count,
            gap["span_start"],
            gap["span_end"],
        ):
            seq += 1
            options.append(
                {
                    "id": f"rep-{seq:03d}",
                    "reservation": gap["reservation"],
                    "party": gap["party"],
                    "center": candidate["center"],
                    "fabric": candidate["fabric"],
                    "resources_json": _json(candidate["resources"]),
                    "span_start": gap["span_start"],
                    "span_end": gap["span_end"],
                    "resource_type": reservation["resource_type"],
                    "count": len(candidate["resources"]),
                    "requires_approval": 1,
                    "note": "仅为补安排候选；原承诺与确认依据保持不变",
                    "source_id": gap["source_id"],
                    "intake": gap["intake"],
                    "source_file": gap["source_file"],
                }
            )
    return options


def _candidate_groups(by_card, location, resource_type, count, start, end, fabric=None):
    """在指定中心的同一通信域内找 count 个整段同规格资源。

    不把跨通信域的零碎资源拼成候选；拼起来的组合通常不能兑现。
    """
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for card, segs in by_card.items():
        for seg in segs:
            # 资源必须覆盖缺口整段、且与缺口所在时段同一通信域，才算候选。
            if seg.center != location or not (seg.start <= start and end <= seg.end):
                continue
            if fabric is not None and seg.fabric != fabric:
                continue
            for res in seg.resources():
                if res["type"] == resource_type:
                    groups[(seg.center, seg.fabric)].append(res["resource"])
    result = []
    for (center, fabric), resources in sorted(groups.items()):
        unique = sorted(set(resources))
        if len(unique) >= count:
            result.append({"center": center, "fabric": fabric, "resources": unique[:count]})
    return result


# ---------------------------------------------------------------------------
# 请求竞争
# ---------------------------------------------------------------------------


def _competition(reservations, by_card):
    pending = [r for r in reservations if r["state"] == "requested"]
    rows: list[dict[str, Any]] = []
    for index, left in enumerate(pending):
        for right in pending[index + 1 :]:
            if left["location"] != right["location"]:
                continue
            if left["resource_type"] != right["resource_type"]:
                continue
            if max(left["span_start"], right["span_start"]) >= min(left["span_end"], right["span_end"]):
                continue
            overlap = (max(left["span_start"], right["span_start"]), min(left["span_end"], right["span_end"]))
            pool: dict[tuple[str, str], set[str]] = defaultdict(set)
            for _card, segs in by_card.items():
                for seg in segs:
                    if seg.center != left["location"]:
                        continue
                    if not (seg.start <= overlap[0] and overlap[1] <= seg.end):
                        continue
                    for res in seg.resources():
                        if res["type"] == left["resource_type"]:
                            pool[(seg.center, seg.fabric)].add(res["resource"])
            combined = left["count"] + right["count"]
            capacity = max((len(v) for v in pool.values()), default=0)
            rows.append(
                {
                    "left_reservation": left["id"],
                    "right_reservation": right["id"],
                    "center": left["location"],
                    "resource_type": left["resource_type"],
                    "span_start": overlap[0],
                    "span_end": overlap[1],
                    "combined_count": combined,
                    "pool_json": _json(
                        {f"{k[0]}/{k[1]}": sorted(v) for k, v in pool.items()}
                    ),
                    "verdict": "contested" if capacity < combined else "sufficient",
                    "note": "两笔未确认请求依赖同一空闲组合；后确认者将拿不到有效凭证",
                    "source_id": left["id"],
                    "intake": left["intake"],
                    "source_file": left["source_file"],
                }
            )
    return rows


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------


def _reconcile(previous, topology_rows, coverage_rows, occupancy_rows):
    if previous is None:
        return []
    before = snapshot_summary(
        previous.topology_segments, previous.coverage_pieces, previous.occupancy_facts
    )
    after = snapshot_summary(topology_rows, coverage_rows, occupancy_rows)
    rows = diff_summaries(before, after)
    if not rows and previous.snapshot_hash:
        rows.append(
            {
                "dimension": "snapshot",
                "key": "previous",
                "before_json": _json({"hash": previous.snapshot_hash}),
                "after_json": _json({"hash": "current"}),
                "delta_json": _json({}),
                "causes_json": _json([]),
                "cause": "none",
                "note": "本次推导对比上一快照：各分量与位置均未变化",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 未解析来源
# ---------------------------------------------------------------------------


def _unresolved_row(kind, row) -> dict[str, Any]:
    return {
        "kind": kind,
        "resource": row["resource"],
        "span_start": row.span[0],
        "span_end": row.span[1],
        "source_id": row.id,
        "intake": row.intake,
        "source_file": row.file,
    }
