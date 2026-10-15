/** FlameKeeper's own updates: what the sidebar badge and the Settings card show. */
import type { AppUpdateStatus } from "../api/types";

/** The sidebar tooltip, or null when there's nothing to update. */
export function updateTooltip(s: AppUpdateStatus | undefined): string | null {
  if (!s?.update_available || !s.latest) return null;
  const head = `FlameKeeper ${s.latest.version} is available (you have ${s.current}).`;
  return s.can_update
    ? `${head} Open Settings to update; it takes about a minute and game servers keep running.`
    : `${head} To update, run "${s.command}" on the server.`;
}

export type UpdatePhase =
  | "idle" // up to date, or nothing known yet
  | "available" // a newer release can be installed
  | "updating" // the helper is running; the old FlameKeeper still answers
  | "restarting" // FlameKeeper doesn't answer while its container is replaced
  | "finishing" // the new FlameKeeper answers; the installer is wrapping up
  | "done" // this page asked for the update and the new version is running
  | "failed";

/**
 * `waitingFor`: the version this page asked for, if it started an update.
 * `unreachable`: the last status request failed.
 */
export function updatePhase(s: AppUpdateStatus | undefined, unreachable: boolean, waitingFor: string | null): UpdatePhase {
  const run = s?.run;
  if (waitingFor && s?.current === waitingFor) return run?.status === "running" ? "finishing" : "done";
  if (waitingFor && unreachable) return "restarting";
  if (run?.status === "running") return s?.current === run.to ? "finishing" : "updating";
  if (run?.status === "failed" && (waitingFor === run.to || s?.latest?.version === run.to)) return "failed";
  if (s?.update_available) return "available";
  return "idle";
}
