from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import settings
from .errors import RouterError
from .limits import LimitManager


THINKING = {"minimal", "low", "medium", "high"}
ROUTABLE_THINKING = ("low", "medium", "high")
THOUGHT_SUMMARIES = {"none", "auto"}
TOOLS = {"google_search", "code_execution"}


def _json_object(value: Any) -> dict[str, Any]:
    # Workers AI may return model-generated JSON either as serialized text or
    # as an already-decoded object, depending on the model/response format.
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise RouterError("Cloudflare planner did not return a JSON object")
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise RouterError("Cloudflare planner returned invalid JSON") from exc
    else:
        raise RouterError("Cloudflare planner response must be a JSON object or JSON text")
    if not isinstance(parsed, dict):
        raise RouterError("Cloudflare planner output must be a JSON object")
    return parsed


class Providers:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=settings.timeout_seconds)
        self.limits = LimitManager()

    async def close(self) -> None:
        await self.http.aclose()

    def require_gemini_config(self) -> None:
        missing = [n for n, v in (("GEMINI_API_KEY", settings.gemini_api_key),) if not v]
        if missing:
            raise RouterError("Missing required environment variable(s): " + ", ".join(missing))

    def require_cloudflare_config(self) -> None:
        missing = [n for n, v in (("CLOUDFLARE_API_TOKEN", settings.cloudflare_api_token), ("CLOUDFLARE_ACCOUNT_ID", settings.cloudflare_account_id)) if not v]
        if missing:
            raise RouterError("Missing required environment variable(s): " + ", ".join(missing))

    async def models(self) -> list[dict[str, Any]]:
        self.require_gemini_config()
        url = f"{settings.gemini_api_base}/models"
        try:
            response = await self.http.get(url, headers={"x-goog-api-key": settings.gemini_api_key}, params={"pageSize": 1000})
        except httpx.HTTPError as exc:
            raise RouterError(f"Gemini model discovery network error: {exc}") from exc
        if response.is_error:
            raise RouterError(f"Gemini model discovery failed ({response.status_code}): {response.text[:500]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RouterError("Gemini model discovery returned invalid JSON") from exc
        found = []
        for item in data.get("models", []):
            name = str(item.get("name", "")).removeprefix("models/")
            methods = item.get("supportedGenerationMethods", [])
            if not name or not any(method in methods for method in ("generateContent", "interactions")):
                continue
            found.append({
                "id": name,
                "display_name": item.get("displayName", name),
                "description": str(item.get("description", ""))[:500],
                "input_token_limit": item.get("inputTokenLimit"),
                "output_token_limit": item.get("outputTokenLimit"),
                "supported_generation_methods": methods,
                "supports_thinking": item.get("thinking"),
                "max_temperature": item.get("maxTemperature"),
                "top_p": item.get("topP"),
                "top_k": item.get("topK"),
            })
        if not found:
            found = [{"id": settings.default_model, "display_name": settings.default_model, "input_token_limit": None, "output_token_limit": None, "supported_generation_methods": [], "supports_thinking": None}]
        return found

    async def plan(self, request: dict[str, Any], models: list[dict[str, Any]], hints: dict[str, Any]) -> dict[str, Any]:
        self.require_cloudflare_config()
        prompt_template = settings.prompt_file.read_text(encoding="utf-8")
        quota_status = self.limits.status(models)
        context = {"available_models": models, "local_quota_status": quota_status["models"], "default_model": settings.default_model, "default_max_output_tokens": settings.default_max_output_tokens, "default_store": settings.default_store, "caller_hints": hints, "supported_tools": sorted(TOOLS), "supported_thinking_levels": list(ROUTABLE_THINKING)}
        safe_request = self._planner_safe_value(request)
        prompt = prompt_template.replace("{{ROUTING_CONTEXT}}", json.dumps(context, ensure_ascii=False)).replace("{{USER_REQUEST}}", json.dumps(safe_request, ensure_ascii=False))
        url = f"{settings.cloudflare_api_base}/accounts/{settings.cloudflare_account_id}/ai/run/{settings.planner_model}"
        try:
            response = await self.http.post(url, headers={"Authorization": f"Bearer {settings.cloudflare_api_token}"}, json={"messages": [{"role": "system", "content": "Return only the JSON object required by the supplied routing prompt."}, {"role": "user", "content": prompt}], "max_tokens": 700, "temperature": 0.1})
        except httpx.HTTPError as exc:
            raise RouterError(f"Cloudflare planner network error: {exc}") from exc
        if response.is_error:
            raise RouterError(f"Cloudflare planner failed ({response.status_code}): {response.text[:500]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RouterError("Cloudflare planner returned invalid JSON") from exc
        result = payload.get("result", {})
        text = result.get("response")
        if not text and isinstance(result.get("choices"), list) and result["choices"]:
            text = result["choices"][0].get("message", {}).get("content")
        if not text:
            raise RouterError("Cloudflare response had no planner text")
        return _json_object(text)

    async def run_gemini(self, body: dict[str, Any]) -> dict[str, Any]:
        self.require_gemini_config()
        for _ in range(9):
            try:
                response = await self.http.post(f"{settings.gemini_api_base}/interactions", headers={"x-goog-api-key": settings.gemini_api_key}, json=body)
            except httpx.HTTPError as exc:
                raise RouterError(f"Gemini request network error: {exc}") from exc
            if not response.is_error:
                try:
                    return response.json()
                except ValueError as exc:
                    raise RouterError("Gemini returned invalid JSON") from exc
            detail = response.text[:1000]
            if response.status_code == 400 and self._adjust_unsupported_config(body, detail):
                continue
            if response.status_code == 429:
                if "quota_exceeded" in detail.lower() or "daily" in detail.lower() or "requests per day" in detail.lower():
                    retry_at = self.limits.next_daily_reset()
                else:
                    try:
                        delay = max(1, int(response.headers.get("Retry-After", "60")))
                    except ValueError:
                        delay = 60
                    retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self.limits.pause(body.get("model", "unknown"), retry_at)
            raise RouterError(f"Gemini request failed ({response.status_code}): {detail}")
        raise RouterError("Gemini rejected every supported thinking-level fallback for this model")

    def _adjust_unsupported_config(self, body: dict[str, Any], detail: str) -> bool:
        message = detail.lower()
        normalized_message = message.replace("_", " ").replace("-", " ")
        unsupported = any(word in message for word in ("not supported", "unsupported", "not allowed", "unknown field", "unrecognized field"))
        if not unsupported:
            return False
        model = body.get("model", "unknown")
        generation = body.get("generation_config", {})
        if "thinking level" in normalized_message or "thinking_level" in message:
            current = generation.get("thinking_level")
            if not current and "thinking_level" not in generation:
                return False
            if current and current.lower() in message:
                self.limits.mark_thinking_level_unsupported(model, current)
                rejected = self.limits.unsupported_thinking_levels(model)
                order = list(ROUTABLE_THINKING)
                if current in order:
                    current_index = order.index(current)
                    choices = sorted((level for level in order if level != current), key=lambda level: (abs(order.index(level) - current_index), order.index(level)))
                else:
                    choices = order
                replacement = next((level for level in choices if level not in rejected), None)
                if replacement:
                    generation["thinking_level"] = replacement
                    return True
            generation.pop("thinking_level", None)
            self.limits.mark_thinking_level_parameter_unsupported(model)
            return True
        if "thinking summar" in message or "thinking_summaries" in message:
            if "thinking_summaries" in generation:
                generation.pop("thinking_summaries", None)
                return True
        for key in ("temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty", "seed", "stop_sequences", "response_mime_type", "response_schema"):
            camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key)
            if key in generation and (key.replace("_", " ") in normalized_message or camel.lower() in message):
                generation.pop(key, None)
                return True
        if "tool_choice" in message and body.pop("tool_choice", None) is not None:
            return True
        if "tool" in message and body.get("tools"):
            body.pop("tools", None)
            body.pop("tool_choice", None)
            return True
        return False

    @classmethod
    def _planner_safe_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            safe = {}
            for key, item in value.items():
                if key == "data" and isinstance(item, str):
                    safe["media_data"] = f"[omitted; encoded length {len(item)}]"
                elif key == "uri":
                    safe[key] = "[provided]"
                else:
                    safe[key] = cls._planner_safe_value(item)
            return safe
        if isinstance(value, list):
            return [cls._planner_safe_value(item) for item in value]
        return value

    @staticmethod
    def _contents_for_count(input_value: Any) -> list[dict[str, Any]]:
        blocks = input_value if isinstance(input_value, list) else [{"type": "text", "text": str(input_value)}]
        parts: list[dict[str, Any]] = []

        def add(block: Any) -> None:
            if not isinstance(block, dict):
                return
            kind = block.get("type")
            if kind == "user_input":
                for child in block.get("content", []):
                    add(child)
            elif kind == "text" and isinstance(block.get("text"), str):
                parts.append({"text": block["text"]})
            elif kind in {"image", "audio", "document", "video"}:
                mime = block.get("mime_type") or block.get("mimeType")
                if block.get("data") and mime:
                    parts.append({"inlineData": {"mimeType": mime, "data": block["data"]}})
                elif block.get("uri") and mime:
                    parts.append({"fileData": {"mimeType": mime, "fileUri": block["uri"]}})
            elif "parts" in block:
                for part in block.get("parts", []):
                    add(part)

        for block in blocks:
            add(block)
        if not parts:
            return []
        return [{"role": "user", "parts": parts}]

    @classmethod
    def _text_for_estimate(cls, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(cls._text_for_estimate(item) for item in value)
        if isinstance(value, dict):
            if value.get("type") == "text" and isinstance(value.get("text"), str):
                return value["text"]
            return " ".join(cls._text_for_estimate(item) for key, item in value.items() if key not in {"data", "uri"})
        return ""

    @classmethod
    def _contains_media(cls, value: Any) -> bool:
        if isinstance(value, list):
            return any(cls._contains_media(item) for item in value)
        if isinstance(value, dict):
            return value.get("type") in {"image", "audio", "document", "video", "media"} or any(cls._contains_media(item) for item in value.values())
        return False

    async def count_input_tokens(self, model: str, request: dict[str, Any]) -> tuple[int, bool]:
        contents = self._contents_for_count(request.get("input"))
        if contents:
            try:
                count_response = await self.http.post(
                    f"{settings.gemini_api_base}/models/{model}:countTokens",
                    headers={"x-goog-api-key": settings.gemini_api_key},
                    json={"generateContentRequest": {
                        "contents": contents,
                        **({"systemInstruction": {"parts": [{"text": request["system_instruction"]}]}} if isinstance(request.get("system_instruction"), str) else {}),
                    }},
                )
                if not count_response.is_error:
                    count = count_response.json().get("totalTokens")
                    if isinstance(count, int) and count >= 0:
                        return count, True
            except (httpx.HTTPError, ValueError):
                pass
        # Fallback is deliberately labeled as an estimate; media tokenization is model-specific.
        text = self._text_for_estimate(request.get("input", ""))
        if isinstance(request.get("system_instruction"), str):
            text += request["system_instruction"]
        return max(1, math.ceil(len(text) / 3)), False

    @staticmethod
    def _token_usage(result: dict[str, Any], fallback_input: int) -> tuple[int, int]:
        usage = result.get("usage") or result.get("usage_metadata") or result.get("usageMetadata") or {}
        input_tokens = usage.get("total_input_tokens", usage.get("input_tokens", usage.get("prompt_token_count", usage.get("promptTokenCount", fallback_input))))
        output_tokens = usage.get("total_output_tokens", usage.get("output_tokens", usage.get("candidates_token_count", usage.get("candidatesTokenCount", 0))))
        return int(input_tokens or fallback_input), int(output_tokens or 0)

    async def run(self, request: dict[str, Any], hints: dict[str, Any] | None = None) -> dict[str, Any]:
        models = await self.models()
        hints = hints or {}
        candidates = self.limits.available_models(models)
        if not candidates:
            raise RouterError("All discovered Gemini models are at a configured local quota or cooldown. Check smart-ai limits or reset local counters.")
        for candidate in candidates:
            candidate["known_unsupported_thinking_levels"] = sorted(self.limits.unsupported_thinking_levels(candidate["id"]))
            candidate["thinking_level_parameter_supported"] = not self.limits.thinking_level_parameter_unsupported(candidate["id"])
        plan = await self.plan(request, candidates, hints)
        model_ids = {m["id"] for m in candidates}
        model = plan.get("model")
        if not isinstance(model, str) or model not in model_ids:
            raise RouterError("Planner selected a model that is unavailable or excluded by the configured local quotas")
        model_info = next(m for m in candidates if m["id"] == model)
        input_tokens, exact_token_count = await self.count_input_tokens(model, request)
        if self.limits.configured_limits(model)["input_tokens_per_minute"] is not None and not exact_token_count:
            raise RouterError("Could not count this request's tokens exactly, so the configured input_tokens_per_minute quota cannot be enforced safely")
        input_limit = model_info.get("input_token_limit")
        output_limit = model_info.get("output_token_limit")
        if input_limit and not exact_token_count and self._contains_media(request.get("input")):
            raise RouterError("Gemini could not count this media input exactly, so smart_AI cannot safely verify the selected model's input token limit")
        if input_limit and input_tokens > int(input_limit):
            precision = "counted" if exact_token_count else "estimated"
            raise RouterError(f"Input is {input_tokens} {precision} tokens, above {model}'s input limit of {input_limit}")
        try:
            raw_max_tokens = plan.get("max_output_tokens", settings.default_max_output_tokens)
            raw_temperature = plan.get("temperature", 0.3)
            if isinstance(raw_max_tokens, bool) or isinstance(raw_temperature, bool):
                raise ValueError("boolean is not a numeric generation setting")
            max_tokens = int(raw_max_tokens)
            temperature = float(raw_temperature)
        except (ValueError, TypeError) as exc:
            raise RouterError("Planner returned invalid token or temperature settings") from exc
        if not 1 <= max_tokens <= 32768 or not 0 <= temperature <= 2:
            raise RouterError("Planner settings are outside permitted limits")
        effective_output_limit = min(int(output_limit), 32768) if output_limit else 32768
        max_tokens = min(max_tokens, effective_output_limit)
        model_max_temperature = model_info.get("max_temperature")
        if model_max_temperature is not None:
            temperature = min(temperature, float(model_max_temperature))
        level = plan.get("thinking_level", "medium")
        summaries = plan.get("thinking_summaries", "none")
        if not isinstance(level, str) or level not in THINKING:
            level = "low"
        if not isinstance(summaries, str) or summaries not in THOUGHT_SUMMARIES:
            summaries = "none"
        tool_names = plan.get("tools", [])
        if not isinstance(tool_names, list) or any(not isinstance(tool, str) or tool not in TOOLS for tool in tool_names):
            raise RouterError("Planner returned unsupported tools")
        # Interactions API accepts string or structured multimodal input. Preserve caller's content exactly.
        user_input = request.get("input")
        if user_input is None:
            raise RouterError("Request requires an 'input' field")
        generation_config = {"max_output_tokens": max_tokens, "temperature": temperature}
        known_unsupported_levels = set(model_info.get("known_unsupported_thinking_levels", []))
        available_levels = [candidate for candidate in ROUTABLE_THINKING if candidate not in known_unsupported_levels]
        if model_info.get("supports_thinking") is not False and model_info.get("thinking_level_parameter_supported", True):
            effective_level = level if level in available_levels else (available_levels[0] if available_levels else None)
            if effective_level:
                generation_config["thinking_level"] = effective_level
            if summaries == "auto":
                generation_config["thinking_summaries"] = summaries
        store_value = plan.get("store", settings.default_store)
        if not isinstance(store_value, bool):
            store_value = settings.default_store
        tool_choice = plan.get("tool_choice", "auto")
        if not isinstance(tool_choice, str) or tool_choice not in {"auto", "none"}:
            tool_choice = "auto"
        body: dict[str, Any] = {"model": model, "input": user_input, "store": store_value, "generation_config": generation_config}
        if request.get("system_instruction"):
            body["system_instruction"] = request["system_instruction"]
        if request.get("previous_interaction_id"):
            body["previous_interaction_id"] = request["previous_interaction_id"]
        if tool_names:
            body["tools"] = [{"type": t} for t in tool_names]
            body["tool_choice"] = tool_choice
        if request.get("tools"):
            # Caller-defined function tools pass through; planner cannot invent executable functions.
            if not isinstance(request["tools"], list):
                raise RouterError("tools must be a list")
            body["tools"] = body.get("tools", []) + request["tools"]
        # Optional explicit user overrides are narrow and deliberately cannot replace routing/model/input.
        overrides = request.get("generation_config", {})
        if overrides:
            if not isinstance(overrides, dict) or set(overrides) - {"temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty", "seed", "stop_sequences", "response_mime_type", "response_schema"}:
                raise RouterError("Unsupported generation_config override")
            body["generation_config"].update(overrides)
        reservation = self.limits.reserve(model, input_tokens, exact_token_count)
        result = await self.run_gemini(body)
        actual_input, actual_output = self._token_usage(result, input_tokens)
        self.limits.finish(reservation, actual_input, actual_output)
        self._record_usage(result)
        local_status = self.limits.status([model_info])["models"][0]
        effective_options = {"generation_config": body["generation_config"], "tools": body.get("tools", []), "tool_choice": body.get("tool_choice", "auto")}
        return {"id": result.get("id"), "status": result.get("status"), "model": model, "model_limits": {"input_tokens": input_limit, "output_tokens": output_limit}, "token_count": {"input_tokens": input_tokens, "exact": exact_token_count}, "effective_request_options": effective_options, "local_quota_status": local_status, "plan": plan, "output_text": self._output_text(result), "steps": result.get("steps", []), "usage": result.get("usage"), "raw": result}

    @staticmethod
    def _output_text(result: dict[str, Any]) -> str:
        chunks: list[str] = []
        for step in result.get("steps", []):
            if step.get("type") == "model_output":
                for block in step.get("content", []):
                    if block.get("type") == "text":
                        chunks.append(block.get("text", ""))
        return "\n".join(filter(None, chunks)) or str(result.get("output_text", ""))

    @staticmethod
    def _record_usage(result: dict[str, Any]) -> None:
        usage = result.get("usage") or result.get("usage_metadata")
        if not usage:
            return
        path = settings.usage_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"requests": 0, "usage": {}}
            data["requests"] = int(data.get("requests", 0)) + 1
            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    data.setdefault("usage", {})[key] = data.setdefault("usage", {}).get(key, 0) + value
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass


