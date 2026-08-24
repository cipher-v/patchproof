# Architecture

## Current architecture (Phase 4)

Three implemented local slices share typed domain models but are not yet orchestrated together.
The ingestion slice is:

```text
GitHub webhook bytes
        |
        v
FastAPI control plane
bounded body + HMAC verification
event/action/public/allowlist filtering
        |
        v
validated PR event
full BASE SHA + full HEAD SHA
        |
        v
VerificationRunStore protocol
        |
        v
SQLite transaction
delivery/revision idempotency
current/stale/superseded history
independent workflow dimensions
```

The Phase 1 deterministic execution slice remains:

```text
base ref + head ref + immutable TestArtifact
                    |
                    v
        GitWorkspaceManager
        resolve full commit SHAs
        detached BASE/HEAD worktrees
                    |
          +---------+---------+
          v                   v
     PytestRunner         PytestRunner
     inject/hash          inject/hash
     contract command     same command/node
     JUnit parse          JUnit parse
          +---------+---------+
                    v
       MechanicalEvidenceClassifier
                    |
                    v
             ChallengeResult
```

Both are local Python components. The control plane records an `ACCEPTED` run but does not yet
queue it or invoke the executor. Mechanical evidence is implemented in the executor; an ingested
run's evidence and semantic claim outcome remain unset.

The Phase 3–4 semantic slice is also local and independently callable:

```text
immutable BASE SHA + immutable HEAD SHA
PR title/body + optional linked-issue excerpts
                    |
                    v
       DeterministicContextRetriever
       bounded per-file Git diffs
       Python AST changed-symbol spans
       nearby committed source/imports
       ranked likely tests and references
                    |
                    v
        bounded PullRequestContext JSON
                    |
                    v
       one Google ADK LlmAgent invocation
       Gemini 3.6 Flash, no tools/history
       structured ClaimSelection output
                    |
                    v
       deterministic schema + grounding check
              /                     \
             v                       v
 one testable BehavioralClaim   typed abstention
                               INSUFFICIENT_EVIDENCE or
                               COUNTERFACTUAL_NOT_APPLICABLE
             |
             v
 bounded candidate-task invocation
 same logical ADK agent identity
 structured CandidateTestProposal
             |
             v
 CandidateTestValidator <----- validated .patchproof.yaml
 path/syntax/test shape         argv templates + allowed paths
 grounded imports/call checks  Python 3.12 + timeout
             |
       +-----+------+
       v            v
 immutable       rejected attempt
 TestArtifact    bounded feedback
 SHA-256             |
       |             v
       |       optional one repair
       |       second/final model call
       +-------------+
             |
             v
 CandidateGenerationSnapshot
 parent ID/hash + model usage + response hash
```

The retriever reads committed Git objects, not uncommitted working-tree content. The model never
searches the repository and does not receive the whole repository. Its output must reference an
exact changed symbol and cite ranges contained in selected deterministic snippets. The live model
test is opt-in and successfully returned a real structured Gemini response.

Candidate proposals contain source and audit rationale, never commands. The contract accepts only
bounded argument arrays matching predefined install/test templates; subprocess execution does not
use a shell. The existing runner is now contract-bound and refuses artifact paths outside the
configured generated-test directories.

There is still no GitHub API narrative client, task service, cloud component, orchestration
between slices, installation runner, execution-driven candidate loop, or result publication.

## Target cloud architecture (future phases)

```text
GitHub pull-request event
          |
          v
Cloud Run control plane -- ADK/Gemini semantic decisions
      |          |
      |          +--> Firestore workflow and evidence records
      v
Cloud Tasks
      |
      v
Cloud Run executor -- checkout, validated commands, pytest, bounded results
      |
      +--> Firestore result/evidence
                    |
                    v
             control plane --> GitHub Check
```

The planned control plane owns orchestration, model access, GitHub App credentials, durable state
transitions, and publication. The planned executor receives immutable revision identifiers and a
validated execution contract, then returns bounded execution facts. It does not receive GitHub
write credentials or model credentials.

## Intended evidence flow

```text
PR diff -> changed symbols -> selected or abstained claim -> candidate test artifact
                                                   |          |
                                                   v          v
                                                 BASE        HEAD
                                                   \          /
                                                    mechanical
                                                  classification
                                                        |
                                                        v
                                            claim-scoped conclusion
```

Webhook authentication, PR identity capture, local workflow persistence, bounded PR context,
claim selection/abstention, artifact BASE/HEAD execution, and mechanical classification are
implemented as separate slices. Candidate generation and bounded repair are also implemented;
semantic execution assessment, dispatch between the slices, and publication remain planned.

## Architectural invariants

- One bounded agent handles semantic decisions; deterministic code owns enforcement and evidence.
- Repository content and PR prose are untrusted model data, never instructions or tool authority.
- Model claims must use exactly supplied changed-symbol identities and snippet-contained citations.
- Each semantic task is an isolated, tool-free, schema-constrained invocation under one logical
  agent identity; claim selection may abstain and candidate generation has a two-call ceiling.
- Generated test commands never come from the model; only validated contract argument arrays are
  executable, without a command shell.
- Candidate source must pass deterministic path, syntax, test-shape, import, and selected-call
  checks before it becomes one immutable hashed `TestArtifact`.
- The same test bytes and identifier are replayed on immutable BASE and HEAD revisions.
- The LLM cannot override mechanical evidence or invent shell commands.
- Insufficient evidence is a valid terminal result.
- Lifecycle, workflow phase, terminal reason, evidence outcome, and publication state remain
  separate dimensions.
- Publication retries do not repeat model or executor work.
- Old revisions remain auditable after a newer HEAD supersedes them.
- GitHub delivery replay and revision replay are distinct idempotency boundaries.
- At most one stored run occurrence is current for a repository and pull request.
- Public claims remain narrower than the technical mechanism.
