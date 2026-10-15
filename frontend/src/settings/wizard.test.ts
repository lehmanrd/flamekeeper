import { describe, expect, it } from "vitest";
import { initialState, stepErrors, toBody } from "./wizard";

const templates = {
  Admin: { canKickBan: true, canAccessInventories: true, canEditWorld: true, canEditBase: true, canExtendBase: true, reservedSlots: 0 },
  Guest: { canKickBan: false, canAccessInventories: false, canEditWorld: true, canEditBase: false, canExtendBase: false, reservedSlots: 0 },
};
let n = 0;
const pw = () => `pw-${++n}`;

describe("wizard", () => {
  it("starts with the default roles and generated passwords", () => {
    const s = initialState(templates, pw);
    expect(s.roles.map((r) => [r.name, r.password])).toEqual([["Admin", "pw-1"], ["Guest", "pw-2"]]);
    expect(s.roles[0].canKickBan).toBe(true);
  });

  it("validates each step", () => {
    const s = initialState(templates, pw);
    expect(stepErrors(s, 0, null)).toContain("Give the world a name.");
    const named = { ...s, name: "Frostmere", port: "15640" };
    expect(stepErrors(named, 0, true)).toEqual([]);
    expect(stepErrors(named, 0, false)).toEqual(["That port is taken."]);
    expect(stepErrors({ ...named, port: "80" }, 0, null)).toEqual(["Port: 1024–65535."]);
    const dupes = { ...named, roles: named.roles.map((r) => ({ ...r, password: "same" })) };
    expect(stepErrors(dupes, 2, null)).toEqual(["Fix the highlighted roles."]);
    expect(stepErrors({ ...named, imageTag: "bad tag" }, 3, null)).toEqual(["Image tag: letters, digits, _ . -"]);
  });

  it("builds the create body", () => {
    const s = { ...initialState(templates, pw), name: "Kids' Valley", preset: "Relaxed", backupCron: "", copyFrom: "abc" };
    const body = toBody(s);
    expect(body.slug).toBe("kids-valley");
    expect(body.query_port).toBeUndefined();
    expect(body.roles?.[0]).toMatchObject({ name: "Admin", password: expect.stringMatching(/^pw-/) });
    expect(body.backup).toEqual({ enabled: false, cron: "0 * * * *", retention_count: 48 });
    expect(body.copy_game_from).toBe("abc");
    expect(body.start).toBe(true);
    expect(toBody(s, false).start).toBe(false);
  });
});
