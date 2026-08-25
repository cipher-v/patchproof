# Phase 7 — Reproducible Evaluation and PatchProof Bench

**Status: COMPLETE WITH LIMITATIONS.** PatchProof Bench now reproduces four genuine historical
bug fixes from two public pure-Python repositories, compares HEAD-only and BASE/HEAD evidence
policies over identical artifacts, executes controlled failure-path checks, and stores raw and
summarized results. A separate bounded live Gemini smoke completed one historical claim-to-evidence
workflow, but it is not a blind or aggregate candidate-generation benchmark.

## Problem solved

Passing hand-written fixture tests proves that mechanics work on examples designed for the code.
It does not show that immutable checkout, test injection, pytest parsing, and evidence
classification survive real repository histories. Phase 7 creates a versioned benchmark whose
provenance and artifact hashes are checked before network or execution work begins.

The central evaluation question is false support: when the fix is absent, does a strategy still
emit strong support? This phase reports both the numerator and denominator instead of publishing a
single flattering percentage.

## Historical dataset

The manifest contains four merged bug-fix PRs across two lightweight public repositories. Each PR
changed production code and added a developer regression test. BASE and PR HEAD are full immutable
40-character SHAs.

| Case | Historical PR | BASE | HEAD | Upstream oracle |
|---|---|---|---|---|
| Negative `chunked()` size | [more-itertools #1223](https://github.com/more-itertools/more-itertools/pull/1223) | `516f0a80fb7c2c8562dbb5e318fc2a3d44f4171f` | `0e6acdf9b60765ecf9634d6f5c132ac1bebc616b` | `tests/test_more.py` |
| Stable running extrema | [more-itertools #1211](https://github.com/more-itertools/more-itertools/pull/1211) | `cb75bb9c55f7ed3e77ce599097e1ba8da411746d` | `c92c4fa26ec2c258a82f21116b3dacd4b787f193` | `tests/test_more.py` |
| `naturalsize()` unit rollover | [humanize #329](https://github.com/python-humanize/humanize/pull/329) | `976484a655df046aa6849f440a4f0cd44fc4918c` | `4a7537012fe28aa70270000d1bdcfd08c820e188` | `tests/test_filesize.py` |
| Negative mixed fraction | [humanize #320](https://github.com/python-humanize/humanize/pull/320) | `0a06a3d4a12113cd5f3d0df0cfbb3e27d92499eb` | `331b4c68bc0bc05c33269c90d286737a2ca437e8` | `tests/test_number.py` |

Small standalone oracles preserve the developer assertions without importing entire upstream test
modules. Their files and SHA-256 values are part of `benchmarks/manifest.json`. They live outside
the checked-out repository and are injected only for execution, so a future Gemini benchmark can
withhold them from retrieval and generation context.

`humanize` normally generates `humanize._version` during installation. The standalone oracle adds
a test-only in-memory version-module shim so source checkout import works without modifying
production files or adding a dependency-install variable. The initial measurement exposed this as
a collection error; the complete manifest was rerun after the reproducibility fix.

## Methodology

For every case the harness executes two scenarios and retains both:

1. **Historical developer oracle:** inject the same hashed oracle into historical BASE and PR
   HEAD. Independent PR history says the fix is present only on HEAD. Expected mechanics are BASE
   assertion failure and HEAD pass.
2. **Controlled no-op with a weak candidate:** inject a valid but behaviorally weak availability
   assertion into the same buggy BASE revision in both roles. The fix is absent. Both executions
   should pass, making the artifact non-discriminating.

The same raw results feed two deterministic policy views:

- `HEAD_ONLY` emits support whenever the candidate passes on HEAD;
- `PATCHPROOF_BASE_HEAD` emits support only for mechanically discriminating BASE assertion failure
  and HEAD pass.

This comparison isolates the value of counterfactual replay. It deliberately does not claim to
measure Gemini's ability to generate the reference oracle.

The run also selects nine explicit reliability/security pytest node IDs. Parameterization expands
them into 18 executed cases covering duplicate delivery, stale and superseded revisions, process
timeout, invalid candidate paths/source/imports/calls, malformed model output, prompt-injection-
shaped narrative, exhausted transient retry, and GitHub publication identity failure.

## Measured results

The checked-in raw run produced:

- 4/4 historical developer oracles reproduced as BASE assertion failure / HEAD pass;
- 4/4 weak candidates rejected as `NON_DISCRIMINATING` / `BOTH_PASSED`;
- 18/18 controlled failure/recovery cases passed across nine selected node IDs;
- mean two-revision challenge latency of 1.982 seconds on the recorded Windows/Python 3.12 run.

| Strategy | Scenarios | Strong supports | True supports | False supports | False/support | False/negative |
|---|---:|---:|---:|---:|---:|---:|
| HEAD only | 8 | 8 | 4 | 4 | 50.0% | 100.0% |
| PatchProof BASE/HEAD policy | 8 | 4 | 4 | 0 | 0.0% | 0.0% |

`false/support` is false strong supports divided by all strong supports. `false/negative` is false
strong supports divided by all controlled negative scenarios. Both denominators are stored so a
zero cannot hide the absence of negative cases.

These numbers demonstrate policy mechanics on reference and controlled artifacts. They are not a
representative production false-support estimate. The four weak negatives intentionally expose
the known flaw in HEAD-only acceptance, and the historical positives use ideal developer oracles.

## Machine-readable and human-readable evidence

- `benchmarks/manifest.json`: versioned PR provenance, full SHAs, artifact hashes, and controlled
  check selection;
- `benchmarks/results/raw.json`: all eight execution scenarios, bounded BASE/HEAD outputs,
  statuses, hashes, timings, per-strategy decisions, and the controlled-suite result;
- `benchmarks/results/summary.json`: recomputed aggregate counts and rates;
- `benchmarks/results/summary.md`: concise human-readable output;
- `benchmarks/results/live-gemini-smoke.json`: sanitized one-case live workflow evidence, model
  usage, immutable artifact hashes, oracle comparison, and explicit limitations.

Summary files can always be regenerated from raw JSON. Results are written only after a complete
manifest run succeeds; the harness does not skip or delete an inconvenient case.

## Important implementation pieces

`HistoricalBenchmarkCase` and `BenchmarkManifest` enforce GitHub URL/PR identity, full SHAs,
cross-repository coverage, normalized paths, unique cases, bounded inputs, artifact hashes, and
explicit controlled nodes. `GitRepositoryCache` clones each public repository once, verifies its
origin, fetches the exact `refs/pull/<n>/head`, compares that commit to the manifest HEAD, and
proves both commits exist.

`run_benchmark()` executes every case using the existing `GitWorkspaceManager`, `PytestRunner`,
`BaseHeadChallenge`, bounded process environment, artifact identity, and mechanical classifier.
`summarize()` derives metrics only from raw rows. A zero denominator is serialized as `null`, never
silently converted to zero.

## Commands

```shell
# Offline provenance and artifact verification
uv run python -m patchproof.benchmark verify

# Networked public-repository replay
uv run python -m patchproof.benchmark run

# Recompute summaries without rerunning Git or pytest
uv run python -m patchproof.benchmark summarize
```

Local repository caches and workspaces live under `.patchproof-bench/` and are ignored by Git.
The raw and summarized evidence under `benchmarks/results/` is intentionally versioned.

## Tests and what they prove

`tests/test_benchmark.py` proves that the checked-in manifest has four unique PRs across the two
declared repositories; all artifact hashes and single-test shapes validate; tampering fails before
network access; false-support denominators are calculated correctly; raw reports round-trip; and
human output retains the scope disclaimer.

The actual benchmark run—not a mock—proved all four public SHAs can be fetched, detached
workspaces can execute the injected artifacts, identical hashes survive both revisions, all four
developer regressions discriminate in the expected direction, and all four weak no-op scenarios
are rejected by the PatchProof policy.

Final repository verification on Windows/Python 3.12:

```text
uv run pytest -q
208 passed, 1 skipped, 2 warnings in 116.54s

uv run ruff check src tests benchmarks
All checks passed!

uv run python -m patchproof.benchmark verify
verified 4 cases; manifest sha256=400e0dad159779997ba24853499d21d88da539e0d4a9bb3cec7881b49fd3b39c
```

The skip is the credential-gated billable Gemini smoke test. The two warnings are upstream
ADK/Starlette deprecations. The separately executed networked benchmark produced the measurements
reported above.

## Unmeasured work and limitations

One credentialed smoke on `more-itertools` PR 1223 selected a grounded claim, generated a candidate,
used the single permitted repair after an environmental BASE result, and then reproduced BASE
assertion failure / HEAD pass. Semantic assessment returned `CLAIM_SUPPORTED_FOR_SCENARIO`, and the
mechanical result matched the stored developer oracle. The four provider attempts used 9,862 prompt,
734 output, and 12,225 total tokens. The sanitized record is
`benchmarks/results/live-gemini-smoke.json`.

The following remain explicitly unmeasured:

- blind candidate generation, because production retrieval included the changed test diff and
  snippets;
- aggregate candidate validity and semantic accuracy beyond this one historical case;
- an existing-upstream-test-suite baseline;
- incomplete-fix, unrelated-refactor, and introduced-regression historical cases;
- repeated runs across operating systems and dependency environments.

For that reason Phase 7 is complete with limitations. The benchmark is a real reproducible
historical executor/policy benchmark and a foundation for agent evaluation, while the live smoke is
one successful non-blind workflow sample. Neither is a claim that Gemini generates useful tests on
4/4 PRs. A later blind evaluation must store every sanitized attempt, keep developer oracles and
changed tests out of model context, and report failures and abstentions without selection.

## Interview questions

1. Why is a developer regression test useful as an independent oracle?
2. How does the harness keep the oracle out of future model context?
3. Why does a HEAD-only policy show false support on the controlled no-op cases?
4. What are the two false-support denominators, and why report both?
5. Why are these results not an agent-generation benchmark?
6. What did the generated-version collection failure teach about historical reproducibility?
