// Shared display and adjustment conventions for both remotes.
const OFFSET_STEPS = [-50, -10, -1, 1, 10, 50];

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

// Both remotes use the same controls, including labels and step sizes.
export function createOffsetButtons(container, onAdjust) {
  container.setAttribute("role", "group");
  container.setAttribute("aria-label", "字幕偏移");
  return OFFSET_STEPS.map((step) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn neutral";
    button.textContent = (step < 0 ? "−" : "+") + Math.abs(step / 10) + "s";
    button.onclick = () => onAdjust(button, step);
    container.append(button);
    return button;
  });
}
