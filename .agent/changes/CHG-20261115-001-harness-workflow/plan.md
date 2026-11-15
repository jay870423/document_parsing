# Implementation Plan: CHG-20261115-001-harness-workflow

## Task graph

| Task | Outcome | Write scope | Depends on | Owner | Mode | Validation |
|---|---|---|---|---|---|---|
| T-001 | Update controller and config | `.agent/main-agent.md`, `.agent/config.yaml` | none | main-agent | serial | YAML and document read |
| T-002 | Add task decomposition rules/templates | `.agent/agents/`, `.agent/skills/`, `.agent/templates/` | T-001 | main-agent | serial | path and content checks |
| T-003 | Add repository CI workflow | `.github/workflows/ci.yml` | T-001 | main-agent | serial | YAML and shell review |
| T-004 | Record Change and memory | `.agent/changes/`, `.agent/memory/` | T-001,T-002,T-003 | main-agent | serial | Change completeness check |

## Parallelization decision

- Execution mode: serial
- Reason: Harness rules, configuration, templates, and CI contract share the same workflow semantics and must be reviewed together.
- Parallel development is allowed for future tasks only after interfaces and write boundaries are fixed.

## Completion evidence

- YAML configuration parsing passed.
- UTF-8 document read validation passed.
- `git diff --check -- .agent` passed.
