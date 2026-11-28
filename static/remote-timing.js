// Shared display and adjustment conventions for both remotes.
export const OFFSET_STEPS = [-50, -10, -1, 1, 10, 50];

export function clock(seconds) {
  return Number.isFinite(seconds)
    ? Math.floor(seconds / 60) + ":" + String(Math.floor(seconds % 60)).padStart(2, "0")
    : "0:00";
}

export function offsetLabel(tenths) {
  return (tenths >= 0 ? "+" : "") + (tenths / 10).toFixed(1) + "s";
}

export function adjustOffset(current, delta) {
  return Math.max(-600, Math.min(600, Number(current || 0) + delta));
}
