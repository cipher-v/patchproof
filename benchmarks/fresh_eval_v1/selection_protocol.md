# Fresh evaluation v1 selection protocol

Frozen before case selection at `2026-09-01T13:54:43Z`.

Implementation under evaluation: `851b8342b3aac6a0c1664c519b5c1827a1fe6079`.

No Gemini or other language model may be used to discover, select, label, author,
validate, or execute this benchmark. The development corpus and its oracles are not
inputs to selection. The following repositories and cases are prohibited because they
were used during development: jsonschema #1208, dateutil #751, more-itertools #1128,
packaging #1345, Starlette #3317, Rich #3938, Jinja #2029, platformdirs #523, AnyIO
#1200, and cattrs #696.

## Population and freeze boundary

Candidates are real, publicly reviewable, merged GitHub pull requests in Python
repositories. Their BASE and HEAD are immutable full Git commit identifiers. Discovery
may use GitHub search, release notes, and repository history, but final inclusion is
decided by direct diff inspection and deterministic local execution. The freeze date is
the timestamp above: later edits to PR prose do not alter the committed manifest copy.

The target is 8 positive and 8 negative-control cases. A minimum of 6 in each class is
permitted only when the rejected-candidate ledger documents why additional compatible,
unambiguous cases could not be obtained without weakening this protocol.

## Positive inclusion rules

A positive must:

1. change Python production behavior rather than only tests, prose, formatting, typing,
   generated output, or packaging metadata;
2. expose a falsifiable behavioral difference through a callable/importable interface
   that exists on both BASE and HEAD;
3. admit an independently written, benchmark-only pytest oracle that fails on BASE by
   assertion and passes on HEAD;
4. execute offline on Python 3.12 using a deterministic install strategy already
   accepted by PatchProof;
5. have every upstream Python test changed by the PR excluded from all model-visible
   discovery, indexing, retrieval, grounding, and prompt paths;
6. be reasonably attributable to the PR rather than an unrelated dependency or timing
   effect.

The oracle must test the observable contract, not copy an upstream test verbatim. It
may not monkeypatch away the changed behavior or inspect the revision identifier.

## Negative-control inclusion rules

A negative must be a real merged PR whose public diff provides no sound basis for an
executable claim that distinguishes BASE from HEAD through shared production behavior.
Eligible categories include documentation/comments, typing-only edits erased at
runtime, formatting/lint cleanup, tests-only changes, behavior-preserving refactors or
renames, and metadata-only changes. At least four controls must change a Python file.

Controls are admitted only after direct diff inspection finds no hidden runtime behavior
change. A type-related control is rejected if annotations are observably changed through
runtime introspection and that is a stated supported behavior. A refactor/rename is
rejected when it changes importable public behavior. Negative labels are evaluation-only
metadata and are never passed to claim investigation or candidate generation.

## Exclusion rules

Reject candidates with mutable or missing revisions; network, clock, locale, service,
database, or nondeterministic runtime requirements; unsupported native/toolchain setup;
ambiguous mixed behavior and cleanup changes; no stable shared interface; a required
oracle that merely duplicates changed upstream tests; incompatibility with Python 3.12;
or an install plan that differs between BASE and HEAD. Reject large migration PRs when
the intended behavioral unit cannot be isolated without case-specific harness logic.

## Diversity constraints

The final set should maximize repository diversity and must span multiple libraries and
behavioral categories. No repository may contribute more than two cases, and a positive
and negative from the same repository must concern unrelated changes. Positives should
cover at least four distinct behavior families (for example parsing/serialization,
state or collection behavior, validation/error behavior, and path/text/protocol
handling). Negatives should cover at least four control categories, with at least four
controls changing Python files.

## Compatibility and ambiguity policy

Compatibility is established by symmetric dependency-plan resolution, successful BASE
and HEAD installation, and the same injected-test readiness path used for candidates.
All execution is local and offline after installation. An environment failure, process
error, collection error, or asymmetric plan is not evidence and causes rejection or an
explicit readiness failure.

When reasonable reviewers could disagree whether a diff changes supported runtime
behavior, reject the candidate rather than resolving ambiguity in favor of either label.
PR title/body are descriptive metadata, not ground truth. Labels come from code review
and (for positives) the independent cross-revision oracle.

## Discovery and freezing procedure

1. Record every seriously screened candidate in `candidate_ledger.json`, including
   rejection reason where applicable.
2. Resolve PR metadata, changed files, BASE, and HEAD directly from GitHub; store full
   immutable SHAs and a copy of the relevant PR prose.
3. Enumerate all changed Python test paths mechanically and copy the exact set into
   `excluded_paths`.
4. For positives, author one independent hidden oracle and prove
   `BASE=ASSERTION_FAILED` and `HEAD=PASSED` through the normal injected-test path.
5. For all cases, run zero-model readiness, verify symmetric installs and Phase-2
   context construction, and ensure a model tripwire cannot be invoked.
6. Freeze the manifest, ledger, protocol, and oracle SHA-256 values. Any later content
   change requires recomputing integrity metadata and is reported as a new construction
   revision, never silently substituted.

Selection does not change prompts, evidence admissibility, candidate budgets,
investigation budgets, grounding, validation, or production behavior. The sealed
evaluation itself is not run during construction.
