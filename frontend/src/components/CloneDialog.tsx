import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { WorldSummary } from "../api/types";
import { SLUG_RE, slugify } from "../format";
import { useJobFocus } from "./JobsDrawer";
import { Modal } from "./Modal";

/** New world from this world's current savegame or one of its backups. */
export function CloneDialog({
  world,
  source,
  sourceLabel,
  open,
  onClose,
}: {
  world: WorldSummary;
  source: string; // "current" or a backup id
  sourceLabel: string;
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const focusJob = useJobFocus();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [port, setPort] = useState("");
  const { data: suggested } = useQuery({ queryKey: ["suggest-port", open], queryFn: api.suggestPort, enabled: open });

  useEffect(() => {
    if (open) {
      setName(`${world.display_name} copy`);
      setSlugEdited(false);
      setPort("");
    }
  }, [open, world.display_name]);
  useEffect(() => {
    if (!slugEdited) setSlug(slugify(name));
  }, [name, slugEdited]);

  const clone = useMutation({
    mutationFn: () =>
      api.cloneWorld(world.id, {
        source,
        display_name: name.trim(),
        slug,
        query_port: port ? Number(port) : undefined,
      }),
    onSuccess: (res) => {
      onClose();
      focusJob(res.job_id);
      navigate(`/worlds/${res.world.slug}`);
    },
  });
  const portNum = port ? Number(port) : suggested?.port;
  const valid = name.trim() && SLUG_RE.test(slug) && (!port || (Number(port) >= 1024 && Number(port) <= 65535));

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Clone to a new world"
      actions={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!valid || clone.isPending} onClick={() => clone.mutate()}>
            Create clone
          </button>
        </>
      }
    >
      <p className="muted" style={{ marginTop: 0 }}>
        A new world with the savegame from <b>{sourceLabel}</b>, plus this world's difficulty and roles (same
        passwords). Its first start downloads the game files again (about 9 GB).
      </p>
      <div className="form" style={{ gridTemplateColumns: "110px 1fr" }}>
        <label htmlFor="c-name">Name</label>
        <input id="c-name" value={name} onChange={(e) => setName(e.target.value)} />
        <label htmlFor="c-slug">Slug</label>
        <span>
          <input id="c-slug" className="mono" value={slug} aria-invalid={!SLUG_RE.test(slug)}
            onChange={(e) => { setSlug(e.target.value); setSlugEdited(true); }} />
        </span>
        <label htmlFor="c-port">Query port</label>
        <input id="c-port" value={port} placeholder={suggested ? String(suggested.port) : "auto"}
          onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))} style={{ width: 110 }} />
      </div>
      {portNum && <p className="muted" style={{ fontSize: 12 }}>Players will join on UDP {portNum}.</p>}
      {clone.error && <p className="error-text">{(clone.error as Error).message}</p>}
    </Modal>
  );
}
