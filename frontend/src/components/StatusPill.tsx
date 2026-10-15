import type { WorldSummary } from "../api/types";
import { STATE_LABEL, STATE_TONE } from "../format";

export function StatusPill({ world }: { world: Pick<WorldSummary, "state" | "update_progress"> }) {
  const tone = STATE_TONE[world.state];
  const busy = world.state === "starting" || world.state === "updating";
  let label = STATE_LABEL[world.state];
  if (world.state === "updating" && world.update_progress != null) {
    label = `Downloading ${Math.floor(world.update_progress)}%`;
  }
  return (
    <span className={`pill ${tone}${busy ? " pulse" : ""}`} role="status">
      {label}
    </span>
  );
}
