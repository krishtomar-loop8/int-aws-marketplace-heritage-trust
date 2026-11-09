// Per-connector connect/reconnect control for the Integrations tab. Each
// connector's auth model is different (see docs/superpowers/specs/
// 2026-11-06-connector-onboarding-design.md), so each gets its own small
// form rather than one generic control. Admin-only — IntegrationsPanel
// renders this only when isAdmin is true.

import { useState } from "react";
import {
  clioAuthorizeUrl,
  clioExchange,
  connectEstateably,
  connectGraph,
  quickbooksStart,
} from "../api";
import { useIntegrationsHealth } from "../lib/integrationsHealth";
import type { ConnectorActionResult } from "../types";

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong.";
}

interface CardProps {
  apiBase: string;
  token?: string;
}

// A 200 response doesn't always mean "working" — the credential may have
// validated but the fuller connectivity check still failed (e.g. missing
// permissions). Show that distinction instead of treating any 200 as success.
function ResultNotice({ result }: { result: ConnectorActionResult | null }) {
  if (!result) return null;
  if (result.ok) {
    return <p className="banner banner-ok">Connected — validated.</p>;
  }
  return (
    <p className="banner banner-error">
      Credential saved, but the connection check failed: {result.detail || "unknown error"}
    </p>
  );
}

function QuickBooksCard({ apiBase, token }: CardProps) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [environment, setEnvironment] = useState("sandbox");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      const result = await quickbooksStart(
        apiBase,
        { client_id: clientId, client_secret: clientSecret, environment },
        token,
      );
      // The browser leaves the app here — QuickBooks redirects back to the
      // hosted callback, which redirects again to this app when it's done.
      window.location.href = result.url;
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <div className="connector-card">
      <label className="field">
        <span>Client ID</span>
        <input value={clientId} onChange={(e) => setClientId(e.target.value)} />
      </label>
      <label className="field">
        <span>Client secret</span>
        <input type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
      </label>
      <label className="field">
        <span>Environment</span>
        <select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
          <option value="sandbox">Sandbox</option>
          <option value="production">Production</option>
        </select>
      </label>
      <button className="link-btn" onClick={connect} disabled={busy || !clientId || !clientSecret}>
        {busy ? <span className="spinner" aria-hidden="true" /> : "Connect"}
      </button>
      {error && <p className="banner banner-error">{error}</p>}
    </div>
  );
}

function ClioCard({ apiBase, token }: CardProps) {
  const { refresh } = useIntegrationsHealth();
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectorActionResult | null>(null);

  async function getLink() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const body = { client_id: clientId, ...(baseUrl ? { base_url: baseUrl } : {}) };
      const link = await clioAuthorizeUrl(apiBase, body, token);
      setAuthorizeUrl(link.url);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function exchange() {
    setBusy(true);
    setError(null);
    try {
      const body = {
        client_id: clientId,
        client_secret: clientSecret,
        code,
        ...(baseUrl ? { base_url: baseUrl } : {}),
      };
      const r = await clioExchange(apiBase, body, token);
      setResult(r);
      await refresh();
      setAuthorizeUrl(null);
      setCode("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="connector-card">
      <label className="field">
        <span>Client ID</span>
        <input value={clientId} onChange={(e) => setClientId(e.target.value)} />
      </label>
      <label className="field">
        <span>Client secret</span>
        <input type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
      </label>
      <label className="field">
        <span>Region / pod (optional — leave blank for the US pod)</span>
        <input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://ca.app.clio.com/api/v4"
        />
      </label>
      <button className="link-btn" onClick={getLink} disabled={busy || !clientId}>
        {busy && !authorizeUrl ? <span className="spinner" aria-hidden="true" /> : "Generate authorization link"}
      </button>
      {authorizeUrl && (
        <>
          <p>
            Send this link to a Clio user with full access:{" "}
            <a href={authorizeUrl} target="_blank" rel="noreferrer">
              {authorizeUrl}
            </a>
          </p>
          <label className="field">
            <span>Code Clio showed after approval</span>
            <input value={code} onChange={(e) => setCode(e.target.value)} />
          </label>
          <button className="link-btn" onClick={exchange} disabled={busy || !code || !clientSecret}>
            {busy ? <span className="spinner" aria-hidden="true" /> : "Connect"}
          </button>
        </>
      )}
      {error && <p className="banner banner-error">{error}</p>}
      <ResultNotice result={result} />
    </div>
  );
}

function EstateablyCard({ apiBase, token }: CardProps) {
  const { refresh } = useIntegrationsHealth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectorActionResult | null>(null);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      const r = await connectEstateably(
        apiBase,
        { username, password, ...(baseUrl ? { base_url: baseUrl } : {}) },
        token,
      );
      setResult(r);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="connector-card">
      <label className="field">
        <span>Username</span>
        <input value={username} onChange={(e) => setUsername(e.target.value)} />
      </label>
      <label className="field">
        <span>Password</span>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
      </label>
      <label className="field">
        <span>Base URL (optional)</span>
        <input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://app.estateably.com"
        />
      </label>
      <button className="link-btn" onClick={connect} disabled={busy || !username || !password}>
        {busy ? <span className="spinner" aria-hidden="true" /> : "Save & validate"}
      </button>
      {error && <p className="banner banner-error">{error}</p>}
      <ResultNotice result={result} />
    </div>
  );
}

function GraphCard({ apiBase, token }: CardProps) {
  const { refresh } = useIntegrationsHealth();
  const [tenantId, setTenantId] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectorActionResult | null>(null);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      const r = await connectGraph(
        apiBase,
        { tenant_id: tenantId, client_id: clientId, client_secret: clientSecret },
        token,
      );
      setResult(r);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="connector-card">
      <p className="connector-card-hint">
        Requires an Entra app registration with admin consent granted first — see the setup guide.
      </p>
      <label className="field">
        <span>Tenant ID</span>
        <input value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
      </label>
      <label className="field">
        <span>Client ID</span>
        <input value={clientId} onChange={(e) => setClientId(e.target.value)} />
      </label>
      <label className="field">
        <span>Client secret</span>
        <input type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
      </label>
      <button
        className="link-btn"
        onClick={connect}
        disabled={busy || !tenantId || !clientId || !clientSecret}
      >
        {busy ? <span className="spinner" aria-hidden="true" /> : "Save & validate"}
      </button>
      {error && <p className="banner banner-error">{error}</p>}
      <ResultNotice result={result} />
    </div>
  );
}

export function ConnectorCard({
  integration,
  apiBase,
  token,
}: {
  integration: string;
  apiBase: string;
  token?: string;
}) {
  switch (integration) {
    case "quickbooks":
      return <QuickBooksCard apiBase={apiBase} token={token} />;
    case "clio":
      return <ClioCard apiBase={apiBase} token={token} />;
    case "estateably":
      return <EstateablyCard apiBase={apiBase} token={token} />;
    case "graph":
      return <GraphCard apiBase={apiBase} token={token} />;
    default:
      return null;
  }
}
