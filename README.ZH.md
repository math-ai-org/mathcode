# Math Code

```
███╗   ███╗ █████╗ ████████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔══██╗╚══██╔══╝██║  ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║███████║   ██║   ███████║██║     ██║   ██║██║  ██║█████╗  
██║╚██╔╝██║██╔══██║   ██║   ██╔══██║██║     ██║   ██║██║  ██║██╔══╝  
██║ ╚═╝ ██║██║  ██║   ██║   ██║  ██║╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
```

**Project Page:** [math-ai-org/mathcode](https://github.com/math-ai-org/mathcode) 

<p align="right"><strong>中文</strong> | <a href="./README.md">English</a></p>

Math Code 是一个终端 AI 编程助手，内置数学形式化引擎。输入一道自然语言数学题，它会自动将其转化为 Lean 4 定理并尝试完成形式化证明。

![](./Demo.png) 

## 主要能力

- 交互式终端界面（TUI）
- `-p` / `--print` 无头模式（可嵌入脚本）
- 自然语言数学题 → Lean 4 定理声明（自动形式化）
- 定理声明 → 完整证明（自动证明）
- 编译-检查-修复循环（最多 10 次尝试）
- 语义忠实度评分（A/B/C/D）
- 实时显示 LLM 思考过程、Lean 代码和编译器错误
- 证明完成后可请求自然语言解释
- Claude OAuth 登录 / API key 认证
- MCP 服务器和插件支持

---

## 快速开始

### 1. 克隆仓库

本项目使用 Git LFS 存储二进制文件。克隆前请确保已安装 `git-lfs`：

```bash
# 安装 git-lfs（如尚未安装）
brew install git-lfs   # macOS
# apt install git-lfs  # Linux

git lfs install
git clone https://github.com/math-ai-org/mathcode.git
cd mathcode
```

> **注意：** 如果克隆后运行 `./run` 出现 `version: command not found` 错误，说明二进制文件未正确下载。请运行 `git lfs pull` 拉取真实文件。

### 2. 系统要求

- macOS (arm64) 或 Linux (x86_64)
- Python 3.10+
- 约 2GB 磁盘空间（Lean + Mathlib 缓存另需 ~5GB）

### 3. 安装并运行

```bash
bash setup.sh
./run
```

`setup.sh` 自动完成：创建 `.env`、安装 Python 依赖、安装 Lean 工具链、下载 Mathlib 缓存。

安装完成后，用 `./run` 启动 Math Code。

### 4. 配置认证

**方式一：Claude OAuth（推荐）**

不需要修改 `.env`。启动 Math Code 后执行：

```
/login
```

按提示在浏览器中完成授权即可。

**方式二：API Key**

在 `.env` 中设置：

```env
ANTHROPIC_API_KEY=sk-ant-...
```

**方式三：第三方兼容端点**

```env
ANTHROPIC_API_KEY=your-key
ANTHROPIC_BASE_URL=https://your-endpoint.com
ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

### 5. 启动

```bash
./run
```

常用形式：

```bash
./run -p "prove that the square of an even number is even"
./run --help
```

---

## 数学工作流

### 整体流程

```
自然语言数学题
    │
    ▼
┌─────────────────────────┐
│   AutoLeanFormalize     │
│                         │
│  1. LLM 推导证明策略     │
│  2. 生成 Lean 4 定理     │
│  3. 编译 → 修复（≤6 轮） │
│  4. 语义忠实度评分        │
└───────────┬─────────────┘
            │
            ▼
   Lean 定理 + sorry 占位符
            │
            ▼
┌─────────────────────────┐
│     AutoLeanProve       │
│                         │
│  1. 规划器生成证明策略    │
│  2. 证明器生成证明代码    │
│  3. 编译 → 修复          │
│  4. 失败后重新规划        │
│  （最多 2 轮 × 5 次）    │
└───────────┬─────────────┘
            │
            ▼
    完整 Lean 4 证明
```

### 示例

在 Math Code 中输入：

```
Prove that for all integers n, if n is even then n^2 is even
```

Math Code 会自动调用 AutoLeanFormalize。形式化完成后，终端显示：

- 评分（A = 完全忠实，B = 基本忠实，C = 部分，D = 较差）
- 绿色边框内的 Lean 代码（语法高亮）
- 选项菜单：
  - **Prove it** — 继续自动证明
  - **Retry formalization** — 重新形式化
  - **Done** — 仅保留形式化结果

证明完成后：
  - **Explain proof** — 用自然语言解释整个证明过程
  - **Retry proving** — 重试
  - **Done** — 结束

### 实时进度

| 内容 | 样式 |
|---|---|
| 思考/规划笔记 | 灰色标题 + 文本 |
| 生成的 Lean 代码 | 绿色圆角边框 + 语法高亮 |
| 编译器错误 | 红色圆角边框 |
| 状态信息 | `[AUTOLEAN]` 前缀 + 粗体 |

### 输出文件

形式化结果保存在 `LeanFormalizations/`：

```
LeanFormalizations/
├── problem_xxx.lean          # Lean 定理 + sorry
├── problem_xxx.eval.json     # 语义评分详情
└── problem_xxx_proven.lean   # 完成的证明（如成功）
```

---

## 数学工作流参数

可以在 `.env` 中调整以下参数：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MATHCODE_MAX_FORMALIZE_ITERS` | 6 | 形式化编译-修复迭代次数 |
| `MATHCODE_ATTEMPTS_BEFORE_REPLAN` | 5 | 重新规划前的证明尝试次数 |
| `MATHCODE_MAX_PLAN_ROUNDS` | 2 | 最大规划轮数 |
| `MATHCODE_PROVE_WORKERS` | 1 | 并行证明工作线程（跨文件，非同题并行） |

例如，增加证明尝试次数和规划轮数：

```env
MATHCODE_ATTEMPTS_BEFORE_REPLAN=8
MATHCODE_MAX_PLAN_ROUNDS=3
```

单个定理的最大尝试次数 = `MATHCODE_ATTEMPTS_BEFORE_REPLAN × MATHCODE_MAX_PLAN_ROUNDS`（默认 5 × 2 = 10）。

---

## 环境变量

| 变量 | 作用 |
|---|---|
| `ANTHROPIC_API_KEY` | API key 认证 |
| `ANTHROPIC_AUTH_TOKEN` | Bearer token 认证 |
| `ANTHROPIC_BASE_URL` | 自定义 API 端点 |
| `ANTHROPIC_MODEL` | 默认模型 |
| `AUTOLEAN_DIR` | 覆盖内置 AUTOLEAN 路径 |
| `LEAN_PROJECT_DIR` | 覆盖内置 Lean 工作区路径 |
| `CLAUDE_CLI_CMD` | 覆盖 AUTOLEAN 使用的 CLI 命令 |
| `MATHCODE_MAX_FORMALIZE_ITERS` | 形式化编译-修复迭代次数 |
| `MATHCODE_ATTEMPTS_BEFORE_REPLAN` | 重新规划前的证明尝试次数 |
| `MATHCODE_MAX_PLAN_ROUNDS` | 最大规划轮数 |
| `MATHCODE_PROVE_WORKERS` | 并行证明工作线程数 |
| `DISABLE_TELEMETRY` | 关闭遥测 |

---

## 目录结构

```
mathcode              # 主程序
run                   # 启动脚本
setup.sh              # 一键安装（Lean + Python）
.env.example          # 配置模板
AUTOLEAN/             # Python 数学形式化管线
lean-workspace/       # Lean 4 + Mathlib 编译工作区
LeanFormalizations/   # 形式化输出（运行后自动创建）
```

---

## 常见问题

**Q: 启动后提示认证失败？**

运行 `/login` 完成 Claude OAuth 登录，或在 `.env` 中设置 API key。

**Q: 形式化/证明过程很慢？**

这是正常的。每次迭代包含 LLM 调用 + Lean 编译。形式化通常 2-5 分钟，证明可能需要 5-15 分钟。

**Q: `lake build` 失败？**

运行 `bash setup.sh` 重新安装，或确保 elan 已安装且在 PATH 中。

**Q: 不做数学，只想用终端 Agent？**

完全可以。Math Code 本身就是一个完整的终端 AI 助手，支持文件编辑、代码搜索、命令执行等所有常规功能。数学工具只在你输入数学题时自动激活。

---

## 致谢

Math Code 的数学形式化与证明管线基于 [AUTOLEAN](https://github.com/T3S1AMAX/autolean.git) 项目。

## 说明

- 本项目仅供学习和研究用途
- 数学工作流依赖 Claude API 访问权限和 Lean 编译环境
- 如果跳过 Mathlib cache，首次编译任务会更慢

## Citation
If you use Math Code in your research, please cite:

```bibtex
@misc{mathcode2026,
  title     = {Math Code: A Frontier Mathematical Coding Agent},
  author    = {Math-AI Team},
  year      = {2026},
  url       = {https://github.com/math-ai-org/mathcode}
}
```
