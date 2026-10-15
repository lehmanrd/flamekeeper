/**
 * Pure logic for the Roles editor: drafts, validation and password tools.
 */
import type { RoleInput, RoleView } from "../api/types";
import { WORDS } from "./words";

export const PERMISSIONS = [
  "canKickBan",
  "canAccessInventories",
  "canEditWorld",
  "canEditBase",
  "canExtendBase",
] as const;
export type Permission = (typeof PERMISSIONS)[number];

/**
 * A role in the editor. `password` is undefined while the saved password is kept
 * (it's only known after "Show"); `source` is the role's index in the saved file.
 */
export interface RoleDraft {
  id: string;
  name: string;
  source: number | null;
  password: string | undefined;
  /** Whether the saved password is non-empty (meaningful while password is undefined). */
  savedHasPassword: boolean;
  /** The saved password once revealed; sending it back unchanged counts as "keep". */
  savedPassword?: string;
  canKickBan: boolean;
  canAccessInventories: boolean;
  canEditWorld: boolean;
  canEditBase: boolean;
  canExtendBase: boolean;
  reservedSlots: number;
}

let nextId = 0;
const newId = () => `r${++nextId}`;

export function fromView(roles: RoleView[]): RoleDraft[] {
  return roles.map((r) => ({
    id: newId(),
    name: r.name,
    source: r.index,
    password: undefined,
    savedHasPassword: r.has_password,
    canKickBan: r.canKickBan,
    canAccessInventories: r.canAccessInventories,
    canEditWorld: r.canEditWorld,
    canEditBase: r.canEditBase,
    canExtendBase: r.canExtendBase,
    reservedSlots: r.reservedSlots,
  }));
}

export function toPayload(drafts: RoleDraft[]): RoleInput[] {
  return drafts.map((d) => {
    const base = {
      name: d.name.trim(),
      canKickBan: d.canKickBan,
      canAccessInventories: d.canAccessInventories,
      canEditWorld: d.canEditWorld,
      canEditBase: d.canEditBase,
      canExtendBase: d.canExtendBase,
      reservedSlots: d.reservedSlots,
    };
    if (d.source !== null && (d.password === undefined || d.password === d.savedPassword)) {
      return { ...base, keep_password_from: d.source };
    }
    return { ...base, password: d.password ?? "" };
  });
}

/** True when the drafts would change something compared to the saved roles. */
export function rolesDirty(drafts: RoleDraft[], saved: RoleView[]): boolean {
  return JSON.stringify(toPayload(drafts)) !== JSON.stringify(toPayload(fromView(saved)));
}

export function isOpen(d: RoleDraft): boolean {
  return d.password === undefined ? !d.savedHasPassword : d.password === "";
}

export function newRole(
  name: string,
  template: Record<string, boolean | number> | undefined,
  password: string,
): RoleDraft {
  return {
    id: newId(),
    name,
    source: null,
    password,
    savedHasPassword: false,
    canKickBan: Boolean(template?.canKickBan),
    canAccessInventories: Boolean(template?.canAccessInventories),
    canEditWorld: Boolean(template?.canEditWorld),
    canEditBase: Boolean(template?.canEditBase),
    canExtendBase: Boolean(template?.canExtendBase),
    reservedSlots: Number(template?.reservedSlots ?? 0),
  };
}

/** A name not used yet: "Friend", "Friend 2", … */
export function uniqueName(base: string, drafts: RoleDraft[]): string {
  const taken = new Set(drafts.map((d) => d.name.trim().toLowerCase()));
  if (!taken.has(base.toLowerCase())) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base} ${i}`.toLowerCase())) return `${base} ${i}`;
}

export interface RoleProblems {
  /** Keyed by draft id, then field ("name", "password", "reservedSlots"). */
  fields: Record<string, Partial<Record<"name" | "password" | "reservedSlots", string>>>;
  general: string[];
  warnings: string[];
}

/**
 * Validation: unique names; unique non-empty passwords; at most one open role
 * (warn when there is one); reserved slots ≤ max players; at least one role.
 * Saved passwords the editor hasn't seen can't be compared here; the server checks them.
 */
export function validateRoles(drafts: RoleDraft[], slotCount: number): RoleProblems {
  const p: RoleProblems = { fields: {}, general: [], warnings: [] };
  const set = (id: string, field: "name" | "password" | "reservedSlots", msg: string) => {
    p.fields[id] = { ...p.fields[id], [field]: p.fields[id]?.[field] ?? msg };
  };
  if (drafts.length === 0) p.general.push("At least one role is required.");
  const names = new Map<string, string>();
  const passwords = new Map<string, string>();
  const open: RoleDraft[] = [];
  let reserved = 0;
  for (const d of drafts) {
    const name = d.name.trim();
    if (!name) set(d.id, "name", "Name is required");
    else if (name.length > 32) set(d.id, "name", "At most 32 characters");
    else if (names.has(name.toLowerCase())) set(d.id, "name", `Another role is named ${names.get(name.toLowerCase())}`);
    else names.set(name.toLowerCase(), name);

    if (isOpen(d)) open.push(d);
    else if (d.password !== undefined) {
      if (d.password.length > 64) set(d.id, "password", "At most 64 characters");
      const other = passwords.get(d.password);
      if (other !== undefined) set(d.id, "password", `Same password as ${other}`);
      else passwords.set(d.password, name || "another role");
    }

    if (!Number.isInteger(d.reservedSlots) || d.reservedSlots < 0 || d.reservedSlots > 16) {
      set(d.id, "reservedSlots", "0–16");
    } else reserved += d.reservedSlots;
  }
  if (open.length > 1) {
    for (const d of open.slice(1)) set(d.id, "password", "Only one role can be open");
  } else if (open.length === 1) {
    p.warnings.push(`Open: anyone can join as ${open[0].name.trim() || "this role"} without a password.`);
  }
  if (reserved > slotCount) {
    p.general.push(`Reserved slots add up to ${reserved}, more than the ${slotCount} max players.`);
  }
  return p;
}

export function hasErrors(p: RoleProblems): boolean {
  return p.general.length > 0 || Object.values(p.fields).some((f) => Object.keys(f).length > 0);
}

/* ---------- password generator ---------- */

function randomInt(maxExclusive: number): number {
  // Rejection sampling for an unbiased pick.
  const buf = new Uint32Array(1);
  const limit = Math.floor(0x1_0000_0000 / maxExclusive) * maxExclusive;
  do crypto.getRandomValues(buf);
  while (buf[0] >= limit);
  return buf[0] % maxExclusive;
}

/** Four random words: "ember-otter-lantern-moss". */
export function generateWords(count = 4): string {
  return Array.from({ length: count }, () => WORDS[randomInt(WORDS.length)]).join("-");
}

const CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";

/** 16 characters without look-alikes (no 0/O, 1/l/I). */
export function generateChars(length = 16): string {
  return Array.from({ length }, () => CHARS[randomInt(CHARS.length)]).join("");
}

export function generatePassword(kind: "words" | "chars"): string {
  return kind === "words" ? generateWords() : generateChars();
}

/** Text for "Copy join info". */
export function joinInfo(serverName: string, host: string, port: number, role: string, password: string): string {
  return [
    `Enshrouded server: ${serverName}`,
    `Find it in the server list, or join by IP: ${host}:${port}`,
    password ? `Password (${role}): ${password}` : `No password needed (${role}).`,
  ].join("\n");
}
