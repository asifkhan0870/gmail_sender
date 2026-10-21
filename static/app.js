
const $ = (id) => document.getElementById(id);

let validation = null;
let activeJobId = null;
let pollTimer = null;
let toastTimer = null;

function toast(message) {
  const el = $("toast");
  if (!el) {
    console.log(message);
    return;
  }

  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3800);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[c]));
}

function getAccessKey() {
  const field = $("accessKey");
  return field?.value?.trim() || "";
}

function appendAccessKey(form) {
  const key = getAccessKey();
  if (!key) {
    throw new Error("Enter your dashboard access key first.");
  }
  form.append("access_key", key);
  return key;
}

async function getJson(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const key = getAccessKey();

  if (key) {
    headers.set("X-Dashboard-Key", key);
  }

  const response = await fetch(url, {
    ...options,
    headers
  });

  let data = {};
  try {
    data = await response.json();
  } catch {
    // Response may not contain JSON.
  }

  if (!response.ok) {
    throw new Error(
      data.detail || `Request failed (${response.status})`
    );
  }

  return data;
}

async function refreshStats() {
  try {
    const data = await getJson("/api/status");
    const stats = data.stats;

    $("statSent").textContent = stats.sent_total;
    $("statToday").textContent = `${stats.sent_today} / ${stats.daily_cap}`;
    $("dailyCap").textContent = stats.daily_cap;
    $("statErrors").textContent = stats.failed_total;
    $("statUnknown").textContent = stats.unknown_total;

    $("apiStatus").textContent = data.send_enabled
      ? "Sending enabled"
      : "Sending disabled";

    const accounts = data.connected_accounts || [];
    const selectedBefore = $("senderEmail").value;

    $("senderEmail").innerHTML = accounts.length
      ? accounts.map((email) =>
          `<option value="${escapeHtml(email)}">${escapeHtml(email)}</option>`
        ).join("")
      : '<option value="">Connect a Gmail account first</option>';

    if (accounts.includes(selectedBefore)) {
      $("senderEmail").value = selectedBefore;
    } else if (data.sender_email && accounts.includes(data.sender_email)) {
      $("senderEmail").value = data.sender_email;
    }

    $("accountHint").textContent = accounts.length
      ? `${accounts.length} account(s) connected. Choose the account you want to use.`
      : "No Gmail account connected. Click Connect Gmail to authorize one.";

    renderActivity(stats.recent || []);

    $("sendDialogNote").textContent = data.send_enabled
      ? "Live sending is enabled. Review the final confirmation carefully."
      : "Live sending is disabled by default on the backend.";

    $("sendBtn").title = data.send_enabled
      ? ""
      : "Live sending is disabled on the backend";

  } catch (e) {
    $("apiStatus").textContent = "Backend unavailable";
    toast(e.message);
  }
}

function renderActivity(rows) {
  const body = $("activityRows");

  if (!rows.length) {
    body.innerHTML =
      '<tr><td colspan="4" class="empty-row">No activity yet.</td></tr>';
    return;
  }

  body.innerHTML = rows.slice(0, 12).map((r) => {
    const status = r.status || "UNKNOWN";
    const detail = (r.detail || "").startsWith("gmail_message_id=")
      ? "Accepted by Gmail"
      : (r.detail || "—");

    return `<tr>
      <td>${escapeHtml(r.email)}</td>
      <td><span class="status-tag status-${escapeHtml(status)}">${escapeHtml(status)}</span></td>
      <td>${escapeHtml((r.timestamp || "").replace("T", " ").slice(0, 19))}</td>
      <td>${escapeHtml(detail)}</td>
    </tr>`;
  }).join("");
}

$("csvFile").addEventListener("change", () => {
  const file = $("csvFile").files[0];
  validation = null;

  $("sendBtn").disabled = true;
  $("validateBtn").disabled = !file;
  $("fileTitle").textContent = file ? file.name : "Drop your CSV here";
  $("fileSub").textContent = file
    ? `${(file.size / 1024).toFixed(1)} KB · ready to validate`
    : "or browse files from your computer";

  $("recipientStats").classList.add("hidden");
  $("recipientPreview").classList.add("hidden");
});

const dropzone = $("dropzone");

["dragenter", "dragover"].forEach((ev) => {
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((ev) => {
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragging");
  });
});

dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (!file) return;

  if (!file.name.toLowerCase().endsWith(".csv")) {
    return toast("Please drop a .csv file.");
  }

  const dt = new DataTransfer();
  dt.items.add(file);
  $("csvFile").files = dt.files;
  $("csvFile").dispatchEvent(new Event("change"));
});

$("validateBtn").addEventListener("click", async () => {
  const file = $("csvFile").files[0];

  if (!file) return toast("Choose a CSV file first.");

  $("validateBtn").disabled = true;
  $("validateBtn").textContent = "Validating…";

  try {
    const form = new FormData();
    form.append("file", file);
    appendAccessKey(form);

    const result = await getJson("/api/validate", {
      method: "POST",
      body: form
    });

    validation = result;

    $("eligibleCount").textContent = result.eligible_count;
    $("pendingCount").textContent = result.pending_count;
    $("duplicateCount").textContent = result.duplicates;
    $("invalidCount").textContent = result.invalid;

    $("recipientStats").classList.remove("hidden");
    $("recipientPreview").classList.remove("hidden");

    $("recipientPreview").innerHTML =
      `<b>Pending recipient preview</b><br>` +
      `${result.sample.map(escapeHtml).join("<br>") || "No pending recipients"}` +
      `${result.pending_count > result.sample.length
        ? `<br>…and ${result.pending_count - result.sample.length} more`
        : ""}` +
      `<br><span>Suppressed: ${result.suppressed} · Previously processed: ${result.previously_processed}</span>`;

    $("sendBtn").disabled = result.pending_count === 0;
    toast(`CSV validated. ${result.pending_count} pending recipient(s).`);

  } catch (e) {
    toast(e.message);
  } finally {
    $("validateBtn").disabled = !$("csvFile").files[0];
    $("validateBtn").innerHTML = 'Validate CSV <span>→</span>';
  }
});

const exampleBody = $("body").value;

$("restoreBtn").addEventListener("click", () => {
  $("body").value = exampleBody;
  toast("Example message restored.");
});

$("connectBtn").addEventListener("click", async () => {
  const btn = $("connectBtn");
  btn.disabled = true;
  btn.textContent = "Opening Google…";

  try {
    const form = new FormData();
    appendAccessKey(form);

    const data = await getJson("/api/connect", {
      method: "POST",
      body: form
    });

    if (!data.authorization_url) {
      throw new Error("Google authorization URL was not returned.");
    }

    window.location.assign(data.authorization_url);

  } catch (e) {
    toast(e.message || "Could not start Google login.");
    btn.disabled = false;
    btn.textContent = "Connect Gmail";
  }
});

function showPreview() {
  if (!validation) {
    return toast("Upload and validate a CSV first.");
  }

  $("previewSubject").textContent = $("subject").value || "(No subject)";
  $("previewFrom").textContent = $("senderEmail").value || "your account";

  $("previewTo").textContent = validation.pending_count
    ? `${validation.pending[0]}${validation.pending_count > 1
        ? ` and ${validation.pending_count - 1} more`
        : ""}`
    : "no recipients selected";

  $("previewBody").textContent = $("body").value;
  $("previewDialog").showModal();
}

$("previewBtn").addEventListener("click", showPreview);

$("sendBtn").addEventListener("click", () => {
  if (!validation || !validation.pending_count) {
    return toast("Validate a CSV with pending recipients first.");
  }

  if (!$("senderEmail").value.trim()) {
    return toast("Connect a Gmail account first.");
  }

  if (!$("subject").value.trim() || !$("body").value.trim()) {
    return toast("Subject and message body are required.");
  }

  $("reviewRecipients").textContent = validation.pending_count;
  $("reviewSender").textContent = $("senderEmail").value.trim();
  $("reviewSubject").textContent = $("subject").value.trim();

  $("consentCheck").checked = false;
  $("confirmCheck").checked = false;
  $("confirmSendBtn").disabled = true;

  $("sendDialog").showModal();
});

function updateConfirm() {
  $("confirmSendBtn").disabled = !(
    $("consentCheck").checked &&
    $("confirmCheck").checked
  );
}

$("consentCheck").addEventListener("change", updateConfirm);
$("confirmCheck").addEventListener("change", updateConfirm);

$("confirmSendBtn").addEventListener("click", async () => {
  if (!validation) return;

  $("confirmSendBtn").disabled = true;
  $("confirmSendBtn").textContent = "Starting…";

  try {
    const data = await getJson("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipients: validation.pending,
        subject: $("subject").value,
        body: $("body").value,
        sender_email: $("senderEmail").value.trim(),
        access_key: getAccessKey(),
        confirm: $("confirmCheck").checked,
        consent_confirmed: $("consentCheck").checked,
        delay_seconds: 10
      })
    });

    activeJobId = data.job_id;
    $("sendDialog").close();

    toast(
      `Send job ${data.job_id} queued for ${data.recipient_count} recipient(s).`
    );

    pollJob();

  } catch (e) {
    toast(e.message);
  } finally {
    $("confirmSendBtn").textContent = "Confirm send →";
    updateConfirm();
  }
});

async function pollJob() {
  if (!activeJobId) return;

  try {
    const data = await getJson(
      `/api/jobs/${encodeURIComponent(activeJobId)}`
    );

    if (data.status === "finished" || data.status === "failed") {
      clearTimeout(pollTimer);
      activeJobId = null;

      if (data.status === "failed") {
        toast(`Send job failed: ${data.error || "Unknown error"}`);
      } else {
        toast(`Job finished: ${data.sent || 0} accepted, ${data.errors || 0} errors.`);
      }

      await refreshStats();
      validation = null;
      $("sendBtn").disabled = true;
      return;
    }

    $("apiStatus").textContent =
      data.status === "running" ? "Sending…" : "Queued";

    pollTimer = setTimeout(pollJob, 2000);

  } catch (e) {
    clearTimeout(pollTimer);
    toast(e.message);
  }
}

refreshStats();
