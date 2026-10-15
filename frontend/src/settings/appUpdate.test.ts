import { describe, expect, it } from "vitest";
import type { AppUpdateStatus } from "../api/types";
import { updatePhase, updateTooltip } from "./appUpdate";

const base: AppUpdateStatus = {
  current: "0.4.1",
  latest: { version: "0.5.0", name: "FlameKeeper 0.5.0", url: null, published_at: null },
  update_available: true,
  checked_at: null,
  check_error: null,
  checks_enabled: true,
  can_update: true,
  cannot_update_reason: null,
  command: "sudo flamekeeper update",
  run: null,
};
const run = (status: "running" | "succeeded" | "failed") =>
  ({ status, from: "0.4.1", to: "0.5.0", started_at: "", finished_at: null, error: null, log_tail: null });

describe("updateTooltip", () => {
  it("nudges toward Settings, or the command when the UI can't update", () => {
    expect(updateTooltip(base)).toMatch(/^FlameKeeper 0\.5\.0 is available \(you have 0\.4\.1\)\. Open Settings/);
    expect(updateTooltip({ ...base, can_update: false })).toContain('run "sudo flamekeeper update"');
    expect(updateTooltip({ ...base, update_available: false })).toBeNull();
    expect(updateTooltip(undefined)).toBeNull();
  });
});

describe("updatePhase", () => {
  it("follows an update from start to finish", () => {
    expect(updatePhase(base, false, null)).toBe("available");
    expect(updatePhase({ ...base, run: run("running") }, false, "0.5.0")).toBe("updating");
    expect(updatePhase({ ...base, run: run("running") }, true, "0.5.0")).toBe("restarting");
    const after = { ...base, current: "0.5.0", update_available: false };
    expect(updatePhase({ ...after, run: run("running") }, false, "0.5.0")).toBe("finishing");
    expect(updatePhase({ ...after, run: run("succeeded") }, false, "0.5.0")).toBe("done");
    expect(updatePhase({ ...after, run: run("succeeded") }, false, null)).toBe("idle");
  });
  it("reports a failure, and an update another browser started", () => {
    expect(updatePhase({ ...base, run: run("failed") }, false, "0.5.0")).toBe("failed");
    expect(updatePhase({ ...base, run: run("failed") }, false, null)).toBe("failed");
    expect(updatePhase({ ...base, run: run("running") }, false, null)).toBe("updating");
    expect(updatePhase(undefined, true, null)).toBe("idle");
  });
});
