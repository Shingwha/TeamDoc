# TeamDoc Lite — Handoff 文档

> 写给后续接手的 Agent / 开发者。**本文档是代码库当前状态的权威说明,与代码冲突时以此为准,并顺手修正本文档。**
> 更新日期:2026-09-12

## 1. 产品现状

TeamDoc Lite:30 人小团队自部署知识库。项目管理文档、实时协同编辑、云空间、全文搜索。界面全简体中文。

**功能已完整**:认证(bootstrap / PAT / 用户管理)、项目与成员(批量圈选添加、成员自助退出)、文档树、Markdown 源码/预览双模式编辑、WebSocket 协同、分层版本历史、云空间(流式上传/分页排序/网格视图/站内预览/文件夹递归删除恢复移动/跨项目移动)、回收站(文档+文件+文件夹,只列子树根)、全文搜索、最近动态(仅已参加项目)、同事目录选人、发现广场、公开项目与单文件公开、文档/文件引用浮层(统一走 preview.js)、管理后台(存储总览/孤儿清理/备份恢复/项目总览与接管)。

**明确不做**:日历、字符级协同、S3、通知、匿名分享链接。(CLI `td` 已独立落地为全局 ZCode skill,不在本仓库,服务端零改动。)

## 2. 技术栈与运行

- 后端:Python 3.13 / FastAPI + uvicorn(**必须单 worker**)+ SQLite(SQLAlchemy 2.0, WAL, busy_timeout=5000)。SQLite 单写者,多 worker 会出现推送丢失、写锁冲突。
- 前端:纯 HTML/CSS/JS,零框架零打包;依赖全部本地 vendor,**零外网请求**,内网/离线直接可用。
- 认证:scrypt 密码(stdlib)、Cookie 会话(`td_sid`, HttpOnly+SameSite=Lax)、PAT(`tdp_` 前缀存 sha256)。不做两步验证。

```bash
cd lite/server
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000   # 必须单 worker
```

- 首次启动只建表不预置账号,浏览器走 bootstrap 向导建首个管理员。数据在 `server/data/`(gitignored),`TEAMDOC_DATA_DIR` 可改(测试隔离)。
- **ID 为 Integer 自增,起点 10000(五位数)、永不复用**(users/pats/projects/members/docs/versions/folders/files;表定义带 sqlite_autoincrement,删掉末尾的行 id 也不会被复用,正文里指向已删资源的死链不会"复活";起点种子由 schema.seed_id_start 幂等写入,旧十六进制库用 lite/tools/convert_ids_to_int.py 重编号)。JSON 里 id 是 number,前端路由边界(App.js)与 dataset 读数处转数字。路径参数非法 → 400 VALIDATION,不存在 → 404。**秘密与标识分离**:会话键是随机 token、PAT 只存 hash,不随标识数字化变可猜。改数据结构仍按 §2.1 删库重建。
- 环境变量:`PORT`(8000)、`MAX_UPLOAD_MB`(20480)、`SESSION_TTL_DAYS`(7)、`REMEMBER_TTL_DAYS`(30)、`VERSION_MERGE_MINUTES`(5)、`STORAGE_RESERVE_MB`(1024)、`BACKUP_DIRS` / `BACKUP_INTERVAL_HOURS`(24)/ `BACKUP_KEEP`(7)、`ZIP_MAX_BYTES_MB`(4096)。

### 2.1 数据库结构:单一来源 models.py(加/删字段必读)

- **结构唯一来源是 `models.py`**。启动时 `schema.init()` 做两件事:create_all 建缺失表;`check_drift` 双向漂移自检(库有模型无 → 可空列提醒、NOT NULL 无默认列阻断插入;模型有库无 → 启动失败)。有漂移就**启动失败**并给可执行的修复指引。
- **开发期没有迁移机制**(曾有,已整体删除——"迁移历史 + 模型"两套来源必然漂移且静默)。遇到漂移:停服 → 删数据目录 → 重启,库按 models.py 重新长出来。
- **加字段**:只在 models.py 加,旧库缺列按自检提示重建。**删字段**:models.py 删的同时库里的列也必须消失(重建,或 SQLite 3.35+ `ALTER TABLE ... DROP COLUMN`)。NOT NULL 残留列会阻断 INSERT——教训:项目色下线只删了模型字段,`projects.color` 留在库里,用户"新建项目"时才炸。
- WAL 模式下**直接复制 `teamdoc.db` 会拿到空库**(未 checkpoint 的写入都在 `-wal` 里),备份必须 `VACUUM INTO`(§4.10)。
- **旧十六进制快照的转换工具已下架**(完成使命,主分支不再携带):如需把 data-pre-convert-* 旧快照再转成整数 id,从 git 历史取回 `lite/tools/convert_ids_to_int.py`(提交 4da86d3 引入),用法见该文件 docstring。

### 2.2 vendor 目录(内网部署关键,勿删)

`web/vendor/`:remixicon(保持 CSS 内字体相对路径)、marked、prism(+ **autoloader 必须来自 plugins/autoloader**,否则只剩 html/css/js 高亮;含 78 个语言包)、katex(+ 40 字体,懒加载)。

- 升级依赖 = 替换 vendor 文件,不改代码。`main.py` 的 no-cache 中间件覆盖 `/vendor/`(文件名无内容哈希,长缓存会让升级不生效)。
- **改完前端资源必跑 `verify_page_assets.py`**:逐一请求资源,断言零外链 + 200 + no-cache。

## 3. 代码地图

```
lite/server/
  main.py     入口:schema.init 前先 apply_pending_restore;路由注册顺序 auth→docs→files→search→admin→ws→静态(不能乱);
              422→400 VALIDATION、no-cache + 安全响应头中间件
  schema.py   建表 + 双向漂移自检(§2.1)          models.py  9 张表 + unlink_quiet + file_abspath
  auth.py     scrypt/会话/PAT/鉴权工具集(§4.7)/同事目录  docs.py  项目/文档树/版本/回收站/反链(save_doc_content REST 与 WS 共用)
  files.py    云空间全量(上传/下载/分页/zip/文件夹树/移动/回收站)  admin.py  管理后台 HTTP 入口(实现见 backup.py)
  backup.py   备份恢复实现 + 全项目唯一后台定时线程        search.py  LIKE 搜索 + /api/recent    ws.py  协同
lite/tests/   纯 stdlib 测试脚本(§6)      lite/DEPLOY.md  部署运维(Windows 服务化/反代/备份恢复)
lite/web/
  index.html  SPA 壳 + 全部 script 标签(顺序即依赖图);内联主题恢复脚本防闪烁
  css/  tokens.css 唯一尺寸颜色来源 → base.css 重置/护栏 → components.css 组件样式 → app.css 壳布局与视图残留 → editor.css 编辑器
  js/   api.js fetch 封装({detail:{code,message}}→ApiError, 401→#/login) · theme.js 主题 ·
        ui.js 组件库(唯一入口,禁止另造) · markdown.js 全站唯一渲染路径(marked+转义+KaTeX+Prism) ·
        preview.js 预览浮层组件(云空间预览/@引用浮层/历史版本共用) · doceditor.js 编辑器增强 ·
        app.js 路由+壳+侧栏 · views/ projects/project/drive/search/discover/admin/settings
```

**加视图**:app.js 的 `PROJECT_NAV` 加一项 + 写 `window.Views.<name>`,路由/侧栏子项/高亮/tab 记忆全自动生效。

## 4. 架构决策与硬约束

### 4.1 前端三层(新增 UI 必读,重构前是 11 套列表行/6 套页头,勿退回)

| 层 | 位置 | 规矩 |
|---|---|---|
| 令牌 | `css/tokens.css` | 唯一尺寸与颜色来源(--ctl-* / --sp-* / --row-* / --fs-* / --icon-* / --state-* / --w-* / 浮层 / z-index 六档)。新样式出现裸像素 = 待补令牌的信号 |
| 组件样式 | `css/components.css` | 视图只引用 class,不自定尺寸 |
| 组件工厂 | `js/ui.js` | 唯一组件入口;标记类返回 HTML 字符串,需事件接线的返回 DOM |

- 两条列表族(先判型再选,不要新造第三种):需要列对齐 → `.data-table`(`UI.tableHead/tableRow`,表头与数据行引用同一组 `--tpl` 值,末列操作固定宽);图标/头像+文字 → `.list-row`(`UI.listRow`)。
- 按钮两档:`--ctl-xl`(40px 页面级)/ `--ctl-m`(32px 行内)。胶囊形与 `.chip` 统一;hover 洗色用 `--state-*` 的 color-mix。
- 颜色全静态零运行时算色(§4.4);文件类型色用主题无关的 `--file-*`,不复用主题角色。
- 权限判定用 `UI.canEdit / canAdmin / canOwn`(ui.js,**唯一入口,勿手写 `roleRank(myRole) >= N`**)。它们直接比级 `myRole`——那是服务端 project_role 的综合结果(成员→成员角色;非成员全局管理员→ADMIN;公开访客→VIEWER;其余 null),无需再看 isMember:公开访客的 VIEWER 天然过不了 EDITOR 及以上判定,而非成员管理员必须拿到管理入口(接管失联项目)。

### 4.2 个人空间 = 个人项目

- 每用户创建时自动获得 `is_personal=true` 项目,唯一成员 = 本人 OWNER。不可删、不可管理成员、**永不可公开**(patch_project 硬拒,连设 private 也拒);列表与搜索排除他人个人项目。
- `project_role` 里 `is_admin → ADMIN` 必须在 `is_personal` 判断**之后**——否则管理员可直读他人私有草稿。
- files/folders 无 scope 字段,全归 project_id;"归属转移"即跨项目移动(`POST /api/files/{id}/move`)。无 `#/drive` 独立视图,旧路由重定向到个人项目 files tab。

### 4.3 侧栏状态机

- 侧栏 = 品牌行 + 搜索框(折叠态顶替为搜索钮)+ 项目树 + 入口 + 底部用户卡片。项目行点击**只展开/收起不导航**,进项目必须点子项。
- 展开态:内存 Map 唯一真相,仅三处写入(用户点击、首渲染种子、新建项目后);**路由变化只改高亮,绝不动展开态**。刷新回到种子状态。
- **id 一律 `Number(...)` 后再比较或作键**:`p.id` 是 JSON 数字,而 `segs`(URL 切分)与 `dataset.*` 都是字符串——不归一化就是"写入的键读不到、相等判断恒 false",表现为"点了没反应/选中态不生效"(资源 id 整数化后踩过四次:侧栏展开、文档树折叠、历史版本选中、移动对话框的排除自己/默认目录)。凡以 id 为键的 Set/Map 与 id 相等比较,一律先归一。
- 两态(72px 图标栏 / 240px 完整)由 `#shell.side-collapsed:not(.nav-open)` 一组 CSS 承载——`:not(.nav-open)` 是关键,抽屉打开时整组规则自然失效,无需反向覆盖。窄屏 ≤960px 恒折叠且**不写 localStorage**(不污染宽屏偏好)。
- 细节:搜索聚焦需 `requestAnimationFrame` 延后一帧;折叠钮 title 随屏宽同步(`syncCollapseBtn`)。

### 4.4 主题

`html[data-theme]`(light/dark)× `html[data-color]`(blue/purple/green/orange/pink/teal)。色值全部静态写在 tokens.css,零运行时算色;首屏由 index.html 内联脚本在样式加载前恢复。编辑器/预览色值也全走令牌(Prism 用 color-mix 派生),**主题切换零 JS 处理**。入口:用户卡片菜单"外观"。

### 4.5 版本与协同(LWW)

- 版本按年龄分层(取代"只留 50 条"——条数的时间跨度不可预测):**1h 内全留 / 1d 内每小时 1 条 / 30d 内每天 1 条 / 1y 内每周 1 条 / 更早每月 1 条**,稳态约 124 条。另有 `VERSION_MERGE_MINUTES`(5)合并窗口:同人窗口内连续保存 = 一个还原点(自动保存 800ms 防抖,没窗口一小时能产几十个版本)。实现在 `docs.py _prune_versions`。
- `docs.version` 是**保存次数不是版本数**,顶栏显示「已保存 ✓ HH:MM」。
- WS `/ws/docs/{doc_id}` 关闭码 4401 未登录 / 4403 非成员 / 4404 不存在;VIEWER 连接 readonly。content 消息:相同→仅回 saved;不同→旧内容存版本 → version+1 → 回发送者 saved + 广播 remote{content,version,by}。**快照与保留由 `save_doc_content()` 统一实现,REST 与 WS 共用,勿另写一份**。
- 客户端:800ms 防抖发 content(WS 断开降级 PUT);收 remote 时无焦点且无脏改才覆盖,否则顶部提示条「{by} 更新了文档 [加载最新]」。
- **每条 content 消息都要重查**会话/is_disabled/角色——WS 是长连接,只握手时校验不够,禁用/移出的人能继续写。

### 4.6 编辑器(源码/预览双模式,已弃用 Vditor)

- 顶栏 seg 切「编辑|预览」:编辑态 = 无边框等宽 textarea(源码即真相);预览态 = MdRender。VIEWER 恒预览,模式记忆 `td:doc-mode`。布局:单一滚动容器 `.editor-scroll`;`#view.view-fill` 满出血,**新元素进编辑器列必须自带水平内边距**;窄屏 ≤720px 树上正文下。
- 引用:`@`/`[[` 弹浮层,插入 `[@标题](teamdoc://doc/{projectId}/{docId})` / `[@名称](teamdoc://file/{fileId})`;**图片文件例外**,插原生 `![名称](/api/files/{id}/download?inline=1)`。点击路由(doceditor.js)统一走 preview.js 浮层:@文档 → 正文摘要+位置/字数 meta+「打开全文」;@文件 → 预览+归属位置+「在云空间中查看」(?highlight= 定位)+下载。无序列化/反序列化层。
- `/` 菜单:行首插入块(标题/列表/引用/代码/分割线/传图);**浮动工具栏只做文字格式,不放上传**。上传(粘贴/拖拽/菜单)自动归入项目根「文档附件」文件夹(promise 缓存)。
- **大纲功能已整体移除**(scrollspy 双形态,交互不佳),重做请从零设计,勿恢复旧代码。

### 4.7 服务端鉴权(改权限逻辑必读)

只用这些入口,不要再手写 `ROLE_RANK.get(...) < ...`:

| 工具 | 用途 |
|---|---|
| `current_user` / `require_write` / `require_admin` | 登录、PAT write scope、全局管理员 |
| `require_project_role("EDITOR")` / `require_doc_role("VIEWER")` | 依赖注入式按路径参数取资源并校验(不存在→404) |
| `ensure_project_role(db, ctx, pid, required)` | 已拿到资源对象时用(不足→403) |
| `get_project_or_404` / `project_role` / `is_project_member` | 取项目 / 只查角色不抛错 / **真实成员关系**(不含管理员与公开访客;区分"能写"与"看得到"必须用它) |
| `is_project_owner_or_admin` | **OWNER 级管辖权**:真所有者或全局管理员。授 OWNER、删项目等"接管"语义一律走它,勿内联 is_admin 特判;个人空间保护在端点里先于本判定。前端对应 `UI.canOwn` |
| `require_write_ctx(ctx, db)` | 补 PAT write scope(定义在 auth,**勿从 docs 导入**) |

- **判定顺序:不存在→404,权限→403,状态→409。授权必须先于状态判定**,否则非成员可凭 409/403 差异探测他人资源。
- 公开语义单点:`project_role()` 里"公开且非成员→VIEWER",文档树/读写/云空间/回收站/搜索/WS 全经它,一处即全站生效。

### 4.8 文件夹语义与分页

- 删除/恢复/彻底删除**三者对称,均作用于整棵子树**(单事务);恢复时父级仍在回收站则回落项目根。回收站只列**子树根**(判据:父级未删除或不存在;已删文件夹 id 集合用全量查询,防 500 截断漏判父级)。
- 移动:项目内整理 EDITOR 即可;跨项目源 ADMIN + 目标 EDITOR;拒移入自己的后代(409);跨项目**整棵子树一起改 project_id**。
- 文件夹树 `GET /api/projects/{id}/folders/tree` 是一切层级需求(移动选择器/面包屑重建)的唯一入口,勿再写扁平遍历。
- 云空间列表:**分页 + 服务端排序**(分页与排序一体,客户端只排当前页是错的);排序字段白名单映射,非法回落;排序键含 id 兜底,翻页不重不漏;hasMore 服务端算。文件夹不分页(上限 2000),文件默认 100/页 + 加载更多。回收站与搜索仍静默截断 500。

### 4.9 安全红线(上线前审查修出的,勿回退)

1. **mime 服务端按文件名判定,不采信客户端**(SVG 带 `<script>` 同源执行,钓的是点开预览的管理员)。inline 是显式白名单:图片/PDF/文本类;**svg/html/xhtml 与未知类型强制 attachment**,media_type 回落 octet-stream 防 sniffing。
2. 服务端回吐 `canInline`/`isText`,**前端不得自写 mime 猜测**;加类型只改服务端白名单,前端自动跟上。
3. 全站安全响应头:nosniff / XFO SAMEORIGIN / Referrer-Policy。未上 CSP(内联脚本多,收益不抵返工)。
4. **管理端写接口必须 `require_admin_write`**——`require_admin` 只看 is_admin 不查 scope,read-only PAT 曾能建新管理员直接提权。GET 类仍用 require_admin。
5. **OWNER 授予/删项目要求 OWNER 级管辖权**(`is_project_owner_or_admin`)——项目 ADMIN 不得自我提权(升 OWNER 后就能删项目);**全局管理员显式豁免**(信任根,否则唯一所有者失联/被禁用的项目会死锁:所有权移不动、项目删不掉),个人空间对管理员仍一律关闭。
6. **公开项目访客看不到成员邮箱与回收站**(只给 id/name/avatarColor;回收站非成员非管理员 403,前端隐藏 tab)。
7. **Markdown 链接协议白名单**(marked 只 encodeURI 不过滤 `javascript:`)。
8. **`files.storage_path` 只存 basename**(绝对路径与机器绑定,换机恢复全 404 且孤儿扫描发现不了);解析统一 `models.file_abspath()`,历史库由 `schema.normalize_storage_paths()` 启动时幂等回填。
9. **备份生成持 `BACKUP_LOCK`**,按**快照里的 files 清单**打包(不扫盘——半截上传/刚删的文件会掺进来,8 并发曾全败)。
10. 输入边界:offset 双向限幅(SQLite INTEGER 溢出 500)、上传 commit 失败必 unlink 已落盘文件、zip 总字节上限、_prune_versions 分批删除。
11. 旧内核防御前置(MediaQueryList.addListener 回退等,否则白屏);api.js 超时 + 网络错误中文化;模态框随路由关闭。

### 4.10 存储与备份恢复

- **生命周期**:删除 = 软删(物理文件保留);彻底删除/删项目才 unlink;失败**不回滚**,残留归孤儿清理兜底(统一 `models.unlink_quiet()`)。上传写盘全程 try/except(异常必 unlink 半成品);写盘前后查磁盘余量(STORAGE_RESERVE_MB,磁盘满 → 明确 400 而非 500+半截文件)。
- **孤儿清理**(`POST /api/admin/storage/cleanup`,仅手动、无自动巡检):dryRun=1 先预览;孤儿 > 磁盘文件总数 2/3 时**熔断拒绝**,需 force=1——防"库空/指向错目录/恢复旧备份"时把磁盘一键删光。**在传文件必跳过**(`models.INFLIGHT_STORAGE`,回 `inflightSkipped` 计数):上传先落盘后提交记录,这窗口里它在库中不存在,只按 DB 比对就会被当垃圾删掉,而上传随后提交成功 → 记录指向已删文件、永久 404(熔断拦不住单个文件)。`missing`(DB 有磁盘无)只报告不删,正解是恢复备份。
- 占用统计:存储总览给**实例总量**(回收站与活跃分列,回收站仍占磁盘);分项目占用在管理后台「项目」区(§5);占比分母只算 TeamDoc 自身,不整盘。
- **备份库必须 `VACUUM INTO` 生成**(直接复制 .db 实测一张表都没有)。`BACKUP_DIRS` 一次写多处;BACKUP_KEEP 只认自己的 `teamdoc-backup-*.zip`,不碰手工文件;每份写完**回读校验**;**目标目录不存在记失败,绝不自动 mkdir**(移动盘没插时会写到假路径)。定时线程 60s tick,全 try/except。
- **恢复 = 上传校验 → 重启生效**(不在请求里换运行中的库)。校验含 zip slip 白名单(只允许 teamdoc.db / RESTORE.txt / files/<平铺名>)、总量不超磁盘可用、**版本一致性**(无迁移机制,旧版库让服务起不来,必须上传时就拒)。重启时把当前库与 files/ 挪到 `data.pre-restore-<ts>/` 留退路再解包。

### 4.11 云空间前端

- 列表(`.data-table` 列头排序)/ 网格(图片墙)两视图,localStorage 记忆;网格默认"最新在前"(仅当用户从未主动点过列头)。缩略图 = `?inline=1` 原图 + CSS 缩放 + lazy,**不引 Pillow**。
- 文本/Markdown 走站内模态预览(`Preview.open/fill`,与 @引用浮层、历史版本共用同一组件;.md 走 MdRender,其余文本 pre+Prism);图片/PDF 开新标签。预览有「存为文档」入口。
- 行/瓦片共用选择器 `ITEM_SEL = '.data-table-row, .tile'`,**加新视图形态必须同步它**,否则多选静默失效。
- 面包屑与 URL 双向同步(`?folder=`,replaceState,改 hash 会整页重路由);刷新/深链经文件夹树重建路径栈。文件图标/配色统一 `UI.fileIcon`。

### 4.12 公开与发现

- **公开 = 本实例所有登录用户只读浏览,不产生成员关系**。不做匿名 token 分享(内网外访问不到本服务)。关闭公开立即生效(无缓存层)。
- 个人空间永不可公开;单文件公开(`files.is_public`)鉴权抽在 `files.ensure_file_access`:项目角色 → 文件公开 → 403。
- **搜索的 visible 集合含公开项目;`/api/recent` 只含已参加项目**——搜索是"全站能见",最近动态是"我的工作台",两者可见性不同是有意的,**勿"对齐"回去**。
- 广场按 `lastUpdatedAt` 倒序(项目内最近一次文档更新或文件上传);前端发现页把广场与 /api/recent 合成一页。

## 5. API 契约要点

- 前缀 `/api`;成功直接 JSON 无信封;删除类 `{"ok":true}` 或 204;错误 `detail={"code","message"}`,状态码 400 VALIDATION / 401 / 403 / 404 / 409 CONFLICT。
- 认证:`Authorization: Bearer tdp_...`(PAT)→ Cookie 会话。PAT read scope 非 GET → 403;**PAT 创建/吊销仅接受 Web 会话**。角色 OWNER>ADMIN>EDITOR>VIEWER;is_admin 全局视为 ADMIN(个人空间除外)。
- 项目 JSON 带 `isPublic` / `isMember` / `lastUpdatedAt`;`myRole` 即有效权限(§4.1),`isMember` 仅用于"真成员关系"语义(如回收站 tab 可见性、成员列表脱敏口径)。`PATCH /api/projects/{id}` 传 isPublic 切换公开。
- 引用格式(markdown 内):图片 `![名称](/api/files/{id}/download?inline=1)`、附件 `[名称](/api/files/{id}/download)`、文档/文件 `[@标题](teamdoc://doc/{pid}/{did})` / `[@名称](teamdoc://file/{fid})`。`GET /api/docs/{id}` 与 `GET /api/files/{id}/meta` 均带 **`location` 契约** `{projectId, projectName, path[]}`(meta 另含 folderId),浮层归属展示一份代码消费。反链:`GET /api/docs/{id}/backlinks`。
- 云空间:`GET /api/files?project_id=&folder_id=&offset=&limit=&sort=&dir=`,项带 `referenced`(被文档引用,徽标+删除警告)/ `canInline` / `isText`,响应带 `total:{folders,files}` 与 `hasMore`;`GET /api/projects/{id}/storage` 占用统计。搜索文件结果带 `mime`/`canInline`/`projectName`/`folderId`。`GET /api/recent` 带 projectName/folderId。
- **上传是 raw body**(非 multipart):`POST /api/files/upload?projectId=&folderId=&name=`,请求体即内容;前端 XHR 拿进度,api.js 支持 Blob body。重名:上传自动加后缀 `foo(2).png`;用户显式操作(建目录/重命名)**409**;改名同步重算 mime。
- zip 打包 `GET /api/files/zip?ids=&folderIds=`:保留目录结构,文件数上限 1000(超限拒绝不截断),**必带 Content-Length**(SpooledTemporaryFile 先压后流式回吐)。恢复/彻底删除:文件、文档、文件夹各有 `/restore` 与 `/permanent`;回收站 `GET /api/projects/{id}/trash` → `{docs,files,folders}` 只列子树根。
- 管理端(仅 is_admin):`GET /api/admin/storage`、`POST /api/admin/storage/cleanup?dryRun=&force=`、`GET /api/admin/backup(/status)`、`POST /api/admin/backup/run`、`GET /api/admin/restore/status`、`POST /api/admin/restore/upload|arm`、`DELETE /api/admin/restore`。写类一律 require_admin_write(§4.9)。
- `GET /api/users/directory`:任意登录用户可调,只回 `id/name/email/avatarColor`,禁用账号不出现。前端 `UI.personPicker` 做选人(单选即选即关;`{multi, roleSelect}` 批量圈选 + 行内角色下拉,确认统一以各自角色加入),搜索按 姓名/id/邮箱 匹配,排除只认 `excludeIds`(身份键唯一=id)——**无手输邮箱框**,加成员接口收 `userId`(`int_field`,数字串兼容)。个人设置页与用户管理表展示用户 id。
- **自助退出** `POST /api/projects/{id}/leave`:判定链镜像 remove_member——个人空间 403 → 本人非成员(公开访客)404 → 末代 OWNER 409(先在成员页转让所有权);入口在项目设置页 danger 卡,退出后前端跳回项目首页。
- **管理后台项目总览** `GET /api/admin/projects`(仅 is_admin):全部**协作项目,不含个人空间**——它们按设计不可管理且对管理员保密(§4.2),占用亦无管理员视图(存储总览只有实例总量);契约保留 `isPersonal`(恒 false,为将来审计开关预留)。项带 `owners`(含 isDisabled,唯一所有者已禁用 = 死锁信号)/ `memberCount` / `docCount` / `storageBytes`(仅活跃文件)/ `lastUpdatedAt`,全部批量聚合。前端「项目」区只做发现 + 跳转(管理成员跳成员页,OWNER 授予在成员页完成)+ 删除;禁用用户时前端点名其唯一拥有的项目。删项目与授 OWNER 对全局管理员豁免(§4.9)。存储区**没有**分项目占用列表——分项目占用就在「项目」区,勿再加回。
- 用户管理(PATCH/DELETE,仅 is_admin):PATCH 可改 email(查重 409)/name/isAdmin/isDisabled/password;DELETE **只允许删从未产生数据的账号**(String 列非外键,删了留悬空引用;命中即 409 提示改用禁用),`GET /api/users` 带 `canDelete`。删除连带清理 sessions/PAT/成员关系与个人空间。
- **改密的轮换语义**(两处不同,勿"对齐"):`POST /api/users/me/password` 只吊销该用户**其它**会话(当前这条保留,否则用户被自己踢下线),PAT 保留(用户自愿改密,CLI 不该被打断),响应带 `revokedSessions`;管理员 PATCH 带 password = 重置密码,**会话与 PAT 全部吊销**——重置的本意就是把持有旧凭据的一方踢出去,不吊销等于没重置(前端弹窗已写明该后果)。
- 邮箱宽松校验(含 @ 且 ≤255),前端登录/初始化页刻意 `type="text"`——浏览器原生校验比服务端严,两边不一致会造出"建得进、登不进"的账号。
- **凭据字段必须带标准 `autocomplete` 令牌**(密码管理器判定"哪个框是账号"只认它):账号=`username`(本站账号就是邮箱)、姓名=`name`、当前密码=`current-password`、新密码=`new-password`。少一个令牌,管理器就退回"猜"(密码框上方最近的文本框当账号)——初始化表单曾因此把「姓名+密码」存成一组凭据。**密码框绝不写 `off`**(浏览器忽略它,反而会回填操作者自己的密码);管理他人资料的表单用 `off` 并别给 `username`。不引 `data-1p-ignore` 之类厂商私有属性(换管理器即失效)。
- 静态资源统一 no-cache + ETag 重验证(杜绝改版跑旧 JS);`avatarColor` 由 id 哈希确定性取色不落库,**所有涉及用户的接口都返回它**。

## 6. 测试惯例(改完直接跑,见 lite/tests/README.md)

| 脚本 | 覆盖 |
|---|---|
| `smoke_all_endpoints.py` | 全路由状态码,**任何 5xx 即失败**;NameError/TypeError 类回归的护栏;可重复运行 |
| `test_folder_recycle.py` | 文件夹回收站闭环 + 权限语义 |
| `test_upload_security.py` | 伪装 svg/html、未知类型、断连、超限 + 物理残留检查 |
| `test_admin_storage.py` | 存储统计、孤儿清理(dry-run/熔断)、删项目清物理文件、备份完整性 |
| `test_backup_restore.py` | 端到端恢复演练(造数据→备份→改→恢复→断言回到时点) |
| `test_avatar_color.py` | 头像取色跨接口一致(含 WS) |
| `visual_sweep.py` | 真实 app.js 逐页巡检,收集 onerror/console.error;**末尾真实点击断言交互**(侧栏项目行展开/收起、文档树折叠——"点了没反应"类缺陷只有点击+断言才抓得到) |
| `verify_page_assets.py` | 零外链 + no-cache(**改前端/内网部署前后必跑**) |

- 启动:`.venv/Scripts/python.exe main.py` + `TEAMDOC_DATA_DIR` + 非常用 `PORT`;首跑 `tests/_bootstrap.py` 建测试管理员(admin@teamdoc.local / admin12345)。
- **测试必须可重复运行**:断言写差值/唯一值,别假设数据目录干净(历史残留正是被测功能要处理的东西)。
- 手写脚本:stdlib urllib、显式 UTF-8;**别用 curl 发中文**(Windows GBK 乱码入库);URL 中文 `quote`;下载类响应先看 Content-Type 再解析;WS 地址从 `TD_BASE` 推导勿硬编码端口(会连到另一台服务,报 4401 假失败);响应头键小写化比较。
- 前端:JS 过 `node --check`;删 CSS 类前 grep 反查(注意 `'cls-'+x` 动态拼接);CSS 查大括号配平。
- Windows 服务管理:`netstat -ano | grep :端口` 找 PID,`taskkill //F //T //PID <pid>` 杀;**测试残留进程占端口会造成"双实例 + 脏 Cookie"的诡异故障,测完必杀**。
- 像素级验证:无头 Chrome 截图 + 差异比对(靠它抓出过列错位 140px、行高回归)。要点:同步 XHR 先登录(HttpOnly 会话无法伪造);`--virtual-time-budget` 下注入 `transition:none`;冻结 `Date`;断言写进页面 DOM;**必须真实点击 + 断言状态变化**——静态截图抓不到"处理器读旧属性名"类交互失效;换掉标记后 grep 所有读取处(dataset.x / [data-x])。

## 7. 已知遗留(择要)

- 窄屏 72px 图标栏下项目子项无文字,辨识度受限(统一导航机制的取舍;反馈不好可恢复窄屏两态)。
- `referenced` 徽标靠全项目扫描正文,文档上千后应改为保存时维护 `doc_file_refs` 索引表。
- 回收站/搜索静默截断 500;文件夹恢复不区分删除批次(子树里先前单独删掉的会一起回来)。
- 文本预览无大小截断(几十 MB 文件会卡浏览器);备份按 **DB 快照**打包(盘上无记录的孤儿不会进包,缺文件另计 `filesMissing`);不做增量备份;恢复只支持整站覆盖,不支持挑单文件取回、不支持回退旧版本代码。
- 公开项目的成员列表**只对真成员/全局管理员**返回 email/isDisabled;公开访客只拿 id/name/avatarColor(邮箱仍可经"同事目录"看到——那是任意登录用户的既定可见面)。公开是实例级,无部门/小组范围控制。
- 引用浮层 Esc 关闭后字面量 `@`/`[[` 留在正文;marked 行内 `$...$` 对价格文本可能误判;冷加载个人项目瞬间成员 tab 可能闪现;文档树与侧栏项目树渲染函数未合并(共用视觉语言)。

## 8. 路线图(用户表达过兴趣的方向)

- CLI `td`:**已落地**(2026-09-11)——独立的全局 ZCode skill(源码随 skill,不随本仓库;typer+httpx,`uv tool install`),服务端零改动。
- 日历模块:`PROJECT_NAV` 加一项即可接入。
- 字符级真协同:pycrdt/Yjs 替换 LWW。
