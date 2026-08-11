// Traveller identity for the memory layer (WS1).
//
// This is a localStorage UUID, and it is deliberately NOT authentication.
// It gives *same-browser persistence*: clear site data, open a private window,
// or switch device and you are a new traveller. The README says so in these
// words, because a memory feature that looks like an account but isn't one is
// the kind of thing a reviewer is right to be suspicious of.
//
// Consequence worth being explicit about: anyone with the id can read that
// traveller's memories, so nothing sensitive should ever be stored under it.

const USER_KEY = "travel-ai:user-id";
const TRIP_KEY = "travel-ai:trip-id";

function uuid(): string {
  // crypto.randomUUID needs a secure context. localhost counts as secure, but
  // a plain-HTTP LAN address (testing from a phone) does not, so fall back.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

/** Read a key, minting and persisting a fresh UUID when absent. */
function getOrCreate(key: string): string | null {
  // Called during SSR/prerender too, where there is no localStorage. Returning
  // null keeps the request anonymous rather than throwing — memory is an
  // enhancement, and the concierge has to work without it.
  if (typeof window === "undefined") return null;
  try {
    const existing = window.localStorage.getItem(key);
    if (existing) return existing;
    const fresh = uuid();
    window.localStorage.setItem(key, fresh);
    return fresh;
  } catch {
    // Safari private mode and "block all cookies" both throw on access.
    return null;
  }
}

/** Stable per-browser traveller id. Null when storage is unavailable. */
export function getUserId(): string | null {
  return getOrCreate(USER_KEY);
}

/** Current trip scope. Null until a trip is explicitly started. */
export function getTripId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(TRIP_KEY);
  } catch {
    return null;
  }
}

/** Begin a new trip scope and return its id. */
export function startTrip(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const id = uuid();
    window.localStorage.setItem(TRIP_KEY, id);
    return id;
  } catch {
    return null;
  }
}

/** End the current trip scope. Traveller memory is untouched. */
export function endTrip(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(TRIP_KEY);
  } catch {
    // nothing to do
  }
}

/**
 * Forget this browser entirely — drops the traveller id, so the next request
 * starts a new identity. Server-side memories are orphaned rather than deleted;
 * per-item deletion goes through the memory panel's forget button.
 */
export function resetIdentity(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(USER_KEY);
    window.localStorage.removeItem(TRIP_KEY);
  } catch {
    // nothing to do
  }
}
