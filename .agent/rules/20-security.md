# Security Rules

- Read credentials only from environment variables, secret managers, or authorized secure input. Never write them to code, docs, logs, or commits.
- Validate external input length, type, format, permissions, and resource limits.
- Use parameterized database operations. Protect file paths, URLs, commands, and templates from injection and traversal.
- Treat authentication, authorization, payment, SMS, API keys, uploads, and admin endpoints as high risk by default.
- Before production actions, confirm target, backup, health checks, rollback, and least privilege.
- Do not log passwords, tokens, cookies, or complete personal data.
- New dependencies require source, purpose, version constraints, and supply-chain review.
