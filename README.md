# course-buddy 🎓

SJTU 课程回放 → 转录 → 笔记，一条龙自动化。

## 它能干什么

1. **下载** — 通过 Canvas LTI 认证自动获取 v.sjtu.edu.cn 课程回放视频
2. **转录** — 本地 whisper.cpp 语音转文字（完全免费，Apple Silicon 优化）
3. **笔记** — LLM 自动生成结构化课程笔记（Markdown 格式）

## 快速开始

```bash
git clone <repo-url> course-buddy && cd course-buddy
bash setup.sh          # 交互式安装向导（一路跟着走就行）
```

安装向导会依次引导你：
1. ⚙️ 自动安装依赖（Python venv、Homebrew 包、whisper 模型）
2. 🔑 输入 LLM API Key 和 Base URL（笔记生成用）
3. 🎓 输入 Canvas Token（自动验证有效性）
4. 📚 自动从 Canvas 获取你的课程列表

全程交互式问答，**不需要手动编辑任何配置文件**。每一步都可以输入 `b` 返回上一步。

安装完成后：
```bash
source .venv/bin/activate
cb list                # 查看已自动配置的课程
cb all --course <ID>   # 一条龙处理
```

如果你之后需要刷新课程列表（比如新学期选课后），运行：

```bash
cb init                # 重新获取课程，已有课程不会被覆盖
cb init --yes          # 跳过确认，直接添加所有新课程
```

## 系统要求

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| **系统** | macOS 12+ (Intel/Apple Silicon) | macOS 14+ (Apple Silicon) |
| **内存** | 4GB（whisper-cpp large-v3-turbo 峰值 ~2GB） | 8GB+ |
| **磁盘** | 3GB（模型 1.5GB + 依赖 + 临时文件） | 10GB+（视频缓存） |
| **Python** | 3.10+ | 3.12+ |
| **CPU** | 任意（Intel 也能跑，只是慢） | Apple Silicon M1+（10x+ 实时速度） |

### 转录性能参考

| 硬件 | 模型 | 55 分钟课程 | 实时倍速 |
|------|------|------------|---------|
| M4 MacBook Air | ggml-large-v3-turbo | ~283s (4.7min) | 11.7x |
| M1 MacBook Pro | ggml-large-v3-turbo | ~6-8min | 7-9x |
| Intel Mac (i7) | ggml-large-v3-turbo | ~15-20min | 3-4x |

## 安装

### 方式 A：交互式向导（推荐）

```bash
bash setup.sh
```

向导会自动处理所有依赖安装，并引导你输入 API Key、Base URL 和 Canvas Token。

### 方式 B：手动安装

```bash
# 1. 系统依赖
brew install ffmpeg whisper-cpp aria2

# 2. Python 环境
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. whisper 模型
mkdir -p ~/.local/share/whisper-cpp
curl -L -o ~/.local/share/whisper-cpp/ggml-large-v3-turbo.bin \
  https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin

# 4. 配置 .env
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY 和 OPENAI_BASE_URL

# 5. Canvas Token
mkdir -p ~/.config/canvas
echo 'YOUR_TOKEN' > ~/.config/canvas/token

# 6. 自动获取课程
cb init
```

### Canvas API Token

安装向导会引导你输入 Token。如需手动获取：

1. 登录 [oc.sjtu.edu.cn](https://oc.sjtu.edu.cn)
2. 左下角「设置」→「+ 新建访问许可证」
3. 保存 token：
   ```bash
   mkdir -p ~/.config/canvas
   echo 'YOUR_TOKEN_HERE' > ~/.config/canvas/token
   ```

## 使用

### 基本命令

```bash
cb init                           # 从 Canvas 自动获取课程配置
cb init --yes                     # 跳过交互确认
cb list                           # 查看已配课程
cb list-videos --course 88884     # 查看视频列表
cb list-videos --course 88884 --since 7d   # 仅查看最近 7 天视频
cb status                         # 查看所有课程处理进度
cb status --course 88884          # 查看单课程进度
```

### 一条龙处理

```bash
cb all --course 88884             # 下载 + 转录 + 笔记（最近 7 天）
cb all --course 88884 --since 2w  # 最近两周
cb all --course 88884 --index 8   # 指定第 9 个视频
```

### 分步操作

```bash
cb fetch --course 88884 --since 7d       # 只下载
cb transcribe --course 88884             # 只转录
cb notes --course 88884                  # 只生成笔记
cb notes --course 88884 --model gpt-4o   # 用指定模型
```

### 自然语言（需安装 `openai`）

```bash
cb ask 整理泛函分析最近一周的笔记
cb ask 下载现代操作系统最新的课
```

### 清理

```bash
cb clean --course 88884 --what video     # 删除已下载视频
cb clean --course 88884 --what all       # 删除视频 + 音频
cb clean --course 88884 --dry-run        # 只预览不删除
```

## 配置

### config.yaml

详见 `config.yaml.example`。核心配置项：

- `courses` — 课程 ID、名称、别名、关键词
- `transcribe.backend` — 转录后端（`whisper-cpp` | `whisper-api` | `summarize` | `local`）
- `llm` — LLM API 配置（笔记生成）
- `transcribe.clean_video` — 转录后是否自动删除视频

### .env

详见 `.env.example`。必填：`LLM_API_KEY`。

### 转录后端对比

| 后端 | 费用 | 速度 | 质量 | 备注 |
|------|------|------|------|------|
| **whisper-cpp** ⭐ | 免费 | M4: 11.7x | 优 | 推荐，本地运行 |
| whisper-api | ¥0.04/min | 即时 | 优 | 需 API key |
| summarize | 免费 | ~同 whisper-cpp | 中 | 不传 language，中英混合易 hallucinate |
| local | 免费 | 慢 | 优 | 需 faster-whisper，GPU 友好 |

## 每节课费用估算

| 项目 | 费用 |
|------|------|
| 视频下载 | 免费 |
| 转录（whisper-cpp） | 免费 |
| 转录（whisper-api） | ~¥2.2/节（55min × ¥0.04/min） |
| 笔记生成（qwen3-max） | ~¥0.05-0.1/节 |
| **总计（推荐方案）** | **~¥0.1/节** |

## 目录结构

```
course-buddy/
├── config.yaml          # 你的配置（git-ignored）
├── .env                 # API keys（git-ignored）
├── setup.sh             # 一键安装脚本
├── course_buddy/
│   ├── cli.py           # CLI 入口
│   ├── config.py        # 配置加载
│   ├── intent.py        # 自然语言解析
│   ├── fetch/
│   │   ├── canvas_api.py   # Canvas REST API（课程列表获取）
│   │   └── downloader.py   # Canvas LTI 认证 + 视频下载
│   ├── transcribe/
│   │   └── asr.py          # 转录引擎（4 种后端）
│   └── notes/
│       └── summarizer.py   # LLM 笔记生成
├── data/                # 数据目录（git-ignored）
│   ├── downloads/       # 视频文件
│   ├── audio/           # 临时音频
│   ├── transcripts/     # 转录 JSON
│   └── notes/           # 生成的笔记 Markdown
└── tests/
```

## 故障排查

### Cookie 过期

```
未找到视频平台登录表单。Cookie 可能已失效
```

→ 在浏览器中重新登录 oc.sjtu.edu.cn，程序会自动从 Chrome 读取新 cookies。

### whisper-cli 找不到

```bash
brew install whisper-cpp
# 或检查路径
which whisper-cli
```

### 转录出现乱码（hallucination）

程序内置了 hallucination 过滤器（外语检测、重复检测）。如果仍有问题：
- 确认 `config.yaml` 中 `transcribe.language` 设置正确
- 确认 `transcribe.target_langs` 包含课程实际语言
- 尝试换用 `whisper-api` 后端对比

### 笔记生成失败

- 检查 `.env` 中 `LLM_API_KEY` 是否正确
- 检查 API base URL 是否可访问：`curl https://aihubmix.com/v1/models`

## License

MIT
