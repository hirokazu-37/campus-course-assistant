"""Windows 原生托盘桌面通知；所有内容通过 stdin JSON 传输，不拼入脚本。"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess

IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Delivery:
    channel: str
    api_submitted: bool = False
    display_event: bool = False
    user_visible: bool | None = None
    fallback_reason: str = ""


class Notifier:
    def __init__(self, channel: str = "auto"):
        self.channel = channel

    def send(self, title: str, message: str) -> Delivery:
        # 终端与运行日志始终有可读结果；桌面通知只是额外展示渠道。
        print(title + "\n" + message)
        if self.channel == "terminal": return Delivery("terminal")
        if not IS_WINDOWS: return Delivery("terminal", fallback_reason="当前系统不是 Windows")
        script = Path(__file__).resolve().parent.parent / "scripts" / "show_notification.ps1"
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        try:
            result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                    input=json.dumps({"title": title[:60], "message": message[:600]}, ensure_ascii=False), encoding="utf-8",
                                    capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            response = json.loads(result.stdout.strip().lstrip("\ufeff"))
            if not isinstance(response, dict): raise ValueError("Invalid notification response")
            submitted = response.get("api_submitted") is True
            observed = response.get("balloon_shown_event") is True
            if result.returncode == 0 and submitted and observed:
                return Delivery("windows_desktop", True, True)
            return Delivery("terminal", submitted, observed, fallback_reason="桌面未报告显示或不支持交互通知；使用终端和日志")
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            return Delivery("terminal", fallback_reason="桌面通知失败/超时；使用终端和日志")
