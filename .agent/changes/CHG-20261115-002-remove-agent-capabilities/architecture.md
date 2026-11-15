# Architecture: Remove AG-UI, A2UI, and WebMCP

- Date: 2026-11-15
- Requirement: `.agent/changes/CHG-20261115-002-remove-agent-capabilities/requirements.md`
- Status: reviewed draft; awaiting plan-level approval

## Current state and change boundary

The capability is implemented as a cross-layer feature: `app/main.py` imports protocol builders from `app/agent_protocols.py`, exposes `/api/agent/capabilities` and `/api/agent/run`, and serves `/api/debug-pro` backed by `app/static/api_debug.html`. `app/static/index.html` renders the protocol panel/workbench and calls those endpoints. `app/static/console.html` links to the protocol debug page.

The underlying parse and RAG endpoints are separate and remain in service.

## Solution overview

Remove the feature at its boundary rather than leaving disabled protocol responses:

1. Delete the protocol workbench UI and all protocol-only JavaScript/CSS from the main page.
2. Remove protocol debug-page routing and the static debug page.
3. Remove agent route handlers, their request model, and protocol-only imports/helpers.
4. Delete `app/agent_protocols.py` after verifying there are no remaining callers.
5. Remove stale links and references from the console and repository docs.

## Components and data flow

After the change, the browser routes directly among document processing, knowledge-base management, RAG search/answer, and retained ordinary API debugging. No request flows through an agent/protocol adapter. Existing parse/RAG backend flows are unchanged.

## Interfaces, state, and data changes

- Removed HTTP interfaces: `GET /api/agent/capabilities`, `POST /api/agent/run`, and `GET /api/debug-pro`.
- Removed frontend state: protocol overview, WebMCP tool list, agent task state, generated surfaces, and event rendering.
- No database, artifact, vector, or configuration migration.

## Key decisions and alternatives

- Recommended: full boundary removal. This prevents hidden protocol behavior and avoids maintaining dead endpoints.
- Alternative rejected: hide only the cards. It leaves public routes and stale client contracts active.
- Alternative rejected: return deprecation payloads. The request is removal, and retaining protocol semantics would violate the scope.

## Compatibility, migration, and rollback

This is a deliberate breaking API change for clients using `/api/agent/*` or `/api/debug-pro`; those routes will return 404 after deployment. Rollback is file-level restoration of the removed routes/module/static page and frontend blocks. No data rollback is needed.

## Security, observability, and operations

Removing routes reduces exposed surface area. No credentials or secrets are touched. Verification must check route registration, static references, Python compilation, whitespace, and the repository CI workflow. Production release remains outside this Change.

## Implementation breakdown

- Backend: remove routes, model, helpers, imports, and protocol module.
- Frontend: remove protocol panel/workbench, calls, styles, and links.
- Validation: compile, static reference scan, route smoke check, diff check, and CI-equivalent checks.

## Validation strategy

Use focused repository searches to confirm no active AG-UI/A2UI/WebMCP references remain outside Change records/history, compile `app/main.py`, inspect FastAPI route list for removed endpoints, and run the project CI checks defined in `.github/workflows/ci.yml`.
