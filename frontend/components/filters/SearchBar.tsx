"use client";

import { useState, useRef, useEffect } from "react";
import { SearchFilters } from "@/lib/api";
import { format, addDays, parseISO, isAfter, isBefore, isSameDay } from "date-fns";

interface SearchBarProps {
  filters: SearchFilters;
  onChange: (updated: Partial<SearchFilters>) => void;
}

function CalendarMonth({
  year,
  month,
  checkIn,
  checkOut,
  hovered,
  onSelect,
  onHover,
}: {
  year: number;
  month: number;
  checkIn: Date | null;
  checkOut: Date | null;
  hovered: Date | null;
  onSelect: (d: Date) => void;
  onHover: (d: Date | null) => void;
}) {
  const firstDay = new Date(year, month, 1);
  const lastDay = new Date(year, month + 1, 0);
  const startPad = firstDay.getDay();
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const days: (Date | null)[] = Array(startPad).fill(null);
  for (let d = 1; d <= lastDay.getDate(); d++) {
    days.push(new Date(year, month, d));
  }

  const endDate = checkOut ?? hovered;

  const monthName = firstDay.toLocaleString("default", {
    month: "long",
    year: "numeric",
  });

  return (
    <div className="flex-1 min-w-[220px]">
      <p className="text-sm font-semibold text-center mb-3 text-gray-800">
        {monthName}
      </p>
      <div className="grid grid-cols-7 gap-0 text-xs text-center">
        {["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].map((d) => (
          <div key={d} className="text-gray-400 font-medium pb-1.5">
            {d}
          </div>
        ))}
        {days.map((day, idx) => {
          if (!day) return <div key={idx} />;
          const isPast = isBefore(day, today);
          const isStart = checkIn && isSameDay(day, checkIn);
          const isEnd = checkOut && isSameDay(day, checkOut);
          const isInRange =
            checkIn &&
            endDate &&
            isAfter(day, checkIn) &&
            isBefore(day, endDate);

          let cls =
            "h-8 w-8 mx-auto flex items-center justify-center rounded-full text-xs transition-all cursor-pointer ";
          if (isPast) {
            cls += "text-gray-300 cursor-not-allowed";
          } else if (isStart || isEnd) {
            cls += "bg-gray-900 text-white font-semibold";
          } else if (isInRange) {
            cls += "bg-gray-100 text-gray-800 rounded-none";
          } else {
            cls += "hover:bg-gray-100 text-gray-700";
          }

          return (
            <div key={idx} className="py-0.5">
              <div
                className={cls}
                onClick={() => !isPast && onSelect(day)}
                onMouseEnter={() => !isPast && onHover(day)}
                onMouseLeave={() => onHover(null)}
              >
                {day.getDate()}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function SearchBar({ filters, onChange }: SearchBarProps) {
  const [showDates, setShowDates] = useState(false);
  const [showGuests, setShowGuests] = useState(false);
  const [hovered, setHovered] = useState<Date | null>(null);

  const dateRef = useRef<HTMLDivElement>(null);
  const guestRef = useRef<HTMLDivElement>(null);

  const today = new Date();
  const [calMonth, setCalMonth] = useState({ year: today.getFullYear(), month: today.getMonth() });

  const checkIn = filters.check_in ? parseISO(filters.check_in) : null;
  const checkOut = filters.check_out ? parseISO(filters.check_out) : null;

  const adults = filters.adults ?? 2;
  const children = filters.children ?? 0;
  const rooms = filters.rooms ?? 1;

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (dateRef.current && !dateRef.current.contains(e.target as Node))
        setShowDates(false);
      if (guestRef.current && !guestRef.current.contains(e.target as Node))
        setShowGuests(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const handleDateSelect = (day: Date) => {
    if (!checkIn || (checkIn && checkOut)) {
      // Start new selection
      onChange({ check_in: format(day, "yyyy-MM-dd"), check_out: undefined, page: 1 });
    } else {
      // Complete selection
      if (isBefore(day, checkIn)) {
        onChange({ check_in: format(day, "yyyy-MM-dd"), check_out: filters.check_in, page: 1 });
      } else {
        onChange({ check_out: format(day, "yyyy-MM-dd"), page: 1 });
        setShowDates(false);
      }
    }
  };

  const clearDates = () => {
    onChange({ check_in: undefined, check_out: undefined, page: 1 });
    setShowDates(false);
  };

  const nextMonth = { year: calMonth.month === 11 ? calMonth.year + 1 : calMonth.year, month: (calMonth.month + 1) % 12 };

  const guestSummary = `${adults} adult${adults !== 1 ? "s" : ""}${children ? `, ${children} child${children !== 1 ? "ren" : ""}` : ""}, ${rooms} room${rooms !== 1 ? "s" : ""}`;

  const dateSummary = checkIn && checkOut
    ? `${format(checkIn, "MMM d")} – ${format(checkOut, "MMM d")}`
    : checkIn
    ? `${format(checkIn, "MMM d")} – ...`
    : "Add dates";

  return (
    <div className="flex items-center gap-2 relative">
      {/* Date picker */}
      <div ref={dateRef} className="relative">
        <button
          onClick={() => { setShowDates(!showDates); setShowGuests(false); }}
          className={`flex items-center gap-2 px-4 py-2.5 rounded-xl border text-sm font-medium transition-all ${
            showDates
              ? "border-gray-900 bg-white shadow-md"
              : "border-gray-200 bg-white hover:border-gray-400"
          } ${checkIn ? "text-gray-900" : "text-gray-500"}`}
        >
          <svg className="w-4 h-4 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
          {dateSummary}
          {checkIn && (
            <span
              onClick={(e) => { e.stopPropagation(); clearDates(); }}
              className="ml-1 text-gray-400 hover:text-gray-700 text-base leading-none"
            >
              ×
            </span>
          )}
        </button>

        {showDates && (
          <div className="absolute top-full left-0 mt-2 bg-white rounded-2xl shadow-2xl border border-gray-100 p-5 z-50 flex gap-6">
            <div className="flex gap-6">
              <CalendarMonth
                year={calMonth.year}
                month={calMonth.month}
                checkIn={checkIn}
                checkOut={checkOut}
                hovered={hovered}
                onSelect={handleDateSelect}
                onHover={setHovered}
              />
              <CalendarMonth
                year={nextMonth.year}
                month={nextMonth.month}
                checkIn={checkIn}
                checkOut={checkOut}
                hovered={hovered}
                onSelect={handleDateSelect}
                onHover={setHovered}
              />
            </div>
            <div className="flex flex-col justify-between min-w-[100px]">
              <div />
              <div className="flex gap-2">
                <button
                  onClick={() => setCalMonth(({ year, month }) => ({
                    year: month === 0 ? year - 1 : year,
                    month: month === 0 ? 11 : month - 1,
                  }))}
                  className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg hover:bg-gray-50"
                >
                  ←
                </button>
                <button
                  onClick={() => setCalMonth(({ year, month }) => ({
                    year: month === 11 ? year + 1 : year,
                    month: (month + 1) % 12,
                  }))}
                  className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg hover:bg-gray-50"
                >
                  →
                </button>
              </div>
              <button
                onClick={clearDates}
                className="text-xs text-gray-500 underline underline-offset-2 text-center"
              >
                Clear dates
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Guests */}
      <div ref={guestRef} className="relative">
        <button
          onClick={() => { setShowGuests(!showGuests); setShowDates(false); }}
          className={`flex items-center gap-2 px-4 py-2.5 rounded-xl border text-sm font-medium transition-all ${
            showGuests
              ? "border-gray-900 bg-white shadow-md"
              : "border-gray-200 bg-white hover:border-gray-400"
          } text-gray-700`}
        >
          <svg className="w-4 h-4 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
          </svg>
          {guestSummary}
        </button>

        {showGuests && (
          <div className="absolute top-full right-0 mt-2 bg-white rounded-2xl shadow-2xl border border-gray-100 p-5 z-50 w-72">
            {[
              { label: "Adults", sub: "Ages 13+", key: "adults" as const, min: 1 },
              { label: "Children", sub: "Ages 2–12", key: "children" as const, min: 0 },
              { label: "Rooms", sub: "", key: "rooms" as const, min: 1 },
            ].map(({ label, sub, key, min }) => {
              const val = key === "adults" ? adults : key === "children" ? children : rooms;
              return (
                <div key={key} className="flex items-center justify-between py-3 border-b border-gray-50 last:border-0">
                  <div>
                    <p className="text-sm font-medium text-gray-800">{label}</p>
                    {sub && <p className="text-xs text-gray-400">{sub}</p>}
                  </div>
                  <div className="flex items-center gap-3">
                    <button
                      onClick={() => onChange({ [key]: Math.max(min, val - 1), page: 1 })}
                      disabled={val <= min}
                      className="w-8 h-8 rounded-full border border-gray-200 flex items-center justify-center text-gray-600 hover:border-gray-400 disabled:opacity-30 disabled:cursor-not-allowed text-lg leading-none"
                    >
                      −
                    </button>
                    <span className="w-5 text-center text-sm font-semibold">{val}</span>
                    <button
                      onClick={() => onChange({ [key]: val + 1, page: 1 })}
                      className="w-8 h-8 rounded-full border border-gray-200 flex items-center justify-center text-gray-600 hover:border-gray-400 text-lg leading-none"
                    >
                      +
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
