from __future__ import annotations

import base64
import io
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.script.bar_chart import (
    PlotPoint,
    ServerTrendInput,
    _draw_mini_chart,
    _nice_y_axis,
    _normalize_hourly_window,
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
        expected = {
            24: "bar",
            25: "line",
            72: "line",
            73: "line",
            96: "line",
            168: "line",
        }
        for hours, mode in expected.items():
            with self.subTest(hours=hours):
                self.assertEqual(_trend_mode(hours), mode)
                self.assertEqual(_normalize_hourly_window([], hours, NOW).mode, mode)

    def test_96_and_168_hour_windows_keep_raw_integer_points_and_missing_hours(self):
        for hours in (96, 168):
            missing_offsets = {-13, -2}
            history = [
                point(offset, (offset + hours) % 11)
                for offset in range(-(hours - 1), 1)
                if offset not in missing_offsets
            ]
            trend = _normalize_hourly_window(history, hours, NOW)

            with self.subTest(hours=hours):
                self.assertEqual(trend.mode, "line")
                self.assertEqual(len(trend.timestamps), hours)
                self.assertEqual(len(trend.points), hours)
                self.assertEqual([item.value for item in trend.points], trend.values)
                self.assertTrue(
                    all(item.value is None or isinstance(item.value, int) for item in trend.points)
                )
                for offset in missing_offsets:
                    index = offset + hours - 1
                    self.assertIsNone(trend.values[index])
                    self.assertIsNone(trend.points[index].value)
                self.assertTrue(
                    all(
                        item.source_start_ts == item.ts == item.source_end_ts
                        for item in trend.points
                    )
                )

    def test_168h_peak_annotation_uses_raw_hour_coordinate(self):
        history = [point(offset, 6) for offset in range(-167, 1)]
        for offset, value in ((-83, 0), (-82, 28), (-81, 0)):
            history[offset + 167] = point(offset, value)
        trend = _normalize_hourly_window(history, 168, NOW)
        self.assertEqual(trend.mode, "line")
        self.assertEqual((trend.stats.peak_ts, trend.stats.peak), (END - 82 * 3600, 28))
        peak_point = next(item for item in trend.points if item.ts == trend.stats.peak_ts)
        self.assertEqual(peak_point.value, 28)

        bounds = (100.0, 20.0, 900.0, 320.0)
        y_max, _, _ = _nice_y_axis(trend.values)
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
        expected_y = bounds[3] - 28 / y_max * (bounds[3] - bounds[1])
        self.assertAlmostEqual(raw_y, expected_y)

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

    def test_mini_chart_draws_nonzero_integer_ticks_as_even_dashed_grid(self):
        values = [0, 1, 2, 3] * 6
        trend = _normalize_hourly_window(
            [point(offset, value) for offset, value in zip(range(-23, 1), values)],
            24,
            NOW,
        )
        image = Image.new("RGBA", (600, 180), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        font = draw.getfont()

        with patch(
            "data.plugins.astrbot_zhouyi_plugin.script.bar_chart._draw_dashed_line"
        ) as draw_dashed_line:
            _draw_mini_chart(
                draw,
                trend,
                (100, 20, 500, 165),
                axis_font=font,
                label_font=font,
                empty_font=font,
                scale=1,
            )

        self.assertEqual(draw_dashed_line.call_count, 3)
        grid_y = [item.args[1][1] for item in draw_dashed_line.call_args_list]
        self.assertAlmostEqual(grid_y[0] - grid_y[1], grid_y[1] - grid_y[2])

    def test_nice_axis_low_range_uses_only_integer_ticks(self):
        y_max, step, ticks = _nice_y_axis([0, 1, None, 2, 3])
        self.assertEqual((y_max, step, ticks), (3, 1, [0, 1, 2, 3]))
        self.assertTrue(all(isinstance(item, int) and item >= 0 for item in ticks))

    def test_nice_axis_high_range_uses_regular_integer_step(self):
        y_max, step, ticks = _nice_y_axis([0, 7, None, 13, 24])
        self.assertEqual((y_max, step, ticks), (25, 5, [0, 5, 10, 15, 20, 25]))

    def test_nice_axis_integer_125_step_covers_larger_values(self):
        for values in ([17], [99], [1234]):
            with self.subTest(values=values):
                y_max, step, ticks = _nice_y_axis(values)
                self.assertGreaterEqual(y_max, max(values))
                self.assertTrue(all(isinstance(item, int) and item >= 0 for item in ticks))
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

    def test_generated_images_cover_24_48_96_and_168_hours(self):
        history = [
            point(offset, (offset + 168) % 25)
            for offset in range(-167, 1)
            if offset != -5
        ]
        server = ServerTrendInput("1", "Alpha", history)
        for hours in (24, 48, 96, 168):
            with self.subTest(hours=hours):
                detail = decode_png(
                    generate_bar_chart_image(
                        history,
                        "Alpha",
                        hours=hours,
                        background=self.background,
                        now_ts=NOW,
                    )
                )
                summary = generate_summary_chart_images(
                    [server],
                    hours=hours,
                    background=self.background,
                    now_ts=NOW,
                )
                self.assertEqual(detail.size, (960, 540))
                self.assertEqual([decode_png(value).size for value in summary], [(1600, 500)])

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
