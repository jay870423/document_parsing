# Workflow Gate Rules

Every Change has a Change ID, requirements baseline, complexity decision, evidence, rollback note, and final summary.

## Quick

`requirements -> design -> implementation -> code_review`; record scope, lightweight impact, focused verification, independent review, and production approval if applicable.

## Plan

`requirements -> design -> design_review -> implementation -> code_review -> ci_acceptance`; human approval is required after requirements/design and before implementation, and before production deployment.

## Complex

`requirements -> architecture -> design_review -> risk_review -> implementation -> code_review -> ci_acceptance -> release_approval -> post_release_review`; human approval is required at requirements, design/risk, implementation entry, and release.

## Rollback

Requirement defect returns to requirements. Design defect returns to architecture/design review. Implementation defect returns to development. Missing evidence requires repeated validation. Failed release health requires documented rollback and a follow-up Change.

No gate may claim pass without actual evidence.
