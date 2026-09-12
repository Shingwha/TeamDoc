"""日志装配:调用方只入队,写出(控制台 + 轮转文件)交给独立线程。

**为什么必须这样**(2026-09-12 事故):uvicorn 的访问日志是在**事件循环线程**上发出的,
而写控制台在 Windows 上会阻塞 —— 控制台窗口一旦被鼠标选中(快速编辑模式),conhost
就挂起所有输出,事件循环随之冻结:进程活着、没有任何报错、全站客户端连不上(当时主线程
栈停在 logging 的 stream.write,连看门狗的告警也一起卡死)。磁盘同理(慢盘/坏盘不能让
主路径陪着等)。所以本模块只做一件事:把"写出去"从调用线程挪到写出线程,队列满就丢日志。

控制台外观与改动前一致:uvicorn 自带的两个 handler(配色 + 紧凑格式 + 状态短语)被整体
搬到写出线程上沿用,应用日志用带时间戳的格式;落盘则一律用后者,便于 grep。
"""
import logging
import queue
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

from models import DATA_DIR

# 日志跟数据走(TEAMDOC_DATA_DIR/logs):同一实例的日志与数据在一起,多实例/测试天然隔离
LOG_DIR = DATA_DIR / "logs"
FORMAT = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

_queue: queue.Queue | None = None
_listener: QueueListener | None = None
_file_handler: RotatingFileHandler | None = None


class _Filter(logging.Filter):
    """按 logger 名把记录分给不同的控制台 handler,避免同一行被写两遍。"""

    def __init__(self, accept):
        super().__init__()
        self._accept = accept

    def filter(self, record):
        return self._accept(record.name)


class _Handler(QueueHandler):
    """只入队:队列满(写出线程被卡住)就丢记录,绝不阻塞调用它的线程。

    入队时**原样**传递记录,不做默认 prepare 的"压成字符串"处理:写出线程上的 handler
    还要按各自的格式渲染,而 uvicorn 访问日志的格式化器依赖 record.args 里的字段。
    """

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass

    def prepare(self, record):
        return record


def _take_uvicorn_handlers():
    """取下 uvicorn 按启动参数配好的控制台 handler(访问日志一个、其余一个),
    连同它自带的格式与配色一起沿用;返回 (access_handlers, other_handlers)。"""
    access_lg = logging.getLogger("uvicorn.access")
    access = list(access_lg.handlers)
    access_lg.handlers = []
    others = []
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            if isinstance(h, QueueHandler):
                continue  # 上一轮我们自己挂的队列 handler(重复调用时别把它当 uvicorn 的)
            lg.removeHandler(h)
            if h not in access and h not in others:
                others.append(h)
    return access, others


def setup():
    """装配日志:应用与 uvicorn 的记录统一入队,由写出线程渲染到控制台与文件;幂等。

    uvicorn 的 handler 由它的 dictConfig 按启动方式在不同时机创建(CLI 启动在导入前、
    python main.py 在 Config 构造时),所以入口在导入时与 __main__ 里各调一次,
    每次重新取一遍它当前的 handler。
    """
    global _queue, _listener, _file_handler

    access_hs, other_hs = _take_uvicorn_handlers()
    if not access_hs and not other_hs:
        other_hs = [logging.StreamHandler()]  # 非常规启动:没有 uvicorn 配置,退回单一控制台
        other_hs[0].setFormatter(FORMAT)
    for h in access_hs:
        h.filters = [_Filter(lambda n: n == "uvicorn.access")]
    for h in other_hs:
        h.filters = [_Filter(lambda n: n.startswith("uvicorn") and n != "uvicorn.access")]
    app_console = logging.StreamHandler()
    app_console.setFormatter(FORMAT)
    app_console.filters = [_Filter(lambda n: not n.startswith("uvicorn"))]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if _file_handler is None:
        _file_handler = RotatingFileHandler(LOG_DIR / "teamdoc.log", maxBytes=10 * 1024 * 1024,
                                            backupCount=5, encoding="utf-8")
        _file_handler.setFormatter(FORMAT)
    handlers = (*access_hs, *other_hs, app_console, _file_handler)
    if _queue is None:
        _queue = queue.Queue(maxsize=20000)
        _listener = QueueListener(_queue, *handlers)
        _listener.start()
    else:
        _listener.handlers = handlers  # uvicorn 重配过 logger:换掉写出线程上的这组 handler

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [_Handler(_queue)]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [_Handler(_queue)]
        lg.propagate = False
    # 日志自身的错误也别去写 stderr —— 那同样是一条可能被挂起的通道
    logging.raiseExceptions = False
