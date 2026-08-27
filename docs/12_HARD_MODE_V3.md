# PatchProof hard-mode V3 evaluation

## Status and scope

This document reports one sealed five-case diagnostic run. It is not an accuracy estimate and does
not establish general pull-request correctness.

- Reliability checkpoint: `c11c060f69e53a43364c4b720e5d03c63b4ebd0d`
- V1 benchmark commit: `b86b5b5675f10611453e8c414934e379f783eaac`
- Sealed V2 commit: `7e23d9bc68d7821b340f120b65fe47bc8e8bc32b`
- Declared run: `hard-mode-v3-gemini-3.6-flash-2026-08-27`
- Model: `gemini-3.6-flash`
- Manifest SHA-256: `855638d4541ec9565da16b9f6a11b235e6bf0dabebd6ebdb2f9c0e2c223a7dbe`
- Oracle-gate SHA-256: `128910e11da8b31b01295fae3f56000c3e67b94479611eb782f514a325481f8b`
- Raw-result SHA-256: `a362aef9b41d653f142a08e380e4ae501f4e99c0b45970379c0acc483deb431b`
- Pacing: `BETWEEN_CASES`, with a declared 60-second delay
- Runtime: Python 3.12.6 on Windows 11

The V3 manifest is a binary-exact copy of the five V2 case declarations, in the same order, except
for the declared V3 run ID. All five oracle gates reproduced `BASE=ASSERTION_FAILED` and
`HEAD=PASSED`. Artifact bytes remained unchanged on both revisions, every anti-leakage check
passed, excluded changed tests were absent from model context, and hidden-oracle lines were absent
from model context.

The four observed inter-case delays were 60.000284, 60.000153, 60.000145, and 60.000368 seconds.
There was no whole-case retry, selective rerun, manual candidate edit, or production-code change.
Astroid and nested workspace ended at claim invocation after the Gemini free-tier request quota was
exhausted. These failures are retained as part of the sealed result.

## V1 versus V2 versus V3

| Case | V1 | V2 | V3 | Interpretation |
|------|----|----|----|----------------|
| Click #3678 | Initial and repair had invalid structured output; no execution. | Initial candidate discriminated and was supported. | Initial candidate discriminated and was supported. | Compact claim generation preserved the V2 success; repair was unnecessary and support matched the oracle direction. |
| attrs #1513 | Initial candidate discriminated and was supported. | Claim output reached about 8,175 tokens and exceeded the response budget; no candidate ran. | Claim completed in 271 output tokens; the initial candidate discriminated and was supported. | The V2 claim-output failure did not recur under the 2,048-token boundary. This single run does not establish a general failure rate. |
| Marshmallow #2903 | Claim invocation failed after a provider 429. | Initial and repair produced BASE `TEST_ERROR`, HEAD `PASSED`; final evidence was environmental. | Initial and repair produced `TEST_ERROR` on both revisions; final evidence was environmental. | The feedback exposed the exception and generated line, but the repair did not convert the domain exception into pytest assertion evidence. |
| Astroid #3075 | Initial and repair had invalid structured output; no execution. | Initial and repair executed but omitted required `FunctionDef` constructor arguments on both revisions. | Claim invocation failed when the free-tier request quota was exhausted. | V3 did not reach candidate generation, so it cannot assess whether the improved TypeError feedback would repair this case. |
| Nested workspace | Repair used a safe filename and discriminated. | Safe initial filename; initial candidate discriminated without repair. | Claim invocation failed when the free-tier request quota was exhausted. | V3 cannot assess filename stability or mock-object style because no claim or candidate was returned. |

### Raw counts

| Count | V1 | V2 | V3 |
|-------|---:|---:|---:|
| Cases | 5 | 5 | 5 |
| Claim calls reached | 5 | 5 | 5 |
| Claims selected | 4 | 4 | 3 |
| Invalid claim outputs | 0 | 1 | 0 |
| Candidate structured-output failures | 4 | 0 | 0 |
| Valid initial candidates | 2 | 4 | 3 |
| Candidate artifacts reaching BASE/HEAD execution | 3 | 6 | 4 |
| Repairs used | 3 | 2 | 1 |
| Validated repairs | 1 | 2 | 1 |
| Repaired candidates reaching BASE/HEAD execution | 1 | 2 | 1 |
| Discriminating generated candidates | 2 | 2 | 2 |
| Supported scenarios | 2 | 2 | 2 |
| Insufficient-evidence outcomes | 2 | 2 | 1 |
| Environmental terminal outcomes | 0 | 2 | 1 |
| Provider-terminal failures | 1 | 0 | 2 |
| Incorrect supports | 0 | 0 | 0 |

These are raw diagnostic counts, not accuracy percentages. V3 had nine completed logical model
results, all reporting one provider attempt. The adapter does not retain a reliable failed-provider-
attempt count after terminal invocation failure.

## V3 case evidence

### Click #3678

The claim selected the behavior "Command.get_help_option creates automatic help option with
_click_default_help as storage name" with confidence 0.95. Claim selection used 4,796 prompt, 347
output, and 5,143 total tokens in 6.617 seconds.

The initial candidate validated with rationale: "Verify Command.get_help_option returns an Option
instance whose internal parameter name is '_click_default_help'."

```text
import click

def test_patchproof_generated_behavior():
    cmd = click.Command("test", add_help_option=True)
    ctx = click.Context(cmd)
    help_option = cmd.get_help_option(ctx)
    assert help_option is not None
    assert help_option.name == "_click_default_help"
```

- Artifact SHA-256: `3fb93c048092c24d0d5ccb47d988381b026ebb116a113f944cf519d43b37fcb5`
- Candidate model: 5,024 prompt, 128 output, 5,152 total tokens; 3.395 seconds
- BASE: `ASSERTION_FAILED`, exit 1, 0.781 seconds; observed name was `help`
- HEAD: `PASSED`, exit 0, 0.860 seconds
- Mechanical: `DISCRIMINATING`, `BASE_ASSERTION_FAILED_HEAD_PASSED`
- Repair used: no
- Semantic: `RELATED`, confidence 0.98
- Final: `CLAIM_SUPPORTED_FOR_SCENARIO`
- Hidden-oracle direction matched: yes

### attrs #1513

The claim selected the behavior "`attr.validators.disabled()` context manager restores previous
validation state upon exiting instead of unconditionally enabling validators" with confidence 0.95.
Claim selection used 1,732 prompt, 271 output, and 2,003 total tokens in 6.860 seconds. This is below
the new 2,048-token output boundary; no claim-output diagnostic was emitted.

The initial candidate validated with rationale: "Verify that nesting attr.validators.disabled()
context managers restores the previous validation state upon exit instead of unconditionally setting
it to True."

```text
import attr

def test_patchproof_generated_behavior():
    assert attr.validators.get_run_validators() is True
    with attr.validators.disabled():
        assert attr.validators.get_run_validators() is False
        with attr.validators.disabled():
            assert attr.validators.get_run_validators() is False
        assert attr.validators.get_run_validators() is False
    assert attr.validators.get_run_validators() is True
```

- Artifact SHA-256: `152068fa0a33b54e22c66e77175b214a7450d7c0e27b219df441c4ae5bd20ba6`
- Candidate model: 1,876 prompt, 151 output, 2,027 total tokens; 6.339 seconds
- BASE: `ASSERTION_FAILED`, exit 1, 0.890 seconds; validators were prematurely enabled after the inner context
- HEAD: `PASSED`, exit 0, 0.703 seconds
- Mechanical: `DISCRIMINATING`, `BASE_ASSERTION_FAILED_HEAD_PASSED`
- Repair used: no
- Semantic: `RELATED`, confidence 0.95
- Final: `CLAIM_SUPPORTED_FOR_SCENARIO`
- Hidden-oracle direction matched: yes

### Marshmallow #2903

The claim selected the behavior "Nested schema sub-partial extraction uses attr_name instead of
field_name to prefix partial attribute keys" with confidence 0.95. Claim selection used 3,648
prompt, 383 output, and 4,031 total tokens in 14.783 seconds.

The initial candidate validated with rationale: "Verify that passing dot-delimited partial attribute
paths using the field's attribute name correctly filters nested sub-partial fields when data_key is
configured."

```text
from marshmallow import Schema, fields

def test_patchproof_generated_behavior():
    class ChildSchema(Schema):
        field1 = fields.Str(required=True)
        field2 = fields.Str(required=True)

    class ParentSchema(Schema):
        child = fields.Nested(ChildSchema, data_key="childData")

    schema = ParentSchema()
    # When partial uses attribute name ('child.field1'), field2 should be ignored
    result = schema.load({"childData": {"field1": "val1"}}, partial=("child.field1",))
    assert result == {"child": {"field1": "val1"}}
```

- Initial artifact SHA-256: `c870ad756f5a6fa8ad8ae14d7a06b584a11f1ca37d46127f772bbaeeccad54b6`
- Initial model: 3,618 prompt, 202 output, 3,820 total tokens; 6.065 seconds
- Initial BASE: `TEST_ERROR`, exit 1, 0.797 seconds; uncaught `ValidationError` for missing `field2`
- Initial HEAD: `TEST_ERROR`, exit 1, 0.703 seconds; the same uncaught `ValidationError`
- Initial mechanical: `ENVIRONMENTAL`, `NOT_COMPARABLE`

The exact bounded repair feedback was:

- Category: `EXECUTION_EVIDENCE`
- Summary: "The previous candidate did not produce discriminating BASE/HEAD evidence. Use only
  these bounded execution facts to repair the generated test."
- `BASE status=TEST_ERROR; exception_type=marshmallow.exceptions.ValidationError; message={'childData': {'field2': ['Missing data for required field.']}}; generated_line=13:result = schema.load({"childData": {"field1": "val1"}}, partial=("child.field1",))`
- `HEAD status=TEST_ERROR; exception_type=marshmallow.exceptions.ValidationError; message={'childData': {'field2': ['Missing data for required field.']}}; generated_line=13:result = schema.load({"childData": {"field1": "val1"}}, partial=("child.field1",))`
- `mechanical_status=ENVIRONMENTAL; pattern=NOT_COMPARABLE`
- `same_exception_on_base_and_head=true`

Because both revisions failed with the same exception, the one-sided `TEST_ERROR`/`PASSED`
guidance was not applicable in this run. The repair changed the schema names but did not catch the
exception or express it as an assertion failure.

```text
from marshmallow import Schema, fields

def test_patchproof_generated_behavior():
    class Inner(Schema):
        a = fields.String(required=True)
        b = fields.String(required=True)

    class Outer(Schema):
        inner = fields.Nested(Inner, data_key="inner_key")

    schema = Outer()
    # partial=("inner.a",) specifies attribute name "inner", not data_key "inner_key"
    # On HEAD (with attr_name prefixing), sub-partial ["a"] is extracted and passed to Inner,
    # so missing "b" is ignored due to partial sub-schema loading.
    # On BASE (with field_name/data_key prefixing), sub-partial extraction fails, so Inner
    # receives partial=False and raises ValidationError for missing field "b".
    res = schema.load({"inner_key": {"a": "hello"}}, partial=("inner.a",))
    assert res == {"inner": {"a": "hello"}}
```

- Repair artifact SHA-256: `668657b6a40754b0dd48f3058efc7efa20ca408e5d401b2433d83d14874cdf10`
- Repair model: 4,070 prompt, 303 output, 4,373 total tokens; 6.432 seconds
- Repair BASE: `TEST_ERROR`, exit 1, 0.781 seconds; uncaught `ValidationError` for missing `b`
- Repair HEAD: `TEST_ERROR`, exit 1, 0.672 seconds; the same uncaught `ValidationError`
- Repair mechanical: `ENVIRONMENTAL`, `NOT_COMPARABLE`
- Semantic assessment: not run
- Final: `INSUFFICIENT_EVIDENCE`, terminal status `ENVIRONMENTAL`

The conservative classifier was not changed. Arbitrary `TEST_ERROR` to `PASSED` remains
non-comparable, and this V3 candidate did not produce that one-sided pattern anyway.

### Astroid #3075

The claim call ended as `CLAIM_INVOCATION_ERROR` before structured claim output was returned. The
terminal provider log reported Gemini free-tier `429 RESOURCE_EXHAUSTED` for the 20-request daily
per-project/per-model quota. The normalized raw record retains only
`ClaimAgentInvocationError: ADK claim invocation failed` and `retryable=false`.

No claim, claim diagnostic, initial candidate, execution, repair feedback, repaired source, or
semantic assessment exists for V3. Consequently V3 does not answer whether the new TypeError
feedback would cause a repair to add `end_lineno` and `end_col_offset`.

### Nested workspace

The claim call also ended as `CLAIM_INVOCATION_ERROR` after the same quota exhaustion. No claim,
candidate, execution, repair, or semantic assessment exists for V3. Therefore V3 cannot determine
whether the initial candidate would remain successful, whether repair would remain unnecessary, or
whether Gemini would continue using mock objects.

## Reliability questions

### Claim reliability

- attrs recovered: yes. Its claim completed in 271 output tokens and its initial candidate
  discriminated in the hidden-oracle direction.
- No completed V3 claim exceeded the 2,048-token output budget. Completed claim outputs were 347,
  271, and 383 tokens. The two quota failures returned no completed claim usage record.
- Invalid claim structured outputs changed from one in V2 to zero in V3. The two V3 claim failures
  were provider invocation failures, not schema-validation failures.

### Candidate reliability

- Candidate malformed-output count remained zero from V2 to V3.
- Click remained stable: its initial candidate discriminated and required no repair.
- Nested-workspace stability was not measured because quota exhaustion stopped at claim invocation.

### Repair quality

- Astroid repair quality was not measured because V3 never reached candidate generation.
- Marshmallow feedback materially exposed the exact `ValidationError`, concise message, generated
  failing line, and same-exception fact. The repair did not materially respond to the exception: it
  still allowed `ValidationError` to escape on both revisions.
- Discriminating cases did not increase: V2 and V3 each had two. attrs recovered, but Astroid and
  nested workspace were unavailable because of provider quota, so V3 does not isolate the effect of
  better feedback across all five cases.

### Safety and false support

- The only repair feedback contained four observations; the longest was 252 characters, below the
  500-character bound. A credential-pattern scan of the feedback found zero matches.
- The oracle gate confirmed hidden-oracle source and excluded changed tests remained outside model
  context for every case.
- Both supported cases had BASE assertion failure, HEAD pass, semantic relation `RELATED`, and
  hidden-oracle direction match.
- Incorrect supports: **0**.

## Technical conclusion

V3 directly observed recovery of the attrs claim boundary and continued zero candidate structured-
output failures among calls that reached candidate generation. It also observed that richer bounded
feedback alone did not make the Marshmallow repair produce assertion evidence in this sample.

The two terminal quota failures materially limit the comparison: V3 provides no new Astroid repair
evidence and no nested-workspace stability evidence. The sealed results must not be completed by
selective reruns; a future declared evaluation would be required to test those questions.
