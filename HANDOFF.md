# TeamDoc Lite — 接手文档

> **本文档是代码库当前状态的权威说明:与代码冲突时以本文为准,并顺手修正本文档。**
> 目标是让第一次接手的人少试错 —— 所以写的是「约束、为什么、改动落在哪」,不复述代码。
> 部署与运维见 `lite/DEPLOY.md`,测试细节见 `lite/tests/README.md`。
> 更新日期:2026-09-13

## 目录与阅读路径

| 你要做的事 | 读 |
|---|---|
| 把服务跑起来、改点东西看效果 | §1、§3、§4 |
| 加一个页面 / 视图 | §4、§5.1、§5.2 |
| 加接口、改权限 | §6.1、§8 |
| 加表 / 加字段 | §6.2 |
| 动上传、下载、长连接 | §6.3 |
| 改编辑器的保存与协同 | §5.4、§7.4 |
| 上线、备份、恢复 | §7.5、`DEPLOY.md` |
| 排查线上问题 | §6.3 末尾、附录 |

## 1. 五分钟上手

```bash
cd lite/server
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker
```

- 首次启动**只建表、不预置账号**:浏览器打开 `http://localhost:8000`,跟初始化向导建第一个管理员。
- 数据全在 `server/data/`(gitignored):`teamdoc.db`、`files/`、`logs/`。`TEAMDOC_DATA_DIR` 可改 —— 测试就靠它做隔离。
- 前端**零构建**:改 `lite/web/` 下的文件刷新即生效,没有编译步骤。
- 改完要跑的验证:服务端 `cd lite/server && uv run pytest ../tests`;动过前端资源再跑 `test_page_assets.py`;JS 至少 `node --check`。
- 三个最容易踩的坑(细节见后文):**必须单 worker**(SQLite 单写者);**别直接复制 `teamdoc.db` 当备份**(WAL 未 checkpoint,复制到的是空库,要用 `VACUUM INTO`);**id 从 JSON 进来是数字、从 URL 与 `dataset` 进来是字符串**,比较或作键前先 `Number()`。

## 2. 它是什么

TeamDoc Lite:30 人小团队**自部署**知识库 —— 项目管理文档、实时协同编辑、云空间、全文搜索,界面全简体中文,依赖全本地化,内网/离线可用。

**功能**:认证(bootstrap / PAT / 用户管理 / 登录节流 + 登录状态与审计)、项目与成员(批量圈选添加、自助退出)、文档树、Markdown 源码/预览双模式编辑(手动保存 + 离开前未保存提示)、WebSocket 协同(基线校验 + 冲突逐块合并)、分层版本历史、云空间(流式上传、分页排序、网格视图、站内预览、文件夹递归删改移、跨项目移动)、回收站(文档+文件+文件夹,只列子树根)、全文搜索、最近动态、同事目录、发现广场与公开项目自助加入、单文件公开、文档/文件引用浮层、管理后台(存储总览、孤儿清理、备份恢复、项目总览、登录详情与动态)。

**明确不做**(是设计选择,不是待办):日历、字符级协同(现为 LWW + 基线校验)、S3/对象存储、通知、匿名分享链接、两步验证。

## 3. 技术栈与运行

- 后端:Python 3.13 / FastAPI + uvicorn(**必须单 worker**)+ SQLite(SQLAlchemy 2.0,WAL,`busy_timeout=5000`)。
- 前端:纯 HTML/CSS/JS,**零框架零打包**;依赖全部本地 vendor(`lite/web/vendor/`),零外网请求。
- 认证:scrypt 口令(标准库)、Cookie 会话(`td_sid`,HttpOnly + SameSite=Lax)、PAT(`tdp_` 前缀,库里只存 sha256)。不做两步验证。
- 另有一个命令行客户端(独立仓库,通过 PAT 调本 API):它只是 API 的消费方,**服务端不为它做任何特殊处理**。

### 3.1 环境变量

开发时最相关的几个(**完整表与调参说明在 `DEPLOY.md` §1.3,不要再抄一份到这里**):

| 变量 | 默认 | 作用 |
|---|---|---|
| `TEAMDOC_DATA_DIR` | `server/data` | 数据目录(库/文件/日志);测试靠它隔离 |
| `MAX_UPLOAD_MB` | 20480 | 单文件上传上限 |
| `VERSION_MERGE_MINUTES` | 0 | 同人连续保存合并为一个还原点的窗口;0 = 不合并(保存是手动的,每次保存都留还原点) |
| `BACKUP_DIRS` | 空 | 备份目标目录(逗号分隔);空 = 不启用定时备份 |

其余(端口、会话有效期、登录节流阈值、备份间隔与保留份数、磁盘保留余量、zip 上限、看门狗阈值)都有可用默认值,需要时再查。

### 3.2 id 契约

- 所有资源 id 是 **Integer 自增,起点 10000、永不复用**(users/pats/projects/members/docs/versions/folders/files)。表定义带 `sqlite_autoincrement`,删掉末尾的行也不会复用 —— 正文里指向已删资源的死链不会"复活"。起点由 `schema.seed_id_start` 幂等写入。
- JSON 里 id 是 number,前端路由边界与 `dataset` 读数是字符串(见 §5.2)。
- 路径参数非法 → 400 `VALIDATION`,资源不存在 → 404。
- **秘密与标识分离**:会话键是随机 token、PAT 只存 hash,不随标识数字化而变得可猜。

## 4. 代码地图

```
lite/server/    (平铺模块,无包;只有 11 张表,见 §6.2)
  main.py      入口:先 apply_pending_restore 再 schema.init;路由注册顺序 auth→projects→docs→files→trash→search→admin→ws→静态(不能乱);
               422→400 VALIDATION;no-cache + 安全响应头中间件
  schema.py    建表 + 双向漂移自检(§6.2) + 启动数据归一(storage_path / mime)
  models.py    11 张表 + 行工具(file_abspath / unlink_quiet / collect_subtree / build_tree / ancestor_names)
  auth.py      scrypt / 会话 / PAT / 鉴权入口(§6.1) / 登录节流与审计 / 同事目录 / 用户管理
  throttle.py  凭据尝试节流机制(键、窗口、冷却、退避、清扫),策略在 auth.py —— 登录与改密共用
  projects.py  项目 CRUD / 成员 / 发现广场 + project_json + batch_stats + visible_project_ids
  docs.py      文档树 / 正文 / 版本 / 反链(save_doc_content 由 REST 与 WS 共用)
  files.py     云空间(上传、下载、分页、zip、文件夹树、移动)+ file_json/folder_json
  trash.py     回收站三资源(删/恢复/彻底删除的树引擎)+ 列表
  search.py    LIKE 搜索 + /api/recent       media.py  mime 判定与 inline 白名单(横切)
  admin.py     管理后台 HTTP 入口             backup.py 备份恢复 + 全项目唯一后台线程
  ws.py        协同 WebSocket                 logsetup.py 非阻塞日志   watchdog.py 事件循环停滞转储
lite/web/       (静态 SPA;index.html 的 script 标签顺序即依赖图)
  css/  tokens.css 唯一尺寸颜色来源 → base.css 重置 → components.css 组件样式 → app.css 壳与视图 → editor.css 编辑器
  js/   api.js fetch 封装({detail:{code,message}}→ApiError,401→#/login;apiText/apiUpload) · theme.js ·
        ui.js 组件库(唯一入口,禁止另造) · markdown.js 全站唯一渲染路径(marked+转义+KaTeX+Prism) ·
        preview.js 预览浮层(云空间预览 / @引用浮层 / 历史版本共用) ·
        diff.js 行级差异 + 逐块合并(冲突弹窗用) · doceditor.js 编辑器增强(引用、斜杠面板) ·
        app.js 路由 + 壳 + 侧栏 · views/ 各视图(project 壳、doc-tree、doc-editor、drive、search、discover、admin、settings、recent)
lite/tests/     pytest 套件(§9)        lite/DEPLOY.md  部署运维
```

**改动落在哪**

| 要做的事 | 去哪 |
|---|---|
| 加 UI 组件 / 列表行 / 按钮 | `js/ui.js`(唯一入口)与 `css/components.css` |
| 加项目内模块(侧栏多一项) | `app.js` 的 `PROJECT_NAV` 加一项 + 写 `window.Views.<name>` —— 路由、侧栏子项、高亮、tab 记忆全自动生效 |
| 加接口 / 改权限 | 对应域模块 + §6.1 的鉴权入口;前端判定用 `UI.canRead/canEdit/canAdmin/canOwn` |
| 加表 / 加字段 | `models.py`(§6.2,没有迁移机制) |
| 尺寸 / 颜色 | `css/tokens.css`(唯一来源) |
| 部署 / 备份 / 反代 | `DEPLOY.md` |

## 5. 前端约定

### 5.1 三层与组件库(新增 UI 必读)

| 层 | 位置 | 规矩 |
|---|---|---|
| 令牌 | `css/tokens.css` | 唯一尺寸与颜色来源(`--ctl-* / --sp-* / --row-* / --fs-* / --icon-* / --state-* / --w-* / z-index` 六档)。新样式里出现裸像素 = 待补令牌的信号 |
| 组件样式 | `css/components.css` | 视图只引用 class,不自定尺寸 |
| 组件工厂 | `js/ui.js` | **唯一组件入口,禁止另造**。标记类返回 HTML 字符串,需接线的返回 DOM。含浮层基座 `floatingLayer`/`menuList`、树工厂 `tree`/`walkTree`、`confirmAction`、`segWire/segSet`、`personPicker`、`download`、`pref`、`errorBanner`/`emptyHtml`、`crumbs`、`sortHead`、`numId`/`sameId` |

- **两条列表族,先判型再选,不要新造第三种**:需要列对齐 → `.data-table`(`UI.tableHead`/`tableRow`,表头与数据行引用同一组 `--tpl`,末列操作固定宽);图标/头像 + 文字 → `.list-row`(`UI.listRow`)。
- 按钮两档:`--ctl-xl`(40px 页面级)/ `--ctl-m`(32px 行内)。胶囊形与 `.chip` 统一;悬停洗色用 `--state-*` 的 `color-mix`。
- 颜色**全静态、零运行时算色**(§5.3);文件类型色用主题无关的 `--file-*`,不复用主题角色。
- 权限判定用 `UI.canRead / canEdit / canAdmin / canOwn`(**唯一入口,勿手写 `roleRank(myRole) >= N`**)。它们直接比级 `myRole` —— 那是服务端 `project_role` 的综合结果(成员→成员角色;非成员全局管理员→ADMIN;**其余含未加入的公开项目→null**),所以无需再看 `isMember`:非成员根本没有角色,不可能误过 EDITOR 及以上判定,而非成员管理员必须拿到管理入口(接管失联项目)。`isMember` 只回答"我是不是真成员"(退出项目、成员列表这类"真成员专属"用)。

### 5.2 id 归一与视图状态

- **id 一律 `Number(...)` 之后再比较或作键**。`p.id` 是 JSON 数字,而 `segs`(URL 切分)与 `dataset.*` 都是字符串 —— 不归一就是"写入的键读不到、相等判断恒 false",表现为"点了没反应 / 选中态不生效"。凡以 id 为键的 Set/Map 与 id 相等比较,一律先归一(id 整数化后已在侧栏展开、文档树折叠、历史版本选中、移动对话框四处踩过)。
- 侧栏 = 品牌行 + 搜索框(折叠态顶替为搜索钮)+ 项目树 + 入口 + 底部用户卡片。**项目行点击只展开/收起、不导航**,进项目必须点子项。
- 展开态:内存 Map 是唯一真相,只有三处写入(用户点击、首渲染种子、新建项目后);**路由变化只改高亮,绝不动展开态**;刷新回到种子状态。
- 两态(72px 图标栏 / 240px 完整)由 `#shell.side-collapsed:not(.nav-open)` 一组 CSS 承载 —— `:not(.nav-open)` 是关键:抽屉打开时整组规则自然失效,无需反向覆盖。窄屏(≤960px)恒折叠且**不写 localStorage**(不污染宽屏偏好)。
- 细节:搜索聚焦要 `requestAnimationFrame` 延后一帧;折叠钮 title 随屏宽同步(`syncCollapseBtn`)。

### 5.3 主题

`html[data-theme]`(light/dark)× `html[data-color]`(blue/purple/green/orange/pink/teal)。色值全部静态写在 `tokens.css`,零运行时算色;首屏由 `index.html` 的内联脚本在样式加载前恢复,防闪烁。编辑器与预览的配色也全走令牌(Prism 用 `color-mix` 派生),**主题切换不需要任何 JS 处理**。入口在用户卡片菜单「外观」。

### 5.4 编辑器

- 顶栏:标题输入 + 保存状态 + **保存按钮(仅脏时可用)** + 在线头像 + `编辑|预览` 分段 + 历史。**保存是手动的**(按钮或 `Ctrl/Cmd+S`),没有自动保存,标题与正文同一套动作。状态栏文案由 `renderStatus()` 单点决定:冲突 > 保存中 > 失败 > 未保存 > 已保存(时间)> 只读。
- **没保存就离开会被拦**:脏时切文档/切路由弹「保存并离开 / 放弃改动并离开 / 取消」,关标签走 `beforeunload`(机制见 §7.4)。
- 编辑态 = 无边框等宽 `<textarea>`(源码即真相,无块模型、无 WYSIWYG;曾试过 Vditor,已弃用);预览态 = `MdRender`。VIEWER 恒预览且禁用编辑。模式记忆在 `td:doc-mode`。布局:单一滚动容器 `.editor-scroll`;`#view.view-fill` 满出血,**新元素进编辑器列必须自带水平内边距**;窄屏 ≤720px 树上正文下。
- 引用:`@` 或 `[[` 弹浮层,插入 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`;**图片文件例外**,插原生 `![名称](/api/files/{id}/download?inline=1)`。点击统一走 `preview.js` 浮层(@文档 → 正文摘要 + 位置/字数 + 「打开全文」;@文件 → 预览 + 归属位置 + 「在云空间中查看」+ 下载)。**无序列化/反序列化层**。
- `/` 菜单:行首插入块(标题/列表/引用/代码/分割线/传图);**浮动工具栏只做文字格式,不放上传**。上传(粘贴/拖拽/菜单)自动归入项目根目录的「文档附件」文件夹(promise 缓存)。
- **大纲功能已整体移除**(scrollspy 双形态,交互不佳):要重做请从零设计,勿恢复旧代码。

## 6. 服务端约定

### 6.1 鉴权:只用这些入口

改权限逻辑不要手写 `ROLE_RANK.get(...) < ...`:

| 入口 | 用途 |
|---|---|
| `current_user` / `require_admin` | 登录用户 / 全局管理员 |
| **`verify_credentials(request, db, ident=, password=, user=)`** | **口令校验唯一入口**:冷却检查(账号 + 来源)→ 恒定耗时校验(账号不存在也跑一次 scrypt,不留枚举时序)→ 失败记账 + 登录审计 → 成功清账号计数。命中节流抛 429 + `Retry-After`。`_verify_password` 是模块私有,**新增凭据端点必须走这里**,否则节流/审计/反枚举必漏 |
| `pat_write_guard`(挂在每个 APIRouter 上) | PAT write scope 全站守卫:非 GET + 只读 PAT 一律 403。挂 router 上意味着**新增写端点不可能漏挂** |
| `require_project_role("EDITOR")` / `require_doc_role` / `require_file_role` / `require_folder_role` | 依赖注入式取资源并校验(不存在 → 404;`for_trash=True` 供回收站端点,409 由端点自己回) |
| `ensure_project_role(db, ctx, pid, required)` | 已拿到资源对象时用(不足 → 403) |
| `get_project_or_404` / `project_role` / `is_project_member` | 取项目 / 只查角色不抛错 / **真实成员关系**(不含管理员;用于"真成员专属"入口) |
| `is_project_owner_or_admin` | **OWNER 级管辖权**:真所有者或全局管理员。授 OWNER、删项目等"接管"语义一律走它,勿内联 `is_admin` 特判;个人空间保护在端点里先于本判定。前端对应 `UI.canOwn` |

- **判定顺序:不存在 → 404,权限 → 403,状态 → 409**。授权必须先于状态判定,否则非成员能靠 403/409 的差异探测他人资源。
- **公开项目不产生任何读权限**:`project_role()` 里没有 public 分支,非成员一律无角色 —— 文档树/读写/云空间/回收站/搜索/WS 全经它,一处即全站收紧。`JOIN_REQUIRED` 的拒绝文案也出在 `ensure_project_role` 这同一处。
- 公开项目的自助加入**唯一入口**是 `POST /api/projects/{id}/join`(与 `leave` 对称);成员关系的写入实现只有 `projects._create_membership` 一处(管理员添加与自助加入共用),角色档位来自 `project.join_role`,值域 `JOIN_ROLES = (VIEWER, EDITOR)` —— **自助加入拿不到管理权**。
- 会话元数据:创建时记 `ip` / `user_agent`,`session_context()` 以 >60s 的频率刷 `last_seen_at`(与 `pat.last_used_at` 同一惯例)。**"在线" = 未过期且最近活跃在 `ONLINE_WINDOW_SECONDS`(300s)内**,用户列表、登录详情抽屉、诊断共用这一口径。

### 6.2 数据库结构演进

- **结构的唯一来源是 `models.py`**(11 张表)。启动时 `schema.init()` 做两件事:`create_all` 建缺失表;双向漂移自检(库有模型无 → 可空列提醒、NOT NULL 无默认列阻断插入;模型有库无 → 直接启动失败)。有漂移就**拒绝启动**并给可执行的修复指引。
- **没有迁移机制**(曾有过,已整体删除:"迁移历史 + 模型"两套来源必然漂移且静默)。遇到漂移:停服 → 删数据目录 → 重启,库按 `models.py` 重新长出来。
- **加字段**:只在 `models.py` 加,旧库缺列按自检提示重建。**删字段**:模型删的同时库里的列也必须消失(重建,或 SQLite 3.35+ `ALTER TABLE ... DROP COLUMN`)——残留的 NOT NULL 列会阻断 INSERT。
- **别用复制文件的方式备份 `teamdoc.db`**:WAL 模式下未 checkpoint 的写入都在 `-wal` 里,复制到的是空库。备份一律 `VACUUM INTO`(§7.5)。

### 6.3 连接生命周期与事件循环(改传输 / 长连接端点必读)

- **事务的边界是"用库的那一段",不是"整个请求"**:FastAPI 的 yield 依赖要等响应体发完才清理,文件响应与流式响应因此会把连接陪跑到传输结束 —— 几 GB 的下载、几小时的上传都算,客户端中途消失(睡眠/断网/暂停下载)时这条连接再也回不来。**凡是响应体要搬大量字节的端点,必须在搬之前 `models.release_db(db)`**(下载、打包、上传、恢复上传、备份下载五处已接)。
- **SQLite 不用连接池**(`poolclass=NullPool`):单文件单写者、连接廉价,而池的"上限 + 借不到就等 30 秒"会把任何一条慢连接放大成全站故障。pragma 顺序:**`busy_timeout` 必须在 `journal_mode` 之前**(反了的话,新连接切 WAL 时库正忙会直接抛 "database is locked")。
- **事件循环上不做阻塞数据库 IO**:`async def` 里直接调同步 SQLAlchemy 会冻结整个进程(HTTP + WS + 静态页全停)。WS 的库操作一律 `run_in_threadpool` + 自建短会话(`ws.py` 的 `_handshake`/`_save_content`);权限复核收敛在 `_access()`,握手与每条消息共用 —— **长连接必须逐条复查**,REST 的每请求校验覆盖不到它。
- **日志是旁路,不允许阻塞事件循环**(`logsetup.py`):调用方只做非阻塞入队,写出(uvicorn 自己的两个控制台 handler + 我们的轮转文件)全在 QueueListener 线程上,队列满就丢日志。原因见附录。回归测试 `test_logging.py`。
- 可观测性:日志与停滞转储都在**数据目录**的 `logs/` 下(`teamdoc.log` 10MB×5 轮转;`stall-*.txt` 是事件循环停滞 >`WATCHDOG_STALL_SECONDS` 时的全线程栈)、`GET /api/admin/diagnostics` 给实时快照;`get_db` 对持有 >2 秒的会话记 WARNING。排障步骤见 `DEPLOY.md` §5。

### 6.4 安全红线(线上审查修出来的,勿回退)

1. **mime 由服务端按文件名判定,不采信客户端**。inline 是显式白名单(图片/PDF/文本类);**svg/html/xhtml 与未知类型强制 attachment**,media_type 回落 octet-stream 防嗅探 —— SVG 可以带 `<script>` 同源执行,钓的是点开预览的人。
2. 服务端回吐 `canInline`/`isText`,**前端不得自写 mime 猜测**。加类型只改服务端白名单,前端自动跟上。
3. 全站安全响应头:nosniff / XFO SAMEORIGIN / Referrer-Policy。**未上 CSP**(内联脚本多,收益不抵返工)。
4. **管理端写接口的守卫有两层**:`require_admin`(看 `is_admin`)+ APIRouter 级的 `pat_write_guard`(只读 PAT 的非 GET 一律 403)。**两者缺一不可** —— 只看 `is_admin` 不查 scope 时,read-only PAT 能建新管理员直接提权。
5. **授 OWNER / 删项目要求 OWNER 级管辖权**(`is_project_owner_or_admin`):项目 ADMIN 不得自我提权(升到 OWNER 就能删项目);**全局管理员显式豁免**(信任根,否则"唯一所有者失联/被禁用"的项目会死锁:所有权移不动、项目删不掉),但个人空间对管理员仍一律关闭。
6. **公开项目加入前不可读**(`project_role` 无 public 分支);自助加入的角色只可能是 VIEWER/EDITOR(`JOIN_ROLES`),禁止把 ADMIN/OWNER 放进这一档。回收站另对非成员关闭(前端隐藏 tab)。
7. **Markdown 链接走协议白名单**(marked 只 `encodeURI`、不过滤 `javascript:`)。
8. **`files.storage_path` 只存 basename**(绝对路径与机器绑定,换机恢复会全 404 且孤儿扫描发现不了);解析统一走 `models.file_abspath()`,历史库由 `schema.normalize_storage_paths()` 启动时幂等回填。
9. **备份生成持 `BACKUP_LOCK`**,按**快照里的 files 清单**打包,不扫盘 —— 否则半截上传或刚删的文件会掺进来。
10. 输入边界:offset 双向限幅(SQLite INTEGER 溢出会 500)、上传 commit 失败必 unlink 已落盘文件、zip 总字节上限、`_prune_versions` 分批删除(避开 SQLite 变量数上限)。
11. 旧内核防御前置(如 `MediaQueryList.addListener` 回退),否则老浏览器白屏;api.js 统一超时 + 网络错误中文化;模态框随路由关闭。
12. **登录节流必须是双维度**(`throttle.py` + `auth.py` 的 `LOGIN_*_POLICY`):账号维度挡"盯着一个人猛试",来源 IP 维度挡"一个来源轮着试很多账号"。三条不能退:①**状态落库**(进程内计数会让"重启即重置"成为绕过路径,有专门断言);②**不存在的邮箱同样计数、同样锁定**(否则"锁没锁"就是账号存在性探测器);③**账号不存在也跑一次 scrypt**(否则响应耗时就是探测器)。冷却期内不跑 scrypt、不累加计数;成功登录只清账号维度、不清来源维度(否则一个有效账号就能重置来源桶)。
13. **管理端展示的会话标识是 `sha256(token)`(ref),真实 token 永不出库** —— 管理页面会进截图、浏览器历史、工单,可冒用的凭据不该出现在那里。**来源 IP / UA 只出现在管理员接口**;`/api/users/directory` 的字段面(`id/name/email/avatarColor`)不得掺入它们(有测试钉住)。
14. **登录审计不记秘密**:`login_events` 只记邮箱/来源/UA/结果;**冷却期内被拦下的 429 不落审计** —— 它们没跑过校验又可以被无限刷,落库只会把审计表变成攻击者的写入放大器(当前锁状态在 `throttle_state`,管理端直接展示)。

## 7. 领域语义

### 7.1 个人空间 = 个人项目

- 每个用户创建时自动获得一个 `is_personal=true` 的项目,唯一成员 = 本人 OWNER。不可删、不可管理成员、**永不可公开**(`patch_project` 硬拒,连"设为 private"也拒);他人的个人项目在列表与搜索里一律不出现。
- `project_role` 里 **`is_admin → ADMIN` 必须在 `is_personal` 判断之后** —— 反了管理员就能直读别人的私有草稿。
- files/folders 没有 scope 字段,全归 `project_id`;"归属转移"即跨项目移动。没有独立的 `#/drive` 视图,旧路由重定向到个人项目的 files 页。

### 7.2 文件、文件夹、回收站与分页

- 删除 / 恢复 / 彻底删除**三者对称,都作用于整棵子树**(单事务),实现收敛在 `trash.py` 的树引擎(`collect_subtree` / `soft_delete_tree` / `restore_tree` / `commit_and_unlink`),端点只声明资源类型。恢复时若父级仍在回收站则回落项目根。回收站只列**子树根**(判据:父级未删除或不存在;已删文件夹 id 用全量查询取,防止 500 截断后漏判父级)。
- 移动:项目内整理 EDITOR 即可;跨项目需源 ADMIN + 目标 EDITOR;拒移入自己的后代(409);跨项目**整棵子树一起改 `project_id`**。
- `GET /api/projects/{id}/folders/tree` 是一切层级需求(移动选择器、面包屑重建)的唯一入口,别再写扁平遍历。
- 云空间列表是**分页 + 服务端排序**(两者一体:只排当前页是错的)。排序字段走白名单映射,非法值回落;排序键含 id 兜底,保证翻页不重不漏;`hasMore` 由服务端算。文件夹不分页(上限 2000),文件默认 100/页 + 加载更多。**回收站与搜索仍是静默截断 500**(已知遗留,§10)。

### 7.3 公开项目与自助加入

- **公开 = 本实例所有登录用户可发现(发现广场)+ 可自助加入;加入前完全不可读**。不做匿名分享链接(内网服务外网也访问不到)。关闭公开立即生效,没有缓存层。
- 加入后的角色由项目设置里的 `join_role` 决定(VIEWER 只读 / EDITOR 可编辑,**默认 VIEWER**)。没有审批环节:需要控制谁能参与的项目就不要公开(私有项目只能靠管理员在成员页加人)。
- 个人空间永不可公开,于是也永远无法被加入。
- 单文件公开(`files.is_public`)是**独立通道**:鉴权抽在 `files.ensure_file_access`,判定链是"项目角色 → 文件公开 → 403",**与项目可见性无关**(公开项目也不会因此能读项目内容)。
- 广场是公开项目的**唯一入口**:`visible_project_ids`(搜索/列表/动态共用)不含公开项目 —— 加入前不可读,自然搜不到;`/api/recent` 照旧只含已参加的项目。
- 广场按 `lastUpdatedAt` 倒序(项目内最近一次文档更新或文件上传);发现页把广场与 `/api/recent` 合成一页,未加入者点卡片先问是否加入。
- 发现页卡片与项目页错误态共用**同一个加入入口** `ProjectsAPI`(`views/projects.js`):加入成功后必须做三件事 —— 入侧栏(模块导航由此而来)、跳转、落库。散在各调用方就必然漏第三步。

### 7.4 文档保存、版本与协同

- **保存是显式动作**:只在点「保存」或 `Ctrl/Cmd+S` 时写库(按钮仅脏时可用),**自动保存与失焦保存已整体移除**。这不是为了省请求,而是让每次写入都是人的决定,从而少撞并发修改。
- **没保存就不让走**:机制在 `app.js` 的 `App.onLeaveGuard`(`hashchange` 先问守卫,被拒则还原 hash 且**不重渲** —— 旧视图连同未保存正文还留在 DOM 里),策略在编辑器(脏时三选「保存并离开 / 放弃改动并离开 / 取消」,存成功才放行)。关闭标签/刷新走 `beforeunload`。去 `#/login` 不拦(登出与 401 失效有自己的语义,拦住会卡死)。`saveContent()` **返回 Promise**(WS 路径也等回包,5 秒超时,超时按失败处理),否则"保存并离开"无法确认落库。
- **版本按年龄分层**(取代"只留最近 N 条"——条数对应的时间跨度不可预测):**1 小时内全留 / 1 天内每小时 1 条 / 30 天内每天 1 条 / 1 年内每周 1 条 / 更早每月 1 条**,稳态约 124 条/文档。实现在 `docs.py _prune_versions`。`VERSION_MERGE_MINUTES` 默认 0(不合并):手动保存是刻意动作,每次保存都该留下可回退的点。
- `docs.version` 是**保存次数不是版本数**。顶栏不显示它 —— 显示会把"改得很勤"误读成"版本很多"。
- **整篇覆盖要声明基线**:`save_doc_content(..., base_version=)` 在内容有变化时比对 `doc.version`,不符即抛 `ContentConflict`(在任何状态改动之前 —— 快照、version+1、剪枝都不做)。这不是防"进程内竞态"(单 worker 下不存在),而是防**陈旧副本整篇盖回去**:两人同编、或 WS 断线后盲写一小时前的快照,此前都是静默丢失且两边都显示"已保存 ✓"。
  - **必填只对 web 会话这一档**(`ctx.via == "web"`):浏览器手里有加载过的缓冲,必须声明基于哪一版。**PAT(CLI / 脚本)缺省即覆盖** —— 它是"把文档定稿成这份内容"的程序化写入,不持有缓冲,与 HTTP 的 `If-Match` 可选、S3 PUT 默认无条件同一模型。**显式带了基线就一律照查**:分档放宽的是"缺省",不是"声明了不生效"。
  - REST 不匹配 → 409 `CONFLICT`,`detail` 额外带 `currentVersion`/`currentContent`/`by`(现场随错误一起回,客户端不必再 GET 一次可能又变了的第三态)。
  - WS:content 消息带 `baseVersion`,缺了按非法消息忽略并记 WARNING;冲突只回**发送者** `conflict{version,content,by}`,不回 `saved`、不向其他人广播(服务端没写库,通知别人只会莫名其妙)。
  - **内容完全相同时短路放行**,不判冲突 —— 没有可丢的东西,不该弹窗打扰人。
  - **豁免两条路径,理由写在代码注释里**:`/append`(读-改-写在服务端同一次事务内完成,拼的是当下正文)与 `/versions/{id}/restore`(用户看着历史主动覆盖,现场另有"还原前"快照兜底)。传 `base_version=None` 表示"服务端自身读取并写入",**不是"强制覆盖"的后门**。
- 客户端 `baseVersion` 只在**真正与服务端对齐**时推进(初次加载 / `saved` 回包 / 采纳远端 / PUT 成功);收到 `remote` 但没采纳时**不能**推进,否则是谎报基线。
- **冲突处理 = 逐块选择合并**(`js/diff.js` 的 `Diff.build`):进冲突态后**所有保存暂停**(不暂停就是又一次覆盖),状态栏常驻「有冲突未解决」且可点开弹窗。弹窗按**最大连续变更段**分块 —— 天然对应 git 的一个冲突块 —— 每块三选「我的 / 服务端 / 两者保留」(默认"我的";未选的一侧**只淡化不隐藏**,看得见丢了什么、布局也不跳);底部「全部用我的 / 全部用服务端」只改选择、「保存合并结果」才写库;**合并结果与服务端现场完全一致时直接本地采用、不写库**(全选服务端走的就是这条路)。Esc/遮罩关掉 = 暂不处理,保存仍然停着。写入时若又有人写过 → 再撞 409 → 弹窗带新现场重画(已做的选择按**块序号**保留:序号稳定,行号会变)。
- WS 断开后保存降级为 `PUT /content`,功能不受影响;重连最多 3 次(指数退避 + 抖动),超出就停,不再空转。
- 脏的时候顶部提示条不叫「加载最新」(旧文案是个静默丢改动的按钮),叫「比较并合并」:打开同一个弹窗、基线用最新远端版本。不脏时仍自动应用远端。

### 7.5 存储与备份恢复

- **生命周期**:删除 = 软删(物理文件保留);彻底删除或删项目才 unlink;失败**不回滚**,残留交给孤儿清理兜底(统一 `models.unlink_quiet()`)。上传写盘全程 try/except(异常必 unlink 半成品);写盘前后都查磁盘余量(`STORAGE_RESERVE_MB`,磁盘满给明确 400 而不是 500 + 半截文件)。
- **孤儿清理**(`POST /api/admin/storage/cleanup`,仅手动、无自动巡检):`dryRun=1` 先预览;孤儿超过磁盘文件总数的 2/3 时**熔断拒绝**,需 `force=1` —— 防"库空 / 指向错目录 / 恢复旧备份"时把磁盘一键删光。**在传文件必跳过**(`models.INFLIGHT_STORAGE`,回 `inflightSkipped` 计数):上传是先落盘后提交记录,这个窗口里它在库中不存在,只按 DB 比对就会被当垃圾删掉,而上传随后提交成功 → 记录指向已删文件、永久 404。`missing`(库有磁盘无)只报告不删,正解是恢复备份。
- 占用统计:存储总览给**实例总量**(回收站与活跃分列,回收站仍占磁盘);分项目占用在管理后台「项目」区;占比的分母只算 TeamDoc 自身,不整盘。
- **备份**:`VACUUM INTO` 生成快照(见 §6.2) → 按快照里的 files 清单打包 → 写到 `BACKUP_DIRS` 的多个目标。`BACKUP_KEEP` 只认自己的 `teamdoc-backup-*.zip`,不碰手工文件;每份写完**回读校验**;**目标目录不存在记失败,绝不自动 mkdir**(移动盘没插时会写到假路径)。定时线程 60s tick,整体 try/except。
- **恢复 = 上传校验 → 重启生效**(不在请求里更换运行中的库)。校验含 zip slip 白名单(只允许 `teamdoc.db` / `RESTORE.txt` / `files/<平铺名>`)、总量不超磁盘可用、**版本一致性**(没有迁移机制,旧版库会让服务起不来,必须在上传时就拒)。重启时把当前库与 `files/` 挪到 `data.pre-restore-<ts>/` 留退路,再解包。只支持整站覆盖,不支持挑单文件取回。

## 8. API 契约要点

- 前缀 `/api`;成功直接返回 JSON 无信封;删除类回 `{"ok":true}` 或 204;错误统一 `detail={"code","message"}`,状态码 400 `VALIDATION` / 401 / 403 / 404 / 409 `CONFLICT` / **429 `TOO_MANY_ATTEMPTS`**(带 `Retry-After: <秒>`,由登录节流产生)。
- **登录节流契约**:`POST /api/auth/login` 与 `POST /api/users/me/password` 走同一咽喉。未锁定时失败仍是 401「邮箱或密码错误」(仅剩 ≤2 次时文案追加「(还可尝试 N 次)」),达到上限即 429;**冷却期内正确密码也 429**(锁定先于校验)。改密路径只挂账号维度(登录态下来源 IP 不是威胁轴,也不该让共享出口 IP 的办公室替一个人挨罚)。
- **登录状态与审计(仅 is_admin)**:
  - `GET /api/users` 每项含 `lastLoginAt` / `lastLoginIp` / `online` / `sessionCount` / `lockedUntil`(最后登录从 `login_events` 聚合,不在 users 上冗余列;全部批量查询,不 N+1),另有 `canDelete`。
  - `GET /api/admin/users/{id}/access`:一次拉齐 `lock{failCount,strikes,lockedUntil,windowStart}` + `maxFails` + `sessions[]`(`ref/ip/device/deviceKind/userAgent/createdAt/lastSeenAt/expiresAt/online/current`)+ `events[]`(最近 50 条,`result` = ok / bad_password / disabled / locked)。
  - `DELETE /api/admin/sessions/{ref}` 强退单条会话(找不到 404);`DELETE /api/admin/users/{id}/sessions` 全部下线。`ref = sha256(token)`。
  - `GET /api/admin/login-events?limit=`(上限 500)全站登录动态,含**账号不存在**的失败(撞库痕迹);`userName` 未知时为 `null`(字段面固定,不让"账号不存在"的行缺键)。
  - `PATCH /api/users/{id}` 支持 `{"unlock": true}` 解除账号维度冷却(来源维度的桶不归属某个账号,靠时间自愈)。
- 认证:`Authorization: Bearer tdp_...`(PAT)或 Cookie 会话。PAT read scope 发非 GET → 403;**PAT 的创建与吊销仅接受 Web 会话**。角色 `OWNER > ADMIN > EDITOR > VIEWER`;`is_admin` 全局视为 ADMIN(个人空间除外)。
- 项目 JSON 带 `isPublic` / `joinRole` / `isMember` / `lastUpdatedAt`;`myRole` 即有效权限,`isMember` 仅用于"真成员关系"语义(回收站 tab 可见性、退出项目卡)。`PATCH /api/projects/{id}` 传 `isPublic` 切换公开、传 `joinRole` 改自助加入后的角色(非法 400)。
- **自助加入** `POST /api/projects/{id}/join`:判定链 404 不存在 → 403 未公开(个人空间恒 private,自然落进这条)→ 409 已是成员。依赖取 `current_user` —— 此刻本人还没有角色,走不了 `require_project_role`。**自助退出** `POST /api/projects/{id}/leave` 与之对称:个人空间 403 → 非成员 403(公开项目给 `JOIN_REQUIRED`;全局管理员能过依赖层,落到端点的"你不是该项目成员" 404)→ 末代 OWNER 409。
- **`403 JOIN_REQUIRED`**(文案「本项目需先加入才能查看」)是公开项目对非成员的统一拒绝,由 `ensure_project_role` 一处产生;前端据此在项目页错误态给「加入项目」按钮。其余权限不足仍是 `FORBIDDEN`。
- 引用格式(markdown 内):图片 `![名称](/api/files/{id}/download?inline=1)`、附件 `[名称](/api/files/{id}/download)`、文档/文件 `[@标题](teamdoc://doc/{pid}/{did})` / `[@名称](teamdoc://file/{fid})`。`GET /api/docs/{id}` 与 `GET /api/files/{id}/meta` 都带 **`location` 契约** `{projectId, projectName, path[]}`(meta 另含 `folderId`),浮层归属展示一份代码消费。反链:`GET /api/docs/{id}/backlinks`。
- **正文写入契约**(§7.4):`PUT /api/docs/{id}/content` 的 `baseVersion` 只对 web 会话必填(缺失/非整数 400);PAT 缺省即覆盖,显式带了就照查。不匹配 409 且 `detail` 带 `currentVersion`/`currentContent`/`by`。`GET /api/docs/{id}` 与建文档的返回都含 `version`。`/append` 与 `/versions/{id}/restore` 豁免。
- mime 在启动时按文件名幂等回填(`schema.normalize_mimes`),全站统一信任库值。云空间 `GET /api/files?project_id=&folder_id=&offset=&limit=&sort=&dir=` 的项带 `referenced`(被文档引用,徽标 + 删除警告)/ `canInline` / `isText`,响应带 `total:{folders,files}` 与 `hasMore`;`GET /api/projects/{id}/storage` 给占用统计。搜索的文件结果带 `mime`/`canInline`/`projectName`/`folderId`;`GET /api/recent` 带 `projectName`/`folderId`。
- **上传是 raw body(非 multipart)**:`POST /api/files/upload?projectId=&folderId=&name=`,请求体即内容;前端用 XHR 拿进度。重名时上传自动加后缀 `foo(2).png`;用户显式操作(建目录、重命名)冲突则 **409**;改名同步重算 mime。
- zip 打包 `GET /api/files/zip?ids=&folderIds=`:保留目录结构,文件数上限 1000(超限拒绝而非截断),**必带 Content-Length**(先压到 SpooledTemporaryFile 再流式回吐)。回收站 `GET /api/projects/{id}/trash` → `{docs,files,folders}`,只列子树根;文件、文档、文件夹各有 `/restore` 与 `/permanent`。
- 管理端(仅 is_admin):`GET /api/admin/storage`、`POST /api/admin/storage/cleanup?dryRun=&force=`、`GET /api/admin/backup(/status)`、`POST /api/admin/backup/run`、`GET /api/admin/restore/status`、`POST /api/admin/restore/upload|arm`、`DELETE /api/admin/restore`。写类的守卫见 §6.4 第 4 条。
- `GET /api/users/directory`:任意登录用户可调,只回 `id/name/email/avatarColor`,禁用账号不出现。前端 `UI.personPicker` 做选人(单选即选即关;`{multi, roleSelect}` 批量圈选 + 行内角色下拉),搜索按 姓名/id/邮箱 匹配,排除只认 `excludeIds`(身份键唯一 = id)—— **没有手输邮箱框**,加成员接口收 `userId`。个人设置页与用户管理表展示用户 id。
- **管理后台项目总览** `GET /api/admin/projects`(仅 is_admin):全部**协作项目,不含个人空间**(它们不可管理且对管理员保密,§7.1);项带 `owners`(含 isDisabled —— 唯一所有者已禁用 = 死锁信号)/ `memberCount` / `docCount` / `storageBytes`(仅活跃文件)/ `lastUpdatedAt`,全部批量聚合。前端「项目」区只做发现 + 跳转 + 删除。**存储区没有分项目占用列表** —— 分项目占用就在「项目」区,勿再加回。
- 用户管理(PATCH/DELETE,仅 is_admin):PATCH 可改 `email`(重名 409)/`name`/`isAdmin`/`isDisabled`/`password`;DELETE **只允许删从未产生数据的账号**(`String` 列不是外键,删了会留悬空引用;命中即 409 提示改用禁用),`GET /api/users` 的 `canDelete` 就是给它用的。删除会连带清理 sessions / PAT / 成员关系 / 个人空间。
- **改密的轮换语义两处不同,勿"对齐"**:`POST /api/users/me/password` 只吊销该用户**其它**会话(当前这条保留,否则用户被自己踢下线),PAT 保留(自愿改密,CLI 不该被打断),响应带 `revokedSessions`;**管理员 PATCH 带 password = 重置密码,会话与 PAT 全部吊销** —— 重置的本意就是把持有旧凭据的一方踢出去。
- 邮箱校验宽松(含 `@` 且 ≤255);前端登录/初始化页刻意用 `type="text"` —— 浏览器原生校验比服务端严,两边不一致会造出"建得进、登不进"的账号。
- **凭据字段必须带标准 `autocomplete` 令牌**(密码管理器判定"哪个框是账号"只认它):账号 = `username`(本站账号就是邮箱)、姓名 = `name`、当前密码 = `current-password`、新密码 = `new-password`。少一个,管理器就退回"猜"(把密码框上方最近的文本框当账号)。**密码框绝不写 `off`**(浏览器忽略它,反而会回填操作者自己的密码);管理他人资料的表单用 `off` 且不给 `username`。不引 `data-1p-ignore` 之类厂商私有属性。
- 静态资源统一 no-cache + ETag 重验证(杜绝改版跑旧 JS);`avatarColor` 由 id 哈希确定性取色、不落库,**所有涉及用户的接口都返回它**。

## 9. 测试与验证

测试是 pytest 套件,服务端由 `conftest` **全自动托管**(空闲端口 + 一次性临时数据目录,起服/bootstrap/杀进程/清目录全自动),没有手工起服步骤。

```bash
cd lite/server
uv run pytest ../tests                        # 快速组(slow 组默认跳过)
uv run pytest ../tests -m "slow or not slow"  # 全量
uv run pytest ../tests/test_visibility.py::test_xxx   # 单跑一条
```

**每个测试文件覆盖什么,见 `lite/tests/README.md`** —— 那里是唯一清单,不要在这边再抄一份(两份必然漂移)。

- 共享底座只此一份:`_harness.py`(Client / Resp / Server / 随机邮箱 / 裸 socket)、`_chrome.py`(无头 Chrome 临时页)、`conftest.py`(fixture 装配)。**写新测试从 fixture 拿 `admin` 客户端、用 `make_user` 建用户,不要再复制 call/login/upload。**
- 需要**自己的阈值**的测试(节流这类)自起实例:`Server(extra_env={...})` + `srv.admin_client()`,并只开被测的那一维度 —— 共享实例上所有客户端都来自同一来源 IP,来源桶会互相累积(conftest 已把共享实例的 `LOGIN_IP_MAX_FAILS` 放宽到 200)。
- **测试天然可重复运行**:每个测试自建数据(邮箱带随机后缀 —— 固定邮箱二跑必撞 409),数据目录一次性的,所以断言绝对值是安全的。
- HTTP 客户端统一在 `Client` 里:标准库 urllib、显式 UTF-8(**别用 curl 发中文**,Windows 控制台是 GBK,乱码会直接入库)、响应头键小写化、会话 cookie 自动续期。WS 地址从 `base_url` 推导,**勿硬编码端口**(会连到另一台服务并报 4401 假失败)。
- 前端:`node --check` 过一遍 JS;删 CSS 类前 grep 反查(注意 `'cls-' + x` 这类动态拼接);CSS 查大括号配平。
- **像素级验证是人工手段**(自动化几何断言已删):无头 Chrome 截图 + 差异比对。要点:同步 XHR 先登录(HttpOnly 会话无法伪造);`--virtual-time-budget` 下注入 `transition:none`;断言写进页面 DOM;**必须真实点击 + 断言状态变化** —— 静态截图抓不到"处理器读了旧属性名"这类交互失效;改掉标记后 grep 所有读取处(`dataset.x` / `[data-x]`)。

## 10. 已知遗留

- 窄屏 72px 图标栏下项目子项没有文字,辨识度受限(统一导航机制的取舍;反馈不好可恢复窄屏两态)。
- `referenced` 徽标靠全项目扫描正文,文档上千后应改为保存时维护 `doc_file_refs` 索引表。
- 回收站与搜索**静默截断 500**(没有"还有更多"的提示);文件夹恢复不区分删除批次(子树里先前单独删掉的会一起回来)。
- 文本预览无大小截断(几十 MB 文件会卡住浏览器);备份是 DB 快照打包,盘上无记录的孤儿不会进包(缺文件另计 `filesMissing`);不做增量备份;恢复只支持整站覆盖,不支持挑单文件取回、不支持回退到旧版本代码。
- 公开项目是**实例级**(没有部门/小组范围控制),自助加入也不设审批 —— 需要控制参与者的项目保持私有即可;能读成员列表的只有真成员与全局管理员。
- 引用浮层 Esc 关闭后字面量 `@`/`[[` 留在正文;marked 的行内 `$...$` 对价格文本可能误判;冷加载个人项目的瞬间成员 tab 可能闪现。
- **登录节流的边界**:①分布式的慢速喷洒(每个 IP 只试一两次)只是被**拖慢**,不换 IP 绕不过去就得靠网关/边界防护 —— 本项目刻意不引 WAF 类依赖;②知道他人邮箱的人可以制造最长 1 小时的账号冷却(锁定式防御的固有代价,已用短初始冷却 + 翻倍上限 + 管理员解锁/强退压低影响);③"在线"是 5 分钟窗口的近似,不是实时;④`throttle_state` / `login_events` 没有后台清理线程,只在写入时机会性清扫(`LOGIN_EVENT_KEEP_DAYS=0` 可整体关掉保留期策略);⑤审计里没有 PAT 的最近使用与来源(数据在 `pats.last_used_at`,接口未做)。

## 11. 路线图

- **日历模块**:`PROJECT_NAV` 加一项即可接入(文档树的同构模块)。
- **字符级真协同**:pycrdt/Yjs 替换 LWW。便宜的中间态(手动保存 + 基线校验 + 逐块合并)已落地;真要上 CRDT 须重做 WS 协议、存储格式(正文降级为投影)、CLI 写入,并引入 JS/Rust 合并实现 —— textarea + 中文输入法的绑定是主要风险。
- **登录详情抽屉纳入 PAT**(名称 / scope / 最近使用 / 来源 / 吊销入口,管理员视角):数据都在 `pats` 表,只差接口与一段列表;与"会话"合起来才是完整的"该账号的凭据全景"。

## 附录:踩过的坑

每条都对应代码里的一处防御,删防御前先读这里。

| 症状 | 原因 | 留下的规矩 |
|---|---|---|
| 全站无响应,进程活着、日志一个字都不出,连看门狗告警也一起冻结 | 事件循环卡在 `logging.stream.write`:控制台被鼠标选中(Windows 的快速编辑会挂起写入)或磁盘慢 | 日志写出全部挪到 QueueListener 线程,调用方只非阻塞入队(§6.3) |
| 池里连接被卡住的传输占满 → 全站请求排队 30 秒 | FastAPI 的 yield 依赖要等响应体发完才清理,大文件传输期间一直占着连接 | 搬字节前先 `release_db(db)`;SQLite 索性不用连接池(§6.3) |
| 备份 zip 里的库"一张表都没有" | 直接复制了 WAL 模式下的 `.db`,写入还在 `-wal` 里 | 备份必须 `VACUUM INTO`(§6.2、§7.5) |
| 用户"新建项目"时才炸 | 项目色功能下线时只删了模型字段,`projects.color` 残留在库里且是 NOT NULL | 删字段必须连库里的列一起删;启动自检会阻断(§6.2) |
| 上传成功的文件随后 404、且文件已被删 | 孤儿清理在"已落盘、未提交记录"的窗口里把它当垃圾删了,熔断拦不住单个文件 | 在传文件登记进 `INFLIGHT_STORAGE` 并跳过(§7.5) |
| "点了没反应 / 选中态不生效"(踩过四次) | JSON 的 id 是数字、URL 与 dataset 的是字符串,不归一就永远匹配不上 | id 一律 `Number()` 后再比较或作键(§5.2) |
| 密码管理器把「姓名 + 密码」存成一组凭据 | 密码框缺标准 `autocomplete` 令牌,管理器只能猜"上方最近的文本框是账号" | 凭据字段必须带标准令牌(§8) |
