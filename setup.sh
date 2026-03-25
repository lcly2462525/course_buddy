#!/usr/bin/env bash
set -euo pipefail

# course-buddy 一键安装脚本
# 用法: bash setup.sh

echo "🎓 course-buddy 安装向导"
echo "========================"
echo ""

# ---- Python venv ----
if [ ! -d ".venv" ]; then
    echo "📦 创建 Python 虚拟环境..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "✅ Python $(python3 --version | cut -d' ' -f2)"

# ---- pip 依赖 ----
echo "📦 安装 Python 依赖..."
pip install -e . -q

# ---- Homebrew 依赖 ----
check_brew() {
    if ! command -v brew &>/dev/null; then
        echo "⚠️  未找到 Homebrew，跳过系统依赖安装"
        echo "   请手动安装: brew install aria2 ffmpeg whisper-cpp"
        return 1
    fi
    return 0
}

if check_brew; then
    for pkg in aria2 ffmpeg whisper-cpp; do
        if brew list "$pkg" &>/dev/null; then
            echo "✅ $pkg 已安装"
        else
            echo "📦 安装 $pkg..."
            brew install "$pkg"
        fi
    done
fi

# ---- whisper-cpp 模型 ----
MODEL_DIR="$HOME/.local/share/whisper-cpp"
MODEL_FILE="$MODEL_DIR/ggml-large-v3-turbo.bin"

if [ -f "$MODEL_FILE" ]; then
    echo "✅ whisper 模型已存在 ($(du -h "$MODEL_FILE" | cut -f1))"
else
    echo ""
    echo "📥 下载 whisper 模型 (ggml-large-v3-turbo, ~1.5GB)..."
    echo "   这是本地转录所需的模型文件"
    mkdir -p "$MODEL_DIR"
    
    # 尝试 HF 镜像（国内友好），失败则用原始地址
    HF_URL="https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
    HF_ORIG="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
    
    if curl -fL --progress-bar -o "$MODEL_FILE" "$HF_URL" 2>/dev/null; then
        echo "✅ 模型下载完成"
    elif curl -fL --progress-bar -o "$MODEL_FILE" "$HF_ORIG" 2>/dev/null; then
        echo "✅ 模型下载完成（via huggingface.co）"
    else
        echo "❌ 模型下载失败，请手动下载："
        echo "   curl -L -o $MODEL_FILE $HF_URL"
    fi
fi

# ---- .env ----
if [ ! -f ".env" ]; then
    echo ""
    echo "📝 创建 .env 配置文件..."
    cp .env.example .env
    echo "⚠️  请编辑 .env 填入你的 API key"
    echo "   vim .env"
else
    echo "✅ .env 已存在"
fi

# ---- Canvas token ----
TOKEN_FILE="$HOME/.config/canvas/token"
if [ -f "$TOKEN_FILE" ]; then
    echo "✅ Canvas API token 已配置"
else
    echo ""
    echo "📝 Canvas API Token 配置:"
    echo "   1. 登录 https://oc.sjtu.edu.cn"
    echo "   2. 点击左下角「设置」→「+ 新建访问许可证」"
    echo "   3. 复制生成的 token"
    echo "   4. 运行: mkdir -p ~/.config/canvas && echo 'YOUR_TOKEN' > ~/.config/canvas/token"
fi

# ---- 验证 ----
echo ""
echo "========================"
echo "🔍 环境检查:"
echo ""

# Python
echo -n "  Python:      "; python3 --version 2>/dev/null | cut -d' ' -f2 || echo "❌ 未找到"

# ffmpeg
echo -n "  ffmpeg:      "; ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3 || echo "❌ 未安装 (brew install ffmpeg)"

# aria2
echo -n "  aria2c:      "; aria2c --version 2>/dev/null | head -1 | cut -d' ' -f3 || echo "⚠️  未安装 (可选, brew install aria2)"

# whisper-cli
echo -n "  whisper-cli: "; whisper-cli --version 2>/dev/null || echo "$(which whisper-cli 2>/dev/null || echo '❌ 未安装 (brew install whisper-cpp)')"

# Model
echo -n "  whisper模型:  "
if [ -f "$MODEL_FILE" ]; then
    echo "✅ $(du -h "$MODEL_FILE" | cut -f1)"
else
    echo "❌ 未下载"
fi

# .env
echo -n "  .env:        "
if [ -f ".env" ]; then
    if grep -q "sk-your-key-here" .env; then
        echo "⚠️  存在但未配置 API key"
    else
        echo "✅"
    fi
else
    echo "❌ 不存在"
fi

# Canvas token
echo -n "  Canvas token: "
if [ -f "$TOKEN_FILE" ]; then echo "✅"; else echo "⚠️  未配置"; fi

# cb command
echo -n "  cb 命令:     "
if command -v cb &>/dev/null; then
    echo "✅ $(which cb)"
else
    echo "⚠️  在 venv 内可用: source .venv/bin/activate"
fi

echo ""
echo "========================"
echo "🎉 安装完成！"
echo ""
echo "快速开始:"
echo "  source .venv/bin/activate"
echo "  cb list                         # 查看已配课程"
echo "  cb list-videos --course 88884   # 查看视频列表"
echo "  cb all --course 88884           # 一条龙处理"
echo ""