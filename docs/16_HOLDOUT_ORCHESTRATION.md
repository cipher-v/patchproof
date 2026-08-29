# Sealed holdout orchestration

## Purpose

The unseen holdout contains exactly ten merged historical pull requests. The development
hard-mode manifest intentionally requires a difficult `LOCAL_SYNTHETIC` case, so its existing
validator cannot load this historical-only dataset. That development rule and V1–V5 behavior
remain unchanged.

`benchmarks/holdout/orchestration.py` is a benchmark-only adapter for the sealed dataset. It adds
the narrower historical-only validation required by this holdout and maps all ten declarations
into the existing immutable `HardModeCase` and `HardModeProtocol` types. It does not modify
PatchProof prompts, context retrieval, candidate generation, repair, mechanical classification,
semantic assessment, retries, pacing, or usage accounting.

## Frozen machinery reuse

After holdout-specific validation, the adapter creates a `HardModeManifest` with Pydantic's
trusted `model_construct` path. This bypasses only the development manifest's synthetic-case
coverage rule; every protocol and case object has already passed the holdout validator and the
frozen case validators.

For a future explicitly authorized run, the adapter injects that validated manifest at the
existing loader seam and delegates to `patchproof.hard_mode.run_live`. Consequently, the actual
case execution remains the private `_run_live_case` function used by V5. Importing and using this
private benchmark helper is intentional at this frozen evaluation checkpoint: it avoids a second
implementation of semantic behavior.

Before that delegation, a deterministic context gate prepares the declared repositories, checks
the changed-test exclusions, and records context hashes. It uses the existing
`HardModeRepositoryCache` and `DeterministicContextRetriever`. Developer oracle source is never
provided to `_run_live_case` or any model-visible context.

## Holdout-specific validation

The adapter requires all of the following before it can reach live execution:

- dataset ID `unseen-historical-python-pr-holdout-v1`;
- exactly ten `HISTORICAL_PR` cases from ten distinct repositories;
- frozen implementation SHA `baf333afc160cd75a90cea1e0568120a9889fb7e`;
- `gemini-3.6-flash` on Vertex AI in `global` at temperature `0.1` and thinking level `LOW`;
- one claim call, one initial candidate plus at most one repair, and at most one semantic
  assessment after mechanical discrimination per case;
- one permitted transient provider retry per logical request;
- 40 maximum logical calls and 80 maximum provider calls;
- `BETWEEN_CASES` pacing with the sealed 60-second delay;
- normalized repositories, commit SHAs, exclusions, repository Python paths, and oracle paths;
- exact oracle hashes and a single declared top-level test in every oracle;
- `ASSERTION_FAILED` on BASE and `PASSED` on HEAD in the sealed construction gate;
- a future output destination resolving exactly to `benchmarks/holdout/results/` without a
  symbolic-link escape.

This does not relax or reinterpret `HardModeManifest`. The adapter imposes a separate, stricter
contract for this one sealed historical dataset.

## Default dry-run

The default invocation is read-only preflight:

```powershell
uv run python benchmarks/holdout/orchestration.py
```

It validates the manifest, case mapping, protocol arithmetic, execution contract, output path,
oracle hashes, construction gate, production-source provenance, and sealed-artifact provenance.
It does not create the results directory, prepare remote repositories, instantiate a provider or
model adapter, select a claim, generate or repair a candidate, or run semantic assessment.

The future output path is predeclared as `benchmarks/holdout/results/`, but this orchestration
checkpoint does not create or populate it.

## Explicit live safeguard

Live execution is reachable only when an operator adds `--live`. Omitting the flag always returns
the preflight report. Tests replace the live runner with a failing sentinel and prove that the
default path cannot enter it.

The live flag must not be used until the blind run is separately authorized. When it is eventually
authorized, the adapter will repeat preflight before any provider configuration is loaded, prepare
the no-oracle context gate, and then enter the frozen exactly-once runner. Existing journal and raw
result protections continue to prevent result replacement.

## Integrity checks

Every preflight runs two fail-closed Git comparisons:

1. `src/patchproof/` must have no difference from frozen implementation commit
   `baf333afc160cd75a90cea1e0568120a9889fb7e`.
2. `benchmarks/holdout/manifest.json`, `benchmarks/holdout/oracle_gate.json`, and
   `benchmarks/holdout/oracles/` must have no difference from sealed construction commit
   `113fc1af287b42447a7cde6b0a91241b7363c52c`.

The adapter and its tests are outside those protected construction paths, so benchmark-only
orchestration may be added without weakening the seal.

## Checkpoint statement

Creating and testing this adapter made zero Gemini or Vertex requests and invoked no claim,
candidate, repair, or assessment agent. No holdout evaluation, live journal, raw result, generated
candidate test, summary, or semantic assessment was produced.
