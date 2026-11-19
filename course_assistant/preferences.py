"""可向后兼容的个人提醒偏好，禁止未知字段和非法类型。"""
from copy import deepcopy
from datetime import time

from .errors import ValidationError
from .models import exact_keys

DEFAULT_REMINDERS = {"lead_days": [3, 1], "daily_check_time": "09:00", "channel": "auto", "course_overrides": {}}


def validate_days(value):
    if not isinstance(value, list) or not value or any(type(day) is not int or not 0 <= day <= 365 for day in value) or len(set(value)) != len(value):
        raise ValidationError("lead_days 必须是不重复的0～365整数列表，至少一项")
    return sorted(value, reverse=True)


def validate_preferences(value):
    exact_keys(value, set(DEFAULT_REMINDERS), "reminder_preferences")
    days = validate_days(value["lead_days"])
    try:
        clock = time.fromisoformat(value["daily_check_time"])
        if clock.tzinfo or clock.isoformat(timespec="minutes") != value["daily_check_time"]:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError("daily_check_time 必须为 HH:MM") from None
    if value["channel"] not in ("auto", "terminal"):
        raise ValidationError("提醒渠道必须为 auto 或 terminal")
    overrides = value["course_overrides"]
    if not isinstance(overrides, dict):
        raise ValidationError("course_overrides 必须为对象")
    for course, rule in overrides.items():
        if not isinstance(course, str) or not course.strip() or course.strip() != course:
            raise ValidationError("课程提醒配置名称必须为非空、无首尾空白字符串")
        exact_keys(rule, {"enabled", "lead_days"}, "course reminder")
        if type(rule["enabled"]) is not bool:
            raise ValidationError("课程提醒 enabled 必须为布尔值")
        validate_days(rule["lead_days"])
    return {**deepcopy(value), "lead_days": days}
