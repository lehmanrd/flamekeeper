import { describe, expect, it } from "vitest";
import type { LogLine } from "./api/types";
import { SLUG_RE, filterLines, formatBytes, formatClock, formatDuration, slugify } from "./format";

describe("slugify", () => {
  it("matches the backend rules", () => {
    expect(slugify("Embervale Keep!")).toBe("embervale-keep");
    expect(slugify("Ünïcode  World")).toBe("unicode-world");
    expect(slugify("***")).toBe("world");
    expect(slugify("a".repeat(60))).toHaveLength(40);
    expect(SLUG_RE.test(slugify("--x--"))).toBe(true);
  });
});

describe("formatting", () => {
  it("formats durations", () => {
    expect(formatDuration(30_000)).toBe("<1m");
    expect(formatDuration(38 * 60_000)).toBe("38m");
    expect(formatDuration(131 * 60_000)).toBe("2h 11m");
    expect(formatDuration((3 * 24 + 4) * 3_600_000)).toBe("3d 4h");
  });
  it("formats bytes", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(16 * 1024 ** 3)).toBe("16.0 GB");
    expect(formatBytes(118 * 1024 ** 3)).toBe("118 GB");
  });
  it("formats log times in the browser's time zone", () => {
    const iso = "2026-10-14T19:55:47.419+00:00";
    expect(formatClock(iso)).toBe(
      new Date(Date.UTC(2026, 9, 14, 19, 55, 47)).toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
      }),
    );
    expect(formatClock(null)).toBe("");
  });
});

describe("filterLines", () => {
  const line = (text: string, level: LogLine["level"] = "info", player = false): LogLine => ({
    ts: null,
    source: "enshrouded-server",
    level,
    text,
    player,
  });
  const lines = [
    line("[Session] 'HostOnline' (up)!"),
    line("WARN something", "warn"),
    line("Failed to parse Config", "error"),
    line("[server] Player 'Ryn' logged in", "info", true),
  ];
  it("filters by level", () => {
    expect(filterLines(lines, "", "all")).toHaveLength(4);
    expect(filterLines(lines, "", "warn")).toHaveLength(2);
    expect(filterLines(lines, "", "error")).toHaveLength(1);
    expect(filterLines(lines, "", "players")[0].text).toContain("Ryn");
  });
  it("searches case-insensitively", () => {
    expect(filterLines(lines, "hostonline", "all")).toHaveLength(1);
    expect(filterLines(lines, "ENSHROUDED", "all")).toHaveLength(4);
  });
});
