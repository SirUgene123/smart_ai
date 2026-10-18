from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


class Settings:
    gemini_api_key = env("GEMINI_API_KEY")
    cloudflare_api_token = env("CLOUDFLARE_API_TOKEN")
    cloudflare_account_id = env("CLOUDFLARE_ACCOUNT_ID")
    planner_model = env("CLOUDFLARE_PLANNER_MODEL", "@cf/meta/llama-3.1-8b-instruct")
    gemini_api_base = env("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
    cloudflare_api_base = env("CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4")
    default_model = env("GEMINI_DEFAULT_MODEL", "gemini-3.8-flash")
    default_max_output_tokens = int(env("GEMINI_MAX_OUTPUT_TOKENS", "4096"))
    default_store = env("GEMINI_STORE", "false").lower() == "true"
    timeout_seconds = float(env("HTTP_TIMEOUT_SECONDS", "90"))
    usage_file = Path(env("SMART_AI_USAGE_FILE", str(Path.home() / ".smart_ai" / "usage.json"))).expanduser()
    quota_file = Path(env("SMART_AI_QUOTAS_FILE", str(Path.home() / ".smart_ai" / "quotas.json"))).expanduser()
    limit_state_file = Path(env("SMART_AI_LIMIT_STATE_FILE", str(Path.home() / ".smart_ai" / "limit_state.json"))).expanduser()
    prompt_file = Path(env("SMART_AI_ROUTER_PROMPT", str(Path(__file__).parent / "prompts" / "router.md")))


settings = Settings()
