import type { LogLine, WorldState } from "./api/types";

export const STATE_LABEL: Record<WorldState, string> = {
  stopped: "Stopped",
  starting: "Starting",
  updating: "Updating",
  online: "Online",
  unresponsive: "Unresponsive",
  crashed: "Crashed",
  missing: "Container missing",
};

export const STATE_TONE: Record<WorldState, "ok" | "warn" | "bad" | "off" | "info"> = {
  stopped: "off",
  starting: "info",
  updating: "info",
  online: "ok",
  unresponsive: "warn",
  crashed: "bad",
  missing: "warn",
};

export function isRunningState(state: WorldState): boolean {
  return state === "online" || state === "starting" || state === "updating" || state === "unresponsive";
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** "3d 4h", "2h 11m", "38m", "<1m". */
export function formatDuration(ms: number): string {
  if (ms < 60_000) return "<1m";
  const m = Math.floor(ms / 60_000);
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  const mm = m % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${mm}m`;
  return `${mm}m`;
}

export function since(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  return formatDuration(now - new Date(iso).getTime());
}

export function formatTime(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) +
        " " +
        d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function slugify(name: string): string {
  return (
    name
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .replace(/[^a-zA-Z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .toLowerCase()
      .slice(0, 40)
      .replace(/-+$/g, "") || "world"
  );
}

export const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/;

export type LevelFilter = "all" | "warn" | "error" | "players";

export function filterLines(lines: LogLine[], query: string, level: LevelFilter): LogLine[] {
  const q = query.trim().toLowerCase();
  return lines.filter((l) => {
    if (level === "error" && l.level !== "error") return false;
    if (level === "warn" && l.level === "info") return false;
    if (level === "players" && !l.player) return false;
    return !q || l.text.toLowerCase().includes(q) || l.source.toLowerCase().includes(q);
  });
}

export const JOB_LABEL: Record<string, string> = {
  create: "Create world",
  start: "Start",
  stop: "Stop",
  restart: "Restart",
  recreate: "Recreate container",
  delete: "Delete world",
  update: "Game update",
  image_update: "Image update",
  backup: "Backup",
  restore: "Restore",
  clone: "Clone",
  image_pull: "Pull image",
};
