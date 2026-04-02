# Engineering Standards

## Repository structure
- Use `src/` layout for Python (`src/autolean/...`).
- Keep CLI entrypoints thin; core logic should live in `autolean/core.py`.
- Keep prompts/templates in `autolean/prompting.py`.

## Code style
- Python:
  - Target Python 3.10+.
  - Use type hints for public functions.
  - Prefer `pathlib.Path` over string paths.
  - Avoid global mutable state.
- Naming:
  - Deterministic naming is required for outputs derived from input filenames (or another stable naming policy).
  - Centralize sanitization in one function and test it.

## Error handling
- Fail fast on invalid JSON schema.
- For generator API and compiler execution:
  - Capture generator responses and compiler stdout/stderr.
  - Return structured results (exit code + outputs).
  - Never throw away compiler diagnostics.

## Logging
- Write per-problem logs under `logs/`.
- Logs must not contain secrets; do not print environment variables.
- Include iteration counters and timestamps.

## Documentation
- Keep README.md and specs consistent with behavior.
- Any ambiguity resolved in implementation must be recorded in specs/requirements.md (Assumptions).

## Version control hygiene
- Do not commit generated docs under `docs/generated/`.
- Keep commits small and message them clearly (even if the user commits manually).
