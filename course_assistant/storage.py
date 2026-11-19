"""JSON 原子持久化、写锁、备份与操作日志（仅使用 Python 标准库）。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator
from uuid import uuid4
import warnings

from .errors import StorageError

DATA_FILES = ("deadlines.json", "courses.json", "config.json")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"非法 JSON 常量：{value}")


class JsonStore:
    def __init__(self, data_dir: Path | str, *, lock_timeout: float = 5.0):
        self.root = Path(data_dir).resolve()
        self.lock_timeout = lock_timeout

    def prepare(self) -> None:
        for directory in (self.root, self.root / "backups", self.root / "logs"):
            directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if name not in (*DATA_FILES, "reminders.json"):
            raise StorageError(f"不允许访问数据文件：{name}")
        return self.root / name

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.prepare()
        path = self.root / ".write.lock"
        limit = time.monotonic() + self.lock_timeout
        while True:
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except (FileExistsError, PermissionError) as exc:
                if time.monotonic() >= limit:
                    raise StorageError(f"数据目录写锁不可用：{path}（被占用或无写入权限）。确认无其他进程使用后，才能手动移除残留锁。") from exc
                time.sleep(0.05)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}))
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            path.unlink(missing_ok=True)

    def read(self, name: str) -> dict:
        path = self._path(name)
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise ValueError("顶层必须是 JSON 对象")
            return value
        except (OSError, UnicodeError, ValueError) as exc:
            raise StorageError(f"无法读取 {path}：{exc}；文件未被覆盖，请检查 backups。") from exc

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _backup(self, name: str) -> Path:
        original = self._path(name)
        folder = self.root / "backups" / original.stem
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = folder / f"{stamp}-{uuid4().hex}.json"
        self._atomic_bytes(destination, original.read_bytes())
        return destination

    def _prune(self, name: str, keep: int) -> None:
        folder = self.root / "backups" / Path(name).stem
        for old in sorted(folder.glob("*.json"), reverse=True)[keep:]:
            old.unlink()

    def write(self, name: str, value: dict, *, keep: int = 20) -> None:
        """调用者必须持有目录锁，并已验证新、旧数据。"""
        path = self._path(name)
        if type(keep) is not int or keep < 1:
            raise StorageError("备份保留数量必须为正整数")
        try:
            payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if path.exists():
                self.read(name)  # 损坏的原文件不进入覆盖流程。
                self._backup(name)  # 备份失败时，不替换原文件。
            self._atomic_bytes(path, payload)
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"保存 {path} 失败，原子替换未完成：{exc}") from exc
        try:
            self._prune(name, keep)
        except OSError as exc:
            warnings.warn(f"数据已保存，但清理旧备份失败：{exc}", RuntimeWarning, stacklevel=2)

    def snapshot(self, *, keep: int) -> list[Path]:
        """调用者持锁；三个文件逐个备份，不声称是跨文件原子事务。"""
        names = [*DATA_FILES, *(["reminders.json"] if (self.root / "reminders.json").exists() else [])]
        paths = [self._backup(name) for name in names]
        for name in names:
            self._prune(name, keep)
        return paths

    def audit(self, action: str, *, task_id: str | None = None, details: dict | None = None) -> None:
        """调用者持锁。日志不包含通知正文；台账内 history 是权威历史。"""
        now = datetime.now(timezone.utc)
        record = {"at": now.isoformat(), "action": action, "task_id": task_id, "details": details or {}}
        path = self.root / "logs" / f"operations-{now:%Y-%m}.jsonl"
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # 已提交的台账不能因辅助日志故障被误报成“操作未发生”。
            warnings.warn(f"操作已保存，但写入日志失败：{exc}", RuntimeWarning, stacklevel=2)
