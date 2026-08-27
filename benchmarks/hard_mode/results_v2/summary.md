# PatchProof hard-mode result

Declared run: `hard-mode-v2-gemini-3.6-flash-2026-08-26`
Model: `gemini-3.6-flash`

## Outcomes

- Cases: 5 (4 historical, 1 synthetic)
- Claims selected: 4/5 (80.0%)
- Discriminating generated tests: 2/5 (40.0%)
- Claim-supported scenarios: 2/5 (40.0%)
- Candidate attempts: 6 (2 repairs)
- Validated candidates: 6
- Invalid candidate structured outputs: 0
- Environmental candidate evaluations: 4
- Incorrect supports versus oracle direction: 0

## Terminal statuses

- `CLAIM_INVALID_OUTPUT`: 1
- `CLAIM_SUPPORTED_FOR_SCENARIO`: 2
- `ENVIRONMENTAL`: 2

## Model accounting

- Completed logical model results: 12
- Provider attempts represented by completed results: 12
- Failed logical model calls: 0
- Failed provider attempts: unavailable from the adapter after terminal invocation failure
- Prompt tokens where reported: 35276
- Output tokens where reported: 3009
- Total tokens where reported: 38285
- Total model duration: 61.738 seconds

## Scope

This is a five-case adversarial diagnostic with fixed denominators, not a representative production accuracy estimate or proof of pull-request correctness.
