"""Claude Messages API client and settings for the LibreOffice extension.

No UNO imports here, so this module is unit-testable with a plain Python.
Raw HTTP (urllib) rather than the `anthropic` SDK: LibreOffice's bundled
Python has no pip, and an extension can't install packages into it.
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_SETTINGS = {
    "api_key": "",
    "model": "claude-opus-5-5",
    "effort": "medium",
    "max_tokens": 16000,
    "extra_instructions": "",
    "base_url": "https://api.anthropic.com",
    "timeout_seconds": 300,
}

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Models that accept the server-side refusal fallback ("default" mode).
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeError(Exception):
    pass


def load_settings(path):
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
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


def build_request(settings, system, user_text):
    model = settings.get("model") or DEFAULT_SETTINGS["model"]
    body = {
        "model": model,
        "max_tokens": int(settings.get("max_tokens") or DEFAULT_SETTINGS["max_tokens"]),
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    # Haiku 4.5 rejects the effort parameter.
    if settings.get("effort") and not model.startswith("claude-haiku"):
        body["output_config"] = {"effort": settings["effort"]}
    if model in _FALLBACK_MODELS:
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = _FALLBACK_BETA
    return body, headers


def parse_response(data):
    """Return (text, truncated) from a Messages API response body."""
    if data.get("stop_reason") == "refusal":
        details = data.get("stop_details") or {}
        reason = details.get("explanation") or details.get("category") or "no details given"
        raise ClaudeError("Claude declined this request (%s)." % reason)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text.strip(), data.get("stop_reason") == "max_tokens"


def ask(settings, system, user_text):
    """Send one message to Claude. Returns (text, truncated). Raises ClaudeError."""
    key = resolve_api_key(settings)
    if not key:
        raise ClaudeError("No API key set. Open Claude > Settings and paste your Anthropic API key "
                          "(from console.anthropic.com), or set ANTHROPIC_API_KEY.")
    body, headers = build_request(settings, system, user_text)
    headers["x-api-key"] = key
    url = (settings.get("base_url") or DEFAULT_SETTINGS["base_url"]).rstrip("/") + "/v1/messages"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    timeout = float(settings.get("timeout_seconds") or DEFAULT_SETTINGS["timeout_seconds"])
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
