"""命令行入口：导入、查窗口、登记预留/交接、看履约、复算历史通知。

    python -m slotwarehouse ingest [--sources DIR]
    python -m slotwarehouse window --center C --start M --end M --type fit7 --count N
    python -m slotwarehouse request --party P --center C --start M --end M --type fit7 --count N
    python -m slotwarehouse confirm --reservation R --center C --resources W01,W02
    python -m slotwarehouse handoff --reservation R --to-center C --confirmed-min M
    python -m slotwarehouse reserve --party P --center C --start M --end M --type fit7 --count N
    python -m slotwarehouse fulfillment [--party P] [--center C] [--reservation R]
    python -m slotwarehouse reconcile
    python -m slotwarehouse notice --id N

所有子命令都输出 JSON，便于再接脚本。默认库为 ``slotwarehouse.sqlite3``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .services import (
    EntryError,
    confirm_request,
    find_window,
    fulfillment_report,
    open_handoff,
    reproduce_notice,
    reserve_window,
    submit_request,
)
from .store import Warehouse


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _open(args) -> Warehouse:
    return Warehouse(args.db, source_dir=args.sources)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slotwarehouse", description="跨中心可交付时段仓")
    parser.add_argument("--db", default="slotwarehouse.sqlite3", help="SQLite 文件路径")
    parser.add_argument("--sources", default=None, help="源资料目录（默认仓库内 sources/）")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="导入新到达并重建视图")
    ingest.add_argument("--sources", default=None, help="本次导入的源目录")

    window = sub.add_parser("window", help="查可承诺窗口")
    window.add_argument("--center", required=True)
    window.add_argument("--start", type=int, required=True)
    window.add_argument("--end", type=int, required=True)
    window.add_argument("--type", dest="resource_type", default="fit7")
    window.add_argument("--count", type=int, default=1)
    window.add_argument("--fabric", default=None)
    window.add_argument("--party", default=None)
    window.add_argument("--purpose", default=None)
    window.add_argument("--no-notice", action="store_true")

    report = sub.add_parser("fulfillment", help="核对既有承诺")
    report.add_argument("--party", default=None)
    report.add_argument("--center", default=None)
    report.add_argument("--reservation", default=None)
    report.add_argument("--skip-requested", action="store_true")

    request = sub.add_parser("request", help="登记待确认请求")
    request.add_argument("--party", required=True)
    request.add_argument("--center", required=True)
    request.add_argument("--start", type=int, required=True)
    request.add_argument("--end", type=int, required=True)
    request.add_argument("--type", dest="resource_type", default="fit7")
    request.add_argument("--count", type=int, default=1)
    request.add_argument("--purpose", default="qualification")

    confirm = sub.add_parser("confirm", help="为请求签发具体资源承诺")
    confirm.add_argument("--reservation", required=True)
    confirm.add_argument("--center", required=True)
    confirm.add_argument("--resources", required=True, help="逗号分隔的资源号")
    confirm.add_argument("--confirmation", default="api-confirmed")

    handoff = sub.add_parser("handoff", help="登记换中心交接")
    handoff.add_argument("--reservation", required=True)
    handoff.add_argument("--to-center", required=True)
    handoff.add_argument("--confirmed-min", type=int, required=True)
    handoff.add_argument("--allowed-to", default=None, help="逗号分隔；默认只允许接手中心")

    reserve = sub.add_parser("reserve", help="查窗口 + 登记 + 确认一步完成")
    reserve.add_argument("--party", required=True)
    reserve.add_argument("--center", required=True)
    reserve.add_argument("--start", type=int, required=True)
    reserve.add_argument("--end", type=int, required=True)
    reserve.add_argument("--type", dest="resource_type", default="fit7")
    reserve.add_argument("--count", type=int, default=1)
    reserve.add_argument("--purpose", default="qualification")
    reserve.add_argument("--fabric", default=None)

    sub.add_parser("reconcile", help="查看按物理卡/合作方/中心的对账变化")

    notice = sub.add_parser("notice", help="按签发水位复算历史窗口通知")
    notice.add_argument("--id", type=int, required=True)

    args = parser.parse_args(argv)
    try:
        with _open(args) as warehouse:
            if args.command == "ingest":
                source_dir = Path(args.sources) if args.sources else None
                accepted = warehouse.ingest(source_dir)
                _emit(
                    {
                        "accepted": accepted,
                        "snapshot_hash": warehouse.current().snapshot_hash,
                        "stats": warehouse.current().stats,
                    }
                )
            elif args.command == "window":
                _emit(
                    find_window(
                        warehouse,
                        center=args.center,
                        span=(args.start, args.end),
                        resource_type=args.resource_type,
                        count=args.count,
                        fabric=args.fabric,
                        party=args.party,
                        purpose=args.purpose,
                        record_notice=not args.no_notice,
                    )
                )
            elif args.command == "fulfillment":
                _emit(
                    fulfillment_report(
                        warehouse,
                        party=args.party,
                        center=args.center,
                        reservation=args.reservation,
                        include_requested=not args.skip_requested,
                    )
                )
            elif args.command == "request":
                _emit(
                    submit_request(
                        warehouse,
                        party=args.party,
                        location=args.center,
                        span=(args.start, args.end),
                        resource_type=args.resource_type,
                        count=args.count,
                        purpose=args.purpose,
                    )
                )
            elif args.command == "confirm":
                _emit(
                    confirm_request(
                        warehouse,
                        args.reservation,
                        center=args.center,
                        resources=[item for item in args.resources.split(",") if item],
                        confirmation=args.confirmation,
                    )
                )
            elif args.command == "handoff":
                allowed = (
                    [item for item in args.allowed_to.split(",") if item]
                    if args.allowed_to
                    else None
                )
                _emit(
                    open_handoff(
                        warehouse,
                        args.reservation,
                        to_center=args.to_center,
                        confirmed_min=args.confirmed_min,
                        allowed_to=allowed,
                    )
                )
            elif args.command == "reserve":
                _emit(
                    reserve_window(
                        warehouse,
                        party=args.party,
                        location=args.center,
                        span=(args.start, args.end),
                        resource_type=args.resource_type,
                        count=args.count,
                        purpose=args.purpose,
                        fabric=args.fabric,
                    )
                )
            elif args.command == "reconcile":
                _emit(warehouse.query("SELECT * FROM derived_reconciliation ORDER BY dimension, key"))
            elif args.command == "notice":
                _emit(reproduce_notice(warehouse, args.id))
            else:  # pragma: no cover - argparse 已拦截
                parser.error(f"未知子命令 {args.command}")
    except EntryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
