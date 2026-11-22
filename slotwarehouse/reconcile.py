"""按物理卡解释"可兑现范围"的变化来源。

对账不再只给一行总计，而是把每张卡的可承诺分钟拆成：

* ``capacity_minutes`` —— 配置覆盖的分钟（拓扑变了才会变）；
* ``uncovered_minutes`` —— 有配置但采集没覆盖的分钟（物理接了但看不到）；
* ``usage_minutes`` —— 被作业占用的分钟（物理事实）；
* ``maintenance_minutes`` —— 被运维封锁的分钟；
* ``commitment_minutes`` —— 已被有效承诺的分钟（同一预约在交接期只算一次）；
* ``deliverable_minutes`` —— 以上都排除后的可承诺分钟。

两个快照之间任一分量的差值都能追到具体到达与来源行，避免把"覆盖缺口"
说成"物理少卡"或把"交接"说成"承诺新增"。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .intervals import intersect, subtract, union

COMPONENTS = (
    "capacity_minutes",
    "covered_minutes",
    "uncovered_minutes",
    "usage_minutes",
    "maintenance_minutes",
    "commitment_minutes",
    "deliverable_minutes",
)


def _minutes(spans: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in union(spans))


def snapshot_summary(
    topology_rows: Sequence[Mapping[str, Any]],
    coverage_rows: Sequence[Mapping[str, Any]],
    occupancy_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """从一份推导结果算出每张卡的分量、合作方与中心承诺分钟。"""
    segments: dict[str, list[tuple[int, int]]] = defaultdict(list)
    centers: dict[str, set[str]] = defaultdict(set)
    fabrics: dict[str, set[str]] = defaultdict(set)
    for row in topology_rows:
        segments[row["card"]].append((row["span_start"], row["span_end"]))
        centers[row["card"]].add(row["center"])
        fabrics[row["card"]].add(row["fabric"])

    coverage: dict[str, list[tuple[int, int]]] = defaultdict(list)
    scoped: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for row in coverage_rows:
        scope = row.get("scope") or "card"
        if scope == "card":
            coverage[row["card"]].append((row["span_start"], row["span_end"]))
        else:
            scoped[(row["card"], scope)].append((row["span_start"], row["span_end"]))

    usage: dict[str, list[tuple[int, int]]] = defaultdict(list)
    holds: dict[str, list[tuple[int, int]]] = defaultdict(list)
    commits: dict[str, list[tuple[int, int]]] = defaultdict(list)
    uncovered: dict[str, list[tuple[int, int]]] = defaultdict(list)
    usage_sources: dict[str, set[str]] = defaultdict(set)
    hold_sources: dict[str, set[str]] = defaultdict(set)
    commit_sources: dict[str, set[str]] = defaultdict(set)
    party_minutes: dict[str, list[tuple[int, int]]] = defaultdict(list)
    center_minutes: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for row in occupancy_rows:
        card = row["card"]
        span = (row["span_start"], row["span_end"])
        kind = row["fact_kind"]
        if kind == "usage":
            usage[card].append(span)
            usage_sources[card].add(f"{row['source_id']}@{row['intake']}")
        elif kind == "maintenance":
            holds[card].append(span)
            hold_sources[card].add(f"{row['source_id']}@{row['intake']}")
        elif kind == "commitment":
            commits[card].append(span)
            commit_sources[card].add(f"{row['source_id']}@{row['intake']}")
            if row.get("party"):
                party_minutes[row["party"]].append(span)
            center_minutes[row["center"]].append(span)
        elif kind == "uncovered":
            uncovered[card].append(span)

    cards: dict[str, dict[str, Any]] = {}
    for card in sorted(segments):
        own = segments[card]
        card_spans = list(coverage.get(card, []))
        if not card_spans:
            # 只有成员级覆盖时，用成员覆盖的并集保守估计整卡可见范围。
            for (scoped_card, _scope), spans in scoped.items():
                if scoped_card == card:
                    card_spans.extend(spans)
        covered = intersect(own, card_spans)
        # 没有覆盖就算不可交付；覆盖内的作业/封锁/承诺才扣减。
        usage_in = intersect(covered, usage.get(card, []))
        holds_in = intersect(covered, holds.get(card, []))
        commits_in = intersect(covered, commits.get(card, []))
        taken = union(usage_in, holds_in, commits_in)
        deliverable = subtract(covered, taken)
        uncovered_spans = subtract(own, card_spans)
        cards[card] = {
            "capacity_minutes": _minutes(own),
            "covered_minutes": _minutes(covered),
            "uncovered_minutes": _minutes(uncovered_spans),
            "usage_minutes": _minutes(usage_in),
            "maintenance_minutes": _minutes(holds_in),
            "commitment_minutes": _minutes(commits_in),
            "deliverable_minutes": _minutes(deliverable),
            "centers": sorted(centers[card]),
            "fabrics": sorted(fabrics[card]),
            "usage_sources": sorted(usage_sources[card]),
            "hold_sources": sorted(hold_sources[card]),
            "commitment_sources": sorted(commit_sources[card]),
        }

    return {
        "cards": cards,
        "parties": {
            party: {"commitment_minutes": _minutes(spans)}
            for party, spans in sorted(party_minutes.items())
        },
        "centers": {
            center: {"commitment_minutes": _minutes(spans)}
            for center, spans in sorted(center_minutes.items())
        },
    }


def diff_summaries(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """按卡/合作方/中心拆解两份快照的差值，并标注原因类别。"""
    rows: list[dict[str, Any]] = []
    before_cards = before["cards"]
    after_cards = after["cards"]
    for card in sorted(set(before_cards) | set(after_cards)):
        old = before_cards.get(card)
        new = after_cards.get(card)
        if old is None or new is None:
            rows.append(
                {
                    "dimension": "card",
                    "key": card,
                    "before_json": _dump(old),
                    "after_json": _dump(new),
                    "delta_json": _dump(
                        {
                            component: (new or _zero())[component] - (old or _zero())[component]
                            for component in COMPONENTS
                        }
                    ),
                    "causes_json": _dump(["topology"]),
                    "cause": "topology",
                    "note": "卡片出现或消失：配置/拓扑到达",
                }
            )
            continue
        delta = {component: new[component] - old[component] for component in COMPONENTS}
        relocated = old["centers"] != new["centers"] or old["fabrics"] != new["fabrics"]
        if not any(delta.values()) and not relocated:
            continue
        causes = _causes(old, new, delta)
        rows.append(
            {
                "dimension": "card",
                "key": card,
                "before_json": _dump(old),
                "after_json": _dump(new),
                "delta_json": _dump(delta),
                "causes_json": _dump(causes),
                "cause": causes[0] if causes else "none",
                "note": _explain(causes, old, new),
            }
        )
    for dimension, key in (("party", "parties"), ("center", "centers")):
        old_map = before[key]
        new_map = after[key]
        for name in sorted(set(old_map) | set(new_map)):
            old_minutes = old_map.get(name, {}).get("commitment_minutes", 0)
            new_minutes = new_map.get(name, {}).get("commitment_minutes", 0)
            if old_minutes == new_minutes:
                continue
            rows.append(
                {
                    "dimension": dimension,
                    "key": name,
                    "before_json": _dump(old_map.get(name)),
                    "after_json": _dump(new_map.get(name)),
                    "delta_json": _dump({"commitment_minutes": new_minutes - old_minutes}),
                    "causes_json": _dump(["commitment"]),
                    "cause": "commitment",
                    "note": "有效承诺分钟变化：确认、交接或让出侧重复侧处理",
                }
            )
    return rows


def _zero() -> dict[str, int]:
    return {component: 0 for component in COMPONENTS}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _causes(old: Mapping[str, Any], new: Mapping[str, Any], delta: Mapping[str, int]) -> list[str]:
    causes: list[str] = []
    if (
        delta["capacity_minutes"]
        or delta["covered_minutes"]
        or delta["uncovered_minutes"]
        or old["centers"] != new["centers"]
        or old["fabrics"] != new["fabrics"]
    ):
        causes.append("coverage")
    if delta["usage_minutes"]:
        causes.append("physical")
    if delta["maintenance_minutes"]:
        causes.append("maintenance")
    if delta["commitment_minutes"]:
        causes.append("commitment")
    if delta["deliverable_minutes"] and not causes:
        causes.append("derived")
    return causes


def _explain(causes: Sequence[str], old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    if not causes:
        return "分量相互抵消，可交付范围未变"
    parts: list[str] = []
    if "coverage" in causes:
        if old["centers"] != new["centers"] or old["fabrics"] != new["fabrics"]:
            parts.append("拓扑位置迁移")
        else:
            parts.append("采集覆盖或配置范围变化")
    if "physical" in causes:
        new_sources = sorted(set(new["usage_sources"]) - set(old["usage_sources"]))
        parts.append(f"作业到达 {', '.join(new_sources)}" if new_sources else "作业占用变化")
    if "maintenance" in causes:
        new_sources = sorted(set(new["hold_sources"]) - set(old["hold_sources"]))
        parts.append(f"维护封锁 {', '.join(new_sources)}" if new_sources else "维护封锁变化")
    if "commitment" in causes:
        new_sources = sorted(set(new["commitment_sources"]) - set(old["commitment_sources"]))
        parts.append(f"承诺变化 {', '.join(new_sources)}" if new_sources else "承诺变化")
    return "；".join(parts)
