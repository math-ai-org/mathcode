# Security & Privacy Standards

## Threat model (lite)
Primary risks:
- Leaking API keys or tokens into logs.
- Command injection through user-provided compile commands.
- Processing sensitive proprietary problem sets.

## Secrets handling
- Read only explicit API key sources from config (env var names and the matching assignment in `~/.zshrc`).
- Do not print environment variables.
- Do not echo API keys.
- If external tools print secrets, redact known patterns before writing logs (future enhancement).

## Dependency hygiene
- Keep dependencies minimal.
- Pin tooling versions where reasonable (optional lockfiles).

## Input validation
- Validate JSON types and required fields.
- Treat all input text as untrusted.

## Least privilege
- Default generator API key source: configured variable name (`PRINCIPIA_KEY`) from env, with `~/.zshrc` fallback.
- Default compilation: run with argument array (no shell) to avoid injection.
- If a shell mode is added, require explicit opt-in and document the risks.

## Privacy
- Assume `problem` text may contain sensitive content.
- Logs are stored locally; document how to disable or relocate logs if needed (future enhancement).
