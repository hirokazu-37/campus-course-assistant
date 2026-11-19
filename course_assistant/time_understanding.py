"""确定性时间计算：只计算模型已定位且能在原文验证的时间表达。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re

from .errors import ModelError, ValidationError
from .deadline_policy import default_clock_note, validate_default_time
from .models import Deadline

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_NUM = "零〇一二两三四五六七八九十"
_WEEK = r"(下下|下|本|这|上)?(?:周|星期|礼拜)([一二三四五六日天1-7])"


def chinese_number(text: str) -> int:
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if text.count("十") == 1:
        tens, ones = text.split("十")
        return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    return digits[text]


def explicit_clock(expression: str) -> str | None:
    """没有明确时段的 1～12 点、晚上12点、模糊时间一律不补全。"""
    if re.search(r"或|左右|大概|大约|之前再定|待定", expression):
        return None
    clocks = list(re.finditer(r"(?<![0-9])([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?(?![0-9])", expression))
    if len(clocks) == 1:
        hour, minute, second = clocks[0].groups()
        if 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59 and (not second or 0 <= int(second) <= 59):
            prefix = expression[:clocks[0].start()].rstrip()
            period = re.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|晚)$", prefix)
            normalized = _period_hour(int(hour), period.group(1) if period else None, colon=True)
            return None if normalized is None else f"{normalized:02}:{minute}" + (f":{second}" if second else "")
        raise ModelError("模型定位的钟点超出合法范围")
    if clocks:
        return None
    pattern = rf"(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|晚)?\s*([0-9{_NUM}]+)(?:点|时)(半|[0-9{_NUM}]+分?)?"
    matches = list(re.finditer(pattern, expression))
    if len(matches) != 1:
        return None
    period, hour_text, minute_text = matches[0].groups()
    try:
        hour = chinese_number(hour_text)
        minute = 30 if minute_text == "半" else chinese_number(minute_text.rstrip("分")) if minute_text else 0
    except (ValueError, KeyError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ModelError("模型定位的钟点超出合法范围")
    hour = _period_hour(hour, period)
    return None if hour is None else f"{hour:02}:{minute:02}"


def _period_hour(hour: int, period: str | None, *, colon: bool = False) -> int | None:
    if period in ("下午", "傍晚", "晚上", "今晚", "晚"):
        if hour == 12 and period in ("晚上", "今晚", "晚"):
            return None  # 日期交界语义需本人确认。
        if 1 <= hour < 12:
            hour += 12
    elif period == "中午":
        if not 11 <= hour <= 13:
            return None
    elif period in ("凌晨", "早上", "上午"):
        if hour >= 12:
            return None
    elif not colon and 1 <= hour <= 12:
        return None
    return hour


def _absolute_dates(expression: str) -> list[date]:
    matches = re.findall(r"(?<![0-9])([0-9]{4})\s*[年./-]\s*([0-9]{1,2})\s*[月./-]\s*([0-9]{1,2})(?:日|号)?(?![0-9])", expression)
    try:
        return [date(int(year), int(month), int(day)) for year, month, day in matches]
    except ValueError:
        raise ModelError("通知中的明确日期不合法，请核实原通知") from None


def deadline_default_year(sent_at: date | datetime | None) -> int:
    """省略年份的截止月日按消息年份理解；无消息日期时按上海当前年。"""
    if isinstance(sent_at, datetime):
        return sent_at.astimezone(SHANGHAI).year
    return sent_at.year if sent_at is not None else datetime.now(SHANGHAI).year


def _month_days(expression: str) -> set[tuple[int, int]]:
    pairs = re.findall(r"(?<![0-9])([0-9]{1,2})\s*月\s*([0-9]{1,2})(?:日|号)?(?![0-9])|(?<![0-9])([0-9]{1,2})[./-]([0-9]{1,2})(?![0-9])", expression)
    return {(int(month or numeric_month), int(day or numeric_day)) for month, day, numeric_month, numeric_day in pairs}


def resolve_deadline(spec: dict, raw: str, sent_at: date | datetime | None, semester_monday: date | None = None,
                     *, default_year: int | None = None, default_time: str | None = None) -> tuple[Deadline, list[str]]:
    notes: list[str] = []
    assumptions: list[str] = []
    expression = spec["expression"] or ""
    clock_expression = spec["time_expression"] or ""
    for quote in (expression, clock_expression):
        if quote and quote not in raw:
            raise ModelError("时间证据不是通知原文的片段，已拒绝解析结果")
    kind = spec["kind"]
    active = {"absolute": {"date"}, "relative_days": {"days"}, "week_day": {"week_offset", "weekday"},
              "semester_week": {"semester_week", "weekday"}, "unknown": set()}[kind]
    for key in {"date", "days", "week_offset", "weekday", "semester_week"} - active:
        if spec[key] is not None:
            raise ModelError("模型时间字段相互冲突，已拒绝解析结果")
    if kind != "unknown" and not expression:
        raise ModelError("模型未提供截止日期的原文证据")
    for key, lower, upper in (("days", -365, 365), ("week_offset", -52, 52), ("weekday", 1, 7), ("semester_week", 1, 60)):
        if spec[key] is not None and not lower <= spec[key] <= upper:
            raise ModelError("模型相对日期参数超出合法范围")
    day: date | None = None
    try:
        if re.search(r"或|待定|可能|大概|左右|另行通知", expression):
            notes.append("截止日期表述存在歧义，请核实原通知")
        elif kind == "absolute":
            dates = _absolute_dates(expression)
            month_days = _month_days(expression)
            if len(dates) == 1:
                proposed = date.fromisoformat(spec["date"]) if spec["date"] else None
                if proposed != dates[0]:
                    raise ModelError("模型给出的绝对日期与原文不一致")
                if len(month_days) > 1:
                    notes.append("截止表达包含多个不同月日，需核实唯一日期")
                else:
                    day = dates[0]
            elif not dates and len(month_days) == 1:
                year = deadline_default_year(sent_at) if sent_at is not None or default_year is None else default_year
                if type(year) is not int or not 1 <= year <= 9999:
                    raise ModelError("默认截止年份不合法")
                qualifiers = set(re.findall(r"今年|明年|后年|去年|前年", expression))
                if len(qualifiers) > 1:
                    notes.append("截止年份表述冲突，需核实")
                else:
                    qualifier = next(iter(qualifiers), None)
                    offset = {None: 0, "今年": 0, "明年": 1, "后年": 2, "去年": -1, "前年": -2}[qualifier]
                    month, number = next(iter(month_days))
                    day = date(year + offset, month, number)
                    if spec["date"] is not None and date.fromisoformat(spec["date"]) != day:
                        raise ModelError("模型补充的年份与消息年份/默认今年规则不一致")
                    source = "消息发送年份" if sent_at is not None else "当前上海年份"
                    assumptions.append(f"原文未写完整年份，按{source}{year}年" + (f"及“{qualifier}”" if qualifier else "") + f"理解为 {day.isoformat()}")
                    base = sent_at.astimezone(SHANGHAI).date() if isinstance(sent_at, datetime) else sent_at
                    if base is not None and not qualifier and day < base:
                        notes.append("按消息年份得到的截止日期早于发送日期，可能涉及跨年，请核对；未擅自改为明年")
            else:
                notes.append("未找到唯一的截止月日，需核实日期")
        elif kind != "unknown" and sent_at is None:
            notes.append("缺少消息实际发送时间，相对日期未计算；不能用录入时间代替")
        elif kind == "relative_days":
            mapping = {"今天": 0, "今日": 0, "今晚": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
            tokens = re.findall(rf"大后天|后天|明天|明日|今天|今日|今晚|[0-9{_NUM}]+天后", expression)
            offsets = []
            for token in tokens:
                offsets.append(mapping[token] if token in mapping else chinese_number(token[:-2]))
            if len(set(offsets)) != 1:
                notes.append("相对天数无法唯一确认")
            elif spec["days"] != offsets[0]:
                raise ModelError("模型的相对天数与原文不一致")
            else:
                base = sent_at.astimezone(SHANGHAI).date() if isinstance(sent_at, datetime) else sent_at
                day = base + timedelta(days=offsets[0])
        elif kind == "week_day":
            matches = re.findall(_WEEK, expression)
            if len(matches) != 1 or not matches[0][0]:
                notes.append("未明确本周/下周，星期对应的日期待确认")
            else:
                prefix, weekday = matches[0]
                offset = {"本": 0, "这": 0, "下": 1, "下下": 2, "上": -1}[prefix]
                number = int(weekday) if weekday.isdigit() else {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}[weekday]
                if (offset, number) != (spec["week_offset"], spec["weekday"]):
                    raise ModelError("模型的周偏移/星期与原文不一致")
                base = sent_at.astimezone(SHANGHAI).date() if isinstance(sent_at, datetime) else sent_at
                day = base - timedelta(days=base.weekday()) + timedelta(weeks=offset, days=number - 1)
        elif kind == "semester_week":
            weeks = re.findall(rf"第([0-9{_NUM}]+)周", expression)
            if len(weeks) != 1 or chinese_number(weeks[0]) != spec["semester_week"]:
                raise ModelError("模型的教学周与原文不一致")
            weekdays = re.findall(_WEEK, expression)
            # “第14周”中的“周”不是星期；带明确“周三/星期三”才有具体日。
            weekday = spec["weekday"]
            if weekday is not None:
                if len(weekdays) != 1:
                    raise ModelError("模型自行补全了教学周中的星期")
                token = weekdays[0][1]
                actual = int(token) if token.isdigit() else {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}[token]
                if weekday != actual:
                    raise ModelError("教学周星期与原文不一致")
            if semester_monday is None:
                notes.append("缺少真实校历的第1周周一，教学周日期待确认")
            else:
                start = semester_monday + timedelta(weeks=spec["semester_week"] - 1)
                if weekday is None:
                    notes.append(f"校历第{spec['semester_week']}周范围为 {start} 至 {start + timedelta(days=6)}；未明确具体日")
                else:
                    day = start + timedelta(days=weekday - 1)
        else:
            notes.append("未找到唯一、明确的截止日期")
        clock = None
        if spec["time"] is not None:
            if not clock_expression:
                raise ModelError("模型给出了没有原文证据的钟点")
            actual_clock = explicit_clock(clock_expression)
            if actual_clock is None:
                notes.append("钟点含糊，未自行补全；请核实具体时间")
            elif actual_clock != spec["time"]:
                raise ModelError("模型给出的钟点与原文不一致")
            else:
                clock = actual_clock
        elif clock_expression:
            notes.append("通知的时间表达未转换为确定钟点，需人工确认")
        if day is None:
            return Deadline(), [*assumptions, *notes]
        if clock is None:
            if default_time is not None and not clock_expression and spec["time"] is None and not notes:
                validate_default_time(default_time)
                # 防止模型遗漏紧邻日期的原文明示时段/钟点后，将其误当“只写日期”。
                contexts = [raw[max(0, match.start() - 3):match.end() + 14] for match in re.finditer(re.escape(expression), raw)]
                has_clock_cue = any(re.search(r"上午|下午|晚上|中午|凌晨|早上|傍晚|[0-9零〇一二两三四五六七八九十]+(?:点|时)|[0-9]{1,2}[:：][0-9]{2}", text) for text in contexts)
                if not has_clock_cue:
                    return Deadline.from_input(day.isoformat() + "T" + default_time), [*assumptions, default_clock_note(default_time)]
            notes.append("只有截止日期，尚无确定钟点")
            return Deadline.from_input(day.isoformat()), [*assumptions, *notes]
        return Deadline.from_input(f"{day.isoformat()}T{clock}", needs_confirmation=spec["uncertain"] or bool(notes)), [*assumptions, *notes]
    except (ValueError, TypeError, KeyError, OverflowError, ValidationError):
        raise ModelError("模型日期或钟点不合法，已拒绝解析结果") from None
