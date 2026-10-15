/** New-world wizard: state, per-step validation and the create body. */
import type { CreateWorldBody, UpdatePolicyName } from "../api/types";
import { SLUG_RE, slugify } from "../format";
import { hasErrors, newRole, toPayload, validateRoles, type RoleDraft } from "./roles";

export const STEPS = ["Basics", "Difficulty", "Roles", "Image & schedules", "Review"] as const;

export interface WizardState {
  name: string;
  slug: string;
  slugEdited: boolean;
  slots: number;
  port: string; // "" = suggested
  preset: string;
  roles: RoleDraft[];
  imageTag: string;
  branch: string;
  updatePolicy: UpdatePolicyName;
  backupCron: string; // "" = off
  keep: number;
  copyFrom: string; // world id, "" = download
}

export function initialState(templates: Record<string, Record<string, boolean | number>>, password: () => string, imageTag = "stable-wine"): WizardState {
  return {
    name: "",
    slug: "",
    slugEdited: false,
    slots: 16,
    port: "",
    preset: "Default",
    roles: Object.entries(templates).map(([name, t]) => newRole(name, t, password())),
    imageTag,
    branch: "public",
    updatePolicy: "when_empty",
    backupCron: "0 * * * *",
    keep: 48,
    copyFrom: "",
  };
}

export function effectiveSlug(s: WizardState): string {
  return s.slugEdited ? s.slug : slugify(s.name);
}

/** Problems that block leaving a step (index into STEPS). */
export function stepErrors(s: WizardState, step: number, portFree: boolean | null): string[] {
  const errors: string[] = [];
  if (step === 0) {
    if (!s.name.trim()) errors.push("Give the world a name.");
    if (!SLUG_RE.test(effectiveSlug(s))) errors.push("The slug may use a–z, 0–9 and '-' (up to 40).");
    if (!(Number.isInteger(s.slots) && s.slots >= 1 && s.slots <= 16)) errors.push("Max players: 1–16.");
    if (s.port) {
      const p = Number(s.port);
      if (!(Number.isInteger(p) && p >= 1024 && p <= 65535)) errors.push("Port: 1024–65535.");
      else if (portFree === false) errors.push("That port is taken.");
    }
  }
  if (step === 2) {
    const p = validateRoles(s.roles, s.slots);
    if (hasErrors(p)) errors.push(...(p.general.length ? p.general : ["Fix the highlighted roles."]));
  }
  if (step === 3) {
    if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(s.imageTag)) errors.push("Image tag: letters, digits, _ . -");
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(s.branch)) errors.push("Steam branch: letters, digits, _ . -");
    if (s.backupCron && !(Number.isInteger(s.keep) && s.keep >= 0)) errors.push("Keep: 0 or more.");
  }
  return errors;
}

/** `start`: start right away, or leave it stopped so its settings can be edited first. */
export function toBody(s: WizardState, start = true): CreateWorldBody {
  return {
    display_name: s.name.trim(),
    slug: effectiveSlug(s),
    slot_count: s.slots,
    query_port: s.port ? Number(s.port) : undefined,
    preset: s.preset,
    roles: toPayload(s.roles),
    image_tag: s.imageTag,
    game_branch: s.branch,
    update_policy: s.updatePolicy,
    backup: s.backupCron
      ? { enabled: true, cron: s.backupCron, retention_count: s.keep }
      : { enabled: false, cron: "0 * * * *", retention_count: s.keep },
    start,
    copy_game_from: s.copyFrom || undefined,
  };
}
