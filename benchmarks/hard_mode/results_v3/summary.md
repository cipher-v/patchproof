# PatchProof hard-mode result

Declared run: `hard-mode-v3-gemini-3.6-flash-2026-08-27`
Model: `gemini-3.6-flash`

## Outcomes

- Cases: 5 (4 historical, 1 synthetic)
- Claims selected: 3/5 (60.0%)
- Discriminating generated tests: 2/5 (40.0%)
- Claim-supported scenarios: 2/5 (40.0%)
- Candidate attempts: 4 (1 repairs)
- Validated candidates: 4
- Invalid candidate structured outputs: 0
- Environmental candidate evaluations: 2
- Incorrect supports versus oracle direction: 0

## Terminal statuses

- `CLAIM_INVOCATION_ERROR`: 2
- `CLAIM_SUPPORTED_FOR_SCENARIO`: 2
- `ENVIRONMENTAL`: 1

## Model accounting

- Completed logical model results: 9
- Provider attempts represented by completed results: 9
- Failed logical model calls: 2
- Failed provider attempts: unavailable from the adapter after terminal invocation failure
- Prompt tokens where reported: 26924
- Output tokens where reported: 1987
- Total tokens where reported: 28911
- Total model duration: 58.391 seconds

## Scope

This is a five-case adversarial diagnostic with fixed denominators, not a representative production accuracy estimate or proof of pull-request correctness.
