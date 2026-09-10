# TeamDoc Lite — Handoff 文档

> 写给后续接手的 Agent / 开发者。**本文档是代码库当前状态的权威说明,与代码冲突时以此为准,并顺手修正本文档。**
> 更新日期:2026-09-10(功能完成 + 编辑器重构 + 前端组件化重构后)

---

## 1. 产品现状

TeamDoc Lite:30 人小团队自部署知识库。项目(组)管理文档、实时协同编辑、云空间、全文搜索。界面全简体中文。

**功能已完整**:认证(bootstrap / TOTP / PAT / 用户管理)、项目与成员、文档树、Markdown 源码/预览双模式编辑、WebSocket 协同、版本历史、云空间(上传/下载/打包/项目间移动)、回收站(文档+文件+文件夹)、全文搜索。

**明确不做**:CLI `td`(将拆为独立项目,服务端零改动即可支持)、日历、字符级协同、S3、通知。

## 2. 技术栈与运行

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13 / FastAPI + uvicorn(**必须单 worker**)+ SQLite(SQLAlchemy 2.0,WAL,busy_timeout=5000) |
| 前端 | 纯 HTML/CSS/JS,零框架零打包;**依赖全部本地 vendor,无任何外网请求** |
| 认证 | scrypt 密码(stdlib)、Cookie 会话(`td_sid`,HttpOnly+SameSite=Lax)、PAT(`tdp_` 前缀存 sha256)、TOTP(pyotp) |

```bash
cd lite/server
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker
```

- 首次启动只建表、不预置账号;浏览器访问走初始化向导(bootstrap 建首个管理员)。
- 数据在 `server/data/`(已 gitignore)。`TEAMDOC_DATA_DIR` 可改数据目录,供测试隔离用。
- 环境变量:`PORT`(8000)、`MAX_UPLOAD_MB`(2048)、`SESSION_TTL_DAYS`(7)、`VERSION_MERGE_MINUTES`(5,见 §4.5)。
- **内网/离线部署直接可用**:第三方资源在 `web/vendor/`(随仓库提交),服务端零外网调用,无联网安装步骤。

**为什么必须单 worker**:SQLite 是单写者模型。多进程会出现推送丢失、写锁冲突等难以排查的问题。

### 2.1 vendor 目录(内网部署关键,勿删)

```
web/vendor/
├── remixicon/   remixicon.css(相对路径)+ woff2/woff
├── marked/      marked.min.js
├── prism/       prism.min.js + prism-autoloader.min.js + components/(78 个语言包)
└── katex/       katex.min.js + katex.min.css + fonts/(40 个 woff2/woff,懒加载)
```

- 升级依赖:替换 `web/vendor/` 下对应文件即可,无需改代码。升级 Remix Icon 时保持 CSS 内字体的相对路径写法。
- **autoloader 必须在 `prism/prism-autoloader.min.js`**:它原本在 CDN 的 `components/` 路径下不存在(导致代码高亮只剩 html/css/js)。正确来源是 `plugins/autoloader/`。
- `main.py` 的 no-cache 中间件覆盖 `/vendor/` —— 文件名不含内容哈希,长缓存会让升级不生效。
- **改完前端资源必跑 `verify_page_assets.py`**:它逐一请求 index.html 与 CSS/JS 里的所有资源,断言零外链 + 200 + no-cache。

## 3. 目录结构

```
lite/
├── server/
│   ├── main.py      (67)   入口:建表、路由注册(auth→docs→files→search→ws→静态托管,顺序不能乱)、
│   │                       422→400 VALIDATION、no-cache 中间件(/css /js /vendor / index.html)
│   ├── models.py    (167)  9 张表:users/sessions/pats/projects/project_members/docs/doc_versions/folders/files
│   ├── auth.py      (538)  scrypt、会话、PAT、TOTP、**鉴权工具集**(§4.7)、用户管理、create_personal_project
│   ├── docs.py      (537)  项目/成员/文档树/内容/版本(§4.5)/回收站/反链;save_doc_content 为 REST 与 WS 共用
│   ├── files.py     (418)  云空间:流式上传(1MB 块)/下载(inline|attachment)/zip 打包/文件夹 CRUD/
│   │                       回收站恢复与彻底删除(级联子树、清物理文件)/项目间移动
│   ├── search.py    (56)   LIKE 搜索 + 权限过滤 + snippet(命中前后各 60 字符)
│   └── ws.py        (119)  /ws/docs/{doc_id}:presence 广播、LWW content→saved/remote、VIEWER readonly
├── tests/                  纯 stdlib 测试脚本(见 tests/README.md)
└── web/
    ├── index.html   (82)   SPA 壳 + 全部 script 标签(加载顺序即依赖图)
    ├── vendor/             第三方资源(§2.1;勿删、勿改 import 路径)
    ├── css/
    │   ├── tokens.css   (285) **唯一尺寸与颜色来源**(§4.1)
    │   ├── base.css     (92)  重置、[hidden] 护栏、滚动条、焦点可见、少量工具类
    │   ├── components.css (774) **组件库样式**(§4.1)
    │   ├── app.css      (545) 壳布局、侧栏、登录页、各视图残留专属样式、响应式
    │   └── editor.css   (222) 编辑器专属:源码 textarea / .markdown-body 排版 / Prism 令牌映射 /
    │                          teamdoc:// chip / 浮动工具栏 / 反链栏
    └── js/
        ├── api.js       (52)   fetch 封装:204→null;{detail:{code,message}}→ApiError;401 跳 #/login
        ├── theme.js     (65)   主题:light/dark/system + 6 种子色,localStorage 持久化,onChange 订阅
        ├── ui.js        (797)  **组件库(全部视图复用,禁止另造)**(§4.1)
        ├── markdown.js  (130)  全站唯一 Markdown 渲染路径:marked + raw HTML 转义(防 XSS)+
        │                       $$/$ 公式(KaTeX 懒加载)+ Prism 高亮;marked 缺失时降级纯文本
        ├── doceditor.js (569) 编辑器增强:teamdoc:// chip 路由、@/[[ 引用浮层、/ 插入菜单、
        │                       选区浮动工具栏、粘贴/拖拽上传;返回 cleanup
        ├── app.js       (534)  hash 路由、壳装配、侧栏状态机与项目树(§4.3)、登录/向导、PROJECT_NAV
        └── views/              projects(项目首页)/project(文档+成员+回收站+设置)/drive(云空间)/
                                search/admin/settings
```

**加视图模块的步骤**:在 `app.js` 的 `PROJECT_NAV` 加一项 `{key, icon, label, view, visible}` + 写一个 `window.Views.<name>` 函数。路由、侧栏子项、高亮、tab 记忆全自动生效(日历模块即可这样接入)。

## 4. 必须理解的约束与架构决策

### 4.1 前端组件化三层(2026-09 重构,新增 UI 前必读)

重构前的状态是"每个功能各写一套":11 套列表行、6 套页头、10 种控件高度、5 种 hover 透明度。
现在收敛为三层,**新增界面时按这三层来,不要退回手写**:

| 层 | 位置 | 规矩 |
|---|---|---|
| 令牌 | `css/tokens.css` | **唯一尺寸与颜色来源**。控件高度 `--ctl-*`、间距 `--sp-*`(含 4pt 半档)、行高 `--row-*`、字阶 `--fs-*`、图标 `--icon-*`、状态层 `--state-*`、内容宽度 `--w-*`、浮层尺寸、z-index 六档表。**新样式里出现的裸像素应视为待补令牌的信号** |
| 组件样式 | `css/components.css` | 组件的 class 定义。视图只引用 class,不自定尺寸 |
| 组件工厂 | `js/ui.js` | **唯一组件入口**。标记类工厂返回 HTML 字符串(与视图的拼接风格一致),需事件接线的才返回 DOM |

**两条列表族系**(先判型再选,不要新造第三种):
- 需要"列与列对齐" → `.data-table`(`UI.tableHead`/`tableRow`)。列宽经 `--tpl` 注入,**表头与数据行必须引用同一组值**;末列(操作)必须固定宽 —— 表头该格为空、数据行该格含按钮,用 auto 会让各列错位。云空间与用户表在用。
- 只需"图标/头像 + 文字 + 操作" → `.list-row`(`UI.listRow`)。成员、回收站、令牌、历史版本、搜索结果在用。

**按钮尺寸两档制**:`--ctl-xl`(40px 页面级:页头、工具栏、表单提交)/ `--ctl-m`(32px 行内级:表格行内、面板内、卡片头)。

**颜色值一律不动**:主题语义色 + 6 种子色 × 浅/深全部静态写在 tokens.css,零运行时算色(见 §4.4)。文件类型色是主题无关的固定色,用 `--file-*`,**不要复用主题角色**(那会在深色模式下变色)。

**其它**:`.chip` 与按钮同为胶囊形;hover 洗色一律用 `--state-*` 的 `color-mix`,不自定百分比;可点卡片用 `.card-link`;`UI.icon()` 对 `ri-` 前缀做归一化(传裸名或全名都可以)。

### 4.2 个人空间 = 个人项目

- 每个用户创建时(bootstrap/管理员建用户)自动获得 `is_personal=true` 的项目"个人空间",唯一成员 = 本人 OWNER。
- **保护规则**:不可删(403「个人空间不可删除」)、不可管理成员(403),可改名/描述。
- 列表:个人项目排最前;管理员 `?all=1` 不含他人个人项目(但直接访问 URL 仍按 ADMIN 权限,保留原语义);搜索同理隔离。
- **files/folders 无 scope/user_id 字段**,全部归属 project_id;原"归属转移"已变为项目间移动(`POST /api/files/{id}/move`,源项目 ADMIN + 目标项目 EDITOR)。
- 前端无 `#/drive` 独立视图,旧路由重定向到个人项目的 files tab。
- **旧库里的 `projects.color` 列是物理残留**(项目色功能已整体删除:前端取色器 + 后端字段),
  无害、不读写;看到它不必"修好"。
- 顶栏(原规格的 48px 搜索/头像条)已删除:搜索与用户菜单都在侧栏,协作者头像只在编辑器内联。

### 4.3 侧栏(项目树 + 折叠/抽屉状态机)

侧栏 = 品牌行(文字 + 折叠钮)+ 搜索框(**折叠态顶替为搜索按钮**)+ 项目树 + 管理后台/个人设置 + 底部用户卡片。

- 项目行 = chevron + 纯文字(无图标无色点),点击**只做展开/收起,不导航**;进入项目必须点子项。
- 展开状态:内存 Map 是**唯一真相**,仅三处写入 —— 用户点击行、一次性种子(刷新/深链首次渲染时展开当前项目)、新建项目后 `App.expandProject(pid)`。**路由变化只影响高亮,绝不动展开态**(离开项目去管理后台不会误收起)。刷新回到种子状态。
- 状态模型只有两种,由同一套 CSS 承载:

| | 折叠(72px 图标栏) | 展开(240px) |
|---|---|---|
| 宽屏 | 用户点折叠钮(挤压主区) | 默认 |
| 窄屏 ≤960px | **恒为此态**(否则没有导航入口) | 点按钮/搜索钮 → `.nav-open` 抽屉浮层化 |

  实现要点(改动前务必理解,否则极易破坏):
  - CSS 唯一入口是 `#shell.side-collapsed:not(.nav-open)` 一组规则。**`:not(.nav-open)` 是关键** —— 抽屉打开时整组规则失效,侧栏自然回到完整布局,因此不需要任何"反向展开"覆盖规则。
  - JS(`applyNavMode`/`setUserCollapsed`/`syncCollapseBtn`):窄屏恒折叠且**不写 localStorage**,所以窄屏用不用抽屉都不会污染宽屏偏好;回宽屏即恢复用户原选择。
  - `#side-collapse` 按钮语义随屏宽变化:宽屏 = 折叠/展开图标栏,窄屏 = 抽屉开关。`syncCollapseBtn()` 保证 title 如实反映"点了会怎样"。
  - 搜索聚焦需 `requestAnimationFrame` 延后一帧(搜索框刚从 `display:none` 恢复,同帧 focus 无效)。

### 4.4 主题系统

- `html[data-theme]`(light/dark)× `html[data-color]`(blue/purple/green/orange/pink/teal,6 种)。
- **全部色值静态写在 tokens.css,零运行时算色**;首屏由 index.html 内联脚本在样式加载前恢复主题,防闪烁。
- 编辑器/预览的色值也全部经令牌引用(Prism 高亮色用 `color-mix` 从令牌派生),**主题切换零 JS 处理**。
- 切换入口:底部用户卡片菜单内的"外观"面板。

### 4.5 文档版本保留与协同(P0)

**版本保留(2026-09 改动)**:由"只留最近 50 条"改为**按年龄分层** —— 条数限制对应的时间跨度不可预测,频繁编辑的文档几周就把历史整段丢光,几乎没人动的却永远留着。现策略:

    1 小时内 → 全部保留(覆盖"刚改坏了要回滚"这一最主要场景)
    1 天内   → 每小时留最新 1 条
    30 天内  → 每天留 1 条
    1 年内   → 每周留 1 条
    更早     → 每月留 1 条

稳态上限约 124 条/文档。另有 **5 分钟合并窗口**(`VERSION_MERGE_MINUTES`):同一人在窗口内的连续保存不再新增还原点,一次编辑会话 = 一个还原点 —— 编辑器自动保存是 800ms 防抖,不加窗口一小时能产生几十个版本。实现见 `docs.py` 的 `_prune_versions` / `save_doc_content`。

注:`docs.version` 字段是**保存次数,不是版本数**,曾因此误显示为 `v{N}` 造成误读,顶栏现改为显示「已保存 ✓ HH:MM」。

**协同语义(LWW)**:
- WS `/ws/docs/{doc_id}` 关闭码:4401 未登录 / 4403 非成员 / 4404 文档不存在;VIEWER 连接 readonly(忽略其 content)。
- content 消息:相同→仅回 saved;不同→先把旧内容存为版本 → `version+1` → 回发送者 saved + 广播其他连接 `remote{content,version,by}`。快照与保留策略由 `docs.save_doc_content()` 统一实现,**REST 与 WS 共用,勿在任一侧另写一份**。
- 客户端:防抖 800ms 发 content(WS 断开降级 PUT);收 remote 时无焦点且无脏改 → 覆盖 textarea,否则顶部提示条「{by} 更新了文档 [加载最新]」。
- presence 广播的用户对象含 `avatarColor`(服务端取色,前端不再本地兜底)。

### 4.6 文档编辑器(源码/预览双模式,已弃用 Vditor)

- 顶栏 `.seg` 切换「编辑 | 预览」:编辑态 = 无边框等宽 textarea(源码即真相);预览态 = `.markdown-body`(MdRender 渲染)。VIEWER 只读恒为预览。模式记忆 `td:doc-mode`。
- 引用:输入 `@` 或 `[[` 弹浮层(空查询列当前项目最近文档 + 最近文件),选中插入 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`;**图片文件例外,插原生 `![名称](/api/files/{id}/download?inline=1)`**(预览直接显示)。预览渲染为 tonal 胶囊 chip(选择器 `a[href^="teamdoc://"]`)。所有链接默认新标签页打开。浮层按 Backspace 退出并删除触发符,Esc 退出则保留字面量。**无序列化/反序列化层**。
- `/` 菜单:行首输入 `/` 弹"插入组件"菜单(标题×3/列表×3/引用/代码块/分割线/上传图片),textarea 保持焦点,继续输入按 label 过滤,↑↓ 循环,Enter/Tab 确认。**与浮动工具栏语义分开:浮动工具栏只做文字格式(加粗/斜体/删除线/行内代码/链接),不放上传**。
- 上传:粘贴/拖拽/菜单 → `POST /api/files/upload` → 光标处插 `![name](url)`。**自动归入项目根目录「文档附件」文件夹**(promise 缓存,没有则建)。
- 浮层定位:按光标上下可用空间取大侧放置并收缩 maxHeight,内容填充/过滤后**重新定位**(否则按空面板量高会翻出屏幕);键盘导航用自写 `scrollItemIntoView` 只滚面板列表,**不用 `scrollIntoView`**(会连带滚动祖先容器,把头部顶出视口;`.doc-editor-col` 已用 `overflow: clip` 根治)。浮层外框复用 `.menu.panel`。
- 布局:**单一滚动容器 `.editor-scroll`**,滚动条在页面右缘;内部 `.editor-canvas` 三列 grid(1fr / minmax(0,880px) / 1fr),正文相对整页居中。textarea 自动撑高(`height=scrollHeight`),编辑/预览滚动行为一致。
- **满出血**:`#view.view-fill` 的 padding 归零,文档页铺满主区;内边距下放到 `.doc-tree-col` 与 `.editor-head` 自行承担。树列无背景无圆角,只用 1px 分隔线与正文区隔;`.doc-row` 的胶囊形高亮保留(那是 Material You 导航规范)。
  **新增元素若放进编辑器列,必须自行处理水平内边距**,否则会因 `#view` padding 为 0 而贴边。窄屏 ≤720px 时树在上/正文在下,分隔线换成 `border-bottom`。
- **大纲功能已整体移除**(曾实现 scrollspy + docked/抽屉双形态,交互不够优雅)。后续若要重做请从零设计,**勿试图恢复旧代码**。
- 反链栏在文档内容流末尾,有反链才出现;历史版本预览与正文预览同走 MdRender,全站一条渲染路径。

### 4.7 服务端鉴权工具集(auth.py,改动权限逻辑前必读)

后续改动**只允许**通过这些入口鉴权,不要再手写 `ROLE_RANK.get(...) < ...` 比较:

| 工具 | 用途 |
|---|---|
| `current_user` / `require_write` / `require_admin` | 依赖注入式:登录、PAT write scope、全局管理员 |
| `require_project_role("EDITOR")` / `require_doc_role("VIEWER")` | 依赖注入式按路径参数取资源并校验角色(不存在→404) |
| `ensure_project_role(db, ctx, project_id, required)` | 路由内已拿到资源对象时使用;不足则 403 |
| `get_project_or_404(db, project_id)` | 取项目或 404 |
| `require_write_ctx(ctx, db)` | 已带角色依赖的路由里补 PAT write scope 校验(定义在 auth,**勿从 docs 导入**) |
| `project_role(db, project_id, user)` | 只查角色不抛错(用于序列化 myRole) |

**判定顺序约定(删除/回收站类端点)**:资源不存在→404;权限不足→403;状态不符→409。
即**授权判定必须先于资源状态判定**,否则非成员可凭 409/403 的差异探测他人资源状态。

### 4.8 文件夹回收站语义

- 文件夹删除 = 软删除进项目回收站(可恢复),与文档/文件一致;回收站接口返回 `{docs, files, folders}`。
- 恢复文件夹时若父文件夹仍在回收站,回落项目根目录(对齐文档恢复语义)。
- 彻底删除文件夹会**级联**清除子树内所有文件夹与文件(含物理文件),返回 `{removedFolders, removedFiles}`。
- "非空不可删"只统计**未删除**内容:子项已各自进回收站时,父文件夹可直接删除;这些子项留在回收站,直到父文件夹被彻底删除时一并级联清除。
- 前端回收站用 `.seg.auto` 三段切换,数据一次取回后缓存在内存(切 tab 只重渲染);默认落在**第一个非空分类**;空分类的段禁用并降透明度,段上带数量徽标。

### 4.9 列表截断

所有列表查询 `LIMIT 500`。云空间接口额外返回 `total:{folders,files}`(截断前计数),前端据此显示 `.banner.warn` 提示("共 N 项,仅显示前 500 项,建议拆分到子文件夹"),**不再静默丢项**。
若后续要支持更大规模,**需引入分页而非提高上限**。回收站与搜索目前仍是静默截断。

## 5. API 契约要点(前端/调用方视角)

- 前缀 `/api`;成功直接返回 JSON(无信封);删除类返回 `{"ok":true}` 或 204;错误 `detail={"code","message"}`,状态码:400 VALIDATION / 401 UNAUTHORIZED(+TOTP_REQUIRED)/ 403 FORBIDDEN / 404 NOT_FOUND / 409 CONFLICT。
- 认证解析顺序:`Authorization: Bearer tdp_...`(PAT)→ Cookie 会话。PAT `scopes=read` 时非 GET → 403。TOTP 已开启且会话未验证 → 除 `/api/auth/totp/*`、`/api/auth/logout`、`/api/auth/me` 外一律 401 TOTP_REQUIRED(PAT 不受此门禁)。PAT 的创建与吊销端点(`/api/auth/pats`)仅接受 Web 会话。
- 角色:OWNER>ADMIN>EDITOR>VIEWER(3/2/1/0);全局 `is_admin` 在任何项目视为 ADMIN。
- 文档引用格式(写入 markdown):图片 `![名称](/api/files/{id}/download?inline=1)`、附件 `[名称](/api/files/{id}/download)`、文档/文件引用 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`。
- 反链:`GET /api/docs/{id}/backlinks`(VIEWER 起;同项目未删除文档中 LIKE `%teamdoc://doc/%/{id})%`,排除自引用,限 100 条)。
- 云空间:`GET /api/files?project_id=` 文件项带 `referenced`(项目内文档正文是否引用该文件,用于"被引用"徽标与删除警告)与 `total:{folders,files}`;`GET /api/search?q=` 的 files 结果带 `mime`(编辑器据此把图片插成原生 `![]()`)。
- 批量与回收站:`GET /api/files/zip?ids=a,b,c` 打包(≤200 个,SpooledTemporaryFile 先压后流式回吐,**必带 Content-Length**,否则 chunked 下载浏览器无进度且 Chrome 安全检查期像"卡住");恢复/彻底删除:文件、文档、文件夹各有 `/restore` 与 `/permanent`(仅限回收站中的项);回收站列表 `GET /api/projects/{id}/trash` → `{docs,files,folders}`。
- 前端配套:上传走 XHR(fetch 无上传进度),并发 3 队列 + 右下角进度面板;文件夹上传(webkitdirectory / 拖拽 `webkitGetAsEntry` 递归,路径→folderId 会话内缓存串行建目录);多选后**表头原地变身**批量操作(Gmail 式,不另起行避免列表抖动);批量下载用锚点 `<a download>`(window.open 对附件流不可靠)。
- 静态资源(`/css/`、`/js/`、`/vendor/`、`/index.html`)统一 `Cache-Control: no-cache`,浏览器每次携 ETag 重验证 —— 杜绝改版后跑旧 JS。
- `avatarColor` 不落库,由 user id 哈希在调色板确定性取色,**所有涉及用户的接口都返回它**(历史上前端曾另有一套调色板,导致同人异色)。

## 6. 测试与验证惯例

**已备好自动化脚本,改完直接跑**(见 `lite/tests/README.md`):

| 脚本 | 用途 |
|---|---|
| `smoke_all_endpoints.py` | 遍历全部 API 路由断言期望状态码,**任何 5xx 视为失败**。改完服务端先跑,是 NameError/TypeError 类回归的护栏 |
| `test_folder_recycle.py` | 文件夹回收站闭环 + 权限语义 |
| `test_avatar_color.py` | 头像取色跨接口一致性(含 WS presence) |
| `verify_page_assets.py` | 模拟浏览器加载全部静态资源,校验零外链 + no-cache。**改完前端 / 内网部署前后必跑** |

- 启动:`.venv/Scripts/python.exe main.py`,配 `TEAMDOC_DATA_DIR`(隔离数据)+ `PORT`(非常用端口)。首次跑 `tests/_bootstrap.py` 建测试管理员(admin@teamdoc.local / admin12345)。
- 手写脚本:标准库 urllib、**显式 UTF-8**;**不要用 curl 发中文**(Windows GBK 会乱码入库);URL 里的中文必须 `urllib.parse.quote`;下载类响应不是 JSON,解析前先看 Content-Type;WS 用 websockets 库测。
  **WS 地址必须从 `TD_BASE` 推导,不要硬编码端口** —— 硬编码会让脚本悄悄连到默认端口那台服务,拿本地会话去认另一台的会话,报出 `4401 未登录` 这种把人引向错误方向的假失败。
- 前端:改动后全部 JS 过 `node --check`;删 CSS 类前必须 grep 反查零引用(注意 `'cls-' + x` 动态拼接);CSS 改完检查 `{` `}` 数量相等。
- 服务进程管理(Windows):`netstat -ano | grep :8123` 找 PID,`taskkill //F //T //PID <pid>` 杀。**测试残留进程会占端口,导致"双实例 + 脏 Cookie"的诡异故障,测完必杀**。

**像素级验证(前端改动强烈建议)**:本项目用无头 Chrome 截图 + 逐图差异比对抓视觉回归,比目测可靠(靠它发现过表格列错位 140px、导航状态类 bug、组件化重构中 3 处行高回归)。要点:

- 临时校验页放 `lite/web/` 下即可用站内绝对路径引真实样式与逻辑,**用完必须删,不要留在仓库**。
- 让真实 `app.js` 跑起来:用**同步 XHR 先登录**拿会话 Cookie(会话是 HttpOnly,无法用 `document.cookie` 伪造)。
- **`--virtual-time-budget` 下 CSS 过渡不推进**:`getComputedStyle().width` 会停在过渡起始值,看起来像 bug。判定前先注入 `transition: none !important` 隔离。
- 涉及时间的界面(如"已保存 HH:MM")必须在页面里冻结 `Date`,否则两张截图时间戳不同,差异全是噪声。
- 断言写进页面 DOM(截图可读),不要只打 console;控制台错误用 `window.onerror` + 覆写 `console.error` 收集后一并显示。
- **静态截图 + "无 JS 错误" 抓不到交互失效**:用组件工厂替换标记时,若处理器还在读旧属性名
  (发生过:`UI.seg` 输出 `data-key`,处理器仍读 `data-mode` → 按钮点了没反应,
  既不报错、元素也在,只是选择器匹配不到)。这类 bug 必须**真实点击 + 断言状态变化**才能发现。
- **换掉标记后,务必 grep 所有读取该标记的处理器**(`dataset.x` / `[data-x]`),这是成对改动。
- **每步操作后即时快照求值**;若等所有点击做完再统一断言,会拿到最终状态而误判。

## 7. 已知遗留 / 改进候选

- **窄屏常驻 72px 图标栏后,项目子项只有图标没有文字**(`.side-label` 被隐藏),项目一多会退化成"一列图标",辨识度受限。这是统一导航机制时接受的取舍;若反馈不好,可让窄屏恢复"完整侧栏 + 抽屉"两态(删掉 `applyNavMode` 里窄屏恒折叠那一行即可),或给子项加 tooltip。
- **每次列云空间目录都会全量扫描该项目所有文档正文**(为算 `referenced` 徽标,`files.py` 用 `LIKE '%/api/files/%'` 取回 content 后正则提取)。30 人规模无感,文档量上千后应改为在 doc 保存时维护引用索引表。
- 引用浮层 Esc 关闭后,已输入的字面量 `@` / `[[` 保留在正文中(需手动删)。
- 远端覆盖为全文替换,光标位置仅粗粒度保留(字数变化大时会偏);真字符级协同见 §8。
- marked 行内公式 `$...$` 对价格类文本(如 $5 和 $10)可能误判为公式;KaTeX 加载失败时公式按源码显示。
- 冷加载个人项目瞬间「成员」导航项可能闪现(项目对象未返回前 `visible` 默认为显示);有缓存后不再出现。
- 文档树(递归 children)与侧栏项目树(扁平列表)因数据结构不同仍未合并渲染函数;两者现在共用视觉语言,仅在展开态管理上各自独立。
- 面包屑父链无 API,前端会话内维护路径栈,刷新回根目录。
- 项目间移动文件固定落到目标项目根目录(接口支持 `folderId`,前端未做选择器)。
- 搜索仍是静默截断(§4.9)。
- 无现成备份脚本:备份即打包 `server/data/` 整个目录(库 + 物理文件)。

## 8. 路线图参考(用户已表达过兴趣的方向)

- CLI `td`:PAT 认证 + JSON 信封 + API 透传 + `--dry-run`,纯标准库单文件,服务端零改动。
- 日历模块:`PROJECT_NAV` 加一项即可接入。
- 字符级真协同:pycrdt/Yjs 替换 LWW。
