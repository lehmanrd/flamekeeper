import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { WorldSummary } from "../api/types";
import { Modal } from "./Modal";

/** After an import: what's left of the old server, and removing it step by step. */
export function ImportCleanup({ world }: { world: WorldSummary }) {
  const qc = useQueryClient();
  const key = ["world-import", world.id];
  const { data } = useQuery({ queryKey: key, queryFn: () => api.worldImport(world.id), refetchInterval: 30_000 });
  const [confirm, setConfirm] = useState<"container" | "volumes" | null>(null);
  const [kept, setKept] = useState<string[]>([]);
  const done = () => qc.invalidateQueries({ queryKey: key });
  const removeContainer = useMutation({
    mutationFn: () => api.removeImportedContainer(world.id),
    onSuccess: () => {
      setConfirm(null);
      done();
    },
  });
  const removeVolumes = useMutation({
    mutationFn: () => api.removeImportedVolumes(world.id),
    onSuccess: (res) => {
      setConfirm(null);
      setKept(res.kept.map((k) => (k.used_by.length ? `${k.name} (used by ${k.used_by.join(", ")})` : `${k.name}: ${k.error}`)));
      done();
    },
  });
  const dismiss = useMutation({ mutationFn: () => api.dismissImport(world.id), onSuccess: done });

  const imp = data?.import;
  if (!imp || ["importing", "dismissed", "volumes_removed"].includes(imp.state)) return null;
  const label = imp.source_label;

  if (imp.state === "rolled_back") {
    return (
      <div className="banner bad">
        The import from {label} failed, so FlameKeeper started {imp.source_kind === "container" ? "it" : "nothing"} again
        {imp.source_kind === "container" ? " with its old restart policy" : ""}. See the job log for why; delete this
        world to try again.
      </div>
    );
  }

  const binds = imp.bind_sources.length > 0 && (
    <span className="muted">
      {" "}Host folders it used are never deleted by FlameKeeper: <span className="mono">{imp.bind_sources.join(", ")}</span>.
    </span>
  );
  const containerGone = imp.state === "container_removed" || imp.container === null;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="top" style={{ margin: 0 }}>
        <h3 style={{ margin: 0 }}>Imported from {label}</h3>
        <span className="spacer" />
        <button className="btn sm" disabled={dismiss.isPending} onClick={() => dismiss.mutate()}>Dismiss</button>
      </div>
      {imp.source_kind !== "container" && (
        <p className="muted" style={{ marginBottom: 0 }}>
          The original files were left as they were; the copy FlameKeeper made is this world's "imported" backup.
          {imp.source_kind === "host_path" && " Delete the old folder yourself once you no longer need it."}
        </p>
      )}
      {imp.source_kind === "container" && !containerGone && imp.container && (
        <>
          <p style={{ marginBottom: 8 }}>
            The old container <b>{imp.container.name}</b> is {imp.container.running ? <b className="error-text">running again</b> : "stopped"}
            {" "}(restart policy <span className="mono">{imp.container.restart_policy}</span>). Its files are untouched, and the
            copy is kept as this world's "imported" backup. Once you're happy with the new world, you can remove it.
          </p>
          {imp.container.running && <div className="banner warn">Stop {imp.container.name} before removing it; it may also be using this world's port.</div>}
          <button className="btn danger" disabled={imp.container.running} onClick={() => setConfirm("container")}>Remove old container…</button>
        </>
      )}
      {imp.source_kind === "container" && containerGone && (
        <>
          <p style={{ marginBottom: 8 }}>
            The old container was removed.
            {imp.volumes.length > 0 ? " Its Docker volumes still hold the old server's files." : " It had no Docker volumes."}
            {binds}
          </p>
          {imp.volumes.length > 0 && (
            <button className="btn danger" onClick={() => setConfirm("volumes")}>Remove its volumes…</button>
          )}
          {kept.length > 0 && <p className="field-warn">Kept: {kept.join("; ")}</p>}
        </>
      )}

      <Modal
        open={confirm === "container"}
        onClose={() => setConfirm(null)}
        title={`Remove ${imp.container?.name ?? "the old container"}?`}
        actions={
          <>
            <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
            <button className="btn danger" disabled={removeContainer.isPending} onClick={() => removeContainer.mutate()}>
              {removeContainer.isPending ? "Removing…" : "Remove container"}
            </button>
          </>
        }
      >
        <p>
          This deletes the container only (like <span className="mono">docker rm</span>). Its volumes and host folders stay,
          so nothing of the old world is lost yet; removing the volumes is a separate step.
        </p>
        {removeContainer.error && <div className="banner bad">{(removeContainer.error as Error).message}</div>}
      </Modal>

      <Modal
        open={confirm === "volumes"}
        onClose={() => setConfirm(null)}
        title="Remove the old server's volumes?"
        actions={
          <>
            <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
            <button className="btn danger" disabled={removeVolumes.isPending} onClick={() => removeVolumes.mutate()}>
              {removeVolumes.isPending ? "Removing…" : "Delete volumes"}
            </button>
          </>
        }
      >
        <p>
          This permanently deletes <span className="mono">{imp.volumes.join(", ")}</span>, including the old server's
          savegame and game files. This world keeps its "imported" backup of the savegame and settings. Volumes that
          another container still uses are skipped.
        </p>
        {removeVolumes.error && <div className="banner bad">{(removeVolumes.error as Error).message}</div>}
      </Modal>
    </div>
  );
}
