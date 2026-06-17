// Typed client for the backend API.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface SearchFilters {
  city?: string;
  check_in?: string;
  check_out?: string;
  adults?: number;
  children?: number;
  rooms?: number;
  price_min?: number;
  price_max?: number;
  min_rating?: number;
  property_types?: string[];
  amenities?: string[];
  sort?: "price_asc" | "rating" | "popularity" | "distance";
  near_lat?: number;
  near_lng?: number;
  page?: number;
  page_size?: number;
}

export interface ListingCard {
  id: string;
  name: string;
  type: string;
  city: string;
  neighbourhood?: string;
  lat: number;
  lng: number;
  price_per_night: number;
  total_for_stay?: number;
  rating?: number;
  review_count: number;
  key_amenities: string[];
  photo?: string;
  distance_km?: number;
}

export interface SearchResponse {
  results: ListingCard[];
  total: number;
  page: number;
  page_size: number;
}

export async function search(filters: SearchFilters): Promise<SearchResponse> {
  const res = await fetch(`${API_URL}/api/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  if (!res.ok) throw new Error(`search failed: ${res.status}`);
  return res.json();
}

// NL search bar -> structured filters (updates the visible filter chips).
export async function nlSearch(query: string): Promise<{ filters: SearchFilters; understood: Record<string, unknown> }> {
  const res = await fetch(`${API_URL}/api/nl-search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!res.ok) throw new Error(`nl-search failed: ${res.status}`);
  return res.json();
}
