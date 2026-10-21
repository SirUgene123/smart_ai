# smart_AI

Python CLI and HTTP API that asks Cloudflare Workers AI to choose a Gemini model and request configuration, validates that decision against Gemini's live model list, then sends the original request to Google's Gemini Interactions API.

## Credential setup

The API keys/tokens pasted into the chat should be treated as compromised. Revoke/rotate the Gemini API key, Cloudflare API token, and R2 access key/secret in their respective dashboards before using the app. R2 credentials are not used by this router. Do not put credentials in source control.

Copy `.env.example` to `.env` and set:

- `GEMINI_API_KEY`: rotated Gemini key.
- `CLOUDFLARE_API_TOKEN`: token with Workers AI permissions.
- `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account ID. The account ID in the provided R2 endpoint appears to be `fba6948f3382fdb7b4a79cc243820d89`; confirm it in your dashboard.

## Install and run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
# Edit .env with newly rotated values
smart-ai ask "Explain how a hash table works"
smart-ai ask "Describe this picture" --file .\picture.jpg
smart-ai ask "Solve this carefully" --thinking-summaries --json
smart-ai-gui
smart-ai models
smart-ai limits
smart-ai serve
```

The API listens on `127.0.0.1:8000` by default. Keep it on localhost unless you put authentication and TLS in front of it.

## HTTP API

`POST /v1/route`

```json
{
  "input": "Explain how a hash table works",
  "hints": {"max_cost": "low", "latency": "normal"}
}
```

Multimodal input can use Gemini Interactions API input blocks. A message with media can be represented as:

```json
{
  "input": [
    {"type":"text","text":"Describe this"},
    {"type":"image","mime_type":"image/jpeg","data":"BASE64_DATA"}
  ]
}
```

Use `system_instruction`, `previous_interaction_id`, `tools` for caller-defined function declarations, and a restricted `generation_config` override for supported output settings. `GET /v1/models` lists Gemini models visible to the configured key. `GET /v1/limits` returns local quota status; `POST /v1/limits/reset` clears local counters (optionally pass `{"model":"gemini-model-id"}`). `GET /health` is a liveness check. Interactive API docs are at `/docs`.

## Excel prompt runner GUI

Run `smart-ai-gui` (or `python -m smart_ai.gui`) to open the desktop prompt runner. Choose an `.xlsx`, `.xlsm`, or `.csv` file, enter a prompt containing a placeholder such as `##`, and select the delay between requests. The app reads non-empty cells in worksheet order, then row and column order (or CSV row and column order), replaces every placeholder occurrence with that cell's value, and sends one request at a time through the normal smart_AI router. Results can be saved as separate numbered `.txt` files. The Stop button finishes the in-flight request and then stops the queue. Keep the `.env` credentials configured as described above.

If Gemini reports temporary model unavailability or rate limiting (HTTP 429/503), the CLI and GUI ask before switching. Timeouts, transient Gemini server errors, local per-model quota or input-limit errors, and Cloudflare planner network failures trigger automatic fallback. The app tries at most three other models that pass the configured local limits, preferring newer text-capable Flash Lite and Flash models, and skips Cloudflare planning for each attempt. TTS, live, embedding, and image-generation models are excluded from text fallbacks, and a model-specific 404/410 is skipped for the rest of the process. In the GUI, a successful fallback is reused for the remaining rows in that run; if it stops working, the app tries other eligible models again. Default request timeouts are 30 seconds for Gemini, 8 seconds for Gemini model discovery, and 15 seconds for Cloudflare planning. Configure them with `GEMINI_REQUEST_TIMEOUT_SECONDS`, `GEMINI_DISCOVERY_TIMEOUT_SECONDS`, and `CLOUDFLARE_TIMEOUT_SECONDS` respectively; `HTTP_TIMEOUT_SECONDS` can lower these values.

## Model limits and quota tracking

`smart-ai models` fetches models visible to the Gemini key and shows each model's input/output token ceilings, supported generation methods, thinking support, and generation parameter metadata. Request quota checks do not call Gemini's `countTokens` endpoint: smart_AI estimates text and media token use locally, checks the configured rolling RPM/TPM/RPD windows, and labels the estimate as inexact. This trades exact preflight tokenization for offline quota accounting; media estimates are especially approximate. After the normal Gemini generation request, returned usage metadata replaces the estimate in local history when available. The router also uses its discovered model metadata to cap `max_output_tokens` at the model's output ceiling.

If Gemini rejects a model-specific thinking level or another optional setting, smart_AI retries only when the 400 error explicitly identifies an unsupported option. For thinking-level errors it tries another level, then omits the setting if needed, and remembers rejected levels per model in local state. The response includes `effective_request_options` so callers can see the settings and tools that Gemini accepted.

Gemini does not return your project's active RPM, input TPM, or daily quota in the model list. Those project-specific limits are shown in Google AI Studio. To enforce them locally, copy `quota_limits.example.json` to `~/.smart_ai/quotas.json` and enter the values shown for your project/model:

```powershell
New-Item -ItemType Directory -Force "$HOME\.smart_ai" | Out-Null
Copy-Item .\quota_limits.example.json "$HOME\.smart_ai\quotas.json"
```

Use exact model IDs from `smart-ai models`. Set `timezone` to `America/Los_Angeles` for Gemini's documented daily quota reset at midnight Pacific. Configured requests-per-minute and input-tokens-per-minute limits use a rolling 60-second window. Daily counters roll over automatically at midnight in the configured timezone. Usage state stores model IDs, timestamps, and token/request counts in `~/.smart_ai/limit_state.json`.

`smart-ai limits` displays a per-model RPM/TPM/RPD table with locally tracked current counts and estimated rolling-window/daily reset times. The command and `GET /v1/limits` read local files only and make no provider requests; the API provides the same data as JSON (`current`, `limits`, `reset_at`). The attached quota snapshot is bundled as the app's starting configuration; create `~/.smart_ai/quotas.json` to override it for your project. Overrides are merged with bundled values, and `0` means that quota is unavailable for the model. The daily reset timezone defaults to `America/Los_Angeles`.

Local current counts include only requests routed through this smart_AI installation. They cannot import the live current usage shown in Google AI Studio or count activity from other apps, so reset times are estimates based on this installation's request timestamps. TPM reset indicates when the rolling input-token window next frees capacity; RPM reset is when the oldest request expires. `smart-ai reset-limits` (or `POST /v1/limits/reset`) clears only smart_AI's local counters and cooldowns; it cannot reset Google's quota. Gemini 429 responses temporarily pause the affected model locally, until its retry time or the next daily reset for daily-quota errors. Google AI Studio remains the source of truth for project-wide usage.

The Cloudflare decision prompt is in `smart_ai/prompts/router.md`. Edit that file to tune routing policy. The planner's model is configurable via `CLOUDFLARE_PLANNER_MODEL`.

## Billing and privacy

Gemini's public model discovery endpoint does not expose a reliable remaining-credit balance or your project-specific rate quotas. The router records numeric usage fields returned by Gemini in `~/.smart_ai/usage.json`; this is usage telemetry, not a remaining-credit estimate or a billing statement. Check Google AI Studio for actual limits/billing. Cloudflare planner usage is not included in Gemini usage.

Interactions may be stored by the provider if `store=true`. The router defaults storage off. Thought summaries are off unless requested using `--thinking-summaries`; summaries are not full internal reasoning. Provider errors are returned without exposing credentials.


