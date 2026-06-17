// SSE client for the streaming concierge. Streams intermediate agent steps and
// answer tokens so the UI can show visible agent progress (brief §2.3/§2.5).
//
// Uses fetch + ReadableStream (not EventSource) because the endpoint is POST.
import { API_URL } from "./api";

export type ConciergeEvent =
  | { type: "step"; agent: string; status: "start" | "done" | "error"; data?: unknown }
  | { type: "token"; text: string }
  | { type: "done"; trace: unknown }
  | { type: "error"; message: string };

export async function* streamConcierge(
  query: string,
  signal?: AbortSignal,
): AsyncGenerator<ConciergeEvent> {
  const res = await fetch(`${API_URL}/api/concierge/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
    signal,
  });
  if (!res.body) throw new Error("no response body");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Parse SSE frames separated by a blank line.
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
}
