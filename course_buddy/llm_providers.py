"""
多 LLM Provider 支持。

支持 --model provider/model 格式自动切换 base_url 和 api_key。
内置常用 provider 预设，用户可在 config.yaml 的 llm.providers 中覆盖或新增。

用法示例：
    cb notes --model deepseek/deepseek-chat
    cb notes --model sjtu/deepseek-reasoner   # 需在 config.yaml 配置 sjtu provider
    cb notes --model qwen3-max                # 使用默认 provider
"""

import os
from typing import Any, Dict, Optional

# 内置 provider 预设
BUILTIN_PROVIDERS: Dict[str, Dict[str, str]] = {
    "aihubmix": {
        "base_url": "https://aihubmix.com/v1",
        "api_key_env": "LLM_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
}


def resolve_provider(model_str: str, llm_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    解析 provider/model 格式，返回 {provider, model, base_url, api_key, api_key_env}。

    支持的 model_str 格式：
    - "provider/model"  → 从 providers 配置或内置预设查找
    - "model"           → 返回 None 值，由调用方使用默认配置

    providers 查找优先级：
    1. config.yaml 中的 llm.providers 字典
    2. 内置预设 (BUILTIN_PROVIDERS)
    """
    cfg = llm_cfg or {}
    user_providers = cfg.get("providers") or {}

    provider_name = None
    model_name = model_str

    if "/" in model_str:
        parts = model_str.split("/", 1)
        candidate = parts[0].lower()
        all_providers = {**BUILTIN_PROVIDERS, **{k.lower(): v for k, v in user_providers.items()}}
        if candidate in all_providers:
            provider_name = candidate
            model_name = parts[1]

    if provider_name:
        # 优先用户自定义 provider
        prov = None
        for k, v in user_providers.items():
            if k.lower() == provider_name:
                prov = v
                break
        if not prov:
            prov = BUILTIN_PROVIDERS.get(provider_name, {})

        base_url = prov.get("base_url", "")
        api_key_env = prov.get("api_key_env", "LLM_API_KEY")
        api_key = prov.get("api_key") or os.environ.get(api_key_env) or ""
    else:
        base_url = None
        api_key = None
        api_key_env = None

    return {
        "provider": provider_name,
        "model": model_name,
        "base_url": base_url if base_url else None,
        "api_key": api_key if api_key else None,
        "api_key_env": api_key_env,
    }


def get_llm_config(llm_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    解析 LLM 配置，自动处理 model 字段中的 provider 前缀。

    优先级：
    1. llm_cfg["model"] 中的 provider 前缀（如 "sjtu/deepseek-chat"）
    2. llm_cfg 中的 base_url / api_key_env 配置
    3. 环境变量 LLM_API_KEY / OPENAI_API_KEY / OPENAI_BASE_URL
    """
    cfg = llm_cfg or {}

    key_env = cfg.get("api_key_env", "LLM_API_KEY")
    default_api_key = (
        os.environ.get(key_env)
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    default_base_url = (
        cfg.get("base_url")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://aihubmix.com/v1"
    )
    model = cfg.get("model", "qwen3-max")
    temperature = cfg.get("temperature", 0.3)

    api_key = default_api_key
    base_url = default_base_url

    # 检测 model 字段是否含 provider 前缀
    if model:
        resolved = resolve_provider(model, cfg)
        model = resolved["model"]
        if resolved["base_url"]:
            base_url = resolved["base_url"]
        if resolved["api_key"]:
            api_key = resolved["api_key"]

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "temperature": temperature,
    }
