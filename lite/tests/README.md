# 测试脚本

标准库 `urllib` + `websockets` 编写,不依赖 pytest,可直接运行。
**不要用 curl 发中文**:Windows GBK 会把中文请求体写坏,必须用 Python 脚本。

## 运行前提

服务端必须在跑(建议用隔离数据目录与非常用端口,避免污染真实数据):

```bash
cd lite/server
TEAMDOC_DATA_DIR=/tmp/td_test PORT=8123 .venv/Scripts/python.exe main.py
```

首次需要建管理员账号(`_bootstrap.py`,已初始化过会提示已初始化,属正常):

```bash
.venv/Scripts/python.exe tests/_bootstrap.py
```

> 各脚本默认连 `http://127.0.0.1:8123`,账号为
> `admin@teamdoc.local` / `admin12345`(由 `_bootstrap.py` 创建)。
> 换端口/账号需同时改脚本顶部的 `BASE` 与账号常量。

## 脚本说明

| 脚本 | 作用 |
|---|---|
| `smoke_all_endpoints.py` | 遍历全部 API 路由并断言期望状态码,**任何 5xx 视为失败**。改完服务端先跑这个(NameError/TypeError 类回归的护栏)。 |
| `test_folder_recycle.py` | 文件夹回收站闭环:非空拒删、回收站分组与倒序、父子回落、级联彻底删除、重复操作 409、权限语义(VIEWER/非成员 403 且不泄露状态)。 |
| `test_avatar_color.py` | 头像取色一致性:成员列表 / 用户列表 / auth me / 重复请求 / WS presence 五处交叉比对同一用户色值。 |
| `verify_page_assets.py` | 模拟浏览器加载 index.html:递归校验全部静态引用可达、零外链、`Cache-Control: no-cache` 生效。**纯内网部署前后的必跑项**。 |

## 清理

测试会自建项目并在结束时删除;若中途失败留下残留,直接删掉隔离数据目录即可:

```bash
taskkill //F //T //PID $(netstat -ano | grep :8123 | grep LISTENING | awk '{print $5}' | head -1)
rm -rf /tmp/td_test
```

**测完务必杀进程**:残留进程占着端口会导致"双实例 + 脏 Cookie"等诡异故障。
