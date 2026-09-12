"""测试共享底座:一份 HTTP 客户端 + 一台被托管的隔离实例。

历史:这套测试原是 14 个手写 stdlib 脚本,call/login/upload/检查器在每个文件各复制一份,
服务进程全靠手工起停(起服务 → bootstrap → 跑完杀进程)。本模块把它们收敛为一处,
conftest.py 再把它们包成 pytest fixture。

从旧脚本继承的坑,别丢:
- 服务器经 `python main.py` + PORT / TEAMDOC_DATA_DIR 环境变量启动(见 main.py __main__,
  它不读 .env,环境变量注入是安全的)
- 杀进程必须整树(taskkill /F /T):残留进程占端口 = "双实例 + 脏 Cookie"事故根源
- 响应头键大小写不稳定,统一小写化后再比较
- 用户邮箱一律随机后缀:系统没有删除用户接口,固定邮箱二跑必撞 409
- 中文 body 只能走 urllib(Windows 的 curl/GBK 会写坏请求体),本模块已保证
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent / "server"
WEB_DIR = TESTS_DIR.parent / "web"

ADMIN_EMAIL = "admin@teamdoc.local"
ADMIN_PASSWORD = "admin12345"
READY_TIMEOUT = 60.0


def _venv_python():
    if sys.platform == "win32":
        return SERVER_DIR / ".venv" / "Scripts" / "python.exe"
    return SERVER_DIR / ".venv" / "bin" / "python"


class Resp:
    """一次 HTTP 交互的结果:data 为 JSON 解析结果(非 JSON 则原始 bytes),headers 键已小写。"""

    def __init__(self, status, data, headers):
        self.status = status
        self.data = data
        self.headers = headers

    def __repr__(self):
        return f"<Resp {self.status} {str(self.data)[:100]}>"


def _parse_body(raw: bytes, ctype: str):
    if "json" in (ctype or ""):
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            pass
    return raw


class Client:
    """带会话的 HTTP 客户端。sid 由 login 绑定;每次响应都接收会话刷新(与浏览器一致)。

    bearer 用于 PAT 路径:设了它就按 `Authorization: Bearer` 发请求(此时不带 Cookie,
    与 CLI/脚本一致)——服务端会据此把调用判为 via="pat",与浏览器走不同的规则分支。
    """

    def __init__(self, base_url, bearer=None):
        self.base_url = base_url.rstrip("/")
        self.sid = None
        self.bearer = bearer

    def request(self, method, path, body=None, raw_body=None,
                ctype="application/json", timeout=60):
        data = raw_body
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None and ctype:
            req.add_header("Content-Type", ctype)
        if self.bearer:
            req.add_header("Authorization", "Bearer " + self.bearer)
        elif self.sid:
            req.add_header("Cookie", "td_sid=" + self.sid)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                headers = {k.lower(): v for k, v in r.headers.items()}
                sc = r.headers.get("Set-Cookie")
                if sc and "td_sid=" in sc:
                    self.sid = sc.split("td_sid=")[1].split(";")[0]
                return Resp(r.status, _parse_body(r.read(), headers.get("content-type")), headers)
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in e.headers.items()}
            return Resp(e.code, _parse_body(e.read(), headers.get("content-type")), headers)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, body=body, **kw)

    def patch(self, path, body=None, **kw):
        return self.request("PATCH", path, body=body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def login(self, email, password):
        """登录并把会话绑定到本客户端。预期外的失败直接炸出来。"""
        r = self.post("/api/auth/login", {"email": email, "password": password})
        assert r.status == 200, f"登录失败 {email}: {r.status} {r.data}"
        return r

    def upload(self, pid, filename, content, folder_id=None, ctype="application/octet-stream"):
        """raw body 上传:请求体即文件,元数据走 query string(见 files.py upload_file)。

        ctype 参数保留:"攻击者伪声明 mime"这个攻击面虽然已被服务端按扩展名判定堵死,
        把伪声明意图留在测试里可读仍有价值 —— 现在的真实攻击面只剩文件名。
        """
        qs = f"?projectId={urllib.parse.quote(str(pid))}&name={urllib.parse.quote(filename)}"
        if folder_id:
            qs += "&folderId=" + urllib.parse.quote(str(folder_id))
        return self.post("/api/files/upload" + qs, raw_body=content, ctype=ctype)


def rand_email(prefix="u"):
    """随机后缀邮箱:系统无删除用户接口,固定邮箱二跑必撞 409。"""
    return f"{prefix}-{uuid.uuid4().hex[:6]}@t.local"


def make_user(admin, name, password="pw12345678"):
    """用管理员建一个随机邮箱用户,返回 (email, user_json)。"""
    email = rand_email()
    r = admin.post("/api/users", {"email": email, "name": name, "password": password})
    assert r.status == 200, f"建用户失败: {r.status} {r.data}"
    return email, r.data


def abort_raw_upload(base_url, sid, pid, filename, total_bytes, send_bytes):
    """裸 socket 发一个 raw body 上传,发到一半直接断开连接。

    模拟"传大文件时网络断/用户关页面":服务端已写了部分字节到 data/files,
    但没有 DB 记录 —— 若不清理,这个文件就永久占着磁盘且界面里看不见。
    用裸 socket 是为了能在任意时刻切断(urllib 会一次性写完再等响应)。
    """
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = (f"/api/files/upload?projectId={urllib.parse.quote(str(pid))}"
            f"&name={urllib.parse.quote(filename)}")
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Cookie: td_sid={sid}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {total_bytes}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        s = socket.create_connection((host, port), timeout=15)
        s.sendall(head)
        # 分块发送,便于在中间切断(一次 sendall 大块会被内核缓冲,断点不精确)
        chunk = b"x" * (64 * 1024)
        sent = 0
        while sent < send_bytes:
            s.sendall(chunk)
            sent += len(chunk)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()
        return True
    except OSError:
        return False


def ws_url(base_url, doc_id):
    """由 HTTP base 推导 WS 地址(不要写死端口)。"""
    return urllib.parse.urlsplit(base_url)._replace(scheme="ws",
                                                    path=f"/ws/docs/{doc_id}").geturl()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    """一台被 pytest 托管的隔离实例(空闲端口 + 临时数据目录),支持演练用的重启。"""

    def __init__(self, extra_env=None, stdout_pipe=False):
        self.port = None
        self.data_dir = None
        self.proc = None
        self._log_fh = None
        self._extra_env = dict(extra_env or {})
        # stdout_pipe=True:输出接到一条**从不读取**的管道上。用于验证"输出端被挂起"
        # (Windows 控制台被选中 / 管道无人消费)时服务不受影响,见 test_logging.py
        self._stdout_pipe = stdout_pipe

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.port = _free_port()
        self.data_dir = Path(tempfile.mkdtemp(prefix="td_test_"))
        self._spawn()

    def restart(self):
        """杀掉后用同一数据目录、同一端口重启(恢复演练:恢复在启动时应用)。"""
        self.stop()
        self._spawn()

    def _spawn(self):
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["TEAMDOC_DATA_DIR"] = str(self.data_dir)
        env.update(self._extra_env)
        if self._stdout_pipe:
            self.out = subprocess.PIPE
        else:
            self._log_fh = open(self.data_dir / "server.log", "ab")
            self.out = self._log_fh
        self.proc = subprocess.Popen([str(_venv_python()), "main.py"], cwd=str(SERVER_DIR),
                                     env=env, stdout=self.out, stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.time() + READY_TIMEOUT
        url = self.base_url + "/api/auth/status"
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务器启动即退出(exit={self.proc.returncode}):\n"
                                   + self._log_tail())
            try:
                urllib.request.urlopen(url, timeout=2)
                return
            except urllib.error.HTTPError:
                return  # 服务已在响应(即使该路由报错,起没起来由测试断言说话)
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"服务器 {READY_TIMEOUT}s 内未就绪:\n" + self._log_tail())

    def _log_tail(self, n=40):
        if self._stdout_pipe:
            return "(stdout 被刻意挂起,未读取)"
        try:
            lines = (self.data_dir / "server.log").read_text("utf-8", "replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return "(无日志)"

    def admin_client(self) -> "Client":
        """一个已登录的管理员客户端(bootstrap 已初始化则直接登录)。

        供"自带实例"的测试用:节流这类测试必须自己控阈值,不能挂在会话级共享实例上 ——
        共享实例里所有客户端都来自 127.0.0.1,一个人的失败会算进所有人的来源桶。
        """
        c = Client(self.base_url)
        r = c.post("/api/auth/bootstrap",
                   {"email": ADMIN_EMAIL, "name": "管理员", "password": ADMIN_PASSWORD})
        assert r.status in (200, 403), f"bootstrap 异常: {r.status} {r.data}"
        if r.status != 200:
            c.login(ADMIN_EMAIL, ADMIN_PASSWORD)
        return c

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None

    def cleanup(self):
        """清掉本会话产生的一切磁盘痕迹:数据目录本体 + 恢复演练改名保留的
        `*.pre-restore-*` 旁目录。Windows 上刚被杀的进程可能短暂锁着文件,
        轮询重试几轮再放弃。"""
        if self.data_dir is None:
            return
        base, name = self.data_dir.parent, self.data_dir.name
        for _ in range(6):
            for p in base.glob(f"{name}*"):
                shutil.rmtree(p, ignore_errors=True)
            if not any(base.glob(f"{name}*")):
                break
            time.sleep(0.5)
        self.data_dir = None
