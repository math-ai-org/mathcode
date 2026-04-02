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

Math Code is a terminal AI coding assistant with a built-in math formalization engine. Give it a math problem in plain language and it will automatically convert it into a Lean 4 theorem and attempt a formal proof.

## Key Features

- Interactive terminal UI (TUI)
- `-p` / `--print` headless mode (scriptable)
- Natural language math → Lean 4 theorem statement (auto-formalization)
- Theorem statement → complete proof (auto-proving)
- Compile-check-repair loop (up to 10 attempts)
- Semantic fidelity grading (A/B/C/D)
- Live display of LLM reasoning, Lean code, and compiler errors
- Natural language proof explanation on demand
- Claude OAuth login / API key authentication
- MCP server and plugin support

---

## Quick Start

### 1. Clone the repository

This project uses Git LFS for the binary. Make sure `git-lfs` is installed before cloning:

```bash
# Install git-lfs (if not already installed)
brew install git-lfs   # macOS
# apt install git-lfs  # Linux

git lfs install
git clone https://github.com/math-ai-org/mathcode-macOS.git
cd mathcode-macOS
```

> **Note:** If `./run` fails with `version: command not found`, the binary wasn't downloaded properly. Run `git lfs pull` to fetch the real file.

### 2. Requirements

- macOS (arm64) or Linux (x86_64)
- Python 3.10+
- ~2GB disk space (Lean + Mathlib cache needs an additional ~5GB)

### 3. Install and run

```bash
bash setup.sh
./run
```

`setup.sh` handles everything: creates `.env`, installs Python dependencies, installs Lean toolchain, and downloads Mathlib cache.

Once setup is done, use `./run` to start Math Code.

### 4. Configure Authentication

**Option A: Claude OAuth (recommended)**

Leave `.env` unchanged. After starting Math Code, run:

```
/login
```

Follow the browser prompt to authorize.

**Option B: API Key**

Set in `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...
```

**Option C: Third-party compatible endpoint**

```env
ANTHROPIC_API_KEY=your-key
ANTHROPIC_BASE_URL=https://your-endpoint.com
ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

### 5. Launch

```bash
./run
```

Common usage:

```bash
./run -p "prove that the square of an even number is even"
./run --help
```

---

## Math Workflow

### Pipeline

```
Natural language math problem
    │
    ▼
┌─────────────────────────────┐
│     AutoLeanFormalize       │
│                             │
│  1. LLM derives strategy    │
│  2. Generate Lean 4 theorem │
│  3. Compile → repair (≤6x)  │
│  4. Semantic fidelity grade │
└─────────────┬───────────────┘
              │
              ▼
     Lean theorem + sorry
              │
              ▼
┌─────────────────────────────┐
│       AutoLeanProve         │
│                             │
│  1. Planner: proof strategy │
│  2. Prover: proof code      │
│  3. Compile → repair        │
│  4. Replan on failure       │
│  (up to 2 rounds × 5 each) │
└─────────────┬───────────────┘
              │
              ▼
      Complete Lean 4 proof
```

### Example

Type into Math Code:

```
Prove that for all integers n, if n is even then n^2 is even
```

Math Code automatically calls AutoLeanFormalize. When done, the terminal shows:

- Grade (A = fully faithful, B = mostly, C = partial, D = poor)
- Lean code in a green bordered box (syntax-highlighted)
- Action menu:
  - **Prove it** — proceed to automated proving
  - **Retry formalization** — try a different approach
  - **Done** — keep the formalization as-is

After proving:
  - **Explain proof** — get a step-by-step natural language walkthrough
  - **Retry proving** — try again
  - **Done** — finished

### Live Progress

| Content | Style |
|---|---|
| Thinking/planning notes | Dimmed header + text |
| Generated Lean code | Green rounded border + syntax highlighting |
| Compiler errors | Red rounded border |
| Status updates | `[AUTOLEAN]` prefix + bold |

### Output Files

Formalization results are saved to `LeanFormalizations/`:

```
LeanFormalizations/
├── problem_xxx.lean          # Lean theorem + sorry
├── problem_xxx.eval.json     # Semantic grade details
└── problem_xxx_proven.lean   # Completed proof (if successful)
```

---

## Proving Parameters

| Parameter | Default | Description |
|---|---|---|
| `attempts_before_replan` | 5 | Attempts before asking the planner for a new strategy |
| `max_plan_rounds` | 2 | Maximum replanning rounds |
| `workers` | 1 | Parallel workers (across files, not same theorem) |

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | API key authentication |
| `ANTHROPIC_AUTH_TOKEN` | Bearer token authentication |
| `ANTHROPIC_BASE_URL` | Custom API endpoint |
| `ANTHROPIC_MODEL` | Default model |
| `AUTOLEAN_DIR` | Override bundled AUTOLEAN path |
| `LEAN_PROJECT_DIR` | Override bundled Lean workspace path |
| `CLAUDE_CLI_CMD` | Override the CLI command used by AUTOLEAN |
| `DISABLE_TELEMETRY` | Disable telemetry |

---

## Directory Layout

```
mathcode              # main executable
run                   # launcher script
setup.sh              # one-command setup (Lean + Python)
.env.example          # config template
AUTOLEAN/             # Python math formalization pipeline
lean-workspace/       # Lean 4 + Mathlib compile workspace
LeanFormalizations/   # formalization output (created at runtime)
```

---

## FAQ

**Q: Authentication fails on startup?**

Run `/login` to complete Claude OAuth, or set an API key in `.env`.

**Q: Formalization / proving is slow?**

This is expected. Each iteration involves an LLM call + Lean compilation. Formalization typically takes 2-5 minutes, proving may need 5-15 minutes.

**Q: `lake build` fails?**

Run `bash setup.sh` to reinstall, or make sure elan is installed and on your PATH.

**Q: Can I use it without math, just as a terminal agent?**

Absolutely. Math Code is a full-featured terminal AI assistant supporting file editing, code search, command execution, and more. The math tools only activate when you input a math problem.

---

## Acknowledgments

The math formalization and proving pipeline in Math Code is based on the [AUTOLEAN](https://github.com/T3S1AMAX/autolean.git) project.

## Notes

- This project is for learning and research purposes
- The math workflow requires Claude API access and a working Lean compile environment
- If the Mathlib cache is skipped, the first compilation task will be slower
