import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { WorldSummary } from "../api/types";
import { useJobFocus } from "./JobsDrawer";
import { Modal } from "./Modal";

export type When = "now" | "empty";

export function playersText(world: WorldSummary): string | null {
  if (!world.players) return null;
  const names = world.player_names.length ? `: ${world.player_names.join(", ")}` : "";
  return `${world.players} player${world.players === 1 ? " is" : "s are"} online${names}.`;
}

/** Start / Stop / Restart buttons with their confirmations. */
export function WorldActions({ world, size }: { world: WorldSummary; size?: "sm" }) {
  const focusJob = useJobFocus();
  const [dialog, setDialog] = useState<null | "stop" | "restart">(null);
  const [when, setWhen] = useState<When>("now");
  const [mode, setMode] = useState<"process" | "container">("process");
  const [error, setError] = useState<string | null>(null);
  const cls = `btn${size ? " sm" : ""}`;

  const run = useMutation({
    mutationFn: (fn: () => Promise<{ job_id: string }>) => fn(),
    onSuccess: (res) => {
      setDialog(null);
      setError(null);
      focusJob(res.job_id);
    },
    onError: (e: Error) => setError(e.message),
  });

  const stopped = world.state === "stopped" || world.state === "missing" || world.state === "crashed";
  const busy = world.busy || run.isPending;
  const players = playersText(world);

  const open = (d: "stop" | "restart") => {
    setWhen("now");
    setMode("process");
    setError(null);
    setDialog(d);
  };

  return (
    <>
      {stopped ? (
        <button className={`${cls} primary`} disabled={busy} onClick={() => run.mutate(() => api.start(world.id))}>
          Start
        </button>
      ) : (
        <>
          <button className={cls} disabled={busy} onClick={() => open("restart")}>
            Restart
          </button>
          <button className={`${cls} danger`} disabled={busy} onClick={() => open("stop")}>
            Stop
          </button>
        </>
      )}
      {error && !dialog && <span className="error-text">{error}</span>}

      <Modal
        open={dialog === "stop"}
        onClose={() => setDialog(null)}
        title={`Stop ${world.display_name}?`}
        actions={
          <>
            <button className="btn" onClick={() => setDialog(null)}>Cancel</button>
            <button
              className="btn primary"
              disabled={run.isPending}
              onClick={() => run.mutate(() => api.stop(world.id, when))}
            >
              {when === "empty" ? "Stop when empty" : "Stop now"}
            </button>
          </>
        }
      >
        <p>The server saves the world before it shuts down (up to the stop timeout).</p>
        {players && <p className="banner warn">{players}</p>}
        <WhenChoice when={when} setWhen={setWhen} verb="Stop" />
        {error && <p className="error-text">{error}</p>}
      </Modal>

      <Modal
        open={dialog === "restart"}
        onClose={() => setDialog(null)}
        title={`Restart ${world.display_name}?`}
        actions={
          <>
            <button className="btn" onClick={() => setDialog(null)}>Cancel</button>
            <button
              className="btn primary"
              disabled={run.isPending}
              onClick={() => run.mutate(() => api.restart(world.id, mode, when))}
            >
              Restart
            </button>
          </>
        }
      >
        {players && <p className="banner warn">{players}</p>}
        <div className="radio-row">
          <label>
            <input type="radio" checked={mode === "process"} onChange={() => setMode("process")} />
            <span>
              <b>Server process</b> <span className="muted">(about 10 s; applies config changes)</span>
            </span>
          </label>
          <label>
            <input type="radio" checked={mode === "container"} onChange={() => setMode("container")} />
            <span>
              <b>Whole container</b> <span className="muted">(use if the server is stuck)</span>
            </span>
          </label>
        </div>
        <WhenChoice when={when} setWhen={setWhen} verb="Restart" />
        {error && <p className="error-text">{error}</p>}
      </Modal>
    </>
  );
}

export function WhenChoice({ when, setWhen, verb }: { when: When; setWhen: (w: When) => void; verb: string }) {
  return (
    <div className="radio-row">
      <label>
        <input type="radio" checked={when === "now"} onChange={() => setWhen("now")} />
        <span>{verb} now</span>
      </label>
      <label>
        <input type="radio" checked={when === "empty"} onChange={() => setWhen("empty")} />
        <span>
          Wait until no players are online <span className="muted">(checks every 30 s; cancel from Jobs)</span>
        </span>
      </label>
    </div>
  );
}
