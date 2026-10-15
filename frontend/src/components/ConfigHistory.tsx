import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ConfigView, RevisionMeta } from "../api/types";
import { formatTime } from "../format";
import { useConfigSaved } from "../hooks/config";
import type { SettingsSchema } from "../settings/schema";
import { ChangeList } from "./ChangeList";
import { Modal } from "./Modal";

const SOURCE: Record<string, string> = {
  created: "World created",
  editor: "Edited",
  restore: "Restored earlier settings",
  import: "Imported",
  server: "Server tab",
  legacy: "Legacy password converted",
};

function revLabel(r: RevisionMeta): string {
  // Older restores carry a "Restored revision N" note; the UI doesn't show revision numbers.
  if (r.source === "restore") return SOURCE.restore;
  return r.note || SOURCE[r.source] || r.source;
}

/** Saved configs with what each save changed, and "Restore these settings". */
export function ConfigHistory({
  worldId,
  config,
  schema,
  open,
  onClose,
  dirty,
}: {
  worldId: string;
  config: ConfigView;
  schema: SettingsSchema | undefined;
  open: boolean;
  onClose: () => void;
  dirty: boolean;
}) {
  const saved = useConfigSaved(worldId);
  const [selected, setSelected] = useState<number | null>(null);
  const [confirming, setConfirming] = useState(false);
  const { data: revisions = [] } = useQuery({
    queryKey: ["revisions", worldId],
    queryFn: () => api.revisions(worldId),
    enabled: open,
  });
  useEffect(() => {
    if (open) {
      setSelected(config.revision);
      setConfirming(false);
    }
  }, [open, config.revision]);
  const { data: detail } = useQuery({
    queryKey: ["revision", worldId, selected],
    queryFn: () => api.revision(worldId, selected!),
    enabled: open && selected !== null,
  });
  const restore = useMutation({
    mutationFn: (rev: number) => api.restoreRevision(worldId, rev, config.revision),
    onSuccess: (res) => {
      saved(res.config);
      setConfirming(false);
      setSelected(res.revision);
    },
  });

  return (
    <Modal open={open} onClose={onClose} title="Config history" wide>
      <p className="muted" style={{ marginTop: 0 }}>
        The last 50 saves. Restoring one saves its settings again as the newest change; passwords come back too.
      </p>
      <div className="history">
        <ol className="history-list">
          {revisions.map((r) => (
            <li key={r.revision}>
              <button
                className={r.revision === selected ? "active" : ""}
                onClick={() => {
                  setSelected(r.revision);
                  setConfirming(false);
                }}
              >
                <b>{formatTime(r.saved_at)}</b> <span className="muted">· {revLabel(r)}</span>
                {r.current && <span className="pill info">current</span>}
                {r.applied && <span className="pill ok">running</span>}
              </button>
            </li>
          ))}
        </ol>
        <div className="history-detail">
          {detail && detail.revision === selected && (
            <>
              <h3>Saved {formatTime(detail.saved_at)}</h3>
              {detail.previous_revision ? (
                <ChangeList changes={detail.changes} schema={schema} />
              ) : (
                <p className="muted">The oldest save in the history.</p>
              )}
              {selected !== config.revision && (
                <div style={{ marginTop: 14 }}>
                  {!confirming ? (
                    <button className="btn" onClick={() => setConfirming(true)}>
                      Restore these settings…
                    </button>
                  ) : (
                    <div className="card" style={{ background: "var(--bg2)" }}>
                      <h3>Restoring changes:</h3>
                      <ChangeList changes={detail.restore_changes} schema={schema} empty="Nothing: it matches the current config." />
                      {dirty && <p className="banner warn">Your unsaved edits on this page will be discarded.</p>}
                      <div className="top" style={{ margin: "12px 0 0" }}>
                        <span className="spacer" />
                        <button className="btn" onClick={() => setConfirming(false)}>Cancel</button>
                        <button
                          className="btn primary"
                          disabled={restore.isPending || !detail.restore_changes.length}
                          onClick={() => restore.mutate(detail.revision)}
                        >
                          Restore
                        </button>
                      </div>
                      {restore.error && <p className="error-text">{(restore.error as Error).message}</p>}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}
