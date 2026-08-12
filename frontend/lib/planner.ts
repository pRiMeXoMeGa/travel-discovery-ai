// Client for the WS3 LangGraph trip planner.
//
// Deliberately separate from `streamConcierge` rather than an extra mode on it.
// The concierge assumes one request per turn — start a stream, consume it, done
// — and the planner breaks that assumption structurally: it can suspend at a
// human checkpoint and be continued later by a SECOND request, possibly against
// a different server process. Overloading the concierge client with that would
// put a working, e2e-tested path at risk for no gain.
//
// The SSE frame parsing IS shared (`parseSseFrames`), because reimplementing it
// is how you lose the CR-strip that makes uvicorn's CRLF frames parse at all.
import { API_URL } from "./api";
import { parseSseFrames } from "./concierge";
import { getTripId, getUserId } from "./identity";

/** What the planner is waiting for a human to decide. */
export interface PlanReview {
  kind: "plan_review";
  stays: number;
  total_cost: number | null;
  within_budget: boolean | null;
  notes: string[];
}

export type PlannerEvent =
  | { type: "step"; agent: string; status: string; data?: unknown }
  | { type: "data"; plan: unknown }
  // Terminal for THIS request, but the turn is NOT over: hold `thread_id` and
  // continue with resumePlanner(). Distinct from `done` on purpose — a client
  // that treats it as `done` silently drops the trip.
  | { type: "awaiting_input"; thread_id: string; review: PlanReview }
  | {
      type: "done";
      thread_id: string;
      finalized: boolean;
      decision: string | null;
      errors: string[];
    }
  | { type: "error"; message: string; detail?: string; thread_id?: string };

/** Friendly labels for the planner's node trail. */
export const PLANNER_STEP_LABEL: Record<string, string> = {
  planner_intent: "Understanding your trip",
  planner_plan: "Building an itinerary",
  planner_budget: "Checking the budget",
  planner_review: "Waiting for your decision",
  planner_finalize: "Finalising",
};

async function open(path: string, body: unknown, signal?: AbortSignal): Promise<Response> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`planner request failed: ${res.status} ${text}`);
  }
  return res;
}

/** Start a plan. Ends either at `done` or at `awaiting_input`. */
export async function* streamPlanner(
  query: string,
  signal?: AbortSignal,
): AsyncGenerator<PlannerEvent> {
  const res = await open(
    "/api/planner/stream",
    { query, user_id: getUserId(), trip_id: getTripId() },
    signal,
  );
  yield* parseSseFrames<PlannerEvent>(res);
}

/** Continue a suspended plan. Returns a fresh stream of what follows.
 *
 * `thread_id` is the only state the client needs to keep: everything else lives
 * in the server-side checkpoint, which is why this still works after the
 * instance has spun down and come back. */
export async function* resumePlanner(
  threadId: string,
  action: "approve" | "adjust",
  feedback?: string,
  signal?: AbortSignal,
): AsyncGenerator<PlannerEvent> {
  const res = await open(
    "/api/planner/resume",
    { thread_id: threadId, action, feedback: feedback ?? null },
    signal,
  );
  yield* parseSseFrames<PlannerEvent>(res);
}
