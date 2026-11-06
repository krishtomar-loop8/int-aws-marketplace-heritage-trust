import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AuthProvider, useAuth } from "react-oidc-context";
import { App } from "./App";
import { Login } from "./components/Login";
import { loadConfig, type AppConfig } from "./config";
import { clearThread } from "./lib/threadStore";
import "./styles.css";

function Splash({ message, error }: { message: string; error?: boolean }) {
  return (
    <div className="splash">
      <div className="splash-card">
        <span className="splash-logo wordmark">Aphrodite</span>
        <p className="splash-tagline">AI Integration Platform</p>
        <div className={`splash-status${error ? " is-error" : ""}`}>
          {!error && <span className="spinner" aria-hidden="true" />}
          <span>{message}</span>
        </div>
      </div>
    </div>
  );
}

function AuthedApp({ config }: { config: AppConfig }) {
  const auth = useAuth();

  if (auth.isLoading) return <Splash message="Signing you in…" />;
  if (auth.error) return <Splash message={`Sign-in error: ${auth.error.message}`} error />;
  if (!auth.isAuthenticated) {
    return (
      <Login
        onMicrosoft={() =>
          void auth.signinRedirect({ extraQueryParams: { identity_provider: "EntraID" } })
        }
        onStaff={() => void auth.signinRedirect()}
      />
    );
  }

  const groups = auth.user?.profile?.["cognito:groups"];
  const isAdmin = Array.isArray(groups) && groups.includes("admins");

  return (
    <App
      apiBase={config.apiEndpoint}
      // The ID token (not the access token): its aud is the app client id, so the
      // API's JWT authorizer accepts it, and it carries the `email` claim the
      // activity log needs for its "Who" column — Cognito access tokens don't.
      token={auth.user?.id_token}
      mock={false}
      user={(auth.user?.profile.email as string) ?? undefined}
      isAdmin={isAdmin}
      onSignOut={() => {
        clearThread(); // don't leave client matter data for the next user on this tab
        void auth.removeUser().then(() => window.location.reload());
      }}
    />
  );
}

async function main() {
  const config = await loadConfig();
  const root = createRoot(document.getElementById("root")!);

  if (config.mock) {
    root.render(
      <StrictMode>
        <App apiBase="" mock user="demo@aphrodite.example" />
      </StrictMode>,
    );
    return;
  }

  root.render(
    <StrictMode>
      <AuthProvider
        authority={config.cognito.authority}
        client_id={config.cognito.clientId}
        redirect_uri={config.cognito.redirectUri}
        scope={config.cognito.scope}
        response_type="code"
        onSigninCallback={() =>
          window.history.replaceState({}, document.title, window.location.pathname)
        }
      >
        <AuthedApp config={config} />
      </AuthProvider>
    </StrictMode>,
  );
}

void main();
