# Autolean — OpenRouter ↔ Lean Formalization Loop

> Bundled inside MathCode: when using AUTOLEAN through this repository, run `bash scripts/setup-local.sh` from the MathCode repo root. That setup script creates `AUTOLEAN/.venv`, bootstraps the bundled `lean-workspace/`, and installs a project-local Lean toolchain only if `lean` / `lake` are missing.

Autolean is a command-line tool that converts a directory of JSON-encoded math problems into Lean 4 files using the OpenRouter API, then compiles, repairs, and evaluates semantic fidelity against the original problem. It is designed for repeatability, logging, and easy integration with an existing Lean + Mathlib environment.

By default model calls use OpenRouter API. You can switch model calls to local `codex exec`
with `--use-codex-exec` while keeping the rest of the pipeline unchanged.

## What it does
For each `*.json` file in the input directory:
1. Validates required fields (`uuid`, `problem`).
   For multipart chains named like `99_1.json`, `99_2.json`, `99_3.json`, Autolean processes them in order and passes prior sub-question context forward.
2. Builds a prompt that embeds the full JSON (authoritative).
3. Runs phase 5.2 (Thinking) only on iteration 1: derive proof idea and likely lemma calls.
4. Runs phase 5.3 (Codex implementation): writes Lean using the phase 5.2 notes.
   By default this is **formalization-only mode** (statement + placeholder `by sorry`, not a full solved proof).
   Autolean rejects trivialized main theorem statements like `: True` or `: False`.
5. Compiles the Lean file (configurable command).
6. If compilation fails, retries phase 5.3 with compiler feedback up to `--max-iters` (reusing iteration-1 phase 5.2 notes).
7. If compilation succeeds, runs a semantic evaluator that grades alignment with the original problem (`A` to `D`, default evaluator: `openai/gpt-5.2` at `xhigh`).
   If evaluator output is malformed, Autolean records the parse reason and automatically re-prompts evaluation.
   Autolean enforces a minimum grade threshold by default (`B`); lower grades are rejected and retried.
8. Writes logs for every iteration.

For multipart chains:
- By default, part `k+1` does not start until part `k` is accepted.
- By default, each part in a multipart chain must reach grade `A` before the next part starts (`--multipart-min-eval-grade A`).
- You can disable chain blocking and continue later parts even if an earlier part fails with `--no-multipart-block-on-failure`.
- You can bypass eval-grade gating during resume/skip checks with `--skip-compiled-ignore-eval-grade` so any currently compiling sub-question is skipped.
- You can enable fast resume with `--autopass-eval-a` to skip compile-check for files that already have latest eval grade `A`.
- You can also enable fast resume with `--autopass-has-eval` to skip compile-check for files that already have any eval artifact, regardless of grade.
- Later parts receive earlier sub-question JSON and earlier Lean formalizations as prerequisite context.
- If a chain has index gaps (e.g., `99_1.json`, `99_3.json` without `99_2.json`), that main question is marked failed and skipped.
- If part `k` fails, parts `k+1...` are skipped, and Autolean continues with the next main question.

## Key features
- Deterministic file naming from input filenames (with sanitization and CJK→pinyin transliteration).
- Strict model output contract: JSON object with a `lean` string.
- One-time planning layer (`5.2 thinking` at iteration 1) then coding-only repairs (`5.3`).
- Multipart chain mode (`*_1`, `*_2`, ...): sequential dependency, prior-part context injection, and prerequisite theorem reuse.
- Statement-only formalization by default (`--formalization-only`), with optional full-proof mode (`--no-formalization-only`).
- Hard anti-trivialization guard for main theorem statement (`: True` / `: False` is rejected).
- Compile-check-repair loop with configurable iterations.
- Post-compile semantic distance grading (`A`–`D`) against the original JSON problem.
- Optional policy to reject `sorry`.
- Resume/skipping when outputs already compile (override with `--force`).
- Optional parallel workers.
- Detailed per-iteration logs and metadata.

## Requirements
- Python 3.10+.
- OpenRouter API key in variable `PRINCIPIA_KEY` (default). The runner first checks env, then falls back to `PRINCIPIA_KEY=...` in `~/.zshrc`.
- Lean 4 + Lake available on PATH **in the environment used for compilation**.
- Mathlib installed in the Lean project used for compilation (the generated Lean always uses `import Mathlib`).

### Reference Lean environment (recommended)
If you want the same environment used by the `leancheck` project on this machine, point `--cwd` to:
`/Users/jcfeng/Documents/Lean/leancheck`

That project is configured with:
- Lean toolchain: `leanprover/lean4:v4.28.0-rc1`.
- Direct dependency: `mathlib` at `v4.28.0-rc1`.
- Transitive packages available via the manifest:
  `plausible`, `LeanSearchClient`, `importGraph`, `proofwidgets`, `aesop`,
  `Qq` (quote4), `batteries`, `Cli` (lean4-cli).

If you compile outside of `leancheck`, you need at least Mathlib; the other packages are only required
if the generated Lean code imports or depends on them.

## Install
```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

## Quick start
```bash
export PRINCIPIA_KEY="your_api_key"
autolean run --input problems --output Formalizations
```

## Split Multi-Part Problems
If a JSON file contains multiple sub-problems in `problem` (for example `Chap4/14.json` with 4 parts),
you can split it into `14_1.json`, `14_2.json`, `14_3.json`, `14_4.json`:

```bash
python scripts/split_subproblems.py --input-dir Chap4
```

By default, after successful split the original multi-part source file is deleted.
Use `--no-delete-original` if you want to keep the source file.

Useful options:
```bash
# Preview without writing files
python scripts/split_subproblems.py --input-dir Chap4 --dry-run

# Overwrite existing split files
python scripts/split_subproblems.py --input-dir Chap4 --force

# Write split files into a separate directory
python scripts/split_subproblems.py --input-dir Chap4 --output-dir Chap4_split

# Keep original multi-part files
python scripts/split_subproblems.py --input-dir Chap4 --no-delete-original
```

## Batch Lean Compile Check
Use this to compile-check all formalizations for a chapter folder and print only failed files.

```bash
python scripts/check_lean_formalizations.py \
  --input-dir Chap5 \
  --formalizations-dir Chap5_Fin \
  --cwd /Users/jcfeng/Documents/Lean/leancheck \
  --compile-cmd "lake env lean {file}" \
  --workers 4
```

Notes:
- If `--input-dir` contains `*.json`, expected formalizations are `problem_<json_stem>.lean`.
- If `--formalizations-dir` is omitted, the checker auto-uses `<input-dir>_Fin` when present.
- Exit code is `1` when any compilation fails (or when expected files are missing, unless `--no-strict-missing` is set).

## Batch Semantic Evaluation (Same Prompt as `autolean run`)
Use this to evaluate existing formalizations with the exact same semantic-eval prompt and parse/retry logic used by `autolean run`.

Codex Exec (default provider, gpt-5.2 + xhigh):
```bash
python scripts/evaluate_lean_formalizations.py \
  --input-dir Book2_Chap2 \
  --formalizations-dir Book2_Chap2_Fin \
  --out-dir Book2_Chap2_Fin/eval_out \
  --provider codex-exec \
  --eval-model openai/gpt-5.2 \
  --reasoning-effort xhigh \
  --workers 10 \
  --api-key-name AUTOLEAN
```

OpenRouter API:
```bash
python scripts/evaluate_lean_formalizations.py \
  --input-dir Book2_Chap2 \
  --formalizations-dir Book2_Chap2_Fin \
  --out-dir Book2_Chap2_Fin/eval_out \
  --provider openrouter \
  --eval-model openai/gpt-5.2 \
  --reasoning-effort xhigh \
  --workers 10 \
  --api-key-name PRINCIPIA_KEY
```

Key outputs in `--out-dir`:
- `<theorem>.eval.json` final normalized evaluation payload.
- `<theorem>.eval_attemptN_stdout.log` / `<theorem>.eval_attemptN_stderr.log`.
- `<theorem>.eval_prompt.txt` (exact prompt used).
- `evaluation_summary.json` and `evaluation_report.txt`.

## Strict A+ Recheck For Prior A Formalizations
Use this to run a stricter second-pass exactness audit on formalizations that already received a prior `A`
or double-check `A/A` result. The script uses OpenRouter by default, with `openai/gpt-5.4`,
reasoning effort `xhigh`, and API key env var `PRINCIPIA_KEY`.

```bash
python scripts/evaluate_strict_aplus.py \
  --input-dir Book2_Chap2 \
  --formalizations-dir Book2_Chap2_Fin \
  --workers 10
```

Behavior:
- Searches the original problem JSON under `--input-dir`.
- Matches each JSON file to `problem_<json_stem>.lean` under `--formalizations-dir`, preserving relative subdirectories.
- Only evaluates targets whose latest prior eval is eligible:
  - top-level `status=ok` and `grade=A`, or
  - double-check payload with `both_a_pass=true` and both graders returning `A`.
- Uses a stricter `A+` vs `A` rubric:
  - `A+` means exact match with nothing to change.
  - `A` means still broadly faithful, but with at least one tiny difference that should be edited to become exact.
- Writes outputs under `--formalizations-dir/new_eval` by default.

Key outputs in `new_eval`:
- `<relative path>/<theorem>.eval.json` strict A+/A payload.
- `<relative path>/<theorem>.eval_attemptN_stdout.log` / `<relative path>/<theorem>.eval_attemptN_stderr.log`.
- `<relative path>/<theorem>.eval_prompt.txt`.
- `evaluation_summary.json` and `evaluation_report.txt`.

## Batch Proof Completion From Existing Lean Formalizations
Use this to take an existing corpus of Lean files with placeholder proofs (for example `A_evaled_lean_formalizations`),
launch several independent LLM proof attempts per theorem, compile-check every attempt, and count how many attempts pass.

OpenRouter API:
```bash
python scripts/prove_lean_formalizations.py \
  --input-dir A_evaled_lean_formalizations \
  --out-dir proof_runs \
  --provider openrouter \
  --model openai/gpt-5.2-codex \
  --reasoning-effort xhigh \
  --attempts 4 \
  --max-iters 3 \
  --cwd /Users/jcfeng/Documents/Lean/leancheck \
  --compile-cmd "lake env lean {file}"
```

Codex Exec:
```bash
python scripts/prove_lean_formalizations.py \
  --input-dir A_evaled_lean_formalizations \
  --out-dir proof_runs \
  --provider codex-exec \
  --model openai/gpt-5.2-codex \
  --reasoning-effort xhigh \
  --attempts 4 \
  --max-iters 3 \
  --cwd /Users/jcfeng/Documents/Lean/leancheck \
  --compile-cmd "lake env lean {file}" \
  --codex-exec-sandbox read-only
```

Behavior:
- Searches `--input-dir` recursively for `*.lean`.
- By default only processes files that still contain `sorry` or `admit`.
- Runs `--attempts` independent workers per theorem in parallel; workers do not share compiler feedback with each other.
- Each worker gets its own compile-repair loop up to `--max-iters`.
- Preserves the file outside the main theorem proof body; only the proof body is regenerated, with optional extra imports if needed.
- Rejects outputs that still contain `sorry`/`admit`, that change frozen file content outside the proof body, or that introduce top-level declarations.

Key outputs in `--out-dir`:
- `<relative theorem path without .lean>/attemptK/iterN.candidate.lean` for every generated proof candidate.
- `<relative theorem path without .lean>/attemptK/outcome.json` for each worker.
- `<relative theorem path without .lean>/summary.json` with pass count and one-shot pass count for that theorem.
- `proof_summary.json` and `proof_report.txt` with corpus-level pass and one-shot pass results.

## Batch Proof Completion With Replanning
Use this when you want exactly one theorem-local proving sequence per problem, but still want parallelism across different problems.

Pipeline per theorem:
1. Ask a planner model for a proof plan.
2. Ask a proving model to generate/repair the proof, feeding compiler output back after each failed compile.
3. After `--attempts-before-replan` failed proof attempts, ask the planner for a new plan using the latest failure report.
4. Run another proving block with the new plan.
5. If all plan rounds fail, move on to the next theorem.

Resume behavior:
- Resume is enabled by default.
- If a theorem folder in `--out-dir` already has `summary.json`, that theorem is treated as finished and skipped.
- If a theorem folder has `progress.json`, the script resumes from the next unfinished attempt.
- If a theorem folder has partial planner/prover artifacts but no `progress.json`, the script reconstructs progress from the existing logs/candidates when possible and continues from there.
- Use `--no-resume` to force a fresh rerun from attempt 1 for every theorem.

Default models:
- planner: `openai/gpt-5.4` with `xhigh`
- prover: `google/gemini-3-flash-preview` with `xhigh`

Example:
```bash
python scripts/prove_lean_formalizations_replan.py \
  --input-dir A_evaled_lean_formalizations \
  --out-dir proof_runs_replan \
  --planner-model openai/gpt-5.4 \
  --planner-reasoning-effort xhigh \
  --prover-model google/gemini-3-flash-preview \
  --prover-reasoning-effort xhigh \
  --attempts-before-replan 5 \
  --max-plan-rounds 2 \
  --workers 8 \
  --cwd /Users/jcfeng/Documents/Lean/leancheck \
  --compile-cmd "lake env lean {file}"
```

Fresh rerun instead of resuming:
```bash
python scripts/prove_lean_formalizations_replan.py \
  --input-dir A_evaled_lean_formalizations \
  --out-dir proof_runs_replan \
  --no-resume \
  --cwd /Users/jcfeng/Documents/Lean/leancheck \
  --compile-cmd "lake env lean {file}"
```

Key outputs in `--out-dir`:
- `<relative theorem path without .lean>/plan_roundK.prompt.txt` and corresponding planner stdout/stderr logs.
- `<relative theorem path without .lean>/proof_attemptN.candidate.lean` for every generated proof candidate.
- `<relative theorem path without .lean>/proof_attemptN.compile_stdout.log` / `.compile_stderr.log`.
- `<relative theorem path without .lean>/progress.json` with theorem-level checkpoint state for resume.
- `<relative theorem path without .lean>/summary.json` with theorem-level pass/fail status and attempt history.
- `proof_summary.json` and `proof_report.txt` with corpus-level results.

## Histograms for Worked-out Proof Results
Use this to generate two histograms from an existing proof-run directory:
- histogram of total passing attempts per worked-out problem
- histogram of one-shot passing attempts per worked-out problem

Example:
```bash
python scripts/plot_proof_histograms.py \
  --input-dir A_evaled_lean_formalizations/Book1_Chap1_Solution \
  --out-dir A_evaled_lean_formalizations/Book1_Chap1_Solution/histograms
```

Behavior:
- Searches `--input-dir` recursively for per-problem `summary.json`.
- Uses only worked-out problems, meaning `pass_count > 0`.
- Writes `worked_out_attempts_histogram.svg` and `worked_out_attempts_histogram.png`.
- Writes `one_shot_worked_out_attempts_histogram.svg` and `one_shot_worked_out_attempts_histogram.png`.
- Writes `histogram_data.json` with the exact bucket counts used for both plots.

## Recommended Run (This Repo)
The following command is a good default for this repository layout (`TestProblem` → `TestOut`) and a local `leancheck` environment:

```bash
autolean run \
  --input TestProblem \
  --output TestOut \
  --cwd /Users/jcfeng/Documents/Lean/leancheck \
  --openrouter-thinking-model openai/gpt-5.2 \
  --openrouter-model openai/gpt-5.2-codex \
  --openrouter-thinking-reasoning-effort xhigh \
  --openrouter-coding-reasoning-effort high \
  --max-iters 6 \
  --force \
  --openrouter-web-search \
  --openrouter-web-search-max-results 5 \
  --workers 4
```

Post-compile A–D grading still runs automatically in this command using defaults:
- `--openrouter-eval-model openai/gpt-5.2`
- `--openrouter-eval-reasoning-effort xhigh`
- `--min-eval-grade B`

Common variants:
```bash
# Use 4 workers
autolean run --input problems --output Formalizations --workers 4

# Stream compiler output live into log files
autolean run --input problems --output Formalizations --live-logs

# Disable progress bar
autolean run --input problems --output Formalizations --no-progress

# Provide a custom compile command
autolean run --input problems --output Formalizations --compile-cmd "lake env lean {file}"

# Compile inside an existing Lean + Mathlib project
autolean run --input problems --output Formalizations --cwd /path/to/lean/project

# Use a specific OpenRouter model
autolean run --input problems --output Formalizations --openrouter-model anthropic/claude-3.5-sonnet

# Use different models for thinking and coding
autolean run --input problems --output Formalizations \
  --openrouter-thinking-model openai/gpt-4o-mini \
  --openrouter-model anthropic/claude-3.5-sonnet

# Convenience switch: use Gemini Flash Preview for both 5.2 and 5.3 on OpenRouter
autolean run --input problems --output Formalizations \
  --openrouter-gemini-flash-preview
# In this mode, evaluation is double-checked by both Gemini Flash and GPT-5.2 (xhigh),
# and the iteration passes only when both graders return A.

# Enable OpenRouter web search plugin
autolean run --input problems --output Formalizations \
  --openrouter-web-search \
  --openrouter-web-search-max-results 5

# Use local codex exec instead of OpenRouter API for 5.2/5.3/eval
autolean run --input problems --output Formalizations \
  --use-codex-exec \
  --codex-exec-sandbox read-only

# Choose API key variable name (default PRINCIPIA_KEY)
autolean run --input problems --output Formalizations \
  --api-key-name AUTOLEAN

# Use a specific evaluator model/effort for post-compile A-D grading
autolean run --input problems --output Formalizations \
  --openrouter-eval-model openai/gpt-5.2 \
  --openrouter-eval-reasoning-effort xhigh

# Allow full-proof generation instead of statement-only mode
autolean run --input problems --output Formalizations --no-formalization-only
```

## CLI reference
Run `autolean --help` or `autolean run --help` to see all options. Key flags:
- `--input`: directory with `*.json` files (default: `problems`).
- `--output`: output directory for `*.lean` files (default: `Formalizations`).
- `--logs`: log directory (default: `logs`).
- `--max-iters`: max repair iterations per file (default: 6).
- `--formalization-only` / `--no-formalization-only`: statement-only formalization mode on/off (default: on).
- `--openrouter-model`: OpenRouter model identifier for phase 5.3 coding (default: `openai/gpt-5.2-codex`).
- `--openrouter-thinking-model`: optional model identifier for phase 5.2 thinking (iteration 1 only; default: same as `--openrouter-model`).
- `--openrouter-gemini-flash-preview` / `--no-openrouter-gemini-flash-preview`: convenience switch that sets both phase 5.2 and phase 5.3 OpenRouter models to `google/gemini-3-flash-preview` (default: off). When enabled on OpenRouter path, evaluation is double-checked by Gemini Flash and `openai/gpt-5.2` at `xhigh`, and pass requires grade `A` from both.
- `--openrouter-thinking-reasoning-effort`: reasoning effort for phase 5.2 (default: `xhigh`).
- `--openrouter-coding-reasoning-effort`: reasoning effort for phase 5.3 (default: `xhigh`).
- `--openrouter-eval-model`: OpenRouter model identifier for post-compile semantic evaluation (default: `openai/gpt-5.2`).
- `--openrouter-eval-reasoning-effort`: reasoning effort for post-compile semantic evaluation (default: `xhigh`).
- `--openrouter-eval-repair-retries`: retries when evaluator output is malformed/unparseable (default: `2`).
- `--min-eval-grade`: minimum acceptable evaluation grade (default: `B`; use `none` to disable).
- `--multipart-min-eval-grade`: minimum required grade per part in multipart chains before continuing to the next part (default: `A`; use `none` to disable).
- `--multipart-block-on-failure` / `--no-multipart-block-on-failure`: block/continue later sub-questions in a multipart chain after an earlier part fails (default: block).
- `--eval-grade-from-output-dir` / `--no-eval-grade-from-output-dir`: during resume/skip checks, read latest evaluation grades from `--output` instead of `--logs` (default: `--logs`).
- `--skip-compiled-ignore-eval-grade` / `--no-skip-compiled-ignore-eval-grade`: during resume/skip checks, treat a currently compiling `.lean` as skippable even when multipart eval-grade gates are configured (default: off).
- `--autopass-eval-a` / `--no-autopass-eval-a`: during resume/skip checks, if latest eval grade is already `A`, skip without running compile-check (default: off).
- `--autopass-has-eval` / `--no-autopass-has-eval`: during resume/skip checks, if any eval artifact already exists, skip without running compile-check, regardless of grade (default: off).
- `--openrouter-base-url`: OpenRouter API base URL (default: `https://openrouter.ai/api/v1`).
- `--openrouter-api-key-env`: variable name for API key lookup (default: `PRINCIPIA_KEY`; also checks `~/.zshrc`).
- `--api-key-name`: convenience selector for common key names (`PRINCIPIA_KEY` or `AUTOLEAN`); overrides `--openrouter-api-key-env` when provided.
- `--openrouter-timeout-s`: OpenRouter request timeout in seconds (default: 180).
- `--openrouter-max-retries`: retries for transient OpenRouter transport/HTTP failures (default: 2).
- `--openrouter-web-search` / `--no-openrouter-web-search`: enable/disable OpenRouter web search plugin for model calls (default: disabled).
- `--openrouter-web-search-engine`: optional search engine name passed to OpenRouter web plugin.
- `--openrouter-web-search-max-results`: optional max result count passed to OpenRouter web plugin.
- `--use-codex-exec` / `--no-use-codex-exec`: use local `codex exec` for model calls (5.2/5.3/eval) instead of OpenRouter API (default: OpenRouter API).
- `--codex-exec-sandbox`: sandbox passed to `codex exec` when enabled (`read-only`, `workspace-write`, `danger-full-access`; default: `read-only`).
  When `--use-codex-exec` is enabled, phase 5.3 coding is forced to `gpt-5.3-codex-spark` with `xhigh` reasoning effort; if that model is unavailable, Autolean retries with `gpt-5.3-codex`.
  OpenRouter-style `openai/...` model IDs are normalized before `codex exec` calls (for example `openai/gpt-5.2-codex` -> `gpt-5.2-codex`).
- `--compile-cmd`: compile command template; must include `{file}`.
- `--cwd`: working directory where compilation runs.
- `--require-no-sorry`: reject outputs that contain `sorry` and retry.
  Incompatible with default `--formalization-only`; use `--no-formalization-only` if you require no `sorry`.
- `--workers`: parallel workers (1 disables parallelism).
- `--live-logs`: stream compiler subprocess output to log files while running.
- `--force`: re-run even if a prior output already compiles.

Exit codes:
- `0` if all problems succeeded.
- `1` if any problem failed.

## Input JSON schema
Required fields:
- `uuid`: string, kept in the prompt as authoritative metadata (not used for naming).
- `problem`: array of strings; lines are concatenated in order.

Other fields are allowed and preserved in the prompt but ignored by the parser.

Example:
```json
{
  "uuid": "chapter1/problem-1",
  "problem": [
    "Prove that A ∪ B = B ∪ A.",
    "Assume A, B are sets."
  ],
  "solution": ["ignored"],
  "remark": []
}
```

## Output naming and sanitization
- Output theorem name: `problem_<sanitized filename>`.
- Output file path: `<output_dir>/problem_<sanitized filename>.lean` (uses the JSON filename stem).
- Sanitization replaces spaces, slashes, and punctuation with underscores, collapses repeats, and transliterates Chinese characters to pinyin (via `pypinyin`).

## Compile environment
The generated Lean files always start with:
```lean
import Mathlib
namespace Formalizations
```
So the compile environment must have Mathlib configured. Use:
- `--cwd` to point at a Lean project that includes Mathlib, or
- `--compile-cmd` to target your own compile wrapper.

The compile command is split via `shlex` and run without a shell to avoid injection risks.

## Logs and reproducibility
Per problem and iteration, Autolean writes:
- Phase 5.2 thinking stdout/stderr logs (full model output on iteration 1; skipped marker on later iterations).
- Phase 5.3 coding stdout/stderr logs.
- Compiler stdout/stderr logs.
- Evaluation stdout/stderr logs and parsed `A`–`D` payload JSON (on compile-success iterations).
- A metadata JSON file with prompt hash and return codes.

Logs live under the directory passed to `--logs` (default: `logs/`).

## Resume behavior
If an output `.lean` already exists and compiles cleanly (and passes the `--require-no-sorry` policy, if enabled), Autolean skips that problem. Use `--force` to re-run.
You can also skip without compile-check by enabling `--autopass-eval-a` when prior eval is already `A`.
Or skip without compile-check by enabling `--autopass-has-eval` when any prior eval artifact exists.

## Troubleshooting
- Missing API key: set `PRINCIPIA_KEY` (or your chosen `--openrouter-api-key-env`) in env or `~/.zshrc`.
- `codex: command not found` when using `--use-codex-exec`: install Codex CLI and ensure `codex` is on PATH.
- OpenRouter HTTP errors in phase 5.2: inspect `logs/*.thinking_stderr.log` and `logs/*.thinking_stdout.log`.
- OpenRouter HTTP errors in phase 5.3: inspect `logs/*.coding_stderr.log` and `logs/*.coding_stdout.log`.
- OpenRouter HTTP/parse errors in evaluation: inspect `logs/*.eval_stderr.log`, `logs/*.eval_stdout.log`, and `logs/*.eval.json`.
- Compiler fails with missing Mathlib: point `--cwd` to a project with Mathlib configured.
- Non-compiling outputs: inspect the corresponding `logs/*.compile_stderr.log` for details.
- Unicode path issues: use ASCII-friendly filenames or sanitize filenames upstream.

## Development
Run tests:
```bash
pytest -q
```

Lint/format:
```bash
ruff check .
ruff format .
```

## Project layout
- `src/autolean/`: core library + CLI.
- `tests/`: unit tests for sanitization and prompt construction.
- `specs/`: requirements and engineering standards.
- `examples/`: sample input JSON and documentation.

## Notes
- This project does not ship a Lean environment; you must provide one.
- Generated outputs and logs are typically not committed.
