// Wishlist + compare state — localStorage-backed, no auth required.
// Designed to be consumed by a React context in the app.

export const WISHLIST_KEY = "tdai_wishlist";
export const COMPARE_KEY = "tdai_compare";

export function getWishlist(): string[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(WISHLIST_KEY) ?? "[]");
  } catch {
    return [];
  }
}

export function toggleWishlist(id: string): string[] {
  const current = getWishlist();
  const next = current.includes(id)
    ? current.filter((x) => x !== id)
    : [...current, id];
  localStorage.setItem(WISHLIST_KEY, JSON.stringify(next));
  return next;
}

export function getCompare(): string[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(COMPARE_KEY) ?? "[]");
  } catch {
    return [];
  }
}

export function toggleCompare(id: string): string[] {
  const current = getCompare();
  if (current.includes(id)) {
    const next = current.filter((x) => x !== id);
    localStorage.setItem(COMPARE_KEY, JSON.stringify(next));
    return next;
  }
  if (current.length >= 4) return current; // max 4
  const next = [...current, id];
  localStorage.setItem(COMPARE_KEY, JSON.stringify(next));
  return next;
}

export function clearCompare(): void {
  localStorage.setItem(COMPARE_KEY, "[]");
}
