from __future__ import annotations

import base64
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.script import mcmod_recipe_image


ICON_A = "https://i.mcmod.cn/item/icon/32x32/0/40.png"
ICON_B = "https://i.mcmod.cn/item/icon/32x32/19/196521.png"
ICON_OUT = "https://i.mcmod.cn/item/icon/32x32/19/196531.png"
ICON_A_128 = "https://i.mcmod.cn/item/icon/128x128/0/40.png"
ICON_B_128 = "https://i.mcmod.cn/item/icon/128x128/19/196521.png"
ICON_OUT_128 = "https://i.mcmod.cn/item/icon/128x128/19/196531.png"


def sample_payload() -> dict:
    plank = {
        "name": "任意木板",
        "source": "https://www.mcmod.cn/oredict/minecraft:planks-1.html",
        "icon_url": ICON_A,
    }
    shaft = {
        "name": "传动杆",
        "source": "https://www.mcmod.cn/item/196521.html",
        "icon_url": ICON_B,
    }
    return {
        "title": "水车",
        "source_url": "https://www.mcmod.cn/item/196531.html",
        "recipe": {
            "method": "工作台",
            "materials": [
                plank | {"count": 8},
                shaft | {"count": 1},
            ],
            "grid_slots": [
                [plank, plank, plank],
                [plank, shaft, plank],
                [plank, plank, plank],
            ],
            "output": {
                "name": "水车",
                "count": 2,
                "source": "https://www.mcmod.cn/item/196531.html",
                "icon_url": ICON_OUT,
            },
            "conditions": ["需要 v0.5.1 或更高版本"],
            "availability": "active",
            "required_mods": [],
        },
    }


class RecipeImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_stable_png_size_and_format(self):
        async def icon_loader(url: str):
            color = (120, 80, 40, 255) if url == ICON_A else (80, 120, 160, 255)
            return Image.new("RGBA", (32, 32), color)

        encoded = await mcmod_recipe_image.render_recipe_image_base64(
            sample_payload(),
            icon_loader=icon_loader,
        )

        self.assertIsInstance(encoded, str)
        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (735, 381))
        self.assertEqual(image.size, mcmod_recipe_image.RECIPE_IMAGE_SIZE)
        self.assertGreater(len(image.convert("RGB").getcolors(maxcolors=100000) or []), 10)

    async def test_loads_128px_urls_and_composites_high_resolution_icon_on_final_canvas(self):
        calls = []
        icon = Image.new("RGBA", (128, 128), (0, 0, 255, 255))
        for x in range(128):
            color = (255, 0, 0, 255) if x % 2 == 0 else (0, 255, 0, 255)
            for y in range(128):
                icon.putpixel((x, y), color)

        async def icon_loader(url: str):
            calls.append(url)
            return icon.copy()

        encoded = await mcmod_recipe_image.render_recipe_image_base64(
            sample_payload(),
            icon_loader=icon_loader,
        )
        image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
        input_icon = image.crop((18, 18, 114, 114))
        middle_row = [input_icon.getpixel((x, input_icon.height // 2)) for x in range(input_icon.width)]
        color_changes = sum(left != right for left, right in zip(middle_row, middle_row[1:]))

        self.assertEqual(calls, [ICON_A_128, ICON_B_128, ICON_OUT_128])
        self.assertGreater(color_changes, 50)
        self.assertEqual(set(middle_row), {(255, 0, 0), (0, 255, 0)})

    async def test_output_count_is_drawn_after_high_resolution_icon(self):
        async def icon_loader(url: str):
            return Image.new("RGBA", (128, 128), (0, 0, 0, 255))

        with_count = sample_payload()
        without_count = sample_payload()
        without_count["recipe"]["output"]["count"] = 1
        counted = await mcmod_recipe_image.render_recipe_image_base64(
            with_count,
            icon_loader=icon_loader,
        )
        uncounted = await mcmod_recipe_image.render_recipe_image_base64(
            without_count,
            icon_loader=icon_loader,
        )
        counted_image = Image.open(io.BytesIO(base64.b64decode(counted))).convert("RGB")
        uncounted_image = Image.open(io.BytesIO(base64.b64decode(uncounted))).convert("RGB")
        count_region = (675, 238, 700, 253)

        self.assertNotEqual(
            counted_image.crop(count_region).tobytes(),
            uncounted_image.crop(count_region).tobytes(),
        )
        self.assertTrue(
            any(
                counted_image.getpixel((x, y)) != (0, 0, 0)
                and uncounted_image.getpixel((x, y)) == (0, 0, 0)
                for x in range(count_region[0], count_region[2])
                for y in range(count_region[1], count_region[3])
            )
        )

    async def test_jei_window_and_slot_pixels_follow_minecraft_bevels(self):
        payload = sample_payload()
        payload["recipe"]["grid_slots"][0][0] = None

        async def icon_loader(url: str):
            return Image.new("RGBA", (32, 32), (120, 80, 40, 255))

        encoded = await mcmod_recipe_image.render_recipe_image_base64(
            payload,
            icon_loader=icon_loader,
        )
        image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")

        logical_width, logical_height = mcmod_recipe_image._LOGICAL_IMAGE_SIZE
        scale = image.width // logical_width
        pixel = lambda x, y: image.getpixel((x * scale, y * scale))
        self.assertEqual(pixel(10, 0), (255, 255, 255))
        self.assertEqual(pixel(0, 10), (255, 255, 255))
        self.assertEqual(pixel(10, logical_height - 1), (85, 85, 85))
        self.assertEqual(pixel(logical_width - 1, 10), (85, 85, 85))
        # 参考 JEI 紧凑布局：九宫格几乎贴左上，输出槽靠近右边缘。
        self.assertEqual(pixel(4, 4), (55, 55, 55))
        self.assertEqual(pixel(7, 7), (139, 139, 139))
        self.assertEqual(pixel(39, 39), (255, 255, 255))
        self.assertEqual(pixel(184, 38), (55, 55, 55))

    async def test_layout_has_no_modern_metadata_or_footer_area(self):
        async def icon_loader(url: str):
            return Image.new("RGBA", (32, 32), (120, 80, 40, 255))

        first_payload = sample_payload()
        second_payload = sample_payload()
        second_payload["title"] = "完全不同的现代大标题"
        second_payload["recipe"]["conditions"] = ["不应绘制的条件文字"]
        second_payload["recipe"]["required_mods"] = ["不应绘制的模组名"]
        second_payload["recipe"]["materials"] = [
            {"name": "不应绘制的材料统计", "count": 999}
        ]

        first = await mcmod_recipe_image.render_recipe_image_base64(
            first_payload,
            icon_loader=icon_loader,
        )
        second = await mcmod_recipe_image.render_recipe_image_base64(
            second_payload,
            icon_loader=icon_loader,
        )
        first_image = Image.open(io.BytesIO(base64.b64decode(first))).convert("RGB")
        second_image = Image.open(io.BytesIO(base64.b64decode(second))).convert("RGB")

        self.assertEqual(first_image.tobytes(), second_image.tobytes())
        logical_width, _ = mcmod_recipe_image._LOGICAL_IMAGE_SIZE
        scale = first_image.width // logical_width
        footer = first_image.crop((5 * scale, 116 * scale, 240 * scale, 122 * scale))
        self.assertEqual(footer.getcolors(maxcolors=10), [(footer.width * footer.height, (198, 198, 198))])

    async def test_repeated_icon_urls_load_once_per_render(self):
        calls = []

        async def icon_loader(url: str):
            calls.append(url)
            return Image.new("RGBA", (16, 16), (100, 100, 100, 255))

        encoded = await mcmod_recipe_image.render_recipe_image_base64(
            sample_payload(),
            icon_loader=icon_loader,
        )

        self.assertIsNotNone(encoded)
        self.assertEqual(calls.count(ICON_A_128), 1)
        self.assertEqual(calls.count(ICON_B_128), 1)
        self.assertEqual(calls.count(ICON_OUT_128), 1)

    async def test_single_icon_failure_uses_placeholder(self):
        async def icon_loader(url: str):
            if url == ICON_B_128:
                raise TimeoutError("icon timeout")
            return Image.new("RGBA", (16, 16), (180, 120, 60, 255))

        encoded = await mcmod_recipe_image.render_recipe_image_base64(
            sample_payload(),
            icon_loader=icon_loader,
        )

        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, mcmod_recipe_image.RECIPE_IMAGE_SIZE)

    async def test_rejects_unreliable_layout_without_guessing(self):
        payload = sample_payload()
        payload["recipe"]["grid_slots"] = [[payload["recipe"]["grid_slots"][0][0]]]
        self.assertIsNone(
            await mcmod_recipe_image.render_recipe_image_base64(payload)
        )

        payload = sample_payload()
        payload["recipe"]["method"] = "熔炉"
        self.assertIsNone(
            await mcmod_recipe_image.render_recipe_image_base64(payload)
        )

        payload = sample_payload()
        payload["recipe"]["output"] = None
        self.assertIsNone(
            await mcmod_recipe_image.render_recipe_image_base64(payload)
        )

    async def test_process_cache_deduplicates_and_returns_copies(self):
        mcmod_recipe_image.clear_icon_cache()
        downloaded = Image.new("RGBA", (16, 16), (1, 2, 3, 255))
        with patch.object(
            mcmod_recipe_image,
            "_download_icon_uncached",
            AsyncMock(return_value=downloaded),
        ) as download:
            first = await mcmod_recipe_image.load_icon(ICON_A)
            second = await mcmod_recipe_image.load_icon(ICON_A_128)

        download.assert_awaited_once_with(ICON_A_128)
        self.assertIsNot(first, second)
        self.assertEqual(first.getpixel((0, 0)), second.getpixel((0, 0)))

    def test_icon_url_allowlist_is_exact_and_normalizes_to_128px(self):
        self.assertEqual(mcmod_recipe_image.normalize_icon_url(ICON_A), ICON_A_128)
        self.assertEqual(mcmod_recipe_image.normalize_icon_url(ICON_A_128), ICON_A_128)
        for unsafe in (
            "http://i.mcmod.cn/item/icon/32x32/0/40.png",
            "https://i.mcmod.cn.evil.example/item/icon/32x32/0/40.png",
            "https://i.mcmod.cn:443/item/icon/32x32/0/40.png",
            "https://user@i.mcmod.cn/item/icon/32x32/0/40.png",
            "https://i.mcmod.cn/item/icon/32x32/0/40.png?q=1",
            "https://i.mcmod.cn/item/icon/64x64/0/40.png",
            "https://i.mcmod.cn/item/icon/128x1280/0/40.png",
            "https://i.mcmod.cn/item/icon/128x128/../40.png",
            "https://i.mcmod.cn/gui/bg/1.gif",
        ):
            with self.subTest(url=unsafe):
                self.assertIsNone(mcmod_recipe_image.normalize_icon_url(unsafe))


if __name__ == "__main__":
    unittest.main()
