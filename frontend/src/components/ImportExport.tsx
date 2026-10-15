import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { ConfigView, DryRunResponse, SaveResponse } from "../api/types";
import { saveError, useConfigSaved } from "../hooks/config";
import type { SettingsSchema } from "../settings/schema";
import { ChangeList } from "./ChangeList";
import { Modal } from "./Modal";

/** Download enshrouded_server.json, or upload one: validate, show the diff, then apply. */
export function ImportExport({
  worldId,
  slug,
  config,
  schema,
  open,
  onClose,
  dirty,
}: {
  worldId: string;
  slug: string;
  config: ConfigView;
  schema: SettingsSchema | undefined;
  open: boolean;
  onClose: () => void;
  dirty: boolean;
}) {
  const saved = useConfigSaved(worldId);
  const [withPasswords, setWithPasswords] = useState(false);
  const [doc, setDoc] = useState<unknown>(null);
  const [fileName, setFileName] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const [convertLegacy, setConvertLegacy] = useState(true);
  const [preview, setPreview] = useState<DryRunResponse | null>(null);
  const hasLegacy = Boolean(doc && typeof doc === "object" && (doc as { password?: unknown }).password);

  const reset = () => {
    setDoc(null);
    setFileName("");
    setParseError(null);
    setPreview(null);
  };

  const run = useMutation({
    mutationFn: ({ dryRun, convert }: { dryRun: boolean; convert: boolean }) =>
      api.importConfig(
        worldId,
        { document: doc, base_revision: config.revision, convert_legacy_password: convert },
        dryRun,
      ),
    onSuccess: (res, { dryRun }) => {
      if (dryRun) setPreview(res);
      else {
        saved((res as SaveResponse).config);
        reset();
        onClose();
      }
    },
  });

  const onFile = async (file: File | undefined) => {
    reset();
    if (!file) return;
    setFileName(file.name);
    try {
      const parsed = JSON.parse(await file.text());
      setDoc(parsed);
      const legacy = Boolean(parsed && typeof parsed === "object" && parsed.password);
      run.mutate({ dryRun: true, convert: legacy && convertLegacy });
    } catch (e) {
      setParseError(`Not valid JSON: ${(e as Error).message}`);
    }
  };

  const err = run.error ? saveError(run.error) : null;

  return (
    <Modal
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Import / export"
      wide
    >
      <h3>Export</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Downloads the current <span className="mono">enshrouded_server.json</span> exactly as saved.
      </p>
      <div className="top" style={{ margin: 0 }}>
        <label className="check">
          <input type="checkbox" checked={withPasswords} onChange={(e) => setWithPasswords(e.target.checked)} />
          Include role passwords
        </label>
        <span className="spacer" />
        <a className="btn" href={api.exportUrl(worldId, withPasswords)} download={`${slug}-enshrouded_server.json`}>
          Download
        </a>
      </div>

      <h3 style={{ marginTop: 22 }}>Import</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Replaces the preset, difficulty, server name, chat, tags and roles. The port, saves and ban
        list stay as they are. Masked passwords (<span className="mono">********</span>) keep each
        role's current password.
      </p>
      <input type="file" accept="application/json,.json" onChange={(e) => onFile(e.target.files?.[0])} aria-label="Config file to import" />
      {fileName && <span className="muted mono"> {fileName}</span>}
      {parseError && <p className="error-text">{parseError}</p>}
      {hasLegacy && (
        <label className="check" style={{ marginTop: 10 }}>
          <input
            type="checkbox"
            checked={convertLegacy}
            onChange={(e) => {
              setConvertLegacy(e.target.checked);
              run.mutate({ dryRun: true, convert: e.target.checked });
            }}
          />
          The file has a legacy top-level password: convert it to a role named “default”
        </label>
      )}
      {err && (
        <div className="banner bad" style={{ marginTop: 12 }}>
          <div>
            <b>{err.message}</b>
            {err.fields.length > 1 && (
              <ul>
                {err.fields.map((f, i) => (
                  <li key={i}>
                    <span className="mono">{f.loc.join(".")}</span>: {f.msg}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
      {preview && !err && (
        <div style={{ marginTop: 12 }}>
          {preview.warnings.map((w, i) => (
            <p key={i} className="banner warn">{w.msg}</p>
          ))}
          <ChangeList changes={preview.changes} schema={schema} empty="The file matches the current config." />
          {dirty && <p className="banner warn">Your unsaved edits on this page will be discarded.</p>}
          <div className="actions">
            <button className="btn" onClick={reset}>Cancel</button>
            <button
              className="btn primary"
              disabled={run.isPending || !preview.changes.length}
              onClick={() => run.mutate({ dryRun: false, convert: hasLegacy && convertLegacy })}
            >
              Import {preview.changes.length} change{preview.changes.length === 1 ? "" : "s"}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
