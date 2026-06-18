"use client";

import React, { createContext, useContext, useState, useCallback, useEffect } from "react";
import { getWishlist, toggleWishlist, getCompare, toggleCompare, clearCompare } from "@/lib/wishlist";
import { ListingCard } from "@/lib/api";

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
  const [hoveredId, setHoveredId] = useState<string | null>(null);

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
        <HoverContext.Provider value={{ hoveredId, setHoveredId }}>
          {children}
        </HoverContext.Provider>
      </CompareContext.Provider>
    </WishlistContext.Provider>
  );
}

export const useWishlist = () => useContext(WishlistContext);
export const useCompare = () => useContext(CompareContext);
export const useHover = () => useContext(HoverContext);
