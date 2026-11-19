"""持久化模型结果缓存：同内容/时间/上下文命中不访问网络。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import unicodedata

from .errors import ModelError, StorageError
from datetime import datetime

from .models import Notice, notice_time
from .schema import load_json, validate_schema
from .storage import JsonStore


def digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("\r\n", "\n").strip()


def notice_id(notice: Notice) -> str:
    point = notice_time(notice.sent_at)
    # 时间戳保留原指纹算法；日期单独标记，不能与该日00:00混同。
    sent = point.timestamp() if isinstance(point, datetime) else "date:" + point.isoformat() if point else None
    return digest({"text": normalized(notice.raw_notice), "sent_at": sent})


class CachedClient:
    def __init__(self, data_dir: Path, factory, schema: dict, *, stage: str, refresh: bool = False):
        self.root = Path(data_dir) / "cache" / stage
        self.factory, self.schema, self.refresh = factory, schema, refresh

    def complete(self, system_prompt: str, payload: dict) -> dict:
        key = digest({"version": 3, "prompt": system_prompt, "payload": payload, "schema": self.schema})
        path = self.root / f"{key}.json"
        # 单独的缓存锁避免并发相同输入重复付费，不持有业务台账写锁等待网络。
        with JsonStore(self.root).lock():
            if path.exists() and not self.refresh:
                try:
                    saved = load_json(path.read_text(encoding="utf-8"))
                    if set(saved) != {"key", "output"} or saved["key"] != key:
                        raise ValueError
                    validate_schema(saved["output"], self.schema)
                    return saved["output"]
                except (ValueError, TypeError, ModelError) as exc:
                    raise StorageError("模型缓存损坏；未重试付费调用。请检查缓存或显式使用 --refresh。") from None
            output = self.factory().complete(system_prompt, payload)
            validate_schema(output, self.schema)
            JsonStore._atomic_bytes(path, (json.dumps({"key": key, "output": output}, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
            return output
