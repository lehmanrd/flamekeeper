import { describe, expect, it } from "vitest";
import { humanize, infraPatch, type InfraDraft } from "./server";

const saved: InfraDraft = {
  query_port: "15637", bind_ip: "0.0.0.0", image_tag: "latest", game_branch: "public",
  steamcmd_args: "validate", puid: "4711", pgid: "4711", use_ntsync: false,
  stop_timeout_s: "90", cpu_limit: "", mem_limit_mb: "",
};

describe("infraPatch", () => {
  it("sends only changed fields, with the right types", () => {
    expect(infraPatch(saved, saved)).toEqual({ patch: {}, errors: {} });
    const draft = { ...saved, query_port: "15700", cpu_limit: "4", mem_limit_mb: "16384", use_ntsync: true };
    expect(infraPatch(draft, saved).patch).toEqual({ query_port: 15700, cpu_limit: 4, mem_limit_mb: 16384, use_ntsync: true });
  });

  it("clearing a limit sends null", () => {
    const withLimit = { ...saved, cpu_limit: "2" };
    expect(infraPatch({ ...withLimit, cpu_limit: "" }, withLimit).patch).toEqual({ cpu_limit: null });
  });

  it("reports invalid fields", () => {
    const { errors, patch } = infraPatch(
      { ...saved, query_port: "80", bind_ip: "localhost", puid: "0", mem_limit_mb: "512", image_tag: "bad tag" },
      saved,
    );
    expect(Object.keys(errors).sort()).toEqual(["bind_ip", "image_tag", "mem_limit_mb", "puid", "query_port"]);
    expect(patch).toEqual({});
  });
});

it("humanizes tags", () => {
  expect(humanize("LookingForPlayers")).toBe("Looking for players");
  expect(humanize("BaseBuilding")).toBe("Base building");
  expect(humanize("Roleplay")).toBe("Roleplay");
});
