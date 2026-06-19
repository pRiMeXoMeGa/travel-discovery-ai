"use client";

import { useState } from "react";
import { nlSearch, SearchFilters } from "@/lib/api";
import { currencySymbol } from "@/lib/currency";

interface Props {
  onApply: (filters: Partial<SearchFilters>) => void;
}

/** Turn the parsed StructuredQuery into short human-readable tags ("what we understood"). */
function summarize(u: Record<string, unknown>): string[] {
  const tags: string[] = [];
  if (u.city) tags.push(String(u.city));
  if (u.vibe) tags.push(String(u.vibe));
  if (u.check_in && u.check_out) tags.push(`${u.check_in} → ${u.check_out}`);
  if (u.party_size) tags.push(`${u.party_size} guests`);
  const cs = currencySymbol(u.city as string | undefined);
  if (u.budget_per_night) tags.push(`≤ ${cs}${u.budget_per_night}/night`);
  if (u.budget_total) tags.push(`≤ ${cs}${u.budget_total} total`);
  for (const c of (u.hard_constraints as string[]) ?? []) tags.push(c);
  for (const c of (u.soft_preferences as string[]) ?? []) tags.push(c);
  return tags;
}

const EXAMPLES = [
  "a quiet 1-bed in Lisbon under 130 with a balcony for late June",
  "family-friendly place in Amsterdam with a kitchen and washer under 200",
];

export function NlSearchBar({ onApply }: Props) {
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [understood, setUnderstood] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(e?: React.FormEvent) {
    e?.preventDefault();
    const query = q.trim();
    if (!query || loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await nlSearch(query);
      onApply({ ...res.filters, page: 1 });
      setUnderstood(summarize(res.understanding));
    } catch {
      setError("Couldn't parse that — try rephrasing.");
      setUnderstood(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="w-full">
      <form onSubmit={submit} className="relative">
        <svg
          className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#e61e4d]"
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
        </svg>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Describe your trip — e.g. “a quiet 1-bed in Lisbon under 130 with a balcony”"
          className="w-full pl-10 pr-24 py-2.5 rounded-xl border border-gray-200 bg-white text-sm
                     focus:border-[#e61e4d] focus:ring-2 focus:ring-[#fff0f3] outline-none transition-all"
        />
        <button
          type="submit"
          disabled={loading || !q.trim()}
          className="absolute right-1.5 top-1/2 -translate-y-1/2 px-3.5 py-1.5 rounded-lg bg-[#e61e4d]
                     text-white text-sm font-medium hover:bg-[#c41840] disabled:opacity-40
                     disabled:cursor-not-allowed transition-colors"
        >
          {loading ? "Parsing…" : "AI Search"}
        </button>
      </form>

      {understood && understood.length > 0 && (
        <div className="mt-2 flex items-center gap-1.5 flex-wrap text-xs">
          <span className="text-gray-500">Understood:</span>
          {understood.map((t, i) => (
            <span key={i} className="px-2 py-0.5 rounded-full bg-[#fff0f3] text-[#c41840] border border-[#f9d2da]">
              {t}
            </span>
          ))}
        </div>
      )}
      {error && <p className="mt-2 text-xs text-red-500">{error}</p>}
      {!understood && !error && (
        <div className="mt-1.5 flex gap-2 flex-wrap">
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              onClick={() => { setQ(ex); }}
              className="text-[11px] text-gray-400 hover:text-[#e61e4d] italic transition-colors"
            >
              “{ex}”
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
