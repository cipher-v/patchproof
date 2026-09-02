const state = { runs: [], selected: 0, activeRunId: null };

const $ = (id) => document.getElementById(id);
const shortSha = (value) => value ? value.slice(0, 8) : "—";
const compactId = (value) => value ? `${value.slice(0, 8)}…${value.slice(-4)}` : "—";
const formatDuration = (value) => Number.isFinite(value) ? `${value.toFixed(2)}s` : "—";
const displayEnum = (value) => value ? value.replaceAll("_", " ") : "—";

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

function appendIdentity(parent, label, value, href) {
  const item = href ? node("a", "", `${label} · ${value}`) : node("span", "", `${label} · ${value}`);
  if (href) {
    item.href = href;
    item.target = "_blank";
    item.rel = "noreferrer";
  }
  parent.append(item);
}

function outcomeTone(run) {
  if (run.status === "FAILED") return "failure";
  return run.claim_outcome === "CLAIM_SUPPORTED_FOR_SCENARIO" ? "success" : "neutral";
}

function runLabel(run) {
  if (!["COMPLETE", "ABSTAINED"].includes(run.status)) return displayEnum(run.status);
  return run.claim_outcome === "CLAIM_SUPPORTED_FOR_SCENARIO" ? "SUPPORTED" : run.status;
}

function renderTabs() {
  const tabs = $("run-tabs");
  tabs.replaceChildren();
  state.runs.forEach((run, index) => {
    const button = node("button", "run-tab");
    button.type = "button";
    button.role = "tab";
    button.ariaSelected = String(index === state.selected);
    button.tabIndex = index === state.selected ? 0 : -1;
    button.addEventListener("click", () => selectRun(index));
    button.addEventListener("keydown", (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const delta = event.key === 'ArrowRight' ? 1 : -1;
      const next = (index + delta + state.runs.length) % state.runs.length;
      selectRun(next);
      tabs.children[next].focus();
    });
    const badge = node("b", outcomeTone(run), runLabel(run));
    const title = node("strong", "", `${run.event_action.toUpperCase()} · PR #${run.pr_number}`);
    title.prepend(badge);
    button.append(title, node("span", "", compactId(run.run_id)));
    tabs.append(button);
  });
}

function selectRun(index) {
  state.selected = index;
  [...$("run-tabs").children].forEach((tab, tabIndex) => {
    tab.ariaSelected = String(tabIndex === index);
    tab.tabIndex = tabIndex === index ? 0 : -1;
  });
  renderRun(state.runs[index]);
  state.activeRunId = state.runs[index].run_id;
  const url = new URL(window.location.href);
  url.searchParams.set("run", state.activeRunId);
  window.history.replaceState({}, "", url);
}

function renderLifecycle(run) {
  const labels = ["Accepted", "Claim", "Candidate", "BASE / HEAD", "Terminal"];
  const active = {
    ACCEPTED: 0,
    QUEUED: 0,
    PREPARING_CONTEXT: 0,
    SELECTING_CLAIM: 1,
    GENERATING_CANDIDATE: 2,
    RUNNING_BASE_HEAD: 3,
    ASSESSING_SEMANTICS: 3,
    FINALIZING: 4,
    COMPLETE: 4,
    ABSTAINED: 4,
    FAILED: 4,
  }[run.status] ?? 0;
  const rail = $("lifecycle-rail");
  rail.replaceChildren(...labels.map((label, index) => {
    const item = node("li", index > active ? "incomplete" : "", label);
    if (run.status === "FAILED" && index === active) item.classList.add("failed");
    return item;
  }));
}

function renderClaim(run) {
  const claim = run.claim;
  $("claim-summary").textContent = claim?.summary || "No sufficiently grounded claim was selected.";
  $("claim-reasoning").textContent = claim?.reasoning_summary || "PatchProof abstained before claim execution.";
  $("claim-precondition").textContent = claim?.preconditions?.[0] || "Not available";
  $("claim-action").textContent = claim?.action || "Not available";
  $("claim-expected").textContent = claim?.expected_behavior || "Not available";
}

function renderCandidates(run) {
  const list = $("candidate-list");
  list.replaceChildren();
  if (!run.candidates.length) {
    list.append(node("p", "muted", "No candidate artifact was generated."));
    return;
  }
  run.candidates.forEach((candidate) => {
    const item = node("div", "candidate");
    const head = node("div", "candidate-head");
    const identity = node("div");
    identity.append(node("strong", "", `Attempt ${candidate.sequence} · ${candidate.candidate_id || "invalid"}`));
    identity.append(node("span", "", candidate.origin));
    head.append(identity, node("span", "candidate-status", candidate.status));
    const details = node("div", "candidate-details");
    details.append(node("p", "", candidate.rationale || "No rationale retained."));
    if (candidate.feedback_summary) {
      const feedback = node("p", "candidate-feedback", `Repair feedback · ${candidate.feedback_summary}`);
      details.append(feedback);
    }
    const disclosure = document.createElement("details");
    const summary = node("summary", "", `View immutable candidate · ${shortSha(candidate.artifact_sha256)}`);
    const source = node("pre", "", candidate.source || "Candidate source unavailable.");
    disclosure.append(summary, source);
    details.append(disclosure);
    item.append(head, details);
    list.append(item);
  });
}

function resultClass(status) {
  if (status === "PASSED") return "pass";
  if (status === "ASSERTION_FAILED") return "fail";
  return "error";
}

function renderExecution(target, execution) {
  target.className = `revision-result ${resultClass(execution?.status)}`;
  target.replaceChildren();
  if (!execution) {
    target.append(node("span", "", "NO EXECUTION"), node("strong", "", "NOT AVAILABLE"));
    return;
  }
  target.append(
    node("span", "", `${execution.role} · ${shortSha(execution.revision_sha)}`),
    node("strong", "", displayEnum(execution.status)),
    node("small", "", `${execution.collected_count} collected · ${formatDuration(execution.duration_seconds)} · artifact ${execution.artifact_unchanged ? "unchanged" : "mismatch"}`),
  );
}

function renderAudit(run) {
  const list = $("audit-list");
  list.replaceChildren();
  const rows = [
    ["Run UUID", run.run_id],
    ["BASE SHA", run.base_sha],
    ["HEAD SHA", run.head_sha],
    ["Evidence SHA-256", run.evidence_sha256 || "not stored"],
    ["Artifact SHA-256", run.selected_artifact_sha256 || "not selected"],
    ["Publication", `${run.publication_attempts} attempt(s), ${run.publication_retries} retries`],
  ];
  rows.forEach(([term, value]) => {
    const row = node("div");
    row.append(node("dt", "", term), node("dd", "", value));
    list.append(row);
  });
  if (run.check_url) {
    const row = node("div");
    const value = node("dd");
    const link = node("a", "", `GitHub Check ${run.check_run_id}`);
    link.href = run.check_url;
    link.target = "_blank";
    link.rel = "noreferrer";
    value.append(link);
    row.append(node("dt", "", "External result"), value);
    list.append(row);
  }
}

function renderRun(run) {
  $("run-kicker").textContent = `${run.repository} · immutable verification run`;
  $("run-title").textContent = run.title;
  const identities = $("run-identities");
  identities.replaceChildren();
  appendIdentity(identities, "PR", `#${run.pr_number}`, run.pr_url);
  appendIdentity(identities, "run", compactId(run.run_id));
  appendIdentity(identities, "revision", run.revision_state);
  appendIdentity(identities, "phase", run.phase);
  const outcome = $("run-outcome");
  outcome.className = `outcome-badge ${outcomeTone(run)}`;
  outcome.textContent = runLabel(run);
  renderLifecycle(run);
  renderClaim(run);
  renderCandidates(run);
  renderExecution($("base-result"), run.base_execution);
  renderExecution($("head-result"), run.head_execution);
  $("mechanical-result").replaceChildren(
    node("strong", "", displayEnum(run.mechanical_status)),
    document.createTextNode(` · ${displayEnum(run.differential_pattern)} — ${run.mechanical_reason || "No comparable execution pair."}`),
  );
  $("conclusion").textContent = run.conclusion || run.failure?.summary || "No conclusion is available yet.";
  const semantic = $("semantic-result");
  semantic.replaceChildren();
  if (run.semantic_assessment) {
    semantic.append(
      node("span", "accent", displayEnum(run.semantic_assessment.assertion_relation)),
      node("span", "", `semantic confidence ${(run.semantic_assessment.confidence * 100).toFixed(0)}%`),
      node("span", "", `${run.semantic_assessment.usage.total_tokens || "—"} assessment tokens`),
    );
  } else {
    semantic.append(node("span", "", "No semantic escalation · mechanical evidence was insufficient"));
  }
  renderAudit(run);
  $("run-panel").hidden = false;
}

function updateMetrics() {
  $("metric-runs").textContent = state.runs.length;
  $("metric-checks").textContent = state.runs.filter((run) => run.publication_state === "PUBLISHED").length;
  $("metric-hashes").textContent = state.runs.filter((run) => run.evidence_hash_verified).length;
}

function upsertRun(run) {
  const existing = state.runs.findIndex((item) => item.run_id === run.run_id);
  if (existing >= 0) state.runs[existing] = run;
  else state.runs.unshift(run);
  state.selected = state.runs.findIndex((item) => item.run_id === run.run_id);
  state.activeRunId = run.run_id;
  $("dashboard-status")?.remove();
  updateMetrics();
  renderTabs();
  selectRun(state.selected);
}

async function loadSnapshot(preferredRunId = null) {
  const response = await fetch("/dashboard/api/runs", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`evidence endpoint returned ${response.status}`);
  const snapshot = await response.json();
  state.runs = snapshot.runs || [];
  $("scope-notice").textContent = snapshot.scope_notice;
  updateMetrics();
  if (!state.runs.length) {
    const status = $("dashboard-status");
    status.className = "loading-card";
    status.replaceChildren(
      node("strong", "", "No durable runs yet"),
      node("p", "", "Submit an onboarded pull request above to create the first cloud run."),
    );
    return;
  }
  $("dashboard-status")?.remove();
  const requested = preferredRunId || state.activeRunId;
  const selected = requested ? state.runs.findIndex((run) => run.run_id === requested) : 0;
  state.selected = selected >= 0 ? selected : 0;
  renderTabs();
  selectRun(state.selected);
}

async function fetchRun(runId) {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`run status endpoint returned ${response.status}`);
  return response.json();
}

async function pollRun(runId) {
  while (true) {
    const run = await fetchRun(runId);
    upsertRun(run);
    $("analyze-status").textContent = `Run ${compactId(runId)} · ${displayEnum(run.status)}`;
    if (["COMPLETE", "ABSTAINED", "FAILED"].includes(run.status)) {
      await loadSnapshot(runId);
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 3000));
  }
}

async function submitAnalysis(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  $("analyze-status").textContent = "Submitting durable cloud run…";
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ pr_url: $("pr-url").value.trim() }),
    });
    const document = await response.json();
    if (!response.ok) throw new Error(document.detail || `analyze endpoint returned ${response.status}`);
    state.activeRunId = document.run_id;
    $("analyze-status").textContent = `Run ${compactId(document.run_id)} · ${displayEnum(document.status)}`;
    await pollRun(document.run_id);
  } catch (error) {
    $("analyze-status").textContent = `Analysis could not start: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function boot() {
  $("analyze-form").addEventListener("submit", submitAnalysis);
  const requestedRun = new URLSearchParams(window.location.search).get("run");
  try {
    await loadSnapshot(requestedRun);
    if (requestedRun && !["COMPLETE", "ABSTAINED", "FAILED"].includes(
      state.runs.find((run) => run.run_id === requestedRun)?.status,
    )) {
      pollRun(requestedRun).catch((error) => {
        $("analyze-status").textContent = `Run polling stopped: ${error.message}`;
      });
    }
  } catch (error) {
    const status = $("dashboard-status");
    status.className = "loading-card error-card";
    status.replaceChildren(
      node("strong", "", "Evidence is unavailable"),
      node("p", "", `The dashboard could not load its bounded run projection: ${error.message}`),
    );
  }
}

boot();
