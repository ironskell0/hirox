"""Catalog of OpenAI-compatible AI providers and lightweight chat client."""

from __future__ import annotations

import copy
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": True,
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini", "o1"],
    },
    "anthropic": {
        "name": "Anthropic (OpenAI Compat)",
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["claude-sonnet-4-20250514", "claude-3-7-sonnet-latest", "claude-3-5-haiku-latest"],
    },
    "gemini": {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    },
    "gemini_oauth": {
        "name": "Gemini OAuth",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_OAUTH_TOKEN",
        "api_key": "",
        "auth_type": "oauth",
        "oauth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    },
    "grok_oauth": {
        "name": "Grok Bot OAuth",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "GROK_OAUTH_TOKEN",
        "api_key": "",
        "auth_type": "oauth",
        "oauth_url": "https://accounts.x.ai/oauth/authorize",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["grok-3", "grok-3-mini", "grok-2-latest"],
    },
    "antigravity_oauth": {
        "name": "Antigravity OAuth",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "ANTIGRAVITY_OAUTH_TOKEN",
        "api_key": "",
        "auth_type": "oauth",
        "oauth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["gemini-3.8-flash-tiered", "gemini-2.5-pro", "claude-sonnet-4-5"],
    },
    "openai_codex_oauth": {
        "name": "OpenAI Codex OAuth",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key_env": "OPENAI_CODEX_OAUTH_TOKEN",
        "api_key": "",
        "auth_type": "oauth",
        "oauth_url": "https://auth.openai.com/oauth/authorize",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["gpt-5.3-codex", "gpt-5.4", "gpt-6-luna", "gpt-6.1-sol", "codex-mini-latest"],
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": [
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4",
            "google/gemini-2.5-flash",
            "deepseek/deepseek-chat",
            "meta-llama/llama-3.3-70b-instruct",
        ],
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "xai": {
        "name": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["grok-3", "grok-3-mini", "grok-2-latest"],
    },
    "mistral": {
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["mistral-large-latest", "mistral-small-latest", "codestral-latest"],
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": [
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
            "deepseek-ai/DeepSeek-V3",
        ],
    },
    "fireworks": {
        "name": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "api_key_env": "FIREWORKS_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": [
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/deepseek-v3",
            "accounts/fireworks/models/qwen2p5-72b-instruct",
        ],
    },
    "perplexity": {
        "name": "Perplexity",
        "base_url": "https://api.perplexity.ai",
        "api_key_env": "PERPLEXITY_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["sonar-pro", "sonar", "sonar-reasoning"],
    },
    "cohere": {
        "name": "Cohere (Compat)",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "api_key_env": "COHERE_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["command-a-03-2025", "command-r-plus", "command-r"],
    },
    "cerebras": {
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["llama-3.3-70b", "llama3.1-8b", "qwen-3-32b"],
    },
    "sambanova": {
        "name": "SambaNova",
        "base_url": "https://api.sambanova.ai/v1",
        "api_key_env": "SAMBANOVA_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["Meta-Llama-3.3-70B-Instruct", "DeepSeek-R1", "Qwen2.5-72B-Instruct"],
    },
    "deepinfra": {
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "api_key_env": "DEEPINFRA_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"],
    },
    "huggingface": {
        "name": "Hugging Face Inference",
        "base_url": "https://router.huggingface.co/v1",
        "api_key_env": "HF_TOKEN",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct"],
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct"],
    },
    "github_models": {
        "name": "GitHub Models",
        "base_url": "https://models.inference.ai.azure.com",
        "api_key_env": "GITHUB_TOKEN",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["gpt-4o", "gpt-4o-mini", "Meta-Llama-3.1-70B-Instruct"],
    },
    "azure_openai": {
        "name": "Azure OpenAI",
        "base_url": "https://YOUR_RESOURCE.openai.azure.com/openai/v1",
        "api_key_env": "AZURE_OPENAI_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "cloudflare": {
        "name": "Cloudflare Workers AI",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/YOUR_ACCOUNT_ID/ai/v1",
        "api_key_env": "CLOUDFLARE_API_TOKEN",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/qwen/qwen2.5-coder-32b-instruct"],
    },
    "replicate": {
        "name": "Replicate (OpenAI Compat)",
        "base_url": "https://openai-proxy.replicate.com/v1",
        "api_key_env": "REPLICATE_API_TOKEN",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta/meta-llama-3.1-405b-instruct", "deepseek-ai/deepseek-r1"],
    },
    "novita": {
        "name": "Novita AI",
        "base_url": "https://api.novita.ai/v3/openai",
        "api_key_env": "NOVITA_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta-llama/llama-3.3-70b-instruct", "deepseek/deepseek-v3"],
    },
    "hyperbolic": {
        "name": "Hyperbolic",
        "base_url": "https://api.hyperbolic.xyz/v1",
        "api_key_env": "HYPERBOLIC_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"],
    },
    "nebius": {
        "name": "Nebius AI Studio",
        "base_url": "https://api.studio.nebius.ai/v1",
        "api_key_env": "NEBIUS_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct"],
    },
    "featherless": {
        "name": "Featherless AI",
        "base_url": "https://api.featherless.ai/v1",
        "api_key_env": "FEATHERLESS_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["meta-llama/Meta-Llama-3.1-70B-Instruct"],
    },
    "chutes": {
        "name": "Chutes AI",
        "base_url": "https://llm.chutes.ai/v1",
        "api_key_env": "CHUTES_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    },
    "venice": {
        "name": "Venice AI",
        "base_url": "https://api.venice.ai/api/v1",
        "api_key_env": "VENICE_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["llama-3.3-70b", "deepseek-r1-671b"],
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
    },
    "moonshot": {
        "name": "Moonshot AI (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    },
    "zhipu": {
        "name": "Zhipu AI (GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["glm-4-plus", "glm-4-flash", "glm-4-air"],
    },
    "qwen": {
        "name": "Alibaba Qwen (DashScope)",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-coder-32b-instruct"],
    },
    "yi": {
        "name": "01.AI (Yi)",
        "base_url": "https://api.lingyiwanwu.com/v1",
        "api_key_env": "YI_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["yi-lightning", "yi-large"],
    },
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "api_key_env": "MINIMAX_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["MiniMax-Text-01", "abab6.5s-chat"],
    },
    "stepfun": {
        "name": "StepFun",
        "base_url": "https://api.stepfun.com/v1",
        "api_key_env": "STEPFUN_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["step-2-16k", "step-1-8k"],
    },
    "poe": {
        "name": "Poe API",
        "base_url": "https://api.poe.com/v1",
        "api_key_env": "POE_API_KEY",
        "api_key": "",
        "openai_compatible": True,
        "requires_key": True,
        "enabled": False,
        "models": ["GPT-4o", "Claude-3.7-Sonnet", "Gemini-2.5-Pro"],
    },
    "ollama": {
        "name": "Ollama (Local)",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "api_key": "ollama",
        "openai_compatible": True,
        "requires_key": False,
        "enabled": False,
        "models": ["llama3.2", "qwen2.5-coder", "deepseek-r1", "mistral"],
    },
    "lmstudio": {
        "name": "LM Studio (Local)",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key_env": "LMSTUDIO_API_KEY",
        "api_key": "lm-studio",
        "openai_compatible": True,
        "requires_key": False,
        "enabled": False,
        "models": ["local-model"],
    },
    "vllm": {
        "name": "vLLM (Local)",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_env": "VLLM_API_KEY",
        "api_key": "EMPTY",
        "openai_compatible": True,
        "requires_key": False,
        "enabled": False,
        "models": ["meta-llama/Llama-3.1-8B-Instruct"],
    },
    "jan": {
        "name": "Jan (Local)",
        "base_url": "http://127.0.0.1:1337/v1",
        "api_key_env": "JAN_API_KEY",
        "api_key": "jan",
        "openai_compatible": True,
        "requires_key": False,
        "enabled": False,
        "models": ["llama3.2-3b-instruct"],
    },
    "localai": {
        "name": "LocalAI",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key_env": "LOCALAI_API_KEY",
        "api_key": "localai",
        "openai_compatible": True,
        "requires_key": False,
        "enabled": False,
        "models": ["gpt-4"],
    },
}


def default_providers() -> dict[str, dict[str, Any]]:
    """Return a deep copy of the built-in provider catalog."""
    return copy.deepcopy(PROVIDER_CATALOG)


def resolve_api_key(provider_cfg: dict[str, Any], provider_id: str = "") -> str:
    """Resolve API key from config value, environment variable, or OAuth store."""
    direct = str(provider_cfg.get("api_key") or "").strip()
    if direct:
        return direct
    env_name = str(provider_cfg.get("api_key_env") or "").strip()
    if env_name:
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
    if provider_cfg.get("auth_type") == "oauth" and provider_id:
        from hirox.oauth import get_valid_oauth_token

        tok = get_valid_oauth_token(provider_id)
        if tok:
            return tok
    return ""


def _extract_text_tool_calls(content: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse fallback <function=...> tool calls emitted as text by some models."""
    pattern = re.compile(
        r"<function=(?:default_api:)?([a-zA-Z0-9_]+)\s*(\{.*?\})\s*(?:</function>|>)?",
        flags=re.DOTALL,
    )
    calls: list[dict[str, Any]] = []
    for idx, m in enumerate(pattern.finditer(content), start=1):
        name = m.group(1).strip()
        raw_args = m.group(2).strip()
        args: dict[str, Any] = {}
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                args = parsed
        except json.JSONDecodeError:
            # Handle unquoted key:value pairs like {command:curl -s ...}
            inner = raw_args[1:-1].strip()
            if ":" in inner:
                k, v = inner.split(":", 1)
                args = {k.strip().strip("\"'"): v.strip().strip("\"'")}
        calls.append(
            {
                "id": f"text_tc_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        )
    cleaned = pattern.sub("", content).strip() if calls else content
    return cleaned, calls


def _flatten_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert tool_calls/tool messages into plain assistant/user text for strict endpoints."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            names = [
                str((tc.get("function") or {}).get("name", "tool"))
                for tc in (m.get("tool_calls") or [])
            ]
            txt = str(m.get("content") or "").strip()
            note = f"[Called tools: {', '.join(names)}]"
            out.append({"role": "assistant", "content": f"{txt}\n{note}".strip()})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": f"[Tool output ({m.get('tool_call_id', 'tool')}):\n{m.get('content', '')}]",
                }
            )
        else:
            out.append(m)
    return out


def _call_codex_responses(
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Call ChatGPT/Codex OAuth Responses SSE backend."""
    from hirox.oauth import extract_chatgpt_account_id, load_oauth_credentials

    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for m in messages:
        role = str(m.get("role", "user"))
        content = str(m.get("content") or "")
        if role == "system":
            if content:
                instructions_parts.append(content)
        elif role == "user":
            input_items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                }
            )
        elif role == "assistant":
            if content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": str(tc.get("id") or "call_1"),
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                    }
                )
        elif role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(m.get("tool_call_id") or "call_1"),
                    "output": content,
                }
            )

    body_dict: dict[str, Any] = {
        "model": model,
        "instructions": "\n\n".join(instructions_parts) or "You are Hirox, a helpful AI assistant.",
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if tools:
        codex_tools: list[dict[str, Any]] = []
        for t_item in tools:
            fn = t_item.get("function") or {}
            if fn.get("name"):
                codex_tools.append(
                    {
                        "type": "function",
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                    }
                )
        if codex_tools:
            body_dict["tools"] = codex_tools

    acct = extract_chatgpt_account_id(api_key) or str(
        load_oauth_credentials("openai_codex_oauth").get("account_id", "")
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.101.0",
    }
    if acct:
        headers["ChatGPT-Account-Id"] = acct

    url = "https://chatgpt.com/backend-api/codex/responses"
    req = urllib.request.Request(
        url,
        data=json.dumps(body_dict).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_sse = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {err_body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error to {url}: {exc.reason}") from exc

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    for line in raw_sse.splitlines():
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            ev = json.loads(data_str)
        except Exception:
            continue
        ev_type = ev.get("type", "")
        if ev_type == "response.output_text.delta":
            delta = ev.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif ev_type == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                tool_calls.append(
                    {
                        "id": str(item.get("call_id") or item.get("id") or f"call_{len(tool_calls)+1}"),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or ""),
                            "arguments": str(item.get("arguments") or "{}"),
                        },
                    }
                )
        elif ev_type == "response.completed":
            resp_obj = ev.get("response") or {}
            if isinstance(resp_obj.get("usage"), dict):
                u = resp_obj["usage"]
                usage = {
                    "prompt_tokens": int(u.get("input_tokens") or 0),
                    "completion_tokens": int(u.get("output_tokens") or 0),
                    "total_tokens": int(u.get("total_tokens") or 0),
                }

    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if usage:
        msg["_usage"] = usage
    return msg


def chat_completion_with_tools(
    provider_cfg: dict[str, Any],
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Send an OpenAI-compatible chat completion request with optional tools."""
    base_url = str(provider_cfg.get("base_url", "")).rstrip("/")
    if not base_url:
        raise ValueError("Provider base_url is empty.")

    api_key = resolve_api_key(provider_cfg)
    if provider_cfg.get("requires_key", True) and not api_key:
        env_hint = provider_cfg.get("api_key_env", "API_KEY")
        raise RuntimeError(
            f"API key missing for {provider_cfg.get('name', 'provider')}. "
            f"Set it in `hirox config` or export {env_hint}."
        )

    if "chatgpt.com/backend-api/codex" in base_url or provider_cfg.get("api_key_env") == "OPENAI_CODEX_OAUTH_TOKEN":
        return _call_codex_responses(
            api_key=api_key,
            model=model,
            messages=messages,
            tools=tools,
            timeout=timeout,
        )

    url = f"{base_url}/chat/completions"
    body_dict: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body_dict["tools"] = tools
    payload = json.dumps(body_dict).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 400 and ("thought_signature" in err_body or tools):
            # Retry with flattened tool history (for Gemini thought_signature) or without tools
            retry_msgs = _flatten_tool_messages(messages) if "thought_signature" in err_body else messages
            fallback_dict: dict[str, Any] = {"model": model, "messages": retry_msgs}
            if tools and "thought_signature" in err_body:
                fallback_dict["tools"] = tools
            req2 = urllib.request.Request(
                url,
                data=json.dumps(fallback_dict).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                    body = json.loads(resp2.read().decode("utf-8"))
            except urllib.error.HTTPError as exc2:
                err2 = exc2.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {exc2.code} from {url}: {err2[:300]}") from exc2
        else:
            raise RuntimeError(f"HTTP {exc.code} from {url}: {err_body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error to {url}: {exc.reason}") from exc

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"Unexpected response from {url}: {body}")
    msg = dict(choices[0].get("message") or {})
    if not msg.get("tool_calls") and isinstance(msg.get("content"), str):
        cleaned, text_calls = _extract_text_tool_calls(msg["content"])
        if text_calls:
            msg["content"] = cleaned
            msg["tool_calls"] = text_calls
    if isinstance(body.get("usage"), dict):
        msg["_usage"] = body["usage"]
    return msg


def estimate_context_window(model: str) -> int:
    """Return real cached context window if known, else accurate model-family estimate."""
    from hirox.oauth import get_cached_context_window

    cached = get_cached_context_window(model)
    if cached and cached > 0:
        return cached
    m = model.lower()
    if "gemini" in m:
        return 1_000_000
    if "claude" in m:
        return 200_000
    if any(k in m for k in ("gpt-6", "gpt-5.4", "gpt-4.1")):
        return 1_000_000
    if any(k in m for k in ("gpt-5", "codex")):
        return 400_000
    if any(k in m for k in ("o1", "o3", "o4")):
        return 200_000
    if any(k in m for k in ("gpt-4o", "deepseek", "qwen", "llama-3", "mistral", "grok")):
        return 128_000
    if "gpt-4" in m:
        return 8_192
    return 128_000


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Roughly estimate token count (~4 chars/token) when API usage is unavailable."""
    total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return max(1, total_chars // 4)


def chat_completion(
    provider_cfg: dict[str, Any],
    model: str,
    messages: list[dict[str, Any]],
    timeout: int = 60,
) -> str:
    """Send an OpenAI-compatible chat completion request using stdlib urllib."""
    message = chat_completion_with_tools(
        provider_cfg=provider_cfg,
        model=model,
        messages=messages,
        tools=None,
        timeout=timeout,
    )
    return str(message.get("content") or "")
