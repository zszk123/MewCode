"""Cron 表达式解析与触发时间计算。

支持标准 5 字段 cron 表达式：
  minute hour day-of-month month day-of-week

字段规则：
- * : 匹配所有值
- */N : 每 N 个单位
- N : 精确值
- N-M : 范围
- N,M,O : 列表
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Any


class CronParseError(Exception):
    """Cron 表达式解析错误。"""
    pass


# 各字段的取值范围
_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day_of_month": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 6),  # 0=Sunday, 6=Saturday
}

# 月份天数
_MONTH_DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _days_in_month(year: int, month: int) -> int:
    """返回指定年月的天数。"""
    if month == 2 and calendar.isleap(year):
        return 29
    return _MONTH_DAYS[month]


class CronExpression:
    """标准 5 字段 cron 表达式。"""

    def __init__(
        self,
        minutes: set[int],
        hours: set[int],
        days_of_month: set[int],
        months: set[int],
        days_of_week: set[int],
    ) -> None:
        self.minutes = minutes
        self.hours = hours
        self.days_of_month = days_of_month
        self.months = months
        self.days_of_week = days_of_week

    @classmethod
    def parse(cls, expr: str) -> CronExpression:
        """解析 cron 表达式字符串。

        Args:
            expr: 5 字段 cron 表达式，如 "0 9 * * 1-5"

        Returns:
            CronExpression 实例。

        Raises:
            CronParseError: 表达式格式错误。
        """
        parts = expr.strip().split()
        if len(parts) != 5:
            raise CronParseError(
                f"Cron expression must have 5 fields, got {len(parts)}: '{expr}'"
            )

        field_names = ["minute", "hour", "day_of_month", "month", "day_of_week"]

        parsed: dict[str, set[int]] = {}
        for name, value in zip(field_names, parts):
            parsed[name] = cls._parse_field(value, name)

        return cls(
            minutes=parsed["minute"],
            hours=parsed["hour"],
            days_of_month=parsed["day_of_month"],
            months=parsed["month"],
            days_of_week=parsed["day_of_week"],
        )

    @staticmethod
    def _parse_field(value: str, field_name: str) -> set[int]:
        """解析单个 cron 字段。"""
        low, high = _FIELD_RANGES[field_name]
        results: set[int] = set()

        # 处理逗号分隔的多个值
        for part in value.split(","):
            part = part.strip()

            # */N 格式
            if part.startswith("*/"):
                try:
                    step = int(part[2:])
                except ValueError:
                    raise CronParseError(
                        f"Invalid step value in '{part}' for field '{field_name}'"
                    )
                if step < 1:
                    raise CronParseError(
                        f"Step must be >= 1, got {step} in '{part}'"
                    )
                for v in range(low, high + 1, step):
                    results.add(v)
                continue

            # N-M 范围格式
            if "-" in part:
                try:
                    r_low, r_high = part.split("-")
                    r_low_int = int(r_low)
                    r_high_int = int(r_high)
                except ValueError:
                    raise CronParseError(
                        f"Invalid range in '{part}' for field '{field_name}'"
                    )
                if r_low_int < low or r_high_int > high:
                    raise CronParseError(
                        f"Range {r_low_int}-{r_high_int} out of bounds "
                        f"[{low}, {high}] for field '{field_name}'"
                    )
                for v in range(r_low_int, r_high_int + 1):
                    results.add(v)
                continue

            # * 通配符
            if part == "*":
                for v in range(low, high + 1):
                    results.add(v)
                continue

            # 单个数字
            try:
                v = int(part)
            except ValueError:
                raise CronParseError(
                    f"Invalid value '{part}' for field '{field_name}'"
                )
            if v < low or v > high:
                raise CronParseError(
                    f"Value {v} out of bounds [{low}, {high}] for field '{field_name}'"
                )
            results.add(v)

        return results

    def next_fire(self, after: datetime | None = None) -> datetime | None:
        """计算下一次触发时间。

        Args:
            after: 起始时间（None = 当前时间）。

        Returns:
            下一次触发时间，或 None（永远不触发）。
        """
        if after is None:
            after = datetime.now(timezone.utc)

        # 确保 naive datetime 转为 UTC aware
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)

        # 从 after 的下一个分钟开始搜索
        current = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # 最多搜索 4 年（防止无限循环）
        max_iterations = 4 * 366 * 24 * 60
        for _ in range(max_iterations):
            if (
                current.month in self.months
                and current.day in self.days_of_month
                and current.weekday() in self.days_of_week  # 需转换：Python weekday() 0=Mon
                and current.hour in self.hours
                and current.minute in self.minutes
            ):
                return current

            current += timedelta(minutes=1)

        return None

    def validate(self) -> bool:
        """验证表达式是否有效（是否有至少一个匹配的时间）。"""
        # 基本验证：所有字段非空
        if not all([
            self.minutes, self.hours, self.days_of_month, self.months, self.days_of_week,
        ]):
            return False

        # 尝试找到下一个触发时间
        try:
            return self.next_fire() is not None
        except Exception:
            return False

    def __repr__(self) -> str:
        return (
            f"CronExpression(minutes={sorted(self.minutes)[:3]}..., "
            f"hours={sorted(self.hours)[:3]}..., "
            f"dom={sorted(self.days_of_month)[:3]}..., "
            f"months={sorted(self.months)}, "
            f"dow={sorted(self.days_of_week)})"
        )


# ---------------------------------------------------------------------------
# weekday 转换工具
# ---------------------------------------------------------------------------

# Python datetime.weekday(): 0=Monday ... 6=Sunday
# Cron day_of_week:      0=Sunday ... 6=Saturday
# 这里我们需要在 next_fire 中正确处理
# 实际上 Python weekday() 0=Mon, cron dow 0=Sun
# 所以需要转换

# 覆盖 monkey-patch: 在 CronExpression 类中正确处理 weekday
# 重新定义 next_fire 中的 weekday 检查

def _cron_weekday_matches(dt: datetime, cron_dows: set[int]) -> bool:
    """检查 datetime 的星期几是否匹配 cron day_of_week。

    Cron DOW: 0=Sunday, 1=Monday, ..., 6=Saturday
    Python weekday(): 0=Monday, ..., 6=Sunday
    Python isoweekday(): 1=Monday, ..., 7=Sunday
    """
    # 将 Python weekday 转为 cron DOW
    py_wd = dt.weekday()  # 0=Mon ... 6=Sun
    cron_wd = (py_wd + 1) % 7  # 0=Sun ... 6=Sat
    return cron_wd in cron_dows


# 替换 next_fire 方法，使其使用正确的 weekday 转换
def _patched_next_fire(self: CronExpression, after: datetime | None = None) -> datetime | None:
    """计算下一次触发时间（修正 weekday 处理）。"""
    if after is None:
        after = datetime.now(timezone.utc)

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)

    current = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    max_iterations = 4 * 366 * 24 * 60
    for _ in range(max_iterations):
        if (
            current.month in self.months
            and current.day in self.days_of_month
            and _cron_weekday_matches(current, self.days_of_week)
            and current.hour in self.hours
            and current.minute in self.minutes
        ):
            return current

        current += timedelta(minutes=1)

    return None


# 应用 patch
CronExpression.next_fire = _patched_next_fire
