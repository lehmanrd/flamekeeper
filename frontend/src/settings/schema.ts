import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Preset, SettingValue } from "../api/types";

/** Types for settings-schema.json (served by GET /api/settings-schema). */

export interface EnumOption {
  value: string;
  label: string;
}

export interface SettingDef {
  key: string;
  env?: string;
  label: string;
  tab: string;
  type: "factor" | "enum" | "bool" | "duration_ns";
  display?: "percent" | "minutes" | "raw";
  min?: number;
  max?: number;
  step?: number;
  default: SettingValue;
  options?: EnumOption[];
  presets: Partial<Record<Preset, SettingValue | null>>;
  description: string;
  dependsOn?: { key: string; equals: SettingValue };
  aliases?: string[];
}

export interface TabDef {
  id: string;
  label: string;
}

export interface ServerFieldDef {
  key: string;
  label: string;
  type: string;
  min?: number;
  max?: number;
  default: unknown;
  options?: string[];
  description?: string;
}

export interface RoleFieldDef {
  key: string;
  type: string;
  label: string;
  description?: string;
  min?: number;
  max?: number;
}

export interface SettingsSchema {
  schemaVersion: number;
  presets: Preset[];
  tabs: TabDef[];
  settings: SettingDef[];
  roles: {
    fields: RoleFieldDef[];
    templates: Record<string, Record<string, boolean | number>>;
  };
  server: ServerFieldDef[];
  presetDescriptions: Record<string, string>;
}

export function useSchema() {
  return useQuery({ queryKey: ["settings-schema"], queryFn: api.settingsSchema, staleTime: Infinity });
}
