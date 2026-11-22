"""窗口查找与预留交接入口。

两个视图共用同一份推导快照：

* :func:`find_window` —— 给安排下一批窗口的人：只把当前有采集覆盖、且没有被
  作业/封锁/既有承诺占住的资源算作可交付；采集空档、维护封锁、短缺分钟分列。
* :func:`fulfillment_report` —— 给核对既有承诺的人：按预约给出已交付、已确认
  缺口与尚待确认缺口，以及补安排候选。

所有写入口都先写 ``api_*`` 追加表，再重建推导；推导只读源到达与这些追加表。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .derive import SEMANTICS_VERSION, Derived, TYPE_UNITS
from .intervals import intersect, subtract, union
from .store import Warehouse


class EntryError(ValueError):
    """入口调用不合法（参数、状态或当前证据不允许）。"""


# ---------------------------------------------------------------------------
# 请求登记
# ---------------------------------------------------------------------------


def _fingerprint(request: Mapping[str, Any]) -> str:
    body = json.dumps(
        {
            key: request[key]
            for key in ("party", "location", "span", "resource_type", "count", "purpose")
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _next_seq(warehouse: Warehouse) -> int:
    return _api_seq_max(warehouse) + 1


def submit_request(
    warehouse: Warehouse,
    *,
    party: str,
    location: str,
    span: tuple[int, int],
    resource_type: str,
    count: int,
    purpose: str = "qualification",
) -> dict[str, Any]:
    """登记一笔待确认请求；完全相同的重提请求复用原请求，不重复占位。"""
    if resource_type not in TYPE_UNITS:
        raise EntryError(f"未知资源类型 {resource_type!r}")
    if not 0 < int(count):
        raise EntryError("count 必须为正整数")
    start, end = int(span[0]), int(span[1])
    if not start < end:
        raise EntryError("窗口必须 start < end")
    if not party or not location:
        raise EntryError("party 与 location 不能为空")

    request = {
        "party": party,
        "location": location,
        "span": (start, end),
        "resource_type": resource_type,
        "count": int(count),
        "purpose": purpose,
    }
    fingerprint = _fingerprint(request)
    existing = warehouse.one(
        "SELECT * FROM api_reservation WHERE party = ? AND location = ? AND span_start = ? "
        "AND span_end = ? AND resource_type = ? AND count = ? AND purpose = ? "
        "AND state IN ('requested', 'confirmed', 'handoff') ORDER BY created_seq LIMIT 1",
        (party, location, start, end, resource_type, int(count), purpose),
    )
    if existing is not None:
        return {
            "reservation": existing["id"],
            "created": False,
            "duplicate_of": existing["id"],
            "fingerprint": fingerprint,
            "state": existing["state"],
        }

    with warehouse.transaction():
        row = warehouse.one("SELECT COUNT(*) AS n FROM api_reservation")
        reservation_id = f"req-{row['n'] + 1:04d}"
        warehouse.conn.execute(
            "INSERT INTO api_reservation(id, party, location, span_start, span_end, "
            "resource_type, count, purpose, state, created_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?)",
            (reservation_id, party, location, start, end, resource_type, int(count), purpose, _next_seq(warehouse)),
        )
    warehouse.rebuild()
    return {"reservation": reservation_id, "created": True, "duplicate_of": None, "fingerprint": fingerprint, "state": "requested"}


# ---------------------------------------------------------------------------
# 窗口查找
# ---------------------------------------------------------------------------


def _free_spans(
    derived: Derived, center: str, fabric: str | None, resource: str
) -> list[tuple[int, int]]:
    rows = [
        row
        for row in derived.availability_pieces
        if row["center"] == center
        and row["resource"] == resource
        and (fabric is None or row["fabric"] == fabric)
        and row["status"] == "free"
    ]
    return union([(row["span_start"], row["span_end"]) for row in rows])


def _resource_catalog(derived: Derived, center: str) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in derived.availability_pieces:
        if row["center"] != center:
            continue
        key = (row["resource"], row["fabric"])
        if key not in seen:
            seen[key] = {
                "resource": row["resource"],
                "resource_type": row["resource_type"],
                "card": row["card"],
                "fabric": row["fabric"],
                "config_ids": [],
            }
        seen[key]["config_ids"].append(row["config_id"])
    for item in seen.values():
        item["config_ids"] = sorted(set(item["config_ids"]))
    return sorted(seen.values(), key=lambda item: (item["fabric"], item["resource"]))


def _blocked_breakdown(
    derived: Derived, center: str, resource: str, window: tuple[int, int]
) -> dict[str, Any]:
    """把窗口内每个资源的非空闲分钟按原因归类。"""
    rows = [
        row
        for row in derived.availability_pieces
        if row["center"] == center
        and row["resource"] == resource
        and max(window[0], row["span_start"]) < min(window[1], row["span_end"])
    ]
    total = window[1] - window[0]
    free = 0
    causes: dict[str, int] = {}
    details: list[dict[str, Any]] = []
    for row in rows:
        minutes = min(window[1], row["span_end"]) - max(window[0], row["span_start"])
        if row["status"] == "free":
            free += minutes
            continue
        reasons = json.loads(row["reasons_json"])
        codes = sorted({reason["code"] for reason in reasons})
        for code in codes:
            causes[code] = causes.get(code, 0) + minutes
        details.append(
            {
                "span": [max(window[0], row["span_start"]), min(window[1], row["span_end"])],
                "status": row["status"],
                "codes": codes,
                "reasons": reasons,
            }
        )
    return {
        "resource": resource,
        "free_minutes": free,
        "total_minutes": total,
        "fully_free": free >= total and bool(rows),
        "blocked_minutes": total - free,
        "causes_minutes": causes,
        "details": details,
    }


def _coverage_gaps_in_window(derived: Derived, center: str, window: tuple[int, int]) -> list[dict[str, Any]]:
    result = []
    for gap in derived.coverage_gaps:
        if gap["center"] != center:
            continue
        clipped = intersect([(gap["span_start"], gap["span_end"])], [window])
        for start, end in clipped:
            result.append(
                {
                    "card": gap["card"],
                    "fabric": gap["fabric"],
                    "span_start": start,
                    "span_end": end,
                    "reason": gap["reason"],
                    "source_id": gap["source_id"],
                    "intake": gap["intake"],
                    "source_file": gap["source_file"],
                }
            )
    return sorted(result, key=lambda row: (row["card"], row["span_start"], row["span_end"]))


def find_window(
    warehouse: Warehouse,
    *,
    center: str,
    span: tuple[int, int],
    resource_type: str,
    count: int,
    party: str | None = None,
    purpose: str | None = None,
    fabric: str | None = None,
    record_notice: bool = True,
) -> dict[str, Any]:
    """查找一个可整段交付的窗口。

    只有同 center、同 fabric、整段空闲且有采集覆盖的资源能组成可交付组合。
    零碎空闲、跨通信域拼凑、采集空档都不构成承诺依据。
    """
    if resource_type not in TYPE_UNITS:
        raise EntryError(f"未知资源类型 {resource_type!r}")
    if int(count) < 1:
        raise EntryError("count 必须为正整数")
    window = (int(span[0]), int(span[1]))
    if not window[0] < window[1]:
        raise EntryError("窗口必须 start < end")

    derived = warehouse.current()
    catalog = [
        item
        for item in _resource_catalog(derived, center)
        if item["resource_type"] == resource_type and (fabric is None or item["fabric"] == fabric)
    ]

    per_resource = []
    full_by_fabric: dict[str, list[str]] = {}
    for item in catalog:
        free_spans = _free_spans(derived, center, item["fabric"], item["resource"])
        fully_free = not subtract([window], free_spans)
        breakdown = _blocked_breakdown(derived, center, item["resource"], window)
        entry = {
            **item,
            "fully_free": fully_free,
            "free_minutes": breakdown["free_minutes"],
            "blocked_minutes": breakdown["blocked_minutes"],
            "causes_minutes": breakdown["causes_minutes"],
        }
        per_resource.append(entry)
        if fully_free:
            full_by_fabric.setdefault(item["fabric"], []).append(item["resource"])

    groups = []
    for pair_fabric, resources in sorted(full_by_fabric.items()):
        resources = sorted(resources)
        if len(resources) >= int(count):
            groups.append(
                {
                    "center": center,
                    "fabric": pair_fabric,
                    "resources": resources[: int(count)],
                    "resource_type": resource_type,
                    "span": list(window),
                    "status": "full",
                }
            )

    coverage_gaps = _coverage_gaps_in_window(derived, center, window)
    coverage_gap_minutes = sum(gap["span_end"] - gap["span_start"] for gap in coverage_gaps)

    blocker_totals: dict[str, int] = {}
    for entry in per_resource:
        for code, minutes in entry["causes_minutes"].items():
            blocker_totals[code] = max(blocker_totals.get(code, 0), minutes)

    if groups:
        verdict = "deliverable"
    elif per_resource and any(entry["free_minutes"] > 0 for entry in per_resource):
        verdict = "partial"
    elif coverage_gaps and not per_resource:
        verdict = "undeliverable"
    else:
        verdict = "undeliverable"

    request_body = {
        "center": center,
        "span": list(window),
        "resource_type": resource_type,
        "count": int(count),
        "party": party,
        "purpose": purpose,
        "fabric": fabric,
    }
    result = {
        "verdict": verdict,
        "request": request_body,
        "snapshot_hash": derived.snapshot_hash,
        "semantics_version": SEMANTICS_VERSION,
        "intake_max": _intake_max(warehouse),
        "window_minutes": window[1] - window[0],
        "groups": groups,
        "candidates": per_resource,
        "shortfall": {
            "needed": int(count),
            "best_group_size": max((len(v) for v in full_by_fabric.values()), default=0),
            "fabric_with_full_resources": sorted(full_by_fabric),
            "blocked_causes_minutes": blocker_totals,
        },
        "coverage_gaps": coverage_gaps,
        "coverage_gap_minutes": coverage_gap_minutes,
        "promise": (
            "可承诺：同通信域内整段空闲且有采集覆盖"
            if verdict == "deliverable"
            else "不可承诺：整段可交付资源不足；采集空档与零碎空闲不据此承诺"
        ),
    }
    if record_notice:
        result["notice_id"] = _record_notice(
            warehouse, derived, request_body, result, kind="window"
        )
    return result


def _intake_max(warehouse: Warehouse) -> int:
    row = warehouse.one("SELECT COALESCE(MAX(intake), 0) AS n FROM intake_batch")
    return int(row["n"])


def _api_seq_max(warehouse: Warehouse) -> int:
    row = warehouse.one(
        "SELECT COALESCE(MAX(seq), 0) AS n FROM ("
        "SELECT created_seq AS seq FROM api_reservation UNION ALL "
        "SELECT created_seq AS seq FROM api_handoff UNION ALL "
        "SELECT created_seq AS seq FROM api_placement UNION ALL "
        "SELECT updated_seq AS seq FROM api_reservation_state)"
    )
    return int(row["n"])


def _record_notice(
    warehouse: Warehouse,
    derived: Derived,
    request_body: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    kind: str,
) -> int:
    payload = {key: value for key, value in result.items() if key != "notice_id"}
    payload["notice_kind"] = kind
    # 记录通知签发时的资料与 API 追加表水位，便于之后原样复算。
    payload["api_seq_max"] = _api_seq_max(warehouse)
    with warehouse.transaction():
        cursor = warehouse.conn.execute(
            "INSERT INTO window_notice(snapshot_hash, semantics_version, intake_max, "
            "center, window_start, window_end, request_json, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                derived.snapshot_hash,
                SEMANTICS_VERSION,
                _intake_max(warehouse),
                str(request_body.get("center") or request_body.get("location") or ""),
                int(request_body["span"][0]),
                int(request_body["span"][1]),
                json.dumps(request_body, ensure_ascii=False, sort_keys=True),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        notice_id = int(cursor.lastrowid)
    return notice_id


def reproduce_notice(warehouse: Warehouse, notice_id: int) -> dict[str, Any]:
    """按通知签发时的资料水位与口径复算，验证历史结论可追溯。"""
    notice = warehouse.one("SELECT * FROM window_notice WHERE notice_id = ?", (notice_id,))
    if notice is None:
        raise EntryError(f"没有通知 {notice_id}")
    stored = json.loads(notice["result_json"])
    intake_max = int(notice["intake_max"])
    api_seq_max = int(stored.get("api_seq_max") or 0)

    deliveries = [
        delivery
        for delivery in warehouse.stored_deliveries()
        if delivery.intake <= intake_max
    ]
    from .derive import derive as derive_fn

    historical = derive_fn(
        deliveries,
        reservation_states={
            row["reservation"]: row["state"]
            for row in warehouse.conn.execute(
                "SELECT reservation, state FROM api_reservation_state WHERE updated_seq <= ?",
                (api_seq_max,),
            )
        },
        extra_reservations=[
            row for row in warehouse._api_rows("api_reservation", "reservations")
            if int(row["created_seq"]) <= api_seq_max
        ],
        extra_handoffs=[
            row for row in warehouse._api_rows("api_handoff", "handoffs")
            if int(row["created_seq"]) <= api_seq_max
        ],
        extra_placements=[
            row for row in warehouse._api_rows("api_placement", "placements")
            if int(row["created_seq"]) <= api_seq_max
        ],
    )
    matches_snapshot = historical.snapshot_hash == notice["snapshot_hash"]
    return {
        "notice_id": notice_id,
        "semantics_version": notice["semantics_version"],
        "current_semantics_version": SEMANTICS_VERSION,
        "intake_max": intake_max,
        "api_seq_max": api_seq_max,
        "snapshot_hash": notice["snapshot_hash"],
        "reproduced_snapshot_hash": historical.snapshot_hash,
        "matches_snapshot": matches_snapshot,
        "recorded_result": stored,
        "notice_issued_at": notice["issued_at"],
    }


# ---------------------------------------------------------------------------
# 预留与交接
# ---------------------------------------------------------------------------


def _reservation(warehouse: Warehouse, reservation_id: str) -> dict[str, Any]:
    row = warehouse.one("SELECT * FROM derived_reservation WHERE id = ?", (reservation_id,))
    if row is None:
        raise EntryError(f"没有预约 {reservation_id}")
    return row


def _placement_for_span(
    derived: Derived, center: str, resource: str, window: tuple[int, int]
) -> tuple[bool, list[dict[str, Any]]]:
    """资源在窗口内是否整段可承诺；返回阻断原因。"""
    rows = [
        row
        for row in derived.availability_pieces
        if row["center"] == center
        and row["resource"] == resource
        and max(window[0], row["span_start"]) < min(window[1], row["span_end"])
    ]
    if not rows:
        return False, [{"code": "unknown_resource", "detail": f"{center} 没有资源 {resource}"}]
    free_spans = union(
        [(row["span_start"], row["span_end"]) for row in rows if row["status"] == "free"]
    )
    missing = subtract([window], free_spans)
    reasons: list[dict[str, Any]] = []
    if missing:
        for row in rows:
            if row["status"] == "free":
                continue
            if not intersect([(row["span_start"], row["span_end"])], missing):
                continue
            for reason in json.loads(row["reasons_json"]):
                reasons.append(
                    {
                        **reason,
                        "span": [row["span_start"], row["span_end"]],
                    }
                )
    return not missing, reasons


def confirm_request(
    warehouse: Warehouse,
    reservation_id: str,
    *,
    center: str,
    resources: Sequence[str],
    confirmation: str = "api-confirmed",
) -> dict[str, Any]:
    """为请求签发具体资源承诺。

    确认时以当前快照复核：资源必须整段可交付；若同一空闲组合已被另一笔预约
    有效确认，后到者会被拒绝，不会出现两份建立在同一组合上的有效确认。
    """
    reservation = _reservation(warehouse, reservation_id)
    handoff = warehouse.one(
        "SELECT * FROM api_handoff WHERE reservation = ? ORDER BY created_seq DESC LIMIT 1",
        (reservation_id,),
    )
    if reservation["state"] not in {"requested", "handoff"}:
        raise EntryError(f"预约 {reservation_id} 当前状态为 {reservation['state']}，不能确认")
    if reservation["state"] == "handoff" and handoff is None:
        raise EntryError(f"预约 {reservation_id} 处于交接状态但缺少交接凭证")
    resources = list(dict.fromkeys(str(r) for r in resources))
    if len(resources) < reservation["count"]:
        raise EntryError(f"需要 {reservation['count']} 个资源，收到 {len(resources)} 个")

    center_ok = center == reservation["location"]
    if not center_ok:
        if handoff is None:
            raise EntryError(f"{center} 与承诺交付地 {reservation['location']} 不同，且没有交接凭证")
        allowed = json.loads(handoff["allowed_to_json"])
        if center != handoff["to_center"] or center not in allowed:
            raise EntryError(f"{center} 不在交接 allowed_to {allowed} 内")

    derived = warehouse.current()
    window = (int(reservation["span_start"]), int(reservation["span_end"]))
    if handoff is not None and center == handoff["to_center"]:
        window = (max(window[0], int(handoff["confirmed_min"])), window[1])

    catalog = {item["resource"] for item in _resource_catalog(derived, center)}
    for resource in resources:
        if resource not in catalog:
            raise EntryError(f"{center} 没有登记资源 {resource}")
        ok, reasons = _placement_for_span(derived, center, resource, window)
        if not ok:
            conflicts = [reason for reason in reasons if reason["code"] == "commitment"]
            covered_gap = [reason for reason in reasons if reason["code"] == "uncovered"]
            if conflicts:
                raise EntryError(
                    f"{resource} 已被预约 {conflicts[0].get('reservation')} 有效确认，"
                    "不能在同一空闲组合上再签发确认"
                )
            if covered_gap:
                raise EntryError(f"{resource} 在 {covered_gap[0]['span']} 没有采集覆盖，不能据此确认")
            raise EntryError(f"{resource} 在窗口 {window} 不可整段交付: {reasons}")

    placement_id = _new_placement_id(warehouse)
    with warehouse.transaction():
        warehouse.conn.execute(
            "INSERT INTO api_placement(id, reservation, center, resources_json, span_start, "
            "span_end, confirmation, created_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                placement_id,
                reservation_id,
                center,
                json.dumps(sorted(resources), ensure_ascii=False),
                window[0],
                window[1],
                confirmation,
                _next_seq(warehouse),
            ),
        )
        _pin_reservation_state(warehouse, reservation, "confirmed")
    warehouse.rebuild()
    return {
        "reservation": reservation_id,
        "placement": placement_id,
        "center": center,
        "resources": sorted(resources),
        "span": list(window),
        "confirmation": confirmation,
    }


def _new_placement_id(warehouse: Warehouse) -> str:
    row = warehouse.one("SELECT COUNT(*) AS n FROM api_placement")
    return f"pl-api-{row['n'] + 1:04d}"


def _pin_reservation_state(
    warehouse: Warehouse, reservation: Mapping[str, Any], state: str
) -> None:
    """记录本应用对预约状态的推进。

    源到达里的预约行保持原样（历史不可改写）；本应用推进过的状态放在
    ``api_reservation_state``，推导时叠加，用于区分"来件就是这样"与
    "我们后来确认/交接了"。
    """
    with warehouse.transaction():
        warehouse.conn.execute(
            "INSERT INTO api_reservation_state(reservation, state, updated_seq) VALUES (?, ?, ?) "
            "ON CONFLICT(reservation) DO UPDATE SET state = excluded.state, updated_seq = excluded.updated_seq",
            (reservation["id"], state, _next_seq(warehouse)),
        )


def open_handoff(
    warehouse: Warehouse,
    reservation_id: str,
    *,
    to_center: str,
    confirmed_min: int,
    allowed_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """登记换中心交接；接手侧从 ``confirmed_min`` 起才生效。"""
    reservation = _reservation(warehouse, reservation_id)
    allowed = list(allowed_to) if allowed_to is not None else [to_center]
    if to_center not in allowed:
        raise EntryError(f"接手地 {to_center} 必须出现在 allowed_to 内")
    start, end = int(reservation["span_start"]), int(reservation["span_end"])
    confirmed_min = int(confirmed_min)
    if not start <= confirmed_min < end:
        raise EntryError(f"confirmed_min 必须落在预约窗口 [{start}, {end}) 内")
    if to_center == reservation["location"]:
        raise EntryError("接手地与交付地相同，不需要交接")
    if reservation["state"] == "requested":
        raise EntryError(f"预约 {reservation_id} 尚未确认，不能开交接")

    handoff_id = _new_handoff_id(warehouse)
    with warehouse.transaction():
        warehouse.conn.execute(
            "INSERT INTO api_handoff(id, reservation, from_center, to_center, confirmed_min, "
            "allowed_to_json, created_seq) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                handoff_id,
                reservation_id,
                reservation["location"],
                to_center,
                confirmed_min,
                json.dumps(allowed, ensure_ascii=False),
                _next_seq(warehouse),
            ),
        )
        _pin_reservation_state(warehouse, reservation, "handoff")
    warehouse.rebuild()
    return {
        "handoff": handoff_id,
        "reservation": reservation_id,
        "from": reservation["location"],
        "to": to_center,
        "confirmed_min": confirmed_min,
        "allowed_to": allowed,
        "state": "handoff",
    }


def _new_handoff_id(warehouse: Warehouse) -> str:
    row = warehouse.one("SELECT COUNT(*) AS n FROM api_handoff")
    return f"h-api-{row['n'] + 1:04d}"


def reserve_window(
    warehouse: Warehouse,
    *,
    party: str,
    location: str,
    span: tuple[int, int],
    resource_type: str,
    count: int,
    purpose: str = "qualification",
    center: str | None = None,
    resources: Sequence[str] | None = None,
    fabric: str | None = None,
) -> dict[str, Any]:
    """一次完成"查窗口 + 登记 + 确认"。

    默认取：同 center、同 fabric、整段空闲的确定性首选组合。
    """
    submitted = submit_request(
        warehouse,
        party=party,
        location=location,
        span=span,
        resource_type=resource_type,
        count=count,
        purpose=purpose,
    )
    reservation_id = submitted["reservation"]
    if resources is None:
        finding = find_window(
            warehouse,
            center=center or location,
            span=span,
            resource_type=resource_type,
            count=count,
            party=party,
            purpose=purpose,
            fabric=fabric,
        )
        if finding["verdict"] != "deliverable":
            return {
                "reservation": reservation_id,
                "confirmed": False,
                "reason": "no_deliverable_group",
                "finding": finding,
            }
        group = finding["groups"][0]
        resources = group["resources"]
        center = group["center"]
    else:
        finding = None
        center = center or location
    confirmed = confirm_request(
        warehouse,
        reservation_id,
        center=center,
        resources=resources,
        confirmation="api-confirmed",
    )
    return {
        "reservation": reservation_id,
        "confirmed": True,
        "placement": confirmed,
        "finding": finding,
    }


# ---------------------------------------------------------------------------
# 履约视图
# ---------------------------------------------------------------------------


def fulfillment_report(
    warehouse: Warehouse,
    *,
    party: str | None = None,
    center: str | None = None,
    reservation: str | None = None,
    include_requested: bool = True,
) -> dict[str, Any]:
    """按合作方/中心/预约核对既有承诺，区分已确认缺口与尚待确认缺口。"""
    clauses: list[str] = []
    params: list[Any] = []
    if party is not None:
        clauses.append("f.party = ?")
        params.append(party)
    if center is not None:
        clauses.append("(f.location = ? OR f.handoff_to = ?)")
        params.extend([center, center])
    if reservation is not None:
        clauses.append("f.reservation = ?")
        params.append(reservation)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = warehouse.query(
        "SELECT f.* FROM derived_fulfillment f " + where + " ORDER BY f.span_start, f.reservation",
        params,
    )
    if not include_requested:
        rows = [row for row in rows if row["state"] != "requested"]

    result_rows = []
    for row in rows:
        gaps = warehouse.query(
            "SELECT * FROM derived_commitment_gap WHERE reservation = ? ORDER BY span_start",
            (row["reservation"],),
        )
        confirmed_gaps = [gap for gap in gaps if gap["gap_kind"] == "confirmed"]
        pending_gaps = [gap for gap in gaps if gap["gap_kind"] == "pending"]
        replenishment = warehouse.query(
            "SELECT * FROM derived_replenishment_option WHERE reservation = ? ORDER BY id",
            (row["reservation"],),
        )
        handoff = warehouse.one(
            "SELECT * FROM derived_handoff_view WHERE reservation = ?", (row["reservation"],)
        )
        result_rows.append(
            {
                **row,
                "confirmed_gaps": confirmed_gaps,
                "pending_gaps": pending_gaps,
                "replenishment": replenishment,
                "handoff": handoff,
            }
        )
    return {
        "snapshot_hash": warehouse.current().snapshot_hash,
        "semantics_version": SEMANTICS_VERSION,
        "rows": result_rows,
        "totals": {
            "reservations": len(result_rows),
            "confirmed_gap_reservations": sum(1 for row in result_rows if row["evidence_state"] == "confirmed_gap"),
            "pending_gap_reservations": sum(1 for row in result_rows if row["evidence_state"] == "pending_gap"),
            "fulfilled_reservations": sum(1 for row in result_rows if row["evidence_state"] == "fulfilled"),
            "gap_minutes": sum(row["gap_minutes"] for row in result_rows),
            "pending_minutes": sum(row["pending_minutes"] for row in result_rows),
        },
    }


__all__ = [
    "EntryError",
    "confirm_request",
    "find_window",
    "fulfillment_report",
    "open_handoff",
    "reproduce_notice",
    "reserve_window",
    "submit_request",
]
