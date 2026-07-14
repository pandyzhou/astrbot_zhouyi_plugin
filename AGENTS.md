# AGENTS.md

## Verification commands

- Keep this repository under AstrBot's `data/plugins/` tree; tests import it as `data.plugins.astrbot_zhouyi_plugin`. Use AstrBot's interpreter, not system Python:
  ```bash
  PYTHONPYCACHEPREFIX="$PWD/temp/pycache" \
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python \
  -m unittest discover -s tests
  ```
- Run a focused Python test with standard `unittest` dotted names, for example:
  ```bash
  PYTHONPYCACHEPREFIX="$PWD/temp/pycache" \
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python \
  -m unittest tests.test_source_update_monitor.SourceUpdateMonitorTests.test_failed_refresh_retains_previous_source_cache
  ```
- Compile-check root modules and Python packages without writing bytecode into source directories:
  ```bash
  PYTHONPYCACHEPREFIX="$PWD/temp/pycache" \
  /data/astrbot/.local/share/uv/tools/astrbot/bin/python \
  -m compileall -q main.py runtime.py web_api.py standalone_web.py \
  zhouyi_page_api.py source_update_monitor.py memory script tests
  ```
- The npm workspace root is `web/`; Node must be `>=20.19.0`. Use the lockfile and verify in this order:
  ```bash
  npm ci --prefix web
  npm run typecheck --prefix web
  npm run build --prefix web
  ```
  The root scripts intentionally build/typecheck `@pandyzhou/astrbot-mc-ui` before `@pandyzhou/astrbot-mc-app`; the app consumes the UI package's generated `dist` exports.
- Query-cache tests use Node's built-in test runner and have no npm script:
  ```bash
  web/packages/app/node_modules/.bin/tsc -p web/packages/app/tsconfig.cache-test.json
  node --test temp/query-cache-tests/queryCacheCore.test.js
  ```
- There is no pytest, lint, formatter, pre-commit, task-runner, or repository CI configuration. Do not invent commands for them. Data-migration tests require Linux/POSIX behavior, including `renameat2(RENAME_NOREPLACE)`.

## Plugin architecture

- `main.py` is the only AstrBot plugin entrypoint and owns the only `@register`. It runs Memory config migration before registration, then delegates startup and shutdown to `PluginRuntime` in `runtime.py`.
- `PluginRuntime` owns Memory, the unified Page API, the standalone HTTPS service, and the hourly Minecraft trend task. Memory startup failure must degrade only Memory; MC commands, Page APIs, standalone service, and trend sampling must continue.
- `memory/service.py` is a component of the root plugin, not an independent plugin. Do not add another `@register`, duplicate `/lmem` decorators, or a separate lifecycle under `memory/`.
- Keep `metadata.yaml` and the `@register(...)` version synchronized. `astrbot_zhouyi_plugin` is the current identifier; `astrbot_plugin_livingmemory` is intentionally retained as a legacy migration source and must not be globally replaced.
- `mcmod_search` is dynamically registered from `_register_mcmod_search_tool()` during `on_plugin_loaded` to avoid conflicts with the retained `mcmod_card` plugin. Preserve inactive state owned by this plugin and do not modify `/data/astrbot/data/plugins/mcmod_card`.

## Page and deployment boundaries

- `zhouyi_page_api.py` is the unified facade:
  - `/page/v1/mc/*` delegates to `web_api.McManagerWebApi`.
  - `/page/v1/memory/*` delegates to Memory page handlers.
  - `/page/v1/sources/updates` and `/page/v1/sources/updates/refresh` use `source_update_monitor.py` and have no legacy aliases.
  - Legacy `/page/*` aliases remain only for the existing MC and Memory compatibility routes.
- MC endpoint changes usually require coordinated edits in `web_api.py`, `zhouyi_page_api.py`, `standalone_web.py`, frontend types/client, and proxy tests. Memory endpoint changes require the facade, `memory/core/page_api_modules`, frontend Memory types/client, and Page API tests. Source-monitor changes require the monitor, facade, frontend types/client/mock/page, and source/route/standalone tests.
- The standalone HTTPS UI serves the same React build. Its exact proxy allowlist is `GET /v1/bootstrap`; MC routes `GET /v1/mc/servers`, `POST /v1/mc/servers/add`, `POST /v1/mc/servers/update`, `POST /v1/mc/servers/delete`, `POST /v1/mc/status`, `GET /v1/mc/settings`, `POST /v1/mc/settings/preview`, `POST /v1/mc/settings`, `GET /v1/mc/trends`, `GET /v1/mc/cleanup`, and `POST /v1/mc/cleanup`; plus source-update routes `GET /v1/sources/updates` and `POST /v1/sources/updates/refresh`. Memory navigation and APIs remain intentionally embedded-only.
- New Dashboard features should support the standalone HTTPS page by default; Memory is the current explicit exception.
- Standalone mode requires the AstrBot Dashboard JWT cookie and configured HTTPS certificate/key. POST `Origin` must exactly match `DEFAULT_PUBLIC_ORIGIN`. Forward the JWT cookie only to the fixed local AstrBot upstream on explicitly allowed routes; never forward API keys, arbitrary routes, dynamic upstream targets, or filesystem paths.

## Migration and persistence invariants

- Memory config migration merges the old standalone config and root `living_memory` section into `memory`, preserves unknown fields, prefers existing `memory` values, backs up changed root config once, and does not delete the old standalone config.
- Runtime Memory data is `<plugin-data>/memory`. The retained legacy source is `StarTools.get_data_dir("astrbot_plugin_livingmemory")`; treat it as read-only. SQLite recovery, WAL/SHM rebuilding, backup, and verification must happen only in private staging/snapshot directories.
- Memory publication relies on `.memory.staging`, `.migration-state.json`, strict staging identity, `READY` recovery, symlink/path-overlap rejection, and atomic no-replace publication. A published target is live mutable data; migration-time hashes are audit evidence, not permanent runtime baselines.
- Minecraft persistence is one shared `<plugin-data>/mc_manager.sqlite3`, isolated by `group_id`. Add schema changes as transactional incremental migrations, reject databases newer than the supported schema, and retain imported top-level `<group_id>.json` files.
- Lowering `max_history_points` requires a persisted preview and explicit confirmation; settings update and pruning remain one transaction. Lowering cleanup days only changes candidates, and `/mccleanup` remains usable when automatic cleanup is disabled.
- Hourly sampling deduplicates by `(host, lookup timeout, status timeout)`, not host alone, then writes with each group's effective history limit.

## Frontend and generated files

- `web/packages/ui` is the shared UI package; `web/packages/app` is the React/Vite dashboard. The app uses `HashRouter` and serves the tracked production build from `pages/zhouyi-dashboard/`.
- `pages/zhouyi-dashboard/` is the only tracked page build. `npm run build --prefix web` clears and regenerates its hashed assets; never hand-edit them, and include new hashes, deleted old hashes, and `index.html` together.
- `web/packages/ui/dist/` is generated and ignored. Obsolete `pages/mc-manager/` and `pages/livingmemory-dashboard/` directories must not return, even if stale prose mentions them.
- Vite dev runs on `0.0.0.0:35021` and proxies to `https://127.0.0.1:35015`; override with `VITE_API_PROXY_TARGET`. `VITE_MOCK_API=true` selects the mock client, but routes restricted to embedded Dashboard mode still require `window.AstrBotPluginPage` to be present.
- Do not place introductory copy directly beneath page, panel, or settings-section titles. Keep `WorkshopPanel` headings title-only; field help, validation, risk, error, and confirmation messages are allowed.

## Repository hygiene

- Put repository scratch data, generated test output, caches, and temporary downloads under `temp/`. For overseas downloads, set `HTTP_PROXY` and `HTTPS_PROXY` to `http://127.0.0.1:7890`.
- Do not commit modified `__pycache__` or `.pyc` files. Always set `PYTHONPYCACHEPREFIX` for Python verification.
- Root `AGENTS.md` is tracked even though it matches `.gitignore`; ordinary `git add AGENTS.md` stages modifications to it.
