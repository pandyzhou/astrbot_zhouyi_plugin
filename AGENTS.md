# AGENTS.md

- 这是一个 AstrBot 插件仓库，不是可安装的 Python 包；根目录没有 `pyproject.toml`、`requirements.txt`、CI、lint 或测试配置。
- 运行入口是根目录 `main.py`。AstrBot 通过继承 `Star` 且带 `@register(...)` 的类加载插件；命令处理器用 `@filter.command(...)` 注册，并在异步 handler 中 `yield event.*_result(...)` 回复。
- `metadata.yaml` 是插件展示/发布元数据；改插件名称、描述、版本、作者或仓库地址时，同时核对 `main.py` 里的 `@register(...)` 参数，避免元数据和运行时注册信息不一致。
- `README.md` 仍是 AstrBot Hello World 模板说明；除其中 AstrBot 文档链接外，不要把 README 当作当前插件行为的可靠来源，优先信 `main.py` 和 `metadata.yaml`。
- 最小验证命令：`python -m py_compile main.py`。这只检查语法，不需要 AstrBot 运行时依赖；功能验证需要在本地 AstrBot 中加载/重载插件后触发对应指令，例如当前的 `/helloworld`。
- 不要提交或依赖 `__pycache__/` 等生成缓存；仓库已有 `.gitignore` 覆盖常见 Python 缓存和工具缓存。
