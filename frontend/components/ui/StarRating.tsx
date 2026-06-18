"use client";

interface StarRatingProps {
  rating: number;
  count?: number;
  size?: "sm" | "md";
}

export function StarRating({ rating, count, size = "sm" }: StarRatingProps) {
  const pct = Math.round((rating / 5) * 100);
  const textSize = size === "sm" ? "text-xs" : "text-sm";
  const starSize = size === "sm" ? "text-sm" : "text-base";

  return (
    <span className={`flex items-center gap-1 ${textSize} text-gray-700`}>
      <span className={`${starSize} text-[#FF385C] leading-none`}>★</span>
      <span className="font-semibold">{rating.toFixed(1)}</span>
      {count != null && (
        <span className="text-gray-400">({count.toLocaleString()})</span>
      )}
    </span>
  );
}
