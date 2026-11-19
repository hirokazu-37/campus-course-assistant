"""Windows桌面外壳：独立WebView2窗口与受控本地服务，业务仍由原模块处理。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from threading import Thread
import time
import urllib.request
from uuid import uuid4

from . import __version__

TITLE = "校园课程管家"


def resource_root() -> Path:
    return Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def application_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else resource_root()


def choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DesktopLog:
    def __init__(self, data_dir: Path):
        folder = data_dir / "logs" / "desktop-runs"
        folder.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid4().hex
        self.path = folder / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{self.run_id}.jsonl"
        self.server_log = self.path.with_suffix(".server.log")

    def write(self, event: str, **details):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **details}, ensure_ascii=False) + "\n")


def prepare_runtime(root: Path, data_dir: Path) -> Path:
    from .service import TaskService
    from .parsing import ParserSettings
    service = TaskService(data_dir)
    service.initialize()  # 损坏文件必须停止，不覆盖已有台账。
    settings = root / "settings" / "parsing.json"
    with service.store.lock():
        if not settings.exists():
            source = resource_root() / "settings" / "parsing.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            service.store._atomic_bytes(settings, source.read_bytes())
    ParserSettings.load(settings)
    return settings


class WindowsJob:
    """父进程退出时由Windows回收整个服务进程树，包括单文件打包子进程。"""
    def __init__(self, process):
        if os.name != "nt":
            self.handle = None
            return
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                        ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Limits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", Counters), ("process_mem", ctypes.c_size_t),
                        ("job_mem", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "无法建立桌面服务进程管理")
        info = Limits(); info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            code = ctypes.get_last_error(); self.close()
            raise OSError(code, "无法管理桌面服务进程，已取消启动")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def server_command(root: Path, data_dir: Path, port: int, route: str, log: Path) -> list[str]:
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(resource_root() / "desktop_app.pyw"))
    return [*command, "--serve", "--root", str(root), "--data-dir", str(data_dir), "--port", str(port), "--route", route,
            "--server-log", str(log), "--stop-file", str(log.with_suffix(".stop"))]


class LocalServer:
    def __init__(self, root: Path, data_dir: Path, log: DesktopLog):
        self.root, self.data_dir, self.log = root, data_dir, log
        self.process = self.job = self.output = None
        self.worker_pid = None
        self.port = choose_port()
        self.route = "desktop-" + secrets.token_hex(12)
        self.url = f"http://127.0.0.1:{self.port}/{self.route}/"

    def start(self, *, timeout: float = 60):
        self.output = self.log.server_log.open("a", encoding="utf-8")
        command = server_command(self.root, self.data_dir, self.port, self.route, self.log.server_log)
        try:
            self.process = subprocess.Popen(command, cwd=self.root, stdin=subprocess.DEVNULL, stdout=self.output,
                                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.job = WindowsJob(self.process)
            self.log.write("server_spawned", pid=self.process.pid, port=self.port)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                if self.process.poll() is not None:
                    raise RuntimeError("本地界面服务未能启动，请查看桌面运行日志")
                try:
                    with opener.open(self.url + "_stcore/health", timeout=1) as response:
                        if response.status == 200 and response.read(32) == b"ok":
                            state = json.loads(self.log.server_log.with_suffix(".state.json").read_text(encoding="utf-8"))
                            if state.get("route") != self.route or type(state.get("pid")) is not int or state["pid"] <= 0:
                                raise RuntimeError("本地服务身份校验失败")
                            self.worker_pid = state["pid"]
                            self.log.write("server_ready", pid=self.process.pid, worker_pid=self.worker_pid, port=self.port)
                            return
                except OSError:
                    pass
                time.sleep(0.15)
            raise TimeoutError("本地界面服务启动超时，请查看桌面运行日志")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.process:
            try:
                if self.process.poll() is None:
                    self.log.server_log.with_suffix(".stop").touch()
                    try:
                        self.process.wait(timeout=12)
                        self.log.write("server_stopped", exit_code=self.process.returncode, graceful=True)
                    except subprocess.TimeoutExpired:
                        self.log.write("server_stop_timeout", graceful=False)
                        if self.job:
                            self.job.close()
                        self.process.kill(); self.process.wait(timeout=5)
            finally:
                if self.job:
                    self.job.close()
                if self.output:
                    self.output.close()
                self.process = self.job = self.output = None
        elif self.output:
            self.output.close()
            self.output = None

    def busy(self) -> bool:
        """只检查本应用服务拥有的锁，不因其他窗口或旧锁阻止退出。"""
        if not self.process or self.process.poll() is not None or self.worker_pid is None:
            return False
        paths = [self.data_dir / ".write.lock", *(self.data_dir / "cache" / stage / ".write.lock" for stage in ("parse", "relations"))]
        for path in paths:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("pid") == self.worker_pid:
                    return True
            except (OSError, ValueError, AttributeError):
                pass
        return False


def serve(args) -> int:
    # 独立隐藏进程；父进程通过本次运行专属标记请求正常退出，不依赖控制台stdin。
    output = args.server_log.open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = output
    from .storage import JsonStore
    JsonStore._atomic_bytes(args.server_log.with_suffix(".state.json"),
                            json.dumps({"pid": os.getpid(), "route": args.route}).encode("utf-8"))
    os.environ["COURSE_ASSISTANT_DATA_DIR"] = str(args.data_dir)
    os.environ["COURSE_ASSISTANT_PROJECT_ROOT"] = str(args.root)
    os.environ["COURSE_ASSISTANT_SETTINGS_PATH"] = str(args.root / "settings" / "parsing.json")
    os.chdir(args.root)
    from streamlit.runtime.runtime import Runtime
    from streamlit.web import bootstrap
    def wait_for_close():
        while not args.stop_file.exists():
            time.sleep(0.2)
        for _ in range(600):
            if Runtime.exists():
                try:
                    Runtime.instance().stop()
                    return
                except RuntimeError:
                    pass
            time.sleep(0.1)
    Thread(target=wait_for_close, name="desktop-shutdown", daemon=True).start()
    options = {
        "server.address": "127.0.0.1", "server.port": args.port, "server.baseUrlPath": args.route,
        "server.headless": True, "server.fileWatcherType": "none", "server.runOnSave": False,
        "browser.gatherUsageStats": False, "global.developmentMode": False, "client.toolbarMode": "viewer",
        "theme.base": "light", "theme.primaryColor": "#16786F", "theme.backgroundColor": "#F7F9F8",
        "theme.secondaryBackgroundColor": "#EDF3F1", "theme.textColor": "#203934",
    }
    # bootstrap.run并不代替CLI加载参数；必须显式应用地址/随机端口/独立路径。
    bootstrap.load_config_options(options)
    bootstrap.run(str(resource_root() / "streamlit_app.py"), False, [], options)
    return 0


def gui_smoke(window, log: DesktopLog):
    """开发者显式测试模式，只读临时台账，不触发模型或通知。"""
    try:
        until = time.monotonic() + 45
        while time.monotonic() < until:
            try:
                result = window.evaluate_js("({title:document.title,nav:[...document.querySelectorAll('.st-key-main_navigation label[data-baseweb=\"radio\"] p')].map(x=>x.innerText),width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,heading_top:document.querySelector('h1')?.getBoundingClientRect().top})")
                if result and result.get("nav") == ["任务总览", "通知导入", "个人设置"] and not result.get("overflow") and 0 <= result.get("heading_top", 999) <= 36:
                    log.write("gui_smoke_passed", **result)
                    return
            except Exception:
                pass
            time.sleep(0.25)
        log.write("gui_smoke_failed", reason="界面布局未在45秒内通过检查")
    finally:
        window.destroy()


def allow_close(server: LocalServer):
    if server.busy():
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, "正在解析通知或保存数据，请等待处理完成后再关闭应用。", TITLE, 0x40)
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校园课程管家 Windows 桌面应用")
    parser.add_argument("--root", type=Path, default=application_root(), help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help="可选数据目录；默认应用旁的data")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--route", help=argparse.SUPPRESS)
    parser.add_argument("--server-log", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.root = args.root.resolve()
    args.data_dir = (args.data_dir or Path(os.environ.get("COURSE_ASSISTANT_DATA_DIR") or args.root / "data")).resolve()
    if args.serve:
        if not args.port or not 1 <= args.port <= 65535 or not args.route or not args.server_log or not args.stop_file:
            return 2
        return serve(args)
    if args.smoke_test and "--data-dir" not in (argv if argv is not None else sys.argv[1:]):
        parser.error("--smoke-test必须明确指定隔离的--data-dir")
    log = None
    try:
        log = DesktopLog(args.data_dir)
        log.write("app_started", pid=os.getpid(), data_dir=str(args.data_dir), version=__version__, smoke_test=args.smoke_test)
        prepare_runtime(args.root, args.data_dir)
        import webview
        if os.name == "nt":
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Lingnan.CampusCourseAssistant")
        server = LocalServer(args.root, args.data_dir, log)
        try:
            server.start()
            window = webview.create_window(TITLE, server.url, width=1200, height=820, min_size=(760, 560),
                                           resizable=True, text_select=True, background_color="#F7F9F8")
            window.events.shown += lambda: log.write("window_shown")
            window.events.closing += lambda: allow_close(server)
            log.write("window_created", renderer="edgechromium")
            webview.start(gui_smoke if args.smoke_test else None, (window, log) if args.smoke_test else None,
                          gui="edgechromium", debug=False, private_mode=True, icon=str(resource_root() / "assets" / "app.ico"))
            log.write("window_closed")
        finally:
            server.stop()
        if args.smoke_test:
            records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
            if not any(row["event"] == "gui_smoke_passed" for row in records):
                return 1
        log.write("app_finished")
        return 0
    except Exception as exc:
        if log:
            log.write("app_failed", error_type=type(exc).__name__)
        message = "应用启动失败。请检查数据文件、WebView2运行环境与桌面运行日志。"
        if log:
            message += "\n\n日志：" + str(log.path) + "\n服务日志：" + str(log.server_log)
        if os.name == "nt" and not args.smoke_test:
            ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)
        return 1
