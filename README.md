# AstrBot 周易 Minecraft 管理插件

![插件图标](logo.png)

面向 AstrBot 的 Minecraft Java 版服务器管理插件。支持群组独立的服务器配置、实时状态查询、在线玩家展示、整点在线人数趋势、SQLite 持久化，以及内嵌/独立 Web 管理界面。

仓库：`https://github.com/pandyzhou/astrbot_zhouyi_plugin`

## 功能

- 按 `group_id` 隔离管理多台 Minecraft 服务器
- 查询在线状态、版本、延迟、在线人数、人数上限、玩家列表和服务器图标
- 使用图片回复展示服务器状态
- 每小时自动采样在线人数，同一地址跨群组只查询一次
- 通过 `/mcdata` 输出最近 1～168 小时的单服趋势仪表卡或全服趋势汇总图
- 使用 SQLite 保存服务器、状态和趋势数据
- 自动迁移旧版 JSON 数据，无需重新添加服务器
- 每台服务器默认保留最多 10000 个趋势采样点，可在 WebUI 调整
- 清理长期未查询成功且没有近期趋势记录的服务器
- 提供 AstrBot Plugin Page 和独立 HTTPS WebUI
- WebUI 支持服务器增删改查、运行配置、可选自动状态刷新和交互式趋势图
- 提供 `mcmod_search` LLM 工具，搜索 MC百科中的模组、整合包、物品/方块和教程

## 环境要求

- 已安装并可正常运行的 AstrBot
- Python 3.10 或更高版本
- Minecraft Java 版服务器地址
- 独立 WebUI 需要 AstrBot Dashboard 已配置 HTTPS 证书

Python 依赖见 `requirements.txt`：

```text
mcstatus
aiohttp
pillow
aiofiles
beautifulsoup4
```

## 安装

### 从 AstrBot 安装

在 AstrBot 插件管理界面安装本仓库，随后重载插件或重启 AstrBot。

### 手动安装

将仓库放入 AstrBot 插件目录：

```text
data/plugins/astrbot_zhouyi_plugin
```

安装依赖：

```bash
pip install -r requirements.txt
```

重载插件后，在群聊中发送：

```text
/mchelp
```

## 命令

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `/mchelp` | 无 | 查看插件帮助 |
| `/mc` | 无 | 查询当前群保存的全部服务器并返回状态图片 |
| `/mcadd` | `名称 地址 [force]` | 添加服务器；`force=True` 时跳过预查询失败限制 |
| `/mcget` | `名称/ID` | 获取服务器地址 |
| `/mcdel` | `名称/ID` | 删除服务器及对应趋势数据 |
| `/mcup` | `名称/ID [新名称] [新地址]` | 更新服务器名称或地址 |
| `/mclist` | 无 | 列出当前群的服务器 ID、名称和地址 |
| `/mccleanup` | 无 | 手动清理长期无有效记录的服务器 |
| `/mcdata` | `[名称/ID] [小时数]` | 指定服输出趋势仪表卡；不指定服务器时输出全服趋势汇总图，默认 24 小时 |

## MC百科 LLM 搜索工具

插件向 AstrBot 的大模型工具系统注册 `mcmod_search`。该工具只返回结构化 JSON，供 LLM 继续组织回答；它不会主动发送消息，也不会生成图片。

| 参数 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `query` | string | 无 | 去除首尾空白后长度 1～100 |
| `category` | string | `all` | `all`、`mod`、`modpack`、`item`、`tutorial` |
| `page` | integer | `1` | 1～20，不接受布尔值 |
| `limit` | integer | `5` | 1～10，不接受布尔值 |

返回状态包括：

- `success`：成功解析并返回结果
- `empty`：搜索结果容器存在，但当前页没有结果
- `invalid_argument`：参数不符合约束，并在 `error` 中说明原因
- `timeout`：请求超时
- `rate_limited`：MC百科返回 HTTP 429
- `upstream_error`：连接失败或上游 HTTP 异常
- `parse_error`：页面结构缺失或结果节点无法解析

返回示例：

```json
{
  "status": "success",
  "query": "机械动力",
  "category": "mod",
  "page": 1,
  "limit": 2,
  "count": 1,
  "results": [
    {
      "title": "机械动力 (Create)",
      "url": "https://www.mcmod.cn/class/2021.html",
      "summary": "以机械动力和自动化为核心的模组。",
      "type": "mod"
    }
  ]
}
```

### 添加服务器

```text
/mcadd 生存服 play.example.com
/mcadd 模组服 play.example.com:25565
/mcadd 临时服 127.0.0.1:25565 True
```

服务器地址只允许字母、数字以及 `.:-`。默认会在保存前进行一次连接测试；仅在确认地址正确但预查询无法通过时使用 `True` 强制添加。

### 查询服务器

```text
/mc
```

插件会逐台查询当前群保存的服务器，并以图片形式返回：

- 服务器名称与 ID
- 主机地址
- 在线状态
- 版本和延迟
- 当前/最大玩家数
- 在线玩家列表
- 服务器图标

成功查询会更新最后成功时间并写入趋势采样；失败会更新最后失败时间和连续失败次数。启用自动清理时，查询完成后会按当前群组配置执行一次清理。

### 更新与删除

```text
/mcup 1 新名称 new.example.com:25565
/mcup 生存服 新名称
/mcdel 1
/mcdel 生存服
```

名称和数字 ID 均可作为服务器标识。删除服务器时，对应趋势数据会一并删除且无法恢复。

### 在线人数趋势

```text
/mcdata
/mcdata 24
/mcdata 生存服 48
/mcdata 2 72
```

规则：

- 不传参数：全部服务器，最近 24 小时
- 只传小时数：全部服务器，使用指定时间范围
- 传名称或 ID：指定服务器
- 小时数会限制在 1～168 之间
- 指定服务器时生成单服趋势仪表卡；不指定服务器时按配置顺序生成全服汇总图，每页最多 4 台
- 当前不可达的服务器会跳过；可达但窗口内无历史的服务器仍会保留，并明确显示“窗口内无数据”
- 1～24 小时使用柱状图，25～72 小时使用面积图，73～168 小时使用严格缺失语义的 3 小时聚合面积图

如果单个数字同时是已存在的服务器 ID，会优先按服务器 ID 处理。

## Web 管理界面

插件提供两种入口，使用同一套后端 API 和数据。

### AstrBot Plugin Page

在 AstrBot Dashboard 的插件页面中打开 Minecraft 管理界面。该入口复用 AstrBot 页面桥接和登录状态。

### 独立 HTTPS 页面

插件启动时默认监听：

```text
https://<AstrBot 主机>:35020/
```

独立页面包含：

- 群组切换
- 服务器添加、编辑和删除
- 可配置页面打开后是否自动查询实时状态
- 手动刷新全部或单台服务器
- 在线/离线状态、版本、延迟、玩家和图标展示
- 最近 1～168 小时的交互式在线人数趋势图
- 鼠标、触屏和键盘可操作的趋势提示卡片
- 支持键盘导航的自定义下拉框
- 全局默认与群组覆盖的运行配置页

独立页面不会向浏览器暴露新的 API 密钥。它要求浏览器已经登录 AstrBot Dashboard，并通过白名单代理转发受支持的管理请求。

### 独立页面配置要求

服务会读取 AstrBot 主配置中的 `dashboard` 配置：

- 上游 Dashboard 端口
- 是否启用 HTTPS
- `dashboard.ssl.cert_file`
- `dashboard.ssl.key_file`

未配置证书和私钥时，独立 HTTPS 服务无法启动，但 AstrBot 内嵌 Plugin Page 不受影响。

独立服务的公开来源由 `standalone_web.py` 中的 `DEFAULT_PUBLIC_ORIGIN` 定义。部署时需要将其设置为实际公开来源，否则 POST 请求会被同源校验拒绝。请勿在公共文档中记录生产环境的域名或完整访问地址。

## 运行配置

WebUI 顶部的“运行配置”页面提供全局默认值和当前群组覆盖。群组配置可以逐项恢复为继承全局值。

| 配置项 | 默认值 | 范围 |
| --- | ---: | ---: |
| 每台服务器最大趋势点数 | 10000 | 168～100000 |
| 启用趋势采样 | 开启 | 开/关 |
| 启用查询后自动清理 | 开启 | 开/关 |
| 自动清理判定天数 | 10 天 | 1～365 天 |
| 页面打开时自动刷新状态 | 开启 | 开/关 |
| 趋势页默认范围 | 24 小时 | 1～168 小时 |
| Minecraft 地址解析超时 | 3 秒 | 0.5～30 秒 |
| Minecraft 状态查询超时 | 7 秒 | 1～60 秒 |
| 最大并发查询数 | 5 | 1～20，仅全局 |

降低趋势点上限时，页面会先预览受影响服务器和待删除点数。只有显式确认后，配置更新和历史裁剪才会在同一 SQLite 事务中执行；预览过期或数量变化时必须重新确认。

降低自动清理天数只会改变候选规则，保存配置本身不会立即删除服务器。手动 `/mccleanup` 始终可执行，不受自动清理开关影响。

## 数据存储

插件使用 AstrBot 提供的插件数据目录，并将数据保存到：

```text
mc_manager.sqlite3
```

所有群组共享同一个数据库文件，通过 `group_id` 隔离数据。主要内容包括：

- 群组的下一个服务器 ID
- 服务器名称和地址
- 创建、最后成功和最后失败时间
- 连续失败次数
- 整点在线人数趋势
- 旧数据迁移记录

SQLite 启用了：

- WAL 日志模式
- 外键约束
- 30 秒 busy timeout
- 删除服务器时级联删除趋势记录

### 旧版 JSON 迁移

初始化存储时，插件会检测旧版群组 JSON 文件并自动导入 SQLite。成功导入后会写入迁移记录，避免重复导入。

升级前仍建议备份插件数据目录，但不需要删除旧数据或重新添加服务器。

## 趋势采样

插件启动后会运行后台采样任务：

1. 扫描全部群组的服务器配置和有效运行配置
2. 跳过关闭趋势采样的群组
3. 按主机地址和查询超时组合去重
4. 在全局最大并发限制内查询服务器
5. 将结果写入所有引用该查询目标的群组和服务器
6. 等待下一个整点继续采样

单台服务器默认保留最近 10000 个趋势点，实际上限由全局或群组配置决定。后台任务异常时会记录日志并自动重试；修改配置会唤醒调度器重新读取设置，但不会在同一整点重复采样。

## 自动清理

默认清理阈值为 10 天，可通过全局或群组运行配置调整。清理判断会综合：

- 服务器最后一次查询成功时间
- 服务器最近一条趋势记录时间

只有两者都早于清理阈值时，服务器才会成为清理候选。

启用自动清理时，插件会在 `/mc` 查询结束后执行；无论自动清理开关是否开启，都可以通过以下命令手动执行：

```text
/mccleanup
```

删除操作会同时删除该服务器的全部趋势数据。

## 项目结构

```text
.
├── main.py                     # AstrBot 插件入口、命令和后台任务
├── web_api.py                  # Plugin Page 后端 API
├── standalone_web.py           # 独立 HTTPS 静态服务与受限代理
├── script/
│   ├── get_server_info.py      # Minecraft 状态查询
│   ├── get_img.py              # 状态图片生成
│   ├── bar_chart.py            # 趋势仪表卡与全服汇总图生成
│   └── json_operate.py         # SQLite 存储与旧 JSON 兼容层
├── web/
│   └── packages/
│       ├── app/                # React 管理界面
│       └── ui/                 # 共用 UI 组件
├── pages/mc-manager/           # WebUI 生产构建产物
└── tests/                      # 存储、API、生命周期和 Web 服务测试
```

## 开发

### Python 测试

在包含 AstrBot 运行依赖的环境中执行：

```bash
python -m unittest discover -s tests
```

测试覆盖：

- SQLite 存储和旧 JSON 迁移
- Web API 契约
- 独立 HTTPS 服务与代理
- 插件启动/停止生命周期
- `/mcdata` 参数解析和图片输出

### WebUI

需要 Node.js 20.19 或更高版本。

```bash
cd web
npm install
npm run typecheck
npm run build
```

开发模式：

```bash
cd web
npm run dev
```

构建会更新：

```text
web/packages/ui/dist/
pages/mc-manager/
```

## 常见问题

### 页面首次打开为什么会查询服务器？

服务器配置接口只返回已保存的数据，不包含实时版本、延迟和玩家信息。默认情况下，WebUI 读取服务器列表后会执行一次全量状态查询；可以在“运行配置”中关闭“进入服务器页时自动刷新”。手动刷新按钮始终可用于重新查询或失败重试。

### 独立页面提示需要登录 AstrBot

先在相同主机的 AstrBot Dashboard 完成登录，再重新打开或刷新 35020 页面。独立页面只接受有效的 AstrBot Dashboard 会话。

### 独立页面无法启动

检查 AstrBot 主配置中的 Dashboard HTTPS 证书和私钥路径。独立服务默认要求有效的 `cert_file` 和 `key_file`。

### 添加服务器提示预查询失败

确认地址和端口正确，并检查 AstrBot 所在主机是否能够访问目标 Minecraft 服务器。只有在确认地址无误时才使用 `True` 强制添加。

### 趋势图没有数据

趋势数据按整点采样。刚添加的服务器可能尚未产生足够的历史记录；执行 `/mc` 或在 WebUI 刷新状态也会追加当前在线人数记录。

### 为什么看不到完整玩家名单？

Minecraft 状态协议返回的是服务器提供的玩家 sample，服务器可以关闭或限制该字段。因此在线人数可能大于页面中列出的玩家名称数量。

## 安全说明

- 独立页面只代理白名单中的插件 API
- POST 请求强制使用 JSON，并限制请求体大小
- 写操作检查 `Origin` 和 `Sec-Fetch-Site`
- 要求有效的 AstrBot Dashboard 登录 Cookie
- 静态文件服务拒绝路径穿越和符号链接逃逸
- 响应包含 CSP、`X-Frame-Options` 等安全头
- 浏览器端不保存新的 AstrBot 密钥

## 支持平台

插件元数据声明支持：

- aiocqhttp
- Telegram

实际命令可用性取决于对应平台是否支持群组 ID、图片消息和 AstrBot 命令解析。

## 许可证

本项目使用 [MIT License](LICENSE)。

## 致谢

服务器信息查询和图片展示功能基于早期 `mcgetter` 实现继续修改；感谢原作者及相关开源项目。
