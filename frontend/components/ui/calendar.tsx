"use client";

import { useEffect, useMemo, useState } from "react";

const WEEKDAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

function toDateKey(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function startOfMonth(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function addMonths(date: Date, amount: number) {
  return new Date(date.getFullYear(), date.getMonth() + amount, 1);
}

function buildMonthDays(month: Date) {
  const firstDay = startOfMonth(month);
  const startOffset = firstDay.getDay();
  const daysInMonth = new Date(firstDay.getFullYear(), firstDay.getMonth() + 1, 0).getDate();
  const days: Array<Date | null> = [];

  for (let i = 0; i < startOffset; i += 1) {
    days.push(null);
  }

  for (let day = 1; day <= daysInMonth; day += 1) {
    days.push(new Date(firstDay.getFullYear(), firstDay.getMonth(), day));
  }

  while (days.length % 7 !== 0) {
    days.push(null);
  }

  return days;
}

export function Calendar({
  selected,
  onSelect,
  month,
  onMonthChange,
}: {
  selected: Date | null;
  onSelect: (date: Date) => void;
  month?: Date;
  onMonthChange?: (date: Date) => void;
}) {
  const [internalMonth, setInternalMonth] = useState<Date>(month ?? selected ?? new Date());
  const activeMonth = month ?? internalMonth;

  useEffect(() => {
    if (!month && selected) {
      setInternalMonth(startOfMonth(selected));
    }
  }, [month, selected]);

  const days = useMemo(() => buildMonthDays(activeMonth), [activeMonth]);
  const selectedKey = selected ? toDateKey(selected) : null;

  const changeMonth = (amount: number) => {
    const nextMonth = addMonths(activeMonth, amount);
    if (onMonthChange) {
      onMonthChange(nextMonth);
    } else {
      setInternalMonth(nextMonth);
    }
  };

  return (
    <div className="select-none">
      <div className="flex items-center justify-between gap-2 px-1 py-1">
        <button
          type="button"
          className="rounded-lg px-2 py-1 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900"
          onClick={() => changeMonth(-1)}
          aria-label="Previous month"
        >
          {"<"}
        </button>
        <div className="text-sm font-semibold tracking-tight text-slate-950">
          {new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric" }).format(activeMonth)}
        </div>
        <button
          type="button"
          className="rounded-lg px-2 py-1 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900"
          onClick={() => changeMonth(1)}
          aria-label="Next month"
        >
          {">"}
        </button>
      </div>

      <div className="mt-2 grid grid-cols-7 text-center text-[11px] font-semibold uppercase tracking-[0.2em] text-slate-400">
        {WEEKDAY_LABELS.map((day) => (
          <div key={day} className="py-2">
            {day}
          </div>
        ))}
      </div>

      <div className="grid grid-cols-7 gap-1">
        {days.map((day, index) => {
          if (!day) {
            return <div key={`empty-${index}`} className="h-10" />;
          }

          const isSelected = selectedKey === toDateKey(day);
          const isToday = toDateKey(day) === toDateKey(new Date());

          return (
            <button
              key={toDateKey(day)}
              type="button"
              onClick={() => onSelect(day)}
              className={[
                "flex h-10 items-center justify-center rounded-xl text-sm transition",
                isSelected
                  ? "bg-slate-950 text-white shadow-[0_12px_24px_-16px_rgba(15,23,42,0.9)]"
                  : "text-slate-700 hover:bg-slate-100 hover:text-slate-950",
                isToday && !isSelected ? "ring-1 ring-slate-300" : "",
              ].join(" ")}
            >
              {day.getDate()}
            </button>
          );
        })}
      </div>
    </div>
  );
}
