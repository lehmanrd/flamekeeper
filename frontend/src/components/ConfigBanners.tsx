import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { Blocker } from "react-router-dom";
import { api } from "../api/client";
import type { WorldSummary } from "../api/types";
import { isRunningState } from "../format";
import { useJobFocus } from "./JobsDrawer";
import { Modal } from "./Modal";
import { WhenChoice, playersText, type When } from "./WorldActions";

/** "Restart required" and "Recreate required" banners with their actions.
 * `offerStart`: on the settings pages, a stopped world gets a Start button, so a world
 * created "edit settings first" can be started from where its settings are edited. */
export function ConfigBanners({ world, offerStart = false }: { world: WorldSummary; offerStart?: boolean }) {
  const focusJob = useJobFocus();
  const [dialog, setDialog] = useState<null | "restart" | "recreate">(null);
  const running = isRunningState(world.state);
  const act = useMutation({
    mutationFn: (fn: () => Promise<{ job_id: string }>) => fn(),
    onSuccess: (r) => {
      setDialog(null);
      focusJob(r.job_id);
    },
  });

  return (
    <>
      {offerStart && world.state === "stopped" && world.desired_state === "stopped" && (
        <div className="banner info">
          <span>
            <b>{world.display_name} is stopped.</b> Saved changes apply when it starts.
          </span>
          <span className="spacer" />
          <button className="btn sm primary" disabled={world.busy || act.isPending} onClick={() => act.mutate(() => api.start(world.id))}>
            Start world
          </button>
        </div>
      )}
      {world.recreate_required && world.container_status && (
        <div className="banner warn">
          <span>
            <b>Container settings changed.</b>{" "}
            {running
              ? "Recreate the container to apply them (the server restarts)."
              : "They apply when the world next starts."}
          </span>
          <span className="spacer" />
          {running && (
            <button className="btn sm" disabled={world.busy} onClick={() => setDialog("recreate")}>
              Recreate now
            </button>
          )}
        </div>
      )}
      {world.pending_restart && running && !world.recreate_required && (
        <div className="banner info">
          <span>
            <b>Restart required:</b> saved changes apply when the server restarts.
          </span>
          <span className="spacer" />
          <button className="btn sm" disabled={world.busy} onClick={() => setDialog("restart")}>
            Restart to apply
          </button>
        </div>
      )}
      <RestartDialog
        world={world}
        open={dialog === "restart"}
        onClose={() => setDialog(null)}
        pending={act.isPending}
        error={act.error ? (act.error as Error).message : null}
        onConfirm={(when) => act.mutate(() => api.applyConfig(world.id, when))}
      />
      <Modal
        open={dialog === "recreate"}
        onClose={() => setDialog(null)}
        title="Recreate container?"
        actions={
          <>
            <button className="btn" onClick={() => setDialog(null)}>Cancel</button>
            <button className="btn primary" disabled={act.isPending} onClick={() => act.mutate(() => api.recreate(world.id))}>
              Recreate
            </button>
          </>
        }
      >
        <p>The server stops, its container is created again with the new settings, then it starts. Game files and saves are kept.</p>
        {playersText(world) && <p className="banner warn">{playersText(world)}</p>}
        {act.error && <p className="error-text">{(act.error as Error).message}</p>}
      </Modal>
    </>
  );
}

/** Confirm a process restart that applies the saved config. */
export function RestartDialog({
  world,
  open,
  onClose,
  onConfirm,
  pending,
  error,
  title = "Restart to apply changes?",
}: {
  world: WorldSummary;
  open: boolean;
  onClose: () => void;
  onConfirm: (when: When) => void;
  pending: boolean;
  error: string | null;
  title?: string;
}) {
  const [when, setWhen] = useState<When>("now");
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={pending} onClick={() => onConfirm(when)}>
            {when === "empty" ? "Restart when empty" : "Restart now"}
          </button>
        </>
      }
    >
      <p>The server process restarts and reads the new settings (about 10 s). Players are disconnected.</p>
      {playersText(world) && <p className="banner warn">{playersText(world)}</p>}
      <WhenChoice when={when} setWhen={setWhen} verb="Restart" />
      {error && <p className="error-text">{error}</p>}
    </Modal>
  );
}

/** "Discard unsaved changes?" when navigating away from an editor. */
export function UnsavedDialog({ blocker }: { blocker: Blocker }) {
  return (
    <Modal
      open={blocker.state === "blocked"}
      onClose={() => blocker.reset?.()}
      title="Discard unsaved changes?"
      actions={
        <>
          <button className="btn" onClick={() => blocker.reset?.()}>Keep editing</button>
          <button className="btn primary" onClick={() => blocker.proceed?.()}>Discard</button>
        </>
      }
    >
      <p>You have changes that aren't saved. Leaving this page discards them.</p>
    </Modal>
  );
}
