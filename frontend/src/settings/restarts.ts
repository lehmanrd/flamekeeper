/** Scheduled restart card logic: presets, the "players online" choice and the summary. */
import type { RestartSchedule } from "../api/types";

export const RESTART_PRESETS: { cron: string; label: string }[] = [
  { cron: "0 5 * * *", label: "Daily at 05:00" },
  { cron: "0 5,17 * * *", label: "Twice a day (05:00, 17:00)" },
  { cron: "0 */6 * * *", label: "Every 6 hours" },
  { cron: "0 5 * * 1", label: "Weekly, Monday 05:00" },
];

/** The preset matching a cron expression, or "custom". */
export function restartPresetFor(cron: string): string {
  const norm = cron.trim().replace(/\s+/g, " ");
  return RESTART_PRESETS.find((p) => p.cron === norm)?.cron ?? "custom";
}

/** What happens when players are online at restart time: one choice over two fields. */
export type PlayersMode = "wait_skip" | "wait_restart" | "restart";

export const PLAYERS_MODES: { id: PlayersMode; label: string }[] = [
  { id: "wait_skip", label: "Wait for them to leave, else skip until next time" },
  { id: "wait_restart", label: "Wait for them to leave, else restart anyway" },
  { id: "restart", label: "Restart anyway (players are disconnected)" },
];

type ModeFields = Pick<RestartSchedule, "only_when_empty" | "if_still_busy">;

export function playersMode(s: ModeFields): PlayersMode {
  if (!s.only_when_empty) return "restart";
  return s.if_still_busy === "restart" ? "wait_restart" : "wait_skip";
}

export function modeFields(mode: PlayersMode): ModeFields {
  if (mode === "restart") return { only_when_empty: false, if_still_busy: "skip" };
  return { only_when_empty: true, if_still_busy: mode === "wait_restart" ? "restart" : "skip" };
}

export const SKIP_REASON: Record<string, string> = {
  players_online: "players were still online",
  recent_start: "the server had just started",
  restarted_meanwhile: "the server restarted while it waited",
};

/** Timeline text for a scheduled_restart event. */
export function restartEventText(data: Record<string, unknown>): string {
  if (data.outcome === "restarting") {
    const n = Number(data.players ?? 0);
    return n ? `restarting with ${n} player${n === 1 ? "" : "s"} online` : "restarting";
  }
  const why = SKIP_REASON[String(data.reason)];
  return why ? `skipped: ${why}` : "skipped";
}
