"""Reporting periods. Weeks run Monday to Sunday (ISO, as used in Ireland); "today" is the
current date in Ireland, so a report generated on Monday morning covers the week just ended."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IRELAND = ZoneInfo("Europe/Dublin")
PERIODS = ("last_week", "this_week", "last_month", "this_month", "custom")
MAX_REPORT_DAYS = 366


class ReportPeriodError(ValueError):
    pass


@dataclass(frozen=True)
class ReportPeriod:
    key: str
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> "ReportPeriod":
        """The period of the same length immediately before this one (for comparisons)."""
        if self.key in ("last_month", "this_month"):
            end = self.start - timedelta(days=1)
            start = end.replace(day=1)
            return ReportPeriod("previous", start, end, "previous month")
        end = self.start - timedelta(days=1)
        return ReportPeriod("previous", end - timedelta(days=self.days - 1), end, "previous period")


def irish_today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(IRELAND).date()


def resolve_period(key: Optional[str], date_from: Optional[str] = None, date_to: Optional[str] = None, today: Optional[date] = None) -> ReportPeriod:
    key = (key or "last_week").strip().lower()
    today = today or irish_today()
    monday = today - timedelta(days=today.weekday())
    if key == "last_week":
        return ReportPeriod(key, monday - timedelta(days=7), monday - timedelta(days=1), "Last week")
    if key == "this_week":
        return ReportPeriod(key, monday, today, "This week to date")
    if key == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return ReportPeriod(key, end.replace(day=1), end, "Last month")
    if key == "this_month":
        return ReportPeriod(key, today.replace(day=1), today, "This month to date")
    if key == "custom":
        try:
            start = date.fromisoformat(date_from or "")
            end = date.fromisoformat(date_to or "")
        except ValueError:
            raise ReportPeriodError("A custom period needs date_from and date_to in the YYYY-MM-DD format")
        if start > end:
            raise ReportPeriodError("The start date must be on or before the end date")
        if (end - start).days + 1 > MAX_REPORT_DAYS:
            raise ReportPeriodError("A report can cover at most one year")
        return ReportPeriod(key, start, end, "Custom period")
    raise ReportPeriodError(f"Unknown period; use one of: {', '.join(PERIODS)}")
