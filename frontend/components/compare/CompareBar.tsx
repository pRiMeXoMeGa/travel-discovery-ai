"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useCompare } from "@/app/providers";
import { price } from "@/lib/currency";
import Image from "next/image";

export function CompareBar() {
  const { compareCards, remove, clear } = useCompare();
  const router = useRouter();

  if (compareCards.length === 0) return null;

  return (
    <div className="fixed bottom-0 left-0 right-0 z-40 bg-white border-t border-gray-200 shadow-2xl px-6 py-3">
      <div className="max-w-7xl mx-auto flex items-center gap-4">
        <div className="flex-1 flex items-center gap-3">
          <span className="text-sm font-semibold text-gray-700">
            Compare ({compareCards.length}/4)
          </span>
          <div className="flex gap-2">
            {compareCards.map((card) => (
              <div
                key={card.id}
                className="relative flex items-center gap-2 bg-gray-50 rounded-xl px-3 py-1.5 pr-7 border border-gray-100"
              >
                {card.photo && (
                  <div className="relative w-8 h-8 rounded-lg overflow-hidden flex-shrink-0">
                    <Image
                      src={card.photo}
                      alt={card.name}
                      fill
                      className="object-cover"
                      sizes="32px"
                    />
                  </div>
                )}
                <div className="min-w-0">
                  <p className="text-xs font-medium text-gray-800 truncate max-w-[120px]">
                    {card.name}
                  </p>
                  <p className="text-xs text-gray-400">
                    {price(card.price_per_night, card.city)}/night
                  </p>
                </div>
                <button
                  onClick={() => remove(card.id)}
                  className="absolute top-1 right-1.5 text-gray-400 hover:text-gray-700 text-base leading-none"
                  aria-label="Remove from compare"
                >
                  ×
                </button>
              </div>
            ))}
            {/* Empty slots */}
            {Array.from({ length: Math.max(0, 2 - compareCards.length) }).map((_, i) => (
              <div
                key={`empty-${i}`}
                className="w-28 h-12 rounded-xl border-2 border-dashed border-gray-200 flex items-center justify-center"
              >
                <span className="text-xs text-gray-300">+ Add</span>
              </div>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            onClick={clear}
            className="text-sm text-gray-400 hover:text-gray-600 underline underline-offset-2"
          >
            Clear
          </button>
          <button
            disabled={compareCards.length < 2}
            onClick={() => {
              const ids = compareCards.map((c) => c.id).join(",");
              router.push(`/compare?ids=${ids}`);
            }}
            className="px-5 py-2.5 bg-gray-900 text-white text-sm font-semibold rounded-xl hover:bg-gray-800 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            Compare {compareCards.length >= 2 ? `${compareCards.length} places` : "(add 1 more)"}
          </button>
        </div>
      </div>
    </div>
  );
}
