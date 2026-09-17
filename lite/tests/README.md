# 测试套件(pytest)

一条命令,服务器全自动托管:

```bash
cd lite/server
uv run pytest ../tests            # 快速组(默认,几秒到几十秒)
uv run pytest ../tests -m "slow or not slow"   # 全量(含弹性组)
uv run pytest ../tests -m slow    # 只跑弹性/压测组
```

不需要手工起服务、建管理员、杀进程:conftest.py 在会话开始自动起一台**隔离实例**
(空闲端口 + 一次性临时数据目录),bootstrap 管理员,结束时杀整棵进程树并删除
临时目录(含恢复演练改名保留的 `*.pre-restore-*`)。

浏览器巡检(`test_visual_sweep.py`)本机没有 Chrome/Edge 时自动 skip,不算失败。

## 结构

| 文件 | 职责 |
|---|---|
| `conftest.py` | fixture 装配:托管服务器、bootstrap、管理员客户端、巡检项目 |
| `_harness.py` | 唯一一份 HTTP 客户端(`Client`/`Resp`)、被托管服务器(`Server`)、`make_user`/`rand_email`/裸 socket 工具 |
| `_chrome.py` | 无头 Chrome 底座:临时校验页注入 + `--dump-dom` 取回结果 |
| `pytest.ini` | markers(`slow`/`browser`)与默认选项 |
| `test_smoke_endpoints.py` | 全路由状态码,**任何 5xx 即失败**(改完服务端先跑它) |
| `test_folder_recycle.py` | 文件夹递归删除/恢复/级联/移动/权限语义 |
| `test_doc_move.py` | 文档移动:项目内改父级/防环/跨项目权限矩阵/子树随迁/**回收站不随迁 + 恢复回落**/move-check;以及引用关系跨项目成立(反链、被引用) |
| `test_doc_move_drill.py` | 浏览器演练:文档树「更多」→「移动到…」→ 换项目 → 移动,真点击断言路由与树都跟着换 |
| `test_id_contract.py` | id 与引用契约的单元层:ULID 形状、字典序=时间序、IdPath 判定、两种引用形态与「只认 id-only」、结构自检能认出列类型不符 |
| `test_upload_security.py` | inline 白名单、mime 服务端判定、断连无孤儿、超限拒绝 |
| `test_admin_storage.py` | 存储统计、孤儿清理(不误删)、删项目清物理文件、备份 zip 完整性 |
| `test_backup_restore.py` | 备份多目标/保留策略、坏包、zip slip、孤儿熔断、**端到端恢复演练(自动重启)** |
| `test_files_paging.py` | 分页、服务端排序、重名、项目占用、最近文件 |
| `test_directory.py` | 同事目录字段面与可见性 |
| `test_visibility.py` | 公开项目:加入前不可读(全 403)、自助加入与 joinRole、广场、搜索不含未加入的公开项目;**文件分享链接**(匿名下载 / 换 token / 过期 / 吊销 / 权限) |
| `test_doc_conflict.py` | 正文写入的基线校验与版本规则:PUT 过期基线 409(服务端内容不动)/web 缺基线 400/同内容短路、WS 冲突只回发送者、append 与 restore 两条豁免、空内容不留还原点、版本记录形状(kind/作者/字数) |
| `test_avatar_color.py` | 头像取色跨接口一致(含 WS) |
| `test_login_throttle.py` | 登录节流:账号维度(锁定/退避/清零/**重启后仍锁**)与来源维度(密码喷洒),自带专用实例 |
| `test_session_audit.py` | 管理端登录状态与审计:用户列表字段、用户详情、强制下线、解锁、登录动态 |
| `test_frontend_refs.py` | 前端跨模块引用的静态检查:`Mod.fn()` 必须真的在模块导出面上(漏导出只在用户点到那个入口时才炸,构建步骤缺失,只能静态盯) |
| `test_page_assets.py` | 零外链 + no-cache + KaTeX 字体/Prism 语言包/Mermaid 图表库全量可达(**内网部署前后必跑,只读可对生产**) |
| `test_visual_sweep.py` | 真实 app.js 巡检:逐页渲染 + 点击交互 + App.route 生成点串比对 + 管理后台五区冒烟 |
| `test_join_drill.py` | 浏览器演练:公开项目自助加入的两条入口(发现页点卡片 / 直链 403),真点击到"加入后能读" |
| `test_markdown_render.py` | Markdown 语法规则单测(在 node 里加载真实 markdown.js,毫秒级;缺 node 自动 skip):公式定界/脚注/高亮/上下标/代码块 class/HTML 转义、"懒加载什么时候该拉库",以及文内锚点点击的分流(拦文内锚点、放行 `#/` 路由) |
| `test_markdown_diagram.py` | 浏览器演练:Markdown 扩展语法 —— 图表(Mermaid)懒加载门槛/渲染出 SVG、公式与价格文本的边界、脚注角标与 ↩ 回跳真的滚动且**不改路由**、零跨源请求、切主题重画 |
| `test_logging.py` | 日志旁路:队列满不阻塞调用方;输出端被挂起时服务照常应答(后两条标 `slow`);控制台仍是 uvicorn 自己的渲染 |
| `test_server_resilience.py` | `slow`:卡住的传输/大量长连接不拖垮其他请求 |

起服、登录、上传、断言这些动作各只有一份实现,都在 `_harness` + conftest 里;
测试文件只写场景。每个会话自建隔离实例(临时数据目录 + 空闲端口 + 真实子进程),
所以可以随便造数据,也能测"重启后生效"这类行为。

## 特殊场景

- **需要自定义阈值的测试**(登录节流这类)自己起一台实例,不要挂在共享实例上:
  ```python
  s = Server(extra_env={"LOGIN_MAX_FAILS": "3", "LOGIN_IP_MAX_FAILS": "0"})
  s.start(); admin = s.admin_client()   # 起服 + bootstrap + 登录,一步到位
  ```
  共享实例上所有客户端都来自 127.0.0.1,来源 IP 的失败计数会互相累积
  (conftest 已把共享实例的 `LOGIN_IP_MAX_FAILS` 放宽到 200 免得互相踩)。
  只开被测的那一维度(另一个设 0 或极大),断言才是确定性的。

- **超限上传**(`test_oversize_upload_rejected`):服务端默认上限 20GB,3MB 测试文件
  不会超限,该场景默认 skip。要真正跑到它,以小上限起测试实例:
  ```bash
  TD_MAX_UPLOAD_MB=1 uv run pytest ../tests/test_upload_security.py
  ```
- **弹性组**(`test_server_resilience.py`)默认不跑(标了 `slow`):它要传 16MB 文件、
  挂起 40+ 条连接,分钟级耗时。改了连接池/流式传输/WS 相关代码后务必 `-m slow` 跑一次。

## 写新测试的约定

- 从 fixture 拿 `admin`(已登录的管理员 `Client`),`make_user(admin, 名字)` 建随机邮箱
  用户;别的身份 `c = Client(base_url); c.login(email, pw)`。
- **用户邮箱一律随机后缀**(`make_user` 已保证):系统没有删除用户接口,固定邮箱
  二跑必撞 409,而它看起来会像服务端 bug。
- 数据目录是一次性的,断言绝对值安全;但同一会话内其他测试的数据还在,**别假设
  "全库只有一个项目"这类全局性质**。
- 别再复制 call/login/upload helper —— `_harness.Client` 里各只有一份。
- HTTP 走 `Client`(stdlib urllib、显式 UTF-8);**别用 curl 发中文**(Windows GBK
  会把请求体写坏);URL 中文先 `quote`;响应头键已被统一小写化。
- WS 地址从 `base_url` 推导,勿硬编码端口 —— 硬编码会连到另一台服务,报 4401 假失败。

## 故障排查

- 端口被占/起不来:几乎不会发生(fixture 每次选空闲端口)。若怀疑有残留实例:
  ```bash
  netstat -ano | grep LISTENING | grep :8123   # 找 PID
  taskkill //F //T //PID <pid>                 # 杀整树
  ```
  残留进程占端口曾造成"双实例 + 脏 Cookie"的诡异故障(现在套件会自清,但手工起过
  的服务要自己收尾)。
- 测试失败想看服务器日志:数据目录已被清理;重跑单个测试并加 `-s`,或临时在
  `_harness.Server.cleanup` 里注释掉删除逻辑。
