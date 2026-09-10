# TeamDoc Lite — Handoff 文档

> 写给后续接手的 Agent / 开发者。**本文档是代码库当前状态的权威说明,与代码冲突时以此为准,并顺手修正本文档。**
> 更新日期:2026-09-10(功能完成 + 编辑器重构 + 组件化重构 + 迁移机制/上传安全/存储运维
> + 云空间整改:文件夹递归操作、raw body 上传、分页、网格视图、站内预览、跨项目最近)

---

## 1. 产品现状

TeamDoc Lite:30 人小团队自部署知识库。项目(组)管理文档、实时协同编辑、云空间、全文搜索。界面全简体中文。

**功能已完整**:认证(bootstrap / TOTP / PAT / 用户管理)、项目与成员、文档树、Markdown 源码/预览双模式编辑、WebSocket 协同、版本历史、云空间(上传/下载/打包含目录结构/文件夹递归删除恢复移动/项目内与跨项目移动/分页/网格图片墙/站内文本预览/占用统计)、回收站(文档+文件+文件夹,只列子树根)、全文搜索、跨项目最近文件、同事目录(成员选择器)、发现广场(公开项目 + 最近动态)、公开项目与单文件公开,管理后台(存储总览/孤儿清理/整站备份)。

**明确不做**:CLI `td`(将拆为独立项目,服务端零改动即可支持)、日历、字符级协同、S3、通知。

## 2. 技术栈与运行

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13 / FastAPI + uvicorn(**必须单 worker**)+ SQLite(SQLAlchemy 2.0,WAL,busy_timeout=5000) |
| 前端 | 纯 HTML/CSS/JS,零框架零打包;**依赖全部本地 vendor,无任何外网请求** |
| 认证 | scrypt 密码(stdlib)、Cookie 会话(`td_sid`,HttpOnly+SameSite=Lax)、PAT(`tdp_` 前缀存 sha256)、TOTP(pyotp) |
| 迁移 | 自研 `migrations.py`:schema_version 表 + 有序迁移列表,启动时按版本递增执行 |

```bash
cd lite/server
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker
```

- 首次启动只建表、不预置账号;浏览器访问走初始化向导(bootstrap 建首个管理员)。
- 数据在 `server/data/`(已 gitignore)。`TEAMDOC_DATA_DIR` 可改数据目录,供测试隔离用。
- 环境变量:`PORT`(8000)、`MAX_UPLOAD_MB`(2048)、`SESSION_TTL_DAYS`(7)、`VERSION_MERGE_MINUTES`(5,见 §4.5)、`STORAGE_RESERVE_MB`(1024,上传要求保留的磁盘余量,见 §4.10)。
- **内网/离线部署直接可用**:第三方资源在 `web/vendor/`(随仓库提交),服务端零外网调用,无联网安装步骤。

**为什么必须单 worker**:SQLite 是单写者模型。多进程会出现推送丢失、写锁冲突等难以排查的问题。

### 2.0 数据库迁移(加字段必读)

`Base.metadata.create_all()` **只建缺失的表,不会给已有的表加列**。所以加字段必须走 `migrations.py`:

1. 在 `models.py` 给模型加字段(`create_all` 负责全新的库)
2. 在 `MIGRATIONS` 末尾追加 `(版本号, 说明, 执行函数)`,函数内用 `_add_column(conn, 表, 列, 声明)` 补存量库
3. **不要修改已发布的迁移条目** —— 它们的版本号已写进库

SQLite 的 `ALTER TABLE ADD COLUMN` 有两条硬约束:不能加 NOT NULL 且无默认值的列,默认值必须是常量,所以新增列一律"可空 + 默认值由应用层写入"。迁移在启动时自动执行(`main.py` 的 `migrations.run(models.engine)`),每个迁移一个事务,可重复执行。

> 实测过的坑:WAL 模式下**直接复制 `teamdoc.db` 会拿到一张空库**(未 checkpoint 的写入全在 `-wal` 里)。备份必须用 `VACUUM INTO`,见 §4.11。


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
│   ├── main.py      (67)   入口:建表/迁移、路由注册(auth→docs→files→search→admin→ws→静态托管,顺序不能乱)、
│   │                       422→400 VALIDATION、no-cache + 安全响应头中间件
│   ├── migrations.py (~95) **数据库迁移**(§2.0):schema_version 表 + 有序迁移列表,启动时补齐存量库
│   ├── models.py    (167)  9 张表:users/sessions/pats/projects/project_members/docs/doc_versions/folders/files
│   │                       + unlink_quiet(物理清理失败不回滚的唯一入口)
│   ├── auth.py      (~565) scrypt、会话、PAT、TOTP(**含管理员重置**)、**鉴权工具集**(§4.7)、
│   │                       用户管理、同事目录(/api/users/directory)、create_personal_project
│   ├── docs.py      (537)  项目/成员/文档树/内容/版本(§4.5)/回收站/反链;save_doc_content 为 REST 与 WS 共用
│   ├── files.py     (~640) 云空间:raw body 流式上传/下载(inline,含 mime 服务端判定与白名单 §4.10)、
│   │                       分页与服务端排序(§4.9)、zip 打包(含文件夹递归)、文件夹树/移动/递归删除恢复
│   ├── admin.py     (~260) **管理后台(仅 is_admin)**:存储统计(§4.11)、无主文件清理、整站备份导出
│   ├── search.py    (~120) LIKE 搜索 + 权限过滤 + snippet + /api/recent(跨项目最近)
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
                                search/discover(发现:广场+动态)/admin(存储+用户)/settings
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
- **保护规则**:不可删(403「个人空间不可删除」)、不可管理成员(403)、**不可公开**(403,连设 private 也拒),可改名/描述。个人空间是私有草稿区,一旦能公开用户就不敢往里放东西,而那正是它的价值。
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
| `project_role(db, project_id, user)` | 只查角色不抛错(用于序列化 myRole)。**成员 → 角色;否则管理员 → ADMIN;否则公开项目 → VIEWER;否则 None**(§4.14) |
| `is_project_member(db, project_id, user)` | **真实成员关系**(不含管理员与公开项目访客)。区分"能写"与"只是看得到"必须用它 |

**判定顺序约定(删除/回收站类端点)**:资源不存在→404;权限不足→403;状态不符→409。
即**授权判定必须先于资源状态判定**,否则非成员可凭 409/403 的差异探测他人资源状态。

### 4.8 文件夹语义(递归子树,与文档对齐)

**删除 / 恢复 / 彻底删除三者对称,均作用于整棵子树。** 历史语义是"非空拒删",后果是用户必须自底向上手工清空才能删掉一个目录(层级越深步骤越多,中途失败还留下删了一半的状态),而恢复又只恢复空壳 —— 同一个文件夹上三种操作语义各不相同。

- 删除文件夹 = **递归软删除**整棵子树(文件夹 + 后代文件夹 + 文件),单事务,返回 `{removedFolders, removedFiles}`。
- 恢复文件夹 = **整棵子树一起回来**。副作用须知:子树里**先前单独删掉**的东西会一起回来(与文档子树恢复一致);要精确区分需要给删除操作记批次 id,本轮不做。
- 恢复时若父文件夹仍在回收站,回落项目根目录(否则恢复出来仍然看不见)。
- 彻底删除 = 级联清除子树内全部内容(含物理文件)。
- **回收站只列"子树根"**:删除即整棵软删,若把子项也列出来,删一个目录会让回收站一次多出几十条,而它们本就随根一起恢复。判据是"父级未删除或父级不存在";文件夹的已删 id 集合用**全量查询**(不受 500 条截断影响),否则父级不在前 500 条时会被误判成根。
- **移动** `POST /api/files/folders/{id}/move`:项目内整理只需 EDITOR(整理自己项目的目录不该要求管理员);跨项目需源 ADMIN + 目标 EDITOR;拒绝移入自己的后代(409)。跨项目时**整棵子树一起改 `project_id`**,否则会留下跨项目悬挂结构。文件移动同样放宽为项目内 EDITOR 即可。
- **文件夹树** `GET /api/projects/{id}/folders/tree` 是"需要文件夹层级"时的唯一入口:移动目标选择器、面包屑重建(见 §4.13)、上传目录路径缓存。别再各写一套扁平遍历。
- 前端回收站用 `.seg.auto` 三段切换,数据一次取回后缓存在内存(切 tab 只重渲染);默认落在**第一个非空分类**;空分类的段禁用并降透明度,段上带数量徽标。

### 4.9 列表分页与排序

云空间列表**分页 + 服务端排序**(`offset/limit/sort/dir`),取代原先的"单次 500 条 + 建议拆目录"——一旦当 NAS 用,扁平大目录很常见,把系统限制转嫁给用户不是办法。

- **排序必须在服务端**:分页与排序是一体的,客户端只排当前页会得到"每页内部有序"这种看似正确实则错误的结果。排序字段走白名单映射(`_FILE_SORTS`),非法值回落默认而非报错。
- 排序键含 `id` 兜底,保证同名项翻页时**不重不漏**。
- 文件夹不参与分页(单目录的文件夹数远小于文件,且树形渲染需要全量),上限 2000;文件走分页(默认 100/页 + 「加载更多」)。
- `hasMore` 由服务端算(与 `total` 一起),前端不自己推断。
- 回收站与搜索仍是静默截断 500(未改)。

### 4.10 上传类型判定与 inline 白名单(安全边界,勿放宽)

**mime 由服务端按文件名判定**(`guess_mime`:`mimetypes` + `_EXTRA_MIME` 覆盖表),**不采信客户端声明**。历史上的实现两者都采信,于是任何人上传一个 SVG(内含 `<script>`)再点"预览",浏览器就在**同源**下执行它——中招的是点开它的管理员;内网不是豁免理由,能上传的普通成员即可钓管理员。

- **inline 是显式白名单**(`_INLINE_MIME`):图片(png/jpeg/gif/webp/bmp/avif/ico)、PDF、文本类。**svg / html / xhtml 与一切未知类型强制 attachment**,且响应 `media_type` 回落 `application/octet-stream` 防 sniffing。
- 全站安全响应头(`main.py` 中间件):`X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、`Referrer-Policy: same-origin`。**未上 CSP**:`index.html` 有内联防闪烁脚本、视图大量内联 style,需 nonce/hash 改造,收益不抵返工。
- 接口回吐 `canInline` / `isText`,**前端不再各写一套 mime 猜测**(`drive.js` 的预览图标、编辑器插图、doceditor 的 `@` 浮层图片判定统一走服务端标志位)。前端若自行按 `image/*` 前缀判断,白名单一收紧就会出现"预览按钮在、点了却下载"。加新类型时**只需改服务端白名单**,前端自动跟上。

### 4.11 存储与磁盘(管理后台 `/api/admin/*`,仅 is_admin)

**三件事互相咬合,改一处要看另两处:**

1. **物理文件的生命周期**。删除 = 软删除(进回收站,物理文件保留);彻底删除 / 删项目 才 unlink。清理失败**不回滚**——DB 记录已提交,残留由孤儿清理兜底,统一走 `models.unlink_quiet()`(替掉了三处重复的 try/except)。
2. **无主文件(孤儿)**:磁盘上有、DB 里无引用。来源是上传中途失败、删项目残留。它占着空间却在界面上永远看不见,所以必须有一个能发现并清理它们的入口(`GET /api/admin/storage` 报告 + `POST /api/admin/storage/cleanup` 清理)。`missing`(DB 有记录、磁盘无文件)只报告**不自动处理**——删记录会把问题藏起来,正解是从备份恢复。
3. **占用统计的语义**:按**项目**算而非按人(文件可跨项目移动且不改 `created_by`,按人统计会随移动漂移;本项目也**不设硬配额**),**回收站占用与活跃占用必须分列**(回收站文件仍占物理磁盘,混在一起会出现"删了文件占用没变"的困惑)。前端占比条的分母只用 TeamDoc 自身占用,不用整盘容量——小团队数据量下后者会让整条几乎全白。

**上传的两个守卫**:写盘全程 try/except(任何异常都 unlink 半成品,否则成为永久孤儿);写盘前与循环中检查 `shutil.disk_usage` 余量(`STORAGE_RESERVE_MB`,默认 1024),把"磁盘写满 → 500 + 半截文件"变成明确的 400 提示。

### 4.12 备份(必读)

`GET /api/admin/backup` 导出整站 zip,内含 `teamdoc.db` + `files/` 全部物理文件 + `RESTORE.txt`(恢复步骤)。

**备份出的库必须用 `VACUUM INTO` 生成**。WAL 模式下未 checkpoint 的写入还在 `teamdoc.db-wal` 里,直接复制 `.db` 会拿到旧快照——实测中复制出的库**一张表都没有**,用它恢复会表现为"最近的数据不见了"。`VACUUM INTO` 产出单文件完整副本,且不需要停机。

恢复必须**停服**后再解包(运行中覆盖库文件会写坏数据)。

### 4.13 云空间前端视图(列表 / 网格 / 预览 / 深链)

- **两种视图**,工具栏 `.seg` 切换,记忆在 `localStorage['td:drive-view']`:列表(`.data-table`,列头排序)/ 网格(图片墙,`.file-grid` + `.tile`)。网格态下 `#drive-table.drive-grid-mode` 隐藏列头(它是为列表设计的),但选中时批量操作栏仍要能用,故 CSS 是 `:not(.selecting)`。
- **网格默认"最新在前"**:图片墙的用途是看最近传了什么图,按名称排会让图片散在大目录各处。仅在用户**从未主动点过列头排序**时套用这个默认(`localStorage['td:drive-sort']` 为空),否则尊重用户选择。
- 缩略图直接用 `?inline=1` 原图 + CSS 缩放 + `loading="lazy"`,**不引 Pillow 生成缩略图**(内网带宽够,零后端依赖更值)。图片是否能 inline 由服务端的 `canInline` 决定。
- **文本/Markdown 走站内模态框预览**(`openPreview`):`.md` 用 `MdRender`(与正文预览同一条渲染路径),其余文本用 `<pre>` + 可选 Prism。图片/PDF 仍开新标签(浏览器原生渲染器更好用)。预览里给"存为文档"入口 —— 解决"上传的 .md 想改只能下载→改→再传,而再传是新建不是新版本"。
- **行/瓦片共用选择器** `ITEM_SEL = '.data-table-row, .tile'`(`itemOf` / `selectedRows` / 全选都用它),加新视图形态时必须同步它,否则多选会静默失效。
- **面包屑与 URL 双向同步**:`syncUrl()` 把当前目录写进 `#/p/{id}/files?folder={fid}`(用 `replaceState`,改 hash 会触发 app.js 整页重路由);`resolveStack()` 从文件夹树重建路径栈,使刷新与"从搜索结果跳进某目录"都能定位。搜索结果的文件行就链到这个深链。
- 文件类型图标/配色统一走 `UI.fileIcon(mime)`(在 ui.js,云空间与搜索页共用),配色类是 `--file-*` 固定色,主题无关。


### 4.14 公开与发现(可见性的所有含义集中在此)

**语义:公开 = 本实例所有登录用户可只读浏览,不产生成员关系。** 不是"好友可见"、不是"匿名可访问"——本项目明确不做匿名 token 分享链接(内网外的人访问不到本服务,收益低)。

- **鉴权只有一处改动**:`auth.project_role()` 里"公开且非成员 → VIEWER"。文档树、文档读写、云空间、回收站、搜索、WS 全部经它判定,因此一处即全站生效;WS 的 readonly 也自动正确(VIEWER < EDITOR)。
- **前端必须同时看 `isMember`**:公开项目的访客与 VIEWER 成员的 `myRole` 都是 VIEWER,仅凭 myRole 会渲染出"点了就 403"的写按钮。统一用 `UI.canEdit / canAdmin / canOwn`(在 ui.js,内部同时查 isMember),**不要再写 `roleRank(myRole) >= N`**。
- **个人空间永不可公开**:`patch_project` 硬拒(403,连设 private 也拒,避免状态歧义),广场与 `?all=1` 也一律排除。
- **单文件公开**(`files.is_public`):让"把这一份发给不在项目里的同事"成立,而不必公开整个项目。下载鉴权抽成 `files.ensure_file_access`:项目角色 → 单文件公开 → 403。
- **广场** `/api/discover/projects`:只列公开项目,按 `lastUpdatedAt`(项目内最近一次文档更新或文件上传)倒序 —— 30 人的项目数量不多,静态目录浏览比直接问同事还慢,所以把"最近有人在动"的排最前。前端发现页(`#/discover`)把广场与 `/api/recent` 合成一页,既发现又能看到动态。
- **搜索与最近文件必须并入公开项目**(`search.py` 的 `visible` 集合),否则会出现"广场里看得到项目、却搜不到里面的内容"。
- 关闭公开后非成员**立即**失去访问,无需等缓存过期(没有缓存层)。

### 4.15 迁移机制的实际用法(以本项目新增字段为例)

见 §2.0。真实例子(版本 2):加 `projects.visibility` 与 `files.is_public` 只需两步 —— 在 `models.py` 加字段、在 `MIGRATIONS` 追加一条用 `_add_column` 的迁移。默认值 `'private'` / `0` 保证**存量数据行为完全不变**(实测 55 个存量项目全为 private、0 个文件公开),迁移不改变任何既有可见性。

## 5. API 契约要点(前端/调用方视角)

- 前缀 `/api`;成功直接返回 JSON(无信封);删除类返回 `{"ok":true}` 或 204;错误 `detail={"code","message"}`,状态码:400 VALIDATION / 401 UNAUTHORIZED(+TOTP_REQUIRED)/ 403 FORBIDDEN / 404 NOT_FOUND / 409 CONFLICT。
- 认证解析顺序:`Authorization: Bearer tdp_...`(PAT)→ Cookie 会话。PAT `scopes=read` 时非 GET → 403。TOTP 已开启且会话未验证 → 除 `/api/auth/totp/*`、`/api/auth/logout`、`/api/auth/me` 外一律 401 TOTP_REQUIRED(PAT 不受此门禁)。PAT 的创建与吊销端点(`/api/auth/pats`)仅接受 Web 会话。
- 角色:OWNER>ADMIN>EDITOR>VIEWER(3/2/1/0);全局 `is_admin` 在任何项目视为 ADMIN。
- **项目可见性**:`projects.visibility` 为 `private`(默认)/`public`;`PATCH /api/projects/{id}` 传 `isPublic` 切换(需 ADMIN,个人空间 403)。项目 JSON 带 `isPublic` / **`isMember`** / `lastUpdatedAt`;`myRole` 对公开项目的访客也返回 VIEWER,**判权限必须同时看 isMember**(§4.14)。
- **单文件公开**:`files.is_public`,`PATCH /api/files/{id}` 传 `isPublic`(需 EDITOR)。下载鉴权顺序:项目角色 → 文件公开 → 403。
- `GET /api/discover/projects`:公开项目广场(按最近活跃倒序)。
- 文档引用格式(写入 markdown):图片 `![名称](/api/files/{id}/download?inline=1)`、附件 `[名称](/api/files/{id}/download)`、文档/文件引用 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`。
- 反链:`GET /api/docs/{id}/backlinks`(VIEWER 起;同项目未删除文档中 LIKE `%teamdoc://doc/%/{id})%`,排除自引用,限 100 条)。
- 云空间:`GET /api/files?project_id=&folder_id=&offset=&limit=&sort=&dir=` 文件项带 `referenced`(项目内文档正文是否引用该文件,用于"被引用"徽标与删除警告)、`canInline`/`isText`(§4.10,前端据此决定预览方式),响应带 `total:{folders,files}` 与 `hasMore`(§4.9)。`GET /api/projects/{id}/storage` 给占用统计(§4.11 的分列规则)。`GET /api/recent` 给跨项目最近文件与文档(带 `projectName`/`folderId`,供搜索页空态与"进入所在目录")。
- **上传是 raw body**(非 multipart):`POST /api/files/upload?projectId=&folderId=&name=`,请求体即文件内容。原因见 §4.10 末尾;前端用 XHR 以便拿进度,`api.js` 已支持 Blob body。
- 重名:上传**自动加后缀**(`foo.png` → `foo(2).png`,用户没有"为文件起名"的动作);用户显式操作(新建文件夹 / 重命名)**遇重名返回 409**,不静默改名。改名会同步重算 mime(改扩展名后预览/下载行为必须跟着变)。
- `GET /api/search?q=` 的 files 结果带 `mime`/`canInline`/`projectName`/`folderId`(编辑器据此把图片插成原生 `![]()`、结果里显示所在项目并可跳进目录)。
- 管理后台(仅 is_admin,`admin.py`):`GET /api/admin/storage`(占用总览 + 孤儿扫描)、`POST /api/admin/storage/cleanup`、`GET /api/admin/backup`(整站 zip,§4.12)。
- `GET /api/users/directory`(任意登录用户):同事目录,只返回 `id/name/email/avatarColor`,**不含 isAdmin/isDisabled/createdAt**(那是管理后台的字段面),禁用账号不出现。前端 `UI.personPicker` 用它做成员选择器(取代让用户手打同事邮箱)。
- `POST /api/users/{id}/totp/reset`(仅管理员):关闭该用户两步验证。**必须有这条出路**——用户换手机后自己无从重置,账号会永久锁死(只能手改数据库)。
- 批量与回收站:`GET /api/files/zip?ids=a,b&folderIds=c,d` 打包(文件夹递归展开并**保留目录结构**;文件数上限 1000,超限拒绝而非截断 —— 下载备份场景下拿到不完整的包却以为是全部更危险。SpooledTemporaryFile 先压后流式回吐,**必带 Content-Length**,否则 chunked 下载浏览器无进度且 Chrome 安全检查期像"卡住");恢复/彻底删除:文件、文档、文件夹各有 `/restore` 与 `/permanent`(仅限回收站中的项);回收站列表 `GET /api/projects/{id}/trash` → `{docs,files,folders}`(只列子树根,§4.8)。
- 前端配套:上传走 XHR(fetch 无上传进度),并发 3 队列 + 右下角进度面板;文件夹上传(webkitdirectory / 拖拽 `webkitGetAsEntry` 递归,路径→folderId 会话内缓存串行建目录);多选后**表头原地变身**批量操作(Gmail 式,不另起行避免列表抖动);批量下载用锚点 `<a download>`(window.open 对附件流不可靠)。
- 静态资源(`/css/`、`/js/`、`/vendor/`、`/index.html`)统一 `Cache-Control: no-cache`,浏览器每次携 ETag 重验证 —— 杜绝改版后跑旧 JS。
- `avatarColor` 不落库,由 user id 哈希在调色板确定性取色,**所有涉及用户的接口都返回它**(历史上前端曾另有一套调色板,导致同人异色)。

## 6. 测试与验证惯例

**已备好自动化脚本,改完直接跑**(见 `lite/tests/README.md`):

| 脚本 | 用途 |
|---|---|
| `smoke_all_endpoints.py` | 遍历全部 API 路由断言期望状态码,**任何 5xx 视为失败**。改完服务端先跑,是 NameError/TypeError 类回归的护栏。测试用户邮箱带随机后缀,**可重复运行** |
| `test_folder_recycle.py` | 文件夹回收站闭环 + 权限语义 |
| `test_upload_security.py` | **上传安全**:伪装 svg/html、未知类型、白名单类型、客户端中途断开、超限拒绝,含物理文件残留检查 |
| `test_admin_storage.py` | **管理后台**:存储统计、孤儿识别与清理、删项目清物理文件、回收站占用单列、备份完整性、TOTP 重置 |
| `test_avatar_color.py` | 头像取色跨接口一致性(含 WS presence) |
| `verify_page_assets.py` | 模拟浏览器加载全部静态资源,校验零外链 + no-cache。**改完前端 / 内网部署前后必跑** |

- 启动:`.venv/Scripts/python.exe main.py`,配 `TEAMDOC_DATA_DIR`(隔离数据)+ `PORT`(非常用端口)。首次跑 `tests/_bootstrap.py` 建测试管理员(admin@teamdoc.local / admin12345)。
- **测试脚本必须可重复运行**:实例的数据目录常被复用,断言要写成差值或唯一值,别假设"数据目录是干净的"——历史残留(比如孤儿文件)正是被测功能要处理的东西,设绝对值为 0 会假失败。
- 手写脚本:标准库 urllib、**显式 UTF-8**;**不要用 curl 发中文**(Windows GBK 会乱码入库);URL 里的中文必须 `urllib.parse.quote`;下载类响应不是 JSON,解析前先看 Content-Type;WS 用 websockets 库测。
  **WS 地址必须从 `TD_BASE` 推导,不要硬编码端口** —— 硬编码会让脚本悄悄连到默认端口那台服务,拿本地会话去认另一台的会话,报出 `4401 未登录` 这种把人引向错误方向的假失败。
- 前端:改动后全部 JS 过 `node --check`;删 CSS 类前必须 grep 反查零引用(注意 `'cls-' + x` 动态拼接);CSS 改完检查 `{` `}` 数量相等。
- **`dict(resp.headers)` 的键大小写不稳定**:测试里读响应头请小写化比较,否则 `Content-Type` / `content-type` 会漏判。
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
- **`referenced` 徽标仍靠全项目扫描文档正文**(`LIKE '%/api/files/%'` + 正则提取,分页后只匹配当前页 id)。单次成本仍是 O(全项目正文),文档量上千后应改为在 doc 保存时维护引用索引表(`doc_file_refs`)。
- 引用浮层 Esc 关闭后,已输入的字面量 `@` / `[[` 保留在正文中(需手动删)。
- 远端覆盖为全文替换,光标位置仅粗粒度保留(字数变化大时会偏);真字符级协同见 §8。
- marked 行内公式 `$...$` 对价格类文本(如 $5 和 $10)可能误判为公式;KaTeX 加载失败时公式按源码显示。
- 冷加载个人项目瞬间「成员」导航项可能闪现(项目对象未返回前 `visible` 默认为显示);有缓存后不再出现。
- 文档树(递归 children)与侧栏项目树(扁平列表)因数据结构不同仍未合并渲染函数;两者现在共用视觉语言,仅在展开态管理上各自独立。
- **回收站与搜索仍是静默截断 500**(云空间已改真分页,见 §4.9)。回收站的子树根过滤会用全量 id 集合,所以不会因为截断而漏判父级;但单分类超过 500 个根时仍会少列。
- **文件夹恢复不区分删除批次**:子树里先前单独删掉的东西会随着一起回来(与文档子树一致)。要精确区分需给删除操作记批次 id。
- 打包下载文件数上限 1000,超限直接拒绝(不截断)。真要打更大的集合需要流式边压边发(现在受限于"先落 spool 再回吐以带 Content-Length")。
- 文本预览对超大文件没有截断保护:会整体读入浏览器内存(服务端不受影响)。几十 MB 的日志文件预览会卡;需要时加一个"仅预览前 1MB"。
- 无主文件只能靠管理后台手动清理(有兜底入口,无自动巡检);删除项目/彻底删除时的 unlink 失败会静默留下残留。
- **公开项目的成员列表对所有登录用户可见**(含邮箱)。这与同事目录的可见面一致(§4.14),30 人内网可接受;若要收紧,需要给成员列表加"仅成员可见"的分支,并想清楚公开项目靠什么展示参与情况。
- 公开项目目前是**全实例公开**,没有"按部门/小组"这类范围控制(本项目不引入 Team 实体,见 §4.14 的取舍)。
- 广场没有分页(一次返回全部公开项目)。项目数量到几百时需要加,但那时更该先做的是项目归档。
- 无部署运维文档(Windows 服务化 / 反代 body 上限与超时 / 日志落盘轮转);uvicorn 日志只进 stdout,重启即丢。

## 8. 路线图参考(用户已表达过兴趣的方向)

- CLI `td`:PAT 认证 + JSON 信封 + API 透传 + `--dry-run`,纯标准库单文件,服务端零改动。
- 日历模块:`PROJECT_NAV` 加一项即可接入。
- 字符级真协同:pycrdt/Yjs 替换 LWW。
