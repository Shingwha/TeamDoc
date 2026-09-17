# TeamDoc 架构与代码地图

面向要改这份代码的人(以及下一个 AI 会话)。只讲**当下**的设计:没有历史沿革、没有迁移
章节、没有"以前是怎么做的" —— 需要知道过去请查 git 历史。

## 1. 它是什么

小团队自部署的文档 + 云空间服务。单进程、单 worker、SQLite、零构建步骤的前端。
面向 30 人内网规模。

```
cd lite/server && uv sync && uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker
```

部署、环境变量、备份与恢复见 `DEPLOY.md`;Markdown 语法契约见 `MARKDOWN.md`。

## 2. 代码地图

### 后端 `lite/server/`(平铺模块,互相直接 import)

| 模块 | 职责 |
|---|---|
| `config.py` | **全部环境变量的读取与校验**;有效限额字典(管理后台展示它) |
| `errors.py` | **全部 HTTP 错误码常量 + `err()` / `bad_request()`** |
| `serialize.py` | **全部响应形状**:`user_json/project_json/doc_json/file_json/folder_json` 的 face,以及 `iso()` 与 `avatar_color()` |
| `ids.py` | ULID 生成与形状判定;`IdPath`(路径参数类型) |
| `models.py` | 表结构(唯一来源)、连接与 PRAGMA、请求级会话、跨资源小工具(`is_live`/`storage_basename`/`file_abspath`/`disk_free_bytes`/`build_tree`/`collect_subtree`/`location_json`…) |
| `schema.py` | 建表 + 结构自检(不一致就拒绝启动) |
| `auth.py` | 会话与 PAT、密码、登录节流与审计、**权限金字塔**、取参与校验工具、用户管理与同事目录 |
| `projects.py` | 项目 CRUD、成员、发现广场、统计聚合(`batch_stats`)、可见项目集合 |
| `docs.py` | 文档树、正文读写与版本、反链、移动 |
| `files.py` | 云空间:上传下载、文件夹、打包、分享链接、项目占用 |
| `trash.py` | 回收站:文档/文件夹/文件的删、恢复、彻底删除 + 列表(一个树引擎 + 薄端点) |
| `search.py` | 搜索;`q` 为空即"最近动态" |
| `admin.py` | 管理后台 HTTP 入口:存储统计、孤儿清理、会话与登录审计、备份/恢复 |
| `backup.py` | 备份与恢复引擎(项目里唯一的后台线程:定时备份) |
| `refs.py` | **引用契约的服务端半边**:正文里两种引用形态的解析与生成 |
| `ws.py` | 文档协同 WebSocket(每条消息重新复核权限) |
| `media.py` | mime 判定与 inline 白名单(**安全边界,勿放宽**) |
| `throttle.py` | 凭据尝试的节流机制(策略在 `auth.py` 定义) |
| `logsetup.py` / `watchdog.py` | 非阻塞日志;事件循环卡顿时的线程栈转储 |

### 前端 `lite/web/`(静态页,无构建步骤)

`index.html` 里的 `<script>` 顺序**就是依赖图**,加文件要放对位置。

| 文件 | 职责 |
|---|---|
| `js/endpoints.js` | **全部后端路径的构造处**(只有这里出现 `/api/...` 字面量) |
| `js/api.js` | fetch 封装、错误信封解析、401 的全局处理 |
| `js/app.js` | hash 路由 + 外壳 + 登录视图;`App.route.*` 是路由串的构造点 |
| `js/ui.js` | 组件与工具库;角色判定镜像 `UI.can*`;`UI.fileIcon`/`canInlineMime` |
| `js/ref.js` | 引用契约的前端半边(与 `refs.py` 逐字对齐) |
| `js/markdown.js` | Markdown 渲染栈(本地 vendor:marked / Prism / KaTeX / Mermaid) |
| `js/preview.js` | 站内预览浮层;`Preview.kindOf` 是"怎么预览"的唯一判定 |
| `js/views/*.js` | 视图(`project.js` 是项目内的壳,`doc-editor.js` 是编辑器) |

### 启动顺序(`main.py`,改动前先读那一段)

日志装配 → `apply_pending_restore()`(替换库文件,**必须在建表之前**)→ `schema.init()`
→ 路由注册 → 静态托管(必须最后,否则 `/` 吃掉所有 API 路径)→ 备份线程。

## 3. 单一来源清单

改这些地方时,只需要改一处 —— 这是本仓库最重要的约定。

| 知识 | 唯一来源 | 谁在消费 |
|---|---|---|
| 表结构 | `models.py` | `schema.init` 建表与自检、备份校验 |
| 环境变量与限额 | `config.py` | 所有模块、管理后台「存储」的 limits |
| 错误码 | `errors.py` 的常量 | 后端全部错误路径、前端按 code 分支 |
| 响应形状 | `serialize.py` 的 face | 所有端点 |
| 时间戳格式 | `serialize.iso()` | 所有响应的日期字段 |
| ULID 生成与形状 | `ids.py` | 表主键默认值、`IdPath`、取参校验 |
| 权限判定 | `auth.project_role`(+ `has_role`/`ensure_project_role`/`require_role`) | 全部端点、WS、前端 `UI.can*` 镜像 |
| "已删即不存在" | `models.is_live` | REST(404)与 WS(4404) |
| 引用语法 | `refs.py` / `ref.js` | 反链、被引用标记、移动前提示、编辑器插入 |
| 文件物理名 | `models.storage_basename` | 下载、孤儿扫描、备份打包 |
| 磁盘余量 | `models.disk_free_bytes` | 上传前预检、写盘复查、存储总览、恢复前检查 |
| 预览方式 | `Preview.kindOf` | 云空间预览按钮、文档 @引用浮层 |
| 项目统计 | `projects.batch_stats` | 项目列表、广场、管理后台 |
| 后端路径 | `endpoints.js` | 全部视图 |
| 回收站响应 | `trash._result()` | 九个变更端点 |
| 登录节流策略 | `auth.LOGIN_*_POLICY` | 登录、自助改密、管理后台解锁 |

## 4. 契约

### id

ULID 字符串(26 位 Crockford base32)。**只有一种形态**:不是数字、不带前缀。
路径参数走 `ids.IdPath`(形状不对 → 400),payload 走 `auth.id_field`/`opt_id`。
不合法就是 400,不会"悄悄归一成另一种写法"。

### 引用(正文里指向站内资源)

两种形态,都是普通 Markdown 链接,**两种都要认**:

```
chip     [@标题](teamdoc://doc/{docId})    文档
         [@名称](teamdoc://file/{fileId})   文件
附件/内嵌 [名称](/api/files/{fileId}/download)
         ![名称](/api/files/{fileId}/download?inline=1)
```

- chip 是编辑器 @ 菜单给出的形态;下载链接是手写附件与内嵌图片的形态。只认一种,
  另一种引用的文件在删除时就得不到"被 N 篇文档引用"的提醒。
- 引用里**只出现资源自己的 id**(不带项目 id):项目归属会变(文档可跨项目移动),
  引用必须跟着资源走。
- 判定必须同源:服务端 `refs.py`,前端 `ref.js`;消费方(反链 / 被引用标记 / 移动前提示)
  不得各写一份正则。

### 错误

响应信封固定为 `{"detail": {"code": ..., "message": ...}}`。

- **code 是机器判据** —— 前端与 CLI 按它分支(`READ_ONLY_TOKEN` 决定提示"换个令牌",
  `JOIN_REQUIRED` 决定提示"去发现页加入")。改 code 等于改契约。
- **message 是人话** —— 文案随时可改,调用方不得嗅探其中的字样。
- 401 一律附带清会话 Cookie:会话失效后浏览器仍会重放死 Cookie,不清掉就是每个视图
  撞一次 401,而它自己不会消失。

### 权限金字塔

```
project_role          有效角色 = max(成员角色, 全局管理员兜底 ADMIN);都不是 → None
  └ has_role          角色比较只此一处
      └ ensure_project_role   不足则 403(公开项目给 JOIN_REQUIRED)
          └ require_project_role / require_role   路径参数端点用
```

- 全局管理员是有效角色的**下限**:加入一个 `join_role=VIEWER` 的项目不会把管理能力降级。
- **公开项目不产生读权限**:公开只等于"可发现 + 可自助加入",加入前一律 403。
- 个人空间对管理员也关闭(列表、搜索、按 id 直连都不给)。
- 前端 `UI.canRead/canEdit/canAdmin/canOwn` 是这套判定的镜像;视图不得自己比角色。

### 文档写入

- `PUT /content` 是整篇覆盖:**web 会话必须带 `baseVersion`**(浏览器手里有缓冲,
  不声明就会让陈旧标签页静默盖回去);PAT 缺省即覆盖(程序化写入不持有缓冲)。
- `append` 与 `versions/restore` 是服务端读-改-写,不需要基线。
- 这条规则只在 `docs.resolve_base_version` 一处判定,端点不得自己决定。
- 冲突返回 409,并把服务端当前正文(`currentVersion`/`currentContent`/`by`)一并带回;
  WS 的 `conflict` 消息同形 —— "冲突现场"是一个契约,两个传输层共用形状。

### 参数

**严格**:取值域外的参数一律 400,不静默回退默认值。整型就是 JSON 整数,布尔就是
JSON true/false。静默回退会让"传错了却看不出"的调用方一直传错,而服务端一切正常 ——
这类静默的数据偏差比一个明确的 400 难查得多。

## 5. 代码约定

1. **注释只讲当下的约束**("为什么必须这样"),不讲"以前如何""为了兼容谁"。需要历史查 git。
2. **不留兼容分支**:同一个东西不接受第二种写法。契约变了就同步改两端 —— 两端都是我们的。
3. **不留迁移代码**:结构与 `models.py` 不一致的库拒绝启动,不做就地改造。
4. **不存派生值**:表里只放无法重算的事实(例如文件类型由名字推导,不落列)。
5. **错误与响应形状**从 `errors.py` / `serialize.py` 取,不在端点里手拼。
6. **路径**从 `endpoints.js` 取,视图里不出现 `/api/` 字面量。
7. 前端组件优先复用 `UI.loadInto`(加载/错误/空态三态)与 `UI.confirmAction`(确认后执行),
  而不是每个视图各写一遍。

## 6. 已知边界

- 单 worker:SQLite 单写者。并发上传会排队;长 I/O 前必须 `release_db()`。
- 云空间列表已分页(100/页,服务端排序),单目录文件夹上限 2000。
- 单次打包上限 1000 个文件 / `ZIP_MAX_BYTES_MB`(默认 4GB)。
- 搜索与回收站单次 500 条截断(超出会少结果/少列)。
- `referenced`(被引用徽标)每次列目录都扫本项目文档正文;文档上千后应改成引用索引表。
- 文档版本按年龄分层保留(稳态约 124 条/文档),不需要手工清理。
- 不做两步验证(内网自部署 + 30 人规模,价值低于"禁用账号 + 可吊销 PAT")。

## 7. 测试

```bash
cd lite/server
uv run pytest ../tests              # 默认跳过 slow 组
uv run pytest ../tests -m slow      # 弹性/压测
```

脚手架(`tests/_harness.py`)每次会话自建隔离实例:临时数据目录 + 空闲端口 + 真实子进程,
所以测试可以随便造数据、也能测"重启后生效"这类行为。浏览器类用例需要无头 Chrome,
环境缺失时自动跳过。

改契约前先看对应测试:它们是契约的第二次声明(`test_auth_cookie` 盯会话边界,
`test_id_contract` 盯 id 与引用,`test_smoke_endpoints` 打全部路由,`test_doc_conflict`
盯写入基线)。测试文件与 `lite/tests/README.md` 是同一份信息的两个粒度。
