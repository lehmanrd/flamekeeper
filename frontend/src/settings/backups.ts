/** Backups page logic: schedule presets and labels. */
import type { BackupItem, BackupKind } from "../api/types";

export const SCHEDULE_PRESETS: { cron: string; label: string }[] = [
  { cron: "*/30 * * * *", label: "Every 30 minutes" },
  { cron: "0 * * * *", label: "Every hour" },
  { cron: "0 */3 * * *", label: "Every 3 hours" },
  { cron: "0 */6 * * *", label: "Every 6 hours" },
  { cron: "0 4 * * *", label: "Daily at 04:00" },
];

/** The preset matching a cron expression, or "custom". */
export function presetFor(cron: string): string {
  const norm = cron.trim().replace(/\s+/g, " ");
  return SCHEDULE_PRESETS.find((p) => p.cron === norm)?.cron ?? "custom";
}

export const KIND_LABEL: Record<BackupKind, string> = {
  scheduled: "scheduled",
  manual: "manual",
  "pre-update": "before update",
  "pre-restore": "before restore",
  "pre-image-update": "before image update",
  uploaded: "uploaded",
  image: "image",
  imported: "imported",
};

export const KIND_TONE: Record<BackupKind, "ok" | "info" | "warn" | "off"> = {
  scheduled: "info",
  manual: "ok",
  "pre-update": "warn",
  "pre-restore": "warn",
  "pre-image-update": "warn",
  uploaded: "ok",
  image: "off",
  imported: "ok",
};

export function totalSize(backups: BackupItem[]): number {
  return backups.reduce((n, b) => n + b.size_bytes, 0);
}

/** Keep-count summary: how many scheduled backups exist vs. are kept. */
export function retentionText(backups: BackupItem[], keep: number): string {
  const scheduled = backups.filter((b) => b.kind === "scheduled").length;
  return keep > 0 ? `${scheduled} of ${keep} scheduled backups kept` : `${scheduled} scheduled backups (all kept)`;
}
