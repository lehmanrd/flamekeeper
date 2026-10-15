import { describe, expect, it } from "vitest";
import {
  customize,
  dependencyOff,
  difficultyPending,
  matchingPreset,
  draftChanges,
  pendingState,
  editCustom,
  formatMinutes,
  formatValue,
  fromInput,
  inputStep,
  minutesToNs,
  nsToMinutes,
  presetValue,
  rangeText,
  selectPreset,
  toInput,
  validateValue,
} from "./difficulty";
import type { SettingDef } from "./schema";

const damage: SettingDef = {
  key: "enemyDamageFactor", label: "Enemy Damage", tab: "enemies", type: "factor", display: "percent",
  min: 0.25, max: 5, step: 0.05, default: 1,
  presets: { Default: 1, Relaxed: 0.5, Hard: 1.5, Survival: 1.5 }, description: "",
};
const starving: SettingDef = {
  key: "enableStarvingDebuff", label: "Starvation Mode", tab: "survival", type: "bool", default: false,
  presets: { Default: false, Relaxed: false, Hard: false, Survival: true }, description: "",
};
const toStarving: SettingDef = {
  key: "fromHungerToStarving", label: "Time until starvation", tab: "survival", type: "duration_ns",
  display: "minutes", min: 300_000_000_000, max: 1_200_000_000_000, step: 60_000_000_000,
  default: 600_000_000_000, dependsOn: { key: "enableStarvingDebuff", equals: true },
  presets: { Default: 600_000_000_000, Relaxed: null, Hard: 600_000_000_000, Survival: 600_000_000_000 },
  description: "",
};
const spawner: SettingDef = {
  key: "randomSpawnerAmount", label: "Enemy Amount", tab: "enemies", type: "enum", default: "Normal",
  options: [
    { value: "Few", label: "Few" }, { value: "Normal", label: "Normal" },
    { value: "Many", label: "More" }, { value: "Extreme", label: "Many" },
  ],
  presets: { Default: "Normal", Relaxed: "Few", Hard: "Many", Survival: "Many" }, description: "",
};
const flameLevel: SettingDef = {
  key: "flameLevelFogRemoval", label: "Flame Level for Altars in the Shroud", tab: "survival", type: "int",
  display: "raw", min: 0, max: 20, step: 1, default: 11,
  presets: { Default: 11, Relaxed: 11, Hard: 11, Survival: 11 }, description: "",
};
const all = [damage, starving, toStarving, spawner];
const byKey = Object.fromEntries(all.map((s) => [s.key, s]));

describe("ns ↔ minutes", () => {
  it("converts both ways", () => {
    expect(nsToMinutes(3_600_000_000_000)).toBe(60);
    expect(minutesToNs(12)).toBe(720_000_000_000);
    expect(minutesToNs(nsToMinutes(1_800_000_000_000))).toBe(1_800_000_000_000);
  });
  it("formats like the game", () => {
    expect(formatMinutes(60)).toBe("1h");
    expect(formatMinutes(30)).toBe("30min");
    expect(formatMinutes(90)).toBe("1h 30min");
    expect(formatMinutes(120)).toBe("2h");
  });
});

describe("formatValue", () => {
  it("matches the in-game labels", () => {
    expect(formatValue(damage, 2)).toBe("200%");
    expect(formatValue(starving, false)).toBe("Off");
    expect(formatValue(toStarving, 600_000_000_000)).toBe("10min");
    expect(formatValue(spawner, "Extreme")).toBe("Many");
    expect(formatValue(spawner, "Many")).toBe("More");
    expect(formatValue(damage, null)).toBe("?");
  });
  it("describes ranges", () => {
    expect(rangeText(damage)).toBe("25% – 500%");
    expect(rangeText(toStarving)).toBe("5min – 20min");
    expect(rangeText(spawner)).toBe("Few · Normal · More · Many");
  });
});

describe("inputs", () => {
  it("round-trips percent and minutes without float noise", () => {
    expect(toInput(damage, 1.15)).toBe(115);
    expect(fromInput(damage, 115)).toBe(1.15);
    expect(fromInput(damage, 33)).toBe(0.33);
    expect(toInput(toStarving, 600_000_000_000)).toBe(10);
    expect(fromInput(toStarving, 15)).toBe(900_000_000_000);
    expect(inputStep(damage)).toBe(5);
    expect(inputStep(toStarving)).toBe(1);
    expect(toInput(flameLevel, 11)).toBe(11);
    expect(fromInput(flameLevel, 12)).toBe(12);
    expect(inputStep(flameLevel)).toBe(1);
  });
  it("validates like the server", () => {
    expect(validateValue(damage, 2)).toBeNull();
    expect(validateValue(damage, 6)).toBe("Allowed: 25% – 500%");
    expect(validateValue(damage, Number.NaN)).toBe("Enter a number");
    expect(validateValue(spawner, "More")).toMatch(/Unknown value/);
    expect(validateValue(starving, 1)).toBe("Must be on or off");
    expect(validateValue(flameLevel, 20)).toBeNull();
    expect(validateValue(flameLevel, 21)).toBe("Allowed: 0 – 20");
    expect(validateValue(flameLevel, 2.5)).toBe("Must be a whole number");
  });
});

describe("presets", () => {
  const draft = { preset: "Hard" as const, custom: { enemyDamageFactor: 1, enableStarvingDebuff: false,
    fromHungerToStarving: 600_000_000_000, randomSpawnerAmount: "Normal" } };

  it("shows preset values, ? for unknown, and — when a dependency is off", () => {
    expect(presetValue(damage, "Relaxed", draft.custom)).toBe(0.5);
    expect(presetValue(toStarving, "Relaxed", draft.custom)).toBeNull();
    expect(dependencyOff(toStarving, "Hard", draft.custom, byKey)).toBe(true);
    expect(dependencyOff(toStarving, "Survival", draft.custom, byKey)).toBe(false);
    expect(dependencyOff(toStarving, "Custom", draft.custom, byKey)).toBe(true);
  });

  it("choosing Custom copies the selected preset's known values", () => {
    const next = selectPreset(all, draft, "Custom");
    expect(next.preset).toBe("Custom");
    expect(next.custom.enemyDamageFactor).toBe(1.5);
    expect(next.custom.randomSpawnerAmount).toBe("Many");
    expect(draftChanges(draft, next)).toBe(3);
  });

  it("unknown preset values keep the current Custom value", () => {
    const relaxed = { ...draft, preset: "Relaxed" as const, custom: { ...draft.custom, fromHungerToStarving: 900_000_000_000 } };
    expect(selectPreset(all, relaxed, "Custom").custom.fromHungerToStarving).toBe(900_000_000_000);
  });

  it("choosing another preset keeps Custom values; editing a cell switches to Custom", () => {
    const relaxed = selectPreset(all, draft, "Relaxed");
    expect(relaxed).toEqual({ ...draft, preset: "Relaxed" });
    const edited = editCustom(relaxed, "enemyDamageFactor", 2);
    expect(edited.preset).toBe("Custom");
    expect(edited.custom.enemyDamageFactor).toBe(2);
  });

  it("tells unsaved edits from saved changes waiting for a restart", () => {
    expect(pendingState(1, 1, 1)).toBeNull();
    expect(pendingState(2, 1, 1)).toBe("unsaved");
    expect(pendingState(2, 2, 1)).toBe("restart");
    // Editing back to the running value still needs a save to cancel the pending one.
    expect(pendingState(1, 2, 1)).toBe("unsaved");
  });

  it("customizing starts from the chosen preset, or keeps the previous custom values", () => {
    const fromRelaxed = customize(all, draft, "Relaxed");
    expect(fromRelaxed.preset).toBe("Custom");
    expect(fromRelaxed.custom.enemyDamageFactor).toBe(0.5);
    expect(matchingPreset(all, fromRelaxed.custom)).toBe("Relaxed");
    const own = editCustom(fromRelaxed, "enemyDamageFactor", 2);
    expect(matchingPreset(all, own.custom)).toBeNull();
    expect(customize(all, own, "Custom")).toEqual(own);
  });

  it("preset buttons ignore custom values unless Custom is selected", () => {
    const hard = { preset: "Hard" as const, custom: { enemyDamageFactor: 1 } };
    const hardOtherValues = { ...hard, custom: { enemyDamageFactor: 3 } };
    expect(difficultyPending(hardOtherValues, hard, hard)).toBeNull();
    expect(difficultyPending({ ...hard, preset: "Default" }, hard, hard)).toBe("unsaved");
    const custom = { preset: "Custom" as const, custom: { enemyDamageFactor: 3 } };
    expect(difficultyPending(custom, custom, { ...custom, custom: { enemyDamageFactor: 2 } })).toBe("restart");
    expect(difficultyPending(custom, { ...custom, custom: { enemyDamageFactor: 2 } }, custom)).toBe("unsaved");
  });
});
