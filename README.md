# Math Code

```
███╗   ███╗ █████╗ ████████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔══██╗╚══██╔══╝██║  ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║███████║   ██║   ███████║██║     ██║   ██║██║  ██║█████╗  
██║╚██╔╝██║██╔══██║   ██║   ██╔══██║██║     ██║   ██║██║  ██║██╔══╝  
██║ ╚═╝ ██║██║  ██║   ██║   ██║  ██║╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
```

<p align="right"><strong>中文</strong> | <a href="./README.en.md">English</a></p>

Math Code 是一个本地可运行的终端代码与数学形式化助手。它把终端 Agent、AUTOLEAN 和 Lean 工作区打包在同一个仓库里，目标是让用户在一次安装后就能直接运行交互式 CLI、做数学题形式化、再继续进入 Lean 证明流程。

默认命令名是 `mathcode`。如果旧脚本、旧文档或 shell history 里出现 `claude` 或其他历史启动名，请统一改成 `mathcode`。

## 概览

仓库内已经内置：
- 终端 Agent / TUI
- `AUTOLEAN/`
- `lean-workspace/`

这意味着默认情况下不需要：
- 单独 checkout AUTOLEAN
- 额外准备外部 Lean 项目
- 手工全局安装 Lean 工具链

## 主要能力

- 交互式终端界面
- `-p` / `--print` 无头模式
- Claude OAuth 默认登录流程
- 可选接入 Anthropic 兼容 API
- 内置 AUTOLEAN 数学形式化与证明工作流
- 仓库内自举 Lean + Mathlib
- 支持 MCP、插件和 Skills
- 提供 Recovery CLI 作为兜底模式

## 快速开始

### 1. 安装

在仓库根目录运行：

```bash
bash scripts/setup-local.sh
```

该脚本会自动完成：
- 复用系统 Bun；如果系统没有，则安装仓库本地 `.bun/`
- 安装 JavaScript 依赖
- 生成 `.env`
- 为 `AUTOLEAN/` 创建 `.venv` 并安装 Python 依赖
- 检查 `lean` / `lake`
- 如果系统没有 Lean，则安装仓库本地 `.local/elan/`
- 初始化 `lean-workspace/`
- 尝试下载 Mathlib cache

说明：
- 如果磁盘空间不足，Mathlib cache 会自动跳过
- 跳过后第一次 Lean 编译会更慢
- 如需显式跳过，可使用：

```bash
MATHCODE_SKIP_MATHLIB_CACHE=1 bash scripts/setup-local.sh
```

### 2. 配置认证

复制模板：

```bash
cp .env.example .env
```

默认推荐使用 Claude OAuth。也就是说，不要在 `.env` 中设置下面这些变量：

```env
# ANTHROPIC_API_KEY=
# ANTHROPIC_AUTH_TOKEN=
# ANTHROPIC_BASE_URL=
```

启动后在 MathCode 内执行：

```text
/login
```

如果你明确要接第三方 Anthropic 兼容端点，再手动设置：
- `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_MODEL`

### 3. 启动

交互模式：

```bash
./bin/mathcode
```

常用形式：

```bash
./bin/mathcode -p "explain this repository"
echo "hello" | ./bin/mathcode -p
./bin/mathcode --help
```

如果需要简化版 CLI：

```bash
CLAUDE_CODE_FORCE_RECOVERY_CLI=1 ./bin/mathcode
```

## 数学工作流

典型流程如下：

1. 运行 `mathcode`
2. 完成登录
3. 输入自然语言数学题目
4. 先使用 `AutoLeanFormalize`
5. 如有需要，再使用 `AutoLeanProve`

默认情况下，相关工具会直接使用仓库内置的：
- `AUTOLEAN/`
- `lean-workspace/`

只有在你明确需要覆盖默认路径时，才需要设置：
- `AUTOLEAN_DIR`
- `LEAN_PROJECT_DIR`
- `CLAUDE_CLI_CMD`

## 证明阶段行为

当前 proving 流程对单个 theorem 的尝试是串行执行的，不是同题并行 5 次。

默认参数：
- `attempts_before_replan = 5`
- `max_plan_rounds = 2`
- `workers = 1`

这意味着：
- 单个 theorem 默认最多尝试 `5 × 2 = 10` 次
- 这 10 次是串行执行
- `workers` 只会并行处理不同的 Lean 文件
- 不会让同一个 theorem 同时跑 5 个 proof attempts

## 环境变量

| 变量 | 作用 |
|------|------|
| `ANTHROPIC_API_KEY` | API key 模式 |
| `ANTHROPIC_AUTH_TOKEN` | Bearer token 模式 |
| `ANTHROPIC_BASE_URL` | 自定义 API 端点 |
| `ANTHROPIC_MODEL` | 默认模型 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Sonnet 映射模型 |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Haiku 映射模型 |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Opus 映射模型 |
| `AUTOLEAN_DIR` | 覆盖内置 AUTOLEAN 路径 |
| `LEAN_PROJECT_DIR` | 覆盖内置 Lean 工作区路径 |
| `CLAUDE_CLI_CMD` | 覆盖 AUTOLEAN 调用的 `mathcode -p` 命令 |
| `API_TIMEOUT_MS` | API 请求超时 |
| `DISABLE_TELEMETRY` | 关闭遥测 |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | 关闭非必要网络流量 |
| `MATHCODE_SKIP_MATHLIB_CACHE` | 安装时跳过 Mathlib cache |

## Windows

`bin/mathcode` 是 bash 脚本，Windows 下建议直接通过 Bun 启动：

```powershell
bun --env-file=.env ./src/entrypoints/cli.tsx
```

无头模式：

```powershell
bun --env-file=.env ./src/entrypoints/cli.tsx -p "your prompt here"
```

Recovery CLI：

```powershell
bun --env-file=.env ./src/localRecoveryCli.ts
```

## 项目结构

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

## 适用场景

- 本地运行终端 Agent
- 把自然语言数学题转成 Lean 4
- 做 Lean + Mathlib 的 compile-check-repair
- 在同一个仓库里打包 CLI、AUTOLEAN 和 Lean workspace

## 致谢

Math Code 的数学形式化与证明管线基于 [AUTOLEAN](https://github.com/T3S1AMAX/autolean.git) 项目。

## 说明

- 本仓库仅供学习和研究用途
- 数学工作流依赖模型调用与 Lean 编译环境
- 如果跳过 Mathlib cache，首次相关任务会更慢
