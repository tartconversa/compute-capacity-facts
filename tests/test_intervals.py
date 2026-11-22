"""左闭右开区间集合运算的边界行为。"""

import unittest

from slotwarehouse.intervals import (
    clip,
    contains,
    intersect,
    is_empty,
    overlaps,
    subtract,
    union,
)


class IntervalTests(unittest.TestCase):
    def test_union_merges_touching_spans(self):
        self.assertEqual(union([(0, 10), (10, 20)], [(5, 15)]), [(0, 20)])

    def test_union_keeps_gaps(self):
        self.assertEqual(union([(0, 10), (11, 20)]), [(0, 10), (11, 20)])

    def test_intersect_is_half_open(self):
        self.assertEqual(intersect([(0, 10)], [(10, 20)]), [])
        self.assertEqual(intersect([(0, 11)], [(10, 20)]), [(10, 11)])

    def test_subtract_splits_span(self):
        self.assertEqual(subtract([(0, 100)], [(40, 60)]), [(0, 40), (60, 100)])
        self.assertEqual(subtract([(0, 100)], [(0, 100)]), [])
        self.assertEqual(subtract([(0, 100)], [(100, 120)]), [(0, 100)])

    def test_clip_bounds(self):
        self.assertEqual(clip((10, 50), [(0, 20), (40, 60)]), [(10, 20), (40, 50)])

    def test_helpers(self):
        self.assertTrue(contains((0, 10), (0, 10)))
        self.assertFalse(contains((0, 10), (5, 11)))
        self.assertFalse(overlaps((0, 10), (10, 20)))
        self.assertTrue(overlaps((0, 11), (10, 20)))
        self.assertTrue(is_empty([(5, 5)]))
        self.assertFalse(is_empty([(5, 6)]))
        self.assertEqual(union([(5, 5), (0, 3)]), [(0, 3)])


if __name__ == "__main__":
    unittest.main()
