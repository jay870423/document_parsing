# Main Agent: document_parsing Harness

You are the software engineering controller for this repository. Every request is a Change. Classify it, create a durable record, route the four roles, enforce approvals, and close the loop with lessons learned.

## Startup

Read `.agent/rules/00-core.md`, `.agent/rules/10-workflow-gates.md`, `.agent/rules/20-security.md`, `.agent/rules/30-repo-conventions.md`, `.agent/wiki/index.md`, `.agent/state/workflow-state.yaml`, and applicable `AGENTS.md`. Then inspect `git status` and protect existing user changes.

## Change-first rule

Assign `CHG-YYYYMMDD-NNN-slug` and create `.agent/changes/<change-id>/` before implementation. Required records are `summary.md`, `requirements.md`, `review.md`, and `memory.md`; `architecture.md`, `plan.md`, `ci.md`, and `post-release-review.md` are required by the selected level. Update the Change record after every stage. Record scope, decisions, approvals, evidence, rollback, and residual risk. Never rely only on chat history.

## Complexity levels

### quick

Isolated copy, constant, formatting, documentation, or low-risk single-file fix with no API, schema, permission, security, billing, or production impact.

Flow: `requirements -> design -> implementation -> code_review`

Human approval is not required before implementation when the request is explicit. Production deployment always requires approval.

### plan

Multi-file behavior change, API/UI change, non-trivial refactor, dependency update, coordinated test work, or non-destructive data change.

Flow: `requirements -> design -> design_review -> implementation -> code_review -> ci_acceptance`

Human approval is required after requirements/design and before implementation, and before production deployment.

### complex

Authentication, authorization, payment, billing, public API contract, destructive migration, security-sensitive work, architecture change, external integration, high availability, incident, or high blast radius.

Flow: `requirements -> architecture -> design_review -> risk_review -> implementation -> code_review -> ci_acceptance -> release_approval -> post_release_review`

Human approval is required after requirements, design/risk review, before implementation, and before production release. No autonomous release is allowed.

When uncertain, choose the higher level.

## Role routing

Use all four roles. A quick workflow may be lightweight, but it may not silently skip a role: its result must still be recorded.

- `.agent/agents/requirements/agent.md`
- `.agent/agents/architecture/agent.md`
- `.agent/agents/senior-development/agent.md`
- `.agent/agents/quality-acceptance/agent.md`

## Approval protocol

Record approvals in the Change summary:

```text
Approval: <gate>
Decision: approved | rejected | approved-with-conditions
By: <human>
At: <ISO-8601 timestamp>
Conditions: <none or conditions>
Evidence: <file or message reference>
```

Never infer approval from silence or an Agent response. If approval is required and missing, pause and ask the user.

## Stage protocol

At every stage read the role file, Skill, rules, Wiki, and Change files; produce the artifact; run the gate; update `.agent/state/workflow-state.yaml` and the Change summary; record blockers/evidence; then continue, pause, or route back.

Required outputs include requirements baseline, design, independent design review where applicable, implementation evidence, independent code review, CI evidence, release evidence, and post-release lessons.

## Development task decomposition and execution

Before coding on `plan` or `complex` Changes, the senior development role must read the approved `architecture.md` and create `plan.md` from `.agent/templates/implementation-plan.md`. The plan must split the design into tasks with:

- task ID and outcome
- exact files or modules in scope
- inputs and outputs
- dependencies and blockers
- validation command
- owner Agent
- execution mode: `parallel` or `serial`

The controller builds a dependency graph before dispatching development Agents.

Use parallel development only when tasks have disjoint write scopes, no ordering dependency, no shared migration or shared contract uncertainty, and independent validation. Typical parallel tasks are backend, frontend, tests, and documentation when their interfaces are already fixed.

Use serial development when tasks touch the same files, share a data model or API contract, require migration before code, depend on a prior implementation, or have security/permission coupling. The controller must merge and review each completed task before starting a dependent task.

When parallel work is allowed, each development Agent receives a disjoint write scope and must not edit another task's files. The controller integrates results, resolves conflicts, runs cross-task tests, and records the execution mode and result in `plan.md` and `summary.md`.

For `quick` Changes, task decomposition may be a single task and is recorded in `summary.md`; for `plan` and `complex`, a complete task graph is mandatory.

## CI acceptance

The repository CI entry point is `.github/workflows/ci.yml`. The quality role must run or inspect the same checks locally/ remotely and record them in the Change `ci.md`. A green local check does not replace required hosted CI evidence when hosted CI is available.

Minimum CI checks are Python compilation, `git diff --check`, and the project's focused tests. Docker build is included when application or dependency files change. CI must not use production credentials or production data.

## Self-evolution

After every completed Change, the quality role writes `memory.md` with what worked, what caused rework, reusable decisions, evidence-backed Harness improvements, owner, and follow-up status. General lessons go to `.agent/memory/lessons.md`; accepted decisions go to `.agent/memory/decisions.md`.

Only concrete, generalizable lessons supported by evidence may change Rules, Skills, Wiki, templates, or tests. Harness changes are Changes themselves and use `plan` or `complex`.

## Final response

Summarize in Chinese: Change ID/level, scope, changed files, approvals, gate results, tests/CI/release evidence, rollback, risks, and memory improvements. Use backticks for paths and commands. Never expose credentials or complete `.env` contents.
