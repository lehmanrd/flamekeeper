import type { ConfigChange } from "../api/types";
import { formatValue } from "../settings/difficulty";
import type { SettingsSchema } from "../settings/schema";

function plain(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "On" : "Off";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  return String(v);
}

function describe(c: ConfigChange, schema: SettingsSchema | undefined): [string, string] | string {
  if (c.secret) {
    if (c.section === "legacy") return "removed";
    return c.kind === "opened" ? "removed: now open to anyone" : "changed";
  }
  if (c.kind === "added" || c.kind === "removed") return "";
  if (c.section === "gameSettings" && c.key && schema) {
    const def = schema.settings.find((s) => s.key === c.key);
    if (def) return [formatValue(def, c.before as never), formatValue(def, c.after as never)];
  }
  if (c.label === "Role order") {
    return [plain(c.before).replace(/, /g, " · "), plain(c.after).replace(/, /g, " · ")];
  }
  return [plain(c.before), plain(c.after)];
}

/** A diff as a table: setting, before → after. Passwords never have values here. */
export function ChangeList({
  changes,
  schema,
  empty = "No changes.",
}: {
  changes: ConfigChange[];
  schema: SettingsSchema | undefined;
  empty?: string;
}) {
  if (!changes.length) return <p className="muted">{empty}</p>;
  return (
    <table className="changes">
      <tbody>
        {changes.map((c, i) => {
          const d = describe(c, schema);
          return (
            <tr key={i}>
              <td>{c.label}</td>
              {typeof d === "string" ? (
                <td colSpan={3} className="muted">{d}</td>
              ) : (
                <>
                  <td className="before">{d[0]}</td>
                  <td className="muted" aria-hidden="true">→</td>
                  <td className="after">{d[1]}</td>
                </>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
