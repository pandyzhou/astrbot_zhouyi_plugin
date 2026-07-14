from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.source_update_monitor import (
    SOURCES,
    HttpResponse,
    SourceUpdateMonitor,
)


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Fetcher:
    def __init__(self, responses: dict[str, HttpResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self, url: str) -> HttpResponse:
        self.calls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            value = self.responses[url]
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.active -= 1


def _json_response(value, *, remaining: int = 50) -> HttpResponse:
    return HttpResponse(
        status=200,
        headers={
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": "1700003600",
        },
        body=json.dumps(value).encode("utf-8"),
    )


def _metadata_response(version: str, *, remaining: int = 49) -> HttpResponse:
    content = base64.b64encode(f"name: plugin\nversion: {version}\n".encode()).decode()
    return _json_response({"encoding": "base64", "content": content}, remaining=remaining)


def _commit_response(
    sha: str,
    repository: str,
    *,
    as_list: bool,
    remaining: int,
) -> HttpResponse:
    value = {
        "sha": sha,
        "html_url": f"https://github.com/{repository}/commit/{sha}",
        "commit": {
            "message": "feat: upstream update\n\nmore details",
            "committer": {"date": "2023-11-14T21:13:20Z"},
        },
    }
    return _json_response([value] if as_list else value, remaining=remaining)


def _responses(
    *,
    living_commit: str | None = None,
    living_version: str = "v2.3.6",
    mc_commit: str | None = None,
    mc_version: str = "1.5.0",
) -> dict[str, HttpResponse | Exception]:
    living, mc = SOURCES
    return {
        living.commit_url: _commit_response(
            living_commit or living.commit,
            living.repository,
            as_list=False,
            remaining=48,
        ),
        living.metadata_url: _metadata_response(living_version, remaining=47),
        mc.commit_url: _commit_response(
            mc_commit or mc.commit,
            mc.repository,
            as_list=False,
            remaining=46,
        ),
        mc.metadata_url: _metadata_response(mc_version, remaining=45),
    }


class SourceUpdateMonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_fixed_source_baselines_and_urls(self):
        living, mc = SOURCES
        self.assertEqual(
            (living.name, living.version, living.commit, living.repository, living.path, living.branch),
            (
                "LivingMemory",
                "2.3.6",
                "fdcdaa063c43dad29f176eeede9cb1c54e325470",
                "lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory",
                "",
                "master",
            ),
        )
        self.assertEqual(
            (mc.name, mc.version, mc.commit, mc.repository, mc.branch),
            (
                "MCGetter Enhanced",
                "v1.5.0",
                "731cc450a44deed185c336fcabc5cd4fbd832f59",
                "xiaoxi68/astrbot_mcgetter_enhanced",
                "main",
            ),
        )
        for source in SOURCES:
            self.assertTrue(source.commit_url.startswith("https://api.github.com/"))
            self.assertTrue(source.metadata_url.startswith("https://api.github.com/"))

    async def test_statuses_timestamps_rate_limit_and_parallel_fetches(self):
        living, mc = SOURCES
        fetcher = _Fetcher(
            _responses(
                living_version="2.4.0",
                mc_commit="a" * 40,
            )
        )
        fetcher.gate = asyncio.Event()
        clock = _Clock(1_700_000_000)
        monitor = SourceUpdateMonitor(fetcher=fetcher, clock=clock)

        task = asyncio.create_task(monitor.get_updates())
        for _ in range(20):
            if len(fetcher.calls) == 4:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(fetcher.calls), 4)
        fetcher.gate.set()
        payload = await task

        sources = {source["id"]: source for source in payload["sources"]}
        self.assertEqual(sources[living.source_id]["status"], "new_version")
        self.assertEqual(sources[mc.source_id]["status"], "new_commits")
        self.assertEqual(sources[living.source_id]["latest_version"], "2.4.0")
        self.assertEqual(sources[living.source_id]["upstream"]["version"], "2.4.0")
        self.assertEqual(sources[mc.source_id]["latest_commit"], "a" * 40)
        self.assertEqual(sources[mc.source_id]["upstream"]["commit_sha"], "a" * 40)
        self.assertEqual(
            sources[mc.source_id]["upstream"]["commit_title"],
            "feat: upstream update",
        )
        self.assertEqual(sources[mc.source_id]["upstream"]["committed_at"], 1_699_996_400)
        self.assertEqual(
            sources[mc.source_id]["upstream"]["repository_url"],
            f"https://github.com/{mc.repository}",
        )
        self.assertEqual(
            sources[mc.source_id]["upstream"]["commit_url"],
            f"https://github.com/{mc.repository}/commit/{'a' * 40}",
        )
        self.assertEqual(
            sources[living.source_id]["baseline"],
            {
                "version": living.version,
                "commit_sha": living.commit,
                "repository": living.repository,
                "branch": living.branch,
            },
        )
        self.assertEqual(sources[living.source_id]["display_name"], "LivingMemory")
        self.assertTrue(sources[living.source_id]["role"])
        self.assertGreaterEqual(fetcher.max_active, 2)
        self.assertEqual(payload["checked_at"], 1_700_000_000)
        self.assertEqual(payload["next_check_at"], 1_700_001_800)
        self.assertEqual(payload["refresh_allowed_at"], 1_700_000_060)
        self.assertEqual(payload["rate_limit"]["limit"], 60)
        self.assertEqual(payload["rate_limit"]["remaining"], 45)
        self.assertEqual(payload["rate_limit"]["reset_at"], 1_700_003_600)

    async def test_changed_and_current_ignore_optional_v_prefix(self):
        living, mc = SOURCES
        fetcher = _Fetcher(
            _responses(
                living_commit="b" * 40,
                living_version="v2.2.0",
                mc_version="1.5.0",
            )
        )
        payload = await SourceUpdateMonitor(fetcher=fetcher).get_updates()
        sources = {source["id"]: source for source in payload["sources"]}
        self.assertEqual(sources[living.source_id]["status"], "changed")
        self.assertEqual(sources[mc.source_id]["status"], "current")

    async def test_cache_expiry_manual_cooldown_and_concurrent_deduplication(self):
        fetcher = _Fetcher(_responses())
        fetcher.gate = asyncio.Event()
        clock = _Clock(1_700_000_000)
        monitor = SourceUpdateMonitor(fetcher=fetcher, clock=clock)

        first, second = await asyncio.gather(
            self._release_after_calls(monitor.get_updates(), fetcher, 4),
            monitor.get_updates(),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(fetcher.calls), 4)

        await monitor.get_updates()
        await monitor.refresh()
        self.assertEqual(len(fetcher.calls), 4)

        clock.value += 61
        fetcher.gate = asyncio.Event()
        manual_first, manual_second = await asyncio.gather(
            self._release_after_calls(monitor.refresh(), fetcher, 8),
            monitor.refresh(),
        )
        self.assertEqual(manual_first, manual_second)
        self.assertEqual(len(fetcher.calls), 8)

        clock.value += 1_801
        fetcher.gate = None
        await monitor.get_updates()
        self.assertEqual(len(fetcher.calls), 12)

    async def test_source_errors_are_independent_and_unavailable(self):
        living, mc = SOURCES
        responses = _responses(mc_commit="c" * 40)
        responses[living.commit_url] = HttpResponse(
            status=403,
            headers={
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1700003600",
            },
            body=b'{"message":"rate limit"}',
        )
        fetcher = _Fetcher(responses)
        payload = await SourceUpdateMonitor(fetcher=fetcher).get_updates()
        sources = {source["id"]: source for source in payload["sources"]}

        self.assertEqual(sources[living.source_id]["status"], "unavailable")
        self.assertIn("GitHub API HTTP 403", sources[living.source_id]["error"])
        self.assertEqual(payload["rate_limit"]["remaining"], 0)
        self.assertEqual(sources[mc.source_id]["status"], "new_commits")
        self.assertIsNone(sources[mc.source_id]["error"])

    async def test_metadata_failure_keeps_commit_based_status(self):
        living, _ = SOURCES
        responses = _responses(living_commit="d" * 40)
        responses[living.metadata_url] = RuntimeError("metadata offline")

        payload = await SourceUpdateMonitor(fetcher=_Fetcher(responses)).get_updates()
        source = next(item for item in payload["sources"] if item["id"] == living.source_id)

        self.assertEqual(source["status"], "new_commits")
        self.assertEqual(source["upstream"]["commit_sha"], "d" * 40)
        self.assertIsNone(source["upstream"]["version"])
        self.assertIn("metadata offline", source["error"])

    async def test_failed_refresh_retains_previous_source_cache(self):
        living, mc = SOURCES
        fetcher = _Fetcher(_responses(living_version="2.4.0"))
        clock = _Clock(1_700_000_000)
        monitor = SourceUpdateMonitor(fetcher=fetcher, clock=clock)
        first = await monitor.get_updates()
        first_living = next(source for source in first["sources"] if source["id"] == living.source_id)

        fetcher.responses[living.commit_url] = RuntimeError("commit offline")
        fetcher.responses[living.metadata_url] = RuntimeError("metadata offline")
        clock.value += 61
        refreshed = await monitor.refresh()
        refreshed_living = next(
            source for source in refreshed["sources"] if source["id"] == living.source_id
        )
        refreshed_mc = next(
            source for source in refreshed["sources"] if source["id"] == mc.source_id
        )

        self.assertEqual(
            refreshed_living["upstream"]["version"],
            first_living["upstream"]["version"],
        )
        self.assertEqual(refreshed_living["status"], first_living["status"])
        self.assertTrue(refreshed_living["stale"])
        self.assertIn("commit offline", refreshed_living["error"])
        self.assertEqual(refreshed_mc["status"], "current")
        self.assertFalse(refreshed_mc["stale"])

    async def test_clock_accepts_datetime(self):
        clock = lambda: datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        payload = await SourceUpdateMonitor(fetcher=_Fetcher(_responses()), clock=clock).get_updates()
        self.assertEqual(payload["checked_at"], 1_704_164_645)

    async def _release_after_calls(self, awaitable, fetcher: _Fetcher, count: int):
        task = asyncio.create_task(awaitable)
        for _ in range(20):
            if len(fetcher.calls) >= count:
                break
            await asyncio.sleep(0)
        if fetcher.gate is not None:
            fetcher.gate.set()
        return await task


if __name__ == "__main__":
    unittest.main()
