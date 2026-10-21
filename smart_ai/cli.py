from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from .config import settings
from .limits import LimitManager
from .router import ModelUnavailableError, Providers, RouterError

app = typer.Typer(help="Smart Cloudflare-planned Gemini model router")


async def _send(request: dict, hints: dict, model_override: str | None = None) -> dict:
    providers = Providers()
    try:
        return await providers.run(request, hints, model_override=model_override)
    finally:
        await providers.close()


async def _send_fallbacks(request: dict, hints: dict, failed_model: str) -> dict:
    providers = Providers()
    try:
        return await providers.run_fallbacks(request, hints, failed_model)
    finally:
        await providers.close()


async def _model_and_limit_status() -> tuple[list[dict], dict]:
    providers = Providers()
    try:
        models = await providers.models()
        return models, LimitManager().status(models)
    finally:
        await providers.close()


@app.command()
def ask(prompt: str = typer.Argument(..., help="Prompt to send"), file: Optional[Path] = typer.Option(None, "--file", "-f", help="Attach an image, audio file, or PDF"), thinking_summaries: bool = typer.Option(False, "--thinking-summaries", help="Request Gemini thought summaries"), store: bool = typer.Option(False, "--store", help="Allow Gemini to store this interaction"), json_output: bool = typer.Option(False, "--json", help="Print complete routing and response JSON")) -> None:
    request = {"input": prompt}
    if file:
        import base64
        import mimetypes
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        media_type = "document" if mime == "application/pdf" else "image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else None
        if media_type is None:
            raise typer.BadParameter("--file supports images, audio, and PDF files")
        request["input"] = [{"type": "text", "text": prompt}, {"type": media_type, "mime_type": mime, "data": base64.b64encode(file.read_bytes()).decode("ascii")}]
    used_fallback = False
    try:
        result = asyncio.run(_send(request, {"thinking_summaries_requested": thinking_summaries, "store_requested": store}))
    except ModelUnavailableError as exc:
        try:
            hints = {"thinking_summaries_requested": thinking_summaries, "store_requested": store}
            if exc.requires_confirmation and not typer.confirm(f"{exc}\nTry another locally eligible model without another Cloudflare planning call?", default=False):
                typer.echo(f"smart_AI: {exc}", err=True)
                raise typer.Exit(code=1)
            result = asyncio.run(_send_fallbacks(request, hints, exc.model))
            used_fallback = True
        except (RouterError, OSError) as fallback_exc:
            typer.echo(f"smart_AI: {fallback_exc}", err=True)
            raise typer.Exit(code=1) from fallback_exc
    except RouterError as exc:
        typer.echo(f"smart_AI: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("output_text"):
            typer.echo(result["output_text"])
        else:
            typer.echo(json.dumps(result.get("raw", {}), ensure_ascii=False, indent=2))
        plan = result.get("plan") or {}
        active_tools = result.get("effective_request_options", {}).get("tools", [])
        search_enabled = "google_search" in plan.get("tools", []) or any(
            (tool.get("type") if isinstance(tool, dict) else tool) == "google_search"
            for tool in active_tools
        )
        route = f"model={result.get('model')} | Google Search={'on' if search_enabled else 'off'}"
        if used_fallback:
            route += " | fallback bypassed Cloudflare planning"
        typer.echo(f"\n[{route} | thinking={plan.get('thinking_level')} | usage={result.get('usage')} ]", err=True)
        request_text = request.get("input", "")
        if isinstance(request_text, list):
            request_text = " ".join(str(part.get("text", "")) for part in request_text if isinstance(part, dict))
        if not search_enabled and re.search(r"https?://", str(request_text), re.IGNORECASE):
            typer.echo("smart_AI: this request included a URL but Google Search was not enabled; the URL contents were not fetched.", err=True)


@app.command()
def serve(host: str = typer.Option("127.0.0.1", help="Bind address"), port: int = typer.Option(8000, help="Port")) -> None:
    uvicorn.run("smart_ai.api:app", host=host, port=port, reload=False)


@app.command()
def usage() -> None:
    path = settings.usage_file
    typer.echo(path.read_text(encoding="utf-8") if path.exists() else "No usage data recorded yet.")


@app.command()
def models() -> None:
    """List models visible to the Gemini key and their live token limits."""
    try:
        model_list, _ = asyncio.run(_model_and_limit_status())
    except RouterError as exc:
        typer.echo(f"smart_AI: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(model_list, ensure_ascii=False, indent=2))


@app.command()
def limits() -> None:
    """Show per-model RPM/TPM/RPD usage and reset times."""
    try:
        report = LimitManager().status()
    except RouterError as exc:
        typer.echo(f"smart_AI: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    def cell(current: int, cap: int | None) -> str:
        if cap is None:
            return f"{current:,} / —"
        return f"{current:,} / {cap:,}"

    def reset_text(value: str | None) -> str:
        if not value:
            return "—"
        then = datetime.fromisoformat(value)
        seconds = max(0, int((then - datetime.now(timezone.utc)).total_seconds()))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        return then.astimezone().strftime("%Y-%m-%d %H:%M %Z")

    rows = report.get("models", [])
    headers = ("Model", "RPM", "TPM", "RPD", "RPM reset", "TPM reset", "RPD reset")
    table = []
    for item in rows:
        current = item.get("current", {})
        caps = item.get("limits", {})
        resets = item.get("reset_at", {})
        table.append((item.get("display_name", item["model"]),
            cell(current.get("rpm", 0), caps.get("requests_per_minute")),
            cell(current.get("tpm", 0), caps.get("input_tokens_per_minute")),
            cell(current.get("rpd", 0), caps.get("requests_per_day")),
            reset_text(resets.get("rpm")), reset_text(resets.get("tpm")), reset_text(resets.get("rpd"))))
    widths = [max(len(headers[i]), *(len(row[i]) for row in table)) if table else len(headers[i]) for i in range(len(headers))]
    typer.echo("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    typer.echo("  ".join("─" * width for width in widths))
    for row in table:
        typer.echo("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    typer.echo(f"\nDaily reset timezone: {report.get('timezone')} (at midnight). Local tracking only; caps from: {report.get('limits_source')}")
    typer.echo(f"RPD reset at next local midnight; rolling RPM/TPM window releases from {report.get('state_file')} event timestamps.")
    typer.echo("All counts are local estimates; no Gemini request is made by this command.")


@app.command("reset-limits")
def reset_limits(model: Optional[str] = typer.Option(None, "--model", help="Reset only this Gemini model's local counters")) -> None:
    """Clear smart_AI's local counters; this does not reset Google's quota."""
    LimitManager().reset(model)
    scope = f"model {model}" if model else "all models"
    typer.echo(f"Reset local smart_AI counters for {scope}. Provider quotas are unchanged.")


if __name__ == "__main__":
    app()

