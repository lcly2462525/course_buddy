#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# course-buddy 交互式安装向导
# ============================================================
# 用法: bash setup.sh

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

TOKEN_FILE="$HOME/.config/canvas/token"
MODEL_DIR="$HOME/.local/share/whisper-cpp"
MODEL_FILE="$MODEL_DIR/ggml-large-v3-turbo.bin"

# ---- 工具函数 ----
print_header() {
    echo ""
    echo -e "${BOLD}════════════════════════════════════════${NC}"
    echo -e "${BOLD}  $1${NC}"
    echo -e "${BOLD}════════════════════════════════════════${NC}"
    echo ""
}

print_step() {
    echo -e "\n${CYAN}[$1/$TOTAL_STEPS]${NC} ${BOLD}$2${NC}\n"
}

print_ok() {
    echo -e "  ${GREEN}✅ $1${NC}"
}

print_warn() {
    echo -e "  ${YELLOW}⚠️  $1${NC}"
}

print_err() {
    echo -e "  ${RED}❌ $1${NC}"
}

print_dim() {
    echo -e "  ${DIM}$1${NC}"
}

# 读取用户输入，支持默认值
# 用法: ask_input "提示" "默认值" result_var
ask_input() {
    local prompt="$1"
    local default="$2"
    local varname="$3"
    local input

    if [ -n "$default" ]; then
        read -rp "  $prompt [${default}]: " input
        input="${input:-$default}"
    else
        read -rp "  $prompt: " input
    fi
    eval "$varname=\$input"
}

# 读取密码/敏感输入（不显示默认值全文）
ask_secret() {
    local prompt="$1"
    local default="$2"
    local varname="$3"
    local input
    local hint=""

    if [ -n "$default" ]; then
        # 只显示前 8 字符
        local preview="${default:0:8}..."
        hint=" [当前: ${preview}]"
    fi

    read -rp "  ${prompt}${hint}: " input
    input="${input:-$default}"
    eval "$varname=\$input"
}

TOTAL_STEPS=4

# ============================================================
# 欢迎
# ============================================================
print_header "🎓 course-buddy 安装向导"
echo "  将引导你完成以下配置："
echo ""
echo "    1. 安装系统依赖"
echo "    2. 配置 LLM API（笔记生成）"
echo "    3. 配置 Canvas Token（课程视频访问）"
echo "    4. 自动获取课程列表"
echo ""
echo -e "  ${DIM}每一步都可以输入 'b' 返回上一步，'q' 退出${NC}"
echo -e "  ${DIM}直接回车使用 [方括号] 中的默认值${NC}"

# ============================================================
# Step 1: 系统依赖（自动，不需要交互）
# ============================================================
print_step 1 "安装系统依赖"

# Python venv
if [ ! -d ".venv" ]; then
    echo "  📦 创建 Python 虚拟环境..."
    python3 -m venv .venv
fi
source .venv/bin/activate
print_ok "Python $(python3 --version | cut -d' ' -f2)"

# pip
echo "  📦 安装 Python 依赖..."
pip install -e . -q 2>/dev/null
print_ok "Python 依赖已安装"

# Homebrew
if command -v brew &>/dev/null; then
    for pkg in aria2 ffmpeg whisper-cpp; do
        if brew list "$pkg" &>/dev/null; then
            print_ok "$pkg 已安装"
        else
            echo "  📦 安装 $pkg..."
            brew install "$pkg"
        fi
    done
else
    print_warn "未找到 Homebrew，跳过系统依赖"
    print_dim "请手动安装: brew install aria2 ffmpeg whisper-cpp"
fi

# whisper 模型
if [ -f "$MODEL_FILE" ]; then
    print_ok "whisper 模型已存在 ($(du -h "$MODEL_FILE" | cut -f1))"
else
    echo ""
    echo "  📥 下载 whisper 模型 (ggml-large-v3-turbo, ~1.5GB)..."
    print_dim "本地转录所需，下载一次即可"
    mkdir -p "$MODEL_DIR"

    HF_URL="https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
    HF_ORIG="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"

    if curl -fL --progress-bar -o "$MODEL_FILE" "$HF_URL" 2>/dev/null; then
        print_ok "模型下载完成"
    elif curl -fL --progress-bar -o "$MODEL_FILE" "$HF_ORIG" 2>/dev/null; then
        print_ok "模型下载完成（via huggingface.co）"
    else
        print_err "模型下载失败"
        print_dim "请手动下载: curl -L -o $MODEL_FILE $HF_URL"
    fi
fi

# ============================================================
# Step 2-4: 交互式配置（支持返回上一步）
# ============================================================

# 从现有 .env 读取当前值
current_api_key=""
current_base_url="https://aihubmix.com/v1"
current_model="qwen3-max"
current_token=""

if [ -f ".env" ]; then
    current_api_key=$(grep -E "^LLM_API_KEY=" .env 2>/dev/null | cut -d'=' -f2- || true)
    _base=$(grep -E "^OPENAI_BASE_URL=" .env 2>/dev/null | cut -d'=' -f2- || true)
    if [ -n "$_base" ] && [ "$_base" != "https://aihubmix.com/v1" ]; then
        current_base_url="$_base"
    fi
fi
if [ -f "$TOKEN_FILE" ]; then
    current_token=$(cat "$TOKEN_FILE" 2>/dev/null || true)
fi

# 存储用户输入
user_api_key="$current_api_key"
user_base_url="$current_base_url"
user_model="$current_model"
user_token="$current_token"

# 向导状态机
step=2

while true; do
    case $step in
    2)
        # ============================================================
        # Step 2: LLM API 配置
        # ============================================================
        print_step 2 "配置 LLM API（用于生成课堂笔记）"

        echo "  选择你的 LLM 服务商（后续可在 config.yaml 中修改或添加更多 provider）："
        echo ""
        echo -e "  ${BOLD}1)${NC} aihubmix     ${DIM}https://aihubmix.com/v1（多模型聚合，国内可用）${NC}"
        echo -e "  ${BOLD}2)${NC} OpenAI       ${DIM}https://api.openai.com/v1${NC}"
        echo -e "  ${BOLD}3)${NC} DeepSeek     ${DIM}https://api.deepseek.com/v1${NC}"
        echo -e "  ${BOLD}4)${NC} SiliconFlow  ${DIM}https://api.siliconflow.cn/v1${NC}"
        echo -e "  ${BOLD}5)${NC} 通义千问     ${DIM}https://dashscope.aliyuncs.com/compatible-mode/v1${NC}"
        echo -e "  ${BOLD}6)${NC} 自定义 URL   ${DIM}（学校接口或其他 OpenAI 兼容 API）${NC}"
        echo ""

        while true; do
            read -rp "  选择 [1-6, 默认 1]: " provider_choice
            provider_choice="${provider_choice:-1}"

            if [ "$provider_choice" = "q" ]; then
                echo -e "\n${DIM}已退出安装向导${NC}"; exit 0
            fi
            if [ "$provider_choice" = "b" ]; then
                print_dim "这已经是第一个配置步骤了"
                continue
            fi

            case "$provider_choice" in
                1) user_base_url="https://aihubmix.com/v1";       default_model="qwen3-max"; break ;;
                2) user_base_url="https://api.openai.com/v1";     default_model="gpt-4o"; break ;;
                3) user_base_url="https://api.deepseek.com/v1";   default_model="deepseek-chat"; break ;;
                4) user_base_url="https://api.siliconflow.cn/v1"; default_model="Qwen/Qwen3-Max"; break ;;
                5) user_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"; default_model="qwen3-max"; break ;;
                6)
                    echo ""
                    read -rp "  自定义 Base URL: " user_base_url
                    if [ -z "$user_base_url" ]; then
                        print_warn "URL 不能为空"
                        continue
                    fi
                    default_model="deepseek-chat"
                    break
                    ;;
                *) print_warn "请输入 1-6"; continue ;;
            esac
        done

        echo -e "  ${DIM}→ Base URL: $user_base_url${NC}"
        echo ""

        # API Key
        while true; do
            if [ -n "$user_api_key" ] && [ "$user_api_key" != "sk-your-key-here" ]; then
                ask_secret "LLM API Key" "$user_api_key" user_api_key
            else
                read -rp "  LLM API Key: " user_api_key
            fi

            if [ "$user_api_key" = "q" ]; then
                echo -e "\n${DIM}已退出安装向导${NC}"; exit 0
            fi
            if [ "$user_api_key" = "b" ]; then
                continue 2  # 回到 step 2 开头重新选 provider
            fi
            if [ -z "$user_api_key" ] || [ "$user_api_key" = "sk-your-key-here" ]; then
                print_warn "API Key 不能为空（笔记生成必需）"
                continue
            fi
            break
        done

        # Model
        echo ""
        if [ -z "$user_model" ] || [ "$user_model" = "$current_model" ]; then
            user_model="$default_model"
        fi
        echo -e "  ${DIM}推荐模型: ${default_model}${NC}"
        ask_input "LLM 模型名" "$user_model" user_model

        if [ "$user_model" = "q" ]; then
            echo -e "\n${DIM}已退出安装向导${NC}"; exit 0
        fi
        if [ "$user_model" = "b" ]; then
            user_model="$current_model"
            continue
        fi

        # 确认
        echo ""
        echo -e "  ${BOLD}LLM 配置预览：${NC}"
        echo -e "    API Key:  ${user_api_key:0:8}..."
        echo -e "    Base URL: $user_base_url"
        echo -e "    模型:     $user_model"
        echo ""
        read -rp "  确认？(Y/n/b返回) " confirm
        confirm="${confirm:-y}"

        if [ "$confirm" = "q" ]; then
            echo -e "\n${DIM}已退出安装向导${NC}"; exit 0
        fi
        if [ "$confirm" = "b" ] || [ "$confirm" = "B" ]; then
            continue  # 回到 step 2 重新填
        fi
        if [ "$confirm" = "n" ] || [ "$confirm" = "N" ]; then
            continue
        fi

        # 写入 .env
        cat > .env << EOF
# course-buddy 环境变量配置（由安装向导生成）

# LLM API key — 用于笔记生成和自然语言解析
LLM_API_KEY=$user_api_key
OPENAI_API_KEY=$user_api_key
OPENAI_BASE_URL=$user_base_url

# === 以下为可选配置 ===
# whisper-api 后端（如需使用 Groq 免费 whisper API）:
# WHISPER_API_KEY=gsk_your-groq-key-here
# WHISPER_BASE_URL=https://api.groq.com/openai/v1
EOF
        chmod 600 .env
        print_ok ".env 已保存"

        step=3
        ;;

    3)
        # ============================================================
        # Step 3: Canvas Token
        # ============================================================
        print_step 3 "配置 Canvas API Token（用于获取课程和视频）"

        echo "  获取方式："
        echo "    1. 登录 https://oc.sjtu.edu.cn"
        echo "    2. 左下角「设置」→「+ 新建访问许可证」"
        echo "    3. 复制生成的 token 粘贴到下面"
        echo ""

        while true; do
            if [ -n "$user_token" ]; then
                ask_secret "Canvas API Token" "$user_token" user_token
            else
                read -rp "  Canvas API Token: " user_token
            fi

            if [ "$user_token" = "q" ]; then
                echo -e "\n${DIM}已退出安装向导${NC}"; exit 0
            fi
            if [ "$user_token" = "b" ]; then
                user_token="$current_token"
                step=2
                break
            fi
            if [ -z "$user_token" ]; then
                print_warn "Token 不能为空（获取课程和视频必需）"
                echo -e "  ${DIM}如暂时没有，输入 q 退出，之后手动配置${NC}"
                continue
            fi

            # 验证 token
            echo -e "  ${DIM}验证 Token...${NC}"
            http_code=$(curl -s -o /dev/null -w "%{http_code}" \
                -H "Authorization: Bearer $user_token" \
                "https://oc.sjtu.edu.cn/api/v1/users/self" 2>/dev/null || echo "000")

            if [ "$http_code" = "200" ]; then
                print_ok "Token 有效"
                mkdir -p ~/.config/canvas
                echo "$user_token" > "$TOKEN_FILE"
                chmod 600 "$TOKEN_FILE"
                print_ok "已保存到 $TOKEN_FILE"
                step=4
                break
            elif [ "$http_code" = "401" ]; then
                print_err "Token 无效（401 Unauthorized），请检查后重试"
                user_token=""
                continue
            else
                print_warn "无法验证（HTTP $http_code），可能是网络问题"
                read -rp "  仍然保存此 Token？(y/N) " save_anyway
                if [ "$save_anyway" = "y" ] || [ "$save_anyway" = "Y" ]; then
                    mkdir -p ~/.config/canvas
                    echo "$user_token" > "$TOKEN_FILE"
                    chmod 600 "$TOKEN_FILE"
                    print_ok "已保存到 $TOKEN_FILE"
                    step=4
                    break
                fi
                user_token=""
                continue
            fi
        done
        ;;

    4)
        # ============================================================
        # Step 4: 自动获取课程
        # ============================================================
        print_step 4 "自动获取课程列表"

        # 生成 config.yaml（如果不存在）
        if [ ! -f "config.yaml" ]; then
            # 从 example 复制，替换 LLM 配置
            python3 -c "
import yaml

with open('config.yaml.example') as f:
    cfg = yaml.safe_load(f) or {}

cfg['courses'] = {}
cfg['llm']['base_url'] = '$user_base_url'
cfg['llm']['model'] = '$user_model'
cfg.get('ask', {}).get('llm', {})['model'] = '$user_model'

with open('config.yaml', 'w') as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
"
            print_ok "config.yaml 已创建"
        fi

        echo ""
        echo "  正在从 Canvas 获取你的课程..."
        echo ""

        # 调用 cb init
        if command -v cb &>/dev/null; then
            cb init || true
        else
            python3 -m course_buddy.cli init || true
        fi

        # 跳出循环
        break
        ;;

    *)
        break
        ;;
    esac
done

# ============================================================
# 最终环境检查
# ============================================================
print_header "🔍 环境检查"

# Python
echo -n "  Python:       "; python3 --version 2>/dev/null | cut -d' ' -f2 || echo "❌"

# ffmpeg
echo -n "  ffmpeg:       "; ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3 || echo "❌ (brew install ffmpeg)"

# aria2
echo -n "  aria2c:       "; aria2c --version 2>/dev/null | head -1 | cut -d' ' -f3 || echo "⚠️  可选 (brew install aria2)"

# whisper-cli
echo -n "  whisper-cli:  "; whisper-cli --version 2>/dev/null || echo "$(which whisper-cli 2>/dev/null || echo '❌ (brew install whisper-cpp)')"

# Model
echo -n "  whisper 模型: "
if [ -f "$MODEL_FILE" ]; then
    echo "✅ $(du -h "$MODEL_FILE" | cut -f1)"
else
    echo "❌ 未下载"
fi

# .env
echo -n "  .env:         "
if [ -f ".env" ]; then
    if grep -q "sk-your-key-here" .env 2>/dev/null; then
        echo "⚠️  未配置 API key"
    else
        echo "✅"
    fi
else
    echo "❌"
fi

# Canvas token
echo -n "  Canvas Token: "
if [ -f "$TOKEN_FILE" ]; then echo "✅"; else echo "⚠️"; fi

# config.yaml
echo -n "  config.yaml:  "
if [ -f "config.yaml" ]; then
    n_courses=$(python3 -c "
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f) or {}
print(len(cfg.get('courses') or {}))
" 2>/dev/null || echo "0")
    echo "✅ (${n_courses} 门课程)"
else
    echo "⚠️  未创建"
fi

# cb command
echo -n "  cb 命令:      "
if command -v cb &>/dev/null; then
    echo "✅ $(which cb)"
else
    echo "⚠️  在 venv 内可用: source .venv/bin/activate"
fi

# ============================================================
# 完成
# ============================================================
print_header "🎉 安装完成！"

echo "  快速开始："
echo ""
echo "    source .venv/bin/activate"
echo "    cb list                         # 查看已配课程"
echo "    cb list-videos --course <ID>    # 查看视频列表"
echo "    cb all --course <ID>            # 一条龙：下载→转录→笔记"
echo ""
echo -e "  ${DIM}如需重新配置: bash setup.sh${NC}"
echo -e "  ${DIM}如需刷新课程: cb init${NC}"
echo ""
