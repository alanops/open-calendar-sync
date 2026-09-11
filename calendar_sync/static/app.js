"use strict";
let csrf = "",
  state = null,
  disconnectId = null,
  pending = false;
const $ = (id) => document.getElementById(id);
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = false;
}
async function api(path, data) {
  const response = await fetch(path, {
    method: data === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      $("login-panel").hidden = false;
      $("dashboard").hidden = true;
    }
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Request failed. Please try again.",
    );
  }
  return result;
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
async function action(button, work) {
  if (pending) return;
  pending = true;
  button.disabled = true;
  $("notice").hidden = true;
  try {
    await work();
  } catch (error) {
    notice(error.message, true);
  } finally {
    pending = false;
    button.disabled = false;
  }
}
function renderAccount(account) {
  const row = node("div", undefined, "account");
  row.append(
    node(
      "div",
      account.provider === "google" ? "G" : "⊞",
      "provider-avatar " + account.provider + "-icon",
    ),
  );
  const info = node("div", undefined, "account-info");
  info.append(
    node("strong", account.email),
    node(
      "small",
      (account.provider === "google" ? "Google" : "Microsoft 365") +
        " · Primary calendar",
    ),
  );
  const actions = node("div", undefined, "account-actions");
  const label = node("label"),
    toggle = node("input");
  toggle.type = "checkbox";
  toggle.checked = Boolean(account.enabled);
  toggle.setAttribute("aria-label", "Include " + account.email + " in sync");
  toggle.addEventListener("change", () =>
    action(toggle, async () => {
      try {
        await api("/api/accounts/" + account.id + "/enabled", {
          enabled: toggle.checked,
        });
      } finally {
        await refresh();
      }
    }),
  );
  label.append(toggle, node("span", "Sync"));
  const disconnect = node("button", "×", "text-button disconnect");
  disconnect.setAttribute("aria-label", "Disconnect " + account.email);
  disconnect.addEventListener("click", () => {
    disconnectId = account.id;
    $("disconnect-email").textContent = account.email;
    $("disconnect-dialog").showModal();
  });
  actions.append(label, disconnect);
  row.append(info, actions);
  return row;
}
async function refresh() {
  state = await api("/api/status");
  csrf = state.csrf;
  $("login-panel").hidden = true;
  $("dashboard").hidden = false;
  $("logout").hidden = false;
  $("account-count").textContent = state.accounts.length;
  $("copy-count").textContent = state.managed_copies;
  $("horizon").textContent = state.days_ahead;
  $("interval").textContent =
    "Every " + Math.round(state.interval_seconds / 60) + " minutes";
  $("sync-badge").textContent = state.running
    ? "Sync in progress"
    : state.paused
      ? "Sync paused"
      : "Automatic sync on";
  $("sync-badge").classList.toggle("active", !state.paused);
  $("toggle-sync").textContent = state.paused
    ? "Start automatic sync →"
    : "Pause automatic sync";
  $("sync-now").disabled = state.paused || state.running;
  $("preview").disabled =
    state.running || state.accounts.filter((a) => a.enabled).length < 2;
  $("toggle-sync").disabled =
    state.running || state.accounts.filter((a) => a.enabled).length < 2;
  $("accounts").replaceChildren(...state.accounts.map(renderAccount));
  $("empty-state").hidden = state.accounts.length > 0;
  const missing = [];
  for (const provider of ["google", "microsoft"]) {
    $("connect-" + provider).disabled = !state.providers[provider];
    if (!state.providers[provider])
      missing.push(provider === "google" ? "Google" : "Microsoft");
  }
  $("provider-setup").hidden = missing.length === 0;
  $("provider-setup").textContent =
    "Setup needed: add " +
    missing.join(" and ") +
    " OAuth app credentials to your server’s .env file, then restart. The self-hosting guide below walks through this.";
  $("activity").replaceChildren();
  if (!state.runs.length)
    $("activity").append(
      node("p", "Your first sync will appear here.", "hint"),
    );
  for (const run of state.runs) {
    const row = node("div", undefined, "activity-row");
    const stamp = new Date(run.at * 1000),
      when = node(
        "time",
        stamp.toLocaleString([], {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        }),
      );
    when.dateTime = stamp.toISOString();
    row.append(
      node("span", run.status === "success" ? "✓" : "!", run.status),
      node("span", run.message),
      when,
    );
    $("activity").append(row);
  }
}
$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    await api("/api/login", { token: $("access-key").value });
    $("access-key").value = "";
    await refresh();
  });
});
$("logout").addEventListener("click", () =>
  action($("logout"), async () => {
    await api("/api/logout", {});
    location.reload();
  }),
);
for (const provider of ["google", "microsoft"]) {
  $("connect-" + provider).addEventListener("click", () =>
    action($("connect-" + provider), async () => {
      const result = await api("/api/connect/" + provider, {});
      location.assign(result.url);
    }),
  );
}
$("preview").addEventListener("click", () =>
  action($("preview"), async () => {
    $("preview-result").textContent = "Reading selected calendars…";
    try {
      const result = await api("/api/preview", {});
      $("preview-result").textContent =
        result.busy_copies +
        " busy copies across " +
        result.accounts +
        " calendars in the sync window. No changes made.";
    } catch (error) {
      $("preview-result").textContent =
        "Preview could not complete. No changes made.";
      throw error;
    }
  }),
);
$("toggle-sync").addEventListener("click", () =>
  action($("toggle-sync"), async () => {
    const resume = state.paused;
    await api("/api/pause", { paused: !resume });
    await refresh();
    if (resume) {
      notice("Sync started. Reading your calendars…");
      const result = await api("/api/sync", {});
      notice(result.message, result.status === "error");
      await refresh();
    }
  }),
);
$("sync-now").addEventListener("click", () =>
  action($("sync-now"), async () => {
    notice("Reading your calendars…");
    const result = await api("/api/sync", {});
    notice(result.message, result.status === "error");
    await refresh();
  }),
);
$("refresh").addEventListener("click", () => action($("refresh"), refresh));
$("cancel-disconnect").addEventListener("click", () =>
  $("disconnect-dialog").close(),
);
$("confirm-disconnect").addEventListener("click", () =>
  action($("confirm-disconnect"), async () => {
    await api("/api/accounts/" + disconnectId + "/disconnect", {});
    $("disconnect-dialog").close();
    await refresh();
    notice("Disconnected. Busy copies cleaned up and sync paused.");
  }),
);
refresh()
  .then(() => {
    const result = new URLSearchParams(location.search).get("connection");
    if (result) {
      notice(
        result === "success"
          ? "Calendar connected. Select it for sync, then preview your busy blocks."
          : "Connection cancelled.",
      );
      history.replaceState({}, "", "/");
    }
  })
  .catch((error) => {
    $("login-panel").hidden = false;
    if (!error.message.includes("Sign in")) notice(error.message, true);
  });
setInterval(() => {
  if (
    !pending &&
    state &&
    !$("dashboard").hidden &&
    !$("disconnect-dialog").open
  )
    refresh().catch(() => {});
}, 30000);
