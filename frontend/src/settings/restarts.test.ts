import { describe, expect, it } from "vitest";
import { modeFields, playersMode, restartEventText, restartPresetFor } from "./restarts";

describe("restarts", () => {
  it("maps cron expressions to presets", () => {
    expect(restartPresetFor(" 0  5 * * * ")).toBe("0 5 * * *");
    expect(restartPresetFor("0 5,17 * * *")).toBe("0 5,17 * * *");
    expect(restartPresetFor("30 3 * * *")).toBe("custom");
  });

  it("round-trips the players-online choice", () => {
    for (const mode of ["wait_skip", "wait_restart", "restart"] as const) {
      expect(playersMode(modeFields(mode))).toBe(mode);
    }
    expect(playersMode({ only_when_empty: false, if_still_busy: "restart" })).toBe("restart");
  });

  it("describes timeline events", () => {
    expect(restartEventText({ outcome: "restarting", players: 0 })).toBe("restarting");
    expect(restartEventText({ outcome: "restarting", players: 2 })).toBe("restarting with 2 players online");
    expect(restartEventText({ outcome: "skipped", reason: "recent_start" })).toBe("skipped: the server had just started");
    expect(restartEventText({ outcome: "skipped", reason: "new" })).toBe("skipped");
  });
});
