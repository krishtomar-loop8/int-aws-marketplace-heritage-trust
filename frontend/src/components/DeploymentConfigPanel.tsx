import { useEffect, useState } from "react";
import { type ConfigField, fetchConfig, updateConfig } from "../api";
import { MOCK_CONFIG } from "../mock";

// The Deployment Config tab — the 13 business-config fields the scheduled
// Lambdas (AML Export, Quarterly Reports, SharePoint crawler) and the Matter
// Search agent read, grouped by which Lambda consumes them (see design spec
// §5). Visible to everyone, like Integrations — isAdmin gates only the edit
// controls, so a non-admin sees every field read-only with its "configured" /
// "using default" badge.

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong.";
}

// Fields whose stored value is a list — edited/displayed as a comma-separated
// line (no multiline input exists elsewhere in this app; a plain text input
// keeps this screen consistent with ConnectorCard's forms).
const LIST_FIELDS = new Set([
  "excluded_sharepoint_folders",
  "own_company_names",
  "internal_title_keywords",
  "quarterly_qualifying_practice_areas",
  "sharepoint_sites",
]);

interface FieldMeta {
  name: string;
  label: string;
  hint?: string;
}

interface Section {
  title: string;
  blurb: string;
  fields: FieldMeta[];
}

const SECTIONS: Section[] = [
  {
    title: "Search",
    blurb: "Read by the Matter Search agent when synthesizing a status file and scoping SharePoint.",
    fields: [
      { name: "org_name", label: "Organization name" },
      { name: "org_office_address", label: "Office address" },
      {
        name: "excluded_sharepoint_folders",
        label: "Excluded SharePoint folders",
        hint: "Comma-separated. Folders staff shouldn't surface in matter search (e.g. HR, Finance).",
      },
    ],
  },
  {
    title: "AML Export",
    blurb: "Read by the monthly AML Name Export Lambda to recognize and exclude internal/administrative matters.",
    fields: [
      { name: "own_company_names", label: "Own company name(s)", hint: "Comma-separated." },
      {
        name: "internal_title_keywords",
        label: "Internal matter title keywords",
        hint: "Comma-separated. Matter titles containing any of these are treated as internal, not a client.",
      },
      {
        name: "internal_matter_number_pattern",
        label: "Internal matter number pattern",
        hint: "A regular expression matched against the matter number.",
      },
    ],
  },
  {
    title: "Quarterly Reports",
    blurb: "Read by the Quarterly Report Draft Generator for the cover letter letterhead and matter selection.",
    fields: [
      { name: "letterhead_name", label: "Letterhead name" },
      { name: "letterhead_address", label: "Letterhead address" },
      { name: "letterhead_signoff", label: "Letterhead signoff" },
      { name: "letterhead_privacy_notice", label: "Letterhead privacy notice" },
      {
        name: "quarterly_qualifying_practice_areas",
        label: "Qualifying practice areas",
        hint: "Comma-separated. Leave empty to qualify every practice area.",
      },
      {
        name: "quarterly_exclusion_field",
        label: "Exclusion field name",
        hint: "The Clio custom field that excludes a matter from quarterly reports when set.",
      },
    ],
  },
  {
    title: "SharePoint",
    blurb: "Read by the SharePoint crawler Lambda to decide which sites it indexes.",
    fields: [{ name: "sharepoint_sites", label: "SharePoint sites", hint: "Comma-separated." }],
  },
];

function toEditValue(field: ConfigField | undefined, isList: boolean): string {
  if (!field) return "";
  if (isList) {
    return Array.isArray(field.value) ? (field.value as unknown[]).map(String).join(", ") : "";
  }
  return field.value == null ? "" : String(field.value);
}

function fromEditValue(raw: string, isList: boolean): unknown {
  if (!isList) return raw;
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function displayValue(field: ConfigField | undefined, isList: boolean) {
  if (!field) return <span className="matter-sub">—</span>;
  if (isList) {
    const items = Array.isArray(field.value) ? (field.value as unknown[]).map(String) : [];
    return items.length > 0 ? items.join(", ") : <span className="matter-sub">(none)</span>;
  }
  const s = field.value == null ? "" : String(field.value);
  return s.length > 0 ? s : <span className="matter-sub">(empty)</span>;
}

function ConfigFieldRow({
  meta,
  field,
  isAdmin,
  busy,
  onSave,
  onReset,
}: {
  meta: FieldMeta;
  field?: ConfigField;
  isAdmin: boolean;
  busy: boolean;
  onSave: (name: string, value: unknown) => void;
  onReset: (name: string) => void;
}) {
  const isList = LIST_FIELDS.has(meta.name);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => toEditValue(field, isList));
  const configured = field?.source === "configured";

  // Re-sync the draft with the stored value (e.g. after a save elsewhere
  // refreshed the whole field set) as long as this row isn't mid-edit.
  useEffect(() => {
    if (!editing) setDraft(toEditValue(field, isList));
  }, [field, isList, editing]);

  function startEdit() {
    setDraft(toEditValue(field, isList));
    setEditing(true);
  }

  function save() {
    onSave(meta.name, fromEditValue(draft, isList));
    setEditing(false);
  }

  return (
    <div className="schedule-card">
      <div className="schedule-row">
        <strong>{meta.label}</strong>
        <span className={`badge${configured ? " badge-review" : ""}`}>
          {configured ? "Configured" : "Using default"}
        </span>
      </div>
      {meta.hint && <p className="schedule-status">{meta.hint}</p>}

      {!editing && <p className="schedule-status">{displayValue(field, isList)}</p>}

      {isAdmin && editing && (
        <div className="reports-controls">
          <label className="field field-grow">
            <span>{meta.label}</span>
            <input value={draft} onChange={(e) => setDraft(e.target.value)} disabled={busy} />
          </label>
        </div>
      )}

      {isAdmin && (
        <div className="reports-controls">
          {editing ? (
            <>
              <button className="link-btn" onClick={save} disabled={busy}>
                {busy ? <span className="spinner" aria-hidden="true" /> : "Save"}
              </button>
              <button className="link-btn ghost" onClick={() => setEditing(false)} disabled={busy}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button className="link-btn ghost" onClick={startEdit} disabled={busy}>
                Edit
              </button>
              {configured && (
                <button className="link-btn ghost" onClick={() => onReset(meta.name)} disabled={busy}>
                  {busy ? <span className="spinner" aria-hidden="true" /> : "Reset to default"}
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function DeploymentConfigPanel({
  apiBase,
  token,
  isAdmin = false,
  mock = false,
}: {
  apiBase: string;
  token?: string;
  isAdmin?: boolean;
  mock?: boolean;
}) {
  const [fields, setFields] = useState<Record<string, ConfigField> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingField, setSavingField] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    const load = mock ? Promise.resolve(MOCK_CONFIG) : fetchConfig(apiBase, token);
    load
      .then((res) => {
        if (!cancelled) setFields(res.fields);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, token, mock]);

  async function apply(name: string, value: unknown) {
    setSavingField(name);
    setSaveNotice(null);
    try {
      if (mock) {
        // No backend to persist to in mock mode — reflect the change locally
        // so the screen still shows a save taking effect.
        setFields((cur) => {
          const prior = cur?.[name];
          return {
            ...(cur ?? {}),
            [name]:
              value === null
                ? { value: prior?.value ?? null, source: "default" }
                : { value, source: "configured" },
          };
        });
      } else {
        const res = await updateConfig(apiBase, { [name]: value }, token);
        setFields(res.fields);
      }
      setSaveNotice({ ok: true, text: "Saved." });
    } catch (e) {
      setSaveNotice({ ok: false, text: errorMessage(e) });
    } finally {
      setSavingField(null);
    }
  }

  return (
    <div className="reports">
      <div className="reports-head">
        <h1 className="reports-title">Deployment Config</h1>
        <p className="reports-lede">
          Business configuration read by the scheduled Lambdas and the Matter Search agent —
          organization details, letterhead, exclusion lists, and which SharePoint sites to index.
          {!isAdmin && " Only an administrator can change these."}
        </p>
      </div>

      {saveNotice && (
        <p className={`banner ${saveNotice.ok ? "banner-ok" : "banner-error"}`}>{saveNotice.text}</p>
      )}

      {loading && (
        <div className="status-note">
          <span className="spinner" aria-hidden="true" />
          Loading configuration…
        </div>
      )}

      {!loading && loadError && <p className="banner banner-error">{loadError}</p>}

      {!loading &&
        !loadError &&
        fields &&
        SECTIONS.map((section) => (
          <div key={section.title}>
            <div className="reports-head" style={{ marginTop: "1.6rem" }}>
              <h2 className="reports-title" style={{ fontSize: "1.15rem" }}>
                {section.title}
              </h2>
              <p className="reports-lede">{section.blurb}</p>
            </div>
            {section.fields.map((meta) => (
              <ConfigFieldRow
                key={meta.name}
                meta={meta}
                field={fields[meta.name]}
                isAdmin={isAdmin}
                busy={savingField === meta.name}
                onSave={(name, value) => apply(name, value)}
                onReset={(name) => apply(name, null)}
              />
            ))}
          </div>
        ))}
    </div>
  );
}
