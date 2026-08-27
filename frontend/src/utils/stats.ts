export function formatMoney(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "Not listed";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

export function notListed(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "Not listed";
  return String(value);
}
