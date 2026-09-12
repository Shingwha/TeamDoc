# 测试脚本

标准库 `urllib` + `websockets` 编写,不依赖 pytest,可直接运行。
**不要用 curl 发中文**:Windows GBK 会把中文请求体写坏,必须用 Python 脚本。

## 运行前提

服务端必须在跑(建议用隔离数据目录与非常用端口,避免污染真实数据):

```bash
cd lite/server
TEAMDOC_DATA_DIR=/tmp/td_test PORT=8123 .venv/Scripts/python.exe main.py
```

脚本默认连 `http://127.0.0.1:8123`。要校验其他实例(例如跑在 8000 的真实实例),
用环境变量覆盖:

```bash
TD_BASE=http://127.0.0.1:8000 python tests/verify_page_assets.py
```

首次需要建管理员账号(`_bootstrap.py`,已初始化过会提示已初始化,属正常):

```bash
.venv/Scripts/python.exe tests/_bootstrap.py
```

> 账号为 `admin@teamdoc.local` / `admin12345`(由 `_bootstrap.py` 创建)。
> 注意:这些脚本会**创建并删除测试数据**,`smoke_all_endpoints.py` 还会临时创建一个用户,
> **不要直接对着真实数据目录运行**;`verify_page_assets.py` 是只读的,可安全用于生产实例。

## 脚本说明

| 脚本 | 作用 |
|---|---|
| `smoke_all_endpoints.py` | 遍历全部 API 路由并断言期望状态码,**任何 5xx 视为失败**。改完服务端先跑这个(NameError/TypeError 类回归的护栏)。测试用户邮箱带随机后缀,可重复运行。 |
| `test_folder_recycle.py` | 文件夹语义闭环:**递归删除非空文件夹**、回收站只列子树根、**递归恢复**、父级仍在回收站时回落项目根、级联彻底删除(含物理文件)、文件夹/文件移动(项目内 EDITOR、跨项目需源 ADMIN、拒绝移入自己后代、跨项目子树跟随)、权限语义(VIEWER/非成员 403 且不泄露状态)。 |
| `test_upload_security.py` | 上传安全与类型白名单:伪装 svg/html 强制 attachment、未知类型不给 inline、白名单类型仍可 inline、客户端中途断开不留孤儿文件、超限拒绝。 |
| `test_admin_storage.py` | 管理后台:存储统计各段、非管理员一律 403、孤儿文件识别与清理(不误删正常文件)、删项目清物理文件、回收站占用单列、备份 zip 完整性与可恢复性。 |
| `test_backup_restore.py` | 备份与恢复:多目标目录一次写入、保留策略(只留最近 N 份且**不误删手工文件**)、目标目录不存在记为失败且不自动创建、坏包(非 zip/缺库/空 body)被拒、恢复上传的 **zip slip 白名单**(穿越/绝对路径/子目录/意外条目)、孤儿清理 **dry-run 不删文件 + 熔断拒绝 + force 越过**、**端到端恢复演练**(造数据→备份→改数据→上传→arm→重启→断言回到备份时点)。 |
| `test_files_paging.py` | 云空间:分页(翻页不重不漏、hasMore)、服务端排序(名称/大小/时间,非法参数回落)、重名(上传自动加后缀 / 显式操作 409 / 改名重算 mime)、项目占用统计(活跃与回收站分列)、跨项目最近文件与可见性隔离。 |
| `test_directory.py` | 同事目录:任意登录用户可读、字段面不含 isAdmin/isDisabled/createdAt、禁用账号不出现、未登录 401、目录 email 可直接加成员。 |
| `test_visibility.py` | 公开项目与单文件公开:私有项目非成员 403、公开项目可读但写全拒、isMember 区分成员与访客、个人空间不可公开(403 且不入广场)、单文件公开只放开那一个文件、广场按活跃倒序、搜索与最近文件并入公开项目、关闭公开立即失效。 |
| `test_avatar_color.py` | 头像取色一致性:成员列表 / 用户列表 / auth me / 重复请求 / WS presence 五处交叉比对同一用户色值。 |
| `verify_page_assets.py` | 模拟浏览器加载 index.html:递归校验全部静态引用可达、零外链、`Cache-Control: no-cache` 生效。**纯内网部署前后的必跑项**。 |
| `visual_sweep.py` | **逐页巡检 + 交互断言**:用真实 app.js 驱动全部路由(首页/发现/搜索/设置/管理后台/文档/文档详情/云空间/成员/回收站/项目设置),收集 window.onerror 与 console.error;末尾再做**真实点击**断言(侧栏项目行展开→再点收起;有子文档时点文档树折叠箭头,断言子层 `hidden` 切换)。改完前端路由、权限判定或交互后必跑 —— 它抓的是"页面整块崩了"和"点了没反应"这两类静态截图看不出的问题。需 `TD_PID`(项目 id,**请传协作项目**:个人空间没有成员页,传它会看到一条预期内的假失败),可选 `TD_DOC`(文档 id)以覆盖编辑器页。 |

`test_admin_storage.py` 要看物理文件残留在不在,需额外传 `TD_DATA_DIR` 指向实例的数据目录:

```bash
TD_BASE=http://127.0.0.1:8123 TD_DATA_DIR=/tmp/td_test python tests/test_admin_storage.py
```

`test_backup_restore.py` 需要 `TD_DATA_DIR`(制造孤儿)与 `TD_BK_DIRS`(备份目标目录,逗号分隔)。
它的**恢复演练分两步**:第一次跑会造数据、备份、上传并置为待生效,然后打印提示;
此时**手动重启服务**(恢复在启动时应用),再带 `--verify-restore` 跑第二次完成断言:

```bash
TD_BASE=http://127.0.0.1:8123 TD_DATA_DIR=/tmp/td_test TD_BK_DIRS=/tmp/td_test/bk \
  python tests/test_backup_restore.py
# 重启服务,然后:
TD_BASE=http://127.0.0.1:8123 python tests/test_backup_restore.py --verify-restore
```

## 写测试的注意

- **断言用差值或唯一值,别假设数据目录是干净的**:实例数据目录常被复用,历史残留(比如孤儿文件)正是被测功能要处理的东西,写成"绝对值必须为 0"会假失败。
- **响应头键的大小写不稳定**,读之前先小写化(`{k.lower(): v for k, v in resp.headers.items()}`)。


## 清理

测试会自建项目并在结束时删除;若中途失败留下残留,直接删掉隔离数据目录即可:

```bash
taskkill //F //T //PID $(netstat -ano | grep :8123 | grep LISTENING | awk '{print $5}' | head -1)
rm -rf /tmp/td_test
```

**测完务必杀进程**:残留进程占着端口会导致"双实例 + 脏 Cookie"等诡异故障。
