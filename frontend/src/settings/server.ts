/** Server tab logic: container-settings draft → PATCH body with validation. */
import type { InfraPatch, WorldDetail } from "../api/types";

export const LANGUAGE_TAGS = new Set([
  "English", "German", "French", "Italian", "Japanese", "Korean", "Polish", "Portuguese",
  "Russian", "Spanish", "Thai", "Turkish", "Ukrainian", "Chinese", "Taiwanese",
]);

/** "LookingForPlayers" → "Looking for players". */
export const humanize = (tag: string) => tag.replace(/(?<=[a-z])(?=[A-Z])/g, " ").replace(/ (\w)/g, (m) => m.toLowerCase());

export interface InfraDraft {
  query_port: string;
  bind_ip: string;
  image_tag: string;
  game_branch: string;
  steamcmd_args: string;
  puid: string;
  pgid: string;
  use_ntsync: boolean;
  stop_timeout_s: string;
  cpu_limit: string;
  mem_limit_mb: string;
}

export const infraFrom = (w: WorldDetail): InfraDraft => ({
  query_port: String(w.infra.query_port),
  bind_ip: w.infra.bind_ip,
  image_tag: w.infra.image_tag,
  game_branch: w.infra.game_branch,
  steamcmd_args: w.infra.steamcmd_args,
  puid: String(w.infra.puid),
  pgid: String(w.infra.pgid),
  use_ntsync: w.infra.use_ntsync,
  stop_timeout_s: String(w.infra.stop_timeout_s),
  cpu_limit: w.infra.cpu_limit == null ? "" : String(w.infra.cpu_limit),
  mem_limit_mb: w.infra.mem_limit_mb == null ? "" : String(w.infra.mem_limit_mb),
});

const int = (s: string) => (/^\d+$/.test(s.trim()) ? Number(s) : NaN);
const IP_RE = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$|:/;

/** Turn the infra draft into a PATCH body, or field errors. Only changed fields are sent. */
export function infraPatch(draft: InfraDraft, saved: InfraDraft): { patch: InfraPatch; errors: Partial<Record<keyof InfraDraft, string>> } {
  const errors: Partial<Record<keyof InfraDraft, string>> = {};
  const patch: InfraPatch = {};
  const changed = (k: keyof InfraDraft) => draft[k] !== saved[k];
  const port = int(draft.query_port);
  if (!(port >= 1024 && port <= 65535)) errors.query_port = "1024–65535";
  else if (changed("query_port")) patch.query_port = port;
  if (!IP_RE.test(draft.bind_ip.trim())) errors.bind_ip = "An IP address, e.g. 0.0.0.0";
  else if (changed("bind_ip")) patch.bind_ip = draft.bind_ip.trim();
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(draft.image_tag)) errors.image_tag = "Letters, digits, _ . -";
  else if (changed("image_tag")) patch.image_tag = draft.image_tag;
  if (!/^[A-Za-z0-9_.-]{1,64}$/.test(draft.game_branch)) errors.game_branch = "Letters, digits, _ . -";
  else if (changed("game_branch")) patch.game_branch = draft.game_branch;
  if (draft.steamcmd_args.length > 200) errors.steamcmd_args = "At most 200 characters";
  else if (changed("steamcmd_args")) patch.steamcmd_args = draft.steamcmd_args;
  for (const k of ["puid", "pgid"] as const) {
    const v = int(draft[k]);
    if (!(v >= 1)) errors[k] = "A user/group id, 1 or more";
    else if (changed(k)) patch[k] = v;
  }
  if (changed("use_ntsync")) patch.use_ntsync = draft.use_ntsync;
  const timeout = int(draft.stop_timeout_s);
  if (!(timeout >= 10 && timeout <= 3600)) errors.stop_timeout_s = "10–3600 seconds";
  else if (changed("stop_timeout_s")) patch.stop_timeout_s = timeout;
  const cpu = draft.cpu_limit.trim() === "" ? null : Number(draft.cpu_limit);
  if (cpu !== null && !(cpu >= 0.1 && cpu <= 256)) errors.cpu_limit = "0.1–256 CPUs, or empty";
  else if (changed("cpu_limit")) patch.cpu_limit = cpu;
  const mem = draft.mem_limit_mb.trim() === "" ? null : int(draft.mem_limit_mb);
  if (mem !== null && !(mem >= 1024)) errors.mem_limit_mb = "At least 1024 MB, or empty";
  else if (changed("mem_limit_mb")) patch.mem_limit_mb = mem;
  return { patch, errors };
}
