"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import Image from "next/image";
import Link from "next/link";
import {
  streamConcierge,
  Citation,
  ConciergeEvent,
  ItineraryPlan,
  PlanStay,
  recomputePlanTotal,
} from "@/lib/concierge";
import { StarRating } from "@/components/ui/StarRating";

// Friendly labels for the agent step trail. "router" is internal → hidden.
const STEP_LABEL: Record<string, string> = {
  intent: "Understanding your request",
  retrieval: "Searching stays",
  review_intel: "Reading reviews",
  itinerary: "Planning your trip",
  answer: "Writing answer",
};

interface StepState {
  agent: string;
  status: "running" | "done" | "error";
}

interface Turn {
  role: "user" | "assistant";
  text: string;
  steps?: StepState[];
  citations?: Citation[];
  plan?: ItineraryPlan;
  /** Per-stay chosen index overrides for swap-out (key = segment index, value = alternative index or -1 for original). */
  swaps?: Record<number, number>;
  running?: boolean;
}

const SUGGESTIONS = [
  "Find me a quiet 1-bedroom in Lisbon near good restaurants for 3 nights in late June, under 130 a night, balcony if possible, and tell me which has the most consistent reviews.",
  "Plan a 4-night Los Angeles trip — one stay near the beach and one near downtown, budget $1200 total. Tell me which neighbourhoods to prioritise.",
];

// ---------------------------------------------------------------------------
// Itinerary card sub-components
// ---------------------------------------------------------------------------

interface StayCardProps {
  stay: PlanStay;
  swapIdx: number; // -1 = using original chosen, >= 0 = alternative index in use
  onSwap: (altIdx: number) => void;
  currencyNote: string;
}

function StayCard({ stay, swapIdx, onSwap, currencyNote }: StayCardProps) {
  const [showAlts, setShowAlts] = useState(false);

  const displayedOption = swapIdx >= 0 ? stay.alternatives[swapIdx] : stay.chosen;
  const listing = displayedOption.listing;
  const stayLabel = `Nights ${stay.segment}–${stay.segment + stay.nights - 1}`;

  return (
    <div className="rounded-xl border border-gray-100 overflow-hidden bg-white shadow-sm">
      {/* Stay header */}
      <div className="px-3.5 pt-3 pb-2 bg-gray-50 border-b border-gray-100">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-[#e61e4d]">
            {stayLabel}
          </span>
          <span className="text-[11px] text-gray-500">
            {stay.check_in} → {stay.check_out}
          </span>
        </div>
        <p className="text-xs font-medium text-gray-700 mt-0.5">{stay.theme}</p>
      </div>

      {/* Listing */}
      <div className="flex gap-3 p-3">
        {/* Thumbnail */}
        <Link
          href={`/listings/${listing.id}`}
          className="relative flex-shrink-0 w-20 h-20 rounded-lg overflow-hidden bg-gray-100 block"
          tabIndex={0}
        >
          {listing.photo ? (
            <Image
              src={listing.photo}
              alt={listing.name}
              fill
              className="object-cover"
              sizes="80px"
            />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-2xl text-gray-300">
              🏠
            </div>
          )}
        </Link>

        {/* Info */}
        <div className="flex-1 min-w-0">
          <Link
            href={`/listings/${listing.id}`}
            className="text-sm font-semibold text-gray-900 hover:text-[#e61e4d] line-clamp-2 leading-snug"
          >
            {listing.name}
          </Link>
          <p className="text-[11px] text-gray-400 mt-0.5 truncate">
            {listing.neighbourhood ?? listing.city}
          </p>

          {listing.rating != null && (
            <div className="mt-1">
              <StarRating rating={listing.rating} count={listing.review_count} />
            </div>
          )}

          <div className="flex items-baseline gap-2 mt-1.5">
            <span className="text-sm font-bold text-gray-900">
              ${Math.round(listing.price_per_night)}
              <span className="text-xs font-normal text-gray-500">/night</span>
            </span>
            <span className="text-xs text-gray-500">
              {currencyNote ? `${currencyNote} ` : ""}${Math.round(displayedOption.stay_cost)} total
            </span>
          </div>
        </div>
      </div>

      {/* Swap-out section */}
      {stay.alternatives.length > 0 && (
        <div className="border-t border-gray-100">
          <button
            onClick={() => setShowAlts((v) => !v)}
            className="w-full flex items-center justify-between px-3.5 py-2 text-xs text-gray-500 hover:text-gray-700 hover:bg-gray-50 transition-colors"
            aria-expanded={showAlts}
          >
            <span className="font-medium">
              {swapIdx >= 0
                ? `Using alternative · ${stay.alternatives.length} option${stay.alternatives.length !== 1 ? "s" : ""}`
                : `${stay.alternatives.length} swap-out${stay.alternatives.length !== 1 ? "s" : ""} available`}
            </span>
            <svg
              className={`w-3.5 h-3.5 transition-transform ${showAlts ? "rotate-180" : ""}`}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {showAlts && (
            <div className="divide-y divide-gray-50">
              {/* "Use original" row when a swap is active */}
              {swapIdx >= 0 && (
                <button
                  onClick={() => onSwap(-1)}
                  className="w-full flex items-center gap-2.5 px-3 py-2 hover:bg-gray-50 text-left transition-colors"
                >
                  <div className="relative flex-shrink-0 w-10 h-10 rounded-lg overflow-hidden bg-gray-100">
                    {stay.chosen.listing.photo ? (
                      <Image src={stay.chosen.listing.photo} alt={stay.chosen.listing.name} fill className="object-cover" sizes="40px" />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-lg text-gray-300">🏠</div>
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium text-gray-700 truncate">{stay.chosen.listing.name}</p>
                    <p className="text-[11px] text-gray-400">Original pick · ${Math.round(stay.chosen.stay_cost)} total</p>
                  </div>
                  <span className="flex-shrink-0 text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">Restore</span>
                </button>
              )}

              {stay.alternatives.map((alt, altIdx) => {
                const isActive = swapIdx === altIdx;
                return (
                  <button
                    key={alt.listing.id}
                    onClick={() => onSwap(isActive ? -1 : altIdx)}
                    className={`w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors ${
                      isActive ? "bg-[#fff0f3]" : "hover:bg-gray-50"
                    }`}
                  >
                    <div className="relative flex-shrink-0 w-10 h-10 rounded-lg overflow-hidden bg-gray-100">
                      {alt.listing.photo ? (
                        <Image src={alt.listing.photo} alt={alt.listing.name} fill className="object-cover" sizes="40px" />
                      ) : (
                        <div className="w-full h-full flex items-center justify-center text-lg text-gray-300">🏠</div>
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-medium text-gray-700 truncate">{alt.listing.name}</p>
                      <p className="text-[11px] text-gray-400 truncate">{alt.listing.neighbourhood ?? alt.listing.city}</p>
                      <p className="text-[11px] text-gray-500 mt-0.5">${Math.round(alt.listing.price_per_night)}/night · ${Math.round(alt.stay_cost)} total</p>
                    </div>
                    <span
                      className={`flex-shrink-0 text-[11px] px-2 py-0.5 rounded-full font-medium transition-colors ${
                        isActive
                          ? "bg-[#e61e4d] text-white"
                          : "bg-gray-100 text-gray-500 hover:bg-[#e61e4d] hover:text-white"
                      }`}
                    >
                      {isActive ? "Selected" : "Use this"}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Itinerary plan header + stay cards
// ---------------------------------------------------------------------------

interface ItineraryViewProps {
  plan: ItineraryPlan;
  swaps: Record<number, number>;
  onSwap: (stayIdx: number, altIdx: number) => void;
}

function ItineraryView({ plan, swaps, onSwap }: ItineraryViewProps) {
  // Compute current total from swaps
  const currentTotal = plan.stays.reduce((sum, stay, idx) => {
    const swapIdx = swaps[idx] ?? -1;
    const cost = swapIdx >= 0 ? stay.alternatives[swapIdx]?.stay_cost ?? stay.chosen.stay_cost : stay.chosen.stay_cost;
    return sum + cost;
  }, 0);

  const hasSwaps = Object.values(swaps).some((v) => v >= 0);
  const budgetStatus =
    plan.within_budget === true
      ? "within"
      : plan.within_budget === false
      ? "over"
      : "unknown";

  return (
    <div className="space-y-3">
      {/* Plan header */}
      <div className="rounded-xl bg-gray-50 border border-gray-100 px-4 py-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div>
            <p className="text-xs text-gray-500 uppercase tracking-wide">{plan.city} · {plan.total_nights} nights</p>
            <p className="text-base font-bold text-gray-900 mt-0.5">
              ${Math.round(currentTotal).toLocaleString()}
              {hasSwaps && currentTotal !== plan.total_cost && (
                <span className="text-xs font-normal text-gray-400 ml-1.5">
                  (was ${Math.round(plan.total_cost).toLocaleString()})
                </span>
              )}
            </p>
            {plan.currency_note && (
              <p className="text-[11px] text-gray-400 mt-0.5">{plan.currency_note}</p>
            )}
          </div>
          <span
            className={`text-xs font-semibold px-2.5 py-1 rounded-full ${
              budgetStatus === "within"
                ? "bg-green-100 text-green-700"
                : budgetStatus === "over"
                ? "bg-amber-100 text-amber-700"
                : "bg-gray-100 text-gray-500"
            }`}
          >
            {budgetStatus === "within"
              ? "Within budget"
              : budgetStatus === "over"
              ? "Over budget"
              : "No budget set"}
          </span>
        </div>

        {plan.notes.length > 0 && (
          <ul className="mt-2 space-y-1">
            {plan.notes.map((note, ni) => (
              <li key={ni} className="text-[11px] text-gray-500 flex gap-1.5">
                <span className="text-gray-400 flex-shrink-0">·</span>
                {note}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Stay cards */}
      {plan.stays.map((stay, idx) => (
        <StayCard
          key={stay.segment}
          stay={stay}
          swapIdx={swaps[idx] ?? -1}
          onSwap={(altIdx) => onSwap(idx, altIdx)}
          currencyNote=""
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export function ConciergePanel() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns]);

  useEffect(() => () => abortRef.current?.abort(), []);

  function patchLast(fn: (t: Turn) => Turn) {
    setTurns((prev) => {
      const next = [...prev];
      const i = next.length - 1;
      if (i >= 0) next[i] = fn(next[i]);
      return next;
    });
  }

  /** Mark any remaining running steps as done — safety net for missed/late step:done events. */
  function flushRunningSteps() {
    patchLast((t) => {
      const steps = (t.steps ?? []).map((s) =>
        s.status === "running" ? { ...s, status: "done" as const } : s
      );
      return { ...t, steps, running: false };
    });
  }

  const handleSwap = useCallback((turnIdx: number, stayIdx: number, altIdx: number) => {
    setTurns((prev) => {
      const next = [...prev];
      const turn = next[turnIdx];
      if (!turn) return prev;
      const swaps = { ...(turn.swaps ?? {}), [stayIdx]: altIdx };
      next[turnIdx] = { ...turn, swaps };
      return next;
    });
  }, []);

  async function send(q: string) {
    const query = q.trim();
    if (!query || running) return;
    setInput("");
    setRunning(true);
    setTurns((prev) => [
      ...prev,
      { role: "user", text: query },
      { role: "assistant", text: "", steps: [], citations: [], running: true },
    ]);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      for await (const ev of streamConcierge(query, ctrl.signal) as AsyncGenerator<ConciergeEvent>) {
        if (ev.type === "step") {
          if (ev.agent === "router") continue;
          const status = ev.status === "start" ? "running" : ev.status;
          patchLast((t) => {
            const steps = [...(t.steps ?? [])];
            const idx = steps.findIndex((s) => s.agent === ev.agent);
            if (idx >= 0) steps[idx] = { agent: ev.agent, status };
            else steps.push({ agent: ev.agent, status });
            return { ...t, steps };
          });
        } else if (ev.type === "data") {
          patchLast((t) => ({ ...t, citations: ev.citations }));
        } else if (ev.type === "itinerary") {
          patchLast((t) => ({ ...t, plan: ev.plan, swaps: {} }));
        } else if (ev.type === "token") {
          patchLast((t) => ({ ...t, text: t.text + ev.text }));
        } else if (ev.type === "done") {
          // Safety net: stream finished — resolve any stuck steps
          flushRunningSteps();
        } else if (ev.type === "error") {
          patchLast((t) => ({
            ...t,
            text: t.text || "Sorry — something went wrong.",
            running: false,
          }));
        }
      }
    } catch {
      patchLast((t) => ({
        ...t,
        text: t.text || "The concierge is unavailable right now.",
        running: false,
      }));
    } finally {
      // Safety net: ensure spinner is always cleared even if answer:done was missed
      flushRunningSteps();
      setRunning(false);
    }
  }

  const turnCount = turns.length;

  return (
    <>
      {/* Launcher */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="fixed bottom-5 right-5 z-40 flex items-center gap-2 px-4 py-3 rounded-full
                   bg-[#e61e4d] text-white shadow-lg hover:bg-[#c41840] transition-colors"
        aria-label="Open AI concierge"
      >
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-4 4v-4z" />
        </svg>
        <span className="text-sm font-semibold hidden sm:block">Ask AI</span>
      </button>

      {/* Panel */}
      {open && (
        <div className="fixed inset-y-0 right-0 z-50 w-full sm:w-[440px] bg-white shadow-2xl border-l border-gray-100 flex flex-col">
          <header className="flex items-center justify-between px-4 py-3 border-b border-gray-100 flex-shrink-0">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-[#e61e4d] flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M5 3v4M3 5h4M13 3l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
                </svg>
              </div>
              <span className="font-semibold text-gray-900">Travel Concierge</span>
            </div>
            <button onClick={() => setOpen(false)} aria-label="Close concierge" className="text-gray-400 hover:text-gray-700">
              <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </header>

          <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
            {turnCount === 0 && (
              <div className="text-sm text-gray-500 space-y-3">
                <p>Ask me to find stays, compare reviews, or plan a multi-stop trip.</p>
                <div className="space-y-2">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} onClick={() => send(s)}
                      className="block w-full text-left text-xs p-2.5 rounded-lg border border-gray-200 hover:border-[#e61e4d] hover:bg-[#fff0f3] transition-colors">
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {turns.map((t, turnIdx) =>
              t.role === "user" ? (
                <div key={turnIdx} className="flex justify-end">
                  <div className="max-w-[85%] bg-[#e61e4d] text-white rounded-2xl rounded-br-sm px-3.5 py-2 text-sm">{t.text}</div>
                </div>
              ) : (
                <div key={turnIdx} className="space-y-3">
                  {/* Agent step trail */}
                  {t.steps && t.steps.length > 0 && (
                    <div className="space-y-1">
                      {t.steps.map((s) => (
                        <div key={s.agent} className="flex items-center gap-2 text-xs text-gray-500">
                          {s.status === "running" ? (
                            <span className="w-3 h-3 border-2 border-gray-300 border-t-[#e61e4d] rounded-full animate-spin flex-shrink-0" />
                          ) : s.status === "error" ? (
                            <span className="flex-shrink-0 w-3 h-3 rounded-full bg-red-100 flex items-center justify-center text-[9px] text-red-600 font-bold">!</span>
                          ) : (
                            <svg className="flex-shrink-0 w-3 h-3 text-green-600" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M2 6l3 3 5-5" />
                            </svg>
                          )}
                          {STEP_LABEL[s.agent] ?? s.agent}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Prose intro (one-line summary above cards) */}
                  {t.text && (
                    <div className="bg-gray-50 rounded-2xl rounded-bl-sm px-3.5 py-2.5 text-sm text-gray-800 whitespace-pre-wrap">
                      {t.text}
                      {t.running && !t.plan && (
                        <span className="inline-block w-1.5 h-3.5 ml-0.5 bg-[#e61e4d] animate-pulse align-middle" />
                      )}
                    </div>
                  )}

                  {/* Itinerary plan cards — rendered independently of prose answer */}
                  {t.plan && (
                    <ItineraryView
                      plan={t.plan}
                      swaps={t.swaps ?? {}}
                      onSwap={(stayIdx, altIdx) => handleSwap(turnIdx, stayIdx, altIdx)}
                    />
                  )}

                  {/* Citations */}
                  {t.citations && t.citations.length > 0 && (
                    <div className="space-y-1">
                      <p className="text-[11px] uppercase tracking-wide text-gray-400">Sources</p>
                      {t.citations.map((c, ci) =>
                        c.kind === "listing" ? (
                          <Link key={ci} href={`/listings/${c.id}`} onClick={() => setOpen(false)}
                            className="block text-xs text-[#c41840] hover:underline truncate">
                            stay: {c.snippet || c.id}
                          </Link>
                        ) : (
                          <p key={ci} className="text-xs text-gray-500 truncate">review: {c.snippet || "review"}</p>
                        )
                      )}
                    </div>
                  )}
                </div>
              )
            )}
          </div>

          <form
            onSubmit={(e) => { e.preventDefault(); send(input); }}
            className="flex-shrink-0 border-t border-gray-100 p-3 flex gap-2"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask the concierge..."
              disabled={running}
              className="flex-1 px-3 py-2 rounded-xl border border-gray-200 text-sm focus:border-[#e61e4d] focus:ring-2 focus:ring-[#fff0f3] outline-none disabled:bg-gray-50"
            />
            <button type="submit" disabled={running || !input.trim()}
              className="px-4 py-2 rounded-xl bg-[#e61e4d] text-white text-sm font-medium hover:bg-[#c41840] disabled:opacity-40 transition-colors">
              Send
            </button>
          </form>
        </div>
      )}
    </>
  );
}
