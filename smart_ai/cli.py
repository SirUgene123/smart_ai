from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from .config import settings
from .limits import LimitManager
from .router import Providers, RouterError

app = typer.Typer(help="Smart Cloudflare-planned Gemini model router")


async def _send(request: dict, hints: dict) -> dict:
    providers = Providers()
    try:
        return await providers.run(request, hints)
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
    try:
        result = asyncio.run(_send(request, {"thinking_summaries_requested": thinking_summaries, "store_requested": store}))
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
        typer.echo(f"\n[{result.get('model')} | thinking={result.get('plan', {}).get('thinking_level')} | usage={result.get('usage')} ]", err=True)


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
    """Show local usage windows, configured quotas, and reset timezone."""
    try:
        _, report = asyncio.run(_model_and_limit_status())
    except RouterError as exc:
        typer.echo(f"smart_AI: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("reset-limits")
def reset_limits(model: Optional[str] = typer.Option(None, "--model", help="Reset only this Gemini model's local counters")) -> None:
    """Clear smart_AI's local counters; this does not reset Google's quota."""
    LimitManager().reset(model)
    scope = f"model {model}" if model else "all models"
    typer.echo(f"Reset local smart_AI counters for {scope}. Provider quotas are unchanged.")


if __name__ == "__main__":
    app()

