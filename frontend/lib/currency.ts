// Per-city currency. Inside Airbnb prices are denominated in each city's local
// currency: Amsterdam & Lisbon → EUR (€), Los Angeles → USD ($).
const CITY_SYMBOL: Record<string, string> = {
  Amsterdam: "€",
  Lisbon: "€",
  "Los Angeles": "$",
};

/** Currency symbol for a city; falls back to "$" for unknown/missing city. */
export function currencySymbol(city?: string | null): string {
  return (city && CITY_SYMBOL[city]) || "$";
}

/** Rounded price with the city's currency symbol, e.g. price(80, "Lisbon") → "€80". */
export function price(amount: number, city?: string | null): string {
  return `${currencySymbol(city)}${Math.round(amount).toLocaleString()}`;
}
