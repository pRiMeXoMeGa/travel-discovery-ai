"use client";

import React, { createContext, useContext, useState, useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { getWishlist, toggleWishlist, getCompare, toggleCompare, clearCompare } from "@/lib/wishlist";
import { ListingCard } from "@/lib/api";
import { ConciergePanel } from "@/components/concierge/ConciergePanel";

// ---- Wishlist ----
interface WishlistCtx {
  wishlist: string[];
  toggle: (id: string) => void;
  has: (id: string) => boolean;
}
const WishlistContext = createContext<WishlistCtx>({
  wishlist: [],
  toggle: () => {},
  has: () => false,
});

// ---- Compare ----
interface CompareCtx {
  compare: string[];
  compareCards: ListingCard[];
  toggle: (card: ListingCard) => void;
  remove: (id: string) => void;
  clear: () => void;
  has: (id: string) => boolean;
}
const CompareContext = createContext<CompareCtx>({
  compare: [],
  compareCards: [],
  toggle: () => {},
  remove: () => {},
  clear: () => {},
  has: () => false,
});

// ---- Hover sync (card <-> map marker) ----
// Split into two contexts so that components which only *write* hover state
// (e.g. map markers) don't re-render when the hovered id changes.
// The setter context value is permanently stable (a ref's .current never
// triggers re-renders), so it is safe to split out.
//
// For *reading* hover state we expose useIsHovered(id) backed by
// useSyncExternalStore so each card only re-renders when its own highlighted
// state flips — not when any other card's hover state changes.  This
// eliminates the render storm (all 20 cards re-rendering on every mouse
// move) that was starving the main thread and delaying click / navigation
// events during image loading.
type HoverListener = () => void;

interface HoverStore {
  getSnapshot: () => string | null;
  subscribe: (cb: HoverListener) => () => void;
  set: (id: string | null) => void;
}

function createHoverStore(): HoverStore {
  let current: string | null = null;
  const listeners = new Set<HoverListener>();
  return {
    getSnapshot: () => current,
    subscribe: (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    set: (id) => {
      if (id === current) return;
      current = id;
      listeners.forEach((cb) => cb());
    },
  };
}

// Setter context carries only the stable `set` function — never changes.
const HoverSetContext = createContext<(id: string | null) => void>(() => {});
// Store context carries the store object itself (also stable after mount).
const HoverStoreContext = createContext<HoverStore | null>(null);

/** Returns whether `id` is currently hovered. Only re-renders when the
 *  highlighted state for THIS id changes, not on every hover event. */
export function useIsHovered(id: string): boolean {
  const store = useContext(HoverStoreContext);
  return useSyncExternalStore(
    store ? store.subscribe : (_cb) => () => {},
    store ? () => store.getSnapshot() === id : () => false,
    () => false,
  );
}

/** Legacy shape kept for MapView and any other consumers that read hoveredId. */
interface HoverCtx {
  hoveredId: string | null;
  setHoveredId: (id: string | null) => void;
}
const HoverContext = createContext<HoverCtx>({
  hoveredId: null,
  setHoveredId: () => {},
});

export function Providers({ children }: { children: React.ReactNode }) {
  const [wishlist, setWishlist] = useState<string[]>([]);
  const [compare, setCompare] = useState<string[]>([]);
  const [compareCards, setCompareCards] = useState<ListingCard[]>([]);

  // Hover store: ref-stable object so HoverStoreContext/HoverSetContext never
  // force a re-render of the provider itself.
  const hoverStoreRef = useRef<HoverStore>(createHoverStore());
  const hoverStore = hoverStoreRef.current;

  // Legacy hoveredId state kept only for MapView's useHover() which reads it.
  // We sync it from the store so MapView still works without changes.
  const [hoveredId, setHoveredIdState] = useState<string | null>(null);

  // Wrap the store's setter to also update the legacy state for MapView.
  const setHoveredId = useCallback((id: string | null) => {
    hoverStore.set(id);
    setHoveredIdState(id);
  }, [hoverStore]);

  useEffect(() => {
    setWishlist(getWishlist());
    setCompare(getCompare());
  }, []);

  const toggleWL = useCallback((id: string) => {
    setWishlist(toggleWishlist(id));
  }, []);

  const toggleCmp = useCallback((card: ListingCard) => {
    const next = toggleCompare(card.id);
    setCompare(next);
    setCompareCards((prev) => {
      if (prev.find((c) => c.id === card.id)) {
        return prev.filter((c) => c.id !== card.id);
      }
      if (prev.length >= 4) return prev;
      return [...prev, card];
    });
  }, []);

  const removeCmp = useCallback((id: string) => {
    const next = getCompare().filter((x) => x !== id);
    localStorage.setItem("tdai_compare", JSON.stringify(next));
    setCompare(next);
    setCompareCards((prev) => prev.filter((c) => c.id !== id));
  }, []);

  const clearCmp = useCallback(() => {
    clearCompare();
    setCompare([]);
    setCompareCards([]);
  }, []);

  return (
    <WishlistContext.Provider
      value={{ wishlist, toggle: toggleWL, has: (id) => wishlist.includes(id) }}
    >
      <CompareContext.Provider
        value={{
          compare,
          compareCards,
          toggle: toggleCmp,
          remove: removeCmp,
          clear: clearCmp,
          has: (id) => compare.includes(id),
        }}
      >
        {/* HoverStoreContext: stable store object, never changes */}
        <HoverStoreContext.Provider value={hoverStore}>
          {/* HoverSetContext: stable setter, never changes */}
          <HoverSetContext.Provider value={hoverStore.set}>
            {/* Legacy HoverContext: only MapView reads hoveredId from here */}
            <HoverContext.Provider value={{ hoveredId, setHoveredId }}>
              {children}
              {/* Concierge is mounted globally so it's reachable from any page. */}
              <ConciergePanel />
            </HoverContext.Provider>
          </HoverSetContext.Provider>
        </HoverStoreContext.Provider>
      </CompareContext.Provider>
    </WishlistContext.Provider>
  );
}

export const useWishlist = () => useContext(WishlistContext);
export const useCompare = () => useContext(CompareContext);
export const useHover = () => useContext(HoverContext);
