# Architecture

## Current architecture (Phase 9 deployed)

The implemented slices connect through a durable run identity and an injectable, idempotent
dispatcher. The ingestion slice is:

```text
GitHub webhook bytes
        |
        v
FastAPI control plane
bounded body + HMAC verification
event/action/public/allowlist filtering
        |
        v
validated PR event
full BASE SHA + full HEAD SHA
        |
        v
VerificationRunStore + dispatcher(run_id)
        |
        v
SQLite transaction
delivery/revision idempotency
current/stale/superseded history
independent workflow dimensions
```

The Phase 1 deterministic execution slice remains:

```text
base ref + head ref + immutable TestArtifact
                    |
                    v
        GitWorkspaceManager
        resolve full commit SHAs
        detached BASE/HEAD worktrees
                    |
          +---------+---------+
          v                   v
     PytestRunner         PytestRunner
     inject/hash          inject/hash
     contract command     same command/node
     JUnit parse          JUnit parse
          +---------+---------+
                    v
       MechanicalEvidenceClassifier
                    |
                    v
             ChallengeResult
```

The local SQLite adapter remains available for development. The deployment composition replaces it
with transactional Firestore and implements the dispatcher with a deterministic named Cloud Task.

The Phase 3–5 workflow is local and independently callable by a worker:

```text
immutable BASE SHA + immutable HEAD SHA
PR title/body + optional linked-issue excerpts
                    |
                    v
       DeterministicContextRetriever
       bounded per-file Git diffs
       Python AST changed-symbol spans
       nearby committed source/imports
       ranked likely tests and references
                    |
                    v
        bounded PullRequestContext JSON
                    |
                    v
       one Google ADK LlmAgent invocation
       Gemini 3.6 Flash, no tools/history
       structured ClaimSelection output
                    |
                    v
       deterministic schema + grounding check
              /                     \
             v                       v
 one testable BehavioralClaim   typed abstention
                               INSUFFICIENT_EVIDENCE or
                               COUNTERFACTUAL_NOT_APPLICABLE
             |
             v
 bounded candidate-task invocation
 same logical ADK agent identity
 structured CandidateTestProposal
             |
             v
 CandidateTestValidator <----- validated .patchproof.yaml
 path/syntax/test shape         argv templates + allowed paths
 grounded imports/call checks  Python 3.12 + timeout
             |
       +-----+------+
       v            v
 immutable       rejected attempt
 TestArtifact    bounded feedback
 SHA-256             |
       |             v
       |       up to two repairs
       |       second/third model calls
       +-------------+
             |
             v
 CandidateGenerationSnapshot
 parent ID/hash + model usage + response hash
             |
             v
 repository environment readiness
 identical validated setup on BASE and HEAD
             |
             v
 credential-minimized bounded process runner
 identical candidate bytes on BASE/HEAD
             |
             v
 mechanical self-rejection -> next bounded repair
             |
             v
 constrained semantic relevance assessment
             |
             v
 immutable evidence JSON + SHA-256
             |
             v
 retry-safe GitHub Check publication
```

The retriever reads committed Git objects, not uncommitted working-tree content. The model never
searches the repository and does not receive the whole repository. Its output must reference an
exact changed symbol and cite ranges contained in selected deterministic snippets. The live model
test is opt-in and successfully returned a real structured Gemini response.

Candidate proposals contain source and audit rationale, never commands. The workflow reads the
same contract from both immutable trees, derives committed paths from HEAD, and requires the
executor to hold that contract. Before candidate generation, the executor establishes BASE/HEAD
readiness using only validated install arrays. Setup failure abstains and cannot trigger repair.
Validated install arrays and pytest commands execute without a shell. Every candidate execution
and semantic/model provenance record is content-addressed.

Installation and pytest child processes receive an explicit environment allowlist with isolated
home/cache/temp paths, never a copy of the control-plane environment. Output is drained into
bounded buffers, commands run without a shell, and timeout terminates the process tree. Logical
semantic calls retry at most once and only for explicit transient provider failures. A worker
exception becomes one immutable, sanitized terminal failure record rather than leaving a run
indefinitely active.

GitHub Check publication uses a short-lived GitHub App installation token, run ID `external_id`,
remote-ID recovery, immutable payload hashes, and separate retry state. Publication code can only
load stored evidence; it cannot access Gemini, repositories, workspaces, or pytest.

The Phase 7 evaluation slice reuses the execution core without entering the production workflow:

```text
versioned benchmark manifest + hashed hidden artifacts
                         |
                         v
public GitHub PR ref fetch + exact SHA verification
                         |
             +-----------+-----------+
             v                       v
historical developer oracle    controlled weak candidate
real BASE -> real PR HEAD       buggy BASE -> same BASE
             +-----------+-----------+
                         v
             shared raw execution facts
                 /                 \
                v                   v
        HEAD-only policy     PatchProof BASE/HEAD policy
                 \                 /
                  raw JSON -> derived JSON/Markdown
```

Reference oracles never enter the historical repositories and are injected after checkout. The
current benchmark measures executor and evidence-policy behavior, not live Gemini generation.

## Implemented cloud composition

```text
GitHub pull-request event
          |
          v
Cloud Run control -- webhook + Firestore + task enqueue
      |
      v
Cloud Tasks -- deterministic run ID + Google OIDC
      |
      v
control task route -- ADK/Gemini + orchestration
      |
      v
private Cloud Run executor -- checkout + validated pytest
      |
      v
bounded facts -> control -> Firestore -> GitHub Check
                              |
                              v
                 sanitized featured-run projection
                              |
                              v
                 read-only evidence dashboard
```

The control plane owns orchestration, model access, GitHub App credentials, durable state, task
dispatch, and publication. The private executor receives immutable identities, a validated
contract, and a hashed artifact, then independently refetches and validates them before returning
bounded facts. It receives no GitHub write credential, Gemini key, or Firestore permission. This
composition and its deployment script are implemented and live in project `patchproof-506606`.
PR #1 exercised a genuine signed GitHub delivery through the task, executor, evidence, and GitHub
Check path. The control service now also serves a same-origin static evidence console. Its API
accepts no run identifier: operators explicitly select at most eight unique durable UUIDs through
deployment configuration. The projection recomputes each evidence hash and checks run/repository/
PR/revision identity before returning only bounded public fields. Candidate source is public for
those featured runs by design; PR body, installation identity, raw model responses, stdout/stderr,
and credentials are excluded.

## Intended evidence flow

```text
PR diff -> changed symbols -> selected or abstained claim -> candidate test artifact
                                                   |          |
                                                   v          v
                                                 BASE        HEAD
                                                   \          /
                                                    mechanical
                                                  classification
                                                        |
                                                        v
                                            claim-scoped conclusion
```

Webhook authentication, PR identity capture, local dispatch boundary, context, claim selection or
abstention, bounded candidate/repair, installation, BASE/HEAD execution, mechanical classification,
semantic relevance assessment, evidence persistence, and GitHub Check publication are connected.
The dispatcher and Firestore adapter implement those cloud boundaries, the resources are live, and
the first deployed end-to-end capture is recorded in `deploy/results/phase8-deployment.json`.
The dashboard release and public-projection checks are recorded in
`deploy/results/phase9-dashboard.json`.

## Architectural invariants

- One bounded agent handles semantic decisions; deterministic code owns enforcement and evidence.
- Repository content and PR prose are untrusted model data, never instructions or tool authority.
- Model claims must use exactly supplied changed-symbol identities and snippet-contained citations.
- Each semantic task is an isolated, tool-free, schema-constrained invocation under one logical
  agent identity; claim selection may abstain and candidate generation has a two-call ceiling.
- Generated test commands never come from the model; only validated contract argument arrays are
  executable, without a command shell.
- Repository child processes receive only an explicit operational environment allowlist; model,
  webhook, and GitHub write credentials are not inherited.
- Subprocess time and retained stdout/stderr are bounded before execution facts enter evidence.
- A logical semantic call may retry once only for an explicit transient failure; malformed output
  and policy failures do not retry.
- Candidate source must pass deterministic path, syntax, test-shape, import, and selected-call
  checks before it becomes one immutable hashed `TestArtifact`.
- The same test bytes and identifier are replayed on immutable BASE and HEAD revisions.
- The LLM cannot override mechanical evidence or invent shell commands.
- Insufficient evidence is a valid terminal result.
- Lifecycle, workflow phase, terminal reason, evidence outcome, and publication state remain
  separate dimensions.
- Publication retries do not repeat model or executor work.
- Current worker failures terminate durably with stable codes and sanitized summaries.
- Every executed candidate remains auditable even when it self-rejects and triggers repair.
- GitHub tokens and private keys never enter workflow/evidence persistence.
- Old revisions remain auditable after a newer HEAD supersedes them.
- GitHub delivery replay and revision replay are distinct idempotency boundaries.
- At most one stored run occurrence is current for a repository and pull request.
- Public claims remain narrower than the technical mechanism.
- Benchmark summaries are derived from checked-in raw rows with explicit denominators; unmeasured
  agent metrics remain unmeasured rather than being inferred from reference-oracle performance.
- A Cloud Task carries only a durable run UUID, uses a deterministic name, and is accepted only
  with an audience- and email-pinned Google OIDC identity.
- The executor Cloud Run identity has no project roles or secret mounts; only the control identity
  can invoke it, and executor independently revalidates commits, contract, artifact, and allowlist.
- The public dashboard can project only operator-configured run UUIDs, never request-selected
  Firestore documents, and must verify stored evidence hashes and immutable identity before output.
- Public evidence rendering uses typed JSON, DOM text nodes, and a same-origin CSP; untrusted
  evidence is never interpreted as HTML or script.
