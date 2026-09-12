"""pytest fixture 装配:一台全自动托管的隔离实例贯穿整个测试会话。

会话开始:选空闲端口 → 建临时数据目录 → 起服务器 → bootstrap 管理员;
会话结束:杀整棵进程树 → 删临时目录。不再有手工起服/bootstrap/杀进程,
也不再需要 TD_BASE / TD_DATA_DIR / TD_BK_DIRS / TD_PID / TD_DOC 这些环境变量契约。

进程与数据的清理规则(_harness.Server):杀进程必须整树 —— 残留进程占端口是
"双实例 + 脏 Cookie"事故的根源;数据目录是一次性的,测试可随便造。
"""
import os

import pytest

from _harness import Server

# 保留旧脚本的超限上传测试能力:设了 TD_MAX_UPLOAD_MB 就用小上限起服务,
# 测试端用同一值判断该场景跑还是跳。
_max_mb = os.environ.get("TD_MAX_UPLOAD_MB")


@pytest.fixture(scope="session")
def backup_dirs(tmp_path_factory):
    """三个备份目标目录:两个真实存在,一个故意不存在(测"不自动创建"契约)。"""
    base = tmp_path_factory.mktemp("backup")
    d1 = base / "bk1"
    d1.mkdir()
    d2 = base / "bk2"
    d2.mkdir()
    return [str(d1), str(d2), str(base / "ghost-dir")]


@pytest.fixture(scope="session")
def server(backup_dirs):
    extra = {"BACKUP_DIRS": ",".join(backup_dirs)}
    if _max_mb:
        extra["MAX_UPLOAD_MB"] = _max_mb
    # 本套件的全部客户端都来自 127.0.0.1:登录失败会累进"来源 IP"节流桶。
    # 共享实例上刻意放宽它,免得某个测试把会话级的桶打满、后续测试集体收到 429
    # (那种失败看起来像服务端 bug)。来源维度本身的语义由
    # test_login_throttle.py 的专用实例验证。
    extra["LOGIN_IP_MAX_FAILS"] = "200"
    s = Server(extra_env=extra)
    s.start()
    yield s
    s.stop()
    s.cleanup()


@pytest.fixture(scope="session")
def base_url(server):
    return server.base_url


@pytest.fixture(scope="session")
def data_dir(server):
    """隔离实例的数据目录(物理文件断言用)。"""
    return server.data_dir


@pytest.fixture(scope="session")
def max_upload_mb():
    return int(_max_mb or 20480)


@pytest.fixture(scope="session")
def admin(server):
    """管理员客户端:bootstrap(已初始化 403 属正常)后登录。"""
    return server.admin_client()


@pytest.fixture(scope="session")
def proj_doc(admin):
    """一个带父子文档的协作项目(浏览器巡检用;个人空间没有成员页,巡检必须用协作项目)。"""
    r = admin.post("/api/projects", {"name": "巡检项目", "description": "浏览器巡检专用"})
    assert r.status == 200, f"建巡检项目失败: {r.data}"
    pid = r.data["id"]
    d = admin.post(f"/api/projects/{pid}/docs", {"title": "巡检父文档"})
    assert d.status == 200, f"建巡检文档失败: {d.data}"
    sub = admin.post(f"/api/projects/{pid}/docs",
                     {"title": "巡检子文档", "parentId": d.data["id"]})
    assert sub.status == 200, f"建巡检子文档失败(文档树折叠断言需要它): {sub.data}"
    yield pid, d.data["id"]
    admin.delete(f"/api/projects/{pid}")
