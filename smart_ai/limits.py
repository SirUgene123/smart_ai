from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .errors import RouterError


class LimitManager:
    """Local conservative quota tracker; provider quotas are configured by the user."""

    _lock = threading.RLock()

    def __init__(self) -> None:
        self.config_path = settings.quota_file
        self.state_path = settings.limit_state_file

    def _config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"timezone": "America/Los_Angeles", "defaults": {}, "models": {}}
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("root must be an object")
            return value
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RouterError(f"Cannot read quota configuration {self.config_path}: {exc}") from exc

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(str(self._config().get("timezone", "America/Los_Angeles")))
        except Exception as exc:
            raise RouterError("Quota timezone must be a valid IANA timezone") from exc

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"events": [], "blocked_until": {}, "unsupported_thinking_levels": {}, "unsupported_thinking_models": []}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("state root must be an object")
            state.setdefault("events", [])
            state.setdefault("blocked_until", {})
            state.setdefault("unsupported_thinking_levels", {})
            state.setdefault("unsupported_thinking_models", [])
            return state
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RouterError(f"Cannot read local quota state {self.state_path}: {exc}") from exc

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            raise RouterError(f"Cannot write local quota state {self.state_path}: {exc}") from exc

    def _limits_for(self, model: str, config: dict[str, Any] | None = None) -> dict[str, int | None]:
        config = config or self._config()
        defaults = config.get("defaults") or {}
        models = config.get("models") or {}
        if not isinstance(defaults, dict) or not isinstance(models, dict):
            raise RouterError("Quota config 'defaults' and 'models' values must be JSON objects")
        per_model = models.get(model, {}) or {}
        if not isinstance(per_model, dict):
            raise RouterError(f"Quota settings for {model} must be a JSON object")
        names = ("requests_per_minute", "input_tokens_per_minute", "requests_per_day")
        limits: dict[str, int | None] = {}
        for name in names:
            raw = per_model.get(name, defaults.get(name))
            if raw is None:
                limits[name] = None
            elif isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                limits[name] = raw
            else:
                raise RouterError(f"Quota '{name}' for {model} must be a positive integer or null")
        return limits

    def configured_limits(self, model: str) -> dict[str, int | None]:
        return self._limits_for(model)

    def _active_events(self, state: dict[str, Any], model: str | None = None) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=1)
        zone = self._zone()
        local_day = now.astimezone(zone).date()
        active = []
        for event in state.get("events", []):
            try:
                at = datetime.fromisoformat(event["at"])
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            if model and event.get("model") != model:
                continue
            if now - timedelta(days=2) <= at <= now and (at >= since or at.astimezone(zone).date() == local_day):
                active.append(event)
        return active

    def status(self, models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        with self._lock:
            config = self._config()
            state = self._read_state()
            now = datetime.now(timezone.utc)
            zone = self._zone()
            local_day = now.astimezone(zone).date()
            records = []
            ids = [m["id"] for m in models or []]
            ids.extend(m for m in config.get("models", {}) if m not in ids)
            for model in ids:
                limits = self._limits_for(model, config)
                events = self._active_events(state, model)
                minute_events = [e for e in events if datetime.fromisoformat(e["at"]).astimezone(timezone.utc) >= now - timedelta(minutes=1)]
                day_events = [e for e in events if datetime.fromisoformat(e["at"]).astimezone(zone).date() == local_day]
                blocked = state.get("blocked_until", {}).get(model)
                blocked_active = False
                if blocked:
                    try:
                        blocked_active = datetime.fromisoformat(blocked) > now
                    except ValueError:
                        pass
                records.append({"model": model, "limits": limits, "used_last_minute": {"requests": len(minute_events), "input_tokens": sum(int(e.get("input_tokens", 0)) for e in minute_events)}, "used_today": {"requests": len(day_events), "input_tokens": sum(int(e.get("input_tokens", 0)) for e in day_events), "output_tokens": sum(int(e.get("output_tokens", 0)) for e in day_events)}, "blocked_until": blocked if blocked_active else None})
            return {"timezone": str(zone), "daily_reset": "midnight local time in configured timezone (default America/Los_Angeles)", "limits_source": str(self.config_path), "state_file": str(self.state_path), "provider_limits_note": "Per-project Gemini RPM/TPM/RPD limits are not exposed by model discovery. Configure them from Google AI Studio; only model input/output token ceilings are automatic.", "models": records}

    def available_models(self, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        config = self._config()
        state = self._read_state()
        candidates = []
        for model in models:
            model_id = model["id"]
            limits = self._limits_for(model_id, config)
            events = self._active_events(state, model_id)
            minute_events = [e for e in events if datetime.fromisoformat(e["at"]).astimezone(timezone.utc) >= now - timedelta(minutes=1)]
            day_events = [e for e in events if datetime.fromisoformat(e["at"]).astimezone(self._zone()).date() == now.astimezone(self._zone()).date()]
            blocked = state.get("blocked_until", {}).get(model_id)
            blocked_active = bool(blocked and datetime.fromisoformat(blocked) > now)
            if blocked_active:
                continue
            if limits["requests_per_minute"] is not None and len(minute_events) >= limits["requests_per_minute"]:
                continue
            if limits["requests_per_day"] is not None and len(day_events) >= limits["requests_per_day"]:
                continue
            candidates.append(model)
        return candidates

    def reserve(self, model: str, input_tokens: int, exact: bool = False) -> str:
        with self._lock:
            config = self._config()
            state = self._read_state()
            limits = self._limits_for(model, config)
            events = self._active_events(state, model)
            now = datetime.now(timezone.utc)
            minute_events = [e for e in events if datetime.fromisoformat(e["at"]).astimezone(timezone.utc) >= now - timedelta(minutes=1)]
            day_events = [e for e in events if datetime.fromisoformat(e["at"]).astimezone(self._zone()).date() == now.astimezone(self._zone()).date()]
            blocked = state.get("blocked_until", {}).get(model)
            if blocked and datetime.fromisoformat(blocked) > now:
                raise RouterError(f"Model {model} is locally paused until {blocked}")
            rpd = limits["requests_per_day"]
            rpm = limits["requests_per_minute"]
            tpm = limits["input_tokens_per_minute"]
            if rpd is not None and len(day_events) >= rpd:
                raise RouterError(f"Model {model} reached its configured requests_per_day quota; local daily counters reset at midnight {self._zone()}")
            if rpm is not None and len(minute_events) >= rpm:
                raise RouterError(f"Model {model} reached its configured requests_per_minute quota; retry after the rolling 60-second window")
            if tpm is not None and sum(int(e.get("input_tokens", 0)) for e in minute_events) + input_tokens > tpm:
                raise RouterError(f"Model {model} would exceed its configured input_tokens_per_minute quota")
            event_id = uuid.uuid4().hex
            state.setdefault("events", []).append({"id": event_id, "model": model, "at": now.isoformat(), "input_tokens": max(0, input_tokens), "output_tokens": 0, "input_tokens_exact": exact})
            state["events"] = [e for e in state["events"] if self._recent(e.get("at"), now)]
            self._write_state(state)
            return event_id

    @staticmethod
    def _recent(value: str | None, now: datetime) -> bool:
        try:
            at = datetime.fromisoformat(value or "")
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return at >= now - timedelta(days=2)
        except ValueError:
            return False

    def finish(self, event_id: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            state = self._read_state()
            for event in state.get("events", []):
                if event.get("id") == event_id:
                    event["input_tokens"] = max(0, int(input_tokens))
                    event["output_tokens"] = max(0, int(output_tokens))
                    break
            self._write_state(state)

    def pause(self, model: str, until: datetime) -> None:
        with self._lock:
            state = self._read_state()
            state.setdefault("blocked_until", {})[model] = until.astimezone(timezone.utc).isoformat()
            self._write_state(state)

    def next_daily_reset(self) -> datetime:
        zone = self._zone()
        local_now = datetime.now(zone)
        next_day = local_now.date() + timedelta(days=1)
        return datetime.combine(next_day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)

    def unsupported_thinking_levels(self, model: str) -> set[str]:
        with self._lock:
            state = self._read_state()
            return set(state.get("unsupported_thinking_levels", {}).get(model, []))

    def mark_thinking_level_unsupported(self, model: str, level: str) -> None:
        with self._lock:
            state = self._read_state()
            levels = set(state.setdefault("unsupported_thinking_levels", {}).get(model, []))
            levels.add(level)
            state["unsupported_thinking_levels"][model] = sorted(levels)
            self._write_state(state)

    def thinking_level_parameter_unsupported(self, model: str) -> bool:
        with self._lock:
            return model in self._read_state().get("unsupported_thinking_models", [])

    def mark_thinking_level_parameter_unsupported(self, model: str) -> None:
        with self._lock:
            state = self._read_state()
            models = set(state.setdefault("unsupported_thinking_models", []))
            models.add(model)
            state["unsupported_thinking_models"] = sorted(models)
            self._write_state(state)

    def reset(self, model: str | None = None) -> None:
        with self._lock:
            state = self._read_state()
            if model:
                state["events"] = [e for e in state.get("events", []) if e.get("model") != model]
                state.get("blocked_until", {}).pop(model, None)
            else:
                state["events"] = []
                state["blocked_until"] = {}
            self._write_state(state)
