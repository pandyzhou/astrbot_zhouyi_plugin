from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import io
import random
import time
from pathlib import Path
from typing import Optional, Sequence
import warnings

import aiohttp
from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError


CANVAS_SIZE = (800, 440)
BACKGROUND_URLS = (
    "https://t.alcy.cc/pc",
    "https://t.alcy.cc/moe",
    "https://t.alcy.cc/fj",
    "https://t.alcy.cc/ys",
)
BACKGROUND_MAX_BYTES = 8 * 1024 * 1024
BACKGROUND_CACHE_TTL = 60.0
_ALLOWED_BACKGROUND_FORMATS = {"JPEG", "PNG", "WEBP"}
_BACKGROUND_TIMEOUT = aiohttp.ClientTimeout(total=6.0)

_background_cache: Optional[Image.Image] = None
_background_cache_at = 0.0
_background_lock: Optional[asyncio.Lock] = None
_background_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _reset_background_cache_for_tests() -> None:
    """重置模块缓存，供跨事件循环测试隔离使用。"""
    global _background_cache, _background_cache_at, _background_lock, _background_lock_loop
    _background_cache = None
    _background_cache_at = 0.0
    _background_lock = None
    _background_lock_loop = None


def _get_background_lock() -> asyncio.Lock:
    global _background_lock, _background_lock_loop
    loop = asyncio.get_running_loop()
    if _background_lock is None or _background_lock_loop is not loop:
        _background_lock = asyncio.Lock()
        _background_lock_loop = loop
    return _background_lock


async def load_font(font_size: int) -> ImageFont.ImageFont:
    font_paths = [
        Path(__file__).resolve().parent.parent / "resource" / "LXGWWenKai-Regular.ttf",
        Path(__file__).resolve().parent.parent / "resource" / "msyh.ttf",
        "msyh.ttf",
        "/usr/share/fonts/zh_CN/msyh.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(str(path), font_size)
        except OSError:
            continue
    try:
        return ImageFont.load_default().font_variant(size=font_size)
    except Exception:
        return ImageFont.load_default()


def _measure_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        box = draw.textbbox((0, 0), text, font=font)
        return float(max(0, box[2] - box[0]))


def _ellipsize_text(
    draw: ImageDraw.ImageDraw,
    text: object,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    value = str(text or "")
    if max_width <= 0:
        return ""
    if _measure_text(draw, value, font) <= max_width:
        return value
    suffix = "…"
    if _measure_text(draw, suffix, font) > max_width:
        return ""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if _measure_text(draw, value[:middle] + suffix, font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return value[:low] + suffix


def _wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: object,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    value = str(text or "")
    if not value or max_lines <= 0:
        return []
    lines: list[str] = []
    current = ""
    consumed = 0
    for index, char in enumerate(value):
        candidate = current + char
        if current and _measure_text(draw, candidate, font) > max_width:
            lines.append(current)
            current = char
            if len(lines) == max_lines:
                consumed = index
                break
        else:
            current = candidate
    else:
        consumed = len(value)
        if current:
            lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if consumed < len(value) and lines:
        remainder = lines[-1] + value[consumed:]
        lines[-1] = _ellipsize_text(draw, remainder, font, max_width)
    return lines


def _layout_player_capsules(
    draw: ImageDraw.ImageDraw,
    players: Sequence[object],
    font: ImageFont.ImageFont,
    bounds: tuple[int, int, int, int],
    *,
    total_online: Optional[int] = None,
    horizontal_gap: int = 8,
    vertical_gap: int = 7,
    padding_x: int = 12,
    height: int = 27,
) -> tuple[list[tuple[str, tuple[int, int, int, int], bool]], int]:
    """计算玩家胶囊，保证汇总胶囊和所有矩形都落在 bounds 内。"""
    left, top, right, bottom = bounds
    max_inner_width = max(1, right - left - padding_x * 2)
    normalized = [
        _ellipsize_text(draw, player, font, max_inner_width)
        for player in players
        if str(player or "").strip()
    ]
    online_count = len(normalized) if total_online is None else max(0, int(total_online))

    def place(texts: Sequence[tuple[str, bool]]):
        items: list[tuple[str, tuple[int, int, int, int], bool]] = []
        x, y = left, top
        for text, is_summary in texts:
            width = min(
                right - left,
                max(height, int(_measure_text(draw, text, font)) + padding_x * 2),
            )
            if x + width > right and x > left:
                x = left
                y += height + vertical_gap
            if y + height > bottom:
                return items, False
            items.append((text, (x, y, x + width, y + height), is_summary))
            x += width + horizontal_gap
        return items, True

    plain = [(name, False) for name in normalized]
    visible_count = len(normalized)
    while visible_count >= 0:
        hidden = max(online_count - visible_count, len(normalized) - visible_count, 0)
        candidate_items = plain[:visible_count]
        if hidden > 0:
            candidate_items.append((f"还有 {hidden} 位玩家", True))
        candidate, fit = place(candidate_items)
        if fit:
            return candidate, hidden
        visible_count -= 1
    return [], max(online_count, len(normalized), 0)


def _open_verified_image(
    data: bytes,
    *,
    allowed_formats: Optional[set[str]] = None,
    output_mode: str = "RGB",
) -> Image.Image:
    if not data:
        raise ValueError("图片数据为空")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                image_format = (probe.format or "").upper()
                if allowed_formats is not None and image_format not in allowed_formats:
                    raise ValueError(f"不支持的图片格式: {image_format or '未知'}")
                probe.verify()
            with Image.open(io.BytesIO(data)) as reopened:
                reopened.load()
                image = ImageOps.exif_transpose(reopened)
                return image.convert(output_mode)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("图片尺寸异常，疑似解压炸弹") from None
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("图片内容损坏或无法识别") from exc


def _decode_background_image(data: bytes) -> Image.Image:
    return _open_verified_image(
        data,
        allowed_formats=_ALLOWED_BACKGROUND_FORMATS,
        output_mode="RGB",
    )


def _cover_image(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    source = image.convert("RGB")
    return ImageOps.fit(source, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def _prepare_background(image: Image.Image, size: tuple[int, int] = CANVAS_SIZE) -> Image.Image:
    """横图 cover；竖图和方图使用模糊铺底并清晰 contain 居中。"""
    source = image.convert("RGB")
    if source.width > source.height:
        return _cover_image(source, size)

    backdrop = _cover_image(source, size).filter(ImageFilter.GaussianBlur(radius=18))
    shade = Image.new("RGBA", size, (18, 24, 36, 58))
    backdrop = Image.alpha_composite(backdrop.convert("RGBA"), shade).convert("RGB")
    foreground = ImageOps.contain(source, size, method=Image.Resampling.LANCZOS)
    x = (size[0] - foreground.width) // 2
    y = (size[1] - foreground.height) // 2
    backdrop.paste(foreground, (x, y))
    return backdrop


def _make_gradient_background(size: tuple[int, int] = CANVAS_SIZE) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(height):
        vertical = y / max(1, height - 1)
        for x in range(width):
            horizontal = x / max(1, width - 1)
            pixels[x, y] = (
                int(31 + 35 * horizontal),
                int(42 + 37 * (1 - vertical)),
                int(72 + 48 * vertical + 18 * horizontal),
            )
    return image


async def _download_background_bytes(
    session: aiohttp.ClientSession,
    url: str,
) -> bytes:
    async with session.get(url, allow_redirects=True) as response:
        if response.status < 200 or response.status >= 300:
            raise ValueError(f"背景接口返回 HTTP {response.status}")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise ValueError("背景 Content-Length 无效") from exc
            if declared_size < 0 or declared_size > BACKGROUND_MAX_BYTES:
                raise ValueError("背景 Content-Length 超过 8MB")

        chunks: list[bytes] = []
        received = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            received += len(chunk)
            if received > BACKGROUND_MAX_BYTES:
                raise ValueError("背景实际下载大小超过 8MB")
            chunks.append(chunk)
        if received == 0:
            raise ValueError("背景接口返回空内容")
        return b"".join(chunks)


async def _fetch_background() -> Image.Image:
    url = random.choice(BACKGROUND_URLS)
    async with aiohttp.ClientSession(timeout=_BACKGROUND_TIMEOUT) as session:
        data = await _download_background_bytes(session, url)
    return _prepare_background(_decode_background_image(data))


async def _get_cached_background() -> Image.Image:
    global _background_cache, _background_cache_at
    now = time.monotonic()
    if _background_cache is not None and now - _background_cache_at < BACKGROUND_CACHE_TTL:
        return _background_cache.copy()

    async with _get_background_lock():
        now = time.monotonic()
        if _background_cache is not None and now - _background_cache_at < BACKGROUND_CACHE_TTL:
            return _background_cache.copy()
        try:
            refreshed = await _fetch_background()
        except Exception as exc:
            logger.warning(f"刷新服务器卡片背景失败，使用本地回退: {exc}")
            if _background_cache is not None:
                return _background_cache.copy()
            return _make_gradient_background()
        _background_cache = refreshed.convert("RGB").resize(CANVAS_SIZE)
        _background_cache_at = time.monotonic()
        return _background_cache.copy()


async def fetch_icon(icon_base64: Optional[str] = None) -> Optional[Image.Image]:
    """安全解码服务器图标，缺失或损坏时回退到本地默认图标。"""
    if icon_base64:
        try:
            encoded = icon_base64.split(",", 1)[1] if "," in icon_base64 else icon_base64
            encoded = "".join(encoded.split())
            icon_data = base64.b64decode(encoded, validate=True)
            return _open_verified_image(icon_data, output_mode="RGBA")
        except Exception as exc:
            logger.warning(f"Base64 图标解码失败，使用默认图标: {exc}")

    try:
        default_path = Path(__file__).resolve().parent.parent / "logo.png"
        return _open_verified_image(default_path.read_bytes(), output_mode="RGBA")
    except Exception as exc:
        logger.warning(f"默认服务器图标读取失败: {exc}")
        return None


def _rounded_panel(
    layer: Image.Image,
    bounds: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int, int],
    outline: Optional[tuple[int, int, int, int]] = None,
    width: int = 1,
) -> None:
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=outline, width=width)


def _paste_icon(canvas: Image.Image, icon: Optional[Image.Image]) -> None:
    icon_bounds = (690, 38, 756, 104)
    size = (icon_bounds[2] - icon_bounds[0], icon_bounds[3] - icon_bounds[1])
    if icon is None:
        return

    fitted = ImageOps.contain(icon.convert("RGBA"), size, method=Image.Resampling.LANCZOS)
    plate = Image.new("RGBA", size, (245, 248, 255, 235))
    plate.alpha_composite(
        fitted,
        ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2),
    )
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=16, fill=255)
    framed = Image.new("RGBA", size, (0, 0, 0, 0))
    framed.paste(plate, (0, 0), mask)
    ImageDraw.Draw(framed).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1),
        radius=16,
        outline=(255, 255, 255, 220),
        width=2,
    )
    canvas.alpha_composite(framed, (icon_bounds[0], icon_bounds[1]))


async def generate_server_info_image(
    players_list: list,
    latency: Optional[int],
    server_name: str,
    plays_max: int,
    plays_online: int,
    server_version: str,
    icon_base64: Optional[str] = None,
    host_address: Optional[str] = None,
    *,
    is_online: bool = True,
    generated_at: Optional[datetime] = None,
) -> str:
    """生成固定 800×440 的 D「相框贴纸卡」，返回 Base64 PNG。"""
    try:
        background = await _get_cached_background()
    except Exception as exc:
        logger.warning(f"读取卡片背景失败，使用本地渐变: {exc}")
        background = _make_gradient_background()

    canvas = background.convert("RGBA")
    overlay = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    _rounded_panel(
        overlay,
        (12, 12, 788, 428),
        radius=25,
        fill=(7, 13, 25, 52),
        outline=(255, 255, 255, 185),
        width=2,
    )
    _rounded_panel(overlay, (24, 22, 776, 148), radius=20, fill=(12, 20, 35, 190))
    _rounded_panel(overlay, (24, 164, 776, 414), radius=20, fill=(12, 20, 35, 204))
    canvas = Image.alpha_composite(canvas, overlay)

    draw = ImageDraw.Draw(canvas)
    title_font = await load_font(28)
    address_font = await load_font(18)
    label_font = await load_font(16)
    value_font = await load_font(23)
    capsule_font = await load_font(16)
    time_font = await load_font(14)

    title_color = (250, 252, 255, 255)
    soft_color = (216, 226, 241, 255)
    muted_color = (174, 190, 211, 255)
    online_color = (119, 238, 177, 255)
    offline_color = (255, 145, 145, 255)
    accent = online_color if is_online else offline_color

    title_lines = _wrap_text_lines(draw, server_name, title_font, 620, 2) or ["未命名服务器"]
    title_y = 38 if len(title_lines) == 1 else 31
    for line in title_lines:
        draw.text((42, title_y), line, font=title_font, fill=title_color)
        title_y += 34

    address = _ellipsize_text(draw, host_address or "地址未知", address_font, 620)
    draw.text((42, 116), address, font=address_font, fill=soft_color)
    _paste_icon(canvas, await fetch_icon(icon_base64))

    columns = (
        (42, 251, "在线", f"{max(0, int(plays_online or 0))}/{max(0, int(plays_max or 0))}" if is_online else "离线"),
        (292, 501, "延迟", f"{int(latency)} ms" if is_online and latency is not None else "--"),
        (542, 758, "版本", str(server_version or "未知")),
    )
    for left, right, label, value in columns:
        draw.text((left, 183), label, font=label_font, fill=muted_color)
        value_text = _ellipsize_text(draw, value, value_font, right - left)
        draw.text((left, 207), value_text, font=value_font, fill=accent if label == "在线" else title_color)

    player_label = "在线玩家" if is_online else "玩家列表"
    draw.text((42, 258), player_label, font=label_font, fill=muted_color)
    player_bounds = (42, 286, 758, 354)
    capsule_players = (players_list or []) if is_online else []
    capsules, _ = _layout_player_capsules(
        draw,
        capsule_players,
        capsule_font,
        player_bounds,
        total_online=max(0, int(plays_online or 0)) if is_online else 0,
    )
    if capsules:
        for text, bounds, is_summary in capsules:
            fill = (73, 112, 158, 225) if not is_summary else (134, 94, 162, 230)
            draw.rounded_rectangle(bounds, radius=13, fill=fill, outline=(255, 255, 255, 70), width=1)
            text_box = draw.textbbox((0, 0), text, font=capsule_font)
            text_height = text_box[3] - text_box[1]
            text_y = bounds[1] + (bounds[3] - bounds[1] - text_height) // 2 - text_box[1]
            draw.text((bounds[0] + 12, text_y), text, font=capsule_font, fill=title_color)
    else:
        empty_text = "暂无玩家在线" if is_online else "服务器当前不可达"
        draw.text((42, 300), empty_text, font=capsule_font, fill=soft_color)

    status_text = "在线" if is_online else "离线"
    status_width = int(_measure_text(draw, status_text, label_font)) + 24
    status_bounds = (42, 372, 42 + status_width, 400)
    draw.rounded_rectangle(status_bounds, radius=14, fill=accent)
    status_box = draw.textbbox((0, 0), status_text, font=label_font)
    status_y = status_bounds[1] + (28 - (status_box[3] - status_box[1])) // 2 - status_box[1]
    draw.text((status_bounds[0] + 12, status_y), status_text, font=label_font, fill=(18, 30, 40, 255))

    timestamp = generated_at or datetime.now()
    query_text = f"刚刚查询 · {timestamp.strftime('%H:%M:%S')}"
    query_text = _ellipsize_text(draw, query_text, time_font, 300)
    query_width = _measure_text(draw, query_text, time_font)
    draw.text((758 - query_width, 383), query_text, font=time_font, fill=muted_color)

    output = canvas.convert("RGB")
    buffer = io.BytesIO()
    output.save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
