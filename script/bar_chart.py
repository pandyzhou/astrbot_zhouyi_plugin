from __future__ import annotations

import base64
import io
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


CARD_BG = (22, 28, 43)
TEXT = (245, 248, 255, 255)
MUTED = (174, 190, 211, 255)
ACCENT = (91, 238, 177, 255)
ACCENT_SOFT = (91, 238, 177, 78)
GRID = (130, 150, 178, 66)
PANEL = (8, 15, 29, 226)


@dataclass(frozen=True)
class TrendStats:
    current: Optional[int]
    peak: Optional[int]
    peak_ts: Optional[int]
    average: Optional[float]
    observed: int
    total: int
    last_sample: Optional[int]


@dataclass(frozen=True)
class PlotPoint:
    ts: int
    value: Optional[float]
    source_start_ts: Optional[int] = None
    source_end_ts: Optional[int] = None


@dataclass(frozen=True)
class HourlyTrend:
    timestamps: list[int]
    values: list[Optional[int]]
    points: list[PlotPoint]
    stats: TrendStats
    mode: str


@dataclass(frozen=True)
class ServerTrendInput:
    id: str
    name: str
    history: Sequence[dict[str, Any]]


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = (
        Path(__file__).resolve().parent.parent / "resource" / "LXGWWenKai-Regular.ttf",
        Path(__file__).resolve().parent.parent / "resource" / "msyh.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default().font_variant(size=size)
    except Exception:
        return ImageFont.load_default()


def _text_bbox(
    draw: ImageDraw.ImageDraw,
    text: object,
    font: ImageFont.ImageFont,
) -> tuple[int, int, int, int]:
    return draw.textbbox((0, 0), str(text), font=font)


def _text_size(
    draw: ImageDraw.ImageDraw,
    text: object,
    font: ImageFont.ImageFont,
) -> tuple[int, int]:
    box = _text_bbox(draw, text, font)
    return max(0, box[2] - box[0]), max(0, box[3] - box[1])


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: object,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    value = str(text or "")
    if max_width <= 0:
        return ""
    if _text_size(draw, value, font)[0] <= max_width:
        return value
    suffix = "…"
    if _text_size(draw, suffix, font)[0] > max_width:
        return ""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if _text_size(draw, value[:middle] + suffix, font)[0] <= max_width:
            low = middle
        else:
            high = middle - 1
    return value[:low] + suffix


def _draw_text_clamped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: object,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    bounds: tuple[int, int, int, int],
    anchor: Optional[str] = None,
) -> None:
    value = str(text)
    box = draw.textbbox(xy, value, font=font, anchor=anchor)
    dx = 0
    dy = 0
    if box[0] < bounds[0]:
        dx = bounds[0] - box[0]
    elif box[2] > bounds[2]:
        dx = bounds[2] - box[2]
    if box[1] < bounds[1]:
        dy = bounds[1] - box[1]
    elif box[3] > bounds[3]:
        dy = bounds[3] - box[3]
    draw.text((xy[0] + dx, xy[1] + dy), value, font=font, fill=fill, anchor=anchor)


def _coerce_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_hourly_window(
    history: Sequence[dict[str, Any]],
    hours: int,
    now_ts: Optional[int] = None,
) -> HourlyTrend:
    """归一化为结束于当前整点的严格 N 小时窗口；缺失值保持为 None。"""
    parsed_hours = _coerce_int(hours)
    window_hours = max(1, parsed_hours if parsed_hours is not None else 24)
    parsed_now = _coerce_int(now_ts)
    current_ts = int(time.time()) if parsed_now is None else parsed_now
    end_bucket = current_ts // 3600 * 3600
    start_bucket = end_bucket - (window_hours - 1) * 3600

    by_bucket: dict[int, int] = {}
    for item in history or ():
        if not isinstance(item, dict):
            continue
        ts = _coerce_int(item.get("ts"))
        count = _coerce_int(item.get("count"))
        if ts is None or count is None or ts < 0:
            continue
        bucket = ts // 3600 * 3600
        if bucket < start_bucket or bucket > end_bucket:
            continue
        by_bucket[bucket] = max(0, count)

    timestamps = [start_bucket + index * 3600 for index in range(window_hours)]
    values = [by_bucket.get(ts) for ts in timestamps]
    observed_pairs = [(ts, value) for ts, value in zip(timestamps, values) if value is not None]
    current = values[-1]
    if observed_pairs:
        peak = max(value for _, value in observed_pairs)
        peak_ts = max(ts for ts, value in observed_pairs if value == peak)
        average = round(sum(value for _, value in observed_pairs) / len(observed_pairs), 1)
        last_sample = observed_pairs[-1][0]
    else:
        peak = None
        peak_ts = None
        average = None
        last_sample = None
    stats = TrendStats(
        current=current,
        peak=peak,
        peak_ts=peak_ts,
        average=average,
        observed=len(observed_pairs),
        total=window_hours,
        last_sample=last_sample,
    )
    mode = _trend_mode(window_hours)
    points = (
        _aggregate_3h_points(timestamps, values)
        if mode == "area3h"
        else [PlotPoint(ts, value, ts, ts) for ts, value in zip(timestamps, values)]
    )
    return HourlyTrend(timestamps, values, points, stats, mode)


def _trend_mode(hours: int) -> str:
    value = max(1, int(hours))
    if value <= 24:
        return "bar"
    if value <= 72:
        return "area"
    return "area3h"


def _aggregate_3h_points(
    timestamps: Sequence[int],
    values: Sequence[Optional[int]],
) -> list[PlotPoint]:
    """从当前小时向前按三小时分组；组内缺任一点即为缺失。"""
    groups: list[PlotPoint] = []
    end = len(timestamps)
    while end > 0:
        start = max(0, end - 3)
        group_ts = timestamps[start:end]
        group_values = values[start:end]
        value: Optional[float]
        if any(item is None for item in group_values):
            value = None
        else:
            value = round(sum(int(item) for item in group_values if item is not None) / len(group_values), 1)
        groups.append(
            PlotPoint(
                ts=group_ts[-1],
                value=value,
                source_start_ts=group_ts[0],
                source_end_ts=group_ts[-1],
            )
        )
        end = start
    groups.reverse()
    return groups


def _split_contiguous_segments(points: Sequence[PlotPoint]) -> list[list[PlotPoint]]:
    segments: list[list[PlotPoint]] = []
    current: list[PlotPoint] = []
    for point in points:
        if point.value is None:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(point)
    if current:
        segments.append(current)
    return segments


def _nice_number(value: float, *, ceil_value: bool) -> float:
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10**exponent)
    choices = (1.0, 2.0, 5.0, 10.0)
    if ceil_value:
        nice_fraction = next(choice for choice in choices if fraction <= choice)
    else:
        nice_fraction = min(choices, key=lambda choice: abs(choice - fraction))
    return nice_fraction * (10**exponent)


def _nice_y_axis(
    values: Iterable[Optional[float]],
    average: Optional[float] = None,
) -> tuple[int, int, list[int]]:
    candidates = [float(value) for value in values if value is not None]
    if average is not None:
        candidates.append(float(average))
    maximum = max(candidates, default=0.0)
    step = max(1, int(math.ceil(_nice_number(maximum / 5.0, ceil_value=True))))
    intervals = max(4, int(math.ceil(maximum / step)))
    while intervals > 6:
        step = max(step + 1, int(math.ceil(_nice_number(step * 1.01, ceil_value=True))))
        intervals = int(math.ceil(maximum / step))
    intervals = max(4, min(6, intervals))
    y_max = step * intervals
    while y_max < maximum:
        intervals += 1
        if intervals > 6:
            step = max(step + 1, int(math.ceil(_nice_number(step * 1.01, ceil_value=True))))
            intervals = max(4, int(math.ceil(maximum / step)))
        y_max = step * intervals
    return y_max, step, [step * index for index in range(intervals + 1)]


def _select_x_axis_labels(
    timestamps: Sequence[int],
    max_labels: int = 6,
) -> list[tuple[int, str]]:
    if not timestamps:
        return []
    limit = max(2, int(max_labels))
    count = len(timestamps)
    if count <= limit:
        indices = list(range(count))
    else:
        indices = sorted(
            {
                round(index * (count - 1) / (limit - 1))
                for index in range(limit)
            }
        )
    start_day = datetime.fromtimestamp(timestamps[0]).date()
    end_day = datetime.fromtimestamp(timestamps[-1]).date()
    crosses_day = start_day != end_day
    pattern = "%m-%d %H:%M" if crosses_day else "%H:%M"
    return [(index, datetime.fromtimestamp(timestamps[index]).strftime(pattern)) for index in indices]


def _make_fallback_background(size: tuple[int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size, CARD_BG)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = (int(20 + 22 * ratio), int(28 + 28 * ratio), int(48 + 42 * ratio))
        draw.line((0, y, width, y), fill=color)
    return image


def _prepare_canvas(
    background: Optional[Image.Image],
    size: tuple[int, int],
) -> Image.Image:
    source = background.copy() if background is not None else _make_fallback_background(size)
    canvas = ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    canvas = canvas.convert("RGBA")
    canvas = Image.alpha_composite(canvas, Image.new("RGBA", size, (3, 8, 18, 105)))
    return canvas


def _paste_logo(
    canvas: Image.Image,
    bounds: tuple[int, int, int, int],
    *,
    radius: int = 14,
) -> None:
    try:
        with Image.open(Path(__file__).resolve().parent.parent / "logo.png") as source:
            source.load()
            logo = ImageOps.contain(
                source.convert("RGBA"),
                (bounds[2] - bounds[0], bounds[3] - bounds[1]),
                method=Image.Resampling.LANCZOS,
            )
        plate_size = (bounds[2] - bounds[0], bounds[3] - bounds[1])
        plate = Image.new("RGBA", plate_size, (245, 248, 255, 235))
        plate.alpha_composite(
            logo,
            ((plate_size[0] - logo.width) // 2, (plate_size[1] - logo.height) // 2),
        )
        mask = Image.new("L", plate_size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, plate_size[0] - 1, plate_size[1] - 1), radius=radius, fill=255
        )
        framed = Image.new("RGBA", plate_size, (0, 0, 0, 0))
        framed.paste(plate, (0, 0), mask)
        canvas.alpha_composite(framed, (bounds[0], bounds[1]))
    except Exception:
        return


def _format_hour(ts: Optional[int]) -> str:
    return "--" if ts is None else datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _format_plot_value(value: float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.1f}"


def _mini_point_render_plan(
    points: Sequence[PlotPoint],
) -> list[tuple[int, PlotPoint, Optional[str]]]:
    """返回非缺失绘制点；仅在点数不超过 24 时附带逐点数值。"""
    show_labels = len(points) <= 24
    return [
        (index, point, _format_plot_value(float(point.value)) if show_labels else None)
        for index, point in enumerate(points)
        if point.value is not None
    ]


def _point_x(index: int, count: int, left: float, right: float) -> float:
    if count <= 1:
        return (left + right) / 2
    return left + index * (right - left) / (count - 1)


def _plot_coordinate(
    ts: int,
    value: float,
    window_start_ts: int,
    window_end_ts: int,
    bounds: tuple[float, float, float, float],
    y_max: float,
) -> tuple[float, float]:
    """按窗口真实时间比例和原始数值映射绘图坐标。"""
    left, top, right, bottom = bounds
    if window_end_ts <= window_start_ts:
        x = (left + right) / 2
    else:
        relative = (ts - window_start_ts) / (window_end_ts - window_start_ts)
        x = left + max(0.0, min(1.0, relative)) * (right - left)
    height = bottom - top
    normalized = 0.0 if y_max <= 0 else max(0.0, min(float(value), y_max)) / y_max
    return x, bottom - normalized * height


def _raw_observation_position(
    timestamps: Sequence[int],
    values: Sequence[Optional[int]],
    ts: int,
    bounds: tuple[float, float, float, float],
    y_max: float,
) -> Optional[tuple[float, float]]:
    """返回指定原始小时观测的真实时间与真实数值坐标。"""
    if not timestamps:
        return None
    try:
        index = timestamps.index(ts)
    except ValueError:
        return None
    if index >= len(values) or values[index] is None:
        return None
    return _plot_coordinate(
        ts,
        float(values[index]),
        timestamps[0],
        timestamps[-1],
        bounds,
        y_max,
    )


def _draw_plot(
    draw: ImageDraw.ImageDraw,
    trend: HourlyTrend,
    bounds: tuple[int, int, int, int],
    axis_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
) -> None:
    left, top, right, bottom = bounds
    if trend.stats.observed == 0:
        empty_font = _load_font(22)
        text = "窗口内无数据"
        width, height = _text_size(draw, text, empty_font)
        draw.text(
            ((left + right - width) / 2, (top + bottom - height) / 2),
            text,
            font=empty_font,
            fill=MUTED,
        )
        return

    y_max, _, ticks = _nice_y_axis(trend.values)
    plot_left = left + 54
    plot_right = right - 16
    plot_top = top + 13
    plot_bottom = bottom - 40
    plot_height = plot_bottom - plot_top

    def y_at(value: float) -> float:
        return plot_bottom - max(0.0, min(float(value), y_max)) / y_max * plot_height

    for tick in ticks:
        y = y_at(tick)
        draw.line((plot_left, y, plot_right, y), fill=GRID, width=1)
        label = str(tick)
        tw, th = _text_size(draw, label, axis_font)
        draw.text((plot_left - tw - 10, y - th / 2), label, font=axis_font, fill=MUTED)

    points = trend.points
    if trend.mode == "bar":
        spacing = (plot_right - plot_left) / max(1, len(points))
        bar_width = max(3.0, min(24.0, spacing * 0.62))
        for index, point in enumerate(points):
            if point.value is None:
                continue
            x = plot_left + spacing * (index + 0.5)
            if point.value == 0:
                draw.rounded_rectangle(
                    (x - bar_width / 2, plot_bottom - 3, x + bar_width / 2, plot_bottom + 1),
                    radius=2,
                    fill=ACCENT,
                )
                continue
            y = y_at(point.value)
            draw.rounded_rectangle(
                (x - bar_width / 2, y, x + bar_width / 2, plot_bottom),
                radius=min(5, int(bar_width / 2)),
                fill=ACCENT,
            )
    else:
        for segment in _split_contiguous_segments(points):
            coordinates = []
            for point in segment:
                index = points.index(point)
                x = _point_x(index, len(points), plot_left, plot_right)
                y = y_at(float(point.value))
                coordinates.append((x, y))
            if len(coordinates) >= 2:
                polygon = coordinates + [(coordinates[-1][0], plot_bottom), (coordinates[0][0], plot_bottom)]
                draw.polygon(polygon, fill=ACCENT_SOFT)
                draw.line(coordinates, fill=ACCENT, width=3, joint="curve")
            elif coordinates:
                x, y = coordinates[0]
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=ACCENT)
            for point, (x, y) in zip(segment, coordinates):
                if point.value == 0:
                    draw.ellipse((x - 3, plot_bottom - 3, x + 3, plot_bottom + 3), fill=ACCENT)

    labels = _select_x_axis_labels(trend.timestamps)
    planned_labels: list[tuple[str, float, int]] = []
    for index, label in labels:
        relative = 0 if len(trend.timestamps) <= 1 else index / (len(trend.timestamps) - 1)
        x = plot_left + relative * (plot_right - plot_left)
        tw, _ = _text_size(draw, label, axis_font)
        label_x = max(left + 2, min(right - tw - 2, x - tw / 2))
        planned_labels.append((label, label_x, tw))
    if planned_labels:
        first_label, first_x, first_width = planned_labels[0]
        draw.text((first_x, plot_bottom + 12), first_label, font=axis_font, fill=MUTED)
        occupied_right = first_x + first_width
        last_left = planned_labels[-1][1]
        for label, label_x, label_width in planned_labels[1:-1]:
            if label_x >= occupied_right + 8 and label_x + label_width <= last_left - 8:
                draw.text((label_x, plot_bottom + 12), label, font=axis_font, fill=MUTED)
                occupied_right = label_x + label_width
        if len(planned_labels) > 1:
            last_label, last_x, _ = planned_labels[-1]
            draw.text((last_x, plot_bottom + 12), last_label, font=axis_font, fill=MUTED)

    annotations: list[tuple[int, str]] = []
    if trend.stats.peak_ts is not None and trend.stats.peak is not None:
        annotations.append((trend.stats.peak_ts, f"峰值 {trend.stats.peak}"))
    current_ts = trend.timestamps[-1]
    if trend.stats.current is not None:
        if annotations and annotations[0][0] == current_ts:
            annotations[0] = (current_ts, f"峰值/当前 {trend.stats.current}")
        else:
            annotations.append((current_ts, f"当前 {trend.stats.current}"))
    annotation_bounds = (plot_left, plot_top, plot_right, plot_bottom)
    for ts, label in annotations:
        position = _raw_observation_position(
            trend.timestamps,
            trend.values,
            ts,
            annotation_bounds,
            y_max,
        )
        if position is None:
            continue
        x, y = position
        draw.ellipse(
            (x - 6, y - 6, x + 6, y + 6),
            fill=(10, 21, 35, 255),
            outline=TEXT,
            width=2,
        )
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=ACCENT)
        tw, th = _text_size(draw, label, value_font)
        label_x = max(plot_left + 4, min(plot_right - tw - 4, x - tw / 2))
        label_y = max(plot_top + 3, min(plot_bottom - th - 3, y - th - 10))
        draw.rounded_rectangle(
            (label_x - 4, label_y - 3, label_x + tw + 4, label_y + th + 3),
            radius=5,
            fill=(10, 21, 35, 230),
        )
        draw.text((label_x, label_y), label, font=value_font, fill=TEXT)


def _encode_png(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def generate_bar_chart_image(
    history: Sequence[dict[str, Any]],
    server_name: str,
    hours: int = 24,
    width: int = 960,
    height: int = 540,
    *,
    background: Optional[Image.Image] = None,
    now_ts: Optional[int] = None,
) -> str:
    """生成单服趋势仪表卡，并同步返回 Base64 PNG。"""
    trend = _normalize_hourly_window(history, hours, now_ts=now_ts)
    canvas = _prepare_canvas(background, (width, height))
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle((18, 18, width - 18, height - 18), radius=28, fill=(5, 11, 23, 82), outline=(255, 255, 255, 130), width=2)
    overlay_draw.rounded_rectangle((28, 196, width - 28, height - 54), radius=22, fill=PANEL, outline=(255, 255, 255, 48), width=1)
    canvas = Image.alpha_composite(canvas, overlay)
    _paste_logo(canvas, (34, 32, 94, 92))
    draw = ImageDraw.Draw(canvas)

    title_font = _load_font(28)
    range_font = _load_font(15)
    stat_label_font = _load_font(14)
    stat_value_font = _load_font(22)
    axis_font = _load_font(12)
    value_font = _load_font(13)
    footer_font = _load_font(13)

    title = _ellipsize(draw, server_name or "未命名服务器", title_font, width - 148)
    draw.text((110, 33), title, font=title_font, fill=TEXT)
    range_text = (
        f"{_format_hour(trend.timestamps[0])} — {_format_hour(trend.timestamps[-1])}"
        f" · 最近 {trend.stats.total} 小时"
    )
    draw.text((110, 70), _ellipsize(draw, range_text, range_font, width - 148), font=range_font, fill=MUTED)

    stat_left = 32
    stat_top = 105
    gap = 12
    stat_width = (width - stat_left * 2 - gap * 2) / 3
    completeness = trend.stats.observed / trend.stats.total if trend.stats.total else 0.0
    stats = (
        ("当前", "--" if trend.stats.current is None else str(trend.stats.current), "当前小时"),
        ("峰值", "--" if trend.stats.peak is None else str(trend.stats.peak), _format_hour(trend.stats.peak_ts)),
        ("完整度", f"{trend.stats.observed}/{trend.stats.total}", f"{completeness:.0%}"),
    )
    for index, (label, value, note) in enumerate(stats):
        left = int(stat_left + index * (stat_width + gap))
        right = int(left + stat_width)
        draw.rounded_rectangle((left, stat_top, right, 182), radius=15, fill=(9, 17, 31, 220), outline=(255, 255, 255, 42), width=1)
        draw.text((left + 14, stat_top + 10), label, font=stat_label_font, fill=MUTED)
        draw.text((left + 14, stat_top + 31), _ellipsize(draw, value, stat_value_font, right - left - 28), font=stat_value_font, fill=TEXT)
        note_text = _ellipsize(draw, note, stat_label_font, right - left - 28)
        note_width, _ = _text_size(draw, note_text, stat_label_font)
        draw.text((right - note_width - 14, stat_top + 13), note_text, font=stat_label_font, fill=MUTED)

    _draw_plot(draw, trend, (34, 202, width - 34, height - 60), axis_font, value_font)
    mode_text = {"bar": "小时柱状", "area": "小时面积", "area3h": "3 小时聚合面积"}[trend.mode]
    footer = f"最后采样：{_format_hour(trend.stats.last_sample)} · {mode_text}"
    draw.text((36, height - 43), _ellipsize(draw, footer, footer_font, width - 72), font=footer_font, fill=MUTED)
    return _encode_png(canvas)


def _summary_scale(width: int) -> float:
    """返回汇总图相对 960px 逻辑画布的绘制缩放。"""
    return max(1, int(width)) / 960.0


def _scaled(value: float, scale: float, *, minimum: int = 1) -> int:
    return max(minimum, round(float(value) * scale))


def _summary_font_sizes(width: int) -> dict[str, int]:
    """集中维护汇总图逻辑字号，确保高分辨率小字仍可读。"""
    scale = _summary_scale(width)
    return {
        "title": _scaled(27, scale),
        "subtitle": _scaled(14, scale),
        "page": _scaled(12, scale),
        "name": _scaled(19, scale),
        "small": _scaled(12, scale),
        "note": _scaled(11, scale),
        "current_max": _scaled(38, scale),
        "current_min": _scaled(35, scale),
        "peak": _scaled(24, scale),
        "completeness": _scaled(17, scale),
        "axis": _scaled(12, scale),
        "point": _scaled(11, scale),
        "empty": _scaled(14, scale),
    }


def _summary_layout(
    server_count: int,
    width: int = 1600,
    height: int = 1200,
) -> tuple[int, list[tuple[int, int, int, int]]]:
    """返回动态页高和卡片边界；height 表示四台时的最大/基准高度。"""
    count = max(1, min(4, int(server_count)))
    canvas_width = max(640, int(width))
    scale = _summary_scale(canvas_width)
    maximum_height = max(_scaled(280, scale), int(height))
    canvas_height = min(maximum_height, round((160 + count * 140) * scale))
    header_bottom = _scaled(104, scale)
    bottom_margin = _scaled(18, scale)
    card_gap = _scaled(10, scale)
    side_margin = _scaled(28, scale)
    available = canvas_height - header_bottom - bottom_margin - card_gap * (count - 1)
    card_height = available / count
    cards: list[tuple[int, int, int, int]] = []
    for index in range(count):
        top = header_bottom if not cards else cards[-1][3] + card_gap
        bottom = (
            canvas_height - bottom_margin
            if index == count - 1
            else round(top + card_height)
        )
        cards.append((side_margin, top, canvas_width - side_margin, bottom))
    return canvas_height, cards


def _summary_card_regions(
    card_bounds: tuple[int, int, int, int],
    scale: Optional[float] = None,
) -> tuple[
    tuple[int, int, int, int],
    tuple[int, int, int, int],
    tuple[int, int, int, int],
]:
    left, top, right, bottom = card_bounds
    resolved_scale = _summary_scale(right + left) if scale is None else scale
    inner_left = left + _scaled(16, resolved_scale)
    inner_right = right - _scaled(16, resolved_scale)
    inner_width = inner_right - inner_left
    gap = _scaled(16, resolved_scale)
    left_width = min(
        _scaled(202, resolved_scale),
        max(_scaled(150, resolved_scale), round(inner_width * 0.232)),
    )
    right_width = min(
        _scaled(216, resolved_scale),
        max(_scaled(170, resolved_scale), round(inner_width * 0.248)),
    )
    vertical_padding = _scaled(10, resolved_scale)
    middle_left = inner_left + left_width + gap
    right_left = inner_right - right_width
    left_region = (inner_left, top + vertical_padding, inner_left + left_width, bottom - vertical_padding)
    middle_region = (middle_left, top + vertical_padding, right_left - gap, bottom - vertical_padding)
    right_region = (right_left, top + vertical_padding, inner_right, bottom - vertical_padding)
    return left_region, middle_region, right_region


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: object,
    maximum_size: int,
    minimum_size: int,
    max_width: int,
) -> ImageFont.ImageFont:
    for size in range(maximum_size, minimum_size - 1, -1):
        font = _load_font(size)
        if _text_size(draw, text, font)[0] <= max_width:
            return font
    return _load_font(minimum_size)


def _draw_mini_x_labels(
    draw: ImageDraw.ImageDraw,
    timestamps: Sequence[int],
    plot_bounds: tuple[int, int, int, int],
    label_y: int,
    font: ImageFont.ImageFont,
    scale: float,
) -> None:
    if not timestamps:
        return
    left, _, right, _ = plot_bounds
    candidates: list[tuple[str, float, int]] = []
    for index, label in _select_x_axis_labels(timestamps, max_labels=3):
        relative = 0.0 if len(timestamps) <= 1 else index / (len(timestamps) - 1)
        center = left + relative * (right - left)
        text_width, _ = _text_size(draw, label, font)
        text_left = max(left, min(right - text_width, center - text_width / 2))
        candidates.append((label, text_left, text_width))
    if not candidates:
        return
    first = candidates[0]
    draw.text((first[1], label_y), first[0], font=font, fill=MUTED)
    occupied_right = first[1] + first[2]
    if len(candidates) > 2:
        middle = candidates[1]
        last_left = candidates[-1][1]
        label_gap = _scaled(8, scale)
        if middle[1] >= occupied_right + label_gap and middle[1] + middle[2] <= last_left - label_gap:
            draw.text((middle[1], label_y), middle[0], font=font, fill=MUTED)
    if len(candidates) > 1:
        last = candidates[-1]
        if last[1] >= occupied_right + _scaled(8, scale):
            draw.text((last[1], label_y), last[0], font=font, fill=MUTED)


def _draw_mini_chart(
    draw: ImageDraw.ImageDraw,
    trend: HourlyTrend,
    bounds: tuple[int, int, int, int],
    *,
    axis_font: ImageFont.ImageFont,
    label_font: ImageFont.ImageFont,
    empty_font: ImageFont.ImageFont,
    scale: float,
) -> None:
    left, top, right, bottom = bounds
    plot_bounds = (left, top + _scaled(4, scale), right, bottom - _scaled(25, scale))
    plot_left, plot_top, plot_right, plot_bottom = plot_bounds
    baseline_width = _scaled(1.5, scale)
    draw.line(
        (plot_left, plot_bottom, plot_right, plot_bottom),
        fill=(188, 205, 226, 110),
        width=baseline_width,
    )
    _draw_mini_x_labels(
        draw,
        trend.timestamps,
        plot_bounds,
        plot_bottom + _scaled(7, scale),
        axis_font,
        scale,
    )

    if trend.stats.observed == 0:
        text = _ellipsize(
            draw,
            "窗口内无历史采样",
            empty_font,
            plot_right - plot_left - _scaled(16, scale),
        )
        tw, th = _text_size(draw, text, empty_font)
        draw.text(
            ((plot_left + plot_right - tw) / 2, (plot_top + plot_bottom - th) / 2),
            text,
            font=empty_font,
            fill=MUTED,
        )
        return

    y_max, _, _ = _nice_y_axis(trend.values)

    def coordinate(point: PlotPoint) -> tuple[float, float]:
        return _plot_coordinate(
            point.ts,
            float(point.value),
            trend.timestamps[0],
            trend.timestamps[-1],
            plot_bounds,
            y_max,
        )

    line_width = _scaled(2, scale)
    point_radius = _scaled(3, scale)
    peak_radius = _scaled(5, scale)
    marker_outline = _scaled(1.5, scale)
    for segment in _split_contiguous_segments(trend.points):
        coordinates = [coordinate(point) for point in segment]
        if len(coordinates) >= 2:
            polygon = coordinates + [
                (coordinates[-1][0], plot_bottom),
                (coordinates[0][0], plot_bottom),
            ]
            draw.polygon(polygon, fill=(91, 238, 177, 42))
            draw.line(coordinates, fill=ACCENT, width=line_width, joint="curve")

    plotted_peak = False
    padding_x = _scaled(3, scale)
    padding_y = _scaled(2, scale)
    label_gap = _scaled(4, scale)
    label_radius = _scaled(3, scale)
    label_outline = _scaled(1, scale)
    stroke_width = _scaled(0.6, scale)
    for point_index, point, label in _mini_point_render_plan(trend.points):
        x, y = coordinate(point)
        is_peak = trend.stats.peak_ts == point.ts and trend.stats.peak == point.value
        radius = peak_radius if is_peak else point_radius
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(7, 16, 29, 255),
            outline=TEXT if is_peak else ACCENT,
            width=marker_outline,
        )
        if is_peak:
            plotted_peak = True
            inner_radius = max(2, round(radius * 0.48))
            draw.ellipse(
                (x - inner_radius, y - inner_radius, x + inner_radius, y + inner_radius),
                fill=ACCENT,
            )
        if label is None:
            continue
        label = _ellipsize(draw, label, label_font, plot_right - plot_left - padding_x * 2)
        tw, th = _text_size(draw, label, label_font)
        label_x = max(plot_left + padding_x, min(plot_right - tw - padding_x, x - tw / 2))
        above_y = y - th - radius - label_gap - padding_y
        below_y = y + radius + label_gap + padding_y
        prefer_below = point_index % 2 == 1 or above_y < plot_top + padding_y
        label_y = below_y if prefer_below else above_y
        label_y = max(plot_top + padding_y, min(plot_bottom - th - padding_y, label_y))
        draw.rounded_rectangle(
            (
                label_x - padding_x,
                label_y - padding_y,
                label_x + tw + padding_x,
                label_y + th + padding_y,
            ),
            radius=label_radius,
            fill=(5, 12, 24, 225),
            outline=(228, 236, 247, 150),
            width=label_outline,
        )
        draw.text(
            (label_x, label_y),
            label,
            font=label_font,
            fill=TEXT,
            stroke_width=stroke_width,
            stroke_fill=(2, 7, 16, 255),
        )

    if trend.stats.peak_ts is not None and not plotted_peak:
        peak_position = _raw_observation_position(
            trend.timestamps,
            trend.values,
            trend.stats.peak_ts,
            plot_bounds,
            y_max,
        )
        if peak_position is not None:
            peak_x, peak_y = peak_position
            draw.ellipse(
                (
                    peak_x - peak_radius,
                    peak_y - peak_radius,
                    peak_x + peak_radius,
                    peak_y + peak_radius,
                ),
                fill=(7, 16, 29, 255),
                outline=TEXT,
                width=marker_outline,
            )
            inner_radius = max(2, round(peak_radius * 0.48))
            draw.ellipse(
                (
                    peak_x - inner_radius,
                    peak_y - inner_radius,
                    peak_x + inner_radius,
                    peak_y + inner_radius,
                ),
                fill=ACCENT,
            )


def _draw_summary_server_card(
    draw: ImageDraw.ImageDraw,
    card_bounds: tuple[int, int, int, int],
    server: ServerTrendInput,
    trend: HourlyTrend,
    *,
    scale: float,
    font_sizes: dict[str, int],
) -> None:
    left, top, right, bottom = card_bounds
    draw.rounded_rectangle(
        card_bounds,
        radius=_scaled(17, scale),
        fill=(6, 13, 26, 242),
        outline=(255, 255, 255, 56),
        width=_scaled(1, scale),
    )
    left_region, middle_region, right_region = _summary_card_regions(card_bounds, scale)
    separator_offset = _scaled(8, scale)
    for separator_x in (left_region[2] + separator_offset, middle_region[2] + separator_offset):
        draw.line(
            (
                separator_x,
                top + _scaled(18, scale),
                separator_x,
                bottom - _scaled(18, scale),
            ),
            fill=(255, 255, 255, 32),
            width=_scaled(1, scale),
        )

    name_font = _load_font(font_sizes["name"])
    id_font = _load_font(font_sizes["small"])
    label_font = _load_font(font_sizes["small"])
    note_font = _load_font(font_sizes["note"])
    peak_value_font = _load_font(font_sizes["peak"])
    completeness_font = _load_font(font_sizes["completeness"])
    axis_font = _load_font(font_sizes["axis"])
    point_font = _load_font(font_sizes["point"])
    empty_font = _load_font(font_sizes["empty"])

    lx0, ly0, lx1, ly1 = left_region
    left_width = lx1 - lx0
    name = _ellipsize(draw, server.name or f"ID {server.id}", name_font, left_width)
    _draw_text_clamped(
        draw,
        (lx0, ly0 + _scaled(1, scale)),
        name,
        font=name_font,
        fill=TEXT,
        bounds=left_region,
    )
    id_text = _ellipsize(draw, f"ID {server.id}", id_font, left_width)
    _draw_text_clamped(
        draw,
        (lx0, ly0 + _scaled(30, scale)),
        id_text,
        font=id_font,
        fill=MUTED,
        bounds=left_region,
    )
    _draw_text_clamped(
        draw,
        (lx0, ly0 + _scaled(57, scale)),
        "当前在线",
        font=label_font,
        fill=MUTED,
        bounds=left_region,
    )
    current_text = "--" if trend.stats.current is None else str(trend.stats.current)
    current_max_width = max(_scaled(48, scale), left_width - _scaled(82, scale))
    current_font = _fit_font(
        draw,
        current_text,
        font_sizes["current_max"],
        font_sizes["current_min"],
        current_max_width,
    )
    current_text = _ellipsize(draw, current_text, current_font, current_max_width)
    current_width, current_height = _text_size(draw, current_text, current_font)
    current_y = min(ly1 - current_height - _scaled(2, scale), ly0 + _scaled(75, scale))
    _draw_text_clamped(
        draw,
        (lx0, current_y),
        current_text,
        font=current_font,
        fill=ACCENT,
        bounds=left_region,
    )
    note_width = _text_size(draw, "人 / 当前小时", note_font)[0]
    note_x = min(lx1 - note_width, lx0 + current_width + _scaled(10, scale))
    _draw_text_clamped(
        draw,
        (note_x, current_y + max(_scaled(6, scale), current_height - _scaled(17, scale))),
        "人 / 当前小时",
        font=note_font,
        fill=TEXT,
        bounds=left_region,
    )

    _draw_mini_chart(
        draw,
        trend,
        middle_region,
        axis_font=axis_font,
        label_font=point_font,
        empty_font=empty_font,
        scale=scale,
    )

    rx0, ry0, rx1, ry1 = right_region
    right_width = rx1 - rx0
    completeness = trend.stats.observed / trend.stats.total if trend.stats.total else 0.0
    missing = max(0, trend.stats.total - trend.stats.observed)
    missing_text = "采样完整" if missing == 0 else f"缺失 {missing} 小时"

    _draw_text_clamped(
        draw,
        (rx0, ry0),
        "峰值",
        font=label_font,
        fill=MUTED,
        bounds=right_region,
    )
    peak_text = "--" if trend.stats.peak is None else f"{trend.stats.peak} 人"
    _draw_text_clamped(
        draw,
        (rx0, ry0 + _scaled(15, scale)),
        _ellipsize(draw, peak_text, peak_value_font, right_width),
        font=peak_value_font,
        fill=TEXT,
        bounds=right_region,
    )
    _draw_text_clamped(
        draw,
        (rx0, ry0 + _scaled(49, scale)),
        "采样完整度",
        font=label_font,
        fill=MUTED,
        bounds=right_region,
    )
    sample_text = f"{trend.stats.observed}/{trend.stats.total} · {completeness:.0%}"
    _draw_text_clamped(
        draw,
        (rx0, ry0 + _scaled(64, scale)),
        _ellipsize(draw, sample_text, completeness_font, right_width),
        font=completeness_font,
        fill=TEXT,
        bounds=right_region,
    )
    progress_height = _scaled(9, scale)
    progress_top = min(ry1 - _scaled(25, scale), ry0 + _scaled(88, scale))
    progress_bounds = (rx0, progress_top, rx1, min(ry1, progress_top + progress_height))
    draw.rounded_rectangle(
        progress_bounds,
        radius=_scaled(4, scale),
        fill=(64, 78, 99, 220),
    )
    filled_right = rx0 + round((rx1 - rx0) * max(0.0, min(1.0, completeness)))
    if filled_right > rx0:
        draw.rounded_rectangle(
            (rx0, progress_top, filled_right, progress_bounds[3]),
            radius=_scaled(4, scale),
            fill=ACCENT,
        )
    status_fill = ACCENT if missing == 0 else (255, 205, 109, 255)
    status = _ellipsize(draw, missing_text, label_font, right_width)
    _draw_text_clamped(
        draw,
        (rx0, progress_bounds[3] + _scaled(6, scale)),
        status,
        font=label_font,
        fill=status_fill,
        bounds=right_region,
    )


def generate_summary_chart_images(
    servers: Sequence[ServerTrendInput],
    hours: int = 24,
    background: Optional[Image.Image] = None,
    now_ts: Optional[int] = None,
    page_size: int = 4,
    width: int = 1600,
    height: int = 1200,
) -> list[str]:
    """生成按输入顺序分页、按当页服务器数量自适应高度的全服汇总图。"""
    normalized_page_size = max(1, min(4, int(page_size)))
    canvas_width = max(640, int(width))
    scale = _summary_scale(canvas_width)
    font_sizes = _summary_font_sizes(canvas_width)
    server_list = list(servers)
    if not server_list:
        return []
    total_pages = math.ceil(len(server_list) / normalized_page_size)
    pages: list[str] = []
    reference = _normalize_hourly_window([], hours, now_ts=now_ts)
    for page_index in range(total_pages):
        page_servers = server_list[
            page_index * normalized_page_size : (page_index + 1) * normalized_page_size
        ]
        page_height, card_bounds = _summary_layout(
            len(page_servers),
            width=canvas_width,
            height=height,
        )
        canvas = _prepare_canvas(background, (canvas_width, page_height))
        canvas = Image.alpha_composite(
            canvas,
            Image.new("RGBA", (canvas_width, page_height), (2, 7, 16, 92)),
        )
        overlay = Image.new("RGBA", (canvas_width, page_height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        outer_margin = _scaled(18, scale)
        overlay_draw.rounded_rectangle(
            (
                outer_margin,
                outer_margin,
                canvas_width - outer_margin,
                page_height - _scaled(10, scale),
            ),
            radius=_scaled(28, scale),
            fill=(3, 9, 20, 128),
            outline=(255, 255, 255, 92),
            width=_scaled(1, scale),
        )
        canvas = Image.alpha_composite(canvas, overlay)
        _paste_logo(
            canvas,
            (
                _scaled(34, scale),
                _scaled(27, scale),
                _scaled(92, scale),
                _scaled(85, scale),
            ),
            radius=_scaled(14, scale),
        )
        draw = ImageDraw.Draw(canvas)
        title_font = _load_font(font_sizes["title"])
        range_font = _load_font(font_sizes["subtitle"])
        page_font = _load_font(font_sizes["page"])

        page_text = f"{page_index + 1}/{total_pages}"
        page_width, page_height_text = _text_size(draw, page_text, page_font)
        pill_right = canvas_width - _scaled(30, scale)
        pill_top = _scaled(30, scale)
        pill = (
            pill_right - page_width - _scaled(24, scale),
            pill_top,
            pill_right,
            pill_top + page_height_text + _scaled(14, scale),
        )
        draw.rounded_rectangle(
            pill,
            radius=_scaled(11, scale),
            fill=(9, 18, 34, 205),
            outline=(255, 255, 255, 42),
            width=_scaled(1, scale),
        )
        draw.text(
            (pill[0] + _scaled(12, scale), pill[1] + _scaled(6, scale)),
            page_text,
            font=page_font,
            fill=MUTED,
        )

        title_x = _scaled(108, scale)
        title = _ellipsize(
            draw,
            "全服在线人数趋势汇总",
            title_font,
            pill[0] - title_x - _scaled(14, scale),
        )
        _draw_text_clamped(
            draw,
            (title_x, _scaled(27, scale)),
            title,
            font=title_font,
            fill=TEXT,
            bounds=(title_x, outer_margin, pill[0] - _scaled(8, scale), _scaled(64, scale)),
        )
        range_text = (
            f"{_format_hour(reference.timestamps[0])} — {_format_hour(reference.timestamps[-1])}"
            f" · 最近 {reference.stats.total} 小时 · 可达服务器 {len(server_list)} 台"
        )
        subtitle_bounds = (
            title_x,
            _scaled(60, scale),
            canvas_width - _scaled(38, scale),
            _scaled(98, scale),
        )
        _draw_text_clamped(
            draw,
            (title_x, _scaled(64, scale)),
            _ellipsize(
                draw,
                range_text,
                range_font,
                subtitle_bounds[2] - subtitle_bounds[0],
            ),
            font=range_font,
            fill=(196, 210, 229, 255),
            bounds=subtitle_bounds,
        )

        for bounds, server in zip(card_bounds, page_servers):
            trend = _normalize_hourly_window(server.history, hours, now_ts=now_ts)
            _draw_summary_server_card(
                draw,
                bounds,
                server,
                trend,
                scale=scale,
                font_sizes=font_sizes,
            )
        pages.append(_encode_png(canvas))
    return pages
