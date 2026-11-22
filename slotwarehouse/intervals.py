"""左闭右开分钟区间的集合运算。

区间一律用 ``(start, end)`` 的二元元组表示，且 ``start < end``。
所有函数接受任意可迭代的区间，输出按起点排序。

约定：
* :func:`union` 合并重叠或首尾相接的区间；
* :func:`subtract` 是集合差；
* :func:`to_pieces` 不合并，用于在结果里保留"同一格来自不同证据"的边界。
"""

from __future__ import annotations

from collections.abc import Iterable

Span = tuple[int, int]
_SpanInput = Iterable[tuple[int, int]]


def _normalise(spans: _SpanInput) -> list[Span]:
    cleaned = [(int(a), int(b)) for a, b in spans if int(a) < int(b)]
    cleaned.sort()
    return cleaned


def is_empty(spans: _SpanInput) -> bool:
    return not any(int(a) < int(b) for a, b in spans)


def union(*groups: _SpanInput) -> list[Span]:
    """合并任意多组区间。"""
    merged: list[Span] = []
    for a, b in _normalise(span for group in groups for span in group):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def intersect(left: _SpanInput, right: _SpanInput) -> list[Span]:
    """两组区间的交集。"""
    a_spans = _normalise(left)
    b_spans = _normalise(right)
    result: list[Span] = []
    i = j = 0
    while i < len(a_spans) and j < len(b_spans):
        a0, a1 = a_spans[i]
        b0, b1 = b_spans[j]
        start, end = max(a0, b0), min(a1, b1)
        if start < end:
            result.append((start, end))
        if a1 < b1:
            i += 1
        else:
            j += 1
    return result


def subtract(base: _SpanInput, remove: _SpanInput) -> list[Span]:
    """从 ``base`` 中挖去 ``remove``，保留剩余区段。"""
    remaining = _normalise(base)
    for r0, r1 in _normalise(remove):
        next_round: list[Span] = []
        for b0, b1 in remaining:
            if r1 <= b0 or r0 >= b1:
                next_round.append((b0, b1))
                continue
            if b0 < r0:
                next_round.append((b0, r0))
            if r1 < b1:
                next_round.append((r1, b1))
        remaining = next_round
    return remaining


def clip(span: tuple[int, int], bounds: _SpanInput) -> list[Span]:
    """把一个区间裁到 ``bounds`` 覆盖的范围里。"""
    return intersect([span], bounds)


def contains(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def overlaps(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])


def add(left: Span, right: Span) -> Span:
    return (left[0] + right[0], left[1] + right[1])


def to_pieces(spans: _SpanInput) -> list[Span]:
    """排序但不合并，保留原始分段。"""
    return [(int(a), int(b)) for a, b in spans if int(a) < int(b)]
