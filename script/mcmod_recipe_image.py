from __future__ import annotations

import asyncio
import base64
import io
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFont


_LOGICAL_IMAGE_SIZE = (245, 127)
_IMAGE_SCALE = 3
RECIPE_IMAGE_SIZE = tuple(dimension * _IMAGE_SCALE for dimension in _LOGICAL_IMAGE_SIZE)
_ICON_BODY_LIMIT = 512 * 1024
_ICON_DIMENSION_LIMIT = 256
_ICON_CACHE_LIMIT = 128
_ICON_CACHE_TTL_SECONDS = 6 * 60 * 60
_ICON_FAILURE_TTL_SECONDS = 5 * 60
_ICON_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
_ICON_PATH_PATTERN = re.compile(
    r"^/item/icon/(?:32x32|128x128)/"
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*$"
)
_ICON_CACHE: OrderedDict[str, tuple[float, Image.Image | None]] = OrderedDict()
_ICON_CACHE_LOCK = threading.Lock()
_FONT_PATH = Path(__file__).resolve().parents[1] / "resource" / "LXGWWenKai-Regular.ttf"
IconLoader = Callable[[str], Awaitable[Image.Image | None]]


def normalize_icon_url(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != "i.mcmod.cn":
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None or parsed.username is not None or parsed.password is not None:
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None
    if not _ICON_PATH_PATTERN.fullmatch(parsed.path):
        return None
    suffix = parsed.path.split("/", 4)[4]
    if any(segment in {".", ".."} for segment in suffix.split("/")):
        return None
    return f"https://i.mcmod.cn/item/icon/128x128/{suffix}"


def clear_icon_cache() -> None:
    with _ICON_CACHE_LOCK:
        _ICON_CACHE.clear()


def _cache_get(url: str) -> tuple[bool, Image.Image | None]:
    now = time.monotonic()
    with _ICON_CACHE_LOCK:
        cached = _ICON_CACHE.get(url)
        if cached is None:
            return False, None
        expires_at, image = cached
        if expires_at <= now:
            _ICON_CACHE.pop(url, None)
            return False, None
        _ICON_CACHE.move_to_end(url)
        return True, image.copy() if image is not None else None


def _cache_put(url: str, image: Image.Image | None) -> None:
    ttl = _ICON_CACHE_TTL_SECONDS if image is not None else _ICON_FAILURE_TTL_SECONDS
    cached_image = image.copy() if image is not None else None
    with _ICON_CACHE_LOCK:
        _ICON_CACHE[url] = (time.monotonic() + ttl, cached_image)
        _ICON_CACHE.move_to_end(url)
        while len(_ICON_CACHE) > _ICON_CACHE_LIMIT:
            _ICON_CACHE.popitem(last=False)


def _icon_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)


def _decode_icon(data: bytes, content_type: str) -> Image.Image | None:
    if content_type not in _ICON_CONTENT_TYPES or not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in {"PNG", "JPEG", "WEBP"}:
                return None
            width, height = probe.size
            if not (0 < width <= _ICON_DIMENSION_LIMIT and 0 < height <= _ICON_DIMENSION_LIMIT):
                return None
            probe.verify()
        with Image.open(io.BytesIO(data)) as decoded:
            decoded.load()
            if decoded.size[0] > _ICON_DIMENSION_LIMIT or decoded.size[1] > _ICON_DIMENSION_LIMIT:
                return None
            return decoded.convert("RGBA")
    except Exception:
        return None


async def _download_icon_uncached(url: str) -> Image.Image | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (AstrBot MC百科配方图)",
        "Accept": "image/png,image/jpeg,image/webp",
    }
    async with aiohttp.ClientSession(
        timeout=_icon_timeout(),
        headers=headers,
        trust_env=True,
    ) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                return None
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in _ICON_CONTENT_TYPES:
                return None
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > _ICON_BODY_LIMIT:
                        return None
                except ValueError:
                    return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(16 * 1024):
                body.extend(chunk)
                if len(body) > _ICON_BODY_LIMIT:
                    return None
    return _decode_icon(bytes(body), content_type)


async def load_icon(url: str) -> Image.Image | None:
    normalized = normalize_icon_url(url)
    if normalized is None:
        return None
    hit, image = _cache_get(normalized)
    if hit:
        return image
    try:
        image = await _download_icon_uncached(normalized)
    except Exception:
        image = None
    _cache_put(normalized, image)
    return image.copy() if image is not None else None


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(_FONT_PATH), size=size)
    except OSError:
        return ImageFont.load_default()


def _draw_slot(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    draw.rectangle(box, fill=(139, 139, 139))
    draw.line((x1, y1, x2 - 1, y1), fill=(55, 55, 55))
    draw.line((x1, y1, x1, y2 - 1), fill=(55, 55, 55))
    draw.line((x1 + 1, y1 + 1, x2 - 2, y1 + 1), fill=(85, 85, 85))
    draw.line((x1 + 1, y1 + 1, x1 + 1, y2 - 2), fill=(85, 85, 85))
    draw.line((x1 + 1, y2 - 1, x2 - 1, y2 - 1), fill=(198, 198, 198))
    draw.line((x2 - 1, y1 + 1, x2 - 1, y2 - 1), fill=(198, 198, 198))
    draw.line((x1, y2, x2, y2), fill=(255, 255, 255))
    draw.line((x2, y1, x2, y2), fill=(255, 255, 255))


def _draw_placeholder(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    name: str,
) -> None:
    x1, y1, x2, y2 = box
    size = min(x2 - x1 + 1, y2 - y1 + 1)
    padding = max(3, size // 5)
    inner = (x1 + padding, y1 + padding, x2 - padding, y2 - padding)
    draw.rectangle(inner, fill=(90, 98, 104), outline=(50, 54, 58))
    label = (name.strip() or "?")[:1]
    font = _load_font(max(7, size // 3))
    bounds = draw.textbbox((0, 0), label, font=font)
    tw, th = bounds[2] - bounds[0], bounds[3] - bounds[1]
    draw.text(
        ((inner[0] + inner[2] - tw) // 2, (inner[1] + inner[3] - th) // 2 - 1),
        label,
        font=font,
        fill=(235, 235, 235),
    )


def _physical_box(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return (
        x1 * _IMAGE_SCALE,
        y1 * _IMAGE_SCALE,
        (x2 + 1) * _IMAGE_SCALE - 1,
        (y2 + 1) * _IMAGE_SCALE - 1,
    )


def _paste_placeholder(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    name: str,
) -> None:
    layer = Image.new("RGBA", _LOGICAL_IMAGE_SIZE, (0, 0, 0, 0))
    _draw_placeholder(layer, ImageDraw.Draw(layer), box, name)
    canvas.alpha_composite(layer.resize(RECIPE_IMAGE_SIZE, Image.Resampling.NEAREST))


def _paste_icon(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    icon: Image.Image | None,
    name: str,
    max_size: int,
) -> None:
    if icon is None:
        _paste_placeholder(canvas, box, name)
        return
    x1, y1, x2, y2 = _physical_box(box)
    working = icon.convert("RGBA")
    working.thumbnail((max_size, max_size), Image.Resampling.NEAREST)
    x = x1 + (x2 - x1 + 1 - working.width) // 2
    y = y1 + (y2 - y1 + 1 - working.height) // 2
    canvas.alpha_composite(working, (x, y))


def _is_renderable_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    recipe = payload.get("recipe")
    if not isinstance(recipe, dict) or recipe.get("availability") == "removed":
        return False
    method = recipe.get("method")
    if not isinstance(method, str) or "工作台" not in method:
        return False
    output = recipe.get("output")
    if not isinstance(output, dict) or not output.get("name"):
        return False
    grid = recipe.get("grid_slots")
    if not isinstance(grid, list) or len(grid) != 3:
        return False
    if any(not isinstance(row, list) or len(row) != 3 for row in grid):
        return False
    return any(isinstance(slot, dict) and slot.get("name") for row in grid for slot in row)


async def _load_unique_icons(payload: dict, icon_loader: IconLoader) -> dict[str, Image.Image | None]:
    recipe = payload["recipe"]
    urls: list[str] = []
    for row in recipe["grid_slots"]:
        for slot in row:
            if isinstance(slot, dict):
                normalized = normalize_icon_url(slot.get("icon_url"))
                if normalized and normalized not in urls:
                    urls.append(normalized)
    output = recipe.get("output")
    if isinstance(output, dict):
        normalized = normalize_icon_url(output.get("icon_url"))
        if normalized and normalized not in urls:
            urls.append(normalized)

    semaphore = asyncio.Semaphore(4)

    async def guarded(url: str) -> tuple[str, Image.Image | None]:
        try:
            async with semaphore:
                loaded = await icon_loader(url)
            return url, loaded.copy() if loaded is not None else None
        except Exception:
            return url, None

    return dict(await asyncio.gather(*(guarded(url) for url in urls)))


def _render(payload: dict, icons: dict[str, Image.Image | None]) -> str:
    width, height = _LOGICAL_IMAGE_SIZE
    logical_canvas = Image.new("RGBA", _LOGICAL_IMAGE_SIZE, (198, 198, 198, 255))
    draw = ImageDraw.Draw(logical_canvas)

    draw.line((0, 0, width - 2, 0), fill=(255, 255, 255))
    draw.line((0, 0, 0, height - 2), fill=(255, 255, 255))
    draw.line((1, 1, width - 3, 1), fill=(219, 219, 219))
    draw.line((1, 1, 1, height - 3), fill=(219, 219, 219))
    draw.line((1, height - 2, width - 2, height - 2), fill=(139, 139, 139))
    draw.line((width - 2, 1, width - 2, height - 2), fill=(139, 139, 139))
    draw.line((0, height - 1, width - 1, height - 1), fill=(85, 85, 85))
    draw.line((width - 1, 0, width - 1, height - 1), fill=(85, 85, 85))

    recipe = payload["recipe"]
    slot_size = 36
    grid_x, grid_y = 4, 4
    input_icons: list[tuple[tuple[int, int, int, int], str | None, str]] = []
    for row_index, row in enumerate(recipe["grid_slots"]):
        for column_index, slot in enumerate(row):
            x1 = grid_x + column_index * slot_size
            y1 = grid_y + row_index * slot_size
            box = (x1, y1, x1 + slot_size - 1, y1 + slot_size - 1)
            _draw_slot(draw, box)
            if isinstance(slot, dict) and slot.get("name"):
                input_icons.append(
                    (box, normalize_icon_url(slot.get("icon_url")), str(slot["name"]))
                )

    arrow = [(127, 57), (149, 57), (149, 51), (168, 63), (149, 75), (149, 69), (127, 69)]
    draw.polygon(arrow, fill=(139, 139, 139))

    output_box = (184, 38, 233, 87)
    _draw_slot(draw, output_box)
    output = recipe["output"]
    output_url = normalize_icon_url(output.get("icon_url"))

    rendered = logical_canvas.resize(RECIPE_IMAGE_SIZE, Image.Resampling.NEAREST)
    for box, icon_url, name in input_icons:
        _paste_icon(
            rendered,
            box,
            icons.get(icon_url) if icon_url else None,
            name,
            96,
        )
    _paste_icon(
        rendered,
        output_box,
        icons.get(output_url) if output_url else None,
        str(output["name"]),
        128,
    )

    count = output.get("count", 1)
    if isinstance(count, int) and count > 1:
        count_layer = Image.new("RGBA", _LOGICAL_IMAGE_SIZE, (0, 0, 0, 0))
        count_draw = ImageDraw.Draw(count_layer)
        count_text = str(count)
        count_font = _load_font(9)
        text_box = count_draw.textbbox((0, 0), count_text, font=count_font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        tx = output_box[2] - text_width - 2
        ty = output_box[3] - text_height - 1
        count_draw.text((tx + 1, ty + 1), count_text, font=count_font, fill=(63, 63, 63))
        count_draw.text((tx, ty), count_text, font=count_font, fill=(255, 255, 255))
        rendered.alpha_composite(
            count_layer.resize(RECIPE_IMAGE_SIZE, Image.Resampling.NEAREST)
        )

    output_buffer = io.BytesIO()
    rendered.convert("RGB").save(output_buffer, format="PNG", optimize=True)
    return base64.b64encode(output_buffer.getvalue()).decode("ascii")


async def render_recipe_image_base64(
    payload: dict,
    *,
    icon_loader: IconLoader | None = None,
) -> str | None:
    if not _is_renderable_payload(payload):
        return None
    loader = icon_loader or load_icon
    icons = await _load_unique_icons(payload, loader)
    return _render(payload, icons)
