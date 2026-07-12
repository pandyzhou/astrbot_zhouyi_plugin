from __future__ import annotations

import base64
import io
import sys
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.script.bar_chart import (
    PlotPoint,
    ServerTrendInput,
    _aggregate_3h_points,
    _format_plot_value,
    _mini_point_render_plan,
    _nice_y_axis,
    _normalize_hourly_window,
    _plot_coordinate,
    _raw_observation_position,
    _select_x_axis_labels,
    _split_contiguous_segments,
    _summary_card_regions,
    _summary_font_sizes,
    _summary_layout,
    _summary_scale,
    _trend_mode,
    generate_bar_chart_image,
    generate_summary_chart_images,
)


NOW = int(datetime(2025, 1, 2, 12, 34).timestamp())
END = NOW // 3600 * 3600


def point(offset: int, count: object, *, seconds: int = 0) -> dict[str, object]:
    return {"ts": END + offset * 3600 + seconds, "count": count}


def decode_png(value: str) -> Image.Image:
    data = base64.b64decode(value)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("not png")
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.copy()


class NormalizeTests(unittest.TestCase):
    def test_fixed_window_missing_zero_duplicate_and_negative(self):
        trend = _normalize_hourly_window(
            [
                point(-4, 9),
                point(-3, 2),
                point(-1, 0),
                point(-1, 7, seconds=10),
                point(0, -3),
            ],
            4,
            now_ts=NOW,
        )
        self.assertEqual(trend.timestamps, [END - 3 * 3600, END - 2 * 3600, END - 3600, END])
        self.assertEqual(trend.values, [2, None, 7, 0])
        self.assertEqual(trend.stats.current, 0)
        self.assertEqual(trend.stats.observed, 3)
        self.assertEqual(trend.stats.total, 4)
        self.assertEqual(trend.stats.last_sample, END)

    def test_current_missing_peak_tie_uses_latest_average_and_completeness(self):
        trend = _normalize_hourly_window(
            [point(-3, 4), point(-2, 8), point(-1, 8)],
            4,
            now_ts=NOW,
        )
        self.assertIsNone(trend.stats.current)
        self.assertEqual(trend.stats.peak, 8)
        self.assertEqual(trend.stats.peak_ts, END - 3600)
        self.assertEqual(trend.stats.average, 6.7)
        self.assertEqual((trend.stats.observed, trend.stats.total), (3, 4))

    def test_bad_old_future_and_boolean_data_are_skipped(self):
        trend = _normalize_hourly_window(
            [
                None,
                {"ts": "bad", "count": 1},
                {"ts": END, "count": "bad"},
                {"ts": True, "count": 1},
                {"ts": END, "count": False},
                point(-10, 8),
                point(1, 9),
                point(0, "5"),
            ],
            3,
            now_ts=NOW,
        )
        self.assertEqual(trend.values, [None, None, 5])
        self.assertEqual(trend.stats.observed, 1)

    def test_modes_at_boundaries(self):
        expected = {24: "bar", 25: "area", 72: "area", 73: "area3h", 168: "area3h"}
        for hours, mode in expected.items():
            with self.subTest(hours=hours):
                self.assertEqual(_trend_mode(hours), mode)
                self.assertEqual(_normalize_hourly_window([], hours, NOW).mode, mode)

    def test_three_hour_aggregation_is_backward_and_strict_about_missing(self):
        timestamps = [END - index * 3600 for index in range(6, -1, -1)]
        values = [1, 2, 3, None, 5, 6, 7]
        points = _aggregate_3h_points(timestamps, values)
        self.assertEqual([item.value for item in points], [1.0, None, 6.0])
        self.assertEqual(points[-1].source_end_ts, END)
        trend = _normalize_hourly_window(
            [point(offset, value) for offset, value in zip(range(-6, 1), values) if value is not None],
            73,
            NOW,
        )
        self.assertEqual(trend.stats.observed, 6)
        self.assertEqual(trend.stats.average, 4.0)

    def test_168h_peak_annotation_uses_raw_hour_coordinate(self):
        history = [point(offset, 6) for offset in range(-167, 1)]
        for offset, value in ((-83, 0), (-82, 28), (-81, 0)):
            history[offset + 167] = point(offset, value)
        trend = _normalize_hourly_window(history, 168, NOW)
        self.assertEqual(trend.mode, "area3h")
        self.assertEqual((trend.stats.peak_ts, trend.stats.peak), (END - 82 * 3600, 28))
        aggregate = next(
            item
            for item in trend.points
            if item.source_start_ts <= trend.stats.peak_ts <= item.source_end_ts
        )
        self.assertEqual(aggregate.value, 9.3)

        bounds = (100.0, 20.0, 900.0, 320.0)
        y_max, _, _ = _nice_y_axis(trend.values, trend.stats.average)
        raw_position = _raw_observation_position(
            trend.timestamps,
            trend.values,
            trend.stats.peak_ts,
            bounds,
            y_max,
        )
        self.assertIsNotNone(raw_position)
        raw_x, raw_y = raw_position
        expected_x = bounds[0] + 85 / 167 * (bounds[2] - bounds[0])
        self.assertAlmostEqual(raw_x, expected_x)

        aggregate_x, aggregate_y = _plot_coordinate(
            aggregate.ts,
            aggregate.value,
            trend.timestamps[0],
            trend.timestamps[-1],
            bounds,
            y_max,
        )
        self.assertLess(raw_y, aggregate_y - 100)
        self.assertNotAlmostEqual(raw_x, aggregate_x)

    def test_contiguous_segments_do_not_cross_missing(self):
        points = [
            PlotPoint(1, 1),
            PlotPoint(2, 2),
            PlotPoint(3, None),
            PlotPoint(4, 0),
            PlotPoint(5, None),
            PlotPoint(6, 3),
        ]
        segments = _split_contiguous_segments(points)
        self.assertEqual([[point.ts for point in segment] for segment in segments], [[1, 2], [4], [6]])

    def test_mini_point_plan_labels_up_to_24_and_keeps_real_zero(self):
        points = [PlotPoint(index, None if index == 7 else index % 4) for index in range(24)]
        plan = _mini_point_render_plan(points)
        self.assertEqual(len(plan), 23)
        self.assertNotIn(7, [index for index, _, _ in plan])
        zero_entries = [(index, label) for index, point, label in plan if point.value == 0]
        self.assertTrue(zero_entries)
        self.assertTrue(all(label == "0" for _, label in zero_entries))
        self.assertTrue(all(label is not None for _, _, label in plan))

    def test_mini_point_plan_draws_all_markers_without_labels_over_24(self):
        points = [PlotPoint(index, None if index in (5, 19) else index / 10) for index in range(25)]
        plan = _mini_point_render_plan(points)
        self.assertEqual([index for index, _, _ in plan], [index for index in range(25) if index not in (5, 19)])
        self.assertTrue(all(label is None for _, _, label in plan))

    def test_plot_value_format_uses_integer_or_one_decimal(self):
        self.assertEqual(_format_plot_value(3.0), "3")
        self.assertEqual(_format_plot_value(2.25), "2.2")
        self.assertEqual(_format_plot_value(2.26), "2.3")

    def test_nice_axis_uses_integer_125_step_and_covers_values(self):
        for values in ([0], [1], [17], [99], [1234]):
            with self.subTest(values=values):
                y_max, step, ticks = _nice_y_axis(values, sum(values) / len(values))
                self.assertGreaterEqual(y_max, max(values))
                self.assertTrue(all(isinstance(item, int) for item in ticks))
                self.assertGreaterEqual(len(ticks) - 1, 4)
                self.assertLessEqual(len(ticks) - 1, 6)
                normalized = step / (10 ** int(len(str(step)) - 1))
                self.assertIn(normalized, (1.0, 2.0, 5.0, 10.0))

    def test_cross_day_labels_include_date_and_keep_endpoints(self):
        timestamps = [int(datetime(2025, 1, 1, 22).timestamp()) + index * 3600 for index in range(8)]
        labels = _select_x_axis_labels(timestamps, max_labels=5)
        self.assertEqual(labels[0][0], 0)
        self.assertEqual(labels[-1][0], len(timestamps) - 1)
        self.assertTrue(all("-" in label for _, label in labels))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.background = Image.new("RGB", (320, 800), (20, 40, 80))
        ImageDraw.Draw(self.background).rectangle((40, 100, 280, 700), fill=(120, 40, 80))

    def test_detail_is_960x540_png_and_does_not_modify_background(self):
        before = (self.background.mode, self.background.size, self.background.tobytes())
        value = generate_bar_chart_image(
            [point(-2, 3), point(-1, 0), point(0, 8)],
            "Alpha",
            hours=24,
            background=self.background,
            now_ts=NOW,
        )
        image = decode_png(value)
        self.assertEqual(image.size, (960, 540))
        self.assertEqual((self.background.mode, self.background.size, self.background.tobytes()), before)

    def test_detail_handles_empty_long_name_and_real_zero(self):
        for history, name in (
            ([], "空服"),
            ([point(0, 0)], "超长服务器名" * 100),
        ):
            with self.subTest(name=name[:4]):
                image = decode_png(
                    generate_bar_chart_image(history, name, background=self.background, now_ts=NOW)
                )
                self.assertEqual(image.size, (960, 540))

    def test_summary_pagination_dynamic_heights_and_background_unchanged(self):
        cases = {
            1: [(1600, 500)],
            4: [(1600, 1200)],
            5: [(1600, 1200), (1600, 500)],
            9: [(1600, 1200), (1600, 1200), (1600, 500)],
        }
        for count, expected_sizes in cases.items():
            servers = [
                ServerTrendInput(
                    str(index),
                    "超长服务器名称" * 20 if index == 2 else f"Server {index}",
                    [] if index == 0 else [point(-2, index), point(-1, 0), point(0, index + 1)],
                )
                for index in range(count)
            ]
            before = (self.background.mode, self.background.size, self.background.tobytes())
            values = generate_summary_chart_images(
                servers,
                hours=24,
                background=self.background,
                now_ts=NOW,
            )
            self.assertEqual(len(values), len(expected_sizes))
            self.assertEqual(
                (self.background.mode, self.background.size, self.background.tobytes()),
                before,
            )
            self.assertEqual([decode_png(value).size for value in values], expected_sizes)

    def test_summary_custom_960x720_keeps_legacy_dynamic_sizes(self):
        servers = [ServerTrendInput(str(index), f"Server {index}", [point(0, index)]) for index in range(4)]
        one_page = generate_summary_chart_images(
            servers[:1],
            width=960,
            height=720,
            background=self.background,
            now_ts=NOW,
        )
        four_page = generate_summary_chart_images(
            servers,
            width=960,
            height=720,
            background=self.background,
            now_ts=NOW,
        )
        self.assertEqual(decode_png(one_page[0]).size, (960, 300))
        self.assertEqual(decode_png(four_page[0]).size, (960, 720))

    def test_summary_page_size_is_clamped_to_one_through_four(self):
        servers = [ServerTrendInput(str(index), f"Server {index}", []) for index in range(5)]
        self.assertEqual(
            len(generate_summary_chart_images(servers, page_size=99, now_ts=NOW)),
            2,
        )
        self.assertEqual(
            len(generate_summary_chart_images(servers, page_size=0, now_ts=NOW)),
            5,
        )

    def test_summary_layout_bounds_are_stable_and_do_not_overflow(self):
        expected_heights = {1: 500, 2: 733, 3: 967, 4: 1200}
        scale = _summary_scale(1600)
        for count, expected_height in expected_heights.items():
            with self.subTest(count=count):
                page_height, cards = _summary_layout(count)
                self.assertEqual(page_height, expected_height)
                self.assertEqual(len(cards), count)
                self.assertEqual(cards[0][1], round(104 * scale))
                self.assertEqual(cards[-1][3], page_height - round(18 * scale))
                for index, card in enumerate(cards):
                    self.assertEqual((card[0], card[2]), (round(28 * scale), 1600 - round(28 * scale)))
                    self.assertGreater(card[3], card[1])
                    self.assertLessEqual(card[3], page_height - round(18 * scale))
                    if index:
                        self.assertEqual(card[1] - cards[index - 1][3], round(10 * scale))
                    left, middle, right = _summary_card_regions(card)
                    for region in (left, middle, right):
                        self.assertGreaterEqual(region[0], card[0])
                        self.assertGreaterEqual(region[1], card[1])
                        self.assertLessEqual(region[2], card[2])
                        self.assertLessEqual(region[3], card[3])
                    self.assertLess(left[2], middle[0])
                    self.assertLess(middle[2], right[0])
                    self.assertGreaterEqual(middle[2] - middle[0], 680)
                    self.assertLessEqual(middle[2] - middle[0], 720)

    def test_summary_layout_keeps_960x720_compatibility(self):
        expected_heights = {1: 300, 2: 440, 3: 580, 4: 720}
        for count, expected_height in expected_heights.items():
            with self.subTest(count=count):
                page_height, cards = _summary_layout(count, width=960, height=720)
                self.assertEqual(page_height, expected_height)
                self.assertEqual(cards[0][1], 104)
                self.assertEqual(cards[-1][3], page_height - 18)
                for index, card in enumerate(cards):
                    self.assertEqual((card[0], card[2]), (28, 932))
                    if index:
                        self.assertEqual(card[1] - cards[index - 1][3], 10)
                    left, middle, right = _summary_card_regions(card)
                    self.assertGreaterEqual(middle[2] - middle[0], 400)
                    self.assertLessEqual(middle[2] - middle[0], 450)
                    for region in (left, middle, right):
                        self.assertGreaterEqual(region[0], card[0])
                        self.assertGreaterEqual(region[1], card[1])
                        self.assertLessEqual(region[2], card[2])
                        self.assertLessEqual(region[3], card[3])

    def test_summary_scale_and_default_typography_minimums(self):
        self.assertAlmostEqual(_summary_scale(1600), 1600 / 960)
        sizes = _summary_font_sizes(1600)
        self.assertGreaterEqual(sizes["title"], 44)
        self.assertGreaterEqual(sizes["subtitle"], 22)
        self.assertGreaterEqual(sizes["page"], 18)
        self.assertGreaterEqual(sizes["name"], 30)
        self.assertGreaterEqual(sizes["small"], 18)
        self.assertGreaterEqual(sizes["current_min"], 58)
        self.assertGreaterEqual(sizes["peak"], 38)
        self.assertGreaterEqual(sizes["completeness"], 26)
        self.assertGreaterEqual(sizes["axis"], 18)
        self.assertGreaterEqual(sizes["point"], 18)


if __name__ == "__main__":
    unittest.main()
