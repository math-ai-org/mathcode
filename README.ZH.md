# MathCode

### MathCode: A Frontier Mathematical Coding Agent

```
███╗   ███╗ █████╗ ████████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔══██╗╚══██╔══╝██║  ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║███████║   ██║   ███████║██║     ██║   ██║██║  ██║█████╗
██║╚██╔╝██║██╔══██║   ██║   ██╔══██║██║     ██║   ██║██║  ██║██╔══╝
██║ ╚═╝ ██║██║  ██║   ██║   ██║  ██║╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
```

**Project Page:** [math-ai-org/mathcode](https://github.com/math-ai-org/mathcode)

<p align="right"><a href="./README.md">English</a> | <strong>中文</strong></p>

MathCode 是一个内置 Lean 能力的终端 AI 编程助手。agent 可以交互式检查
goal、验证候选源码、检索声明并严格验证最终证明。

![](./Demo.png)

## 快速开始

```bash
git clone https://github.com/math-ai-org/mathcode.git
cd mathcode
bash setup.sh
codex auth login
mathcode
```

`setup.sh` 会准备发行版 checkout：下载或修复 bundle 内运行时，准备本地配置，并为后续 shell 安装 user-local 的 `mathcode` 启动命令。Linux 上还会在 bootstrap Lean workspace 前要求 `bwrap`（`bubblewrap` 包）和 `socat`。

如果当前 shell 还没有 reload profile，可以先用 bundle 内的即时兜底入口 `./run`。

### Setup 会负责什么

运行时文件：

- 如果运行时文件缺失、过旧、无法验证，或当前平台 payload 无效，就下载匹配的
  `mathcode-vX.Y.Z-<os>-<arch>.tar.gz`
- 在需要修复时，从 archive 恢复 `./mathcode`、`./mathcode-webui` 和
  `vendor/ripgrep/`
- 使用 `shasum` 或 `sha256sum` 校验当前平台的 `SHA256SUMS.txt` 条目
- 替换已有可用安装前，先校验下载得到的运行时文件
- 记录 CLI 和 WebUI helper 的 release metadata，方便后续 `setup.sh` 和
  `setup.sh --status` 发现过旧或无法验证的二进制

本地配置：

- 在需要时从 `.env.example` 创建 `.env`
- 默认在 `~/.local/bin/` 安装受管的 user-local `mathcode` launcher
- 创建 `tools/`、`plugins/` 扩展目录和自带的 `skills/` 参考文档目录；
  项目技能从 `.mathcode/skills/<name>/SKILL.md` 加载
- 在 `vendor/ripgrep/` 内自带 MathCode 内部搜索使用的 `rg` 二进制

Lean 工具链：

- 自带版本化的 `lean-workspace/lake-manifest.json`，让 setup 和本地运行使用
  当前 release 锁定的依赖图
- setup 会在需要时物化空的受管 `VaultLibs/UserVaultLibs/` 骨架；源码构建主机
  上的本地 vault 镜像、测试 fixture 与临时 Lean 文件不会进入发布包
- setup 会直接物化该锁定依赖图，不执行 `lake update`，也不会重写 manifest
- 可选的 Mathlib cache 获取结束后，必须完成本地 `MathCodeLean` readiness
  build；跳过 cache 或下载失败时会回退到该构建，而构建失败会中止 setup
- 默认使用 bundle-local 的完整 `.local/elan` Lean/Lake 工具对
- 接受 Git Bash/MSYS 下可见的 `lean.exe` / `lake.exe`
- 在 bootstrap Lean workspace 前先修复不完整的本地 elan 工具文件
- 仅在 `MATHCODE_SETUP_USE_SYSTEM_LEAN=1` 且系统 `lean` / `lake` 都可用时
  复用系统 Lean/Lake，并保留现有 `ELAN_HOME`

### Launcher 和 PATH 行为

setup 只会覆盖它自己之前创建过的 launcher 文件，避免覆盖已有但不属于这次安装的 `mathcode` 命令。

如果设置了 `MATHCODE_INSTALL_BIN_DIR`，相对路径会先按 bundle 根目录解析成绝对路径，
再写入 launcher、记录状态文件和受管 PATH 配置块。

即使该目录已经在当前 shell 的 `PATH` 里，setup 也会刷新受管 profile 配置块，
让之后新开的 shell 继续识别 `mathcode`。

如果选定的 launcher 目录无法使用，setup 只会跳过 launcher 步骤，其余安装流程继续执行。

当 `MATHCODE_SETUP_USE_SYSTEM_LEAN=1` 时，setup 会在切换到 bundle 根目录前捕获系统
`lean` / `lake` 路径，并把已验证的绝对路径写入 `.env`。即使之后进程的
`PATH` 不同，runtime 也会重新验证并使用这组精确路径。这两个受管路径采用严格的
`base64:` UTF-8 编码，因此反斜杠、引号和反引号都能同时安全通过 Bun dotenv
解析与 `./run` 的 shell sourcing。

未设置这个 opt-in 时，setup 会清除过期的受管系统工具链选择，`--status` 会报告
默认的本地 `.local/elan` 路径，而不会把系统 Lean 当成已安装。

生成的普通 `.env` 路径值会按 shell 规则引用，因此 bundle 路径里包含 `$` 或单引号等字符时，`./run` source `.env` 后仍会保留字面值；受管的系统 Lean/Lake 路径使用上面的双解析器编码。

### 维护命令

```bash
bash setup.sh --status   # 检查二进制和依赖是否健康
bash setup.sh --clean    # 删除安装产物，但保留证明结果和 vault 数据
bash setup.sh --help     # 查看全部 setup 参数
```

`setup.sh --status` 会检查：

- `./mathcode --version` 和 checksum 是否匹配当前 release tag 的 metadata
- `./mathcode-webui` 是否匹配记录的 release metadata
- 当前平台的 bundled `rg` 是否可执行并能输出 ripgrep 版本信息

`setup.sh --clean` 会保留 `LeanFormalizations/`、vault 里的用户输出和当前
release 锁定的 Lake manifest。

如果 setup 曾经记录过受管 launcher，之后即使不再设置
`MATHCODE_INSTALL_BIN_DIR`，`--status` 和 `--clean` 也会继续跟踪它。

## 环境要求

- macOS (arm64)，或基于 glibc 且支持 AVX2 的 Linux（x86_64，使用 Ubuntu 22.04 构建）
- `curl`，用于 setup/bootstrap 下载
- `shasum` 或 `sha256sum`，用于校验 release archive 并写入 metadata
- 足够的磁盘空间用于 bundle、Lean 工具链和 Mathlib cache
- 如果你想走默认后端和默认数学流程，需要本机安装 `codex` CLI
- Python 3.12+（可选，仅 `tools/` 目录下的分析脚本需要）

## 常用命令

### CLI

```bash
mathcode -p "prove that the square of an even number is even"
echo "hello" | mathcode -p
mathcode --help
```

MCP XAA IdP setup 要求 issuer URL 非空且使用 HTTPS。`mathcode mcp xaa setup --issuer ...`
会在写入 settings 前拒绝 `http://`，包括 loopback URL。

如果当前 shell 还没 reload，可以使用 bundle 内兜底入口：

```bash
./run -p "prove that the square of an even number is even"
echo "hello" | ./run -p
./run --help
```

Agent 会直接编辑所选 workspace 中的 Lean 文件。四个 Lean 原子工具不会另建
run 目录，也不会写入证明库产物。

### 浏览器 UI

```bash
./run webui
```

`./run webui` 会先 source bundle 内的 `.env`，再启动本地 daemon，并打印浏览器认证 URL。

如果直接运行打包后的 `./mathcode-webui` helper，它会先重新进入同目录的
`./run webui` wrapper。

直接运行和 wrapper 运行会使用同一份 `.env`、本地 Lean 工具链和 bundle 默认配置。
如果同目录 wrapper 存在但无法执行，会作为启动失败报告。

在交互式 `./run` session 内，`/webui` 和 `/webUI` 会启动或管理同一个本地
daemon。该 slash command 支持 `--no-browser`、`--port <port>`、`--status`
和 `--stop`；source-only 的 `--rebuild` 在发行版 bundle 中不可用。完整认证 URL
只写到本地终端，command result/status 文本只显示 `token=<redacted>`。通过 slash
command 启动时，它选定的 port 和 workspace 会覆盖 bundle `.env` 中同名的 WebUI
设置。

### Goal 和命令限制

- `MATHCODE_GOAL_MAX_TOKEN_BUDGET` 会限制 source `/goal`、`/goal` daemon
  commands 和 `/api/v1/sessions/:id/goal` 接受的 token budget。它支持与
  `/goal` 相同的正整数、整数值小数和 `k`/`m`/`b` 紧凑格式；未设置或非法值会回退到
  `1000000000`。
- `MATHCODE_MAX_CHAINED_COMMAND_INPUTS` 会限制 `QueryEngine` abort 前允许的嵌套本地
  slash-command next-input 提交数量。未设置或非法值会回退到 `25`。

### Goal 命令语法

交互式发布版 session 支持：

- `/goal <token-budget> <objective>`
- `/goal --budget <token-budget>`
- `/goal --budget=<token-budget>`
- 可选的 `--max-continuations N` 或 `--max-continuations=<N>`
- `/goal pause`、`/goal resume`、`/goal status`、`/goal clear`
- 裸 `/goal`、`/goal help`、`/goal -h`、`/goal --help`

该命令会继续同一个 session，不会启动单独 agent。以 `/` 开头的 objective
会作为普通 goal 文本提交，不会再次解析成 slash command。

budget 后的第一个 objective token 可以是 `--help`。objective 解析已经开始后，
flag-like token 会保留为 objective 文本，除非后置的有效 `--budget`
正在用于提供必需的显式 budget。

当 `--budget` 被解析为 budget 选项时，非法值会被拒绝，包括这样的数值表达式
objective：

```text
1 + 1 ... --budget nope
```

### 模型 Effort

`--effort <level>` 或交互式 `/effort <level>` 可使用 `low`、`medium`、
`high`、`max` 或正整数；`/effort auto` 和 `/effort unset` 会让当前
session 回到模型默认值。
CLI 模型覆盖里的保留值 `default` 会按大小写不敏感匹配；自定义模型 ID
会保留原始大小写。

### 自定义 Agent

自定义 agent 定义会 trim `description`、JSON `prompt`、markdown prompt 正文、`initialPrompt`，以及 `effort`、`permissionMode`、`memory`、`isolation` 这类 JSON enum 字段。

空白的必填 description/prompt 会被拒绝，空白的可选 initial prompt 会被忽略；
JSON `skills` 列表会按 markdown frontmatter 的规则归一化。

### Session 诊断、Compaction 和任务

交互式 context 显示会保留诊断信息：

- `/context` 在交互式和非交互式会话里使用同一套可见 markdown transcript 输出
- `/config` 里有默认开启的 `Show tool-use warnings` 开关。关闭后只会抑制
  非错误、且不是 stream parser/drop diagnostic 的 runtime warning transcript
  事件；tool error、权限拒绝、validation failure、stream-json parser diagnostic
  以及会影响 agent 下一步的 tool-call 诊断仍然会保留给 agent。
- markdown 表格会转义单元格内容
- slash-command 和 deferred built-in tool 明细保持可见
- 显示 MCP loaded/available 状态
- current-usage 表格会排除 deferred categories
- manual compact reserve 会作为 reserved buffer 展示
- 当前 usage 为空时仍显示 free/reserved 行
- malformed token rows 和 zero-token synthetic windows 不会产生非法建议百分比
- server-side/MCP tool blocks 会计入 message breakdown

Compact 和 autocompact 路径会：

- 钳制异常阈值、token 计数、legacy content shape 和空白 tool ID
- 保留 singleton tool-result 配对
- 把 statusline、away summary、survey、sticky prompt UI 限定到 compact 后的活跃 transcript
- partial compact 后抑制陈旧 warning
- 合并重复的远端 compacting 状态

任务处理：

- `/tasks`、`TaskStop` 和 SDK `stop_task` 不会把可选中的 leader 行计入 running teammate
- 可以停止 pending remote agent 和 running in-process teammate
- task tools 和 SDK `stop_task` 会 trim task id
- deprecated `shell_id` 以及 TaskOutput `agentId`/`bash_id` alias
  可以回填空白 `task_id`
- legacy `wait_up_to` 秒数会被归一化
- 会跨用户可见的 status shape 恢复旧版持久化 task status
- 接受 legacy `TaskUpdate` status alias
- 拒绝空白任务文本字段
- task metadata key 会被 trim，空白或不安全的 `__proto__` metadata key 会被拒绝
- TaskOutput timeout 必须是整数值
- idle in-process teammate output 会被视为可读取，而不是等到 timeout
- TaskOutput/TaskStop 的混合 text/structured result 数组会正确回放
- legacy TaskOutput output 回放会保留类似 `<error>` 的普通文本
- command 空白被 trim 时不会误显示 TaskStop 截断省略号
- 很矮的终端里仍显示隐藏任务摘要，recently completed 行会按时过期

Shell sleep 自动后台化和 path validation 会识别：

- decimal、suffix、signed、exponent 和 trailing-dot duration，例如
  `sleep 2s`、`sleep 2m`、`sleep +2` 和 `sleep 2e0`
- `env ... sleep 2s` 这类 wrapped shell 写法
- PowerShell quoted、commented、redirected 和 module-qualified sleep command，
  例如 `& 'sleep' 2`、`Start-Sleep -Seconds:2 > $null` 和
  `Microsoft.PowerShell.Utility\Start-Sleep -Seconds 2`
- TimeSpan `-Duration` 值，以及 PowerShell 参数缩写和 common parameters
- 短、小数、signed 和 exponent `timeout` wrapper

## 功能特性

### 持久化 Lean 反馈后端

符合条件的普通 compile caller 可以选择启用进程内 Lean REPL：

```env
MATHCODE_LEAN_REPL=1
```

外部 Kimina Lean Server 还可以服务原子探索：

```env
MATHCODE_KIMINA_SERVER=1
MATHCODE_KIMINA_CMD="/absolute/path/to/kimina-lean-server/.venv/bin/python -m server"
MATHCODE_KIMINA_CWD=/absolute/path/to/kimina-lean-server
MATHCODE_KIMINA_PROJECT_ROOT=/absolute/path/to/served-lean-project
```

在 macOS 上，只有 declared project 与解析出的项目完全一致，并且在线 guard 确认 Lean
版本一致时，`LeanGoal` 与 `LeanCheck` 才会复用该 server；否则会回退到
pinned subprocess，并把原因作为 warning 返回。Kimina 反馈不是完成证书。
`LeanVerify` 与隔离的 paper agent 始终使用全新的隔离 subprocess。MathCode
会在 fail-closed Lean sandbox 内以 loopback-only、随机 bearer key（不会进入
Lean REPL 环境）、无 provider credential、仅 scratch 可写的方式启动 Kimina。
若使用 virtualenv，它必须位于 `MATHCODE_KIMINA_CWD` 下；
`MATHCODE_KIMINA_CMD` 必须直接以该 Python 可执行文件开头，不能使用命令包装器。如果 manifest package
root 包含项目或其他受保护 host scope，启动会直接失败；
remote package storage 必须留在项目内。只使用 Python 标准库的 guardian
位于独立只读 support root，并复用已选择的 Kimina Python，因此 sandbox 不会
开放 MathCode 源码或第二套 runtime；交接路径进入该解释器前，Python site、
`.pth` 与 `sitecustomize` 加载会被禁用。该 guardian 介入受支持的 `setsid` Lake
启动，在 Lean 启动前移除私有交接环境、两个 Kimina key 名称及 mode-0600
认证交接文件，并独立监测 owner pipe HUP，所以 Lake 即使停止读取输入也会被
连同其单独进程组清理。父进程退出以及
`SIGINT`/`SIGTERM`/`SIGHUP` 也会清理完整的已拥有进程树。
取消当前 `/api/check` 请求会保留共享服务；取消旧版 `/verify` 请求或
backend/transport 状态不确定时，才会清理整棵服务进程树并重启。Linux 与
Windows 继续使用 pinned subprocess。

### 计划文件

`/plan` 默认把当前 session 的计划 markdown 写到 active user config home
的 `plans/` 槽位里：默认是 `~/.mathcode/plans/`；如果设置了
`MATHCODE_CONFIG_DIR`，则跟随重定位后的 config home。若要把计划文件保留
在项目树内，可在 project 或 local settings 里把 `plansDirectory` 设置为
相对于项目根的自定义目录。嵌套目录会按需创建；如果路径通过 symlink 逃出
项目根，则会回退到 user config home 的 `plans/` 目录。

### 定理库

显式管理可供后续证明复用的定理库：

```bash
/theorem-store store <file> <qualified-declaration> # 验证并存储一个定理
/theorem-store sync   # 检查候选，并询问要存储哪些声明
/theorem-store check  # compile-check 组装后的定理库
/theorem-store status # 查看入库数量和 vault 信息
```

`/theorem-store store` 会针对一个显式的 fully qualified 声明调用
`LeanTheoremLibrary`。该工具执行新鲜、严格的验证，在持久化前立即重新核对
源码与依赖快照，然后对组装后 `Stored.lean` 中已重命名的最终声明再次执行
严格验证。重命名前后的 elaborated 命题必须一致；成功后会立即构建可 import
的 workspace module。定理库、workspace 镜像、编译产物与索引作为一个可回滚
事务更新。
私有定理编译与公开 Lake 发布分别使用有界的 300 秒构建预算；若超时，事务仍会
回滚，不会暴露不完整产物。
`/theorem-store sync` 只是可选的 agent 指导：先发现候选并询问要存储哪些
声明，再对每个确认项分别执行同一个逐声明工具调用。Lean 反馈工具不会把
定理作为隐藏副作用写入定理库。

### 公理库

将对话中的假设存储为持久化、一致性检查的声明：

```bash
/axiomatize "A 比 B 快"             # 形式化 + 存储
/axiomatize list                     # 查看所有活跃公理
/axiomatize check                    # 一致性审查
/axiomatize remove <name>            # 删除一个声明
```

公理按 vault 存储，带有 Lean 形式化并经过编译检查。Lean 原子工具不会隐式
注入这些公理；只有在它们确实属于目标证明上下文时，才显式 import 或引用。

### Obsidian 定理图谱

生成 Obsidian vault，以知识图谱的形式可视化定理依赖关系：

```bash
/obsidian on       # 启用并从已有形式化结果生成
/obsidian off      # 禁用
/obsidian generate # 立即重新生成
```

修改证明后使用 `/obsidian generate` 显式刷新 vault。Lean 原子工具不会把更新
vault 作为隐藏副作用。在 Obsidian 中打开并使用 Graph View 查看定理与引理之间
的关系。
刷新只覆盖带有 MathCode 托管标记的笔记，以及托管标记引入前由 MathCode 生成
且精确符合旧格式的投影；旧投影刷新后会补上标记。如果定理、引理、索引或
blueprint 文件名被其他用户笔记占用，生成会失败并保留该笔记。

每个引理条目都包含通过 `#print` 从 Mathlib 查询到的完整 Lean 定义。

### Agentic Lean

普通 Lean 工作可以由 agent 自主选择四个原子工具：

- `LeanGoal` 检查一个明确的源码位置。
- `LeanCheck` 编译文件或不落盘的候选源码，并返回结构化反馈。
- `LeanSearch` 只查询一个明确指定的 provider，不做隐藏 fan-out。
- `LeanVerify` 对一个 fully qualified declaration 执行严格的最终检查；只有 `data.verified=true` 才代表完成。

可选的 `/lean` skill 只提供启发，不强制阶段、tactic 顺序、重试预算或
planner。原有固定控制器已经移除，不是发行包入口，也不在 model-visible
工具目录中。
被废弃的是固定 scheme，不是有用方法：agent 仍可按需选择子目标分解、helper
lemma、分支、milestone、stuck detection、诊断 repair 和 theorem reuse，并可
自由重排或放弃。axiom 与 theorem library 是独立的显式动作，不会成为 Lean
原子调用的隐藏副作用。
严格验证只使用项目目录外解析出的 Lean/Lake 可执行文件，并保留 Lake 解析出的
规范源码/module context；目标的 axiom usage 与 module 直接声明的公理都来自
Lean environment API，不会信任源码可控制的 macro 或输出。

### 定时 Agent 循环

发行版自带循环调度能力，不需要额外构建参数。

在交互式 MathCode 会话里可以直接用：

```bash
/loop 10m check the deploy
/loop 1h /standup 1
```

短期提醒或监控建议直接用这种循环；如果你希望任务在重启后继续保留，就在交互式会话里创建持久化定时任务。

## 可扩展性

MathCode 支持三种扩展机制：

### 技能 (`.mathcode/skills/`)

项目技能应放在 `.mathcode/skills/<name>/SKILL.md`。每个技能使用独立目录；
单独的 `skills/*.md` 文件不会被加载。

### 工具 (`tools/`)

放入带有 YAML frontmatter 的 Python `.py` 脚本即可添加分析工具。启动时自动发现。

内置 3 个分析工具：`axiom-checker`、`lib-search` 和 `proof-stats`。即使从其他
workspace 启动 MathCode，它们仍然可用；workspace 中同名（规范化后）的工具会覆盖
bundle 内置版本。仅在使用这些工具时才需要 Python 3.12+。

### 插件 (`plugins/`)

放入带有 `.mathcode-plugin/plugin.json` 清单的插件文件夹，即可添加命令、技能、
Agent、MCP 服务器、钩子等。

通过 `--plugin-dir` 加载，或在 MathCode 内通过 `/plugin` 从 Git 仓库安装。

## 后端设置

### 默认 Codex/OpenAI 路线

默认路线不需要改 `.env`：

```bash
codex auth login
mathcode
```

如果你还在刚执行完 setup 的同一个 shell 里，先用 `./run` 也可以；reload shell 之后再直接用 `mathcode`。

发行版的 `.env` 模板现在会选择 GPT-5.6 Sol 与 xhigh 推理强度。若要把相同
配置应用到由旧版发行包创建的现有 `.env`，并同时选择 CLI 的最高 effort
level，请设置：

```env
OPENAI_MODEL=gpt-5.6-sol
OPENAI_SMALL_MODEL=gpt-5.6-sol
OPENAI_REASONING_EFFORT=xhigh
MATHCODE_EFFORT_LEVEL=max
```

如果你想改成 Anthropic 兼容后端，可以设置：

```env
MATHCODE_USE_OPENAI=0

ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
```

发行版 `./run` wrapper 会先 source bundle 内的 `.env`，再启动 MathCode。
交互式 `/webui` slash-command 启动时，它选定的 WebUI port 和 workspace
会覆盖该 `.env` 中同名的键。

WebUI 路由默认值独立于 CLI `.env`：全新的 WebUI 设置使用
`openai` / `gpt-5.6-sol`，已有保存的 provider/model 选择会被保留，不会重写。

### WebUI Provider 密钥

WebUI 设置页里的 provider-key 行只包含 daemon 当前能传给真实 child session 的
secret：`anthropic` 和 `openrouter`。Codex/OpenAI 路线使用 Codex OAuth，
不使用 `OPENAI_API_KEY` 行。
WebUI 的 `minimal` reasoning effort 会在 OpenAI/OpenRouter 路线上保留；
在 Anthropic 兼容路线中会映射到 CLI 当前可用的最低档 `low`。

### 打包的 Provider 依赖

发行版二进制会打包 Anthropic 兼容、Bedrock、Vertex 和 Foundry 分支所需的
provider SDK，以及 MCPB/DXT plugin package；这些路线不需要源码 checkout
里的 `node_modules`。Bedrock、Vertex 和 Foundry 使用各自 provider 的认证方式，
不使用 Anthropic 兼容的 `ANTHROPIC_AUTH_TOKEN` / `apiKeyHelper` bearer headers。

## 常见问题

**Q: setup 之后立刻执行 `mathcode` 还是找不到命令**

开一个新的 shell，或者执行：

```bash
source ~/.zshrc
```

如果你想在 reload 之前先继续使用，也可以直接运行：

```bash
./run
```

**Q: `./run` 报 `exec format error`、`Bad CPU type in executable` 或类似启动错误**

通常是因为下载了错误平台的二进制。重新执行 `bash setup.sh`，或者手动从 GitHub Releases 下载匹配你平台的 asset。

**Q: 启动时提示缺少 Codex 认证**

执行：

```bash
codex auth login
```

**Q: 能不能不 clone，直接下载 release asset**

可以。你也可以直接从 GitHub Releases 下载 `.tar.gz` bundle 后解压使用。

这个 archive 本身是自包含的；只有当 bundled runtime 文件缺失、过旧或无法验证时，
`bash setup.sh` 才会再从 GitHub 下载。bootstrap 仓库只是把 `bash setup.sh`
作为默认路径。

## Star History

想看项目的关注度变化，可以直接查看下面的 Star History 图表：

[![Star History Chart](https://star-history.dera.page/svg?repos=math-ai-org/mathcode&type=Date)](https://star-history.dera.page/#math-ai-org/mathcode&Date)

## 引用

如果你在研究中使用 MathCode，可以按下面的方式引用：

```bibtex
@misc{mathcode2026,
  title = {MathCode: A Frontier Mathematical Coding Agent},
  author = {Team Math-AI},
  journal = {math-ai-org.github.io},
  year = {2026},
  month = {April},
  url = "https://github.com/math-ai-org/mathcode"
}
```

## 社区

加入我们的 Discord 获取帮助、反馈和讨论：**[discord.gg/f2AFP9W5](https://discord.gg/f2AFP9W5)**

## 致谢

MathCode 将有用的证明搜索思想保留为可选的 agent 指导，同时始终以 Lean
作为证明权威。
