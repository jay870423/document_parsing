# Implementation Plan: Remove AG-UI, A2UI, and WebMCP

- Requirement: `.agent/changes/CHG-20261115-002-remove-agent-capabilities/requirements.md`
- Architecture: `.agent/changes/CHG-20261115-002-remove-agent-capabilities/architecture.md`

## Task list and dependency graph

| Task | Outcome | Files/modules | Depends on | Owner | Mode | Validation |
|---|---|---|---|---|---|---|
| T-001 | Remove backend protocol routes and module | `app/main.py`, `app/agent_protocols.py` | approval | senior-development | serial | compile and route inspection |
| T-002 | Remove protocol UI and static debug entry | `app/static/index.html`, `app/static/console.html`, `app/static/api_debug.html` | T-001 contract decision | senior-development | serial | static reference scan |
| T-003 | Review and validate integrated diff | Change records and target files | T-001,T-002 | quality-acceptance | serial | CI-equivalent checks |

## Parallelization decision

- Execution mode: serial
- Why: backend and frontend share the same removed API contract and both modify high-churn files; serial work avoids stale references and makes route/UI verification deterministic.
- Shared interfaces frozen at: architecture review approval.
- Conflict integration owner: main-agent.

## Definition of done

All acceptance criteria in the requirements baseline have evidence, no unrelated files are reverted, and the quality review records a pass or conditional pass.
