# PatchProof hard-mode result

Declared run: `hard-mode-gemini-3.6-flash-2026-08-26`
Model: `gemini-3.6-flash`

## Outcomes

- Cases: 5 (4 historical, 1 synthetic)
- Claims selected: 4/5 (80.0%)
- Discriminating generated tests: 2/5 (40.0%)
- Claim-supported scenarios: 2/5 (40.0%)
- Candidate attempts: 7 (3 repairs)
- Validated candidates: 3
- Invalid candidate structured outputs: 4
- Environmental candidate evaluations: 1
- Incorrect supports versus oracle direction: 0

## Terminal statuses

- `CLAIM_INVOCATION_ERROR`: 1
- `CLAIM_SUPPORTED_FOR_SCENARIO`: 2
- `INVALID_TEST`: 2

## Model accounting

- Completed logical model results: 13
- Provider attempts represented by completed results: 13
- Failed logical model calls: 1
- Failed provider attempts: unavailable from the adapter after terminal invocation failure
- Prompt tokens where reported: 34114
- Output tokens where reported: 3728
- Total tokens where reported: 38162
- Total model duration: 94.082 seconds

## Scope

This is a five-case adversarial diagnostic with fixed denominators, not a representative production accuracy estimate or proof of pull-request correctness.
