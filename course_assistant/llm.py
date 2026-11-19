"""OpenAI 兼容 Chat Completions / json_schema；标准库 HTTP，无自动重试。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import http.client
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from .errors import ModelError
from .schema import PARSE_SCHEMA, load_json, validate_schema


@dataclass(frozen=True)
class ApiSettings:
    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 60.0
    response_mode: str = "json_schema"

    def __post_init__(self):
        if not self.api_key or any(ord(c) < 32 or ord(c) > 126 for c in self.api_key):
            raise ModelError("请配置有效的 OPENAI_API_KEY（不会显示密钥）")
        if not self.model.strip() or len(self.model) > 200:
            raise ModelError("请通过 OPENAI_MODEL 配置支持结构化 JSON 输出的模型")
        if self.response_mode not in ("json_schema", "json_object"):
            raise ModelError("OPENAI_RESPONSE_MODE 必须为 json_schema 或 json_object")
        try:
            url = urllib.parse.urlsplit(self.base_url)
            url.port
        except ValueError:
            raise ModelError("OPENAI_BASE_URL 地址格式不合法") from None
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ModelError("OPENAI_BASE_URL 必须是无凭据、查询参数和片段的 API 基础地址")
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ModelError("API 必须使用 HTTPS；仅本机测试服务允许 HTTP")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ModelError("OPENAI_TIMEOUT_SECONDS 必须大于0且不超过300秒")

    @classmethod
    def from_env(cls) -> ApiSettings:
        try:
            timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60"))
        except ValueError:
            raise ModelError("OPENAI_TIMEOUT_SECONDS 必须是数字") from None
        return cls(os.environ.get("OPENAI_API_KEY", "").strip(), os.environ.get("OPENAI_MODEL", "").strip(),
                   os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"), timeout,
                   os.environ.get("OPENAI_RESPONSE_MODE", "json_schema").strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不允许重定向把 Authorization 发送到另一个主机。


class StructuredClient:
    def __init__(self, settings: ApiSettings, *, schema: dict = PARSE_SCHEMA, schema_name: str = "course_notice"):
        self.settings = settings
        self.schema, self.schema_name = schema, schema_name

    def complete(self, system_prompt: str, payload: dict) -> dict:
        response_format = {"type": "json_schema", "json_schema": {"name": self.schema_name, "strict": True, "schema": self.schema}}
        if self.settings.response_mode == "json_object":
            # 明确配置的兼容路径，不自动降级；JSON模式仍须通过完全相同的本地Schema校验。
            response_format = {"type": "json_object"}
            if self.schema_name == "notice_relation":
                example = {"kind": "uncertain", "target_ids": [], "reason": "信息不足", "evidence": "原文片段", "changes": []}
            elif self.schema_name == "message_time":
                example = {"date": None, "time": None, "evidence": "", "uncertain": True, "reason": "缺少发送日期证据"}
            else:
                example = {"classification": "non_task", "tasks": [], "warnings": []}
            system_prompt += "\n仅输出一个JSON对象，不输出Markdown。严格遵守以下JSON Schema：\n" + json.dumps(self.schema, ensure_ascii=False)
            system_prompt += "\n以下仅为结构示意，不能当作通知内容或照抄证据：\n" + json.dumps(example, ensure_ascii=False)
        body = {"model": self.settings.model,
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "response_format": response_format}
        if self.settings.response_mode == "json_object": body["max_tokens"] = 4096
        request = urllib.request.Request(self.settings.base_url.rstrip("/") + "/chat/completions",
                                         data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                                         headers={"Authorization": "Bearer " + self.settings.api_key, "Content-Type": "application/json"}, method="POST")
        try:
            handlers = [_NoRedirect()]
            if urllib.parse.urlsplit(self.settings.base_url).hostname in ("localhost", "127.0.0.1", "::1"):
                handlers.append(urllib.request.ProxyHandler({}))
            with urllib.request.build_opener(*handlers).open(request, timeout=self.settings.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ModelError("API 返回内容过大，已拒绝处理")
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            hints = {401: "认证失败，请检查 API Key", 403: "接口拒绝访问", 429: "额度不足或请求过于频繁，请稍后重试",
                     400: "请求被拒绝，请核对模型和输出模式；DeepSeek按官方JSON模式显式设置 OPENAI_RESPONSE_MODE=json_object"}
            raise ModelError(f"API HTTP {status}：{hints.get(status, '服务异常，请检查接口配置或稍后重试')}（未记录响应正文）") from None
        except (TimeoutError, socket.timeout):
            raise ModelError("API 请求超时；没有写入任务，可稍后重试") from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            raise ModelError("API 连接失败；请检查网络、证书和接口地址（详情已隐藏）") from None
        try:
            envelope = load_json(raw.decode("utf-8"))
            choice = envelope["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise ModelError("模型拒绝解析该通知；没有写入任务")
            if choice["finish_reason"] != "stop":
                raise ModelError("模型输出被截断或未正常结束；没有写入任务")
            if message.get("tool_calls") or not isinstance(message["content"], str):
                raise ModelError("API 返回了非文本结构化结果；已拒绝处理")
            output = load_json(message["content"])
            validate_schema(output, self.schema)
            return output
        except (KeyError, IndexError, TypeError, AttributeError, UnicodeError):
            raise ModelError("API 响应格式异常；没有写入任务") from None
