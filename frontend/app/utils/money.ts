const euroFormatter = new Intl.NumberFormat("en-IE", {
  style: "currency",
  currency: "EUR",
});

export function formatCurrencyEur(value: number | string | null | undefined): string {
  const amount = Number(value ?? 0);
  return euroFormatter.format(Number.isFinite(amount) ? amount : 0);
}
