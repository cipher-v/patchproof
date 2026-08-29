# PatchProof hard-mode V5 evaluation

## Status and scope

This document reports the final sealed development-diagnostic run on the existing five hard-mode
cases. It is not an accuracy estimate and does not establish general pull-request correctness. These
cases are retired from development tuning after V5; remaining limitations must be measured on an
unseen holdout.

- Production checkpoint: `baf333afc160cd75a90cea1e0568120a9889fb7e`
- Previous sealed evaluation: `ae2a3240c4da1b91f43e69c3759a97c5b6c49f19`
- Declared run: `hard-mode-v5-gemini-3.6-flash-vertex-2026-08-29`
- Model: `gemini-3.6-flash`
- Provider surface: `VERTEX_AI`
- Project/location: `patchproof-506606` / `global`
- Manifest SHA-256: `a017e984cb6d1144c2243b98136c887cdb545b9e83173e108dc3b466cf39f1fa`
- Oracle-gate SHA-256: `c3fafc748d4be2811a3d178d869ae0277060039cfcf81e02ab42b9ab52ce801c`
- Raw-result SHA-256: `018d6242057cc23b13bb038db7ae35a4cab42fbfbaed799a782027b19ac86531`
- Runtime: Python 3.12.6 on Windows 11

V5 reused the exact V4 case declarations in the same order. Before the live journal was created,
all five hidden oracles reproduced `BASE=ASSERTION_FAILED` and `HEAD=PASSED`. The gate verified the
immutable revisions, identical oracle bytes on both revisions, complete changed-test exclusions,
and absence of hidden-oracle material from model context. A separate static signature-source audit
confirmed that every extracted signature came from immutable production source, never an excluded
or test path.

There was no whole-case rerun, selective rerun, model smoke test, manual candidate edit, prompt
change, production-code change, or post-observation repair. The live journal completed all five
cases exactly once.

## Provider and call-budget preflight

Before inference, PatchProof confirmed `VERTEX_AI`, Application Default Credentials, project
`patchproof-506606`, location `global`, enabled service `aiplatform.googleapis.com`, and model
`gemini-3.6-flash`.

- Cases: 5
- Maximum logical calls: `5 × (1 claim + 2 candidate + 1 assessment) = 20`
- Maximum provider attempts per logical call: `1 + 1 transient retry = 2`
- Maximum possible provider calls: `20 × 2 = 40`
- Operator-declared available provider calls: 40
- Preflight passed: yes

The declared capacity is an operator assumption, not a live Vertex quota lookup. V5 completed 15
logical model results using 15 provider attempts; every completed result used one provider attempt.

The pacing policy remained a declared 60-second delay between cases. Observed delays were
1346.054457, 60.000464, 60.000363, and 60.000385 seconds. The first interval was extended by an
operator/tool-session scheduling pause while the append-only run remained active. It caused no
case rerun or additional model request and is preserved as an operational anomaly.

## V5 outcomes

| Case | Final status | Final outcome | Discriminating | Supported | Oracle direction |
| ---- | ------------ | ------------- | -------------- | --------- | ---------------- |
| Click #3678 | `CLAIM_SUPPORTED_FOR_SCENARIO` | `CLAIM_SUPPORTED_FOR_SCENARIO` | yes | yes | matched |
| attrs #1513 | `CLAIM_SUPPORTED_FOR_SCENARIO` | `CLAIM_SUPPORTED_FOR_SCENARIO` | yes | yes | matched |
| Marshmallow #2903 | `INVALID_TEST` | `INSUFFICIENT_EVIDENCE` | no | no | not reached |
| Astroid #3075 | `NON_DISCRIMINATING` | `INSUFFICIENT_EVIDENCE` | no | no | not matched |
| Nested workspace | `CLAIM_SUPPORTED_FOR_SCENARIO` | `CLAIM_SUPPORTED_FOR_SCENARIO` | yes | yes | matched |

### False-support audit

Every supported case met all five required conditions:

| Case | BASE | HEAD | Mechanical | Semantic | Hidden-oracle direction |
| ---- | ---- | ---- | ---------- | -------- | ----------------------- |
| Click #3678 | `ASSERTION_FAILED` | `PASSED` | `DISCRIMINATING` | `RELATED` | matched |
| attrs #1513 | `ASSERTION_FAILED` | `PASSED` | `DISCRIMINATING` | `RELATED` | matched |
| Nested workspace | `ASSERTION_FAILED` | `PASSED` | `DISCRIMINATING` | `RELATED` | matched |

Incorrect supports: **0**.

## Engineering questions

### A. Click expectation normalization

V5 did not repeat V4's warning claim and did not generate `pytest.warns`, so the live run did not
directly exercise `DID NOT WARN` normalization. Claim selection instead chose the automatic help
option's internal storage name. Its ordinary assertion failed on BASE and passed on HEAD, and the
independent semantic assessment returned `RELATED`.

The run did show that true runtime/domain errors were not broadly converted into assertion failures:
Marshmallow's escaping `ValidationError` remained `TEST_ERROR`. Therefore V5 produced no evidence
of weakened runtime-error handling, but the Click-specific expectation-normalization question was
not empirically exercised by this run.

### B. Marshmallow repair materiality

The initial candidate produced BASE `TEST_ERROR` from `marshmallow.exceptions.ValidationError` and
HEAD `PASSED`. The repair added unused `pytest` and `ValidationError` imports and removed comments,
but left executable behavior unchanged. Initial and repair fingerprints were both
`486b382b385f60142e1fb662f8e413b5841511cdeffd6fcf866187355939579b`.

The repair was rejected before execution with
`NO_BEHAVIORAL_REPAIR_CHANGE`. No arbitrary domain exception was converted into support.

### C. Astroid signature grounding

The exact supplied signatures are listed below. They included
`TreeRebuilder.__init__(manager, parser_module=None, data=None)` and
`TreeRebuilder._get_position_info(node, parent)`. V5 candidates consequently got past the V2/V4
constructor failures; both initial and repair executed on both revisions without a constructor
`TypeError`. The exact `FunctionDef` constructor signature was not among the bounded eight
signatures, although the candidate independently supplied its required parent and position fields.

Grounding therefore reduced the API-construction failure seen previously, but did not solve the
behavioral setup. Both candidates returned a concrete `Position` on BASE and HEAD, so both
assertions failed and the case remained non-discriminating.

### D. attrs and workspace stability

Both remained stable. Each selected a bounded relevant claim, produced a valid initial candidate,
required no repair, reproduced BASE assertion failure / HEAD pass, received semantic `RELATED`, and
matched the hidden-oracle direction.

### E. False supports

None. All three supports passed the complete mechanical, semantic, and oracle-direction audit.

### F. Provider and structured-output failures

There were no provider-terminal failures, no transient retries in completed results, no invalid
claim output, and no candidate structured-output failure.

## Case evidence

### Click #3678

Selected claim: **Automatic help option uses reserved name `_click_default_help` to prevent
parameter name collisions with `help`.** Affected symbol: `src/click/core.py::Command.get_help_option`.

Initial fingerprint:
`3762be82a6eacea597ab58af6745f875c897c6875df640297d954b6f578eada4`.

```python
import click


def test_patchproof_generated_behavior():
    cmd = click.Command("test")
    ctx = click.Context(cmd)
    help_option = cmd.get_help_option(ctx)
    assert help_option is not None
    assert help_option.name == "_click_default_help"
```

Execution: BASE `ASSERTION_FAILED` because the observed name was `help`; HEAD `PASSED`. Mechanical
status was `DISCRIMINATING`; semantic relation was `RELATED` with confidence 0.98. No repair.

Signature context: count 8, truncated yes, SHA-256
`869c37207d88863d07a99379248fe3a28898b593a3c3f91d8435170dd77acd41`.

- `src/click/core.py :: Command.get_help_option(ctx)`
- `src/click/core.py :: Command.__init__(name, context_settings=None, callback=None, params=None, help=None, epilog=None, short_help=None, options_metavar='[OPTIONS]', add_help_option=True, no_args_is_help=False, hidden=False, deprecated=False)`
- `src/click/core.py :: Command.get_params(ctx)`
- `src/click/core.py :: Argument.__init__(param_decls, required=None, help=None, **attrs)`
- `src/click/core.py :: Command.format_usage(ctx, formatter)`
- `src/click/core.py :: Command.get_help_option_names(ctx)`
- `src/click/core.py :: Command.invoke(ctx)`
- `src/click/core.py :: Command.make_parser(ctx)`

### attrs #1513

Selected claim: **`validators.disabled()` saves and restores previous validation state upon exit,
allowing nested context managers.** Affected symbol: `src/attr/validators.py::disabled`.

Initial fingerprint:
`b2093cd5a19d172b703a2bc5c95d45146ff01716b5a894de92244b1ae15c0f68`.

```python
import attr


def test_patchproof_generated_behavior():
    attr.validators.set_run_validators(True)
    assert attr.validators.get_run_validators() is True

    with attr.validators.disabled():
        assert attr.validators.get_run_validators() is False
        with attr.validators.disabled():
            assert attr.validators.get_run_validators() is False
        assert attr.validators.get_run_validators() is False

    assert attr.validators.get_run_validators() is True
```

Execution: BASE `ASSERTION_FAILED`; HEAD `PASSED`. Mechanical status was `DISCRIMINATING`; semantic
relation was `RELATED` with confidence 0.95. No repair.

Signature context: count 3, truncated yes, SHA-256
`e0eaaeb6b0d4309c981f510b118ba135919a809b9626682e494733a9a2b1b0b9`.

- `src/attr/validators.py :: disabled()`
- `src/attr/_config.py :: get_run_validators()`
- `src/attr/_config.py :: set_run_validators(run)`

### Marshmallow #2903

Selected claim: **Use `attr_name` instead of `field_name` for the prefix when constructing
sub-partial keys for nested schemas.** Affected symbol:
`src/marshmallow/schema.py::Schema._deserialize`.

Initial fingerprint:
`486b382b385f60142e1fb662f8e413b5841511cdeffd6fcf866187355939579b`.

```python
from marshmallow import Schema, fields


class ChildSchema(Schema):
    nested_field = fields.Str(required=True)


class ParentSchema(Schema):
    child = fields.Nested(ChildSchema, data_key="childDataKey")


def test_patchproof_generated_behavior():
    schema = ParentSchema()
    result = schema.load({"childDataKey": {}}, partial=("child.nested_field",))
    assert result == {"child": {}}
```

Initial execution: BASE `TEST_ERROR` from `ValidationError`; HEAD `PASSED`; mechanical status
`ENVIRONMENTAL` / `NOT_COMPARABLE`.

Repair fingerprint:
`486b382b385f60142e1fb662f8e413b5841511cdeffd6fcf866187355939579b`.

```python
import pytest
from marshmallow import Schema, fields, ValidationError


class ChildSchema(Schema):
    nested_field = fields.Str(required=True)


class ParentSchema(Schema):
    child = fields.Nested(ChildSchema, data_key="childDataKey")


def test_patchproof_generated_behavior():
    schema = ParentSchema()
    result = schema.load({"childDataKey": {}}, partial=("child.nested_field",))
    assert result == {"child": {}}
```

The repair was rejected as `NO_BEHAVIORAL_REPAIR_CHANGE` and did not execute.

Signature context for both attempts: count 8, truncated yes, SHA-256
`c317fad9d83754aba27eafe999a152288ce496c0aaa853e8aa04f2119cad3405`.

- `src/marshmallow/schema.py :: Schema._deserialize(data, *, error_store, many=False, partial=None, unknown=RAISE, index=None)`
- `src/marshmallow/schema.py :: Schema.__init__(*, only=None, exclude=(), many=None, load_only=(), dump_only=(), partial=None, unknown=None)`
- `src/marshmallow/schema.py :: Schema.dump(obj, *, many=None)`
- `src/marshmallow/schema.py :: Schema.dumps(obj, *args, many=None, **kwargs)`
- `examples/package_json_example.py :: Version._deserialize(value, *args, **kwargs)`
- `src/marshmallow/error_store.py :: ErrorStore.store_error(messages, field_name=SCHEMA, index=None)`
- `src/marshmallow/fields.py :: AwareDateTime._deserialize(value, attr, data, **kwargs)`
- `src/marshmallow/fields.py :: Boolean._deserialize(value, attr, data, **kwargs)`

### Astroid #3075

Selected claim: **`TreeRebuilder._get_position_info` handles `TokenError` from `generate_tokens` by
returning `None` instead of raising.** Affected symbol:
`astroid/rebuilder.py::TreeRebuilder._get_position_info`.

Initial fingerprint:
`9dba403a31a29715321b6934979ccb32b48bb093fcea5eb19c52967b4e45c820`.

```python
import ast
import pytest
from tokenize import TokenError
from astroid.rebuilder import TreeRebuilder
from astroid.nodes import FunctionDef
from astroid.manager import AstroidManager


def test_patchproof_generated_behavior():
    manager = AstroidManager()
    rebuilder = TreeRebuilder(manager=manager, data="def foo((\n    pass")
    ast_node = ast.parse("def foo(): pass").body[0]
    ast_node.lineno = 1
    ast_node.end_lineno = 2
    ast_node.body[0].lineno = 2
    parent_node = FunctionDef(
        name="foo", lineno=1, col_offset=0, parent=None, end_lineno=2, end_col_offset=8
    )
    try:
        res = rebuilder._get_position_info(ast_node, parent_node)
        assert res is None
    except TokenError:
        pytest.fail("TreeRebuilder._get_position_info raised TokenError instead of returning None")
```

Initial execution: BASE `ASSERTION_FAILED`; HEAD `ASSERTION_FAILED`; both returned the same concrete
`Position`. Mechanical status was `NON_DISCRIMINATING`.

Repair fingerprint:
`b9b49d0802ca72047bb62eed4e5059d532b8fe76bf22210f4bc5d8c52749efb6`.

```python
import ast
from astroid.rebuilder import TreeRebuilder
from astroid.nodes import FunctionDef
from astroid.manager import AstroidManager


def test_patchproof_generated_behavior():
    manager = AstroidManager()
    rebuilder = TreeRebuilder(manager=manager, data="def foo(\n")
    ast_node = ast.parse("def foo(): pass").body[0]
    ast_node.lineno = 1
    ast_node.end_lineno = 1
    ast_node.body[0].lineno = 1
    parent_node = FunctionDef(
        name="foo", lineno=1, col_offset=0, parent=None, end_lineno=1, end_col_offset=8
    )
    res = rebuilder._get_position_info(ast_node, parent_node)
    assert res is None
```

The repair materially changed executable behavior and was validated, but BASE and HEAD again both
returned the same `Position` and both assertions failed. No second repair was allowed.

Signature context for both attempts: count 8, truncated yes, SHA-256
`e9067113cfc182f7729a77d16f604d6d7cda618f7c93b3d5615085c9b211a2e3`.

- `astroid/rebuilder.py :: TreeRebuilder._get_position_info(node, parent)`
- `astroid/rebuilder.py :: TreeRebuilder.__init__(manager, parser_module=None, data=None)`
- `astroid/rebuilder.py :: TreeRebuilder._get_context(node)`
- `astroid/rebuilder.py :: TreeRebuilder._get_doc(node)`
- `astroid/_ast.py :: get_parser_module(type_comments=True)`
- `astroid/builder.py :: AstroidBuilder._post_build(module, builder, encoding)`
- `astroid/nodes/node_classes.py :: AssignAttr.__init__(attrname, lineno, col_offset, parent, *, end_lineno, end_col_offset)`
- `astroid/nodes/node_classes.py :: Attribute.__init__(attrname, lineno, col_offset, parent, *, end_lineno, end_col_offset)`

### Nested workspace

Selected claim: **`resolve_owner` selects the deepest workspace owning `candidate_path` instead of
the shallowest.** Affected symbol: `workspace_registry.py::resolve_owner`.

Initial fingerprint:
`1dceaa8b27b2f41338826002d51c2b8ae06d0d7e5d92fb15aed9c3b9a73f5f9f`.

```python
import pytest
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
    shallow = MockWorkspace(root="/projects")
    deep = MockWorkspace(root="/projects/app/src")
    registry = MockRegistry(candidates=[shallow, deep])
    result = resolve_owner(registry, "/projects/app/src/main.py")
    assert result == deep
```

Execution: BASE `ASSERTION_FAILED`; HEAD `PASSED`. Mechanical status was `DISCRIMINATING`; semantic
relation was `RELATED` with confidence 1.0. No repair.

Signature context: count 2, truncated no, SHA-256
`9ecd32103a6f3d944e0f1dfcf3bfe4c4c00c57189e4cf57b86fb7e731601d248`.

- `workspace_registry.py :: resolve_owner(registry, candidate_path)`
- `workspace_registry.py :: ProjectRegistry.candidates_for(candidate_path)`

## V1–V5 case comparison

| Case | V1 | V2 | V3 | V4 | V5 | Interpretation |
| ---- | -- | -- | -- | -- | -- | -------------- |
| Click #3678 | Initial and repair had invalid candidate output. | Initial candidate supported. | Initial candidate supported. | Warning candidate produced BASE `TEST_ERROR`, HEAD `PASSED`; environmental. | Initial storage-name assertion supported. | V5 selected a different, directly testable claim; `DID NOT WARN` normalization was not exercised live. |
| attrs #1513 | Initial candidate supported. | Claim exceeded response budget. | Initial candidate supported. | Initial candidate supported. | Initial candidate supported. | Nested state restoration remained stable and repair-free. |
| Marshmallow #2903 | Claim invocation failed. | Initial and repair environmental. | Initial and repair environmental. | Initial and repair environmental. | Initial environmental; cosmetic repair rejected as a behavioral no-op. | Fingerprinting prevented a byte-distinct but behaviorally unchanged repair from executing. |
| Astroid #3075 | Initial and repair had invalid candidate output. | Constructor arguments remained incomplete. | Claim invocation failed. | One repair still omitted required constructor state. | Constructors executed, but initial and repair failed assertions on both revisions. | Signature grounding reduced API-construction failure; behavioral setup remained non-discriminating. |
| Nested workspace | Repair supported. | Initial candidate supported. | Claim invocation failed. | Initial candidate supported. | Initial candidate supported. | The safe initial mock-object scenario remained stable. |

### Raw diagnostic counts

| Count | V1 | V2 | V3 | V4 | V5 |
| ----- | -: | -: | -: | -: | -: |
| Claim calls reached | 5 | 5 | 5 | 5 | 5 |
| Claims selected | 4 | 4 | 3 | 5 | 5 |
| Invalid claim outputs | 0 | 1 | 0 | 0 | 0 |
| Provider-terminal failures | 1 | 0 | 2 | 0 | 0 |
| Valid initial candidates | 2 | 4 | 3 | 5 | 5 |
| Candidate structured-output failures | 4 | 0 | 0 | 0 | 0 |
| Candidates reaching execution | 3 | 6 | 4 | 8 | 6 |
| Repairs used | 3 | 2 | 1 | 3 | 2 |
| Repairs rejected as byte duplicates | 0 | 0 | 0 | 0 | 0 |
| Repairs rejected as behavioral no-ops | 0 | 0 | 0 | 0 | 1 |
| Validated repairs | 1 | 2 | 1 | 3 | 1 |
| Discriminating cases | 2 | 2 | 2 | 2 | 3 |
| Supported scenarios | 2 | 2 | 2 | 2 | 3 |
| Insufficient-evidence outcomes | 2 | 2 | 1 | 3 | 2 |
| Environmental terminal outcomes | 0 | 2 | 1 | 3 | 0 |
| Environmental candidate evaluations | 1 | 4 | 2 | 6 | 1 |
| Incorrect supports | 0 | 0 | 0 | 0 | 0 |

These are raw diagnostic counts, not accuracy percentages. Marshmallow accounts for V5's one
environmental candidate evaluation, but its final status became `INVALID_TEST` after the no-op
repair was rejected.

## Model accounting

- Completed logical model results: 15
- Provider attempts represented by completed results: 15
- Failed logical model calls: 0
- Prompt tokens: 49,864
- Output tokens: 3,287
- Total tokens: 53,292
- Total model duration: 96.702 seconds
- Repairs: 2
- Semantic assessments: 3, only after mechanical discrimination

## Remaining limitations for the unseen holdout

1. V5 did not exercise live `DID NOT WARN` or `DID NOT RAISE` normalization because Click selected a
   different claim. The feature remains covered by deterministic local tests, not by this sealed run.
2. Bounded signature selection can omit a callable the model later chooses, as happened with
   Astroid's `FunctionDef`; truncation metadata makes this omission auditable.
3. Correct signatures prevent one failure class but cannot guarantee that a generated input reaches
   the intended behavior. Astroid constructed objects successfully but did not trigger `TokenError`.
4. Behavioral fingerprints intentionally detect syntax-level executable equivalence, not deep
   semantic equivalence.
5. The first pacing interval was longer than declared because of an external tool-session scheduling
   pause. The journal preserved the actual duration and no case was rerun.

No V6 is authorized or planned. No V5 outcome will be used to tune production code or prompts on
these five retired cases.
