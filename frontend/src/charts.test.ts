import { describe, expect, it } from "vitest";
import { formatMb, formatRate, linePath, niceMax } from "./charts";

describe("charts", () => {
  it("rounds axis maxima", () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(7)).toBe(10);
    expect(niceMax(130)).toBe(200);
    expect(niceMax(2.2)).toBe(2.5);
    expect(niceMax(16384)).toBe(20000);
  });

  it("draws lines with gaps for missing data", () => {
    const pts = [{ t: 0, v: 0 }, { t: 50, v: 10 }, { t: 60, v: null }, { t: 100, v: 5 }];
    expect(linePath(pts, 0, 100, 10, 100, 20)).toBe("M0.0,20.0L50.0,0.0M100.0,10.0");
    expect(linePath([{ t: 0, v: 1 }, { t: 90, v: 1 }], 0, 100, 2, 100, 20, 60)).toBe("M0.0,10.0M90.0,10.0");
  });

  it("formats rates and memory", () => {
    expect(formatRate(512)).toBe("512 B/s");
    expect(formatRate(1536)).toBe("1.5 KB/s");
    expect(formatRate(null)).toBe("—");
    expect(formatMb(6144)).toBe("6.0 GB");
    expect(formatMb(512)).toBe("512 MB");
  });
});
