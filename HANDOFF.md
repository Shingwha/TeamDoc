# TeamDoc Lite — Handoff 文档

> 写给后续接手的 Agent / 开发者。本文档是当前代码库的**实际状态快照**,与《构建文档.md》(原始规格)有出入处均已标注——**代码以此文档为准,规格文档仅作背景参考**。
> 更新日期:2026-09-10(第一轮构建 + 多轮 UI 重构 + 编辑器重构[去 Vditor,源码/预览双模式] + 内网化/正确性/精简/设计一致性四轮优化 + 布局重构[移动端统一导航/文档页平铺/回收站分段]后)
> 全部改动均已分阶段 commit(P0 内网化 → P1 正确性 → P2 精简 → P3 设计一致性 → 布局重构),详见 `git log`。

---

## 1. 产品现状

TeamDoc Lite:30 人小团队自部署知识库。项目(组)管理文档、实时协同编辑、云空间、全文搜索。界面全简体中文。

**已完成**:§1-§14 全部功能(REST API、WebSocket 协同、TOTP、PAT、回收站、归属转移→已改为项目间移动、搜索),以及两轮重要产品演进(见 §5 决策记录)。

**未做(明确排除)**:
- CLI `td`(原 §16,后续拆为独立项目,服务端零改动即可支持)
- 原 §15 非目标清单全部(日历、字符级协同、S3、通知等)

## 2. 技术栈与运行

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13 / FastAPI + uvicorn(**必须单 worker**)+ SQLite(SQLAlchemy 2.0, WAL, busy_timeout=5000) |
| 前端 | 纯 HTML/CSS/JS,零框架零打包;**依赖全部本地 vendor(无任何外网请求,可纯内网部署)**:Remix Icon 4.5 + marked 11.1.1 + Prism 1.29.0(autoloader + 78 个语言包)+ KaTeX 0.16.9(懒加载) |
| 认证 | scrypt 密码(hashlib,stdlib)、Cookie 会话(`td_sid`, HttpOnly+SameSite=Lax)、PAT(`tdp_` 前缀存 sha256)、TOTP(pyotp) |

```bash
cd lite/server
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

- 首次启动只建表不预置账号,浏览器访问走初始化向导(bootstrap 建首个管理员)。
- 数据在 `server/data/`(已 gitignore);`TEAMDOC_DATA_DIR` 环境变量可改数据目录(测试隔离用,非原始规格)。
- 环境变量:`PORT`(8000)、`MAX_UPLOAD_MB`(2048)、`SESSION_TTL_DAYS`(7)。
- **内网/离线部署:直接可用**。前端第三方资源在 `web/vendor/`(1.5 MB,随仓库提交),
  `index.html` 与 `markdown.js` 全部指向 `/vendor/…`;服务端零外网调用。不需要任何联网安装步骤。

### 2.1 vendor 目录(内网部署关键,勿删)

```
web/vendor/
├── remixicon/   remixicon.css(已改相对路径)+ woff2/woff(已删 eot/ttf/svg)
├── marked/      marked.min.js
├── prism/       prism.min.js + prism-autoloader.min.js + components/(78 个语言包)
└── katex/       katex.min.js + katex.min.css + fonts/(40 个 woff2/woff,已删 ttf)
```

- **autoloader 必须放 `prism/prism-autoloader.min.js`**:它原本在 CDN 的
  `components/` 路径下不存在(历史上一直 404,导致代码高亮只剩 html/css/js)。
  正确来源是 `plugins/autoloader/prism-autoloader.min.js`,已本地化。
- 新增语言包:`web/vendor/prism/components/prism-<lang>.min.js`;
  autoloader 会按需拉取,依赖表里的 36 种语言已全部备齐(零缺失)。
- 升级依赖:替换 `web/vendor/` 下对应文件即可,无需改代码;
  升级 Remix Icon 时要保持 CSS 内字体的相对路径写法。
- `main.py` 的 no-cache 中间件已覆盖 `/vendor/`(文件名不含内容哈希,
  长缓存会导致升级不生效)。

## 3. 目录结构与文件职责

```
lite/
├── server/
│   ├── main.py      (67 行)  入口:建表、路由注册(auth→docs→files→search→ws→静态托管,顺序不能乱)、422→400 VALIDATION、
│   │                        no-cache 中间件(覆盖 /css /js /vendor / index.html)
│   ├── models.py    (167)    9 张表;users/sessions/pats/projects/project_members/docs/doc_versions/folders/files
│   ├── auth.py      (537)    scrypt、会话、PAT、TOTP、**鉴权工具集**(见 §4.6)、用户管理、create_personal_project
│   ├── docs.py      (469)   项目/成员/文档树/内容/版本(留 50 条)/回收站(文档+文件+文件夹)/反链 backlinks;
│   │                        save_doc_content(REST 与 WS 共用的版本快照实现);个人项目保护规则
│   ├── files.py     (413)   云空间:流式上传(1MB 块)/下载(inline|attachment, filename*=UTF-8'')/zip 打包下载(带 Content-Length)/
│   │                        文件夹 CRUD + 回收站恢复 + 彻底删除(级联子树、清物理文件)/项目间移动
│   ├── search.py    (56)     LIKE 搜索 + 权限过滤 + snippet(命中位置前后各 60 字符)
│   └── ws.py        (118)    /ws/docs/{doc_id} 协同:presence 广播(带 avatarColor)、LWW content→saved/remote、VIEWER readonly
├── tests/                   纯 stdlib 测试脚本(见 tests/README.md):全端点冒烟 / 文件夹回收站 / 头像取色 / 页面资源校验
└── web/
    ├── index.html   (82)     SPA 壳:侧栏(品牌行+搜索框/搜索钮+项目树+管理后台+个人设置+用户卡片)+ #view
    ├── vendor/               第三方前端资源(内网部署关键,详见 §2.1;勿删、勿改 import 路径)
    ├── css/
    │   ├── tokens.css   (190) 设计令牌:Material You 语义色(浅/深 × 6 种子色 + error/warning/success)、形状/间距/字阶/z-index 五档层级表
    │   ├── base.css     (82)   重置、[hidden]{display:none!important} 护栏、滚动条、mark/secret-box/md-fallback
    │   ├── components.css (394) 自研组件:按钮(6 变体)/表单/卡片/chip/头像/toast/模态框/菜单/空态/spinner/seg 分段切换/list-warn
    │   ├── app.css      (788)  壳布局、侧栏(折叠/抽屉共用一套规则)、登录页、各视图专属样式、响应式
    │   └── editor.css   (270)  文档编辑器全部样式:源码 textarea / .markdown-body 预览排版 / Prism 令牌映射 /
    │                           teamdoc:// chip / 引用浮层 .td-panel / 浮动工具栏 .td-floatbar / 反链栏
    └── js/
        ├── api.js       (52)   fetch 封装(契约见 §6);204→null;{detail:{code,message}}→ApiError;401 跳 #/login
        ├── theme.js     (65)   主题管理:light/dark/system + 6 种子色,localStorage 持久化,系统监听,onChange 订阅
        ├── ui.js        (505)  组件库(全部视图复用,禁止另造):toast/modal/confirmDialog/inputDialog/**formModal**/
        │                        dropdownMenu(支持 direction:'up')/avatar/emptyState/spinner/loadingRow/icon/
        │                        fmtSize/fmtDate/debounce/esc/copyText/roleRank/roleLabel
        ├── markdown.js  (104)  全站唯一 Markdown 渲染路径 MdRender.render/mount:marked + raw HTML 转义(防 XSS)+
        │                        $$/$ 公式扩展(KaTeX 按需懒加载)+ Prism autoloader 高亮(失败降级不高亮不报错);
        │                        marked 缺失时降级为纯文本预览(.md-fallback)
        ├── doceditor.js (568) 编辑器增强 DocEditor.enhance(textarea,{upload}):teamdoc:// chip 全局点击路由(注册一次)、
        │                        @/[[ 引用浮层(空查询给默认候选;图片文件插原生 ![]())、/ 行首插入组件菜单、
        │                        选区浮动工具栏(纯文字格式)、粘贴/拖拽/菜单上传(归入「文档附件」);返回 cleanup
        ├── app.js       (535)  hash 路由、壳装配、**侧栏状态机与项目树**(见 §4.2)、登录/初始化向导、PROJECT_NAV 配置数组、用户卡片菜单(含外观面板)
        └── views/              projects(项目首页)/project(文档模块+成员+回收站[.seg 三段切换]/设置)/
                                drive(云空间:统一列表+多选批量+上传队列)/search/admin/settings
```

## 4. 关键架构决策(后续改动必须理解这些)

### 4.1 个人空间 = 个人项目(已统一,非原始规格)
- 每个用户创建时(bootstrap/管理员建用户)自动获得 `is_personal=true` 的项目"个人空间",唯一成员=本人 OWNER。
- **保护规则**:个人项目不可删(403「个人空间不可删除」)、不可管理成员(403),可改名/描述。
- 列表:个人项目排最前;管理员 `?all=1` 不含他人个人项目(但直接访问 URL 仍有 ADMIN 权限,保留原语义);搜索同理隔离。
- **files/folders 已无 scope/user_id 字段**,全部归属 project_id;原"归属转移"变为项目间移动(`POST /api/files/{id}/move {projectId, folderId?}`,源项目 ADMIN + 目标项目 EDITOR)。
- 前端无 `#/drive` 独立视图,旧路由重定向到个人项目的 files tab。

### 4.2 侧栏(项目树 + 折叠/抽屉状态机)
- 侧栏 = 顶部品牌行(TeamDoc 文字 + 折叠钮)+ 搜索框(**折叠态顶替为搜索按钮**)+ **项目树** + 管理后台/个人设置 + 底部用户卡片。
- 项目行 = chevron + 纯文字(字重 600,**无图标无色点**),点击**只做展开/收起**,不导航;进入项目必须点子项。
- 子项由 `PROJECT_NAV` 配置数组驱动(app.js 顶部):`{key, icon, label, view, visible}`,`key` 即路由段 `#/p/{id}/{key}`。**加新模块(如日历)= 数组加一项 + 写一个视图函数**,路由/侧栏/高亮/tab 记忆全自动生效。
- 展开状态(2026-09 重构):内存 Map 是**唯一真相**,仅三处写入——用户点击行、一次性种子(刷新/深链首次渲染时展开当前项目)、新建项目后 `App.expandProject(pid)`;**路由变化只影响高亮,绝不动展开态**(离开项目去管理后台/个人设置不会误收起)。刷新回到种子状态。
- **侧栏状态模型(2026-09 二次重构,已废弃 `#mobile-bar` 顶条)** —— 只有两种状态,由同一套 CSS 承载:
  | | 折叠(72px 图标栏) | 展开(240px) |
  |---|---|---|
  | 宽屏 | 用户点折叠钮(挤压主区) | 默认 |
  | 窄屏 ≤960px | **恒为此态**(否则没有导航入口) | 点按钮/搜索钮 → `.nav-open` 抽屉**浮层化** |

  实现要点(改动前务必理解,否则极易破坏):
  - CSS 唯一入口是 `#shell.side-collapsed:not(.nav-open)` 一组规则(app.css)。`:not(.nav-open)` 是关键——
    抽屉打开时整组规则失效,侧栏自然回到完整布局,**因此不需要任何"反向展开"覆盖规则**。
  - JS(`applyNavMode` / `setUserCollapsed` / `syncCollapseBtn`,app.js 启动段):窄屏恒折叠且**不写 localStorage**,
    所以窄屏用不用抽屉都不会污染宽屏偏好;回宽屏即恢复用户原选择。
  - `#side-collapse` 按钮语义随屏宽变化:宽屏 = 折叠/展开图标栏;窄屏 = **抽屉开关**。
    `syncCollapseBtn()` 保证 title 始终如实反映"点了会怎样"。
  - 折叠态 `.side-search` 隐藏、`.side-search-btn` 显示(44px,与 `.side-item` 同规格),点击在窄屏开抽屉、
    宽屏先展开再聚焦。聚焦需 `requestAnimationFrame` 延后一帧(搜索框刚从 `display:none` 恢复,同帧 focus 无效)。
  - 窄屏下 `#sidebar` 是 72px 内联栏(不叠加遮罩),仅 `.nav-open` 时才变 `position:fixed` 抽屉(z-index 800)。

### 4.3 主题系统
- `html[data-theme]`(light/dark)× `html[data-color]`(blue/purple/green/orange/pink/cyan),**全部色值静态写在 tokens.css**,零运行时算色。
- 编辑器/预览全部色值经 editor.css 引用令牌(Prism 高亮色也用 color-mix 从令牌派生),**主题切换零 JS 处理**。
- 切换入口:底部用户卡片菜单内的"外观"面板。

### 4.4 文档编辑器(源码/预览双模式,2026-09 重构,已弃用 Vditor)
- 顶栏 `.seg` 分段切换「编辑 | 预览」:编辑态 = 无边框等宽 textarea(源码即真相);预览态 = `.markdown-body`(MdRender 渲染)。VIEWER 只读恒为预览。
- 模式记忆:localStorage `td:doc-mode`(edit/preview),默认编辑。
- 引用:输入 `@` 或 `[[` 弹搜索浮层(空查询默认列当前项目最近文档 + 云空间最近文件),选中插入标准 Markdown 链接 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`;**图片文件例外,插原生 `![名称](/api/files/{id}/download?inline=1)`**(预览直接显示,不靠 chip)。预览渲染为 tonal 胶囊 chip(CSS 选择器 `a[href^="teamdoc://"]`)。**所有链接(含 chip)默认新标签页打开**(renderer.link 统一加 `target=_blank`;chip 点击由 doceditor.js 全局路由 window.open)。浮层空查询按 Backspace 退出并删除触发符,Esc 退出则保留字面量。**无序列化/反序列化层**。
- `/` 菜单:行首输入 `/` 弹"插入组件"菜单(标题×3/列表×3/引用/代码块/分割线/上传图片附件),textarea 保持焦点、继续输入按 label 过滤,↑↓ 循环选择(首尾接续),Enter/Tab 确认。**与选区浮动工具栏语义分开:浮动工具栏只做文字格式(加粗/斜体/删除线/行内代码/链接),不放上传**。
- 上传:粘贴/拖拽/`/` 菜单 → `POST /api/files/upload`(projectId 必填)→ 光标处插 `![name](url)`(图片带 `?inline=1`)。**编辑器上传自动归入项目根目录「文档附件」文件夹**(ensureAttachFolder,promise 缓存,没有则建)。
- 浮层定位:placeAt 按光标上下可用空间取大侧放置并收缩 maxHeight,内容填充/过滤后重新 placeAt(否则按空面板量高会翻出屏幕);键盘导航用自写 scrollItemIntoView 只滚面板列表,**不用 scrollIntoView**(会连带滚动祖先容器——overflow:hidden 的祖先也能被程序化滚动,把头部顶出视口;`.doc-editor-col` 已用 overflow:clip 根治)。
- 布局(2026-09 二次重构):**单一滚动容器 `.editor-scroll`**,滚动条在页面右缘;内部 `.editor-canvas` 三列 grid(1fr / minmax(0,880px) / 1fr),**正文相对整页居中**。编辑态 textarea **自动撑高**(`height=scrollHeight`,自身 overflow:hidden 不内滚),编辑/预览滚动行为一致。
- 大纲功能(2026-09)曾实现过(scrollspy、docked/抽屉双形态),因交互不够优雅已**整体移除**(project.js 逻辑 + editor.css 样式 + 头部按钮);后续重做时从零设计,勿试图恢复旧代码。
- 反链栏:在**文档内容流末尾**(`.doc-content` 内部,margin-top:40px,无分割线),有反链才出现。
- 历史版本预览与正文预览同走 MdRender,全站一条渲染路径。

### 4.5 协同语义(LWW)
- WS `/ws/docs/{doc_id}`:4401 未登录/4403 非成员/4404 文档不存在;VIEWER 连接 readonly(忽略其 content)。
- content 消息:相同→仅回 saved;不同→旧内容存 DocVersion(label="自动",留 50 条)→ version+1 → 回发送者 saved + 广播其他连接 remote{content,version,by}。
  快照与保留策略由 `docs.save_doc_content()` 统一实现,REST 与 WS 共用(勿在任一侧另写一份)。
- 客户端:防抖 800ms 发 content(WS 断开降级 PUT /content);收 remote 时无焦点无脏改→覆盖 textarea(保留光标,程序赋值不触发 input 无需抑制回环),否则顶部提示条「{by} 更新了文档 [加载最新]」。
- presence 广播的用户对象含 `avatarColor`(服务端取色,前端不再本地兜底)。

### 4.6 服务端鉴权工具集(auth.py,改动权限逻辑前必读)
后续改动**只允许**通过这几个入口做鉴权,不要再手写 `ROLE_RANK.get(...) < ...` 比较:

| 工具 | 用途 |
|---|---|
| `current_user` / `require_write` / `require_admin` | 依赖注入式:登录、PAT write scope、全局管理员 |
| `require_project_role("EDITOR")` / `require_doc_role("VIEWER")` | 依赖注入式按路径参数取项目/文档并校验角色(文档不存在→404) |
| `ensure_project_role(db, ctx, project_id, required)` | 路由内已拿到资源对象时使用(如先 `db.get` 再校验);不足则 403 |
| `get_project_or_404(db, project_id)` | 取项目或 404(原先 docs/files 各有一份副本) |
| `require_write_ctx(ctx, db)` | 已带角色依赖的路由里补 PAT write scope 校验(定义在 auth,**勿再从 docs 导入**) |
| `project_role(db, project_id, user)` | 只查询角色不抛错(用于序列化 myRole 等) |

**判定顺序约定(回收站/删除类端点)**:资源不存在→404;权限不足→403;状态不符→409。
即**授权判定必须先于资源状态判定**,否则非成员可凭 409/403 的差异探测他人资源状态。

### 4.7 文件夹回收站语义(2026-09 修复)
- 文件夹删除 = 软删除进项目回收站(可恢复),与文档/文件一致;回收站接口返回 `{docs, files, folders}`。
- 恢复文件夹时若父文件夹仍在回收站,则回落项目根目录(对齐文档恢复语义)。
- 彻底删除文件夹会**级联**清除其子树内所有文件夹与文件(含物理文件),返回 `{removedFolders, removedFiles}`。
- "非空不可删"只统计**未删除**内容:若子项已各自进回收站,父文件夹可直接删除;
  这些子项留在回收站,直到父文件夹被彻底删除时一并级联清除。

### 4.8 列表截断提示
云空间单目录、回收站、搜索等列表均 `LIMIT 500`。云空间列表接口额外返回
`total: {folders, files}`(截断前计数),前端据此显示 `.list-warn` 提示条
("共 N 项,仅显示前 500 项,建议拆分到子文件夹"),**不再静默丢项**。
若后续要支持更大规模,需引入分页而非提高上限。

### 4.9 文档页平铺布局(2026-09 二次重构)
- **满出血**:`#view.view-fill` 的 padding 归零,文档页从主区左缘铺到右缘;
  内边距下放到 `.doc-tree-col`(水平 `--sp-2`)与 `.editor-head`(上/右 `--sp-4`/`--sp-6`)自行承担。
  对比旧版(树列圆角面板 + `#view` 24/28px 内边距 + 16px 间隙)横向省约 71px、纵向 48px。
- **树列不再是"悬浮面板"**:去掉了 `background` 与 `border-radius`,只用
  `border-right: 1px solid var(--md-outline-variant)` 与正文区隔;`.docs-wrap` 的 `gap` 归零。
- `.doc-row` 的**胶囊形高亮保留**(`--radius-full` + `--md-primary-container`),
  那是 Material You 导航规范,与"容器是否悬浮"是两件事,勿一并去掉。
- 窄屏 ≤720px:树在上/正文在下,分隔线由 `border-right` 换成 `border-bottom`,并恢复水平内边距避免文字贴边。
- 新增元素若放进编辑器列,**必须自行处理水平内边距**(`.editor-head` / `.remote-bar` 就是先例),
  否则会因 `#view` padding 为 0 而贴边。

### 4.10 回收站分类切换(2026-09)
- 三类(文档/文件/文件夹)原来是一个长列表里三段堆叠(`.tr-group` 小标题),项多时要滚很久;
  现改为 `.seg` 分段控件切换(复用编辑器「编辑|预览」同款组件,见 components.css)。
- **数据一次取回后缓存在内存**(`data` 变量),切 tab 只重渲染,不重新请求接口。
- 默认落在**第一个非空分类** —— 只删过文件时不会先看到空的"文档"页。
- 空分类的段**禁用**并降透明度,段上带数量徽标(`.seg-count`)便于一眼看出哪类有待处理项。
- 行副标题不再重复"文档/文件"字样(分类已由 tab 表达),只留 `大小 · 删除于 …`。
- `.tr-group` 已删除,勿再使用。

## 5. 与原始规格(构建文档.md)的出入汇总

| 项 | 规格 | 现状 |
|---|---|---|
| 个人空间 | 独立 scope=personal 云空间,无文档 | 统一为个人项目(含文档),scope 概念已删(§4.1) |
| 项目 color | projects.color 字段 + 前端取色器 | **已整体删除**(前端 UI + 后端字段);旧库物理残留 color 列,无害不读写 |
| 项目页布局 | 页头 + 顶部 Tabs + ⚙ 模态框组 | 侧栏项目树 + 子项独立路由视图(members/trash/settings) |
| 顶栏 | 48px 顶栏(搜索/头像区/菜单) | **已删除**;搜索/用户菜单在侧栏;协作者头像只在编辑器内联 |
| 云空间列表 | 文件夹表 + 文件表两个表格 | **统一单列表**(文件夹置顶,行 hover 操作,列头可排序);多选批量打包下载(zip)/删除,上传带进度面板与并发队列,支持整文件夹上传 |
| 回收站 | 只收文档,仅恢复 | 文档+文件+**文件夹**统一收,支持恢复与彻底删除(§4.7) |
| PAT 端点 | 规格只定义语义 | `GET/POST /api/auth/pats`、`DELETE /api/auth/pats/{id}`(创建/吊销仅 Web 会话) |
| avatarColor | users 表无此字段但登录响应要求 | 不落库,由 user id 哈希在调色板确定性取色;**所有涉及用户的接口都返回它**(§4.6 之前的坑:前端曾另有一套调色板导致同人异色) |
| append 快照 | 未说明 | 与 PUT 一致先存"覆盖前"快照 |
| 删项目 | 只删 docs/members | 一并删该项目 files/folders 记录(物理文件保留,规格 §15 明确不做物理清理) |
| 前端依赖 | CDN 引入(jsdelivr) | **全部本地 vendor**(`web/vendor/`,纯内网可用);见 §2.1 |
| 移动端布局 | (未定义) | **无独立移动版式**:窄屏侧栏常驻 72px 图标栏,展开即抽屉式浮层,宽窄屏共用同一套状态机(§4.2) |
| 文档页布局 | 树列 + 编辑器并列 | 满出血平铺,树列无背景圆角仅以 1px 分隔线区隔(§4.9) |
| 回收站 | 文档/文件单列表 | `.seg` 三段切换,数据内存缓存(§4.10) |

其余契约(§5 数据模型剩余字段、§7 API 路径、§8 协同协议)**严格遵守规格**,前端依赖这些字段名,改动前先查构建文档。

## 6. API 契约要点(前端/调用方视角)

- 前缀 `/api`;成功直接返回 JSON(无信封);删除类 `{"ok":true}` 或 204;错误 `HTTPException detail={"code","message"}`,状态码约定:400 VALIDATION / 401 UNAUTHORIZED(+TOTP_REQUIRED)/ 403 FORBIDDEN / 404 NOT_FOUND / 409 CONFLICT。
- 认证解析顺序:`Authorization: Bearer tdp_...`(PAT)→ Cookie 会话。PAT scopes=read 时非 GET → 403。TOTP 已开启且会话未验证 → 除 `/api/auth/totp/*`、`/api/auth/logout`、`/api/auth/me` 外一律 401 TOTP_REQUIRED(PAT 不受此门禁)。
- 角色:OWNER>ADMIN>EDITOR>VIEWER(3/2/1/0);全局 `is_admin` 在任何项目视为 ADMIN。
- 文档引用格式(写入 markdown):图片 `![名称](/api/files/{id}/download?inline=1)`、附件 `[名称](/api/files/{id}/download)`、
  文档/文件引用 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`(见 §4.4)。
- 反链:`GET /api/docs/{id}/backlinks`(VIEWER 起;同项目未删除文档中正则 LIKE `%teamdoc://doc/%/{id})%`,排除自引用,限 100 条)。
- 云空间:`GET /api/files?project_id=` 文件项带 `referenced`(项目内文档正文是否引用 `/api/files/{id}/`,用于列表"被引用"徽标与删除警告),并返回 `total:{folders,files}`(截断提示依据,§4.8);`GET /api/search?q=` 的 files 结果带 `mime`(编辑器据此把图片插成原生 `![]()`)。
- 云空间批量与回收站(2026-09):`GET /api/files/zip?ids=a,b,c` 打包下载(≤200 个,SpooledTemporaryFile 先压后流式回吐,**必带 Content-Length**,否则 chunked 下载浏览器无进度且 Chrome 安全检查期像"卡住");`POST /api/files/{id}/restore`、`DELETE /api/files/{id}/permanent`、`DELETE /api/docs/{id}/permanent`、`POST /api/files/folders/{id}/restore`、`DELETE /api/files/folders/{id}/permanent`(仅限回收站中的项;文件彻底删除连物理文件清,文档连子树+版本清,文件夹连子树+文件清);回收站列表 `GET /api/projects/{id}/trash` → `{docs,files,folders}`(取代旧 `/docs/trash`)。前端:上传走 XHR(fetch 无上传进度),并发 3 队列 + 右下角进度面板;文件夹上传(webkitdirectory / 拖拽 webkitGetAsEntry 递归,路径→folderId 会话内缓存串行建目录);多选后**表头原地变身**批量操作(Gmail 式,不另起行避免列表抖动);批量下载用锚点 `<a download>`(window.open 对附件流不可靠)。
- 静态资源(`/css/`、`/js/`、`/vendor/`、`/index.html`)统一 `Cache-Control: no-cache`(main.py 中间件),浏览器每次携 ETag 重验证,杜绝改版后跑旧 JS。
- 完整端点表见《构建文档.md》§7(注意 §5 的出入已对 color/scope 修正)。

## 7. 测试与验证惯例

- **已备好自动化脚本,改完代码请直接跑**:见 `lite/tests/README.md`。
  - `smoke_all_endpoints.py` — 遍历全部 API 路由断言期望状态码,**任何 5xx 视为失败**;
    改完服务端先跑这个,它是 NameError/TypeError 类回归的护栏。
  - `test_folder_recycle.py` — 文件夹回收站闭环 + 权限语义。
  - `test_avatar_color.py` — 头像取色跨接口一致性(含 WS presence)。
  - `verify_page_assets.py` — 模拟浏览器加载全部静态资源,校验零外链 + no-cache;
    **内网部署前后必跑**。
- 启动方式:`.venv/Scripts/python.exe main.py`,配 `TEAMDOC_DATA_DIR`(隔离数据)+ `PORT`(非常用端口)。
  首次跑 `tests/_bootstrap.py` 建测试管理员(admin@teamdoc.local / admin12345)。
- 手写新脚本时:标准库 urllib,**显式 UTF-8**;**不要用 curl 发中文**(Windows GBK 会乱码入库);
  URL 里的中文必须 `urllib.parse.quote`;下载类响应不是 JSON,解析前先看 Content-Type。
  WS 用 websockets 库测。
- 前端:改动后全部 JS 过 `node --check`;CSS 类删除前必须 grep 反查零引用(注意 `'cls-'+x` 动态拼接);
  CSS 改完顺手检查 `{` `}` 数量相等。
- 服务进程管理(Windows):`netstat -ano | grep :8123` 找 PID,`taskkill //F //T //PID <pid>` 杀。
  **教训:测试残留进程会占端口导致"双实例 + 脏 Cookie"诡异故障,测完必杀**。

## 8. 已知遗留 / 改进候选

- 引用浮层 Esc 关闭后,已输入的字面量 `@` / `[[` 保留在正文中(需手动删);页面滚动时浮层自动关闭。
- 远端覆盖为全文替换,光标位置仅粗粒度保留(字数变化大时会偏);真字符级协同见 §9。
- marked 行内公式 `$...$` 对价格类文本(如 $5 和 $10)可能误判为公式;KaTeX 加载失败时公式按源码显示。
- 冷加载个人项目瞬间「成员」导航项可能闪现(项目对象未返回前 visible 默认为显示);有缓存后不再出现。
- **窄屏常驻 72px 图标栏后,项目子项只有图标没有文字**(`.side-subitem` 的 `.side-label` 被隐藏),
  项目/文档一多会退化成"一列图标",辨识度受限。这是 2026-09 统一导航机制时接受的取舍;
  若反馈不好,可让窄屏恢复"完整侧栏 + 抽屉"两态(删掉 `applyNavMode` 里窄屏恒折叠那一行即可),
  或给子项加 tooltip/仅显示当前项目的子项。
- 文档树的操作按钮(新建子文档/更多)依赖 `.doc-row:hover` 显示,**触屏无 hover 故不可见**
  (云空间的 `.row-acts` 已有 `@media (hover: none)` 补偿,文档树没有);窄屏下这两个操作实际不可达。
- **每次列云空间目录都会全量扫描该项目所有文档正文**(为算 `referenced` 徽标,`files.py` 中
  用 `LIKE '%/api/files/%'` 取回 content 后正则提取)。30 人规模无感,文档量上千后
  应改为在 doc 保存时维护引用索引表。
- 各视图仍普遍是字符串模板拼接(`'<div>'+...`);文档树与侧栏项目树两套相似渲染逻辑未合并
  (数据结构不同:一个含 children,一个是扁平列表)。
- 面包屑父链无 API,前端会话内维护路径栈,刷新回根目录。
- 项目间移动文件固定落到目标项目根目录(接口支持 folderId,前端未做选择器)。
- 列表查询一律 LIMIT ≤500:云空间已加截断提示,回收站/搜索仍是静默截断(§4.8)。
- CSS 中仍存在非 4pt 网格的硬编码间距(2/6/10/14px 等,约 100 处);
  网格内的值已全部走 `--sp-*` 令牌,剩余项需按设计意图逐个判断,勿机械替换。
- 无现成备份脚本:备份即打包 `server/data/` 整个目录(库 + 物理文件)。

## 9. 路线图参考(用户已表达过兴趣的方向)

- CLI `td`(原规格 §16,设计已定稿:PAT 认证 + JSON 信封 + API 透传 + --dry-run,纯标准库单文件,服务端零改动)
- 日历模块(PROJECT_NAV 加一项即可接入)
- 字符级真协同(pycrdt/Yjs 替换 LWW)
