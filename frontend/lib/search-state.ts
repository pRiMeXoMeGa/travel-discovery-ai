// Central search state — shared between filters, results list, and map.
// URL params are the source of truth; this file provides serialisers.

import { SearchFilters } from "./api";

export const AMENITY_LABELS: Record<string, string> = {
  wifi: "WiFi",
  pool: "Pool",
  kitchen: "Kitchen",
  parking: "Parking",
  balcony: "Balcony",
  ac: "A/C",
  gym: "Gym",
  washer: "Washer",
  pets_allowed: "Pets allowed",
  hot_tub: "Hot tub",
  bbq: "BBQ",
  workspace: "Workspace",
  beach_access: "Beach access",
  concierge: "Concierge",
  breakfast_included: "Breakfast",
  ev_charger: "EV charger",
  elevator: "Elevator",
  baby_cot: "Baby cot",
};

export const PROPERTY_TYPE_LABELS: Record<string, string> = {
  "entire place": "Entire place",
  "private room": "Private room",
  hotel: "Hotel",
  "shared room": "Shared room",
};

export const SORT_LABELS: Record<string, string> = {
  popularity: "Most popular",
  rating: "Top rated",
  price_asc: "Price: low to high",
  distance: "Nearest",
};

export const CITIES = ["Lisbon", "Dubai"];

export function filtersToSearchParams(filters: SearchFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (filters.city) p.set("city", filters.city);
  if (filters.check_in) p.set("check_in", filters.check_in);
  if (filters.check_out) p.set("check_out", filters.check_out);
  if (filters.adults != null) p.set("adults", String(filters.adults));
  if (filters.children != null) p.set("children", String(filters.children));
  if (filters.rooms != null) p.set("rooms", String(filters.rooms));
  if (filters.price_min != null) p.set("price_min", String(filters.price_min));
  if (filters.price_max != null) p.set("price_max", String(filters.price_max));
  if (filters.min_rating != null) p.set("min_rating", String(filters.min_rating));
  if (filters.sort) p.set("sort", filters.sort);
  if (filters.page) p.set("page", String(filters.page));
  if (filters.property_types?.length) {
    filters.property_types.forEach((t) => p.append("pt", t));
  }
  if (filters.amenities?.length) {
    filters.amenities.forEach((a) => p.append("am", a));
  }
  return p;
}

export function searchParamsToFilters(p: URLSearchParams): SearchFilters {
  const filters: SearchFilters = {};
  if (p.has("city")) filters.city = p.get("city")!;
  if (p.has("check_in")) filters.check_in = p.get("check_in")!;
  if (p.has("check_out")) filters.check_out = p.get("check_out")!;
  if (p.has("adults")) filters.adults = Number(p.get("adults"));
  if (p.has("children")) filters.children = Number(p.get("children"));
  if (p.has("rooms")) filters.rooms = Number(p.get("rooms"));
  if (p.has("price_min")) filters.price_min = Number(p.get("price_min"));
  if (p.has("price_max")) filters.price_max = Number(p.get("price_max"));
  if (p.has("min_rating")) filters.min_rating = Number(p.get("min_rating"));
  if (p.has("sort")) filters.sort = p.get("sort") as SearchFilters["sort"];
  if (p.has("page")) filters.page = Number(p.get("page"));
  const pts = p.getAll("pt");
  if (pts.length) filters.property_types = pts;
  const ams = p.getAll("am");
  if (ams.length) filters.amenities = ams;
  return filters;
}

export const DEFAULT_FILTERS: SearchFilters = {
  city: "Lisbon",
  adults: 2,
  rooms: 1,
  sort: "popularity",
  page: 1,
  page_size: 20,
};
