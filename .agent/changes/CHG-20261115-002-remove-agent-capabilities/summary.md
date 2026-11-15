# Change Summary: CHG-20261115-002-remove-agent-capabilities

- Title: Remove AG-UI, A2UI, and WebMCP capabilities
- Complexity: plan
- Status: closed
- Owner: main-agent
- Created: 2026-11-15
- Request: current user request with UI screenshot

## Goal

Remove the three protocol capabilities from the product UI and runtime surface.

## Scope and non-goals

See `requirements.md` and `architecture.md`. The change preserves ordinary document parsing, RAG, auth, billing, and existing data.

## Decision log

| Time | Decision | Reason | Evidence |
|---|---|---|---|
| 2026-11-15 | Classify as `plan` | Multi-file UI/API behavior change with intentional route removal | `.agent/main-agent.md` |
| 2026-11-15 | Prefer full boundary removal | Hiding UI would leave capabilities active | `architecture.md` |

## Gate status

| Gate | Status | Approval required | Evidence |
|---|---|---|---|
| Requirements | passed | no | `requirements.md` |
| Design | passed | yes, satisfied | `architecture.md`, `review.md` |
| Implementation | passed | yes, satisfied | approval record below |
| Code review | passed | no | `review.md` |
| CI acceptance | passed | no | `ci.md` |

## Approvals

Approval: implementation entry
Decision: pending
By: pending user confirmation
At: pending
Conditions: none recorded
Evidence: user message "批准实施"

## Changed files

`app/main.py`, `app/static/index.html`, and `app/static/console.html` updated; `app/agent_protocols.py`, `app/static/api_debug.html`, and `tmp_api_debug.js` removed. Existing unrelated working-tree changes were preserved.

## Tests, CI, and release evidence

Focused validation completed; evidence is recorded in `ci.md`.

## Rollback

Restore the removed routes/module/static page and frontend blocks from the pre-change revision. No data rollback is required.

## Residual risks

External clients using the removed agent routes will receive 404 after release.

## Final outcome

Pending implementation approval.
