# AGENTS.md

## 仓库边界与事实来源

- 仓库必须位于 AstrBot 的 `data/plugins/astrbot_zhouyi_plugin`；测试按 `data.plugins.astrbot_zhouyi_plugin` 导入，脱离该目录层级会失败。
- Python 只有根目录 `requirements.txt`，没有 `pyproject.toml` 或 Python 锁文件；前端唯一 workspace 根是 `web/`，锁文件是 `web/package-lock.json`。
- 可执行脚本、Schema、测试和运行时代码优先于 README；`README.md` 包含用户文档，不是路由、构建或迁移契约。
- `metadata.yaml` 的 `version` 与 `main.py` 的唯一 `@register(...)` 版本必须同步；当前插件标识是 `astrbot_zhouyi_plugin`。
- `astrbot_plugin_livingmemory` 只作为旧配置/数据迁移来源保留，不能全局替换或作为第二个运行插件恢复。

## 对用户表达

- 首次出现专业概念时使用 `中文术语（English term）` 并给出简短定义；后续使用中文术语。不要把仅在 SPA 运行期间保留的 Zustand 状态称为“状态持久化”。
- 路由切换导致组件本地状态丢失称为 `跨路由状态丢失（cross-route state loss）`；移入 Zustand 等应用级 store 称为 `全局客户端状态管理（global client state management）`。
- 根因、触发条件、失败点、调用链、输入校验和降级路径等结论必须对应代码、配置、日志或命令证据。

## 验证命令

- Python 必须使用 AstrBot 解释器，并把字节码写到仓库 `temp/`：`PYTHONPYCACHEPREFIX="$PWD/temp/pycache" /data/astrbot/.local/share/uv/tools/astrbot/bin/python -m unittest discover -s tests`。
- 单测使用标准 `unittest` dotted name，例如：`PYTHONPYCACHEPREFIX="$PWD/temp/pycache" /data/astrbot/.local/share/uv/tools/astrbot/bin/python -m unittest tests.test_memory_config_api.MemoryConfigApiTests.test_get_is_available_when_memory_is_disabled_or_failed`。
- 根模块编译检查：`PYTHONPYCACHEPREFIX="$PWD/temp/pycache" /data/astrbot/.local/share/uv/tools/astrbot/bin/python -m compileall -q main.py runtime.py web_api.py standalone_web.py zhouyi_page_api.py memory_config_api.py source_update_monitor.py memory script tests`。
- 前端要求 Node `>=20.19.0`；按顺序运行 `npm install --prefix web`、`npm run typecheck --prefix web`、`npm run test:memory-config --prefix web`、`npm run build --prefix web`。
- workspace 根脚本会先构建/检查 `@pandyzhou/astrbot-mc-ui`，再处理 `@pandyzhou/astrbot-mc-app`；app 的包导出依赖 UI 生成的 `dist`。
- Query cache 测试没有 npm script：`web/packages/app/node_modules/.bin/tsc -p web/packages/app/tsconfig.cache-test.json && node --test temp/query-cache-tests/queryCacheCore.test.js`。
- 仓库没有 pytest、lint、formatter、pre-commit、任务运行器或 CI 配置；不要发明对应命令。
- Memory 数据迁移测试依赖 Linux/POSIX，特别是 `renameat2(RENAME_NOREPLACE)`；非 Linux 环境不能代表完整验证结果。

## 插件与运行时

- `main.py` 是唯一 AstrBot 插件入口和唯一 `@register`；模块导入时先迁移 Memory 配置，再由 `PluginRuntime` 启动组件。
- `PluginRuntime` 分别管理 Memory、统一 Page API、独立 HTTPS 服务和整点趋势任务；Memory 导入、迁移或初始化失败只能降级 Memory，MC、配置 Page API、独立页和趋势任务仍应继续。
- `memory/service.py` 是根插件内的组合组件；不得在 `memory/` 下增加 `@register`、重复 `/lmem` 装饰器或独立生命周期。
- `mcmod_search` 在 `on_plugin_loaded` 中动态注册以处理与 `mcmod_card` 的冲突；保留本插件工具的 inactive 状态，不修改相邻插件目录。
- Memory 仅在根配置存在 `memory` 对象且 `memory.enabled is True` 时启动；`MemoryService` 的核心初始化是异步、非阻塞的。

## Page API 与独立部署

- `zhouyi_page_api.py` 是统一 facade：`/page/v1/mc/*` 委托 `McManagerWebApi`，`/page/v1/memory/*` 委托 Memory handlers，来源更新只使用 `/page/v1/sources/*`。
- `/page/v1/config/memory` 由独立的 `MemoryConfigApi` 注册，不依赖 Memory 服务是否启用、迁移成功或完成初始化，并且没有旧 `/page/*` alias。
- 旧 `/page/*` alias 只保留现有 MC、Memory 内容和 bootstrap 兼容路由；来源更新与 Memory 配置不得新增 legacy alias。
- MC 契约变更通常要同步 `web_api.py`、facade、standalone allowlist、前端 types/client 和代理测试；Memory 内容契约还要同步 `memory/core/page_api_modules`，配置契约则同步 Schema、Pydantic 模型、配置 API 和前端 Schema parser。
- `standalone_web.py::_ALLOWED_API_ROUTES` 是独立页唯一代理白名单；它精确开放 bootstrap、MC、来源更新、Memory 内容和 Memory 配置所需 API，新增路由仍必须显式加入固定白名单。
- React 的 Memory capability 不再按 standalone 隐藏；独立页与 AstrBot 内嵌页均可使用 Memory 概览、管理、召回测试、知识图谱和记忆配置。
- 两个入口共用同一套路由和 `pages/zhouyi-dashboard/` 构建产物；独立服务默认监听 `0.0.0.0:35020`，缺少 Dashboard 证书/私钥只会使独立服务启动失败。
- 独立代理要求 `astrbot_dashboard_jwt` Cookie；POST 还要求 `Origin === DEFAULT_PUBLIC_ORIGIN`、可选 `Sec-Fetch-Site` 为 `same-origin`、JSON 且请求体不超过 64 KiB。
- 代理目标固定为本机 AstrBot Dashboard；转发原始 `Cookie`、可选 `Accept` 和 POST `Content-Type`，明确不转发 `Authorization` 或 `X-API-Key`。

## Memory 配置页约束

- Memory 页面 Schema 唯一来源是根 `_conf_schema.json` 的 `memory` 节；API 额外从 `MemoryConfig.model_json_schema()` 提取数值约束，并只暴露 Provider 的 `id/model/type`。
- 保存必须提交完整 `memory` 对象和 `expected_revision`；后端做 Schema 类型/选项检查、严格 Pydantic 校验、Provider 白名单检查和 SHA-256 revision 冲突检查。
- 配置迁移会保留未知字段，但配置 POST 会拒绝 `_conf_schema.json` 中不存在的字段；新增或删除字段必须同步 Schema、`MemoryConfig`、迁移策略和契约测试。
- 前端“基础与模型、会话与召回、记忆处理、图记忆与权重、数据维护”五类只是 `memoryConfigSchema.ts` 的展示分组，不改变后端 key、嵌套结构或持久化格式；未知未来区段显示到“其他设置”。
- `fake_tool_call_deepseek_v4` 与 `system_prompt` 只保留后端历史配置兼容并在运行时降级；它们不得重新出现在 `_conf_schema.json` 选项、提示或前端下拉框中。旧值应迁移或显示为实际回退值，不能让下拉框长期显示“请选择”。
- 只有保存并重载成功、取消和普通重新加载完成等非阻塞结果可以用 3–5 秒自动消失的 `role=status` toast；保存进度不得提前消失，“需手动重载/重载失败/确认超时”必须作为持久警告，且不能标为“操作完成”。
- 字段校验、请求错误、后台刷新错误、持久重载警告和 revision 冲突使用内联或不自动消失的 `role=alert`；首次阻断式加载失败使用 `DataState`，revision 冲突必须保留草稿并提供“重新加载并保留草稿”。
- 草稿 dirty 或正在保存时会锁定 HashRouter 导航、群组选择和 `beforeunload`；不要绕过这条未保存更改保护。

## 迁移与持久化

- 配置迁移优先现有 `memory` 值，再补根 `living_memory` 和旧独立配置；迁移后删除根 `living_memory` 节、保留旧独立配置文件，并且首次改写根配置前只创建一次备份。
- Memory 运行目录是 `<plugin-data>/memory`；旧 `StarTools.get_data_dir("astrbot_plugin_livingmemory")` 只能读取，不能写入或删除。
- Memory 首次发布使用同级 `.memory.staging`、`.migration-state.json`、严格 identity、READY 恢复和 Linux 原子 no-replace；符号链接、路径重叠或未知 staging 必须拒绝。
- SQLite backup、WAL/SHM 处理和完整性验证只在私有 snapshot/staging 中进行，不能直接连接运行中的已发布数据库做迁移验证。
- 已发布 READY 目录是可变运行数据；迁移记录中的哈希是审计证据，运行期修改后不应刷新或强制匹配旧哈希。
- Minecraft 使用一个 `<plugin-data>/mc_manager.sqlite3`，按 `group_id` 隔离；schema 只做事务式增量迁移，并拒绝高于当前支持版本的数据库。
- 顶层旧 `<group_id>.json` 成功或失败迁移后都保留；不要删除或改名作为“清理”。
- 降低 `max_history_points` 必须先生成持久 preview，再显式确认；设置更新与裁剪在同一 SQLite 事务中，preview 过期或计数变化必须重做。
- 降低自动清理天数只改变候选，不在保存时删除；手动 `/mccleanup` 不受自动清理开关限制。
- 整点采样按 `(host, lookup timeout, status timeout)` 去重，而不是仅按 host；每个目标仍按所属群组的有效历史上限写入。

## 前端与生成产物

- `web/packages/ui` 是共享组件包，`web/packages/app` 是 React/Vite 应用；app 使用 `HashRouter`。
- `npm run build --prefix web` 会清空并重建已跟踪的 `pages/zhouyi-dashboard/`；哈希资源新增、旧资源删除和 `index.html` 必须一起提交，禁止手改构建产物。
- `web/packages/ui/dist/` 是忽略的生成目录；旧 `pages/mc-manager/` 与 `pages/livingmemory-dashboard/` 不应恢复。
- Vite dev 固定 `0.0.0.0:35021`，默认代理到 `https://127.0.0.1:35015`；只用 `VITE_API_PROXY_TARGET` 覆盖目标。
- `VITE_MOCK_API=true` 走 mock client；非 mock 模式优先使用 `window.AstrBotPluginPage` bridge，否则才走 standalone fetch。

## 工作区卫生

- 仓库内缓存、测试输出、临时下载和迁移实验只放 `temp/`；海外下载设置 `HTTP_PROXY`、`HTTPS_PROXY` 为 `http://127.0.0.1:7890`。
- Python 验证必须设置 `PYTHONPYCACHEPREFIX`；不要提交或覆盖现有 `__pycache__`、`.pyc`。
- 根 `.gitignore` 忽略 `/AGENTS.md`，但该文件已经被 Git 跟踪；修改仍会出现在 diff 中并可用普通 `git add AGENTS.md` 暂存。
