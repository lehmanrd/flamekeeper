import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useBlocker } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { ConfigView, FieldProblem } from "../api/types";

export function useConfig(worldId: string | undefined) {
  return useQuery({
    queryKey: ["config", worldId],
    queryFn: () => api.config(worldId!),
    enabled: Boolean(worldId),
  });
}

/** After a save: put the new config in the cache and refresh dependent views. */
export function useConfigSaved(worldId: string) {
  const qc = useQueryClient();
  return (config?: ConfigView) => {
    if (config) qc.setQueryData(["config", worldId], config);
    else qc.invalidateQueries({ queryKey: ["config", worldId] });
    qc.invalidateQueries({ queryKey: ["revisions", worldId] });
    qc.invalidateQueries({ queryKey: ["world", worldId] });
    qc.invalidateQueries({ queryKey: ["events", worldId] });
  };
}

/** Ask before leaving a page with unsaved edits (in-app navigation and tab close). */
export function useUnsavedGuard(dirty: boolean) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);
  return blocker;
}

/** Field problems from a 422, and a readable message for anything else. */
export function saveError(e: unknown): { message: string; fields: FieldProblem[]; stale: boolean } {
  if (e instanceof ApiError) {
    return { message: e.message, fields: e.errors, stale: e.status === 409 };
  }
  return { message: e instanceof Error ? e.message : String(e), fields: [], stale: false };
}
