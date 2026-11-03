// Runtime config, fetched from /config.json so the same build works across
// environments (Terraform writes the real values on deploy). In mock mode the
// app renders sample data without auth or a live API — used for local design
// review and Playwright screenshots.

export interface AppConfig {
  apiEndpoint: string;
  cognito: {
    authority: string;
    clientId: string;
    redirectUri: string;
    scope: string;
  };
  mock: boolean;
}

export async function loadConfig(): Promise<AppConfig> {
  try {
    const res = await fetch("/config.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`config.json ${res.status}`);
    return (await res.json()) as AppConfig;
  } catch {
    // No config → safe mock default (local dev).
    return {
      apiEndpoint: "",
      cognito: { authority: "", clientId: "", redirectUri: "", scope: "openid email profile" },
      mock: true,
    };
  }
}
