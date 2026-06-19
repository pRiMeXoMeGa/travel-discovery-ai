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

export interface Host {
  id: string;
  name: string;
  superhost: boolean;
  joined_year: number;
}

export interface AvailabilityDay {
  date: string;
  available: boolean;
  price: number;
}

export interface AspectAvg {
  noise: number | null;
  staff: number | null;
  value: number | null;
  location: number | null;
  cleanliness: number | null;
}

export interface ListingDetail {
  id: string;
  name: string;
  type: string;
  city: string;
  neighbourhood?: string;
  lat: number;
  lng: number;
  base_price: number;
  beds: number;
  amenities: string[];
  photos: string[];
  host: Host;
  rating?: number;
  review_count: number;
  neighbourhood_price_pct?: number;
  summary?: string;
  aspect_avg?: AspectAvg;
  availability_window: AvailabilityDay[];
}

export interface Review {
  id: string;
  date?: string;
  reviewer?: string;
  rating?: number;
  text: string;
  language?: string;
  aspects?: AspectAvg;
  sentiment?: number;
}

export interface ReviewsResponse {
  results: Review[];
  total: number;
  page: number;
  page_size: number;
}

export interface NlSearchResponse {
  understanding: Record<string, unknown>;
  filters: SearchFilters;
  results: SearchResponse;
}

export interface CompareResponse {
  listings: ListingDetail[];
  verdict: string | null;
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

export async function getListing(id: string): Promise<ListingDetail> {
  const res = await fetch(`${API_URL}/api/listings/${id}`);
  if (!res.ok) throw new Error(`getListing failed: ${res.status}`);
  return res.json();
}

export async function getReviews(
  id: string,
  params: { language?: string; min_score?: number; topic?: string; page?: number } = {}
): Promise<ReviewsResponse> {
  const qs = new URLSearchParams();
  if (params.language) qs.set("language", params.language);
  if (params.min_score != null) qs.set("min_score", String(params.min_score));
  if (params.topic) qs.set("topic", params.topic);
  if (params.page) qs.set("page", String(params.page));
  const res = await fetch(`${API_URL}/api/listings/${id}/reviews?${qs}`);
  if (!res.ok) throw new Error(`getReviews failed: ${res.status}`);
  return res.json();
}

export async function compareListing(listing_ids: string[]): Promise<CompareResponse> {
  const res = await fetch(`${API_URL}/api/batch/compare`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ listing_ids }),
  });
  if (!res.ok) throw new Error(`compare failed: ${res.status}`);
  return res.json();
}

// NL search bar -> structured filters (updates the visible filter chips).
export async function nlSearch(query: string): Promise<NlSearchResponse> {
  const res = await fetch(`${API_URL}/api/nl-search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!res.ok) throw new Error(`nl-search failed: ${res.status}`);
  return res.json();
}
