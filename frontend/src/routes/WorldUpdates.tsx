import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { UpdatePolicy, UpdatePolicyName, UpdateStatus, WorldSummary } from "../api/types";
import { useJobFocus } from "../components/JobsDrawer";
import { Modal } from "../components/Modal";
import { Toggle } from "../components/Toggle";
import { WhenChoice, playersText, type When } from "../components/WorldActions";
import { formatDuration, formatTime, isRunningState, since } from "../format";
import { useJobs } from "../hooks/live";
import { NotFound } from "./NotFound";
import { useWorldBySlug } from "./WorldOverview";

const POLICY_LABEL: Record<UpdatePolicyName, string> = {
  manual: "Manual",
  immediate: "Auto – immediately",
  when_empty: "Auto – when nobody is online",
  window: "Auto – in a maintenance window",
};

export function WorldUpdates() {
  const { world, loading } = useWorldBySlug();
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  return <Updates key={world.id} world={world} />;
}

const short = (d: string | null) => (d ? d.replace("sha256:", "").slice(0, 12) : "—");

function Updates({ world }: { world: WorldSummary }) {
  const qc = useQueryClient();
  const focusJob = useJobFocus();
  const { data: jobs = [] } = useJobs();
  const { data: status, refetch } = useQuery({ queryKey: ["update", world.id], queryFn: () => api.updateStatus(world.id) });
  const { data: history = [], refetch: refetchHistory } = useQuery({
    queryKey: ["update-history", world.id],
    queryFn: () => api.updateHistory(world.id),
  });
  const [dialog, setDialog] = useState<null | { target: "game" | "image"; force: boolean }>(null);

  // Refresh when an update job for this world changes state.
  const jobKey = jobs.filter((j) => j.world_id === world.id && (j.type === "update" || j.type === "image_update")).map((j) => j.id + j.status).join();
  useEffect(() => {
    refetch();
    refetchHistory();
  }, [jobKey, refetch, refetchHistory]);

  const check = useMutation({
    mutationFn: () => api.checkUpdates(world.id),
    onSuccess: (s) => qc.setQueryData(["update", world.id], s),
  });
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelJob(id), onSuccess: () => refetch() });

  if (!status) return <p className="muted">Loading…</p>;
  const { game, image, pending_job: pending } = status;

  return (
    <>
      <div className="top">
        <h1>Updates</h1>
        <span className="muted">{world.display_name}</span>
        <span className="spacer" />
        <button className="btn" disabled={check.isPending} onClick={() => check.mutate()}>
          {check.isPending ? "Checking Steam…" : "Check now"}
        </button>
      </div>
      {check.error && <div className="banner bad">{(check.error as Error).message}</div>}

      {pending ? (
        <div className="banner info">
          <span>
            <b>Update {pending.status}:</b> {pending.message}
          </span>
          <span className="spacer" />
          <button className="btn sm" onClick={() => focusJob(pending.id)}>Show progress</button>
          {pending.status === "running" && pending.message.startsWith("Waiting for") && (
            <button className="btn sm" disabled={cancel.isPending} onClick={() => cancel.mutate(pending.id)}>Cancel queued update</button>
          )}
        </div>
      ) : game.update_available ? (
        <div className="banner warn">
          <span>
            ⟳ <b>Game update available:</b> build {game.installed} → {game.latest} ({game.branch} branch). Policy: {POLICY_LABEL[status.policy.update_policy].toLowerCase()}.
            {status.auto_blocked_build === game.latest && " The automatic update to this build failed; update by hand."}
          </span>
          <span className="spacer" />
          <button className="btn sm primary" disabled={world.busy} onClick={() => setDialog({ target: "game", force: false })}>Update now…</button>
        </div>
      ) : image.update_available ? (
        <div className="banner warn">
          <span>⟳ <b>Container image update available</b> for <span className="mono">{image.image}</span>.</span>
          <span className="spacer" />
          <button className="btn sm primary" disabled={world.busy} onClick={() => setDialog({ target: "image", force: false })}>Update image…</button>
        </div>
      ) : (
        game.latest && <div className="banner info">✔ Up to date: build {game.installed} is the latest on the {game.branch} branch.</div>
      )}

      <div className="grid g2">
        <div className="card">
          <h3>Versions</h3>
          <table>
            <thead>
              <tr><th /><th>Installed</th><th>Latest</th><th>Checked</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>Game server <span className="muted">(app 2278520, {game.branch})</span></td>
                <td className="mono">{game.installed ?? "—"}</td>
                <td className="mono">{game.latest ?? "—"}</td>
                <td className="muted">{game.checked_at ? `${since(game.checked_at)} ago` : "not yet"}{game.source ? ` · ${game.source}` : ""}</td>
              </tr>
              <tr>
                <td>Container image <span className="mono muted">{image.image.split(":").pop()}</span></td>
                <td className="mono" title={image.local_digest ?? ""}>{short(image.local_digest)}</td>
                <td className="mono" title={image.remote_digest ?? ""}>{short(image.remote_digest)}</td>
                <td className="muted">{image.checked_at ? `${since(image.checked_at)} ago` : "not yet"}</td>
              </tr>
            </tbody>
          </table>
          {game.error && <p className="error-text">Steam check failed: {game.error}</p>}
          {image.error && <p className="error-text">Image check failed: {image.error}</p>}
          <div className="top" style={{ margin: "12px 0 0" }}>
            <button className="btn sm" disabled={world.busy || !!pending} onClick={() => setDialog({ target: "game", force: true })}>Force update…</button>
            <button className="btn sm" disabled={world.busy || !!pending} onClick={() => setDialog({ target: "image", force: false })}>Pull image &amp; recreate…</button>
          </div>

          <h3 style={{ marginTop: 18 }}>History</h3>
          {history.length === 0 ? (
            <p className="muted">No updates run by FlameKeeper yet.</p>
          ) : (
            <table>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id}>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>{formatTime(h.created_at)}</td>
                    <td>{h.type === "image_update" ? "Image" : "Game"}: {h.message}</td>
                    <td><span className={`pill ${h.status === "succeeded" ? "ok" : h.status === "failed" ? "bad" : "info"}`}>{h.status}</span></td>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>
                      {h.started_at && h.finished_at ? formatDuration(new Date(h.finished_at).getTime() - new Date(h.started_at).getTime()) : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <PolicyCard world={world} status={status} />
      </div>

      {dialog && <UpdateDialog world={world} status={status} target={dialog.target} force={dialog.force} onClose={() => setDialog(null)} />}
    </>
  );
}

function UpdateDialog({ world, status, target, force, onClose }: { world: WorldSummary; status: UpdateStatus; target: "game" | "image"; force: boolean; onClose: () => void }) {
  const focusJob = useJobFocus();
  const [when, setWhen] = useState<When>("now");
  const run = useMutation({
    mutationFn: () => api.startUpdate(world.id, { target, when, force }),
    onSuccess: (r) => {
      onClose();
      focusJob(r.job_id);
    },
  });
  const running = isRunningState(world.state);
  const title = target === "image" ? "Update the container image?" : force ? "Force a game update?" : "Update the game server?";
  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={run.isPending} onClick={() => run.mutate()}>
            {when === "empty" && running ? "Update when empty" : "Update"}
          </button>
        </>
      }
    >
      <ol className="steps">
        {target === "image" && <li>Pull <span className="mono">{status.image.image}</span>.</li>}
        {running && <li>Stop the server (it saves first).</li>}
        {status.policy.pre_update_backup && <li>Back up the savegame.</li>}
        {target === "game" ? (
          <li>
            {force ? "Delete the Steam manifests and re-download the game (about 9 GB)." : `Download build ${status.game.latest ?? "latest"} with steamcmd.`}
          </li>
        ) : (
          <li>Recreate the container on the new image (game files and saves are kept).</li>
        )}
        <li>{running ? "Start the server and wait until it's online." : "Start the server to install the update, then stop it again."}</li>
      </ol>
      {playersText(world) && <p className="banner warn">{playersText(world)}</p>}
      {running && <WhenChoice when={when} setWhen={setWhen} verb="Update" />}
      {run.error && <p className="error-text">{(run.error as Error).message}</p>}
    </Modal>
  );
}

function PolicyCard({ world, status }: { world: WorldSummary; status: UpdateStatus }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<UpdatePolicy>(status.policy);
  useEffect(() => setDraft(status.policy), [status.policy]);
  const dirty =
    draft.update_policy !== status.policy.update_policy ||
    draft.update_window_cron.trim() !== status.policy.update_window_cron ||
    draft.image_update_policy !== status.policy.image_update_policy ||
    draft.pre_update_backup !== status.policy.pre_update_backup;
  const save = useMutation({
    mutationFn: () =>
      api.setUpdatePolicy(world.id, {
        update_policy: draft.update_policy,
        update_window_cron: draft.update_window_cron.trim(),
        image_update_policy: draft.image_update_policy,
        pre_update_backup: draft.pre_update_backup,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["update", world.id] }),
  });
  const set = <K extends keyof UpdatePolicy>(k: K, v: UpdatePolicy[K]) => {
    save.reset();
    setDraft((d) => ({ ...d, [k]: v }));
  };
  return (
    <div className="card">
      <h3>Policy</h3>
      <div className="form" style={{ gridTemplateColumns: "130px 1fr" }}>
        <label htmlFor="u-game">Game updates</label>
        <select id="u-game" value={draft.update_policy} onChange={(e) => set("update_policy", e.target.value as UpdatePolicyName)}>
          {(Object.keys(POLICY_LABEL) as UpdatePolicyName[]).map((p) => <option key={p} value={p}>{POLICY_LABEL[p]}</option>)}
        </select>
        <label htmlFor="u-window">Window</label>
        <span>
          <input id="u-window" className="mono" value={draft.update_window_cron} disabled={draft.update_policy !== "window"} onChange={(e) => set("update_window_cron", e.target.value)} style={{ width: 130 }} />
          {status.policy.next_window && draft.update_policy === "window" && <span className="muted small"> next {formatTime(status.policy.next_window)}</span>}
        </span>
        <label htmlFor="u-image">Image updates</label>
        <select id="u-image" value={draft.image_update_policy} onChange={(e) => set("image_update_policy", e.target.value as UpdatePolicy["image_update_policy"])}>
          <option value="notify">Notify only</option>
          <option value="when_empty">Auto – when nobody is online</option>
        </select>
        <label>Back up first</label>
        <Toggle checked={draft.pre_update_backup} onChange={(v) => set("pre_update_backup", v)} label="Back up before updating" />
      </div>
      <div className="top" style={{ margin: "14px 0 0" }}>
        <span className="spacer" />
        <button className="btn sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>Save policy</button>
      </div>
      {save.error && <p className="error-text">{(save.error as Error).message}</p>}
      <div className="note">
        FlameKeeper asks Steam for the latest build every 30 minutes. Automatic updates only run while the world is
        running; a stopped world updates itself the next time it starts. A failed automatic update isn't retried for
        the same build.
      </div>
    </div>
  );
}
