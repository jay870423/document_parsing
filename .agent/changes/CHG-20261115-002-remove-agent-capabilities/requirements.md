# Requirements Baseline: Remove AG-UI, A2UI, and WebMCP

- Date: 2026-11-15
- Requester: user
- Status: confirmed by current request; awaiting plan-level implementation approval

## Goal

Remove the project's AG-UI, A2UI, and WebMCP capabilities so users no longer see or invoke the three protocol features.

## User scenarios

- A signed-in user uses the document-processing console and sees no AG-UI, A2UI, or WebMCP cards, tabs, labels, or debug links.
- A client attempting to call the removed agent capability endpoints receives a normal not-found response rather than protocol data.
- Existing document parsing, knowledge-base management, RAG search/answer, authentication, billing, and ordinary API debugging continue to work.

## Scope

- Remove the protocol capability panel and protocol workbench from `app/static/index.html`.
- Remove the protocol-specific debug page entry and file `app/static/api_debug.html`.
- Remove `/api/agent/capabilities` and `/api/agent/run` from `app/main.py`.
- Remove protocol-only request models, helpers, imports, and `app/agent_protocols.py` when no remaining caller exists.
- Remove stale references from `console.html`, documentation, and frontend JavaScript associated only with these capabilities.

## Non-goals

- Do not remove the underlying `/api/v1/parse` document parsing endpoint.
- Do not remove knowledge-base search, RAG answer, authentication, subscription, API-key, or billing behavior.
- Do not alter database schemas or delete user documents, vectors, artifacts, or runtime configuration.
- Do not deploy to production as part of this Change.

## Assumptions and dependencies

- “去除功能能力” means complete runtime and UI removal, not merely hiding the three cards.
- Existing uncommitted user changes must remain untouched except where the target files are intentionally edited.
- The normal `/api/debug` page is retained if it exercises ordinary document parsing rather than the removed protocol workbench.

## Acceptance criteria

- [x] Normal path: no user-facing page contains AG-UI, A2UI, or WebMCP capability entries or links.
- [x] Boundary path: `/api/agent/capabilities` and `/api/agent/run` are no longer registered by the FastAPI app.
- [x] Failure path: stale protocol URLs return 404 and do not expose protocol payloads.
- [x] Compatibility/data: document parsing, RAG, auth, billing, and existing data paths remain intact; no schema/data migration occurs.
- [x] Security/permissions: removal does not broaden permissions or expose new endpoints.
- [x] Operations/release: Python compilation, focused static-reference checks, `git diff --check`, and CI checks pass.

## Risks and open questions

- Existing external clients may depend on `/api/agent/*`; this is an intentional breaking change and should be called out in release notes if released.
- The repository contains pre-existing uncommitted changes; verification must distinguish this Change from unrelated diffs.

## Definition of done

The three capabilities are absent from UI, frontend calls, backend routes, and protocol-only implementation files; preserved product flows compile and focused checks provide evidence.
