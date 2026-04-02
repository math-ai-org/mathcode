# Math Code

```
███╗   ███╗ █████╗ ████████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔══██╗╚══██╔══╝██║  ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║███████║   ██║   ███████║██║     ██║   ██║██║  ██║█████╗  
██║╚██╔╝██║██╔══██║   ██║   ██╔══██║██║     ██║   ██║██║  ██║██╔══╝  
██║ ╚═╝ ██║██║  ██║   ██║   ██║  ██║╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
```

<p align="right"><a href="./README.md">中文</a> | <strong>English</strong></p>

Math Code is a locally runnable terminal coding and math formalization assistant. It packages the terminal agent, AUTOLEAN, and a Lean workspace in a single repository so users can install once and immediately run the CLI, formalize math problems, and continue into Lean proof attempts.

The supported command name is `mathcode`. If older scripts, notes, or shell history still mention `claude` or any other legacy launcher name, replace them with `mathcode`.

## Overview

This repository already includes:
- the terminal agent / TUI
- `AUTOLEAN/`
- `lean-workspace/`

That means the default setup does not require:
- a separate AUTOLEAN checkout
- an external Lean project
- a manual global Lean installation

## Main Capabilities

- Interactive terminal interface
- `-p` / `--print` headless mode
- Claude OAuth as the default login path
- Optional Anthropic-compatible API endpoint support
- Bundled AUTOLEAN formalization and proving workflow
- In-repo Lean + Mathlib bootstrap
- MCP, plugin, and Skills support
- Recovery CLI fallback mode

## Quick Start

### 1. Install

Run this from the repository root:

```bash
bash scripts/setup-local.sh
```

The setup script will:
- reuse system Bun if it already exists, otherwise install a project-local Bun in `.bun/`
- install JavaScript dependencies
- create `.env`
- create `AUTOLEAN/.venv` and install Python dependencies
- check for `lean` and `lake`
- install a project-local Lean toolchain in `.local/elan/` if Lean is missing
- initialize `lean-workspace/`
- try to download the Mathlib cache

Notes:
- if disk space is low, the Mathlib cache step is skipped automatically
- if the cache is skipped, the first Lean compile will be slower
- you can skip it explicitly with:

```bash
MATHCODE_SKIP_MATHLIB_CACHE=1 bash scripts/setup-local.sh
```

### 2. Configure Authentication

Copy the template:

```bash
cp .env.example .env
```

Claude OAuth is the recommended default. That means you should leave these unset in `.env`:

```env
# ANTHROPIC_API_KEY=
# ANTHROPIC_AUTH_TOKEN=
# ANTHROPIC_BASE_URL=
```

Then start Math Code and run:

```text
/login
```

Only set the following variables if you intentionally want to use a third-party Anthropic-compatible provider:
- `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_MODEL`

### 3. Start

Interactive mode:

```bash
./bin/mathcode
```

Common examples:

```bash
./bin/mathcode -p "explain this repository"
echo "hello" | ./bin/mathcode -p
./bin/mathcode --help
```

If you need the simplified fallback CLI:

```bash
CLAUDE_CODE_FORCE_RECOVERY_CLI=1 ./bin/mathcode
```

## Math Workflow

The typical flow is:

1. Run `mathcode`
2. Sign in
3. Enter a natural-language math problem
4. Use `AutoLeanFormalize` first
5. Use `AutoLeanProve` if you want to continue into proof generation

By default, the math tools use the bundled:
- `AUTOLEAN/`
- `lean-workspace/`

You only need to set override paths if you want custom locations:
- `AUTOLEAN_DIR`
- `LEAN_PROJECT_DIR`
- `CLAUDE_CLI_CMD`

## Proving Behavior

The current proving flow runs attempts sequentially for a single theorem. It does not run 5 proof attempts in parallel for the same theorem.

Default parameters:
- `attempts_before_replan = 5`
- `max_plan_rounds = 2`
- `workers = 1`

In practice this means:
- a single theorem gets up to `5 × 2 = 10` attempts by default
- those attempts are sequential
- `workers` only parallelizes across different Lean files
- the same theorem is not proven by 5 parallel workers at once

## Environment Variables

| Variable | Purpose |
|------|------|
| `ANTHROPIC_API_KEY` | API key mode |
| `ANTHROPIC_AUTH_TOKEN` | Bearer token mode |
| `ANTHROPIC_BASE_URL` | Custom API endpoint |
| `ANTHROPIC_MODEL` | Default model |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Sonnet model mapping |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Haiku model mapping |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Opus model mapping |
| `AUTOLEAN_DIR` | Override the bundled AUTOLEAN path |
| `LEAN_PROJECT_DIR` | Override the bundled Lean workspace path |
| `CLAUDE_CLI_CMD` | Override the `mathcode -p` command used by AUTOLEAN |
| `API_TIMEOUT_MS` | API request timeout |
| `DISABLE_TELEMETRY` | Disable telemetry |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Disable non-essential network traffic |
| `MATHCODE_SKIP_MATHLIB_CACHE` | Skip Mathlib cache during setup |

## Windows

`bin/mathcode` is a bash script, so on Windows you should start the app through Bun:

```powershell
bun --env-file=.env ./src/entrypoints/cli.tsx
```

Headless mode:

```powershell
bun --env-file=.env ./src/entrypoints/cli.tsx -p "your prompt here"
```

Recovery CLI:

```powershell
bun --env-file=.env ./src/localRecoveryCli.ts
```

## Project Structure

```text
bin/mathcode
scripts/setup-local.sh
.env.example
AUTOLEAN/
lean-workspace/
src/
├── entrypoints/cli.tsx
├── main.tsx
├── localRecoveryCli.ts
├── setup.ts
├── screens/
├── tools/
├── commands/
├── skills/
├── services/
└── utils/
```

## Common Uses

- running the terminal agent locally
- turning natural-language math problems into Lean 4
- performing Lean + Mathlib compile-check-repair loops
- keeping the CLI, AUTOLEAN, and Lean workspace in one repository

## Notes

- this repository is for learning and research use
- the math workflow depends on model access and a working Lean compile environment
- if the Mathlib cache is skipped, the first related task will be slower
