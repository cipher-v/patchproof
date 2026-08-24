# Phase 1 — Deterministic BASE/HEAD Executor and Evidence Model

## Status

**COMPLETE.** This phase implements and tests the first real PatchProof vertical slice. It makes no
semantic claim-support decision and does not call an LLM.

## Problem this phase solves

A generated regression test is weak evidence if PatchProof runs it only on the proposed revision.
It may already pass on the old code, fail for an unrelated reason, select no test, be skipped, or
change between executions. Phase 1 creates the deterministic counterfactual boundary needed to
answer a narrower question:

> Did one exact test artifact and one exact pytest node produce comparable, different behavior on
> two immutable Git commits?

The answer is a mechanical evidence status. It is not a verdict on the claim or pull request.

## Implemented components

### Domain models

`src/patchproof/models.py` defines frozen, slotted dataclasses and string enums:

- `Revision` pairs a `BASE` or `HEAD` role with a resolved full 40- or 64-character Git SHA.
- `TestArtifact` stores a normalized relative Python path, a single pytest node ID, and immutable
  bytes. Its SHA-256 hash is derived from those bytes.
- `ExecutionResult` records revision identity, expected and observed artifact hashes, test node,
  normalized execution status, collected count, exit code, duration, stdout, stderr, and detail.
- `EvidenceAssessment` separates the mechanical status and outcome pattern from the optional
  `ClaimOutcome`. Phase 1 always leaves `claim_outcome` unset.
- `ChallengeResult` joins the artifact, BASE result, HEAD result, and assessment.

The primary normalized execution statuses are:

```text
PASSED
ASSERTION_FAILED
TEST_ERROR
COLLECTION_ERROR
NOT_COLLECTED
MULTIPLE_TESTS_COLLECTED
SKIPPED
XFAILED
XPASSED
INVALID_ARTIFACT
TIMED_OUT
PROCESS_ERROR
```

Separating assertion failure from other exceptions is essential. A `RuntimeError`, fixture error,
or import failure must not be treated as evidence that a behavioral oracle failed.

### Immutable revision workspaces

`GitWorkspaceManager` resolves each supplied ref once with:

```text
git rev-parse --verify <ref>^{commit}
```

Only the resulting full SHA enters the `Revision` model. The manager creates detached temporary
worktrees for those exact commits and independently verifies each worktree's `HEAD`. Worktrees are
removed and pruned when the context exits, including exceptional paths. A cleanup failure is
reported without masking the original execution exception.

Detached worktrees were chosen because they share Git objects with the source repository while
providing two independent checked-out filesystem trees. PatchProof never checks out BASE or HEAD
in the source working tree.

### Exact artifact injection and hashing

`TestArtifact` rejects absolute, traversing, backslash-based, non-normalized, or non-Python paths.
Its pytest node ID must begin with its own artifact path. The runner resolves the injection target
and verifies that it remains inside the revision workspace, including existing symlink resolution.
It refuses to overwrite a repository file.

The same immutable `bytes` value is written separately into BASE and HEAD. Each execution records:

```text
expected SHA-256
SHA-256 immediately before pytest
SHA-256 immediately after pytest
pytest node ID
```

The classifier requires every observed hash to equal the expected hash. A test that edits or
deletes itself is `INVALID_TEST`, even if it passes on both revisions.

### Bounded pytest execution

`PytestRunner` first decodes the artifact as UTF-8 and parses it with Python's AST. Invalid syntax
is rejected before a subprocess starts, while the bytes are still injected and hashed on both
revisions for auditability.

For syntactically valid artifacts, the runner constructs a fixed argument vector equivalent to:

```text
<configured-python> -m pytest
  -p no:cacheprovider
  --color=no
  --tb=short
  --maxfail=1
  -q -rxX
  -o junit_family=xunit2
  --junitxml=<executor-owned-temporary-path>
  <artifact-node-id>
```

No shell is used and no arbitrary command string is accepted. `PYTEST_ADDOPTS` is removed so the
ambient environment cannot silently change the command. Third-party plugin autoload and Python
bytecode writes are disabled. The process has a hard per-revision timeout. Exit code, duration,
stdout, and stderr are captured even when the result is later rejected.

This fixed Phase 1 command is not the final `.patchproof.yaml` execution contract. Repository
installation and approved command configuration arrive in a later phase.

### JUnit normalization

Terminal output is useful to a human but unstable as a parser interface. `PytestJUnitParser` reads
pytest's xUnit2 report and normalizes exactly one selected test case. It distinguishes assertion
failures, runtime/setup errors, collection failures, skip, xfail, xpass, no collection, and a node
that unexpectedly selects multiple tests. Terminal text is used only as a conservative fallback
when pytest cannot produce a report and to identify non-strict XPASS.

### Mechanical classification

`MechanicalEvidenceClassifier` first validates artifact and node identity, then rejects invalid or
non-comparable executions before considering the BASE/HEAD pattern.

| BASE | HEAD | Mechanical status | Pattern |
| --- | --- | --- | --- |
| assertion failed | passed | `DISCRIMINATING` | `BASE_ASSERTION_FAILED_HEAD_PASSED` |
| passed | assertion failed | `DISCRIMINATING` | `BASE_PASSED_HEAD_ASSERTION_FAILED` |
| passed | passed | `NON_DISCRIMINATING` | `BOTH_PASSED` |
| assertion failed | assertion failed | `NON_DISCRIMINATING` | `BOTH_ASSERTION_FAILED` |
| invalid/not selected/skip/xfail/xpass | any | `INVALID_TEST` | `NOT_COMPARABLE` |
| collection/runtime/process error or timeout | any | `ENVIRONMENTAL` | `NOT_COMPARABLE` |
| hash or node mismatch | any | `INVALID_TEST` | `NOT_COMPARABLE` |

`COUNTERFACTUAL_NOT_APPLICABLE` exists in the evidence model but is not inferred in Phase 1. That
status needs interface and environment comparability analysis that this deterministic slice does
not yet possess.

## End-to-end control and data flow

```text
base ref + head ref + TestArtifact
              |
              v
resolve both refs to full commit SHAs
              |
              v
create and verify detached BASE/HEAD worktrees
              |
       +------+------+
       |             |
       v             v
inject bytes      inject same bytes
hash before       hash before
run one node      run same node
parse JUnit       parse JUnit
hash after        hash after
       |             |
       +------+------+
              v
validate identity and comparability
              |
              v
mechanical EvidenceAssessment
              |
              v
remove worktrees and retain structured result
```

`BaseHeadChallenge` is intentionally a small orchestrator. Git mechanics, execution/parsing, and
classification remain separate classes so failure paths can be tested without a god object.

## Failure handling

- Invalid refs or Git command failures raise `GitWorkspaceError`; mutable refs are resolved before
  worktree creation.
- Unsafe paths, existing target files, invalid UTF-8, and invalid syntax become
  `INVALID_ARTIFACT` results.
- Missing nodes, multiple selected nodes, skip, xfail, and xpass become invalid evidence.
- Collection/import failures, runtime/setup errors, process-start failures, and timeouts are
  environmental evidence rather than assertion failures.
- Candidate hash changes invalidate the pair.
- Both revisions are attempted so the record contains paired facts even if one side is weak.
- Temporary worktrees and machine-readable result files are cleaned after the challenge.

## Alternatives and trade-offs

### Git worktrees versus two clones

Two clones provide more filesystem independence but duplicate Git objects and add setup latency.
Detached worktrees are smaller and deterministic for the current allowlisted-repository scope.
They are not a security sandbox.

### JUnit XML versus parsing stdout

JUnit provides structured test cases and outcome elements across terse or verbose terminal modes.
It still cannot express every pytest nuance perfectly, so unsupported ambiguity is classified
conservatively rather than guessed.

### One pytest invocation versus collect then execute

A single node-scoped invocation avoids running import and fixture setup twice. Collection success
is derived from the resulting structured test case. A separate collection process could provide
more phase detail later, but it adds cost and can itself change observable behavior.

### Dataclasses versus Pydantic

Phase 1 objects are internal immutable Python values, not an external JSON boundary. Standard
dataclasses keep the runtime dependency set empty. Pydantic becomes more useful when webhook,
database, task, or model payloads cross process boundaries.

## Tests and what they prove

The suite creates a real disposable Git repository with two commits:

```python
# BASE
def add(left, right):
    return left - right


# HEAD
def add(left, right):
    return left + right
```

Tests cover:

- SHA normalization and rejection of mutable/malformed revision identifiers;
- artifact path, byte, node-ID, and hash invariants;
- detached worktree revision verification and cleanup;
- BASE assertion-fail / HEAD pass;
- BASE pass / HEAD pass self-rejection;
- BASE assertion-fail / HEAD assertion-fail self-rejection;
- BASE pass / HEAD assertion-fail potential-regression pattern;
- invalid Python rejected without launching pytest;
- import/collection failure;
- selected node not found;
- unrelated runtime exception;
- process failure before artifact hashing remains environmental;
- subprocess timeout;
- a passing candidate that mutates its own bytes;
- JUnit assertion, runtime error, collection error, skip, xfail, xpass, multiple-test, and
  no-test parsing;
- source repository cleanliness after execution.

The integration tests prove that real Git and pytest processes produce the expected structured
facts on Windows. They do not prove compatibility with arbitrary third-party repositories,
dependency installation, hostile-code containment, or semantic claim relevance.

## Commands and verified results

All final commands ran with Python 3.12.6 and uv's cache redirected to the ignored workspace cache
because the managed sandbox cannot access uv's normal user cache.

| Command | Result |
| --- | --- |
| `uv sync --frozen --all-groups` | Environment synchronized successfully. |
| `uv run pytest` | 52 tests passed. |
| `uv run ruff check .` | All checks passed. |
| `uv run ruff format --check .` | All discovered Python and Markdown files formatted. |
| `uv lock --check` | Lockfile current. |
| `uv build` | Source and wheel distributions built successfully. |
| `git diff --check` | No whitespace errors. |

The first integration attempt exposed two managed-Windows test-infrastructure constraints rather
than product assertions: pytest's default per-user temp directory was unreadable, and mode-700
directories created by `tempfile` were not traversable by child Git processes. Tests now use an
ignored workspace-local fixture root with normal inherited permissions, and production worktree
paths are explicitly created. Read-only Git object files are handled during fixture teardown. The
final suite is clean and leaves no registered temporary worktrees.

## Limitations

- Phase 1 uses a supplied Python executable and a fixed pytest command; it does not install each
  repository's dependencies or parse `.patchproof.yaml`.
- It does not establish dependency/environment equivalence beyond using the same runner settings.
- JUnit normalization targets the initial single-node pytest scope, not every plugin-specific
  outcome.
- Timeout terminates the direct pytest process; process-tree and resource enforcement are deferred
  to the reliability/security phase.
- Stdout and stderr are captured but not yet truncated.
- Worktrees provide revision isolation, not hardened untrusted-code isolation.
- No persistence, workflow state, GitHub API, Gemini/ADK, or publication exists.
- Mechanical discrimination intentionally produces no claim-level outcome.

## Interview questions to answer

1. Why is resolving a branch name once different from repeatedly executing against that name?
2. Why do detached worktrees avoid modifying the source checkout?
3. Which hashes are recorded, and why is a post-execution hash necessary?
4. Why is a runtime exception not equivalent to an assertion failure?
5. Why does BASE fail / HEAD pass remain mechanically discriminating rather than claim-supported?
6. Why are pass/pass and fail/fail both non-discriminating?
7. What makes skip, xfail, xpass, and no collection invalid evidence?
8. Why use JUnit XML while retaining stdout and stderr?
9. What does the subprocess timeout guarantee, and what does it not guarantee?
10. What must `.patchproof.yaml` and environment comparability add later?

## Next phase

Phase 2 will add a GitHub webhook control plane and durable workflow-state abstraction. It must not
start until explicitly approved.
