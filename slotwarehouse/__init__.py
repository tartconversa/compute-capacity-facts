"""跨算力中心的可交付资源时段仓。

常用入口：

    from slotwarehouse import Warehouse
    from slotwarehouse.services import find_window, fulfillment_report

- :class:`Warehouse` —— 只追加导入、推导重建、SQLite 查询；
- :func:`find_window` —— 安排下一批窗口；
- :func:`fulfillment_report` —— 核对既有承诺。
"""

from .intervals import intersect, subtract, union
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

__all__ = [
    "EntryError",
    "Warehouse",
    "confirm_request",
    "find_window",
    "fulfillment_report",
    "intersect",
    "open_handoff",
    "reproduce_notice",
    "reserve_window",
    "submit_request",
    "subtract",
    "union",
]
