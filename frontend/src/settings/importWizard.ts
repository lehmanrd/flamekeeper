/** Import wizard: state, per-step validation and the create body. */
import type { ImportCreateBody, ImportScan } from "../api/types";
import { SLUG_RE, slugify } from "../format";

export const IMPORT_STEPS = ["Source", "What we found", "Settings", "Confirm"] as const;

/** copyFrom value: copy game files from the server being imported. */
export const FROM_SOURCE = "@source";

export type SourceKind = "container" | "host_path" | "upload";

export interface ImportState {
  kind: SourceKind;
  container: string; // container id
  path: string;
  name: string;
  slug: string;
  slugEdited: boolean;
  port: string;
  savePath: string;
  imageTag: string;
  copyFrom: string; // world id, FROM_SOURCE = the old server, "" = download
  backupCron: string; // "" = off
  keep: number;
  confirmPlayers: boolean;
}

export function initialImportState(imageTag = "stable-wine"): ImportState {
  return {
    kind: "container",
    container: "",
    path: "",
    name: "",
    slug: "",
    slugEdited: false,
    port: "",
    savePath: "",
    imageTag,
    copyFrom: "",
    backupCron: "0 * * * *",
    keep: 48,
    confirmPlayers: false,
  };
}

/** Defaults taken from a scan: the old server's name, port and save folder. */
export function fromScan(s: ImportState, scan: ImportScan): ImportState {
  const fallback = scan.source.kind === "container" ? scan.source.container_name : "Imported world";
  return {
    ...s,
    name: (scan.summary.name ?? fallback).slice(0, 100),
    slugEdited: false,
    port: String(scan.port),
    savePath: scan.save_path ?? "",
    // Copying the old server's game files saves a 9 GB download (and downtime).
    copyFrom: scan.game_files ? FROM_SOURCE : s.copyFrom === FROM_SOURCE ? "" : s.copyFrom,
    confirmPlayers: false,
  };
}

export function importSlug(s: ImportState): string {
  return s.slugEdited ? s.slug : slugify(s.name);
}

/** Does the import stop a running server? Then the admin confirms unless nobody is online. */
export function needsPlayerConfirm(scan: ImportScan): boolean {
  return scan.source.kind === "container" && scan.running && scan.players_online !== 0;
}

/** The port check can ignore the old server's own port: it is stopped first. */
export function portNeedsCheck(s: ImportState, scan: ImportScan | null): boolean {
  return Boolean(s.port) && Number(s.port) !== scan?.reusable_port;
}

/** Problems that block leaving a step (index into IMPORT_STEPS). */
export function importStepErrors(s: ImportState, step: number, scan: ImportScan | null, portFree: boolean | null): string[] {
  const errors: string[] = [];
  if (step === 1 && scan?.errors.length) errors.push("Fix the problems in the old server's config first.");
  if (step === 2) {
    if (!s.name.trim()) errors.push("Give the world a name.");
    if (!SLUG_RE.test(importSlug(s))) errors.push("The slug may use a–z, 0–9 and '-' (up to 40).");
    const p = Number(s.port);
    if (!s.port || !(Number.isInteger(p) && p >= 1024 && p <= 65535)) errors.push("Port: 1024–65535.");
    else if (portNeedsCheck(s, scan) && portFree === false) errors.push("That port is taken.");
    if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(s.imageTag)) errors.push("Image tag: letters, digits, _ . -");
    if (s.backupCron && !(Number.isInteger(s.keep) && s.keep >= 0)) errors.push("Keep: 0 or more.");
  }
  if (step === 3 && scan && needsPlayerConfirm(scan) && !s.confirmPlayers) {
    errors.push("Confirm that the old server may be stopped.");
  }
  return errors;
}

export function importBody(s: ImportState, scan: ImportScan | null, start = true): ImportCreateBody {
  return {
    display_name: s.name.trim(),
    slug: importSlug(s),
    query_port: s.port ? Number(s.port) : undefined,
    image_tag: s.imageTag,
    backup: s.backupCron
      ? { enabled: true, cron: s.backupCron, retention_count: s.keep }
      : { enabled: false, cron: "0 * * * *", retention_count: s.keep },
    start,
    copy_game_from: s.copyFrom && s.copyFrom !== FROM_SOURCE ? s.copyFrom : undefined,
    copy_game_from_source: s.copyFrom === FROM_SOURCE,
    save_path: s.savePath && s.savePath !== scan?.save_path ? s.savePath : undefined,
    confirm_players: s.confirmPlayers,
  };
}
