# PatchProof unseen historical-PR holdout protocol

Status: **predeclared before candidate discovery**

Protocol date: 2026-08-29
Protocol version: 1

## Purpose

Construct and seal an unseen holdout of exactly 10 real merged historical Python bug-fix pull
requests. This phase selects cases and verifies independent developer oracles only. PatchProof's
claim, candidate, repair, and semantic-assessment agents must not run during construction. No Gemini
or Vertex model inference is permitted.

The evaluated implementation was frozen before construction at
`baf333afc160cd75a90cea1e0568120a9889fb7e`. The final development diagnostic commit is
`3b724d33b42a533002e13cc670a43632b4dcd35d`. V1–V5 and their five development cases are retired
and cannot influence holdout selection or future tuning.

## Target and discovery pool

- Final size: exactly 10 cases.
- Every case must be a real, public, merged, historical PR in a Python repository.
- Investigate at least 15 serious candidates, preferably 15–20, before sealing.
- Use at least 7 repositories; prefer 10 when practical; no repository may contribute more than 2.
- Prefer repositories outside pallets/click, python-attrs/attrs, marshmallow-code/marshmallow, and
  pylint-dev/astroid.

## Eligibility rules

A candidate qualifies only when all of the following are true:

1. The PR is merged and its public PR URL, merge timestamp, exact BASE SHA, and exact pre-merge HEAD
   SHA are verifiable.
2. It is a genuine behavioral bug fix, not primarily documentation, formatting, typing, dependency,
   test-only, benchmark-only, or non-behavioral packaging work.
3. The relevant interface exists sufficiently on BASE and HEAD for the same fair differential test.
4. An independently written, deterministic pytest oracle can use identical bytes on both revisions
   and produce BASE `ASSERTION_FAILED` and HEAD `PASSED`.
5. The behavior runs locally on Python 3.12, or on an explicitly documented bounded compatible
   environment, without credentials, live network access, external services, GPUs, browsers,
   Docker-in-Docker, proprietary dependencies, or excessive resources.
6. Every changed upstream Python test path can be identified and excluded from all model-visible
   diff, snippet, repository-scan, and signature-retrieval context.
7. The behavioral description corresponds to the merged production fix and HEAD is the PR's actual
   bug-fix head, not an unrelated later revision.

Preferred cases require realistic objects, interacting conditions, state or call sequences, nested
behavior, validation/error semantics, or a meaningful edge case. A simple constant replacement is
insufficient.

## Diversity targets

Selection should cover varied categories when qualifying cases permit, including parsing or
serialization, state management, filesystem/path handling, argument validation, data
transformation, configuration, cache invalidation, deterministic async/control flow, error handling,
object construction, text processing, and CLI behavior. Do not force a category by weakening the
eligibility gate and do not select 10 near-identical bugs.

## Candidate ledger and permitted rejection reasons

Every seriously evaluated PR, including rejected candidates, must be recorded in
`candidate_ledger.json` with provenance, changed production/test paths, category, initial rationale,
status, and rejection reason.

Permitted rejection reasons are factual dataset-construction reasons such as failed deterministic
reproduction, incompatible or excessive environment, missing BASE interface, required external
service/network, unsafe changed-test leakage, non-behavioral change, or excess category duplication.
Perceived model difficulty or expected PatchProof success is never a selection or rejection reason.

## Deterministic selection priority

Qualifying candidates are selected in this order:

1. satisfies every eligibility rule;
2. improves repository diversity;
3. improves behavioral-category diversity;
4. has lower environmental complexity;
5. has stronger deterministic reproducibility;
6. stable ordering by repository, then PR number, then merged timestamp.

No candidate may be ranked by perceived LLM difficulty.

## Independent hidden oracles

Each selected case receives one concise developer-only pytest oracle under `oracles/`. The oracle is
written from the public behavioral contract, PR description, issue behavior, and production diff;
it must not copy the upstream regression test. The same exact oracle bytes run on BASE and HEAD,
without network access. The manifest records its SHA-256 and sole top-level test function.

Oracle development may be corrected while constructing the dataset because PatchProof has not run
on these cases. Material revisions are noted in the ledger. Once the manifest and gate are sealed,
oracles are immutable.

## Oracle and anti-leakage gate

The deterministic gate must verify, for all 10 cases:

- immutable BASE and HEAD commits exist and match the intended PR revisions;
- exact oracle bytes are injected unchanged into both workspaces;
- BASE is `ASSERTION_FAILED` and HEAD is `PASSED`;
- all changed upstream Python test paths exactly match `excluded_paths`;
- excluded paths do not occur in retrieved context;
- hidden oracle source is never model-visible;
- bounded callable-signature extraction cannot use excluded or test paths.

Any other BASE/HEAD outcome disqualifies the case. Production PatchProof logic cannot be changed to
accommodate a candidate.

## Frozen future evaluation protocol

No model calls occur during construction. A separately authorized future blind evaluation is frozen
as follows:

- production commit: `baf333afc160cd75a90cea1e0568120a9889fb7e`;
- model: `gemini-3.6-flash`;
- provider: `VERTEX_AI`;
- location: `global`;
- temperature: 0.1;
- thinking level: `LOW`;
- claim calls: 1 per case;
- candidate budget: 1 initial plus at most 1 repair;
- semantic assessment: once, only after mechanically discriminating evidence;
- transient provider retries: at most 1 per logical call;
- pacing: `BETWEEN_CASES`, 60 seconds;
- mechanical support: BASE `ASSERTION_FAILED`, HEAD `PASSED`;
- semantic support: `RELATED`;
- prompts, model, provider, retry policy, candidate budget, and classifier remain frozen.

For 10 cases the maximum is 40 logical calls:
`10 × (1 claim + 2 candidate + 1 assessment)`. With one transient retry, maximum provider attempts
are 80. This is a future operator-capacity declaration, not a quota lookup.

## Sealing rule

The construction commit contains dataset/protocol/oracle artifacts only. It contains no PatchProof
holdout results, generated candidate tests, semantic assessments, production changes, prompt changes,
deployment changes, or V6. After sealing, holdout outcomes cannot be used to modify the evaluated
system.
