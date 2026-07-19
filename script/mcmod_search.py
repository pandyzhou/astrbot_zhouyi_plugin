import asyncio
import copy
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from astrbot.api import logger
from bs4 import BeautifulSoup, NavigableString, Tag


SEARCH_URL = "https://search.mcmod.cn/s"
RESULT_BASE_URL = "https://www.mcmod.cn/"
CATEGORY_FILTERS = {
    "all": 0,
    "mod": 1,
    "modpack": 2,
    "item": 3,
    "tutorial": 4,
}
RETRYABLE_STATUSES = {502, 503, 504}
RESULT_PATH_PATTERNS = (
    re.compile(r"^/class/\d+\.html$"),
    re.compile(r"^/modpack/\d+\.html$"),
    re.compile(r"^/item/\d+\.html$"),
    re.compile(r"^/post/\d+\.html$"),
)
ITEM_PATH_PATTERN = re.compile(r"^/item/(\d+)\.html$")
MOD_PATH_PATTERN = re.compile(r"^/class/(\d+)\.html$")
MARKUP_PATTERN = re.compile(
    r"\[(?:ban:[^\]]*|h\d+\s*=[^\]]*|[a-zA-Z][\w-]*(?::|=)[^\]]*)\]",
    re.IGNORECASE,
)
WHITESPACE_PATTERN = re.compile(r"\s+")
CJK_WHITESPACE_PATTERN = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
TITLE_TYPE_PREFIXES = {
    "(模组)": "mod",
    "（模组）": "mod",
    "(整合包)": "modpack",
    "（整合包）": "modpack",
    "(物品/方块)": "item",
    "（物品/方块）": "item",
    "(教程)": "tutorial",
    "（教程）": "tutorial",
}
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
DETAIL_REMOVE_SELECTORS = (
    "script, style, noscript, img, .uknowtoomuch, "
    ".item-content-edit, .item-edit, .editor, .edit, .tools, .tool, "
    ".toolbar, .toolbox, [class*='edit'], [class*='tool']"
)
DETAIL_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
QQ_PLAIN_TEXT_REPLY_INSTRUCTION = (
    "最终回复必须使用 QQ 兼容纯文本：禁止 Markdown 标题符号、粗体或斜体标记、"
    "反引号、代码块、表格、分隔线和 Markdown 链接；需要分层时使用【标题】、"
    "普通换行、1. 2. 3. 编号或“·”项目符号，注册名直接写普通文本。"
    "严格只回答用户明确询问的内容，不要根据详情页主动扩展未被询问的信息；"
    "若用户只问制作或合成方法，必须优先使用详情 recipes 中的实际材料、九宫格摆放、"
    "产物数量和必要的版本条件；不得因 introduction 未包含配方而回答无法确定，"
    "禁止补充注册名、物品命令、最大堆叠、资料分类、怎么用、用途、性能、外观、"
    "小提示或其他百科内容。"
    "默认正文最多 2 个短段，只有用户明确要求完整介绍时才可扩展；"
    "第一句直接给答案，禁止寒暄、称呼用户、确认问题或描述查询过程；"
    "不要复述问题，不要添加重复总结，不要邀请继续提问；结尾仅保留“来源：原始网址”。"
)
ITEM_SEARCH_FOLLOWUP_INSTRUCTION = (
    "当前结果仅用于选择候选，信息不完整，禁止依据 title 或 summary 回答用户。"
    "若候选仍不明确，先向用户消歧；若已能确定目标，必须直接调用 mcmod_item_detail，"
    "并且只能使用该详情工具返回的内容生成最终答案。"
)
MCMOD_QQ_PLAIN_TEXT_EXTRA_KEY = "mcmod_qq_plain_text_reply"
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\(\s*([^\s)]+)\s*\)")
_TABLE_SEPARATOR_CELL_PATTERN = re.compile(r"^:?-{3,}:?$")
_FENCED_CODE_PATTERN = re.compile(r"^(?:`{3,}|~{3,})(?:[A-Za-z0-9_.+-]+)?\s*$")
_CRAFTING_ONLY_BANNED_SECTIONS = {
    "怎么用",
    "使用方法",
    "用途",
    "性能",
    "外观",
    "小提示",
    "其他信息",
    "你知道吗",
    "历史",
}
_CRAFTING_ONLY_BANNED_LINE_TERMS = (
    "最大堆叠",
    "资料分类",
    "物品命令",
    "应力生产",
    "RPM 生产",
    "一句话总结",
)
_CRAFTING_ONLY_RECOMMENDATION_TERMS = (
    "JEI",
    "合成表图片",
    "游戏内查看",
    "查看会更靠谱",
    "去看百科",
)
_CRAFTING_ONLY_FILLER_PATTERN = re.compile(
    r"^(?:唔|嗯|呀|诶|好呢|好的|好哒)[…~！!。,.，\s]*$"
)
_CRAFTING_ONLY_ADDRESS_PATTERN = re.compile(r"^(?:主人|爸爸)[，,、：:\s]+")
_CRAFTING_ONLY_SELF_REFERENCE_PATTERN = re.compile(r"^(?:所以)?星瑶没法")
_CRAFTING_ONLY_UNCERTAINTY_PATTERN = re.compile(
    r"(?:(?:没有|未能|没能|没法|无法|不能)(?:拿到|获取|解析出|给出|找到|确定|判断)"
    r".{0,30}(?:配方|材料|摆放|产物|数量)|(?:配方|材料|摆放|产物|数量)"
    r".{0,30}(?:无法|没法|不能)(?:确定|判断|获取|给出))"
)
_QQ_HEADING_PATTERN = re.compile(r"^【([^】]+)】$")
_RECIPE_COUNT_PATTERN = re.compile(r"\*\s*(\d+)")
_RECIPE_POSITION_PATTERN = re.compile(
    r"margin:\s*(-?\d+)px\s+0\s+0\s+(-?\d+)px",
    re.IGNORECASE,
)
_RECIPE_GRID_X = {46: 0, 82: 1, 118: 2}
_RECIPE_GRID_Y = {34: 0, 70: 1, 106: 2}


def _format_mcmod_inline_text(text: str) -> str:
    text = _MARKDOWN_LINK_PATTERN.sub(
        lambda match: (
            f"{match.group(1).strip()}：{match.group(2)}"
            if match.group(1).strip()
            else match.group(2)
        ),
        text,
    )
    return text.replace("**", "").replace("__", "").replace("`", "")


def format_mcmod_qq_plain_text(text: str) -> str:
    """将常见 Markdown 回复转换为适合 QQ 展示的纯文本。"""
    if not text:
        return ""

    lines: list[str] = []
    in_fenced_code = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        if _FENCED_CODE_PATTERN.fullmatch(stripped):
            in_fenced_code = not in_fenced_code
            continue
        if in_fenced_code:
            lines.append(_format_mcmod_inline_text(line).rstrip())
            continue
        if re.fullmatch(r"(?:-{3,}|\*{3,})", stripped):
            continue

        table_candidate = stripped.strip("|")
        table_cells = [cell.strip().replace(" ", "") for cell in table_candidate.split("|")]
        if len(table_cells) >= 2 and all(
            _TABLE_SEPARATOR_CELL_PATTERN.fullmatch(cell) for cell in table_cells
        ):
            continue

        line = re.sub(r"^\s*(?:>\s*)+", "", line)

        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            line = f"【{heading.group(1).strip()}】"
        else:
            line = re.sub(r"^(\s*)[-*+]\s+", r"\1· ", line)
            line = re.sub(r"^(\s*)(\d+)[.)]\s+", r"\1\2. ", line)

        stripped = line.strip()
        if "|" in stripped and (stripped.startswith("|") or stripped.endswith("|")):
            table_cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            line = "｜".join(table_cells)

        lines.append(_format_mcmod_inline_text(line).rstrip())

    normalized_lines: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized_lines.append("" if is_blank else line)
        previous_blank = is_blank

    return "\n".join(normalized_lines).strip()


def format_mcmod_crafting_only_reply(text: str) -> str:
    """裁剪制作类问题中未被询问的百科扩展内容。"""
    if not text:
        return ""

    kept_lines: list[str] = []
    skip_section = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _CRAFTING_ONLY_ADDRESS_PATTERN.sub("", raw_line)
        line = _CRAFTING_ONLY_SELF_REFERENCE_PATTERN.sub("因此无法", line)
        stripped = line.strip()
        heading_match = _QQ_HEADING_PATTERN.fullmatch(stripped)
        if heading_match:
            heading = heading_match.group(1).strip()
            skip_section = any(term in heading for term in _CRAFTING_ONLY_BANNED_SECTIONS)
            if skip_section:
                continue

        if stripped.startswith("来源："):
            skip_section = False
        if skip_section:
            continue
        if _CRAFTING_ONLY_FILLER_PATTERN.fullmatch(stripped):
            continue
        if _CRAFTING_ONLY_UNCERTAINTY_PATTERN.search(stripped):
            continue
        if any(term in stripped for term in _CRAFTING_ONLY_BANNED_LINE_TERMS):
            continue
        if any(term in stripped for term in _CRAFTING_ONLY_RECOMMENDATION_TERMS):
            continue

        kept_lines.append(line.rstrip())

    normalized_lines: list[str] = []
    previous_blank = False
    for line in kept_lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized_lines.append("" if is_blank else line)
        previous_blank = is_blank

    return "\n".join(normalized_lines).strip()


def _request_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=10, connect=4, sock_read=8)


def _response(
    status: str,
    query: Any,
    category: Any,
    page: Any,
    limit: Any,
    **extra: Any,
) -> dict:
    result = {
        "status": status,
        "query": query,
        "category": category,
        "page": page,
        "limit": limit,
        "count": 0,
        "results": [],
        "reply_instruction": QQ_PLAIN_TEXT_REPLY_INSTRUCTION,
    }
    result.update(extra)
    return result


def _detail_response(status: str, source_url: Any, detail: dict | None = None) -> dict:
    return {
        "status": status,
        "source_url": source_url,
        "detail": detail,
        "content_is_untrusted": True,
        "reply_instruction": QQ_PLAIN_TEXT_REPLY_INSTRUCTION,
    }


def _validate_arguments(
    query: Any,
    category: Any,
    page: Any,
    limit: Any,
) -> tuple[str | None, str | None, str | None]:
    if not isinstance(query, str):
        return None, None, "query 必须是字符串"

    normalized_query = query.strip()
    if not 1 <= len(normalized_query) <= 100:
        return None, None, "query 长度必须为 1-100 个字符"

    if not isinstance(category, str):
        return None, None, "category 必须是字符串"
    normalized_category = category.strip().lower()
    if normalized_category not in CATEGORY_FILTERS:
        return None, None, "category 必须是 all、mod、modpack、item 或 tutorial"

    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 20:
        return None, None, "page 必须是 1-20 的整数"
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        return None, None, "limit 必须是 1-10 的整数"

    return normalized_query, normalized_category, None


def _clean_text(text: str, max_length: int | None = None) -> str:
    cleaned = MARKUP_PATTERN.sub(" ", text)
    cleaned = WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    cleaned = CJK_WHITESPACE_PATTERN.sub("", cleaned)
    if max_length is not None and len(cleaned) > max_length:
        return cleaned[:max_length].rstrip()
    return cleaned


def _is_valid_result_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc.lower() not in {"mcmod.cn", "www.mcmod.cn"}:
        return False
    if parsed.params or parsed.query or parsed.fragment:
        return False
    return any(pattern.fullmatch(parsed.path) for pattern in RESULT_PATH_PATTERNS)


def normalize_mcmod_item_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"mcmod.cn", "www.mcmod.cn"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None
    match = ITEM_PATH_PATTERN.fullmatch(parsed.path)
    if match is None:
        return None
    return f"https://www.mcmod.cn/item/{match.group(1)}.html"


def _normalize_mcmod_mod_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"mcmod.cn", "www.mcmod.cn"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None
    match = MOD_PATH_PATTERN.fullmatch(parsed.path)
    if match is None:
        return None
    return f"https://www.mcmod.cn/class/{match.group(1)}.html"


def _result_type(title: str, url: str) -> str:
    for prefix, result_type in TITLE_TYPE_PREFIXES.items():
        if title.startswith(prefix):
            return result_type

    path = urlparse(url).path
    if path.startswith("/modpack/"):
        return "modpack"
    if path.startswith("/item/"):
        return "item"
    if path.startswith("/post/"):
        return "tutorial"
    if path.startswith("/class/"):
        return "mod"
    return "unknown"


def parse_search_html(html: str, limit: int) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(".search-result-list")
    if container is None:
        return "parse_error", []

    result_nodes = container.select(".result-item")
    if not result_nodes:
        return "empty", []

    results = []
    for node in result_nodes:
        head = node.select_one(".head")
        if head is None:
            continue

        title = ""
        result_url = ""
        for link in head.select("a[href]"):
            candidate_title = _clean_text(link.get_text(" ", strip=True))
            candidate_url = urljoin(RESULT_BASE_URL, link.get("href", "").strip())
            if candidate_title and _is_valid_result_url(candidate_url):
                title = candidate_title
                result_url = candidate_url
                break

        if not title or not result_url:
            continue

        body = node.select_one(".body")
        summary = _clean_text(body.get_text(" ", strip=True), 500) if body else ""
        results.append(
            {
                "title": title,
                "url": result_url,
                "summary": summary,
                "type": _result_type(title, result_url),
            }
        )
        if len(results) >= limit:
            break

    if not results:
        return "parse_error", []
    return "success", results


def _clean_attribute_part(text: str, *, key: bool = False) -> str:
    cleaned = _clean_text(text)
    if key:
        return cleaned.rstrip("：: ")
    return cleaned.lstrip("：: ")


def _extract_detail_paragraphs(content: Tag) -> list[str]:
    paragraphs: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        text = _clean_text(" ".join(buffer))
        buffer.clear()
        if text and (not paragraphs or paragraphs[-1] != text):
            paragraphs.append(text)

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                buffer.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            if child.name == "br":
                flush()
                continue
            if child.name in DETAIL_BLOCK_TAGS:
                flush()
                block_text = _clean_text(child.get_text(" ", strip=True))
                if block_text and (not paragraphs or paragraphs[-1] != block_text):
                    paragraphs.append(block_text)
                continue
            walk(child)

    walk(content)
    flush()
    return paragraphs


def _image_is_128(image: Tag) -> bool:
    source = str(image.get("src", "")).strip().lower()
    if "/128x128/" in source or "@128x128" in source:
        return True
    width = str(image.get("width", "")).strip().lower().removesuffix("px")
    height = str(image.get("height", "")).strip().lower().removesuffix("px")
    if width == "128" and height == "128":
        return True
    style = re.sub(r"\s+", "", str(image.get("style", "")).lower())
    return bool(
        re.search(r"(?:^|;)width:128px(?:;|$)", style)
        and re.search(r"(?:^|;)height:128px(?:;|$)", style)
    )


def _extract_icon_url(soup: BeautifulSoup, source_url: str) -> str | None:
    detail_tables = soup.select("table.group-2, table.righttable, .item-table, .item-info")
    for table in detail_tables:
        for image in table.select("img[src]"):
            if _image_is_128(image):
                return urljoin(source_url, image.get("src", "").strip())
    return None


def _normalize_recipe_source(href: str, source_url: str) -> str | None:
    try:
        parsed = urlparse(urljoin(source_url, href.strip()))
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"mcmod.cn", "www.mcmod.cn"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None
    if not (
        ITEM_PATH_PATTERN.fullmatch(parsed.path)
        or parsed.path.startswith("/oredict/")
    ):
        return None
    return f"https://www.mcmod.cn{parsed.path}"


def _parse_recipe_entry(node: Tag, source_url: str) -> dict | None:
    anchor = node.select_one("a[href]")
    if anchor is None:
        return None
    name = _clean_text(anchor.get_text(" ", strip=True))
    source = _normalize_recipe_source(str(anchor.get("href", "")), source_url)
    if not name or source is None:
        return None
    text = _clean_text(node.get_text(" ", strip=True))
    count_match = _RECIPE_COUNT_PATTERN.search(text)
    count = int(count_match.group(1)) if count_match else 1
    return {"name": name, "count": count, "source": source}


def _recipe_variant_names(gui: Tag, source_url: str) -> dict[str, list[str]]:
    variants: dict[str, list[str]] = {}
    for node in gui.select(".item-table-hover"):
        anchor = node.select_one("a[href]")
        image = node.select_one("img[alt]")
        if anchor is None or image is None:
            continue
        source = _normalize_recipe_source(str(anchor.get("href", "")), source_url)
        name = _clean_text(str(image.get("alt", "")))
        if source is None or not name:
            continue
        names = variants.setdefault(source, [])
        if name not in names:
            names.append(name)
    return variants


def _longest_common_suffix(names: list[str]) -> str:
    if len(names) < 2:
        return ""
    reversed_names = [name[::-1] for name in names]
    suffix_length = 0
    for chars in zip(*reversed_names):
        if len(set(chars)) != 1:
            break
        suffix_length += 1
    return names[0][-suffix_length:] if suffix_length else ""


def _friendly_recipe_name(
    name: str,
    source: str,
    variants: dict[str, list[str]],
) -> str:
    if not name.startswith("标签:"):
        return name
    common_suffix = _longest_common_suffix(variants.get(source, []))
    if len(common_suffix) >= 2:
        return f"任意{common_suffix}"
    return name


def _extract_recipe_grid(
    gui: Tag,
    source_url: str,
    material_labels: dict[str, str],
) -> list[list[str]] | None:
    table_block = gui.select_one(".TableBlock")
    if table_block is None or "bg/1.gif" not in str(table_block.get("style", "")):
        return None

    grid = [["" for _ in range(3)] for _ in range(3)]
    found = False
    for node in table_block.select(".item-table-hover[style]"):
        position = _RECIPE_POSITION_PATTERN.search(str(node.get("style", "")))
        anchor = node.select_one("a[href]")
        if position is None or anchor is None:
            continue
        row = _RECIPE_GRID_Y.get(int(position.group(1)))
        column = _RECIPE_GRID_X.get(int(position.group(2)))
        source = _normalize_recipe_source(str(anchor.get("href", "")), source_url)
        if row is None or column is None or source not in material_labels:
            continue
        grid[row][column] = material_labels[source]
        found = True
    return grid if found else None


def _extract_item_recipes(soup: BeautifulSoup, source_url: str) -> list[dict]:
    recipes: list[dict] = []
    for row in soup.select("table.item-table-block tr"):
        count_cell = row.select_one("td.item-table-count")
        gui_cell = row.select_one("td.item-table-gui")
        if count_cell is None or gui_cell is None:
            continue

        paragraphs = count_cell.select("p")
        method = ""
        materials: list[dict] = []
        output = None
        after_arrow = False
        for paragraph in paragraphs:
            text = _clean_text(paragraph.get_text(" ", strip=True))
            if not text:
                continue
            if "使用:" in text or "使用：" in text:
                method_anchor = paragraph.select_one("a[href]")
                if method_anchor is not None:
                    method = _clean_text(method_anchor.get_text(" ", strip=True))
                continue
            if "↓" in text:
                after_arrow = True
                continue
            entry = _parse_recipe_entry(paragraph, source_url)
            if entry is None:
                continue
            if after_arrow:
                if output is None:
                    output = entry
            else:
                materials.append(entry)

        if not materials or output is None:
            continue

        variants = _recipe_variant_names(gui_cell, source_url)
        for material in materials:
            material["name"] = _friendly_recipe_name(
                material["name"], material["source"], variants
            )
        material_labels = {
            material["source"]: material["name"] for material in materials
        }
        grid = _extract_recipe_grid(gui_cell, source_url, material_labels)

        conditions: list[str] = []
        remarks = row.select_one("td.item-table-remarks")
        if remarks is not None:
            for node in remarks.select(".alert, .remark"):
                condition = _clean_text(node.get_text(" ", strip=True))
                if condition and condition not in conditions:
                    conditions.append(condition)

        recipes.append(
            {
                "method": method or None,
                "materials": materials,
                "grid": grid,
                "output": output,
                "conditions": conditions,
            }
        )
    return recipes


def parse_item_detail_html(
    html: str,
    source_url: str,
    max_intro_length: int = 2500,
) -> tuple[str, dict | None]:
    soup = BeautifulSoup(html, "html.parser")

    title_node = soup.select_one(".itemname .name h5")
    title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""

    mod = None
    for link in soup.select(".common-nav a[href]"):
        candidate_url = _normalize_mcmod_mod_url(
            urljoin(source_url, link.get("href", "").strip())
        )
        if candidate_url is None:
            continue
        candidate_name = _clean_text(link.get_text(" ", strip=True))
        if candidate_name:
            mod = {"name": candidate_name, "url": candidate_url}
            break

    command_node = soup.select_one(".item-give span")
    item_command = (
        _clean_text(command_node.get_text(" ", strip=True)) if command_node else None
    )
    if not item_command:
        item_command = None

    rows = soup.select("table.group-2 tr")
    if not rows:
        rows = soup.select(".righttable tr")
    attributes: dict[str, str] = {}
    for row in rows:
        cells = row.select("td")
        if len(cells) < 2:
            continue
        key = _clean_attribute_part(cells[0].get_text(" ", strip=True), key=True)
        value = _clean_attribute_part(cells[1].get_text(" ", strip=True))
        if not key or not value or key in attributes:
            continue
        attributes[key] = value
        if len(attributes) >= 20:
            break

    content_node = soup.select_one(".item-content.common-text")
    introduction = ""
    if content_node is not None:
        content_copy = copy.copy(content_node)
        for unwanted in content_copy.select(DETAIL_REMOVE_SELECTORS):
            unwanted.decompose()
        paragraphs = _extract_detail_paragraphs(content_copy)
        introduction = "\n\n".join(paragraphs).strip()

    if isinstance(max_intro_length, bool) or not isinstance(max_intro_length, int):
        max_intro_length = 2500
    max_intro_length = max(1, max_intro_length)
    if len(introduction) > max_intro_length:
        introduction = introduction[: max_intro_length - 1].rstrip() + "…"

    recipes = _extract_item_recipes(soup, source_url)
    if not title or (not introduction and not recipes):
        return "parse_error", None

    detail = {
        "title": title,
        "mod": mod,
        "item_command": item_command,
        "attributes": attributes,
        "introduction": introduction,
        "recipes": recipes,
        "icon_url": _extract_icon_url(soup, source_url),
        "source_url": source_url,
    }
    return "success", detail


async def _fetch_search_html(
    params: dict,
    *,
    session_factory: Callable[..., Any] | None = None,
) -> tuple[str, str | None]:
    factory = session_factory or aiohttp.ClientSession

    async with factory(
        timeout=_request_timeout(), headers=REQUEST_HEADERS, trust_env=True
    ) as session:
        for attempt in range(2):
            try:
                async with session.get(SEARCH_URL, params=params) as response:
                    if response.status == 200:
                        return "success", await response.text(errors="replace")
                    if response.status == 429:
                        logger.warning("MC百科搜索请求受到频率限制")
                        return "rate_limited", None
                    if response.status in RETRYABLE_STATUSES and attempt == 0:
                        logger.warning("MC百科搜索上游暂时不可用，准备重试")
                        await asyncio.sleep(0.3)
                        continue
                    logger.warning("MC百科搜索请求失败，HTTP 状态码：%s", response.status)
                    return "upstream_error", None
            except asyncio.TimeoutError:
                logger.warning("MC百科搜索请求超时")
                return "timeout", None
            except aiohttp.ClientConnectionError:
                if attempt == 0:
                    logger.warning("MC百科搜索连接失败，准备重试")
                    await asyncio.sleep(0.3)
                    continue
                logger.warning("MC百科搜索连接失败")
                return "upstream_error", None
            except aiohttp.ClientError as exc:
                logger.warning("MC百科搜索请求异常：%s", type(exc).__name__)
                return "upstream_error", None

    return "upstream_error", None


async def _fetch_item_detail_html(
    url: str,
    *,
    session_factory: Callable[..., Any] | None = None,
) -> tuple[str, str | None]:
    factory = session_factory or aiohttp.ClientSession

    async with factory(
        timeout=_request_timeout(), headers=REQUEST_HEADERS, trust_env=True
    ) as session:
        for attempt in range(2):
            try:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status == 200:
                        return "success", await response.text(errors="replace")
                    if response.status == 429:
                        logger.warning("MC百科物品详情请求受到频率限制")
                        return "rate_limited", None
                    if response.status in RETRYABLE_STATUSES and attempt == 0:
                        logger.warning("MC百科物品详情上游暂时不可用，准备重试")
                        await asyncio.sleep(0.3)
                        continue
                    logger.warning(
                        "MC百科物品详情请求失败，HTTP 状态码：%s", response.status
                    )
                    return "upstream_error", None
            except asyncio.TimeoutError:
                logger.warning("MC百科物品详情请求超时")
                return "timeout", None
            except aiohttp.ClientConnectionError:
                if attempt == 0:
                    logger.warning("MC百科物品详情连接失败，准备重试")
                    await asyncio.sleep(0.3)
                    continue
                logger.warning("MC百科物品详情连接失败")
                return "upstream_error", None
            except aiohttp.ClientError as exc:
                logger.warning("MC百科物品详情请求异常：%s", type(exc).__name__)
                return "upstream_error", None

    return "upstream_error", None


async def search_mcmod(
    query: str,
    category: str = "all",
    page: int = 1,
    limit: int = 5,
) -> dict:
    normalized_query, normalized_category, error = _validate_arguments(
        query, category, page, limit
    )
    if error:
        return _response(
            "invalid_argument",
            query,
            category,
            page,
            limit,
            error=error,
        )

    fetch_status, html = await _fetch_search_html(
        {
            "key": normalized_query,
            "filter": CATEGORY_FILTERS[normalized_category],
            "page": page,
        }
    )
    if fetch_status != "success" or html is None:
        return _response(
            fetch_status,
            normalized_query,
            normalized_category,
            page,
            limit,
        )

    try:
        parse_status, results = parse_search_html(html, limit)
    except Exception as exc:
        logger.warning("MC百科搜索结果解析异常：%s", type(exc).__name__)
        parse_status, results = "parse_error", []

    response_extra: dict[str, Any] = {
        "count": len(results),
        "results": results,
    }
    if normalized_category == "item" and results:
        response_extra.update(
            {
                "answer_state": "incomplete_candidates_only",
                "required_next_tool": "mcmod_item_detail",
                "followup_instruction": ITEM_SEARCH_FOLLOWUP_INSTRUCTION,
            }
        )

    return _response(
        parse_status,
        normalized_query,
        normalized_category,
        page,
        limit,
        **response_extra,
    )


async def get_mcmod_item_detail(url: str) -> dict:
    normalized_url = normalize_mcmod_item_url(url)
    if normalized_url is None:
        return _detail_response(
            "invalid_argument", url if isinstance(url, str) else None
        )

    fetch_status, html = await _fetch_item_detail_html(normalized_url)
    if fetch_status != "success" or html is None:
        return _detail_response(fetch_status, normalized_url)

    try:
        parse_status, detail = parse_item_detail_html(html, normalized_url)
    except Exception as exc:
        logger.warning("MC百科物品详情解析异常：%s", type(exc).__name__)
        return _detail_response("parse_error", normalized_url)

    return _detail_response(parse_status, normalized_url, detail)
