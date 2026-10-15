import { describe, expect, it } from "vitest";
import type { RoleView } from "../api/types";
import {
  fromView,
  generateChars,
  generateWords,
  hasErrors,
  isOpen,
  joinInfo,
  newRole,
  rolesDirty,
  toPayload,
  uniqueName,
  validateRoles,
} from "./roles";
import { WORDS } from "./words";

const view = (index: number, name: string, has_password = true): RoleView => ({
  index, name, has_password, canKickBan: false, canAccessInventories: false, canEditWorld: true,
  canEditBase: false, canExtendBase: false, reservedSlots: 0,
});

describe("role drafts", () => {
  it("keeps unseen passwords by index and sends new ones", () => {
    const drafts = fromView([view(0, "Admin"), view(1, "Guest")]);
    const reordered = [drafts[1], { ...drafts[0], password: "new-secret" }, newRole("Builder", { canEditBase: true }, "pw")];
    expect(toPayload(reordered)).toEqual([
      expect.objectContaining({ name: "Guest", keep_password_from: 1 }),
      expect.objectContaining({ name: "Admin", password: "new-secret" }),
      expect.objectContaining({ name: "Builder", password: "pw", canEditBase: true, canKickBan: false }),
    ]);
    expect(toPayload(reordered)[0]).not.toHaveProperty("password");
  });

  it("a revealed but unchanged password is not an edit", () => {
    const saved = [view(0, "Admin"), view(1, "Guest")];
    const drafts = fromView(saved);
    expect(rolesDirty(drafts, saved)).toBe(false);
    drafts[0] = { ...drafts[0], password: "AdminX1", savedPassword: "AdminX1" };
    expect(rolesDirty(drafts, saved)).toBe(false);
    drafts[0] = { ...drafts[0], password: "AdminX2" };
    expect(rolesDirty(drafts, saved)).toBe(true);
    expect(rolesDirty([drafts[1]], saved)).toBe(true);
  });

  it("knows open roles before and after editing", () => {
    const [saved] = fromView([view(0, "Visitor", false)]);
    expect(isOpen(saved)).toBe(true);
    expect(isOpen({ ...saved, password: "x" })).toBe(false);
  });

  it("picks unused names", () => {
    const drafts = fromView([view(0, "Friend"), view(1, "Friend 2")]);
    expect(uniqueName("Friend", drafts)).toBe("Friend 3");
    expect(uniqueName("Guest", drafts)).toBe("Guest");
  });
});

describe("validateRoles", () => {
  it("accepts the defaults", () => {
    const p = validateRoles(fromView([view(0, "Admin"), view(1, "Friend")]), 16);
    expect(hasErrors(p)).toBe(false);
    expect(p.warnings).toEqual([]);
  });

  it("flags duplicate names, duplicate passwords and a second open role", () => {
    const drafts = [
      newRole("Admin", undefined, "same"),
      newRole("admin", undefined, "same"),
      newRole("Open1", undefined, ""),
      newRole("Open2", undefined, ""),
    ];
    const p = validateRoles(drafts, 16);
    expect(p.fields[drafts[1].id]).toEqual({ name: "Another role is named Admin", password: "Same password as Admin" });
    expect(p.fields[drafts[3].id]?.password).toBe("Only one role can be open");
    expect(hasErrors(p)).toBe(true);
  });

  it("warns about one open role and checks reserved slots", () => {
    const visitor = { ...newRole("Visitor", undefined, ""), reservedSlots: 3 };
    const p = validateRoles([visitor, { ...newRole("Admin", undefined, "a"), reservedSlots: 2 }], 4);
    expect(p.warnings).toEqual(["Open: anyone can join as Visitor without a password."]);
    expect(p.general).toEqual(["Reserved slots add up to 5, more than the 4 max players."]);
    expect(validateRoles([], 16).general).toEqual(["At least one role is required."]);
  });
});

describe("passwords", () => {
  it("generates four words or 16 unambiguous characters", () => {
    const words = generateWords().split("-");
    expect(words).toHaveLength(4);
    words.forEach((w) => expect(WORDS).toContain(w));
    const chars = generateChars();
    expect(chars).toMatch(/^[A-HJ-NP-Za-km-z2-9]{16}$/);
    expect(new Set(WORDS).size).toBe(256);
  });

  it("formats join info", () => {
    expect(joinInfo("Embervale", "192.168.1.5", 15637, "Friend", "ember-otter")).toBe(
      "Enshrouded server: Embervale\nFind it in the server list, or join by IP: 192.168.1.5:15637\nPassword (Friend): ember-otter",
    );
  });
});
