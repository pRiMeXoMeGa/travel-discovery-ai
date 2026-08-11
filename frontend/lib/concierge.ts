// SSE client for the streaming concierge. Streams intermediate agent steps and
// answer tokens so the UI can show visible agent progress (brief §2.3/§2.5).
//
// Uses fetch + ReadableStream (not EventSource) because the endpoint is POST.
import { API_URL } from "./api";
import type { ListingCard } from "./api";
import { getTripId, getUserId } from "./identity";

export interface Citation {
  kind: "listing" | "review";
  id: string;
  snippet?: string;
}

// Itinerary plan types — mirror the backend schema exactly.
export interface StayOption {
  listing: ListingCard;
  rationale: string;
  stay_cost: number;
}

export interface PlanStay {
  segment: number;
  theme: string;
  nights: number;
  check_in: string;
  check_out: string;
  chosen: StayOption;
  alternatives: StayOption[];
  days: string[];
}

export interface ItineraryPlan {
  city: string;
  total_nights: number;
  total_cost: number;
  currency_note: string;
  within_budget: boolean | null;
  notes: string[];
  stays: PlanStay[];
}

// Memory (WS1) rides the existing `step` event with agent === "memory" — no new
// SSE event type. `data.phase` separates the pre-intent recall from the
// post-answer write, which matters because both arrive under the same agent
// name and would otherwise collide in the step trail.
export interface RecalledMemory {
  id: string;
  memory: string;
  score?: number;
  /** Present only on validated standing rules; absent on soft preferences. */
  kind?: "dealbreaker";
  field?: "amenities" | "type";
  value?: string;
  op?: "must" | "must_not";
}

export interface MemoryRecallData {
  phase: "recall";
  traveller: RecalledMemory[];
  trip: RecalledMemory[];
  /** Rules the user believes are enforced but which have no payload field. */
  unmapped?: string[];
}

export interface MemoryWriteData {
  phase: "write";
  written: RecalledMemory[];
}

export type MemoryStepData = MemoryRecallData | MemoryWriteData;

/** Narrow a step event's `unknown` payload to a memory payload.
 *
 * A dedicated union arm for `agent: "memory"` does not work: the general arm
 * types `agent` as `string`, which subsumes the literal, so TypeScript
 * intersects the two `data` types down to `{}` instead of picking one. A guard
 * keeps the runtime check and the type narrowing in the same place, which is
 * also where a malformed frame from the wire has to be rejected anyway.
 */
export function isMemoryStepData(data: unknown): data is MemoryStepData {
  if (typeof data !== "object" || data === null) return false;
  const phase = (data as { phase?: unknown }).phase;
  return phase === "recall" || phase === "write";
}

export type ConciergeEvent =
  | { type: "step"; agent: string; status: "start" | "done" | "error"; data?: unknown }
  | { type: "data"; citations: Citation[] }
  | { type: "itinerary"; plan: ItineraryPlan }
  | { type: "token"; text: string }
  | { type: "done"; trace: unknown }
  | { type: "error"; message: string };

export async function* streamConcierge(
  query: string,
  signal?: AbortSignal,
): AsyncGenerator<ConciergeEvent> {
  // Identity is best-effort: null when storage is blocked, and the backend
  // treats a missing user_id as an anonymous turn with no memory.
  const user_id = getUserId();
  const trip_id = getTripId();

  const res = await fetch(`${API_URL}/api/concierge/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, user_id, trip_id }),
    signal,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`concierge request failed: ${res.status} ${text}`);
  }
  if (!res.body) throw new Error("no response body");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      // The spec uses \n\n but many ASGI servers (e.g. uvicorn) emit \r\n
      // line endings, making the actual frame boundary \r\n\r\n.
      // Strip all bare \r so we only deal with \n, then split on \n\n.
      buffer = buffer.replace(/\r/g, "");

      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        try {
          yield JSON.parse(dataLine.slice(5).trim()) as ConciergeEvent;
        } catch {
          // ignore malformed frame
        }
      }
    }

    // Flush any remaining buffer content (stream ended without trailing blank line)
    if (buffer.trim()) {
      const dataLine = buffer.split("\n").find((l) => l.startsWith("data:"));
      if (dataLine) {
        try {
          yield JSON.parse(dataLine.slice(5).trim()) as ConciergeEvent;
        } catch {
          // ignore
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** Recompute a plan's total_cost from per-stay costs (used after a swap-out). */
export function recomputePlanTotal(plan: ItineraryPlan, stays: PlanStay[]): number {
  return stays.reduce((sum, s) => sum + s.chosen.stay_cost, 0);
}
