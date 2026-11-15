# Change Memory: CHG-20261115-002-remove-agent-capabilities

## What worked

Boundary-first removal eliminated the UI, routes, static debug surface, and protocol-only implementation together. Route inspection plus reference scanning caught stale entry points before closeout.

## What caused rework

PowerShell text-editing commands initially encoded literal newline markers, causing temporary Python syntax errors. The issue was caught by compilation and corrected before acceptance.

## Reusable decision or rule

For feature removals, verify both positive preservation (retained routes still exist) and negative removal (deleted routes and references are absent). Prefer focused route and static scans before broader tests.

## Proposed Harness improvement

- Target: add a repository helper for UTF-8-safe patching on Windows.
- Evidence: multiple shell-to-patch encoding failures during this Change.
- Owner: main-agent
- Status: proposed

## Follow-up Change

None required for this scope; production release remains a separate approval-gated action.
