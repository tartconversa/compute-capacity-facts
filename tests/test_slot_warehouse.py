"""跨中心可交付时段仓的端到端验证。

覆盖题目要求的三类可重复场景：

* 分片变更与跨中心接替相遇（:class:`SplitHandoffTests`）；
* 重提请求（:class:`ResubmissionTests`）；
* 两份请求竞争同一空闲组合（:class:`CompetitionTests`）。

其余测试把窗口、履约、缺口、补安排和通知复算逐项钉在样例资料的已知值上。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from slotwarehouse.derive import derive
from slotwarehouse.services import (
    EntryError,
    confirm_request,
    find_window,
    fulfillment_report,
    open_handoff,
    reproduce_notice,
    reserve_window,
    submit_request,
)
from slotwarehouse.sources import SourceError, load_deliveries
from slotwarehouse.store import Warehouse

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"


def _copy_sources() -> Path:
    target = Path(tempfile.mkdtemp(prefix="slots-"))
    for path in SOURCES.glob("*.json"):
        shutil.copy(path, target / path.name)
    shutil.copy(ROOT / "source-fields.json", target / "source-fields.json")
    return target


def _add_delivery(directory: Path, intake: int, table: str, records: list[dict]) -> Path:
    path = directory / f"{intake:02d}-{table}.json"
    path.write_text(
        json.dumps({"intake": intake, "table": table, "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class WarehouseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.warehouse = Warehouse(":memory:")
        self.warehouse.ingest(SOURCES)
        self.addCleanup(self.warehouse.close)


# ---------------------------------------------------------------------------


class SampleDerivationTests(WarehouseCase):
    def test_topology_segments_keep_config_windows(self):
        rows = self.warehouse.query(
            "SELECT config_id, card, span_start, span_end, mode FROM derived_topology_segment "
            "WHERE card = 'E01' ORDER BY span_start"
        )
        self.assertEqual(
            rows,
            [
                {"config_id": "e1", "card": "E01", "span_start": 0, "span_end": 60, "mode": "split"},
                {"config_id": "e2", "card": "E01", "span_start": 60, "span_end": 240, "mode": "whole"},
            ],
        )

    def test_split_usage_respects_config_window(self):
        # E01p 的用量只出现在 E01 的分片配置段 [0,60) 内。
        rows = self.warehouse.query(
            "SELECT resource, span_start, span_end FROM derived_usage_piece "
            "WHERE resource = 'E01p' ORDER BY span_start"
        )
        self.assertEqual(rows, [{"resource": "E01p", "span_start": 10, "span_end": 39}])

    def test_corrected_usage_supersedes_prior_receipt(self):
        rows = self.warehouse.query(
            "SELECT usage_id, status, replaces FROM derived_usage_fact ORDER BY usage_id"
        )
        by_id = {row["usage_id"]: row for row in rows}
        self.assertEqual(by_id["a3"]["status"], "superseded")
        self.assertEqual(by_id["moved-end"]["status"], "effective")
        self.assertEqual(by_id["moved-end"]["replaces"], "a3")
        # 更正后 E01 的整卡占用延到 124。
        span = self.warehouse.one(
            "SELECT span_start, span_end FROM derived_usage_piece "
            "WHERE card = 'E01' AND whole_card = 1 AND party = 'alpha'"
        )
        self.assertEqual((span["span_start"], span["span_end"]), (60, 124))

    def test_whole_card_suppresses_member_shares(self):
        # 整卡作业与分片作业若有重叠，子实例用量应记为不计入。
        rows = self.warehouse.query(
            "SELECT COUNT(*) AS n FROM derived_occupancy_fact WHERE fact_kind = 'usage_suppressed'"
        )
        self.assertEqual(rows[0]["n"], 0)
        # 而同一时段两类用量不同时不叠加：E01 在 [60,124) 只有一份整卡容量。
        facts = self.warehouse.query(
            "SELECT fact_kind, resource, share_units FROM derived_occupancy_fact "
            "WHERE card = 'E01' AND span_start >= 60 AND span_end <= 124 AND covered = 1"
        )
        kinds = [row["fact_kind"] for row in facts]
        self.assertIn("usage", kinds)
        self.assertNotIn("usage_suppressed", kinds)

    def test_maintenance_and_drain_both_block_new_commitments(self):
        rows = self.warehouse.query(
            "SELECT card, kind, span_start, span_end FROM derived_hold_piece ORDER BY card"
        )
        kinds = {(row["card"], row["kind"]) for row in rows}
        self.assertIn(("E02", "excluded"), kinds)
        self.assertIn(("W01", "drain"), kinds)
        maintenance = self.warehouse.query(
            "SELECT DISTINCT card FROM derived_occupancy_fact WHERE fact_kind = 'maintenance'"
        )
        self.assertEqual({row["card"] for row in maintenance}, {"E02", "W01"})
        # drain 期间已有作业继续跑完：W01 的实际作业区间仍算物理占用。
        usage = self.warehouse.one(
            "SELECT span_start, span_end FROM derived_usage_piece WHERE card = 'W01'"
        )
        self.assertEqual((usage["span_start"], usage["span_end"]), (96, 140))

    def test_handoff_splits_commitment_by_confirmed_min(self):
        rows = self.warehouse.query(
            "SELECT resource, side, effective_start, effective_end, dual_side "
            "FROM derived_commitment_piece WHERE reservation = 'r-move' ORDER BY effective_start, resource"
        )
        self.assertEqual(
            [(r["resource"], r["side"], r["effective_start"], r["effective_end"], r["dual_side"]) for r in rows],
            [
                ("E01", "from", 60, 96, 0),
                ("E02", "from", 60, 96, 0),
                ("W01", "to", 96, 150, 0),
                ("W02", "to", 96, 150, 0),
            ],
        )

    def test_dual_physical_occupancy_counted_once(self):
        handoff = self.warehouse.one("SELECT * FROM derived_handoff_view WHERE reservation = 'r-move'")
        self.assertEqual(handoff["counted_once"], 1)
        self.assertEqual(handoff["dual_physical_start"], 96)
        # 让出侧实际作业跑到 124，物理卡真正释放。
        self.assertEqual(handoff["from_release_min"], 124)
        # 承诺分钟按侧计一次：east 2×36 + west 2×54。
        fulfillment = self.warehouse.one(
            "SELECT commitment_minutes FROM derived_fulfillment WHERE reservation = 'r-move'"
        )
        self.assertEqual(fulfillment["commitment_minutes"], 180)

    def test_confirmed_gap_after_maintenance_never_rewrites_commitment(self):
        fulfillment = self.warehouse.one(
            "SELECT * FROM derived_fulfillment WHERE reservation = 'r-move'"
        )
        self.assertEqual(fulfillment["evidence_state"], "confirmed_gap")
        self.assertEqual(fulfillment["gap_minutes"], 21)
        gaps = self.warehouse.query(
            "SELECT * FROM derived_commitment_gap WHERE reservation = 'r-move' ORDER BY span_start"
        )
        self.assertEqual(
            [(gap["span_start"], gap["span_end"], gap["cause"]) for gap in gaps],
            [(85, 96, "maintenance"), (140, 150, "maintenance")],
        )
        # 确认依据保留为原 placement，不被新资料改成其他资源。
        placements = self.warehouse.query(
            "SELECT id, resources_json FROM derived_placement WHERE reservation = 'r-move' ORDER BY id"
        )
        self.assertEqual([p["id"] for p in placements], ["pl-earlier", "pl-handoff"])

    def test_replenishment_candidates_cover_full_gap(self):
        gaps = self.warehouse.query(
            "SELECT * FROM derived_commitment_gap WHERE reservation = 'r-move' AND gap_kind = 'confirmed'"
        )
        options = self.warehouse.query("SELECT * FROM derived_replenishment_option ORDER BY id")
        self.assertGreater(len(options), 0)
        for option in options:
            self.assertEqual(option["requires_approval"], 1)
            resources = json.loads(option["resources_json"])
            self.assertTrue(resources)
            # 候选必须与缺口一样长，不能只填一半。
            self.assertEqual(option["span_end"] - option["span_start"], option["span_end"] - option["span_start"])
            covering = [
                gap for gap in gaps
                if gap["reservation"] == option["reservation"]
                and gap["span_start"] == option["span_start"]
                and gap["span_end"] == option["span_end"]
            ]
            self.assertEqual(len(covering), 1)


# ---------------------------------------------------------------------------


class WindowLookupTests(WarehouseCase):
    def test_deliverable_window_uses_single_fabric(self):
        finding = find_window(
            self.warehouse, center="edge-west", span=(160, 200), resource_type="fit7", count=2
        )
        self.assertEqual(finding["verdict"], "deliverable")
        self.assertEqual(len(finding["groups"]), 1)
        group = finding["groups"][0]
        self.assertEqual(group["fabric"], "ring-w")
        self.assertEqual(group["resources"], ["W01", "W02"])
        # W03 在别的通信域，不能和 W01/W02 拼成组合。
        fabrics = {candidate["resource"]: candidate["fabric"] for candidate in finding["candidates"]}
        self.assertEqual(fabrics["W03"], "ring-v")

    def test_partial_window_without_full_group_is_not_promised(self):
        finding = find_window(
            self.warehouse, center="edge-east", span=(20, 80), resource_type="fit7", count=2
        )
        self.assertEqual(finding["verdict"], "partial")
        self.assertEqual(finding["groups"], [])
        self.assertEqual(finding["shortfall"]["best_group_size"], 0)
        # 零碎空闲与整卡占用都体现在候选明细里。
        causes = {
            candidate["resource"]: candidate["causes_minutes"]
            for candidate in finding["candidates"]
        }
        self.assertTrue(any("usage_whole_card" in value for value in causes.values()))

    def test_coverage_gaps_listed_separately_and_not_used(self):
        directory = _copy_sources()
        # 撤回 W02 的补充覆盖，留下 [96, 240) 的采集空档。
        (directory / "02-coverage.json").unlink()
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        gaps = warehouse.query("SELECT * FROM derived_coverage_gap ORDER BY span_start")
        self.assertEqual(
            [(gap["card"], gap["span_start"], gap["span_end"], gap["reason"]) for gap in gaps],
            [("W02", 96, 240, "no_coverage")],
        )
        finding = find_window(
            warehouse, center="edge-west", span=(160, 200), resource_type="fit7", count=2
        )
        self.assertEqual(finding["verdict"], "partial")
        self.assertEqual(finding["coverage_gap_minutes"], 40)
        self.assertFalse(any("W02" in group["resources"] for group in finding["groups"]))

    def test_uncovered_commitment_is_pending_not_confirmed(self):
        directory = _copy_sources()
        (directory / "02-coverage.json").unlink()
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        row = warehouse.one("SELECT * FROM derived_fulfillment WHERE reservation = 'r-move'")
        self.assertIn(row["evidence_state"], {"pending_gap", "confirmed_gap"})
        pending = warehouse.query(
            "SELECT * FROM derived_commitment_gap WHERE reservation = 'r-move' AND gap_kind = 'pending'"
        )
        self.assertTrue(pending)
        self.assertTrue(all(gap["cause"] == "coverage_missing" for gap in pending))
        confirmed = warehouse.query(
            "SELECT * FROM derived_commitment_gap WHERE reservation = 'r-move' AND gap_kind = 'confirmed'"
        )
        # W01 的 drain 缺口仍是已确认的；缺少覆盖的 W02 部分只能算尚待确认。
        self.assertTrue(confirmed)
        self.assertTrue(
            all(
                gap["cause"] != "coverage_missing"
                for gap in confirmed
            )
        )
        self.assertTrue(
            all(gap["cause"] == "coverage_missing" for gap in pending)
        )

    def test_maintenance_keeps_healthy_job_running(self):
        # drain 期间 alpha 自己的作业继续跑，直到真正结束才释放。
        availability = self.warehouse.query(
            "SELECT status, reasons_json FROM derived_availability_piece "
            "WHERE card = 'W01' AND span_start = 115 AND span_end = 140"
        )
        self.assertEqual(len(availability), 1)
        self.assertEqual(availability[0]["status"], "blocked")
        reasons = json.loads(availability[0]["reasons_json"])
        self.assertIn("hold", {reason["code"] for reason in reasons})
        row = self.warehouse.one("SELECT * FROM derived_fulfillment WHERE reservation = 'r-move'")
        # 作业结束到 140，drain 仍延伸到 150：只有作业结束后的 [140,150) 是缺口。
        self.assertEqual(row["gap_minutes"], 21)


# ---------------------------------------------------------------------------


class CorrectionAndScopingTests(WarehouseCase):
    def test_forward_reference_does_not_supersede_later_receipt(self):
        directory = _copy_sources()
        _add_delivery(
            directory,
            4,
            "usage",
            [
                {
                    "id": "early",
                    "resource": "W03",
                    "span": [0, 10],
                    "party": "alpha",
                    "job": "x",
                    "replaces": "future",
                }
            ],
        )
        _add_delivery(
            directory,
            5,
            "usage",
            [
                {
                    "id": "future",
                    "resource": "W03",
                    "span": [0, 10],
                    "party": "alpha",
                    "job": "x",
                    "replaces": None,
                }
            ],
        )
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        rows = {
            row["usage_id"]: row["status"]
            for row in warehouse.query("SELECT usage_id, status FROM derived_usage_fact")
        }
        # 早到的引用不能把后到的凭证作废。
        self.assertEqual(rows["early"], "effective")
        self.assertEqual(rows["future"], "effective")

    def test_member_scoped_hold_does_not_block_whole_card_commitment(self):
        directory = _copy_sources()
        _add_delivery(
            directory,
            4,
            "holds",
            [{"id": "ban-member", "resource": "W04a", "span": [0, 60], "kind": "excluded"}],
        )
        _add_delivery(
            directory,
            5,
            "topology",
            [
                # 后续把 W04 切成两片，使其成为分片配置段，兄弟成员不受封锁影响。
                {
                    "id": "w4-split",
                    "center": "edge-west",
                    "fabric": "ring-w",
                    "card": "W04",
                    "span": [60, 120],
                    "members": ["W04a:fit3", "W04b:fit3"],
                }
            ],
        )
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        rows = warehouse.query(
            "SELECT resource, status, reasons_json FROM derived_availability_piece "
            "WHERE card = 'W04' AND span_start = 0 AND span_end = 60"
        )
        reasons = {row["resource"]: json.loads(row["reasons_json"]) for row in rows}
        self.assertTrue(any(reason["code"] == "hold" for reason in reasons["W04a"]))
        self.assertFalse(any(reason["code"] == "hold" for reason in reasons["W04b"]))

    def test_api_handoff_lands_on_destination_side(self):
        request = submit_request(
            self.warehouse,
            party="iota",
            location="edge-east",
            span=(150, 210),
            resource_type="fit7",
            count=1,
        )
        confirm_request(
            self.warehouse, request["reservation"], center="edge-east", resources=["E03"]
        )
        open_handoff(
            self.warehouse, request["reservation"], to_center="edge-west", confirmed_min=180
        )
        confirm_request(
            self.warehouse, request["reservation"], center="edge-west", resources=["W03"]
        )
        pieces = self.warehouse.query(
            "SELECT resource, side, effective_start, effective_end FROM derived_commitment_piece "
            "WHERE reservation = ? ORDER BY effective_start",
            (request["reservation"],),
        )
        self.assertEqual(
            [(piece["resource"], piece["side"], piece["effective_start"], piece["effective_end"])
             for piece in pieces],
            [("E03", "from", 150, 180), ("W03", "to", 180, 210)],
        )
        report = fulfillment_report(
            self.warehouse, reservation=request["reservation"]
        )["rows"][0]
        self.assertEqual(report["evidence_state"], "fulfilled")
        self.assertEqual(report["commitment_minutes"], 60)


class MemberCoverageTests(WarehouseCase):
    """成员级 coverage 不能冒充整卡覆盖。"""

    def test_member_only_coverage_does_not_cover_whole_card(self):
        directory = _copy_sources()
        (directory / "01-coverage.json").unlink()
        _add_delivery(
            directory,
            1,
            "coverage",
            [
                {"id": "cov-e1p", "resource": "E01p", "span": [0, 60]},
                {"id": "cov-e2", "resource": "E02", "span": [0, 240]},
                {"id": "cov-e3", "resource": "E03", "span": [0, 240]},
                {"id": "cov-w1", "resource": "W01", "span": [0, 240]},
                {"id": "cov-w2", "resource": "W02", "span": [0, 240]},
                {"id": "cov-w3", "resource": "W03", "span": [0, 240]},
                {"id": "cov-w4", "resource": "W04", "span": [0, 240]},
            ],
        )
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        gaps = warehouse.query(
            "SELECT scope, span_start, span_end FROM derived_coverage_gap "
            "WHERE card = 'E01' ORDER BY span_start, scope"
        )
        # E01q 的 [0,60) 没有覆盖；整卡 [60,240) 也没有覆盖。
        self.assertIn({"scope": "E01q", "span_start": 0, "span_end": 60}, gaps)
        self.assertIn({"scope": "E01", "span_start": 60, "span_end": 240}, gaps)
        # E01p 有成员级覆盖；E01q 没有。
        p = warehouse.query(
            "SELECT status FROM derived_availability_piece "
            "WHERE card = 'E01' AND resource = 'E01p' AND span_start = 0 AND span_end = 10"
        )
        self.assertEqual(p[0]["status"], "free")
        q = warehouse.query(
            "SELECT status FROM derived_availability_piece "
            "WHERE card = 'E01' AND resource = 'E01q' AND span_start = 0 AND span_end = 10"
        )
        self.assertEqual(q[0]["status"], "uncovered")

    def test_member_coverage_shows_source_scope(self):
        rows = self.warehouse.query("SELECT DISTINCT scope FROM derived_coverage_piece ORDER BY scope")
        self.assertEqual({row["scope"] for row in rows}, {"card"})


class SplitHandoffTests(WarehouseCase):
    """分片配置变更与跨中心接替在时间上相遇时的行为。"""

    def test_split_config_change_does_not_leak_member_availability(self):
        # E01 在 [60,240) 是整卡配置；E01p/E01q 不应在这段时间被当成实例。
        rows = self.warehouse.query(
            "SELECT DISTINCT resource FROM derived_availability_piece "
            "WHERE card = 'E01' AND span_start >= 60"
        )
        self.assertEqual({row["resource"] for row in rows}, {"E01"})

    def test_split_to_whole_and_back(self):
        directory = _copy_sources()
        # 追加一次重新分片：E01 从 124 起切成两片。
        _add_delivery(
            directory,
            4,
            "topology",
            [
                {
                    "id": "e1-late",
                    "center": "edge-east",
                    "fabric": "ring-x",
                    "card": "E01",
                    "span": [124, 240],
                    "members": ["E01r:fit3", "E01s:fit3"],
                }
            ],
        )
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        rows = warehouse.query(
            "SELECT resource, span_start, span_end FROM derived_availability_piece "
            "WHERE card = 'E01' AND span_start >= 60 ORDER BY span_start, resource"
        )
        resources_after = {row["resource"] for row in rows if row["span_start"] >= 124}
        self.assertEqual(resources_after, {"E01r", "E01s"})
        # 配置变更只影响生效时段之后，不改写之前的整卡占用与承诺。
        commitment = warehouse.query(
            "SELECT resource, effective_start, effective_end FROM derived_commitment_piece "
            "WHERE reservation = 'r-move' AND side = 'from' ORDER BY resource"
        )
        self.assertEqual(
            [(row["resource"], row["effective_start"], row["effective_end"]) for row in commitment],
            [("E01", 60, 96), ("E02", 60, 96)],
        )

    def test_member_usage_after_reconfiguration_is_flagged(self):
        directory = _copy_sources()
        _add_delivery(
            directory,
            4,
            "topology",
            [
                {
                    "id": "e1-whole-again",
                    "center": "edge-east",
                    "fabric": "ring-x",
                    "card": "E01",
                    "span": [124, 240],
                    "members": [],
                }
            ],
        )
        _add_delivery(
            directory,
            5,
            "usage",
            [
                {
                    "id": "stale-member",
                    "resource": "E01p",
                    "span": [130, 140],
                    "party": "alpha",
                    "job": "late",
                    "replaces": None,
                }
            ],
        )
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        row = warehouse.one(
            "SELECT severity, whole_card FROM derived_usage_piece WHERE usage_id = 'stale-member'"
        )
        self.assertEqual(row["severity"], "member_without_split_config")
        # 旧分片号在新配置里不能算子实例，只能按整卡占用计入。
        self.assertEqual(row["whole_card"], 1)

    def test_handoff_confirmed_before_new_split(self):
        directory = _copy_sources()
        _add_delivery(
            directory,
            4,
            "topology",
            [
                {
                    "id": "e1-migrated",
                    "center": "edge-east",
                    "fabric": "ring-s",
                    "card": "E01",
                    "span": [130, 240],
                    "members": [],
                }
            ],
        )
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(SOURCES)
        warehouse.ingest(directory)
        # 接替后接手侧承诺仍从 confirmed_min 起，不因让出侧重新分片而推迟。
        rows = warehouse.query(
            "SELECT resource, side, effective_start FROM derived_commitment_piece "
            "WHERE reservation = 'r-move' ORDER BY resource"
        )
        self.assertEqual(
            [(row["resource"], row["side"], row["effective_start"]) for row in rows],
            [("E01", "from", 60), ("E02", "from", 60), ("W01", "to", 96), ("W02", "to", 96)],
        )
        handoff = warehouse.one("SELECT * FROM derived_handoff_view WHERE reservation = 'r-move'")
        self.assertEqual(handoff["from_release_min"], 124)
        # 让出侧新拓扑不改变已签发承诺，但会进入对账说明。
        recon = warehouse.query(
            "SELECT * FROM derived_reconciliation WHERE dimension = 'card' AND key = 'E01'"
        )
        # 配置段 [130,240) 迁移通信域：已确认承诺不变，但对账要说明拓扑变化。
        self.assertTrue(recon)
        self.assertIn("coverage", json.loads(recon[0]["causes_json"]))

    def test_handoff_without_to_side_evidence_is_pending(self):
        directory = _copy_sources()
        (directory / "02-placements.json").unlink()
        warehouse = Warehouse(":memory:", source_dir=directory)
        self.addCleanup(warehouse.close)
        warehouse.ingest(directory)
        row = warehouse.one("SELECT * FROM derived_fulfillment WHERE reservation = 'r-move'")
        self.assertTrue(row["evidence_state"].startswith("pending"))
        pending = warehouse.query(
            "SELECT * FROM derived_commitment_gap WHERE reservation = 'r-move' AND gap_kind = 'pending'"
        )
        self.assertTrue(any(gap["cause"] == "missing_evidence_to_side" for gap in pending))
        handoff = warehouse.one("SELECT * FROM derived_handoff_view WHERE reservation = 'r-move'")
        self.assertEqual(handoff["to_evidence_state"], "pending_missing_evidence")
        self.assertEqual(handoff["from_evidence_state"], "evidenced")

    def test_dual_physical_occupancy_is_single_commitment(self):
        # 让出侧 [96,124) 与接手侧同时存在物理占用，但承诺分钟只算一次。
        commitments = self.warehouse.query(
            "SELECT * FROM derived_commitment_piece WHERE reservation = 'r-move'"
        )
        from_minutes = sum(
            row["effective_end"] - row["effective_start"]
            for row in commitments
            if row["side"] == "from" and not row["dual_side"]
        )
        to_minutes = sum(
            row["effective_end"] - row["effective_start"]
            for row in commitments
            if row["side"] == "to" and not row["dual_side"]
        )
        self.assertEqual(from_minutes, 72)
        self.assertEqual(to_minutes, 108)


# ---------------------------------------------------------------------------


class ResubmissionTests(WarehouseCase):
    def test_identical_resubmission_reuses_request(self):
        first = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        second = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["reservation"], first["reservation"])
        count = self.warehouse.one("SELECT COUNT(*) AS n FROM api_reservation")["n"]
        self.assertEqual(count, 1)

    def test_resubmission_after_confirmation_stays_one_commitment(self):
        first = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        confirm_request(
            self.warehouse, first["reservation"], center="edge-west", resources=["W01", "W02"]
        )
        again = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        self.assertFalse(again["created"])
        placements = self.warehouse.query(
            "SELECT * FROM derived_placement WHERE reservation = ?", (first["reservation"],)
        )
        self.assertEqual(len(placements), 1)
        commitment = self.warehouse.query(
            "SELECT * FROM derived_commitment_piece WHERE reservation = ?", (first["reservation"],)
        )
        self.assertEqual(len(commitment), 2)

    def test_resubmission_during_handoff_explained_by_one_reservation(self):
        request = submit_request(
            self.warehouse,
            party="eta",
            location="edge-east",
            span=(160, 200),
            resource_type="fit7",
            count=1,
        )
        confirm_request(
            self.warehouse, request["reservation"], center="edge-east", resources=["E03"]
        )
        result = open_handoff(
            self.warehouse, request["reservation"], to_center="edge-west", confirmed_min=180
        )
        self.assertEqual(result["state"], "handoff")
        again = submit_request(
            self.warehouse,
            party="eta",
            location="edge-east",
            span=(160, 200),
            resource_type="fit7",
            count=1,
        )
        # 交接前的重提仍落在同一笔预约上，不会生成第二份承诺。
        self.assertEqual(again["reservation"], request["reservation"])
        self.assertFalse(again["created"])
        handoffs = self.warehouse.query(
            "SELECT * FROM derived_handoff WHERE reservation = ?", (request["reservation"],)
        )
        self.assertEqual(len(handoffs), 1)
        commitments = self.warehouse.query(
            "SELECT * FROM derived_commitment_piece WHERE reservation = ? AND dual_side = 0",
            (request["reservation"],),
        )
        self.assertEqual(len(commitments), 1)

    def test_different_request_is_not_merged(self):
        first = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        second = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=1,
        )
        self.assertNotEqual(first["reservation"], second["reservation"])
        self.assertTrue(second["created"])


# ---------------------------------------------------------------------------


class CompetitionTests(WarehouseCase):
    def test_two_requests_cannot_confirm_same_combination(self):
        beta = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        gamma = submit_request(
            self.warehouse,
            party="gamma",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        confirmed = confirm_request(
            self.warehouse, beta["reservation"], center="edge-west", resources=["W01", "W02"]
        )
        self.assertEqual(confirmed["resources"], ["W01", "W02"])
        with self.assertRaises(EntryError) as caught:
            confirm_request(
                self.warehouse, gamma["reservation"], center="edge-west", resources=["W01", "W02"]
            )
        self.assertIn(beta["reservation"], str(caught.exception))
        # 竞争视图把两笔请求标为共用同一组合。
        # 竞争记录在确认前就能看到；确认后未获确认的一方仍留在视图里。
        rows = self.warehouse.query(
            "SELECT * FROM derived_competition WHERE left_reservation = 'r-b' AND right_reservation = 'r-c'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "contested")

    def test_competition_can_be_resolved_by_different_fabric(self):
        beta = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        gamma = submit_request(
            self.warehouse,
            party="gamma",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=1,
        )
        confirm_request(
            self.warehouse, beta["reservation"], center="edge-west", resources=["W01", "W02"]
        )
        # gamma 要 1 个整卡，可改用 ring-v 的 W03，不共享同一组合。
        confirmed = confirm_request(
            self.warehouse, gamma["reservation"], center="edge-west", resources=["W03"]
        )
        self.assertEqual(confirmed["resources"], ["W03"])
        report = fulfillment_report(
            self.warehouse, party="gamma", reservation=gamma["reservation"]
        )
        self.assertEqual(report["rows"][0]["evidence_state"], "fulfilled")

    def test_source_competition_is_visible(self):
        rows = self.warehouse.query(
            "SELECT * FROM derived_competition WHERE left_reservation = 'r-b' AND right_reservation = 'r-c'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "contested")
        pool = json.loads(rows[0]["pool_json"])
        self.assertEqual(sorted(pool), ["edge-west/ring-v", "edge-west/ring-w"])


# ---------------------------------------------------------------------------


class DurabilityTests(WarehouseCase):
    def test_ingest_is_append_only_and_rejects_conflicting_batches(self):
        accepted = self.warehouse.ingest(SOURCES)
        self.assertEqual(accepted, [])  # 已经导入过
        directory = _copy_sources()
        _add_delivery(
            directory,
            4,
            "holds",
            [{"id": "ban-w01", "resource": "W01", "span": [160, 200], "kind": "excluded"}],
        )
        self.assertEqual(self.warehouse.ingest(directory), ["04-holds.json"])
        # 同序号不同内容必须换新到达号。
        _add_delivery(
            directory,
            4,
            "holds",
            [{"id": "ban-w01", "resource": "W01", "span": [161, 200], "kind": "excluded"}],
        )
        with self.assertRaises(ValueError):
            self.warehouse.ingest(directory)

    def test_bad_source_shape_is_rejected(self):
        directory = _copy_sources()
        path = directory / "04-holds.json"
        path.write_text(
            json.dumps({"intake": 4, "table": "holds", "records": [{"id": "x"}]}),
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_deliveries(directory)

    def test_new_data_narrows_deliverable_without_rewriting_confirmation(self):
        self.warehouse.ingest(SOURCES)
        beta = submit_request(
            self.warehouse,
            party="beta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        confirm_request(
            self.warehouse, beta["reservation"], center="edge-west", resources=["W01", "W02"]
        )
        before_hash = self.warehouse.current().snapshot_hash
        directory = _copy_sources()
        _add_delivery(
            directory,
            4,
            "holds",
            [{"id": "ban-w01", "resource": "W01", "span": [160, 200], "kind": "excluded"}],
        )
        self.warehouse.ingest(directory)
        after_hash = self.warehouse.current().snapshot_hash
        self.assertNotEqual(before_hash, after_hash)
        row = self.warehouse.one(
            "SELECT * FROM derived_fulfillment WHERE reservation = ?", (beta["reservation"],)
        )
        self.assertEqual(row["evidence_state"], "confirmed_gap")
        self.assertEqual(row["gap_minutes"], 40)
        # 确认依据不变：原 placement 还是 W01/W02。
        placement = self.warehouse.one(
            "SELECT * FROM derived_placement WHERE reservation = ? AND origin = 'api'",
            (beta["reservation"],),
        )
        self.assertEqual(json.loads(placement["resources_json"]), ["W01", "W02"])
        # 对账说明变化来自维护，而非承诺本身。
        recon = self.warehouse.one(
            "SELECT * FROM derived_reconciliation WHERE dimension = 'card' AND key = 'W01'"
        )
        self.assertIn("maintenance", json.loads(recon["causes_json"]))
        # 补安排候选不改写承诺，只标记需要批准。
        options = self.warehouse.query(
            "SELECT * FROM derived_replenishment_option WHERE reservation = ?", (beta["reservation"],)
        )
        self.assertTrue(options)
        self.assertTrue(all(option["requires_approval"] == 1 for option in options))

    def test_historical_notice_is_reproducible_after_new_intake(self):
        finding = find_window(
            self.warehouse, center="edge-west", span=(160, 200), resource_type="fit7", count=2
        )
        notice_id = finding["notice_id"]
        self.assertEqual(finding["verdict"], "deliverable")
        directory = _copy_sources()
        _add_delivery(
            directory,
            4,
            "holds",
            [{"id": "ban-w01", "resource": "W01", "span": [160, 200], "kind": "excluded"}],
        )
        self.warehouse.ingest(directory)
        current = find_window(
            self.warehouse, center="edge-west", span=(160, 200), resource_type="fit7", count=2
        )
        self.assertEqual(current["verdict"], "partial")
        replay = reproduce_notice(self.warehouse, notice_id)
        self.assertTrue(replay["matches_snapshot"])
        self.assertEqual(replay["recorded_result"]["verdict"], "deliverable")
        self.assertEqual(replay["semantics_version"], replay["current_semantics_version"])

    def test_reserve_window_round_trip(self):
        result = reserve_window(
            self.warehouse,
            party="delta",
            location="edge-west",
            span=(160, 200),
            resource_type="fit7",
            count=2,
        )
        self.assertTrue(result["confirmed"])
        report = fulfillment_report(self.warehouse, party="delta")
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["evidence_state"], "fulfilled")

    def test_reserve_window_refuses_impossible_window(self):
        result = reserve_window(
            self.warehouse,
            party="delta",
            location="edge-east",
            span=(20, 80),
            resource_type="fit7",
            count=2,
        )
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "no_deliverable_group")
        self.assertEqual(result["finding"]["verdict"], "partial")


# ---------------------------------------------------------------------------


class DirectDeriveTests(unittest.TestCase):
    """不经过数据库也能得到同一份推导，便于离线复算。"""

    def test_deterministic_snapshot(self):
        deliveries, _schema = load_deliveries(SOURCES)
        first = derive(deliveries)
        second = derive(deliveries)
        self.assertEqual(first.snapshot_hash, second.snapshot_hash)
        self.assertEqual(first.stats, second.stats)

    def test_intervals_are_half_open(self):
        deliveries, _schema = load_deliveries(SOURCES)
        derived = derive(deliveries)
        for piece in derived.availability_pieces:
            self.assertLess(piece["span_start"], piece["span_end"])
        for piece in derived.coverage_pieces:
            self.assertLess(piece["span_start"], piece["span_end"])


if __name__ == "__main__":
    unittest.main()
