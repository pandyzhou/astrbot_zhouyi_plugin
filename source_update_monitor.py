from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping

import aiohttp

CACHE_SECONDS = 30 * 60
REFRESH_COOLDOWN_SECONDS = 60
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=2, sock_read=4)
_VERSION_RE = re.compile(r"^\s*version\s*:\s*['\"]?([^\s#'\"]+)", re.MULTILINE)


@dataclass(frozen=True)
class SourceBaseline:
    source_id: str
    name: str
    role: str
    repository: str
    path: str
    branch: str
    version: str
    commit: str
    commit_url: str
    metadata_url: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


SOURCES: tuple[SourceBaseline, ...] = (
    SourceBaseline(
        source_id="livingmemory",
        name="LivingMemory",
        role="提供长期记忆 Memory 能力",
        repository="lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory",
        path="",
        branch="master",
        version="2.3.6",
        commit="fdcdaa063c43dad29f176eeede9cb1c54e325470",
        commit_url=(
            "https://api.github.com/repos/lxfight-s-Astrbot-Plugins/"
            "astrbot_plugin_livingmemory/commits/master"
        ),
        metadata_url=(
            "https://api.github.com/repos/lxfight-s-Astrbot-Plugins/"
            "astrbot_plugin_livingmemory/contents/metadata.yaml?ref=master"
        ),
    ),
    SourceBaseline(
        source_id="mcgetter_enhanced",
        name="MCGetter Enhanced",
        role="提供 Minecraft 服务器管理能力",
        repository="xiaoxi68/astrbot_mcgetter_enhanced",
        path="",
        branch="main",
        version="v1.5.0",
        commit="731cc450a44deed185c336fcabc5cd4fbd832f59",
        commit_url=(
            "https://api.github.com/repos/xiaoxi68/astrbot_mcgetter_enhanced/commits/main"
        ),
        metadata_url=(
            "https://api.github.com/repos/xiaoxi68/astrbot_mcgetter_enhanced/"
            "contents/metadata.yaml?ref=main"
        ),
    ),
)

HttpFetcher = Callable[[str], Awaitable[HttpResponse]]
Clock = Callable[[], float | datetime]


class SourceUpdateMonitor:
    """检查固定来源基线，并在进程内缓存检查结果。"""

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        clock: Clock = time.time,
        cache_seconds: int = CACHE_SECONDS,
        refresh_cooldown_seconds: int = REFRESH_COOLDOWN_SECONDS,
    ) -> None:
        self._fetcher = fetcher or self._fetch_http
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._refresh_cooldown_seconds = refresh_cooldown_seconds
        self._cache: dict[str, object] | None = None
        self._next_check_at = 0.0
        self._refresh_allowed_at = 0.0
        self._refresh_task: asyncio.Task[dict[str, object]] | None = None
        self._refresh_lock = asyncio.Lock()

    async def get_updates(self) -> dict[str, object]:
        now = self._now()
        if self._cache is not None and now < self._next_check_at:
            return self._response_copy()
        return await self._refresh(manual=False)

    async def refresh(self) -> dict[str, object]:
        return await self._refresh(manual=True)

    async def _refresh(self, *, manual: bool) -> dict[str, object]:
        async with self._refresh_lock:
            now = self._now()
            task = self._refresh_task
            if task is None or task.done():
                if manual and self._cache is not None and now < self._refresh_allowed_at:
                    return self._response_copy()
                if not manual and self._cache is not None and now < self._next_check_at:
                    return self._response_copy()
                self._refresh_allowed_at = now + self._refresh_cooldown_seconds
                task = asyncio.create_task(self._perform_refresh(now))
                self._refresh_task = task

        try:
            return copy.deepcopy(await task)
        finally:
            async with self._refresh_lock:
                if self._refresh_task is task and task.done():
                    self._refresh_task = None

    async def _perform_refresh(self, checked_at: float) -> dict[str, object]:
        previous_sources = {
            str(source["id"]): source
            for source in (self._cache or {}).get("sources", [])
            if isinstance(source, dict) and "id" in source
        }
        results = await asyncio.gather(
            *(self._check_source(source) for source in SOURCES)
        )
        merged_sources: list[dict[str, object]] = []
        rate_limits: list[dict[str, int | None]] = []
        for source, result in zip(SOURCES, results):
            source_data, rate_limit = result
            rate_limits.extend(rate_limit)
            previous = previous_sources.get(source.source_id)
            if (
                source_data["status"] == "unavailable"
                and previous is not None
                and previous.get("status") != "unavailable"
            ):
                retained = copy.deepcopy(previous)
                retained["error"] = source_data["error"]
                retained["stale"] = True
                merged_sources.append(retained)
            else:
                merged_sources.append(source_data)

        self._next_check_at = checked_at + self._cache_seconds
        self._cache = {
            "checked_at": self._timestamp(checked_at),
            "next_check_at": self._timestamp(self._next_check_at),
            "rate_limit": self._merge_rate_limits(rate_limits),
            "sources": merged_sources,
        }
        return self._response_copy()

    async def _check_source(
        self, source: SourceBaseline
    ) -> tuple[dict[str, object], list[dict[str, int | None]]]:
        commit_result, metadata_result = await asyncio.gather(
            self._fetch_one(source.commit_url),
            self._fetch_one(source.metadata_url),
            return_exceptions=True,
        )
        rate_limits: list[dict[str, int | None]] = []
        commit_error: str | None = None
        metadata_error: str | None = None
        upstream = self._empty_upstream(source)

        if isinstance(commit_result, asyncio.CancelledError):
            raise commit_result
        if isinstance(commit_result, BaseException):
            commit_error = f"commit: {commit_result}"
        else:
            response, rate_limit = commit_result
            rate_limits.append(rate_limit)
            if response.status != 200:
                commit_error = f"commit: GitHub API HTTP {response.status}"
            else:
                try:
                    upstream.update(self._parse_commit(response.body, source))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    commit_error = f"commit: {exc}"

        if isinstance(metadata_result, asyncio.CancelledError):
            raise metadata_result
        if isinstance(metadata_result, BaseException):
            metadata_error = f"metadata: {metadata_result}"
        else:
            response, rate_limit = metadata_result
            rate_limits.append(rate_limit)
            if response.status != 200:
                metadata_error = f"metadata: GitHub API HTTP {response.status}"
            else:
                try:
                    upstream["version"] = self._parse_metadata_version(response.body)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    metadata_error = f"metadata: {exc}"

        payload = self._base_source_payload(source)
        payload.update(
            {
                "latest_version": upstream["version"],
                "latest_commit": upstream["commit_sha"],
                "upstream": upstream,
            }
        )
        if commit_error:
            errors = [commit_error]
            if metadata_error:
                errors.append(metadata_error)
            payload.update(
                {
                    "status": "unavailable",
                    "error": "; ".join(errors),
                    "stale": False,
                }
            )
            return payload, rate_limits

        latest_version = upstream["version"]
        latest_commit = upstream["commit_sha"]
        commit_changed = latest_commit != source.commit
        version_relation = self._compare_versions(latest_version, source.version)
        if version_relation == 1:
            status = "new_version"
        elif latest_version is not None and version_relation in {-1, None} and self._normalized_version(latest_version) != self._normalized_version(source.version):
            status = "changed"
        elif commit_changed:
            status = "new_commits"
        else:
            status = "current"
        payload.update({"status": status, "error": metadata_error, "stale": False})
        return payload, rate_limits

    async def _fetch_one(
        self, url: str
    ) -> tuple[HttpResponse, dict[str, int | None]]:
        response = await self._fetcher(url)
        return response, self._rate_limit_from_headers(response.headers)

    @staticmethod
    async def _fetch_http(url: str) -> HttpResponse:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "astrbot-zhouyi-plugin-source-monitor",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with aiohttp.ClientSession(
            timeout=HTTP_TIMEOUT,
            trust_env=True,
            headers=headers,
        ) as session:
            async with session.get(url, allow_redirects=True, max_redirects=3) as response:
                if response.url.scheme != "https" or response.url.host != "api.github.com":
                    raise RuntimeError("GitHub API 返回了不受信任的重定向地址")
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers),
                    body=await response.read(),
                )

    @classmethod
    def _parse_commit(
        cls, body: bytes, source: SourceBaseline
    ) -> dict[str, object]:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, list):
            if not payload:
                raise ValueError("GitHub commit 响应为空")
            payload = payload[0]
        if not isinstance(payload, dict):
            raise ValueError("GitHub commit 响应格式无效")
        commit_sha = payload.get("sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ValueError("GitHub commit 响应缺少 sha")

        commit_data = payload.get("commit")
        commit_data = commit_data if isinstance(commit_data, dict) else {}
        message = commit_data.get("message")
        title = message.splitlines()[0].strip() if isinstance(message, str) else None
        committer = commit_data.get("committer")
        committer = committer if isinstance(committer, dict) else {}
        committed_at = cls._parse_github_time(committer.get("date"))
        html_url = payload.get("html_url")
        commit_url = (
            html_url
            if isinstance(html_url, str) and html_url.startswith("https://github.com/")
            else f"https://github.com/{source.repository}/commit/{commit_sha}"
        )
        return {
            "commit_sha": commit_sha,
            "committed_at": committed_at,
            "commit_title": title or None,
            "commit_url": commit_url,
        }

    @staticmethod
    def _parse_metadata_version(body: bytes) -> str:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("GitHub metadata 响应格式无效")
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            raise ValueError("GitHub metadata 响应缺少 content")
        try:
            metadata = base64.b64decode(encoded, validate=False).decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("metadata.yaml 内容无法解码") from exc
        match = _VERSION_RE.search(metadata)
        if match is None:
            raise ValueError("metadata.yaml 缺少 version")
        return match.group(1)

    @staticmethod
    def _base_source_payload(source: SourceBaseline) -> dict[str, object]:
        return {
            "id": source.source_id,
            "name": source.name,
            "display_name": source.name,
            "role": source.role,
            "repository": source.repository,
            "path": source.path or None,
            "branch": source.branch,
            "baseline_version": source.version,
            "baseline_commit": source.commit,
            "baseline": {
                "version": source.version,
                "commit_sha": source.commit,
                "repository": source.repository,
                "branch": source.branch,
            },
        }

    @staticmethod
    def _empty_upstream(source: SourceBaseline) -> dict[str, object]:
        return {
            "version": None,
            "commit_sha": None,
            "committed_at": None,
            "commit_title": None,
            "repository_url": f"https://github.com/{source.repository}",
            "commit_url": None,
        }

    @staticmethod
    def _parse_github_time(value: object) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())

    @staticmethod
    def _normalized_version(version: object) -> str | None:
        if not isinstance(version, str):
            return None
        value = version.strip()
        return value[1:] if value[:1].lower() == "v" else value

    @classmethod
    def _compare_versions(cls, left: object, right: object) -> int | None:
        left_value = cls._normalized_version(left)
        right_value = cls._normalized_version(right)
        if left_value is None or right_value is None:
            return None
        if left_value == right_value:
            return 0
        if not re.fullmatch(r"\d+(?:\.\d+)*", left_value) or not re.fullmatch(
            r"\d+(?:\.\d+)*", right_value
        ):
            return None
        left_parts = [int(part) for part in left_value.split(".")]
        right_parts = [int(part) for part in right_value.split(".")]
        width = max(len(left_parts), len(right_parts))
        left_parts.extend([0] * (width - len(left_parts)))
        right_parts.extend([0] * (width - len(right_parts)))
        return 1 if left_parts > right_parts else -1

    @staticmethod
    def _rate_limit_from_headers(headers: Mapping[str, str]) -> dict[str, int | None]:
        normalized = {str(key).lower(): value for key, value in headers.items()}

        def integer(name: str) -> int | None:
            value = normalized.get(name)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "limit": integer("x-ratelimit-limit"),
            "remaining": integer("x-ratelimit-remaining"),
            "reset": integer("x-ratelimit-reset"),
        }

    def _merge_rate_limits(
        self, values: list[dict[str, int | None]]
    ) -> dict[str, object]:
        limits = [value["limit"] for value in values if value["limit"] is not None]
        remaining = [
            value["remaining"] for value in values if value["remaining"] is not None
        ]
        resets = [value["reset"] for value in values if value["reset"] is not None]
        reset = max(resets) if resets else None
        return {
            "limit": min(limits) if limits else None,
            "remaining": min(remaining) if remaining else None,
            "reset_at": self._timestamp(reset) if reset is not None else None,
        }

    def _response_copy(self) -> dict[str, object]:
        if self._cache is None:
            raise RuntimeError("来源更新缓存尚未初始化")
        response = copy.deepcopy(self._cache)
        response["refresh_allowed_at"] = self._timestamp(self._refresh_allowed_at)
        return response

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return float(value)

    @staticmethod
    def _timestamp(value: float) -> int:
        return int(value)


_default_monitor: SourceUpdateMonitor | None = None


def get_source_update_monitor() -> SourceUpdateMonitor:
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = SourceUpdateMonitor()
    return _default_monitor
