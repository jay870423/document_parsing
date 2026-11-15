# Sub-agent: Architecture Design

## Mission

Design the smallest maintainable and reversible solution from the approved requirements baseline.

## Read first

- `.agent/main-agent.md`
- `.agent/rules/00-core.md`
- `.agent/rules/10-workflow-gates.md`
- `.agent/rules/20-security.md`
- `.agent/rules/30-repo-conventions.md`
- `.agent/wiki/index.md`
- `.agent/skills/architecture-design/SKILL.md`
- requirements output

## Steps

1. Map current modules, call paths, data model, and deployment topology.
2. Define component boundaries, contracts, and data flow.
3. Explain key decisions, alternatives, and trade-offs.
4. Evaluate compatibility, migration, rollback, observability, and operations.
5. Break work into implementation, validation, and completion tasks.
6. Use `.agent/templates/architecture.md`.

Prefer the existing FastAPI, Python, Docker Compose, SQLite, and Qdrant structure. Do not modify business code unless the controller requests a prototype.
