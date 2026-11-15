# Skill: Senior Development

Turn the design into the smallest maintainable patch. Inspect current implementations and callers before changing the root cause.

Before coding, decompose the approved design into atomic tasks with file scope, dependencies, owner, validation, and execution mode. Use parallel Agents only for disjoint scopes with fixed contracts and independent validation; use serial execution for shared files, schemas, contracts, migrations, security, or dependency chains.

Check existing workspace changes, validation, errors, credentials, injection, traversal, authorization, data compatibility, and rollback. Add focused tests or reproducible checks. Report commands and real results.
