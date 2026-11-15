# Change Summary: CHG-20261115-001-harness-workflow

- Title: Add complexity-based gated Harness workflow
- Complexity: plan
- Status: accepted
- Owner: main-agent
- Created: 2026-11-15
- Request: Make future development use a complete Harness with changes, approvals, CI, and self-evolution.

## Goal

Make every future request traceable through a complexity-appropriate software lifecycle.

## Scope and non-goals

Scope includes quick/plan/complex workflows, human approval points, Change records, durable memory, CI/release evidence, development task decomposition, parallel/serial execution rules, and post-change learning. It adds a repository-level GitHub Actions CI workflow but does not change application runtime behavior.

## Decision log

| Time | Decision | Reason | Evidence |
|---|---|---|---|
| 2026-11-15 | Use quick/plan/complex levels | Match process depth to risk and scope | `.agent/main-agent.md` |
| 2026-11-15 | Every request gets a Change ID and summary | Preserve durable development memory | `.agent/templates/change-summary.md` |
| 2026-11-15 | Human approval is mandatory for release and high-risk gates | Prevent autonomous high-impact actions | `.agent/config.yaml` |
| 2026-11-15 | Development tasks use dependency-aware parallel or serial execution | Prevent write conflicts while enabling safe concurrency | `.agent/templates/implementation-plan.md` |
| 2026-11-15 | Add GitHub Actions validation workflow | Make Harness and Python checks repeatable in CI | `.github/workflows/ci.yml` |

## Gate status

| Gate | Status | Approval required | Evidence |
|---|---|---|---|
| Requirements | passed | no | `.agent/main-agent.md` |
| Design | passed | plan-level review | `.agent/rules/10-workflow-gates.md` |
| Implementation | passed | no runtime code change | `.agent/config.yaml` |
| Code review | passed | recorded by Harness validation | `git diff --check -- .agent` |
| CI acceptance | passed | not executed as application CI | YAML and path checks |

## Approvals

Approval: Harness design
Decision: approved
By: user request
At: 2026-11-15
Conditions: Keep production deployment and destructive changes behind human confirmation.
Evidence: current user request

## Changed files

- `.agent/main-agent.md`
- `.agent/config.yaml`
- `.agent/rules/10-workflow-gates.md`
- `.agent/state/workflow-state.yaml`
- `.agent/templates/`
- `.agent/changes/`
- `.agent/memory/`

## Tests, CI, and release evidence

- YAML configuration parsing: passed.
- UTF-8/ASCII document read validation: passed.
- `git diff --check -- .agent`: passed.
- Application behavior tests: not run because this Change modifies only Harness documentation and metadata.
- CI workflow validation: Python compilation, YAML parsing, metadata path checks, and whitespace checks are defined in `.github/workflows/ci.yml`.

## Rollback

Revert the `.agent/` files introduced by this Change. Application runtime and deployment files are unchanged.

## Residual risks

The Harness is a documented protocol; actual approval enforcement depends on the host Agent following `.agent/main-agent.md`.

## Final outcome

Accepted. Future requests must use the selected workflow and durable Change records.
