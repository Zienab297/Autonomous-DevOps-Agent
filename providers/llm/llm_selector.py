"""
providers/llm/llm_selector.py
-------------------------------
Per-agent LLM provider selector.

Rules:
  - ALWAYS asks provider + model every time an agent needs LLM
  - API keys are saved and reused — user can update if needed
  - Key is asked AFTER provider choice, BEFORE model fetch
  - Provider is built with the CORRECT key (no stale key bug)
  - On quota error → shows message → asks to switch provider
  - On missing package → shows install command → falls back to default models
  - Config stored in: <project_root>/.devops_llm_config (JSON)
"""

import os
import json
import getpass
from pathlib import Path
from typing import Optional

from providers.llm.base_llm_provider import BaseLLMProvider

# ── Config file ────────────────────────────────────────────────────────────────
_CONFIG_FILE = Path(__file__).resolve().parents[2] / ".devops_llm_config"

# ── Provider registry ──────────────────────────────────────────────────────────
_PROVIDERS = {
    "1": ("ollama", "Ollama  (local — no API key needed)"),
    "2": ("groq",   "Groq    (free cloud — fast)"),
    "3": ("openai", "OpenAI  (GPT-4o, GPT-4o-mini)"),
    "4": ("claude", "Claude  (Anthropic)"),
    "5": ("gemini", "Gemini  (Google)"),
}

_NEEDS_KEY = {"groq", "openai", "claude", "gemini"}

_ENV_MAP = {
    "groq"  : "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_AGENT_LABELS = {
    "scaffold"  : "Scaffold Agent  — Dockerfile, CI/CD, k8s files",
    "knowledge" : "Knowledge Agent — RAG + solution generation",
    "healing"   : "Self-Healing Agent — applies code fixes",
    "monitoring": "Monitoring Agent — incident analysis",
    "default"   : "DevOps Agent",
}

_DEFAULT_MODELS = {
    "ollama": ["llama3.2:3b", "llama3.1:8b", "mistral:7b", "gemma2:9b", "phi3:mini"],
    "groq"  : ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
    "claude": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "gemini": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
}

_REQUIRED_PACKAGES = {
    "gemini": "google-generativeai",
    "openai": "openai",
    "claude": "anthropic",
    "groq"  : "groq",
}

# ── ANSI ───────────────────────────────────────────────────────────────────────
_R  = "\033[0m"
_B  = "\033[1m"
_D  = "\033[2m"
_CY = "\033[36m"
_GR = "\033[32m"
_YL = "\033[33m"
_RD = "\033[31m"


# ── Config persistence ─────────────────────────────────────────────────────────

def _load_config() -> dict:
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    try:
        _CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def _get_saved_api_key(provider: str) -> str:
    """Return saved API key for provider, or empty string."""
    cfg = _load_config()
    key = cfg.get("api_keys", {}).get(provider, "")
    if key:
        return key
    return os.getenv(_ENV_MAP.get(provider, ""), "")


def _save_api_key(provider: str, api_key: str) -> None:
    """Save API key for provider — persists across runs."""
    cfg = _load_config()
    if "api_keys" not in cfg:
        cfg["api_keys"] = {}
    cfg["api_keys"][provider] = api_key
    _save_config(cfg)
    env_var = _ENV_MAP.get(provider, "")
    if env_var:
        _write_to_dotenv(env_var, api_key)
        os.environ[env_var] = api_key


def _write_to_dotenv(env_var: str, key: str) -> None:
    env_path = Path.cwd() / ".env"
    try:
        lines, found = [], False
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{env_var}="):
                    lines.append(f"{env_var}={key}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"{env_var}={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


# ── Build provider instance ────────────────────────────────────────────────────

def _build_provider(provider: str, api_key: str, model: str) -> BaseLLMProvider:
    if provider == "ollama":
        from providers.llm.ollama_provider import OllamaProvider
        return OllamaProvider(model=model)
    elif provider == "groq":
        from providers.llm.groq_provider import GroqProvider
        return GroqProvider(api_key=api_key, model=model)
    elif provider == "openai":
        from providers.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=api_key, model=model)
    elif provider == "claude":
        from providers.llm.claude_provider import ClaudeProvider
        return ClaudeProvider(api_key=api_key, model=model)
    elif provider == "gemini":
        from providers.llm.gemini_provider import GeminiProvider
        return GeminiProvider(api_key=api_key, model=model)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _check_package(provider: str) -> bool:
    """Returns True if the required package is installed."""
    pkg = _REQUIRED_PACKAGES.get(provider)
    if not pkg:
        return True
    try:
        if provider == "gemini":
            import google.generativeai
        elif provider == "openai":
            import openai
        elif provider == "claude":
            import anthropic
        elif provider == "groq":
            import groq
        return True
    except ImportError:
        return False


# ── Interactive selector ───────────────────────────────────────────────────────

def _ask_provider(agent: str) -> tuple[str, str, str]:
    """
    Ask user to choose provider + model + API key.
    
    Order (IMPORTANT — fixes stale-key bug):
    1. Choose provider
    2. Handle API key → saved / new
    3. Fetch models using the CORRECT key
    4. Choose model
    
    Returns (provider_name, model, api_key).
    """
    label = _AGENT_LABELS.get(agent, agent)

    print(f"\n  {_B}{_CY}LLM SETUP — {label}{_R}")
    print(f"  {'─'*55}")
    print(f"  {_B}Choose provider:{_R}")
    for k, (_, desc) in _PROVIDERS.items():
        print(f"    {_B}[{k}]{_R} {desc}")
    print(f"  {'─'*55}")

    # Step 1: Choose provider
    while True:
        try:
            choice = input("  Enter number [1-5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "2"
        if choice in _PROVIDERS:
            provider_name = _PROVIDERS[choice][0]
            break
        print(f"  {_RD}Invalid — enter 1 to 5{_R}")

    # Check package is installed
    if not _check_package(provider_name):
        pkg = _REQUIRED_PACKAGES.get(provider_name, provider_name)
        print(f"\n  {_RD}⚠  Package not installed: {pkg}{_R}")
        print(f"  Run: {_B}pip install {pkg}{_R}")
        print(f"  Then restart and try again.")
        print(f"\n  {_YL}Falling back to Ollama (local).{_R}")
        provider_name = "ollama"

    # Step 2: Handle API key BEFORE fetching models
    api_key = ""
    if provider_name in _NEEDS_KEY:
        saved_key = _get_saved_api_key(provider_name)
        env_var   = _ENV_MAP[provider_name]

        if saved_key:
            masked = saved_key[:6] + "..." + saved_key[-4:] if len(saved_key) > 10 else "****"
            print(f"\n  {_GR}✔ Found saved {env_var}: {masked}{_R}")
            print(f"  {'─'*55}")
            print(f"  {_B}[1]{_R} Use saved token")
            print(f"  {_B}[2]{_R} Enter new token")
            print(f"  {'─'*55}")

            while True:
                try:
                    token_choice = input("  Choose [1/2]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    token_choice = "1"

                if token_choice == "1":
                    api_key = saved_key
                    print(f"  {_GR}✔ Using saved {env_var}{_R}")
                    break
                elif token_choice == "2":
                    try:
                        new_key = getpass.getpass(f"  Enter new {env_var}: ").strip()
                    except Exception:
                        try:
                            new_key = input(f"  Enter new {env_var}: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            new_key = ""
                    if new_key:
                        _save_api_key(provider_name, new_key)
                        api_key = new_key
                        print(f"  {_GR}✔ New {env_var} saved{_R}")
                    else:
                        api_key = saved_key
                        print(f"  {_YL}No key entered — using saved{_R}")
                    break
                else:
                    print(f"  {_RD}Invalid — enter 1 or 2{_R}")
        else:
            print(f"\n  {_YL}{env_var} not found.{_R}")
            try:
                api_key = getpass.getpass(f"  Enter {env_var}: ").strip()
            except Exception:
                try:
                    api_key = input(f"  Enter {env_var}: ").strip()
                except (EOFError, KeyboardInterrupt):
                    api_key = ""
            if api_key:
                _save_api_key(provider_name, api_key)
                print(f"  {_GR}✔ {env_var} saved{_R}")

    # Step 3: Fetch models using the CORRECT key
    print(f"\n  {_B}Available models for {provider_name.upper()}:{_R}")
    print(f"  {_D}Fetching...{_R}", end="\r")
    try:
        tmp    = _build_provider(provider_name, api_key, "")
        models = tmp.list_models()
        if not models:
            models = _DEFAULT_MODELS.get(provider_name, [])
    except Exception:
        models = _DEFAULT_MODELS.get(provider_name, [])

    # Step 4: Choose model
    if models:
        display = models[:10]
        for i, m in enumerate(display, 1):
            print(f"    {_B}[{i}]{_R} {m}   ")
        print(f"    {_B}[c]{_R} {_D}type custom model name{_R}")
        print(f"  {'─'*55}")
        while True:
            try:
                idx = input(f"  Choose [1-{len(display)}/c]: ").strip()
            except (EOFError, KeyboardInterrupt):
                idx = "1"
            if idx.lower() == "c":
                try:
                    model = input("  Model name: ").strip() or models[0]
                except (EOFError, KeyboardInterrupt):
                    model = models[0]
                break
            elif idx.isdigit() and 1 <= int(idx) <= len(display):
                model = display[int(idx)-1]
                break
            elif idx and not idx.isdigit():
                model = idx
                break
            print(f"  {_RD}Invalid{_R}")
    else:
        print(f"  {_YL}Could not fetch models — enter manually{_R}")
        try:
            model = input("  Model name: ").strip()
        except (EOFError, KeyboardInterrupt):
            model = "default"

    print(f"\n  {_GR}✔ {provider_name.upper()} / {model}{_R}\n")
    return provider_name, model, api_key


# ── Public API ─────────────────────────────────────────────────────────────────

def get_llm_provider(
    agent: str = "default",
    force_select: bool = True,
) -> BaseLLMProvider:
    """
    Returns a ready provider for the given agent.
    Always asks provider + model interactively.
    API key: shows saved / asks for new.
    """
    provider_name, model, api_key = _ask_provider(agent)
    return _build_provider(provider_name, api_key, model)


def handle_quota_error(
    current_provider: BaseLLMProvider,
    agent: str = "default",
) -> Optional[BaseLLMProvider]:
    """
    Called when quota is exhausted.
    Shows message, asks to switch provider, returns new provider.
    """
    print(f"\n  {_RD}{_B}⚠  QUOTA EXCEEDED{_R}")
    if current_provider and hasattr(current_provider, "name"):
        print(f"  Provider: {current_provider.name.upper()}")
    print(f"  Quota or rate limit reached.")
    print(f"  You need to switch to a different provider to continue.\n")

    try:
        answer = input("  Switch to a different provider? [yes/no]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "yes"

    if answer not in ("yes", "y"):
        return None

    provider_name, model, api_key = _ask_provider(agent)
    return _build_provider(provider_name, api_key, model)


def is_quota_error(error: Exception) -> bool:
    msg = str(error).lower()
    return any(kw in msg for kw in [
        "quota", "rate limit", "rate_limit", "429",
        "insufficient_quota", "exceeded", "billing",
        "limit reached", "resource_exhausted", "overloaded",
    ])


def clear_api_key(provider: str) -> None:
    """Remove saved API key for a provider."""
    cfg = _load_config()
    if "api_keys" in cfg and provider in cfg["api_keys"]:
        del cfg["api_keys"][provider]
        _save_config(cfg)