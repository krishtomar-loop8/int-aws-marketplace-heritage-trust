// Branded sign-in page. Replaces the auto-redirect to the Cognito Hosted UI so
// we control the wording + logo: a "Sign in with your Microsoft organisation
// account" button (jumps straight to Microsoft/Entra) plus a discreet
// break-glass link to the native username/password form.

function MicrosoftLogo() {
  // Microsoft's four-square mark (brand colours), inline so it's self-contained.
  return (
    <svg className="ms-logo" viewBox="0 0 21 21" aria-hidden="true">
      <rect x="1" y="1" width="9" height="9" fill="#f25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
      <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  );
}

export function Login({
  onMicrosoft,
  onStaff,
}: {
  onMicrosoft: () => void;
  onStaff: () => void;
}) {
  return (
    <div className="splash">
      <div className="splash-card">
        <span className="splash-logo wordmark">Aphrodite</span>
        <p className="splash-tagline">AI Integration Platform</p>

        <button className="ms-button" onClick={onMicrosoft}>
          <MicrosoftLogo />
          <span>Sign in with your Microsoft organisation account</span>
        </button>

        <button className="login-alt" onClick={onStaff}>
          Staff sign-in (username &amp; password)
        </button>
      </div>
    </div>
  );
}
