# AGENTS.md

## Toolchain and verification

- This repository must remain under AstrBot's `data/plugins/` tree; tests import it as `data.plugins.astrbot_zhouyi_plugin`. Use AstrBot's interpreter, not system Python:
  ```bash
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python
  ```
- Keep bytecode out of tracked source directories:
  ```bash
  PYTHONPYCACHEPREFIX="$PWD/temp/pycache" \
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python \
  -m unittest discover -s tests
  ```
- Run one module or method with standard `unittest` names:
  ```bash
  PYTHONPYCACHEPREFIX="$PWD/temp/pycache" \
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python \
  -m unittest tests.test_data_migration

  PYTHONPYCACHEPREFIX="$PWD/temp/pycache" \
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python \
  -m unittest tests.test_zhouyi_page_api.ZhouyiDashboardApiTests.test_memory_status_failure_is_reported_in_bootstrap
  ```
- Python compile check:
  ```bash
  PYTHONPYCACHEPREFIX="$PWD/temp/pycache" \
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python \
  -m compileall -q main.py runtime.py web_api.py standalone_web.py \
  zhouyi_page_api.py memory script tests
  ```
- The npm workspace root is `web/`; Node must be `>=20.19.0`. Use the lockfile and verify in this order:
  ```bash
  npm ci --prefix web
  npm run typecheck --prefix web
  npm run build --prefix web
  ```
  The root build intentionally builds `@pandyzhou/astrbot-mc-ui` before `@pandyzhou/astrbot-mc-app` because the app consumes the UI package's `dist` exports.
- There is no pytest config, frontend test script, Makefile, or CI workflow; do not invent those commands. Network and Minecraft calls are mocked in the Python suite. Full data-migration coverage depends on Linux/POSIX semantics, including `renameat2(RENAME_NOREPLACE)`.

## Runtime boundaries

- `main.py` is the only AstrBot plugin entrypoint. It performs Memory config migration before registration, owns commands/hooks, and delegates startup and shutdown to `PluginRuntime` in `runtime.py`.
- `PluginRuntime` owns Memory, the unified Page API, the standalone HTTPS service, and the hourly Minecraft trend task. Memory startup failure must degrade only Memory; Minecraft commands, Page APIs, standalone service, and trend sampling must continue.
- `memory/service.py` is a component of the root plugin, not an independent AstrBot plugin. Do not add another `@register`, duplicate `/lmem` decorators, or a separate plugin lifecycle under `memory/`.
- Keep `metadata.yaml` and `@register(...)` versions synchronized. The current plugin/data/API identifier is `astrbot_zhouyi_plugin`; `astrbot_plugin_livingmemory` remains intentionally referenced as the legacy migration source and must not be globally replaced.

## API and page architecture

- `zhouyi_page_api.py` is the unified facade:
  - `/page/v1/mc/*` delegates to `web_api.McManagerWebApi`.
  - `/page/v1/memory/*` delegates to Memory page handlers.
  - legacy `/page/*` aliases remain compatibility routes.
- The standalone HTTPS UI serves the same React build but proxies only the MC allowlist in `standalone_web.py`. Memory APIs and Memory navigation are intentionally unavailable in standalone mode; do not add `/v1/memory/*` to the proxy allowlist.
- MC endpoint changes usually require coordinated updates in `web_api.py`, `zhouyi_page_api.py`, `standalone_web.py`, frontend API types/client, and API/proxy tests. Memory endpoint changes require the facade, `memory/core/page_api_modules`, frontend Memory types/client, and Page API tests.
- Standalone mode requires the AstrBot Dashboard JWT cookie and configured HTTPS certificate/key. POST `Origin` must exactly match `DEFAULT_PUBLIC_ORIGIN`. Never forward Dashboard JWTs, API keys, arbitrary upstream routes, or filesystem paths.

## Dashboard UI conventions

- Do not place introductory or explanatory copy directly beneath page, panel, or settings-section titles. Keep `WorkshopPanel` headings title-only. Preserve field-level help and actionable context such as scope state, validation, risk, error, and confirmation messages.

## Memory migration invariants

- Config migration runs during `main.py` import. It merges the old standalone config and the root `living_memory` section into `memory`, preserves unknown fields, prefers existing `memory` values, backs up changed root config once, and does not delete the old standalone config.
- Runtime Memory data is `<astrbot_zhouyi_plugin data>/memory`. The retained legacy source is `StarTools.get_data_dir("astrbot_plugin_livingmemory")`; the old plugin must stay disabled, but its plugin and data directories must not be deleted.
- `memory/data_migration.py` treats the legacy source as read-only. SQLite recovery, WAL/SHM rebuilding, backup, and verification happen only in private staging/snapshot directories. Do not open the source databases directly with SQLite or "clean up" their sidecars.
- The migration uses `.memory.staging`, `.migration-state.json`, strict staging identity, `READY` recovery, symlink/path-overlap rejection, and atomic no-replace publication. Do not manually delete staging/state files or weaken fail-closed checks to repair production.
- A published `READY` target is live mutable data: normal writes, checkpoints, sidecars, and backup rotation may change its hashes and counts. Migration-time `verification` is audit evidence, not a permanent runtime hash baseline. Runtime reuse validates current SQLite files through a private verification snapshot.

## Minecraft persistence and tools

- Minecraft storage is one shared `<plugin-data>/mc_manager.sqlite3`, isolated by `group_id`; `read_json()`/`write_json()` are compatibility names backed by SQLite.
- Add storage changes as transactional, incremental migrations. Never overwrite `storage_meta.schema_version`; databases newer than the supported schema must remain rejected. Imported top-level `<group_id>.json` files are retained and must not be rewritten or deleted.
- Lowering `max_history_points` requires a persisted preview and explicit confirmation; settings update and pruning remain one transaction. Lowering cleanup days only changes candidates, and `/mccleanup` remains usable when automatic cleanup is disabled.
- Hourly sampling deduplicates by `(host, lookup timeout, status timeout)`, not host alone, then writes using each group's effective history limit.
- `mcmod_search` is dynamically registered from `_register_mcmod_search_tool()` via `on_plugin_loaded`, not a static `llm_tool` decorator. This avoids load-order conflicts with the retained `mcmod_card` plugin. Preserve inactive state owned by this plugin and do not modify or delete `/data/astrbot/data/plugins/mcmod_card`.

## Generated files and repository hygiene

- `pages/zhouyi-dashboard/` is the only tracked production page build. `pages/mc-manager/` and `pages/livingmemory-dashboard/` are obsolete and must not return. `web/packages/ui/dist/` is generated and ignored.
- Never hand-edit hashed files under `pages/zhouyi-dashboard/assets/`; `npm run build --prefix web` clears and regenerates the directory, so stage new hashes and deleted old hashes together.
- Vite dev runs on `0.0.0.0:35021` and proxies to `https://127.0.0.1:35015`; override with `VITE_API_PROXY_TARGET`. `VITE_MOCK_API=true` selects the frontend mock client.
- Put all repository scratch data, build caches, and temporary downloads under `temp/`. For overseas downloads, set `HTTP_PROXY` and `HTTPS_PROXY` to `http://127.0.0.1:7890`.
- Root `AGENTS.md` is ignored by `.gitignore`; use `git add -f AGENTS.md` only when the user explicitly asks to commit it.
