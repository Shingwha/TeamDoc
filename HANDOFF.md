# TeamDoc Lite — Handoff 文档

> 写给后续接手的 Agent / 开发者。本文档是当前代码库的**实际状态快照**,与《构建文档.md》(原始规格)有出入处均已标注——**代码以此文档为准,规格文档仅作背景参考**。
> 更新日期:2026-09-10(第一轮构建 + 多轮 UI 重构 + 编辑器重构[去 Vditor,源码/预览双模式]后)

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
| 前端 | 纯 HTML/CSS/JS,零框架零打包;CDN(jsdelivr 固定版本):**Remix Icon 4.5** + **marked 11.1.1** + **Prism 1.29.0**(autoloader)+ **KaTeX 0.16.9**(懒加载) |
| 认证 | scrypt 密码(hashlib,stdlib)、Cookie 会话(`td_sid`, HttpOnly+SameSite=Lax)、PAT(`tdp_` 前缀存 sha256)、TOTP(pyotp) |

```bash
cd lite/server
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

- 首次启动只建表不预置账号,浏览器访问走初始化向导(bootstrap 建首个管理员)。
- 数据在 `server/data/`(已 gitignore);`TEAMDOC_DATA_DIR` 环境变量可改数据目录(测试隔离用,非原始规格)。
- 环境变量:`PORT`(8000)、`MAX_UPLOAD_MB`(2048)、`SESSION_TTL_DAYS`(7)。

## 3. 目录结构与文件职责

```
lite/
├── server/
│   ├── main.py      (56 行)  入口:建表、路由注册(auth→docs→files→search→ws→静态托管,顺序不能乱)、422→400 VALIDATION
│   ├── models.py    (167)    9 张表;users/sessions/pats/projects/project_members/docs/doc_versions/folders/files
│   ├── auth.py      (~450)   scrypt、会话、PAT、TOTP、权限依赖(current_user/require_write/require_admin/require_project_role)、
│   │                        用户管理、create_personal_project(建用户时自动建个人项目)
│   ├── docs.py      (~490)   项目/成员/文档树/内容/版本(留 50 条)/回收站(文档+文件统一)/反链 backlinks;个人项目保护规则
│   ├── files.py     (~340)   云空间:流式上传(1MB 块)/下载(inline|attachment, filename*=UTF-8'')/zip 打包下载(带 Content-Length)/
│   │                        文件夹/项目间移动/回收站恢复/彻底删除(清物理文件)
│   ├── search.py    (55)     LIKE 搜索 + 权限过滤 + snippet(命中位置前后各 60 字符)
│   └── ws.py        (131)    /ws/docs/{doc_id} 协同:presence 广播、LWW content→saved/remote、VIEWER readonly
└── web/
    ├── index.html   (86)     SPA 壳:侧栏(品牌行+搜索框+项目树+管理后台+个人设置+用户卡片)+ #view + #mobile-bar(窄屏)
    ├── css/
    │   ├── tokens.css   (180) 设计令牌:Material You 语义色(浅/深 × 6 种子色)、形状/间距/字阶/z-index 五档层级表
    │   ├── base.css     (73)   重置、[hidden]{display:none!important} 护栏、滚动条、mark/secret-box
    │   ├── components.css (384) 自研组件:按钮(6 变体)/表单/卡片/chip/头像/toast/模态框/菜单/空态/spinner/seg 分段切换
    │   ├── app.css      (641)  壳布局、侧栏项目树、登录页、各视图专属样式、三态响应式(展开/折叠图标栏/窄屏抽屉)
    │   └── editor.css   (306)  文档编辑器全部样式:源码 textarea / .markdown-body 预览排版 / Prism 令牌映射 /
    │                           teamdoc:// chip / 引用浮层 .td-panel / 浮动工具栏 .td-floatbar / 反链栏
    └── js/
        ├── api.js       (52)   fetch 封装(契约见 §6);204→null;{detail:{code,message}}→ApiError;401 跳 #/login
        ├── theme.js     (65)   主题管理:light/dark/system + 6 种子色,localStorage 持久化,系统监听,onChange 订阅
        ├── ui.js        (418)  组件库(全部视图复用,禁止另造):toast/modal/confirmDialog/inputDialog/dropdownMenu(支持 direction:'up')/
        │                        avatar(首字圆形,id 哈希取色)/emptyState/spinner/loadingRow/icon/fmtSize/fmtDate/debounce/esc/copyText/roleRank/roleLabel
        ├── markdown.js  (88)   全站唯一 Markdown 渲染路径 MdRender.render/mount:marked + raw HTML 转义(防 XSS)+
        │                        $$/​$ 公式扩展(KaTeX 按需懒加载)+ Prism autoloader 高亮(失败降级不高亮不报错)
        ├── doceditor.js (560+) 编辑器增强 DocEditor.enhance(textarea,{upload}):teamdoc:// chip 全局点击路由(注册一次)、
        │                        @/[[ 引用浮层(空查询给默认候选;图片文件插原生 ![]())、/ 行首插入组件菜单、
        │                        选区浮动工具栏(纯文字格式)、粘贴/拖拽/菜单上传(归入「文档附件」);返回 cleanup
        ├── app.js       (494)  hash 路由、壳装配、**项目树侧栏**(见 §4)、登录/初始化向导、PROJECT_NAV 配置数组、用户卡片菜单(含外观面板)
        └── views/              projects(项目首页)/project(文档模块+成员+回收站+设置)/drive(云空间:统一列表+多选批量+上传队列)
                                search/admin/settings
```

## 4. 关键架构决策(后续改动必须理解这些)

### 4.1 个人空间 = 个人项目(已统一,非原始规格)
- 每个用户创建时(bootstrap/管理员建用户)自动获得 `is_personal=true` 的项目"个人空间",唯一成员=本人 OWNER。
- **保护规则**:个人项目不可删(403「个人空间不可删除」)、不可管理成员(403),可改名/描述。
- 列表:个人项目排最前;管理员 `?all=1` 不含他人个人项目(但直接访问 URL 仍有 ADMIN 权限,保留原语义);搜索同理隔离。
- **files/folders 已无 scope/user_id 字段**,全部归属 project_id;原"归属转移"变为项目间移动(`POST /api/files/{id}/move {projectId, folderId?}`,源项目 ADMIN + 目标项目 EDITOR)。
- 前端无 `#/drive` 独立视图,旧路由重定向到个人项目的 files tab。

### 4.2 侧栏项目树(纯树,无模式切换)
- 侧栏 = 顶部品牌行(TeamDoc 文字 + 折叠钮)+ 搜索框 + **项目树** + 管理后台/个人设置 + 底部用户卡片。
- 项目行 = chevron + 纯文字(字重 600,**无图标无色点**),点击**只做展开/收起**,不导航;进入项目必须点子项。
- 子项由 `PROJECT_NAV` 配置数组驱动(app.js 顶部):`{key, icon, label, view, visible}`,`key` 即路由段 `#/p/{id}/{key}`。**加新模块(如日历)= 数组加一项 + 写一个视图函数**,路由/侧栏/高亮/tab 记忆全自动生效。
- 展开状态(2026-09 重构):内存 Map 是**唯一真相**,仅三处写入——用户点击行、一次性种子(刷新/深链首次渲染时展开当前项目)、新建项目后 `App.expandProject(pid)`;**路由变化只影响高亮,绝不动展开态**(离开项目去管理后台/个人设置不会误收起)。刷新回到种子状态。
- 三态:宽屏展开(240px)/ 宽屏手动折叠(72px 图标栏,项目显示彩色首字头像,localStorage `td:side-collapsed` 持久化)/ 窄屏 ≤960px(侧栏隐藏 + `#mobile-bar` 48px 顶条[汉堡/TeamDoc/搜索],抽屉式浮出侧栏 + 遮罩)。

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
- 客户端:防抖 800ms 发 content(WS 断开降级 PUT /content);收 remote 时无焦点无脏改→覆盖 textarea(保留光标,程序赋值不触发 input 无需抑制回环),否则顶部提示条「{by} 更新了文档 [加载最新]」。

## 5. 与原始规格(构建文档.md)的出入汇总

| 项 | 规格 | 现状 |
|---|---|---|
| 个人空间 | 独立 scope=personal 云空间,无文档 | 统一为个人项目(含文档),scope 概念已删(§4.1) |
| 项目 color | projects.color 字段 + 前端取色器 | **已整体删除**(前端 UI + 后端字段);旧库物理残留 color 列,无害不读写 |
| 项目页布局 | 页头 + 顶部 Tabs + ⚙ 模态框组 | 侧栏项目树 + 子项独立路由视图(members/trash/settings) |
| 顶栏 | 48px 顶栏(搜索/头像区/菜单) | **已删除**;搜索/用户菜单在侧栏;协作者头像只在编辑器内联 |
| 云空间列表 | 文件夹表 + 文件表两个表格 | **统一单列表**(文件夹置顶,行 hover 操作,列头可排序);多选批量打包下载(zip)/删除,上传带进度面板与并发队列,支持整文件夹上传 |
| 回收站 | 只收文档,仅恢复 | 文档+文件统一收,支持恢复与彻底删除(§6) |
| PAT 端点 | 规格只定义语义 | `GET/POST /api/auth/pats`、`DELETE /api/auth/pats/{id}`(创建/吊销仅 Web 会话) |
| avatarColor | users 表无此字段但登录响应要求 | 不落库,由 user id 哈希在调色板确定性取色 |
| append 快照 | 未说明 | 与 PUT 一致先存"覆盖前"快照 |
| 删项目 | 只删 docs/members | 一并删该项目 files/folders 记录(物理文件保留,规格 §15 明确不做物理清理) |

其余契约(§5 数据模型剩余字段、§7 API 路径、§8 协同协议)**严格遵守规格**,前端依赖这些字段名,改动前先查构建文档。

## 6. API 契约要点(前端/调用方视角)

- 前缀 `/api`;成功直接返回 JSON(无信封);删除类 `{"ok":true}` 或 204;错误 `HTTPException detail={"code","message"}`,状态码约定:400 VALIDATION / 401 UNAUTHORIZED(+TOTP_REQUIRED)/ 403 FORBIDDEN / 404 NOT_FOUND / 409 CONFLICT。
- 认证解析顺序:`Authorization: Bearer tdp_...`(PAT)→ Cookie 会话。PAT scopes=read 时非 GET → 403。TOTP 已开启且会话未验证 → 除 `/api/auth/totp/*`、`/api/auth/logout`、`/api/auth/me` 外一律 401 TOTP_REQUIRED(PAT 不受此门禁)。
- 角色:OWNER>ADMIN>EDITOR>VIEWER(3/2/1/0);全局 `is_admin` 在任何项目视为 ADMIN。
- 文档引用格式(写入 markdown):图片 `![名称](/api/files/{id}/download?inline=1)`、附件 `[名称](/api/files/{id}/download)`、
  文档/文件引用 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`(见 §4.4)。
- 反链:`GET /api/docs/{id}/backlinks`(VIEWER 起;同项目未删除文档中正则 LIKE `%teamdoc://doc/%/{id})%`,排除自引用,限 100 条)。
- 云空间:`GET /api/files?project_id=` 文件项带 `referenced`(项目内文档正文是否引用 `/api/files/{id}/`,用于列表"被引用"徽标与删除警告);`GET /api/search?q=` 的 files 结果带 `mime`(编辑器据此把图片插成原生 `![]()`)。
- 云空间批量与回收站(2026-09):`GET /api/files/zip?ids=a,b,c` 打包下载(≤200 个,SpooledTemporaryFile 先压后流式回吐,**必带 Content-Length**,否则 chunked 下载浏览器无进度且 Chrome 安全检查期像"卡住");`POST /api/files/{id}/restore`、`DELETE /api/files/{id}/permanent`、`DELETE /api/docs/{id}/permanent`(仅限回收站中的项;文件彻底删除连物理文件清,文档连子树+版本清);回收站列表 `GET /api/projects/{id}/trash` → `{docs,files}`(取代旧 `/docs/trash`)。前端:上传走 XHR(fetch 无上传进度),并发 3 队列 + 右下角进度面板;文件夹上传(webkitdirectory / 拖拽 webkitGetAsEntry 递归,路径→folderId 会话内缓存串行建目录);多选后**表头原地变身**批量操作(Gmail 式,不另起行避免列表抖动);批量下载用锚点 `<a download>`(window.open 对附件流不可靠)。
- 静态资源(`/css/`、`/js/`、`/index.html`)统一 `Cache-Control: no-cache`(main.py 中间件),浏览器每次携 ETag 重验证,杜绝改版后跑旧 JS。
- 完整端点表见《构建文档.md》§7(注意 §5 的出入已对 color/scope 修正)。

## 7. 测试与验证惯例

- 后端自测:Python 脚本(标准库 urllib,**显式 UTF-8**;**不要用 curl 发中文**,Windows GBK 会乱码入库)+ websockets 库测 WS。用 `TEAMDOC_DATA_DIR` + 非常用端口隔离,测完清理。
- 前端:改动后全部 JS 过 `node --check`;CSS 类删除前必须 grep 反查零引用(注意 `'cls-'+x` 动态拼接)。
- 服务进程管理(Windows):`netstat -ano | grep :8000` 找 PID,`taskkill //F //T //PID <pid>` 杀。**教训:测试残留进程会占 8000 端口导致"双实例 + 脏 Cookie"诡异故障,测完必杀**。

## 8. 已知遗留 / 改进候选

- 引用浮层 Esc 关闭后,已输入的字面量 `@` / `[[` 保留在正文中(需手动删);页面滚动时浮层自动关闭。
- 远端覆盖为全文替换,光标位置仅粗粒度保留(字数变化大时会偏);真字符级协同见 §9。
- marked 行内公式 `$...$` 对价格类文本(如 $5 和 $10)可能误判为公式;KaTeX 加载失败时公式按源码显示。
- 冷加载个人项目瞬间「成员」导航项可能闪现(项目对象未返回前 visible 默认为显示);有缓存后不再出现。
- 抽屉收起无反向动画(display 切换无法过渡);需常驻 fixed + transform 方案才可做,改动较大未做。
- 各视图字符串模板拼接模式(`'<div>'+...`)普遍存在;文档树与侧栏项目树两套相似树渲染逻辑未合并(数据结构不同)。
- 面包屑父链无 API,前端会话内维护路径栈,刷新回根目录。
- 项目间移动文件固定落到目标项目根目录(接口支持 folderId,前端未做选择器)。
- 列表查询一律 LIMIT ≤500(规格要求,30 人规模足够)。

## 9. 路线图参考(用户已表达过兴趣的方向)

- CLI `td`(原规格 §16,设计已定稿:PAT 认证 + JSON 信封 + API 透传 + --dry-run,纯标准库单文件,服务端零改动)
- 日历模块(PROJECT_NAV 加一项即可接入)
- 字符级真协同(pycrdt/Yjs 替换 LWW)
