# Harness Engineering System

The controller is `.agent/main-agent.md`.

Every request is a Change. The controller classifies it as `quick`, `plan`, or `complex`, creates a durable Change record, routes the four roles, enforces human approval points, records CI/release evidence, and performs a self-evolution review.

See `.agent/config.yaml` for machine-readable workflow definitions.
