"""Claude Messages API client and settings for the LibreOffice extension.

No UNO imports here, so this module is unit-testable with a plain Python.
Raw HTTP (urllib) rather than the `anthropic` SDK: LibreOffice's bundled
Python has no pip, and an extension can't install packages into it.
"""

import json
import os
import urllib.error
import urllib.request

CLAUDE_CODE = "claude_code"   # the user's Claude Code install, signed in with their subscription
API = "api"                    # an Anthropic API key, billed per use

DEFAULT_SETTINGS = {
    "backend": CLAUDE_CODE,
    "api_key": "",
    "model": "",               # blank: Claude Code's default, or API_DEFAULT_MODEL for the API
    "claude_path": "",
    "effort": "medium",
    "max_tokens": 16000,
    "instructions_writer": "",    # standing instructions, kept separately per app like the M365 add-ins
    "instructions_calc": "",
    "instructions_impress": "",
    "instructions_draw": "",
    "track_changes": False,      # Writer: insert Claude's edits as tracked changes
    "native_sidebar": False,     # Windows: keep the sidebar's simple version instead of the full panel
    "base_url": "https://api.anthropic.com",
    "timeout_seconds": 300,
}

API_DEFAULT_MODEL = "claude-opus-5-5"
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1"]
MODEL_LABELS = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5",
                "claude-haiku-4-5": "Haiku 4.5", "claude-fable-5-1": "Fable 5.1"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EFFORT_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max"}


def supports_effort(model):
    """Haiku 4.5 has no effort control; everything current does."""
    return "haiku" not in (model or "")

# Models that accept the server-side refusal fallback ("default" mode).
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeError(Exception):
    pass


class Cancelled(Exception):
    pass


def load_settings(path):
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
            # 0.3 had one "extra_instructions" for both apps.
            old = stored.get("extra_instructions")
            if old and "instructions_writer" not in stored and "instructions_calc" not in stored:
                settings["instructions_writer"] = settings["instructions_calc"] = old
    except (OSError, ValueError):
        pass
    return settings


def save_settings(path, settings):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def resolve_api_key(settings):
    return (settings.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()


def build_request(settings, system, user_text, images=None):
    model = settings.get("model") or API_DEFAULT_MODEL
    body = {
        "model": model,
        "max_tokens": int(settings.get("max_tokens") or DEFAULT_SETTINGS["max_tokens"]),
        "system": system,
        "messages": [{"role": "user", "content": user_content(user_text, images)}],
    }
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    # Haiku 4.5 rejects the effort parameter.
    if settings.get("effort") and supports_effort(model):
        body["output_config"] = {"effort": settings["effort"]}
    if model in _FALLBACK_MODELS:
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = _FALLBACK_BETA
    return body, headers


def user_content(text, images):
    """The message: plain text, or the pictures followed by the text."""
    if not images:
        return text
    return [{"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}}
            for i in images] + [{"type": "text", "text": text}]


def parse_response(data):
    """Return (text, truncated) from a Messages API response body."""
    if data.get("stop_reason") == "refusal":
        details = data.get("stop_details") or {}
        reason = details.get("explanation") or details.get("category") or "no details given"
        raise ClaudeError("Claude declined this request (%s)." % reason)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text.strip(), data.get("stop_reason") == "max_tokens"


def ask(settings, system, user_text, cancel=None, images=None):
    """Ask through whichever connection is configured. Returns (text, truncated).

    cancel: optional threading.Event; Claude Code requests are stopped when it is set.
    images: optional [{"media_type": "image/png", "data": base64}] sent along, such as screenshots.
    """
    if settings.get("backend") == API:
        return ask_api(settings, system, user_text, images)
    import claude_cli
    return claude_cli.ask(settings, system, user_text, cancel, images)


def ask_api(settings, system, user_text, images=None):
    """Send one message to the Messages API. Returns (text, truncated). Raises ClaudeError."""
    key = resolve_api_key(settings)
    if not key:
        raise ClaudeError("No API key set. Open Claude > Settings and paste your Anthropic API key "
                          "(from console.anthropic.com), set ANTHROPIC_API_KEY, or switch the "
                          "connection to Claude Code to use your Claude subscription.")
    body, headers = build_request(settings, system, user_text, images)
    headers["x-api-key"] = key
    url = (settings.get("base_url") or DEFAULT_SETTINGS["base_url"]).rstrip("/") + "/v1/messages"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    # The reply comes all at once, so a long one sends nothing for minutes; allow for that.
    timeout = max(float(settings.get("timeout_seconds") or DEFAULT_SETTINGS["timeout_seconds"]), 900)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ClaudeError(_http_error_message(e)) from None
    except urllib.error.URLError as e:
        raise ClaudeError("Could not reach the Claude API: %s" % e.reason) from None
    except TimeoutError:
        raise ClaudeError("The request timed out after %d seconds." % timeout) from None
    return parse_response(data)


def _http_error_message(e):
    try:
        message = json.loads(e.read().decode("utf-8"))["error"]["message"]
    except Exception:
        message = e.reason
    hints = {
        401: "Invalid API key - check Claude > Settings.",
        403: "This API key isn't allowed to use that model.",
        429: "Rate limited - wait a moment and try again.",
        529: "The API is overloaded - try again shortly.",
    }
    hint = hints.get(e.code, "")
    return "Claude API error %d: %s%s" % (e.code, message, ("\n\n" + hint) if hint else "")
