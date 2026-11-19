"""API JSON Schema 及其严格本地校验器；不把 JSON 模式当成语义正确保证。"""

from __future__ import annotations

import json
from typing import Any

from .errors import ModelError


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
NULL_STRING = {"type": ["string", "null"]}
NULL_INT = {"type": ["integer", "null"]}
REVIEW_FIELDS = ["course", "title", "description", "deadline", "submission.method", "submission.format_requirements"]
DEADLINE_SCHEMA = obj({
    "kind": {"type": "string", "enum": ["absolute", "relative_days", "week_day", "semester_week", "unknown"]},
    "expression": NULL_STRING,
    "date": NULL_STRING,
    "days": NULL_INT,
    "week_offset": NULL_INT,
    "weekday": NULL_INT,
    "semester_week": NULL_INT,
    "time": NULL_STRING,
    "time_expression": NULL_STRING,
    "uncertain": {"type": "boolean"},
})
TASK_SCHEMA = obj({
    "course": NULL_STRING, "course_evidence": STRING,
    "title": STRING, "description": STRING, "task_evidence": STRING,
    "deadline": DEADLINE_SCHEMA,
    "submission_method": NULL_STRING, "submission_evidence": STRING,
    "format_requirements": NULL_STRING, "format_evidence": STRING,
    "uncertain_fields": {"type": "array", "items": {"type": "string", "enum": REVIEW_FIELDS}},
    "notes": {"type": "array", "items": STRING},
})
PARSE_SCHEMA = obj({
    "classification": {"type": "string", "enum": ["tasks", "non_task"]},
    "tasks": {"type": "array", "items": TASK_SCHEMA},
    "warnings": {"type": "array", "items": STRING},
})


def validate_schema(value: Any, schema: dict = PARSE_SCHEMA, path: str = "$") -> None:
    """覆盖本模块 Schema 使用的全部关键字；未知类型/多余键均拒绝。"""
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if not any(type(value) is types[kind] for kind in kinds):
        raise ModelError(f"模型输出校验失败：{path} 类型不正确")
    if "enum" in schema and value not in schema["enum"]:
        raise ModelError(f"模型输出校验失败：{path} 枚举值不合法")
    if type(value) is dict:
        if set(value) != set(schema["required"]):
            raise ModelError(f"模型输出校验失败：{path} 字段缺失或多余")
        for key, child in value.items():
            validate_schema(child, schema["properties"][key], f"{path}.{key}")
    elif type(value) is list:
        if len(value) > 100:
            raise ModelError(f"模型输出校验失败：{path} 数组过长")
        for index, child in enumerate(value):
            validate_schema(child, schema["items"], f"{path}[{index}]")
    elif type(value) is str and len(value) > 20000:
        raise ModelError(f"模型输出校验失败：{path} 字符串过长")


def load_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("non-finite number")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ModelError("模型返回的内容不是严格合法的 JSON") from None
