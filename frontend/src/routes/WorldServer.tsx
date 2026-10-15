import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ConfigView, ServerFields, WorldDetail, WorldSummary } from "../api/types";
import { ConfigBanners, UnsavedDialog } from "../components/ConfigBanners";
import { Toggle } from "../components/Toggle";
import { isRunningState } from "../format";
import { saveError, useConfig, useConfigSaved, useUnsavedGuard } from "../hooks/config";
import { useSchema, type SettingsSchema } from "../settings/schema";
import { LANGUAGE_TAGS, humanize, infraFrom, infraPatch, type InfraDraft } from "../settings/server";
import { NotFound } from "./NotFound";
import { useWorldBySlug } from "./WorldOverview";


type GameFields = Pick<ServerFields, "name" | "slotCount" | "voiceChatMode" | "enableVoiceChat" | "enableTextChat" | "tags">;

const gameFrom = (c: ConfigView): GameFields => ({
  name: c.server.name,
  slotCount: c.server.slotCount,
  voiceChatMode: c.server.voiceChatMode,
  enableVoiceChat: c.server.enableVoiceChat,
  enableTextChat: c.server.enableTextChat,
  tags: [...c.server.tags],
});

export function WorldServer() {
  const { world, loading } = useWorldBySlug();
  const { data: schema } = useSchema();
  const { data: config } = useConfig(world?.id);
  const { data: detail } = useQuery({
    queryKey: ["world", world?.id],
    queryFn: () => api.world(world!.id),
    enabled: Boolean(world),
  });
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  if (!schema || !config || !detail) return <p className="muted">Loading…</p>;
  return <ServerEditor key={world.id} world={world} schema={schema} config={config} detail={detail} />;
}

function ServerEditor({ world, schema, config, detail }: { world: WorldSummary; schema: SettingsSchema; config: ConfigView; detail: WorldDetail }) {
  const qc = useQueryClient();
  const saved = useConfigSaved(world.id);
  const { data: system } = useQuery({ queryKey: ["system"], queryFn: api.system, staleTime: 60_000 });
  const savedGame = useMemo(() => gameFrom(config), [config]);
  const savedInfra = useMemo(() => infraFrom(detail), [detail]);
  const [game, setGame] = useState<GameFields>(savedGame);
  const [infra, setInfra] = useState<InfraDraft>(savedInfra);
  const [notice, setNotice] = useState<string | null>(null);

  const gameDirty = JSON.stringify(game) !== JSON.stringify(savedGame);
  const { patch, errors: infraErrors } = infraPatch(infra, savedInfra);
  const infraDirty = Object.keys(patch).length > 0;
  const dirty = gameDirty || infraDirty;
  const blocker = useUnsavedGuard(dirty);

  // Follow saves made elsewhere while not editing.
  useEffect(() => {
    if (!gameDirty) setGame(savedGame);
  }, [savedGame]);
  useEffect(() => {
    if (!infraDirty) setInfra(savedInfra);
  }, [savedInfra]);

  const tagDef = schema.server.find((f) => f.key === "tags");
  const voiceModes = schema.server.find((f) => f.key === "voiceChatMode")?.options ?? ["Proximity", "Global"];
  const gameErrors: Partial<Record<keyof GameFields, string>> = {};
  if (!game.name.trim()) gameErrors.name = "Required";
  else if (game.name.length > 100) gameErrors.name = "At most 100 characters";
  if (!(Number.isInteger(game.slotCount) && game.slotCount >= 1 && game.slotCount <= 16)) gameErrors.slotCount = "1–16";
  const invalid = Object.keys(gameErrors).length > 0 || Object.keys(infraErrors).length > 0;

  const save = useMutation({
    mutationFn: async () => {
      const done: string[] = [];
      if (gameDirty) {
        const res = await api.saveConfig(world.id, { base_revision: config.revision, server: game });
        saved(res.config);
        if (res.changes.length) done.push("server settings");
      }
      let recreate = false;
      if (infraDirty) {
        const res = await api.patchWorld(world.id, patch);
        qc.setQueryData(["world", world.id], res.world);
        recreate = res.recreate_required;
        saved();
        if (res.changed.length) done.push("container settings");
      }
      return { done, recreate };
    },
    onSuccess: ({ done, recreate }) => {
      const running = isRunningState(world.state);
      setNotice(
        done.length === 0
          ? "Nothing changed."
          : `Saved ${done.join(" and ")}.` +
              (gameDirty && running && !recreate ? " Restart the server to apply." : "") +
              (recreate && running ? " Recreate the container to apply." : "") +
              (!running ? " They apply when the world starts." : ""),
      );
    },
  });
  const err = save.error ? saveError(save.error) : null;
  const serverFieldError = (key: string) => err?.fields.find((f) => f.loc[0] === "server" && f.loc[1] === key)?.msg;

  const revert = () => {
    save.reset();
    setGame(savedGame);
    setInfra(savedInfra);
    setNotice(null);
  };
  const setI = (k: keyof InfraDraft, v: string | boolean) => {
    setInfra((d) => ({ ...d, [k]: v }));
    setNotice(null);
  };
  const setG = <K extends keyof GameFields>(k: K, v: GameFields[K]) => {
    setGame((g) => ({ ...g, [k]: v }));
    setNotice(null);
  };
  const toggleTag = (tag: string) =>
    setG("tags", game.tags.includes(tag) ? game.tags.filter((t) => t !== tag) : [...game.tags, tag]);

  const field = (k: keyof InfraDraft, label: string, opts: { width?: number; hint?: string; placeholder?: string; mono?: boolean } = {}) => (
    <>
      <label htmlFor={`f-${k}`}>{label}</label>
      <span>
        <input
          id={`f-${k}`}
          className={opts.mono ? "mono" : undefined}
          value={String(infra[k])}
          placeholder={opts.placeholder}
          onChange={(e) => setI(k, e.target.value)}
          aria-invalid={Boolean(infraErrors[k])}
          style={{ width: opts.width ?? 200 }}
        />
        {infraErrors[k] && <span className="field-error"> {infraErrors[k]}</span>}
      </span>
      {opts.hint && <span className="hint">{opts.hint}</span>}
    </>
  );

  return (
    <>
      <div className="top">
        <h1>Server</h1>
        <span className="muted">{world.display_name}</span>
        <span className="spacer" />
        <button className="btn" disabled={!dirty} onClick={revert}>Revert</button>
        <button className="btn primary" disabled={!dirty || invalid || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
      <ConfigBanners world={world} offerStart />
      {err && <div className="banner bad" role="alert">{err.message}</div>}
      {notice && !err && <div className="banner info" role="status">{notice}</div>}

      <div className="grid server-grid">
        <div className="card">
          <h3>Game server <span className="muted small">· applies on restart</span></h3>
          <div className="form">
            <label htmlFor="f-name">Server name</label>
            <span>
              <input id="f-name" value={game.name} onChange={(e) => setG("name", e.target.value)} aria-invalid={Boolean(gameErrors.name || serverFieldError("name"))} style={{ width: "100%" }} />
              {(gameErrors.name || serverFieldError("name")) && <span className="field-error">{gameErrors.name ?? serverFieldError("name")}</span>}
            </span>
            <label htmlFor="f-slots">Max players</label>
            <span>
              <input id="f-slots" type="number" min={1} max={16} value={Number.isFinite(game.slotCount) ? game.slotCount : ""} onChange={(e) => setG("slotCount", e.target.value === "" ? NaN : Number(e.target.value))} aria-invalid={Boolean(gameErrors.slotCount || serverFieldError("slotCount"))} style={{ width: 90 }} />
              {(gameErrors.slotCount || serverFieldError("slotCount")) && <span className="field-error"> {gameErrors.slotCount ?? serverFieldError("slotCount")}</span>}
            </span>
            <label>Voice chat</label>
            <span className="inline">
              <Toggle checked={game.enableVoiceChat} onChange={(v) => setG("enableVoiceChat", v)} label="Voice chat" />
              <select value={game.voiceChatMode} onChange={(e) => setG("voiceChatMode", e.target.value)} disabled={!game.enableVoiceChat} aria-label="Voice chat mode">
                {voiceModes.map((m) => <option key={m}>{m}</option>)}
              </select>
            </span>
            <label>Text chat</label>
            <Toggle checked={game.enableTextChat} onChange={(v) => setG("enableTextChat", v)} label="Text chat" />
            <label>Server tags</label>
            <span>
              <span className="chips" role="group" aria-label="Server tags">
                {(tagDef?.options ?? []).map((t) => (
                  <button key={t} type="button" className={`chip${game.tags.includes(t) ? " on" : ""}`} aria-pressed={game.tags.includes(t)} onClick={() => toggleTag(t)}>
                    {LANGUAGE_TAGS.has(t) ? t : humanize(t)}
                  </button>
                ))}
              </span>
            </span>
            <span className="hint">Shown in the in-game server browser. Only these tags exist; the server won't start with any other.</span>
          </div>
        </div>

        <div className="card">
          <h3>Container <span className="muted small">· changes recreate the container</span></h3>
          <div className="form">
            {field("query_port", "Query port (UDP)", { width: 110, hint: "Players join on this port; forward it on your router." })}
            {field("bind_ip", "Bind IP", { width: 160, hint: "0.0.0.0 listens on every address of the host." })}
            <label>Image</label>
            <span className="mono">{detail.infra.image_repo}</span>
            {field("image_tag", "Image tag", { width: 160, mono: true, hint: "latest (Proton) is the default; stable-wine also exists." })}
            {field("game_branch", "Steam branch", { width: 160, mono: true })}
            {field("steamcmd_args", "steamcmd args", { width: 220, mono: true, hint: "validate checks game files on every start." })}
            <label>Run as (PUID / PGID)</label>
            <span className="inline">
              <input aria-label="PUID" value={infra.puid} onChange={(e) => setI("puid", e.target.value)} aria-invalid={Boolean(infraErrors.puid)} style={{ width: 80 }} />
              <input aria-label="PGID" value={infra.pgid} onChange={(e) => setI("pgid", e.target.value)} aria-invalid={Boolean(infraErrors.pgid)} style={{ width: 80 }} />
              {(infraErrors.puid || infraErrors.pgid) && <span className="field-error">{infraErrors.puid ?? infraErrors.pgid}</span>}
            </span>
            <span className="hint">Changing these hands the world's files to the new owner on recreate.</span>
            <label>ntsync</label>
            <span className="inline">
              <Toggle checked={infra.use_ntsync} onChange={(v) => setI("use_ntsync", v)} label="ntsync" />
              <span className="muted">{system?.ntsync_available ? "/dev/ntsync available on this host" : "/dev/ntsync not available (needs kernel 6.14+); ignored"}</span>
            </span>
            {field("stop_timeout_s", "Stop timeout (s)", { width: 80, hint: "How long the server gets to save before Docker kills it." })}
            {field("cpu_limit", "CPU limit", { width: 100, placeholder: "none" })}
            {field("mem_limit_mb", "Memory limit (MB)", { width: 120, placeholder: "none", hint: "Keen recommends 16 GB per server; leave empty for no limit." })}
          </div>
        </div>
      </div>
      <UnsavedDialog blocker={blocker} />
    </>
  );
}
