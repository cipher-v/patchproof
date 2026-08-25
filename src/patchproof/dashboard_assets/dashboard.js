const state = { runs: [], selected: 0 };

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
  return run.claim_outcome === "CLAIM_SUPPORTED_FOR_SCENARIO" ? "success" : "neutral";
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
    const badge = node("b", outcomeTone(run), run.claim_outcome === "CLAIM_SUPPORTED_FOR_SCENARIO" ? "SUPPORTED" : "ABSTAINED");
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
}

function renderLifecycle(run) {
  const labels = ["Webhook accepted", "Task queued", "Worker running", "Evidence stored", "Check published"];
  const rail = $("lifecycle-rail");
  rail.replaceChildren(...labels.map((label) => node("li", "", label)));
  if (run.publication_state !== "PUBLISHED") rail.lastElementChild.classList.add("incomplete");
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
  outcome.textContent = displayEnum(run.claim_outcome || run.terminal_reason);
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

async function boot() {
  try {
    const response = await fetch("/dashboard/api/runs", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`evidence endpoint returned ${response.status}`);
    const snapshot = await response.json();
    state.runs = snapshot.runs || [];
    $("scope-notice").textContent = snapshot.scope_notice;
    $("metric-runs").textContent = state.runs.length;
    $("metric-checks").textContent = state.runs.filter((run) => run.publication_state === "PUBLISHED").length;
    $("metric-hashes").textContent = state.runs.filter((run) => run.evidence_hash_verified).length;
    if (!state.runs.length) throw new Error("no featured run IDs are configured");
    $("dashboard-status").remove();
    renderTabs();
    renderRun(state.runs[0]);
  } catch (error) {
    const status = $("dashboard-status");
    status.className = "loading-card error-card";
    status.replaceChildren(
      node("strong", "", "Evidence is unavailable"),
      node("p", "", `The read-only dashboard could not load its configured run projection: ${error.message}`),
    );
  }
}

boot();
