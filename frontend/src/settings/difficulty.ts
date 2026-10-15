/**
 * Pure logic for the Difficulty editor: formatting, unit conversion,
 * presets, dependsOn and validation. Everything is driven by the settings schema.
 */
import type { Preset, SettingValue } from "../api/types";
import type { SettingDef } from "./schema";

export const NS_PER_MINUTE = 60_000_000_000;
/** The game's built-in presets, in the game's order; Custom comes after them. */
export const NAMED_PRESETS: Preset[] = ["Default", "Relaxed", "Hard", "Survival"];

export type Values = Record<string, SettingValue>;

export const nsToMinutes = (ns: number): number => ns / NS_PER_MINUTE;
export const minutesToNs = (min: number): number => Math.round(min * NS_PER_MINUTE);

/** "1h", "1h 30min", "45min" (like the game). */
export function formatMinutes(min: number): string {
  const rounded = Math.round(min * 10) / 10;
  if (rounded >= 60 && rounded % 60 === 0) return `${rounded / 60}h`;
  if (rounded > 60) return `${Math.floor(rounded / 60)}h ${Math.round(rounded % 60)}min`;
  return `${rounded}min`;
}

/** How the game shows a value: "200%", "On", "1h", "Lose Some Progress". `null` → "?". */
export function formatValue(s: SettingDef, v: SettingValue | null | undefined): string {
  if (v === null || v === undefined) return "?";
  if (s.type === "bool") return v ? "On" : "Off";
  if (s.type === "enum") return s.options?.find((o) => o.value === v)?.label ?? String(v);
  if (typeof v !== "number") return String(v);
  if (s.display === "percent") return `${Math.round(v * 100)}%`;
  if (s.type === "duration_ns") return formatMinutes(nsToMinutes(v));
  return String(v);
}

/** The number shown in a Custom input: percent for factors, minutes for durations. */
export function toInput(s: SettingDef, v: SettingValue): number {
  if (typeof v !== "number") return NaN;
  if (s.display === "percent") return Math.round(v * 10000) / 100;
  if (s.type === "duration_ns") return Math.round(nsToMinutes(v) * 100) / 100;
  return v;
}

/** Inverse of toInput. */
export function fromInput(s: SettingDef, input: number): number {
  if (s.display === "percent") return Math.round(input * 100) / 10000;
  if (s.type === "duration_ns") return minutesToNs(input);
  return input;
}

/** Step for the stepper, in input units (5% for factors, 1 min for durations). */
export function inputStep(s: SettingDef): number {
  if (s.display === "percent") return Math.round((s.step ?? 0.05) * 100);
  if (s.type === "duration_ns") return (s.step ?? NS_PER_MINUTE) / NS_PER_MINUTE;
  return s.step ?? 1;
}

export function clamp(s: SettingDef, v: number): number {
  let out = v;
  if (s.min !== undefined) out = Math.max(s.min, out);
  if (s.max !== undefined) out = Math.min(s.max, out);
  return out;
}

/** Range text for the description panel: "25% – 500%", "Few · Normal · …", "On / Off". */
export function rangeText(s: SettingDef): string {
  if (s.type === "bool") return "On / Off";
  if (s.type === "enum") return (s.options ?? []).map((o) => o.label).join(" · ");
  if (s.min === undefined || s.max === undefined) return "—";
  return `${formatValue(s, s.min)} – ${formatValue(s, s.max)}`;
}

/** Client-side check matching the server's (enums strictly, numbers by range). */
export function validateValue(s: SettingDef, v: unknown): string | null {
  if (s.type === "bool") return typeof v === "boolean" ? null : "Must be on or off";
  if (s.type === "enum") {
    return s.options?.some((o) => o.value === v) ? null : `Unknown value ${String(v)}`;
  }
  if (typeof v !== "number" || Number.isNaN(v)) return "Enter a number";
  if (s.type === "duration_ns" && !Number.isInteger(v)) return "Must be whole nanoseconds";
  if ((s.min !== undefined && v < s.min) || (s.max !== undefined && v > s.max)) {
    return `Allowed: ${rangeText(s)}`;
  }
  return null;
}

/** Values a preset column shows; null when the schema has no value for that preset. */
export function presetValue(s: SettingDef, preset: Preset, custom: Values): SettingValue | null {
  if (preset === "Custom") return custom[s.key] ?? s.default;
  const v = s.presets[preset];
  return v === undefined ? null : v;
}

/** True when the setting's dependency is off in this column (the game shows "—"). */
export function dependencyOff(
  s: SettingDef,
  preset: Preset,
  custom: Values,
  byKey: Record<string, SettingDef>,
): boolean {
  if (!s.dependsOn) return false;
  const dep = byKey[s.dependsOn.key];
  if (!dep) return false;
  return presetValue(dep, preset, custom) !== s.dependsOn.equals;
}

/**
 * Choosing Custom while another preset is active copies that preset's known values in
 * as a starting point; unknown (null) preset values keep the current Custom value.
 */
export function customFromPreset(settings: SettingDef[], preset: Preset, custom: Values): Values {
  if (preset === "Custom") return custom;
  const out: Values = { ...custom };
  for (const s of settings) {
    const v = s.presets[preset];
    if (v !== null && v !== undefined) out[s.key] = v;
  }
  return out;
}

/**
 * Where a value stands: "unsaved" when the editor differs from the saved revision,
 * "restart" when it's saved but the running server still has another value.
 */
export type PendingState = "unsaved" | "restart" | null;
export function pendingState<T>(draft: T, saved: T, applied: T): PendingState {
  if (draft !== saved) return "unsaved";
  return saved !== applied ? "restart" : null;
}

/** Keys whose value differs between two value maps. */
export function changedKeys(a: Values, b: Values): string[] {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].filter((k) => a[k] !== b[k]);
}

/** Editor state: active preset plus the Custom column's values. */
export interface DifficultyDraft {
  preset: Preset;
  custom: Values;
}

export function draftChanges(saved: DifficultyDraft, draft: DifficultyDraft): number {
  return changedKeys(saved.custom, draft.custom).length + (saved.preset !== draft.preset ? 1 : 0);
}

/** Select a preset; choosing Custom starts from the current preset's values. */
export function selectPreset(settings: SettingDef[], draft: DifficultyDraft, preset: Preset): DifficultyDraft {
  if (preset === draft.preset) return draft;
  if (preset === "Custom") {
    return { preset, custom: customFromPreset(settings, draft.preset, draft.custom) };
  }
  return { ...draft, preset };
}

/** Switch to Custom, starting from a preset's values ("Custom" keeps the current values). */
export function customize(settings: SettingDef[], draft: DifficultyDraft, baseline: Preset): DifficultyDraft {
  return { preset: "Custom", custom: customFromPreset(settings, baseline, draft.custom) };
}

/** The built-in preset whose values all match `custom`, if any. */
export function matchingPreset(settings: SettingDef[], custom: Values): Preset | null {
  return NAMED_PRESETS.find((p) => settings.every((s) => s.presets[p] == null || s.presets[p] === custom[s.key])) ?? null;
}

/**
 * Pending state of the whole difficulty (for the preset buttons). A built-in preset
 * ignores the custom values, so they only count while Custom is selected.
 */
export function difficultyPending(draft: DifficultyDraft, saved: DifficultyDraft, applied: DifficultyDraft): PendingState {
  const differs = (a: DifficultyDraft, b: DifficultyDraft) =>
    a.preset !== b.preset || (a.preset === "Custom" && changedKeys(a.custom, b.custom).length > 0);
  if (differs(draft, saved)) return "unsaved";
  return differs(saved, applied) ? "restart" : null;
}

/** Editing any Custom cell switches the world to Custom. */
export function editCustom(draft: DifficultyDraft, key: string, value: SettingValue): DifficultyDraft {
  return { preset: "Custom", custom: { ...draft.custom, [key]: value } };
}
