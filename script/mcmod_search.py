import asyncio
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from astrbot.api import logger
from bs4 import BeautifulSoup


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
    }
    result.update(extra)
    return result


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


async def _fetch_search_html(
    params: dict,
    *,
    session_factory: Callable[..., Any] | None = None,
) -> tuple[str, str | None]:
    timeout = aiohttp.ClientTimeout(total=10, connect=4, sock_read=8)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    factory = session_factory or aiohttp.ClientSession

    async with factory(timeout=timeout, headers=headers, trust_env=True) as session:
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

    return _response(
        parse_status,
        normalized_query,
        normalized_category,
        page,
        limit,
        count=len(results),
        results=results,
    )
