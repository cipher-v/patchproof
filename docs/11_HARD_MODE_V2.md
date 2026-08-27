# PatchProof hard-mode V2 evaluation

## Status and scope

This document reports one sealed five-case diagnostic run. It is not an accuracy estimate and it
does not establish general pull-request correctness.

- Reliability checkpoint: `968d7e012b2cdcdba37f6b7ab2a063ac9e9ac89d`
- V1 benchmark commit: `b86b5b5675f10611453e8c414934e379f783eaac`
- V2 declared run: `hard-mode-v2-gemini-3.6-flash-2026-08-26`
- Model: `gemini-3.6-flash`
- V2 manifest SHA-256: `8c11541035c8a179eb7b66fe7fcaaac78a42aafddae03917b7a08fa315155a78`
- V2 oracle-gate SHA-256: `08c5cf2e0c851e795e685eecae2f7c8c70c28b8d41cf2972ce7f0c94517f8aed`
- V2 raw-result SHA-256: `a1e3cdfd031200de71926a53d66faf6ec29b84ae4f7dfb9b3eeb17d0c8f1871f`
- Pacing: `BETWEEN_CASES`, with a declared 60-second delay

The official Gemini [rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits)
states that active limits vary by project and tier and are shown in AI Studio. The repository had no
larger declared requirement, so the protocol used the preselected 60-second interval.

The V2 manifest contains the same five case objects, in the same order, as V1. All five V2 oracle
gates independently reproduced `BASE=ASSERTION_FAILED` and `HEAD=PASSED`. Artifact bytes were
unchanged on both revisions, all anti-leakage checks passed, excluded changed tests were absent from
model context, and no oracle line entered model context.

The four observed inter-case delays were 60.000171, 60.000149, 60.000438, and 60.000629 seconds.
There was no case retry, quota-failure restart, or manual candidate edit.

## V1 versus V2

| Case | V1 | V2 | What changed |
|------|----|----|--------------|
| Click #3678 | Claim selected; initial and repair were `INVALID_MODEL_OUTPUT`; no test executed. | Initial draft validated and executed; BASE assertion failed, HEAD passed; semantic relation `RELATED`; claim supported. | Removing model-generated control-plane fields coincided with a valid two-field draft and a real discriminating test. |
| attrs #1513 | Initial test discriminated and the claim was supported. | Claim response exceeded the configured budget; no candidate was requested. | This regressed before candidate generation, so no V2 source exists to compare with the successful V1 source. |
| Marshmallow #2903 | Claim call ended in a provider invocation error after a 429; no candidate was requested. | Claim selected; initial and repair drafts validated and executed; both produced BASE `TEST_ERROR` and HEAD `PASSED`, so evidence was environmental and insufficient. | No provider-terminal 429 occurred under the declared pacing. Candidate generation was reached, but unhandled expected `ValidationError` on BASE was not an assertion failure. |
| Astroid #3075 | Initial and repair were `INVALID_MODEL_OUTPUT`; no test executed. | Initial and repair drafts validated and executed; both errored on both revisions because `FunctionDef` construction omitted required keyword-only arguments. | Structured-output failure was eliminated, exposing a test-construction error instead. |
| Nested workspace | Initial source used an invalid dotted filename and failed collection; repair used a safe filename and discriminated. | PatchProof assigned a safe filename; the initial candidate discriminated and was supported without repair. | Deterministic filename assignment removed the collection failure. Gemini still used local mock workspace and registry objects rather than integrated production object implementations. |

### Raw counts

| Count | V1 | V2 |
|-------|---:|---:|
| Cases | 5 | 5 |
| Claim calls reached | 5 | 5 |
| Claims selected | 4 | 4 |
| Invalid claim outputs | 0 | 1 |
| Valid initial candidates | 2 | 4 |
| Invalid candidate structured outputs | 4 | 0 |
| Executable initial candidates under the summary's non-environmental definition | 1 | 2 |
| Initial artifacts that actually reached BASE/HEAD execution | 2 | 4 |
| Repairs used | 3 | 2 |
| Validated repairs | 1 | 2 |
| Discriminating repairs | 1 | 0 |
| Cases with a discriminating generated candidate | 2 | 2 |
| Claim-supported scenarios | 2 | 2 |
| Insufficient-evidence outcomes | 2 | 2 |
| Provider-terminal failures | 1 | 0 |
| Incorrect supports | 0 | 0 |

These counts are diagnostic denominators, not accuracy.

## Case evidence

### Click #3678

Claim selection succeeded with claim ID `claim-help-option-storage-name`, summary "Automatic help
option uses reserved storage name _click_default_help", and confidence 0.95.

The initial candidate validated. Its rationale was: "Verify that Command.get_help_option returns a
help option whose internal storage name (name) is set to '_click_default_help'."

```text
import click

def test_patchproof_generated_behavior():
    cmd = click.Command("test_cmd")
    ctx = click.Context(cmd)
    help_opt = cmd.get_help_option(ctx)
    assert help_opt is not None
    assert help_opt.name == "_click_default_help"
```

- Artifact SHA-256: `6f59c401b424ea2f84ba661e239d103b06927828248b2d9a1823db9822934cc0`
- BASE: `ASSERTION_FAILED`, exit 1, 1.015 seconds; observed name was `help`
- HEAD: `PASSED`, exit 0, 0.875 seconds
- Mechanical: `DISCRIMINATING`, `BASE_ASSERTION_FAILED_HEAD_PASSED`
- Repair: not used
- Semantic: `RELATED`, confidence 0.98
- Final outcome: `CLAIM_SUPPORTED_FOR_SCENARIO`
- Hidden-oracle direction matched: yes

Model accounting was claim 4,799/379/5,178 tokens in 8.412 seconds, candidate
4,994/121/5,115 in 2.577 seconds, and assessment 890/110/1,000 in 2.326 seconds. Each completed
logical result used one provider attempt. Token triples are prompt/output/total.

### attrs #1513

The claim call returned output exceeding the configured response budget. The terminal status was
`CLAIM_INVALID_OUTPUT`; raw response SHA-256 was
`c0c548a62bd9b3b6bbe02008cbbe05a20b5c63348ea7b1a103aed5616adcfa0d`. No claim, candidate,
execution, repair, or semantic assessment was accepted. The call reported 1,735 prompt tokens,
8,175 output tokens, 9,910 total tokens, 47.503 seconds, and one provider attempt.

V1 had selected `claim-disabled-validator-nesting-state-restoration` and generated a successful
nested-context test. Because V2 failed at the earlier claim boundary, schema simplification produced
no V2 candidate source; it is therefore impossible to say that the attrs test source materially
changed.

### Marshmallow #2903

Claim selection completed with ID `claim-schema-partial-attr-name-prefix`, summary "Nested partial
field matching uses attribute name prefix instead of data_key", and confidence 0.95.

The initial candidate validated with artifact SHA-256
`51ac4fbe52b745298b6d1a1d7a1b9c005d12a0a38239ab93aeaf541fd7417c69`.

```text
from marshmallow import Schema, fields

def test_patchproof_generated_behavior():
    class ChildSchema(Schema):
        city = fields.Str(required=True)
        zip_code = fields.Str(required=True)

    class ParentSchema(Schema):
        address = fields.Nested(ChildSchema, data_key="homeAddress")

    schema = ParentSchema()
    data = {"homeAddress": {"city": "New York"}}
    result = schema.load(data, partial=("address.zip_code",))
    assert result == {"address": {"city": "New York"}}
```

Its rationale was: "Verify that specifying a dot-delimited partial path using the attribute name
('address.zip_code') works when the nested field has a custom data_key ('homeAddress')."

- Initial BASE: `TEST_ERROR`, exit 1, 0.844 seconds; uncaught `ValidationError` for missing `zip_code`
- Initial HEAD: `PASSED`, exit 0, 0.766 seconds
- Initial mechanical: `ENVIRONMENTAL`, `NOT_COMPARABLE`

The exact bounded repair feedback was:

- Category: `EXECUTION_EVIDENCE`
- Summary: "The previous candidate did not produce discriminating BASE/HEAD evidence."
- Observations: `mechanical_status=ENVIRONMENTAL`, `pattern=NOT_COMPARABLE`,
  `BASE=TEST_ERROR; HEAD=PASSED`

The repair validated with artifact SHA-256
`371390115cb9fe91c9618697258baf53479131341b3bbbaab499d9e6475a7ae2`.

```text
from marshmallow import Schema, fields

def test_patchproof_generated_behavior():
    class ChildSchema(Schema):
        city = fields.Str(required=True)
        zip_code = fields.Str(required=True)

    class ParentSchema(Schema):
        address = fields.Nested(ChildSchema, data_key="homeAddress")

    schema = ParentSchema()
    data = {"homeAddress": {"zip_code": "10001"}}
    result = schema.load(data, partial=("address.city",))
    assert result == {"address": {"zip_code": "10001"}}
```

Its rationale was: "Verify that passing partial=('address.city',) matching nested field attribute
name allows partial deserialization when data_key is configured on the nested field."

- Repair BASE: `TEST_ERROR`, exit 1, 0.828 seconds; uncaught `ValidationError` for missing `city`
- Repair HEAD: `PASSED`, exit 0, 0.625 seconds
- Repair mechanical: `ENVIRONMENTAL`, `NOT_COMPARABLE`
- Semantic assessment: not run
- Final outcome: `INSUFFICIENT_EVIDENCE`

Model accounting was claim 3,651/466/4,117 tokens in 5.326 seconds, initial candidate
3,646/202/3,848 in 3.325 seconds, and repair 3,928/202/4,130 in 3.538 seconds. Each completed
logical result used one provider attempt. There was no V2 provider-terminal failure. This observation
does not prove pacing alone caused the absence of a 429.

### Astroid #3075

Claim selection completed with ID `claim-tokenerror-position-info`, summary
"TreeRebuilder._get_position_info returns None instead of raising TokenError on malformed source
slices", and confidence 0.95.

The initial candidate validated with artifact SHA-256
`33cd191a505fbcd1e253a234104478a52ad19c6700c14712dfd3d2ec62904bbd`.

```text
import ast
from unittest.mock import MagicMock
from astroid.rebuilder import TreeRebuilder
from astroid import nodes

def test_patchproof_generated_behavior():
    manager = MagicMock()
    # Malformed data slice that causes tokenize.TokenError (e.g., unterminated bracket)
    rebuilder = TreeRebuilder(manager, data="def foo(: [")

    ast_node = ast.parse("def foo(): pass").body[0]
    ast_node.lineno = 1
    ast_node.end_lineno = 1

    parent_node = nodes.FunctionDef("foo", lineno=1, col_offset=0, parent=None)

    result = rebuilder._get_position_info(ast_node, parent_node)
    assert result is None
```

Its rationale was: "Verify that `TreeRebuilder._get_position_info` catches `TokenError` caused by
malformed data slices and returns `None` instead of raising an exception."

- Initial BASE: `TEST_ERROR`, exit 1, 1.719 seconds
- Initial HEAD: `TEST_ERROR`, exit 1, 1.766 seconds
- Error on both: `FunctionDef.__init__()` omitted `end_lineno` and `end_col_offset`
- Initial mechanical: `ENVIRONMENTAL`, `NOT_COMPARABLE`

The exact bounded repair feedback was:

- Category: `EXECUTION_EVIDENCE`
- Summary: "The previous candidate did not produce discriminating BASE/HEAD evidence."
- Observations: `mechanical_status=ENVIRONMENTAL`, `pattern=NOT_COMPARABLE`,
  `BASE=TEST_ERROR; HEAD=TEST_ERROR`

The repair validated with artifact SHA-256
`09994fb08f682efa5ebfa0edf883cbfeb346e2c2750bb28862e9f3be0f6dc1a1`.

```text
import ast
from astroid.rebuilder import TreeRebuilder
from astroid import nodes

def test_patchproof_generated_behavior():
    rebuilder = TreeRebuilder(manager=None, data="def foo(: [")
    ast_node = ast.parse("def foo(): pass").body[0]
    ast_node.lineno = 1
    ast_node.end_lineno = 1
    parent_node = nodes.FunctionDef("foo", lineno=1, col_offset=0, parent=None)
    result = rebuilder._get_position_info(ast_node, parent_node)
    assert result is None
```

Its rationale was: "Verify that TreeRebuilder._get_position_info catches tokenize.TokenError when
processing a malformed code slice and returns None without raising an exception."

- Repair BASE: `TEST_ERROR`, exit 1, 2.000 seconds
- Repair HEAD: `TEST_ERROR`, exit 1, 1.781 seconds
- Repair mechanical: `ENVIRONMENTAL`, `NOT_COMPARABLE`
- Semantic assessment: not run
- Final outcome: `INSUFFICIENT_EVIDENCE`

Model accounting was claim 3,105/406/3,511 tokens in 4.681 seconds, initial candidate
3,259/259/3,518 in 3.334 seconds, and repair 3,599/209/3,808 in 3.509 seconds. Each completed
logical result used one provider attempt.

### Nested workspace

Claim selection completed with ID `claim-resolve-owner-deepest-workspace`, summary "resolve_owner
selects the workspace with the deepest root path among candidates", and confidence 0.95.

The initial candidate validated. Its rationale was: "Verify that resolve_owner returns the workspace
with the deepest root path parts (max depth) when multiple candidates exist."

```text
from dataclasses import dataclass
from workspace_registry import resolve_owner

@dataclass
class MockWorkspace:
    root: str

class MockRegistry:
    def __init__(self, candidates):
        self._candidates = candidates
    def candidates_for(self, candidate_path: str):
        return self._candidates

def test_patchproof_generated_behavior():
    w_shallow = MockWorkspace(root="/a")
    w_deep = MockWorkspace(root="/a/b/c")
    registry = MockRegistry([w_shallow, w_deep])
    result = resolve_owner(registry, "/a/b/c/file.py")
    assert result == w_deep
```

- Artifact SHA-256: `090780546528b61dc63cde7780f5a2578473ad1644435cb4d5e9797f6a132d91`
- Assigned path: `patchproof_generated_tests/test_patchproof_generated_initial.py`
- BASE: `ASSERTION_FAILED`, exit 1, 0.438 seconds
- HEAD: `PASSED`, exit 0, 0.375 seconds
- Mechanical: `DISCRIMINATING`, `BASE_ASSERTION_FAILED_HEAD_PASSED`
- Repair: not used
- Semantic: `RELATED`, confidence 0.98
- Final outcome: `CLAIM_SUPPORTED_FOR_SCENARIO`
- Hidden-oracle direction matched: yes

Model accounting was claim 1,059/353/1,412 tokens in 18.920 seconds, candidate
1,279/207/1,486 in 3.229 seconds, and assessment 1,067/95/1,162 in 2.562 seconds. Each completed
logical result used one provider attempt.

The filename defect is gone: the initial test collected and executed directly. The semantic style did
not become more integrated. V1 used `DummyWorkspace` and `DummyRegistry`; V2 used equivalent
`MockWorkspace` and `MockRegistry` objects and invoked only the production `resolve_owner`
function.

## Structured-output root-cause comparison

V1 retained only response hashes and the generic `MALFORMED_OUTPUT` issue for Click and Astroid:

| Case | Attempt | V1 response SHA-256 |
|------|---------|--------------------|
| Click | Initial | `a07927b790d14422f3cf0f3889f9368984a20752228f802c0156bdbd54e3a55a` |
| Click | Repair | `1417572d4f9abaa7ca3f69a5cc8cd94dfacf68bd9bd95495f3a2631acaa8e789` |
| Astroid | Initial | `e72088cb471cc346a0bbd63368c835b59a2dbf38a8e0aad2d17d0b3f732e091d` |
| Astroid | Repair | `ad82f514e7e58c6e0c2276fc90c56c234d02ebd52300afb09cbe2ea7b14110b3` |

Because V1 did not retain structural diagnostics or response bodies, it is not possible to determine
whether those four responses used invalid JSON, omitted fields, returned extra fields, or violated a
local metadata validator. Any more specific V1 diagnosis would be speculation.

V2 had no malformed candidate response, so no malformed-output diagnostic object was emitted. A
`VALIDATED` attempt proves that JSON parsing and the strict two-field `CandidateTestDraft` validation
succeeded: `source` and `rationale` were present, there were no unexpected fields, `source` was a
string, there were no Pydantic errors, and diagnostic stage was not applicable.

| Case | Attempt | Source characters | Source SHA-256 |
|------|---------|------------------:|---------------|
| Click | Initial | 242 | `6f59c401b424ea2f84ba661e239d103b06927828248b2d9a1823db9822934cc0` |
| Astroid | Initial | 623 | `33cd191a505fbcd1e253a234104478a52ad19c6700c14712dfd3d2ec62904bbd` |
| Astroid | Repair | 463 | `09994fb08f682efa5ebfa0edf883cbfeb346e2c2750bb28862e9f3be0f6dc1a1` |

The controlled observation is that simplifying the candidate schema from five model-generated fields
to two coincided with eliminating all four candidate structured-output failures. It does not prove
which exact V1 field or syntax caused each failure, nor does a single stochastic V2 run isolate schema
simplification as the sole causal factor.

## False-support audit

Both `CLAIM_SUPPORTED_FOR_SCENARIO` cases satisfied all required conditions:

- Click: BASE assertion failure, HEAD pass, semantic relation `RELATED`, hidden-oracle direction
  matched.
- Nested workspace: BASE assertion failure, HEAD pass, semantic relation `RELATED`, hidden-oracle
  direction matched.

Incorrect supports: **0**.

## Technical verdict

The V2 run demonstrates a real structured-output reliability improvement on these five fixed cases:
candidate malformed-output count fell from four to zero, and Click and Astroid both reached actual
BASE/HEAD execution. The improvement generalized across every case that reached candidate generation
in V2, but the sample is too small and stochastic to establish a general rate.

PatchProof can honestly claim that, in this sealed run, its reduced two-field draft schema produced
strictly valid candidate output in all six candidate calls; deterministic filename assignment removed
the prior nested-workspace collection error; pacing coincided with zero provider-terminal failures;
and two independently generated tests produced oracle-aligned, semantically related differential
evidence.

PatchProof cannot claim general accuracy, general pull-request correctness, guaranteed quota
avoidance, or robust test synthesis for every repository. attrs regressed at the claim-output budget;
Astroid generated API-invalid setup twice; Marshmallow exposed behavior by an exception that the
current classifier treats as non-comparable rather than an assertion failure; and the nested-workspace
test still relied on lightweight mock objects. The run therefore supports improved candidate
transport reliability, not comprehensive semantic or execution reliability.
