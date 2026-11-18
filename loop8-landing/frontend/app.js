// Loop8 landing page — subscribe form wiring for setup.html.
//
// Plain vanilla JS, no framework/build step (see task brief). Only wires up
// behavior when the expected elements exist on the page, so this same file
// can be loaded (harmlessly) from index.html too, which has no form.

// Separate API subdomain (api.deploy.loop8.io), not a relative path — see
// docs/superpowers/specs/2026-11-17-marketplace-landing-page-design.md.
const SUBSCRIBE_URL = "https://api.deploy.loop8.io/subscribe";

function showResult(container, form, node, cssClass) {
  form.hidden = true;
  container.innerHTML = "";
  container.appendChild(node);
  container.className = "result " + cssClass;
  container.hidden = false;
}

function renderSuccess(container, form, launchUrl) {
  const fragment = document.createDocumentFragment();

  const intro = document.createElement("p");
  intro.textContent =
    "You're all set. Click below to launch Aphrodite in your own AWS account via CloudFormation.";
  fragment.appendChild(intro);

  const link = document.createElement("a");
  link.className = "launch-link";
  link.setAttribute("href", launchUrl);
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
  link.textContent = "Launch Aphrodite";
  fragment.appendChild(link);

  showResult(container, form, fragment, "success");
}

function renderError(container, form, message) {
  const paragraph = document.createElement("p");
  paragraph.textContent = message;
  showResult(container, form, paragraph, "error");
}

function initSetupForm() {
  const form = document.getElementById("setup-form");
  const result = document.getElementById("result");
  if (!form || !result) {
    return;
  }

  const sessionId = new URLSearchParams(window.location.search).get("session");
  if (!sessionId) {
    renderError(
      result,
      form,
      "Missing or invalid session. Please start again from the AWS Marketplace listing."
    );
    return;
  }

  form.hidden = false;

  form.addEventListener("submit", function (event) {
    event.preventDefault();

    const submitButton = form.querySelector('button[type="submit"]');
    submitButton.disabled = true;

    const companyName = document.getElementById("company_name").value.trim();
    const adminEmail = document.getElementById("admin_email").value.trim();

    fetch(SUBSCRIBE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        company_name: companyName,
        admin_email: adminEmail,
      }),
    })
      .then(function (response) {
        return response.json().then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (outcome) {
        if (outcome.ok && outcome.data && outcome.data.launch_url) {
          renderSuccess(result, form, outcome.data.launch_url);
        } else {
          const reason = (outcome.data && outcome.data.error) || "Something went wrong. Please try again.";
          renderError(result, form, reason);
        }
      })
      .catch(function () {
        renderError(
          result,
          form,
          "Could not reach the server. Please check your connection and try again."
        );
      })
      .finally(function () {
        submitButton.disabled = false;
      });
  });
}

document.addEventListener("DOMContentLoaded", initSetupForm);
