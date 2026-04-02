# Testing Standards

## Test levels
- Unit tests (required):
  - Deterministic naming / sanitization
  - Prompt composition
  - JSON validation
  - Compile command formatting (`{file}` substitution)
- Integration tests (optional):
  - End-to-end run using a mocked model API and a mocked compiler command.

## Frameworks
- Use `pytest`.
- Prefer pure functions for testability.
- Avoid flaky tests; do not depend on network.

## Coverage guidance
- Cover all branches in sanitization and validation.
- Cover failure modes for subprocess invocation (non-zero exit code, missing output).

## Golden files
- If using fixtures, keep them small and place in `examples/` or `tests/fixtures/`.
