from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw, ImageFont, ImageOps, features

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.script import get_img


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def encode_image(image: Image.Image, image_format: str) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def decode_card(value: str) -> Image.Image:
    with Image.open(io.BytesIO(base64.b64decode(value))) as image:
        image.load()
        return image.copy()


class GetImgPureFunctionTests(unittest.TestCase):
    def setUp(self):
        self.measure_image = Image.new("RGB", (800, 440), "white")
        self.draw = ImageDraw.Draw(self.measure_image)
        self.font = ImageFont.truetype(
            str(Path(get_img.__file__).resolve().parents[1] / "resource" / "LXGWWenKai-Regular.ttf"),
            16,
        )

    def test_long_text_helpers_and_player_layout_stay_bounded(self):
        title = "这是一个非常长的服务器标题" * 20
        lines = get_img._wrap_text_lines(self.draw, title, self.font, 180, 2)
        self.assertLessEqual(len(lines), 2)
        self.assertTrue(all(get_img._measure_text(self.draw, line, self.font) <= 180 for line in lines))

        bounds = (42, 286, 758, 354)
        players = [f"玩家-{index}-" + "超长名字" * 12 for index in range(40)]
        capsules, hidden = get_img._layout_player_capsules(
            self.draw, players, self.font, bounds
        )
        self.assertGreater(hidden, 0)
        self.assertTrue(any(is_summary for _, _, is_summary in capsules))
        for _, (left, top, right, bottom), _ in capsules:
            self.assertGreaterEqual(left, bounds[0])
            self.assertGreaterEqual(top, bounds[1])
            self.assertLessEqual(right, bounds[2])
            self.assertLessEqual(bottom, bounds[3])

    def test_player_layout_combines_total_online_and_sample_count(self):
        bounds = (42, 286, 758, 354)
        capsules, hidden = get_img._layout_player_capsules(
            self.draw,
            ["Alice", "Bob", "Carol"],
            self.font,
            bounds,
            total_online=20,
        )
        self.assertEqual(hidden, 17)
        self.assertEqual([text for text, _, summary in capsules if summary], ["还有 17 位玩家"])
        self.assertEqual(sum(not summary for _, _, summary in capsules), 3)

    def test_player_layout_reserves_summary_space_and_handles_empty_sample(self):
        narrow_bounds = (0, 0, 160, 27)
        capsules, hidden = get_img._layout_player_capsules(
            self.draw,
            ["Alice", "Bob"],
            self.font,
            narrow_bounds,
            total_online=8,
        )
        visible = sum(not summary for _, _, summary in capsules)
        self.assertLess(visible, 2)
        self.assertEqual(hidden, max(8 - visible, 2 - visible, 0))
        self.assertTrue(any(summary for _, _, summary in capsules))

        capsules, hidden = get_img._layout_player_capsules(
            self.draw,
            [],
            self.font,
            (0, 0, 300, 27),
            total_online=5,
        )
        self.assertEqual(hidden, 5)
        self.assertEqual(capsules[0][0], "还有 5 位玩家")
        self.assertTrue(capsules[0][2])

        capsules, hidden = get_img._layout_player_capsules(
            self.draw,
            [],
            self.font,
            (0, 0, 300, 27),
            total_online=0,
        )
        self.assertEqual(capsules, [])
        self.assertEqual(hidden, 0)

    def test_background_cache_ttl_is_short(self):
        self.assertEqual(get_img.BACKGROUND_CACHE_TTL, 60.0)

    def test_horizontal_background_uses_cover(self):
        source = Image.new("RGB", (1000, 300), "red")
        ImageDraw.Draw(source).rectangle((400, 0, 599, 299), fill="blue")
        actual = get_img._prepare_background(source)
        expected = ImageOps.fit(
            source,
            get_img.CANVAS_SIZE,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        self.assertEqual(actual.size, get_img.CANVAS_SIZE)
        self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_vertical_and_square_backgrounds_use_blurred_fill_and_clear_center(self):
        for size in ((120, 360), (240, 240)):
            with self.subTest(size=size):
                source = Image.new("RGB", size, (240, 40, 40))
                ImageDraw.Draw(source).rectangle(
                    (0, size[1] // 3, size[0] - 1, size[1] * 2 // 3),
                    fill=(30, 210, 80),
                )
                actual = get_img._prepare_background(source)
                foreground = ImageOps.contain(
                    source,
                    get_img.CANVAS_SIZE,
                    method=Image.Resampling.LANCZOS,
                )
                x = (800 - foreground.width) // 2
                y = (440 - foreground.height) // 2
                center = actual.crop((x, y, x + foreground.width, y + foreground.height))
                self.assertEqual(center.tobytes(), foreground.tobytes())
                if foreground.width < 800:
                    self.assertNotEqual(actual.getpixel((5, 220)), foreground.getpixel((0, foreground.height // 2)))

    def test_jpeg_png_webp_are_recognized(self):
        image = Image.new("RGB", (32, 24), (10, 20, 30))
        for image_format in ("JPEG", "PNG"):
            with self.subTest(image_format=image_format):
                decoded = get_img._decode_background_image(encode_image(image, image_format))
                self.assertEqual(decoded.mode, "RGB")
                self.assertEqual(decoded.size, image.size)

        if not features.check("webp"):
            self.skipTest("当前 Pillow 不支持 WebP")
        decoded = get_img._decode_background_image(encode_image(image, "WEBP"))
        self.assertEqual(decoded.mode, "RGB")
        self.assertEqual(decoded.size, image.size)

    def test_other_format_and_bad_bytes_are_rejected(self):
        gif_data = encode_image(Image.new("RGB", (10, 10), "red"), "GIF")
        with self.assertRaises(ValueError):
            get_img._decode_background_image(gif_data)
        with self.assertRaises(ValueError):
            get_img._decode_background_image(b"not-an-image")


class GetImgAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        get_img._reset_background_cache_for_tests()

    async def asyncTearDown(self):
        get_img._reset_background_cache_for_tests()

    async def test_missing_and_broken_icon_fall_back_to_default(self):
        default_path = Path(get_img.__file__).resolve().parents[1] / "resource" / "default_icon.png"
        with Image.open(default_path) as expected:
            expected_rgba = expected.convert("RGBA")
            expected_bytes = expected_rgba.tobytes()
            expected_size = expected_rgba.size

        for icon_base64 in (None, "broken-base64"):
            with self.subTest(icon_base64=icon_base64):
                icon = await get_img.fetch_icon(icon_base64)
                self.assertIsNotNone(icon)
                self.assertEqual(icon.mode, "RGBA")
                self.assertEqual(icon.size, expected_size)
                self.assertEqual(icon.tobytes(), expected_bytes)

    async def test_online_and_offline_cards_are_800x440_png(self):
        background = get_img._make_gradient_background()
        with patch.object(get_img, "_get_cached_background", AsyncMock(return_value=background)):
            online = await get_img.generate_server_info_image(
                ["Alice", "Bob"],
                42,
                "在线服务器",
                20,
                2,
                "1.21.4",
                host_address="example.org:25565",
                is_online=True,
                generated_at=datetime(2025, 1, 2, 3, 4, 5),
            )
            offline = await get_img.generate_server_info_image(
                [],
                None,
                "离线服务器",
                0,
                0,
                "未知",
                icon_base64="broken-base64",
                host_address="offline.example:25565",
                is_online=False,
            )
        for value in (online, offline):
            image = decode_card(value)
            self.assertEqual(image.format, None)
            self.assertEqual(image.size, (800, 440))
            self.assertEqual(image.mode, "RGB")
            self.assertTrue(base64.b64decode(value).startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_long_card_content_and_non_square_palette_icon_do_not_fail(self):
        icon = Image.new("P", (120, 36))
        icon.putpalette([0, 0, 0, 255, 90, 20] + [0, 0, 0] * 254)
        icon_data = base64.b64encode(encode_image(icon, "PNG")).decode("ascii")
        with patch.object(
            get_img,
            "_get_cached_background",
            AsyncMock(return_value=get_img._make_gradient_background()),
        ):
            value = await get_img.generate_server_info_image(
                ["玩家" + str(index) + "名字" * 30 for index in range(80)],
                9999,
                "超长标题" * 80,
                999999,
                888888,
                "超长版本字符串" * 50,
                icon_data,
                "very-long-host." * 40 + ":25565",
                is_online=True,
            )
        self.assertEqual(decode_card(value).size, (800, 440))

    async def test_fresh_cache_does_not_download(self):
        get_img._background_cache = Image.new("RGB", (800, 440), "red")
        get_img._background_cache_at = 100.0
        with (
            patch.object(get_img.time, "monotonic", return_value=101.0),
            patch.object(get_img, "_fetch_background", AsyncMock()) as fetch,
        ):
            result = await get_img._get_cached_background()
        fetch.assert_not_awaited()
        self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))
        self.assertIsNot(result, get_img._background_cache)

    async def test_concurrent_refresh_downloads_once(self):
        calls = 0

        async def fake_fetch():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return Image.new("RGB", (800, 440), "blue")

        with patch.object(get_img, "_fetch_background", side_effect=fake_fetch):
            results = await asyncio.gather(
                *(get_img._get_cached_background() for _ in range(8))
            )
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(image.getpixel((0, 0)) == (0, 0, 255) for image in results))

    async def test_refresh_failure_uses_stale_cache_or_gradient(self):
        get_img._background_cache = Image.new("RGB", (800, 440), "green")
        get_img._background_cache_at = 0.0
        with (
            patch.object(get_img.time, "monotonic", return_value=1000.0),
            patch.object(get_img, "_fetch_background", AsyncMock(side_effect=RuntimeError("down"))),
        ):
            stale = await get_img._get_cached_background()
        self.assertEqual(stale.getpixel((0, 0)), (0, 128, 0))

        get_img._reset_background_cache_for_tests()
        with patch.object(
            get_img, "_fetch_background", AsyncMock(side_effect=RuntimeError("down"))
        ):
            fallback = await get_img._get_cached_background()
        self.assertEqual(fallback.size, (800, 440))
        self.assertEqual(fallback.mode, "RGB")

    async def test_download_rejects_non_2xx_and_oversized_content(self):
        non_2xx = FakeSession(FakeResponse(status=503))
        with self.assertRaises(ValueError):
            await get_img._download_background_bytes(non_2xx, "https://example.invalid")

        declared = FakeSession(
            FakeResponse(
                headers={"Content-Length": str(get_img.BACKGROUND_MAX_BYTES + 1)}
            )
        )
        with self.assertRaises(ValueError):
            await get_img._download_background_bytes(declared, "https://example.invalid")

        accumulated = FakeSession(
            FakeResponse(
                chunks=(b"x" * get_img.BACKGROUND_MAX_BYTES, b"y"),
            )
        )
        with self.assertRaises(ValueError):
            await get_img._download_background_bytes(accumulated, "https://example.invalid")

    async def test_download_accepts_in_memory_response(self):
        session = FakeSession(
            FakeResponse(
                status=200,
                headers={"Content-Length": "6"},
                chunks=(b"abc", b"def"),
            )
        )
        data = await get_img._download_background_bytes(session, "https://example.invalid")
        self.assertEqual(data, b"abcdef")
        self.assertEqual(len(session.calls), 1)

    async def test_download_accepts_content_length_mismatch_within_limit(self):
        session = FakeSession(
            FakeResponse(
                status=200,
                headers={"Content-Length": "3"},
                chunks=(b"abc", b"def"),
            )
        )
        data = await get_img._download_background_bytes(session, "https://example.invalid")
        self.assertEqual(data, b"abcdef")


if __name__ == "__main__":
    unittest.main()
