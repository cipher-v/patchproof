# PatchProof hard-mode V4 evaluation

## Status and scope

This document reports one sealed five-case diagnostic run. It is not an accuracy estimate and does
not establish general pull-request correctness.

- Vertex migration checkpoint: `bb0297c8cf0798c1aa6867c3b83e7da5559aaf53`
- V1 benchmark commit: `b86b5b5675f10611453e8c414934e379f783eaac`
- Sealed V2 commit: `7e23d9bc68d7821b340f120b65fe47bc8e8bc32b`
- Sealed V3 commit: `0ac4946a78158b2e5ce146bbe7aa15062e01ca49`
- Declared run: `hard-mode-v4-gemini-3.6-flash-vertex-2026-08-28`
- Model: `gemini-3.6-flash`
- Provider surface: `VERTEX_AI`
- Project/location: `patchproof-506606` / `global`
- Manifest SHA-256: `93b0dadedba843c37b4fc67ea9fe19a08a9e645759eb4129f3530c819100aceb`
- Oracle-gate SHA-256: `57e1ca8a646236bc227164f6fd1915c7ab15d66bcc2e98947bf9e68a53d72437`
- Raw-result SHA-256: `b4a071fb925390f56563f8341a60ef098f0eb116a9e27e9a7d4224e256afbc90`
- Pacing: `BETWEEN_CASES`, with a declared 60-second delay
- Runtime: Python 3.12.6 on Windows 11

The five V4 case declarations are semantically identical to V3 and remain in the same order. All
five oracle gates reproduced `BASE=ASSERTION_FAILED` and `HEAD=PASSED`. The gate also verified the
immutable revisions, identical oracle artifact bytes on BASE and HEAD, complete changed-test
exclusions, and absence of hidden-oracle material from model context. The observed inter-case delays
were 60.000632, 60.000220, 60.000813, and 60.000657 seconds.

There was no whole-case rerun, selective rerun, manual candidate edit, or post-observation production
change. No provider-terminal failure or candidate structured-output failure occurred. All 15
completed logical model results reported one provider attempt.

## Provider and call-budget preflight

Before the live journal was created, PatchProof confirmed `VERTEX_AI`, ADC availability, project
`patchproof-506606`, location `global`, and enabled service `aiplatform.googleapis.com`. The model
remained `gemini-3.6-flash`.

- Cases: 5
- Maximum logical calls: `5 × (1 claim + 2 candidate + 1 assessment) = 20`
- Maximum provider attempts per logical call: `1 + 1 transient retry = 2`
- Maximum possible provider calls: `20 × 2 = 40`
- Operator-declared available provider calls: 40
- Preflight passed: yes

The declared availability is a conservative operator capacity assumption, not a live quota lookup.

## V1 versus V2 versus V3 versus V4

| Case | V1 | V2 | V3 | V4 | Interpretation |
| ---- | -- | -- | -- | -- | -------------- |
| Click #3678 | Initial and repair had invalid candidate output; no execution. | Initial candidate discriminated and was supported. | Initial candidate discriminated and was supported. | Initial and repair produced BASE `TEST_ERROR`, HEAD `PASSED`; environmental. | V4 selected a warning behavior rather than V2/V3's help-option storage-name behavior. `pytest.warns` failure was conservatively classified as `TEST_ERROR`, and changing `UserWarning` to `Warning` did not repair it. |
| attrs #1513 | Initial candidate discriminated and was supported. | Claim exceeded the response budget; no candidate ran. | Compact claim completed; initial candidate discriminated and was supported. | Initial candidate discriminated and was supported. | The nested `disabled()` behavior remained stable, bounded, and repair-free; the hidden-oracle direction matched. |
| Marshmallow #2903 | Claim invocation failed after provider 429. | Initial and repair produced BASE `TEST_ERROR`, HEAD `PASSED`; environmental. | Initial and repair produced `TEST_ERROR` on both revisions; environmental. | Initial and repair produced BASE `TEST_ERROR`, HEAD `PASSED`; environmental. | The initial test expressed the intended omitted-field/partial relationship, but the repair did not encode the expected old-version `ValidationError` as assertion evidence or otherwise change the failed assumption. |
| Astroid #3075 | Initial and repair had invalid candidate output; no execution. | Initial and repair omitted required `FunctionDef` constructor arguments. | Claim invocation failed after provider quota exhaustion. | Initial used unsupported `doc`; repair removed it and added position arguments but omitted required `parent`; environmental. | Bounded TypeError evidence caused a material constructor change, but one repair still did not reach the TokenError behavior. |
| Nested workspace | Repair used a safe filename and discriminated. | Safe initial filename; initial candidate discriminated without repair. | Claim invocation failed after provider quota exhaustion. | Safe initial filename; initial candidate discriminated and was supported without repair. | The mock-object approach remained stable across provider migration and matched the hidden-oracle direction. |

### Raw counts

| Count | V1 | V2 | V3 | V4 |
| ----- | -: | -: | -: | -: |
| Cases | 5 | 5 | 5 | 5 |
| Claim calls reached | 5 | 5 | 5 | 5 |
| Claims selected | 4 | 4 | 3 | 5 |
| Invalid claim outputs | 0 | 1 | 0 | 0 |
| Provider-terminal failures | 1 | 0 | 2 | 0 |
| Valid initial candidates | 2 | 4 | 3 | 5 |
| Candidate structured-output failures | 4 | 0 | 0 | 0 |
| Initial candidates reaching execution | 2 | 4 | 3 | 5 |
| Repairs used | 3 | 2 | 1 | 3 |
| Validated repairs | 1 | 2 | 1 | 3 |
| Repaired candidates reaching execution | 1 | 2 | 1 | 3 |
| Discriminating candidates | 2 | 2 | 2 | 2 |
| Supported scenarios | 2 | 2 | 2 | 2 |
| Insufficient-evidence outcomes | 2 | 2 | 1 | 3 |
| Environmental terminal outcomes | 0 | 2 | 1 | 3 |
| Incorrect supports | 0 | 0 | 0 | 0 |

These are raw diagnostic counts, not accuracy percentages.

## V4 case evidence

### Click #3678

The claim selected: "Command.get_params warns when a parameter shares a name with the automatic
help option or an argument," confidence 0.95. Claim use was 5,554 prompt, 317 output, and 5,871
total tokens in 6.589 seconds.

The validated initial candidate rationale was: "Verify that `Command.get_params` issues a warning
when an argument and an option share the same parameter name."

```text
import pytest
import click

def test_patchproof_generated_behavior():
    cmd = click.Command("test", params=[click.Argument(["a"]), click.Option(["--a"])])
    ctx = click.Context(cmd)
    with pytest.warns(UserWarning, match="used by an argument and another parameter"):
        cmd.get_params(ctx)
```

- Initial artifact SHA-256: `e6f290f272572024e5507d7d8b845e3613c103b6da197dbb991264f6e3414e4b`
- Initial model use: 5,332 prompt, 134 output, 5,466 total tokens; 5.634 seconds
- Initial BASE: `TEST_ERROR`, exit 1, 1.140 seconds; `pytest.warns` reported `DID NOT WARN`
- Initial HEAD: `PASSED`, exit 0, 1.094 seconds
- Initial mechanical result: `ENVIRONMENTAL`, `NOT_COMPARABLE`

The exact bounded repair feedback was:

- Category: `EXECUTION_EVIDENCE`
- Summary: "The previous candidate did not produce discriminating BASE/HEAD evidence. Use only these bounded execution facts to repair the generated test."
- `BASE status=TEST_ERROR; generated_line=7:with pytest.warns(UserWarning, match="used by an argument and another parameter"):`
- `HEAD status=PASSED`
- `mechanical_status=ENVIRONMENTAL; pattern=NOT_COMPARABLE`
- `An exception escaping on one revision is mechanically TEST_ERROR. If it represents expected claim behavior and its exception import is grounded in the supplied context, convert it into an explicit deterministic pytest assertion/failure; do not catch unrelated exceptions.`

The validated repair rationale was: "Catch `Warning` instead of `UserWarning` as Click's
`warnings.warn` without a category emits default `UserWarning` which inherits from `Warning`,
ensuring BASE fails gracefully with `pytest.warns` expectation failure (no warning raised) while
HEAD passes."

```text
import pytest
import click

def test_patchproof_generated_behavior():
    cmd = click.Command("test", params=[click.Argument(["a"]), click.Option(["--a"])])
    ctx = click.Context(cmd)
    with pytest.warns(Warning, match="used by an argument and another parameter"):
        cmd.get_params(ctx)
```

- Repair artifact SHA-256: `d97e89c4918f5f1c8d426793cddf9e02132e4e14e154ddaf3ef7942d18629d3a`
- Repair model use: 5,634 prompt, 165 output, 5,799 total tokens; 2.214 seconds
- Repair BASE: `TEST_ERROR`, exit 1, 1.172 seconds; `pytest.warns` again reported `DID NOT WARN`
- Repair HEAD: `PASSED`, exit 0, 1.031 seconds
- Repair mechanical result: `ENVIRONMENTAL`, `NOT_COMPARABLE`
- Semantic assessment: not run
- Final: `INSUFFICIENT_EVIDENCE`, terminal `ENVIRONMENTAL`

### attrs #1513

The claim selected: "attr.validators.disabled() restores the previous validators state upon exiting
rather than unconditionally enabling validators," confidence 0.95. Claim use was 2,490 prompt, 327
output, and 2,817 total tokens in 6.223 seconds. The exact output-token count was **327**.

The validated initial candidate rationale was: "Verify that exiting a nested
attr.validators.disabled() context restores the previous disabled validator state rather than
prematurely setting it to True."

```text
import attr
import attr.validators

def test_patchproof_generated_behavior():
    assert attr.validators.get_run_validators() is True
    with attr.validators.disabled():
        assert attr.validators.get_run_validators() is False
        with attr.validators.disabled():
            assert attr.validators.get_run_validators() is False
        assert attr.validators.get_run_validators() is False
    assert attr.validators.get_run_validators() is True
```

- Artifact SHA-256: `f8dd886095b8104f7a98bb566a2e6b7210174a3499eb6c277b94b1a2c432068f`
- Candidate model use: 2,188 prompt, 157 output, 2,345 total tokens; 5.432 seconds
- BASE: `ASSERTION_FAILED`, exit 1, 0.828 seconds; validators became enabled after the inner context
- HEAD: `PASSED`, exit 0, 0.797 seconds
- Mechanical: `DISCRIMINATING`, `BASE_ASSERTION_FAILED_HEAD_PASSED`
- Repair used: no
- Semantic: `RELATED`, confidence 0.98; 1,472 prompt, 86 output, 1,558 total tokens in 5.886 seconds
- Final: `CLAIM_SUPPORTED_FOR_SCENARIO`
- Hidden-oracle direction matched: yes

### Marshmallow #2903

The claim selected nested partial loading by internal attribute name rather than `data_key`,
confidence 0.95. Claim use was 4,406 prompt, 427 output, and 4,833 total tokens in 8.345 seconds.

The validated initial candidate rationale was: "Corrected test assumption regarding nested partial
sub-key prefixes: when partial loading uses Python attribute names for nested fields configured with
data_key, the prefix calculation now correctly matches attribute names rather than external data
keys."

```text
from marshmallow import Schema, fields


class InnerSchema(Schema):
    zip_code = fields.Str(required=True)
    city = fields.Str(required=True)


class OuterSchema(Schema):
    address = fields.Nested(InnerSchema, data_key="homeAddress")


def test_patchproof_generated_behavior():
    schema = OuterSchema()
    result = schema.load({"homeAddress": {"city": "NYC"}}, partial=("address.zip_code",))
    assert result == {"address": {"city": "NYC"}}
```

- Initial artifact SHA-256: `60a3d184a440956e9f3fb5b35fef0dbc017b9d1aecade2252a65d7d1a4b7a441`
- Initial model use: 4,003 prompt, 203 output, 5,360 total tokens; 10.602 seconds
- Initial BASE: `TEST_ERROR`, exit 1, 0.968 seconds; `ValidationError` reported missing required `zip_code`
- Initial HEAD: `PASSED`, exit 0, 0.719 seconds
- Initial mechanical result: `ENVIRONMENTAL`, `NOT_COMPARABLE`

The exact bounded repair feedback was:

- Category: `EXECUTION_EVIDENCE`
- Summary: "The previous candidate did not produce discriminating BASE/HEAD evidence. Use only these bounded execution facts to repair the generated test."
- `BASE status=TEST_ERROR; exception_type=marshmallow.exceptions.ValidationError; message={'homeAddress': {'zip_code': ['Missing data for required field.']}}; generated_line=15:result = schema.load({"homeAddress": {"city": "NYC"}}, partial=("address.zip_code",))`
- `HEAD status=PASSED`
- `mechanical_status=ENVIRONMENTAL; pattern=NOT_COMPARABLE`
- `An exception escaping on one revision is mechanically TEST_ERROR. If it represents expected claim behavior and its exception import is grounded in the supplied context, convert it into an explicit deterministic pytest assertion/failure; do not catch unrelated exceptions.`

The validated repair rationale was: "Corrected test assumption so that partial deserialization using
attribute names correctly passes in sub-partial keys to nested schemas configured with data_key."

```text
import pytest
from marshmallow import Schema, fields, ValidationError


class InnerSchema(Schema):
    zip_code = fields.Str(required=True)
    city = fields.Str(required=True)


class OuterSchema(Schema):
    address = fields.Nested(InnerSchema, data_key="homeAddress")


def test_patchproof_generated_behavior():
    schema = OuterSchema()
    result = schema.load({"homeAddress": {"city": "NYC"}}, partial=("address.zip_code",))
    assert result == {"address": {"city": "NYC"}}
```

- Repair artifact SHA-256: `cb8df26b34c02ddda5b07edd7fb0dc5184b8733de821925cc77e211021e1a28b`
- Repair model use: 4,412 prompt, 196 output, 4,608 total tokens; 2.098 seconds
- Repair BASE: `TEST_ERROR`, exit 1, 0.766 seconds; the same `ValidationError`
- Repair HEAD: `PASSED`, exit 0, 0.734 seconds
- Repair mechanical result: `ENVIRONMENTAL`, `NOT_COMPARABLE`
- Semantic assessment: not run
- Final: `INSUFFICIENT_EVIDENCE`, terminal `ENVIRONMENTAL`

The repair did **not** change the failed assumption. It added `pytest` and `ValidationError` imports
but used neither; the executable schema/load/assert behavior was unchanged and it did not turn the
old-version domain exception into assertion evidence.

### Astroid #3075

The claim selected catching `TokenError` in `TreeRebuilder._get_position_info` and returning `None`,
confidence 0.95. Claim use was 3,860 prompt, 366 output, and 4,226 total tokens in 6.622 seconds.

The validated initial candidate rationale was: "Verify that TreeRebuilder._get_position_info catches
TokenError when tokenizing malformed source lines and returns None."

```text
import ast
from astroid import nodes
from astroid.rebuilder import TreeRebuilder

def test_patchproof_generated_behavior():
    rebuilder = TreeRebuilder(manager=None, data="(")
    node = ast.parse("def foo():\n    pass").body[0]
    parent = nodes.FunctionDef(name="foo", doc=None)
    assert rebuilder._get_position_info(node, parent) is None
```

- Initial artifact SHA-256: `ae69da561e197314985e7234a48a1d101d298c17eb76e3aa3932c79db2f5d0c9`
- Initial model use: 3,618 prompt, 152 output, 5,135 total tokens; 9.761 seconds
- Initial BASE: `TEST_ERROR`, exit 1, 1.953 seconds; unexpected keyword argument `doc`
- Initial HEAD: `TEST_ERROR`, exit 1, 1.921 seconds; the same TypeError
- Initial mechanical result: `ENVIRONMENTAL`, `NOT_COMPARABLE`

The exact bounded repair feedback was:

- Category: `EXECUTION_EVIDENCE`
- Summary: "The previous candidate did not produce discriminating BASE/HEAD evidence. Use only these bounded execution facts to repair the generated test."
- `BASE status=TEST_ERROR; exception_type=TypeError; message=FunctionDef.__init__() got an unexpected keyword argument 'doc'; generated_line=8:parent = nodes.FunctionDef(name="foo", doc=None)`
- `HEAD status=TEST_ERROR; exception_type=TypeError; message=FunctionDef.__init__() got an unexpected keyword argument 'doc'; generated_line=8:parent = nodes.FunctionDef(name="foo", doc=None)`
- `mechanical_status=ENVIRONMENTAL; pattern=NOT_COMPARABLE`
- `same_exception_on_base_and_head=true`

The validated repair rationale was: "Fix constructor signature for nodes.FunctionDef so that
TreeRebuilder._get_position_info can be called and verify that TokenError during tokenization returns
None."

```text
import ast
from astroid import nodes
from astroid.rebuilder import TreeRebuilder

def test_patchproof_generated_behavior():
    rebuilder = TreeRebuilder(manager=None, data="(")
    node = ast.parse("def foo():\n    pass").body[0]
    parent = nodes.FunctionDef(name="foo", lineno=1, col_offset=0, end_lineno=1, end_col_offset=7)
    assert rebuilder._get_position_info(node, parent) is None
```

- Repair artifact SHA-256: `6c78451e3099105ef967008a4e0967bb39aefedb8d1b5ae4c1cad30ccab65ca8`
- Repair model use: 3,965 prompt, 184 output, 4,149 total tokens; 2.562 seconds
- Repair BASE: `TEST_ERROR`, exit 1, 2.015 seconds; missing required positional argument `parent`
- Repair HEAD: `TEST_ERROR`, exit 1, 1.969 seconds; the same TypeError
- Repair mechanical result: `ENVIRONMENTAL`, `NOT_COMPARABLE`
- Semantic assessment: not run
- Final: `INSUFFICIENT_EVIDENCE`, terminal `ENVIRONMENTAL`

The repair **did** respond materially to the observed diagnostic: it removed unsupported `doc` and
added the positional fields. It was incomplete because it omitted the required `parent` argument,
so the test never reached the TokenError behavior.

### Nested workspace

The claim selected deepest-path ownership resolution, confidence 0.95. Claim use was 1,814 prompt,
310 output, and 2,124 total tokens in 5.473 seconds.

The validated initial candidate rationale was: "Verify that resolve_owner selects the workspace
candidate with the deepest path (maximum number of path parts) rather than the shallowest one."

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
    ws_shallow = MockWorkspace(root="/a")
    ws_deep = MockWorkspace(root="/a/b/c")
    registry = MockRegistry([ws_shallow, ws_deep])
    result = resolve_owner(registry, "/a/b/c/file.txt")
    assert result is ws_deep
```

- Artifact SHA-256: `3d2a070f9f1056836883fa70c6228f491a51e1a38e351a8ea7b101fde30fd00e`
- Candidate model use: 1,631 prompt, 222 output, 1,853 total tokens; 4.825 seconds
- Safe target path: `patchproof_generated_tests/test_patchproof_generated_initial.py`
- BASE: `ASSERTION_FAILED`, exit 1, 0.578 seconds; shallow workspace was returned
- HEAD: `PASSED`, exit 0, 0.454 seconds
- Mechanical: `DISCRIMINATING`, `BASE_ASSERTION_FAILED_HEAD_PASSED`
- Repair used: no
- Semantic: `RELATED`, confidence 0.95; 1,198 prompt, 104 output, 1,302 total tokens in 4.057 seconds
- Final: `CLAIM_SUPPORTED_FOR_SCENARIO`
- Hidden-oracle direction matched: yes

## False-support audit

The only support outcomes were attrs and nested workspace. Each independently had:

- BASE `ASSERTION_FAILED`;
- HEAD `PASSED`;
- semantic relation `RELATED`; and
- a generated-evidence direction matching the hidden oracle.

Incorrect supports: **0**.

## Provider migration interpretation

V1, V2, and V3 used `gemini-3.6-flash` through the Gemini Developer API. V4 used
`gemini-3.6-flash` through Vertex AI. V4 therefore differs in provider surface. The model name,
semantic schemas, instructions, temperature, thinking level, retry policy, cases, BASE/HEAD
revisions, and hidden oracles remained fixed.

The V4 changes in observed outcomes cannot be attributed solely to PatchProof reliability changes,
because provider surface may also contribute. In this one diagnostic, Vertex eliminated the V3
provider-terminal failures and every reached structured output validated, but the number of
discriminating and supported cases remained two. Click shifted to a mechanically non-comparable
warning assertion; Marshmallow repair did not use the supplied domain exception; Astroid repair was
responsive but incomplete.

## Model accounting

- Completed logical model results: 15
- Provider attempts represented by completed results: 15
- Provider-terminal failures: 0
- Candidate structured-output failures: 0
- Prompt tokens reported: 51,577
- Output tokens reported: 3,350
- Total tokens reported: 57,446
- Total model duration: 86.323 seconds

No V5 evaluation was run.
