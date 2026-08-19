/** Цвет одной gate dot: PASS зелёный, FAIL красный, null/undefined серый. */
export function gateColor(passed: boolean | null | undefined): string {
  if (passed === true) return 'var(--ed-up)';
  if (passed === false) return 'var(--ed-down)';
  return 'var(--ed-ink-faint)';
}
