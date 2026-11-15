# Change Memory: CHG-20261115-001-harness-workflow

## What worked

A three-level workflow makes the simple path fast while preserving stronger gates for data, security, architecture, and release risk.

## What caused rework

The initial Harness content was vulnerable to Windows command encoding issues, so stable ASCII documentation was selected for cross-editor reliability.

## Reusable decision or rule

When complexity is uncertain, choose the higher workflow level. Every Change must retain its own summary, evidence, approvals, rollback, and lessons.

## Proposed Harness improvement

- Target: workflow validation test
- Evidence: current path and YAML checks are manual commands
- Owner: main-agent
- Status: proposed

## Follow-up Change

Add automated Harness consistency checks after the first real usage cycle.
