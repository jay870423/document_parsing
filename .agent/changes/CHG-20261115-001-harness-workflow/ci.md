# CI Acceptance: CHG-20261115-001-harness-workflow

- Environment: GitHub Actions, Ubuntu latest, Python 3.11
- Result: defined and locally pre-validated

## Jobs

| Job | Result | Evidence |
|---|---|---|
| Python compilation | defined | `.github/workflows/ci.yml` |
| Whitespace check | defined | `.github/workflows/ci.yml` |
| Harness YAML/path validation | defined | `.github/workflows/ci.yml` |
| Repository tests when present | defined | `.github/workflows/ci.yml` |

## Note

Hosted CI execution requires the repository to be connected to GitHub Actions. No production credentials or production data are used.
