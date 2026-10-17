"""Screen cursor projection, independent of cell parsing."""

import unittest
from goals_iterm_mcp.screen_coordinates import screen_cursor_coord


class ScreenCoordinateTests(unittest.TestCase):
    def test_absolute_cursor_is_projected_onto_screen(self):
        point = screen_cursor_coord(
            {"x": 2, "y": 34},
            {"coord_range": {"start": {"x": 0, "y": 13}, "end": {"x": 0, "y": 38}}},
            row_count=25,
        )
        self.assertEqual((point.x, point.y), (2, 21))

    def test_missing_or_malformed_window_range_does_not_fabricate_visible_cursor(self):
        raw_cursor = {"x": 2, "y": 34}
        self.assertIsNone(screen_cursor_coord(raw_cursor, None, row_count=25))
        self.assertIsNone(
            screen_cursor_coord(
                raw_cursor,
                {"coord_range": {"start": {"x": 0, "y": 13}}},
                row_count=25,
            )
        )
        self.assertIsNone(
            screen_cursor_coord(
                raw_cursor,
                {
                    "coord_range": {
                        "start": {"x": 0, "y": 13},
                        "end": {"x": 0, "y": 38},
                    }
                },
                row_count=10,
            )
        )

    def test_column_window_is_applied_before_visible_projection(self):
        projected = screen_cursor_coord(
            {"x": 7, "y": 4},
            {
                "coord_range": {
                    "start": {"x": 0, "y": 3},
                    "end": {"x": 0, "y": 5},
                },
                "columns": {"location": 5, "length": 10},
            },
            row_count=2,
        )
        self.assertIsNone(projected)

    def test_partial_or_short_ranges_never_fabricate_visible_cursor_rows(self):
        # A range that ends before the advertised screen height cannot anchor
        # a row-zero projection, even when the absolute cursor is positive.
        self.assertIsNone(
            screen_cursor_coord(
                {"x": 2, "y": 34},
                {
                    "coord_range": {
                        "start": {"x": 0, "y": 13},
                        "end": {"x": 0, "y": 14},
                    }
                },
                row_count=25,
            )
        )
        # A nonzero x origin would subtract a column offset from later rows;
        # only full-row ranges are safe for this screen DTO projection.
        self.assertIsNone(
            screen_cursor_coord(
                {"x": 7, "y": 34},
                {
                    "coord_range": {
                        "start": {"x": 5, "y": 13},
                        "end": {"x": 5, "y": 38},
                    }
                },
                row_count=25,
            )
        )
