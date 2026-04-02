# Requirements — Autolean (OpenRouter ↔ Lean loop)

## 1. Overview
Autolean is a command-line tool intended to automate an iterative workflow:
- Input: a directory of JSON files, each representing a natural-language math problem (often containing LaTeX).
- Output: Lean 4 source files that (ideally) compile under a user-provided Lean environment.
- Verification: the Lean compiler is the oracle; failures produce diagnostics that are fed back to the model for repair attempts.

## 2. Goals
- Provide a repeatable pipeline from JSON problem statements to Lean 4 files.
- Automate a compile-check-repair loop using OpenRouter API and Lean compiler output.
- Preserve logs for debugging, reproducibility, and later evaluation.

## 3. Non-Goals / Out of Scope
- Guaranteeing that every problem can be fully proven in Lean.
- Building a full Lean+Mathlib project from scratch (users may supply an existing project via `--cwd`).
- Advanced lemma retrieval/search over Mathlib (future improvement).

## 4. Assumptions
- Users have installed and configured:
  - OpenRouter API key (`PRINCIPIA_KEY` by default, via env or `~/.zshrc`) and model access.
  - Lean 4 + Lake and any needed libraries (e.g., Mathlib) in the chosen compile environment.
- The JSON schema is broadly consistent with the provided example (fields like `uuid` and `problem`).
- Unicode filenames/identifiers are acceptable in the user’s environment; Chinese characters are transliterated to pinyin for naming.

## 5. Functional Requirements
**FR-1**: The system shall ingest all `*.json` files from an input directory.

**FR-2**: The system shall validate the presence of required fields:
- `uuid` (string)
- `problem` (array of strings; concatenated in order)

**FR-3**: The system shall run phase 5.2 thinking only on iteration 1 to derive proof idea and candidate lemmas, then run phase 5.3 coding to generate Lean 4 output under a strict JSON contract (`{"lean": ...}`).

**FR-4**: The system shall run a compile check using a configurable compile command (default `lake env lean {file}`), and treat non-zero exit code as failure.

**FR-5**: On compile failure, the system shall feed the exact compiler output back into the next phase 5.3 repair iteration and retry up to `--max-iters` while reusing the iteration-1 phase 5.2 planning notes.

**FR-6**: The system shall optionally enforce a policy that forbids `sorry` (`--require-no-sorry`).

**FR-7**: The system shall provide a CLI interface `autolean run ...` with flags to configure:
- input/output directories
- max iterations
- OpenRouter model and API settings (including optional separate thinking model)
- compile command and working directory

**FR-8**: When a generated Lean file compiles successfully, the system shall run a post-compile semantic evaluation against the authoritative JSON problem and assign an `A`–`D` fidelity grade.

**FR-9**: For multipart inputs split as `name_1.json`, `name_2.json`, ..., the system shall process parts sequentially, provide prior part JSON/formalization context to later parts, and gate progression so part `k+1` starts only after part `k` is accepted under the configured multipart minimum grade.

**FR-10**: If a multipart chain has missing indices (gaps), the system shall mark that main question as failed and skip the chain. If part `k` fails, remaining parts `k+1...` of that chain shall be skipped, and processing shall continue with the next main question.

## 6. Non-Functional Requirements
**NFR-1 Reliability**: Runs must be resumable and produce deterministic outputs for the same inputs and settings (subject to model nondeterminism). Failures must not corrupt existing outputs.

**NFR-2 Observability**: The tool must log (per problem and per iteration):
- Thinking and coding prompt metadata (including iteration number)
- Thinking and coding stdout/stderr
- Compiler stdout/stderr and return codes
- Post-compile evaluation stdout/stderr and parsed evaluation payload (on compile-success iterations)

**NFR-3 Safety**: Secrets (API keys) must never be written to logs. The tool must avoid shell injection risks by default (use argument arrays; require explicit opt-in for shell execution if ever added).

**NFR-4 Performance**: The tool should handle hundreds of problems without excessive memory usage; optional parallelism may be added later.
Optional parallelism is supported via a bounded worker pool (`--workers`).

## 7. Interfaces (CLI/UI/API)
### CLI
Primary command:
- `autolean run --input <dir> --output <dir> [options]`

Key options (initial):
- `--max-iters N`
- `--compile-cmd "..."` (supports `{file}` placeholder)
- `--cwd <path>` (where compilation is executed)
- `--openrouter-model <name>`
- `--openrouter-thinking-model <name>` (optional; iteration 1 only)
- `--openrouter-thinking-reasoning-effort <level>`
- `--openrouter-coding-reasoning-effort <level>`
- `--openrouter-eval-model <name>` (post-compile evaluator)
- `--openrouter-eval-reasoning-effort <level>`
- `--openrouter-base-url <url>`
- `--openrouter-api-key-env <env_var>`
- `--openrouter-timeout-s <seconds>`
- `--openrouter-max-retries <count>`
- `--openrouter-web-search/--no-openrouter-web-search`
- `--openrouter-web-search-engine <name>` (optional)
- `--openrouter-web-search-max-results <count>` (optional)
- `--require-no-sorry`
- `--workers N` (optional parallelism)
- `--live-logs/--no-live-logs` (stream compiler output to log files)
- `--progress/--no-progress` (progress bar)

No GUI is required.

## 8. Data Model
### Input JSON (minimum)
- `uuid: string` — identifier provided by the source data (may contain slashes/Unicode; not necessarily unique)
- `problem: string[]` — natural language (may include LaTeX)

Other fields may exist and should be ignored by default (`solution`, `remark`, `reference`, `figures`, etc.).

### Output artifacts
- Lean file per problem: `Formalizations/<theorem_name>.lean`
- Logs per problem and iteration under `logs/`

## 9. Acceptance Criteria
**AC-1**: Given an input directory with at least one valid JSON file, running `autolean run ...` produces a Lean file in the output directory and writes logs.

**AC-2**: If compilation fails, the tool retries and records each attempt; it stops after `--max-iters`.

**AC-3**: With `--require-no-sorry`, outputs containing `sorry` trigger a retry (up to max iterations) and are logged as policy failures.

**AC-4**: The CLI help text documents all required and major optional flags.

## 10. Test Strategy
- Unit tests:
  - UUID sanitization / deterministic naming
  - Prompt construction includes the authoritative JSON
  - JSON validation (missing fields, wrong types)
- Smoke test (optional/manual):
  - Run against a tiny JSON example with a known Lean environment.

## 11. Risks & Mitigations
- **Model nondeterminism**: outputs may vary; mitigate with strong prompts, optional temperature control (if available), and caching/resume.
- **Environment drift** (Lean/Mathlib versions): compilation may fail depending on user setup; mitigate with explicit `--cwd` and documentation.
- **Unicode path issues**: some shells/OS may break; mitigate with an ASCII filename policy option.
- **Secret leakage in logs**: ensure logs do not include environment variables; never print tokens.

## 12. Glossary
- **OpenRouter API**: API gateway used to call supported language models through `/chat/completions`.
- **Lean 4**: The theorem prover and programming language used to formalize mathematics.
- **Lake**: Lean’s build tool/package manager.
- **Ralph**: An autonomous agent runner that uses `PROMPT.md` and `@fix_plan.md` to drive work.
