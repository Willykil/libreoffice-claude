"""Usage: the plan limits, rate limits and tokens shown in the panel's Usage sheet.

What can be known depends on the connection:
- Claude Code (subscription): Claude Code's own /usage data, asked for with its `get_usage`
  control request. No model call, so it costs nothing. Gives the 5-hour session and weekly
  limits (percent used and when they reset), per-model weekly limits and extra usage spend.
  The extension still never sees the user's Claude credentials; Claude Code does the asking.
- API key: the anthropic-ratelimit-* headers of each reply (requests and tokens left in the
  current minute), and the tokens each reply used, priced at the list prices below. The
  account's balance and monthly spend need an Admin key, so they're left to the Console.

No UNO imports, so this is unit-testable with a plain Python.
"""

import json
import subprocess
import sys
import tempfile
import threading
import time

# $ per million tokens: input, output, cache read. Cache writes cost 1.25x input.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}

RATE_HEADERS = ("requests", "tokens", "input-tokens", "output-tokens")

UPDATE_HELP = ("This version of Claude Code can't report plan usage. Update it (run  claude update  in a "
               "terminal) to see it here.")

_lock = threading.Lock()
_session = None
_rate_limits = None      # API: from the last reply's headers
_plan_status = None      # Claude Code: the last rate_limit_event (allowed / allowed_warning / rejected)


def reset():
    global _session, _rate_limits, _plan_status
    with _lock:
        _session = {"started": time.time(), "requests": 0, "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0, "priced": True}
        _rate_limits = None
        _plan_status = None


reset()


def cost(model, usage):
    """Dollar cost of one reply's usage, or None for a model without a known price."""
    price = PRICES.get(model or "")
    if price is None:
        return None
    inp, out, read = price
    return (usage.get("input_tokens", 0) * inp + usage.get("output_tokens", 0) * out
            + usage.get("cache_read_input_tokens", 0) * read
            + usage.get("cache_creation_input_tokens", 0) * inp * 1.25) / 1e6


def record(model, usage, cost_usd=None):
    """Add one reply's token usage to this LibreOffice session's totals."""
    if not isinstance(usage, dict):
        return
    if cost_usd is None:
        cost_usd = cost(model, usage)
    with _lock:
        s = _session
        s["requests"] += 1
        s["input_tokens"] += int(usage.get("input_tokens") or 0)
        s["output_tokens"] += int(usage.get("output_tokens") or 0)
        s["cache_read_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
        s["cache_write_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)
        if cost_usd is None:
            s["priced"] = False
        else:
            s["cost_usd"] += cost_usd


def rate_limits_from_headers(headers):
    """{"requests": {"limit", "remaining", "reset"}, "tokens": {...}, ...} from a reply's headers."""
    get = (lambda k: headers.get(k)) if hasattr(headers, "get") else (lambda k: None)
    out = {}
    for name in RATE_HEADERS:
        entry = {}
        for field in ("limit", "remaining"):
            value = get("anthropic-ratelimit-%s-%s" % (name, field))
            try:
                entry[field] = int(value)
            except (TypeError, ValueError):
                pass
        reset_at = get("anthropic-ratelimit-%s-reset" % name)
        if reset_at:
            entry["reset"] = reset_at
        if "limit" in entry:
            out[name] = entry
    retry = get("retry-after")
    if retry:
        out["retry_after"] = retry
    return out or None


def note_api_reply(headers, model, data):
    """After a Messages API reply (or a 429): remember its rate limits and add up its usage."""
    global _rate_limits
    limits = rate_limits_from_headers(headers)
    if limits:
        with _lock:
            _rate_limits = dict(limits, at=time.time())
    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
        record(data.get("model") or model, data["usage"])


def note_cli_output(stdout):
    """After a Claude Code run: its usage (from the result event) and plan status (rate_limit_event)."""
    global _plan_status
    for line in stdout.splitlines():
        if '"rate_limit_event"' not in line and '"result"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            with _lock:
                _plan_status = dict(event["rate_limit_info"], at=time.time())
        elif event.get("type") == "result" and isinstance(event.get("usage"), dict):
            # Claude Code works out what it would have cost on the API; a subscription isn't
            # billed that way, so it's kept only as a token count.
            model_usage = event.get("modelUsage") or {}
            model = next(iter(model_usage), "") if len(model_usage) == 1 else ""
            record(model, event["usage"], event.get("total_cost_usd"))


def session():
    with _lock:
        return dict(_session)


def api_snapshot():
    with _lock:
        return {"connection": "api", "session": dict(_session),
                "rate_limits": dict(_rate_limits) if _rate_limits else None}


# ------------------------------------------------------------------ Claude Code plan usage

PROBE_SECONDS = 25


def probe_command(exe):
    return [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]


def probe_message(request_id="usage"):
    return (json.dumps({"type": "control_request", "request_id": request_id,
                        "request": {"subtype": "get_usage", "skip_behaviors": True}}) + "\n").encode("utf-8")


def parse_probe(stdout, request_id="usage"):
    """The get_usage answer from Claude Code's output. Returns (response dict or None, error or None)."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "control_response":
            continue
        resp = event.get("response") or {}
        if resp.get("request_id") not in (request_id, None):
            continue
        if resp.get("subtype") == "success" and isinstance(resp.get("response"), dict):
            return resp["response"], None
        error = str(resp.get("error") or "unknown error")
        if "unsupported" in error.lower() or "unknown" in error.lower():
            return None, UPDATE_HELP
        return None, "Claude Code couldn't get your plan usage: " + error[:300]
    return None, None


def plan_rows(rate_limits):
    """The plan's meters as [{label, group, percent, resets_at, severity}], in display order."""
    if not isinstance(rate_limits, dict):
        return []
    rows = []
    # Newer Claude Code passes the server's own rows through; use them verbatim when present.
    if isinstance(rate_limits.get("limits"), list):
        for r in rate_limits["limits"]:
            if not isinstance(r, dict) or r.get("percent") is None:
                continue
            scope = r.get("scope") or {}
            name = ((scope.get("model") or {}).get("display_name")
                    or (scope.get("surface") or {}).get("display_name") or "")
            kind = r.get("kind") or ""
            label = {"session": "Current session", "weekly_all": "All models"}.get(kind) or name or kind
            rows.append({"label": label, "group": r.get("group") or "", "percent": r["percent"],
                         "resets_at": r.get("resets_at"), "severity": r.get("severity") or ""})
        if rows:
            return rows
    windows = (("five_hour", "Current session", "session"), ("seven_day", "All models", "weekly"),
               ("seven_day_opus", "Opus", "weekly"), ("seven_day_sonnet", "Sonnet", "weekly"))
    for key, label, group in windows:
        w = rate_limits.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            rows.append({"label": label, "group": group, "percent": w["utilization"],
                         "resets_at": w.get("resets_at"), "severity": ""})
    for m in rate_limits.get("model_scoped") or []:
        if isinstance(m, dict) and m.get("utilization") is not None:
            rows.append({"label": m.get("display_name") or "Model", "group": "weekly",
                         "percent": m["utilization"], "resets_at": m.get("resets_at"), "severity": ""})
    return rows


def extra_usage(rate_limits):
    extra = (rate_limits or {}).get("extra_usage") if isinstance(rate_limits, dict) else None
    if not isinstance(extra, dict) or not extra.get("is_enabled"):
        return None
    return {"used": extra.get("used_credits"), "limit": extra.get("monthly_limit"),
            "percent": extra.get("utilization"), "currency": extra.get("currency") or "USD"}


def cli_snapshot(settings):
    """Plan usage through the user's Claude Code, plus this session's totals."""
    import claude_cli
    from claude_api import ClaudeError
    out = {"connection": "subscription", "session": session(), "plan": None, "error": None}
    with _lock:
        out["status"] = dict(_plan_status) if _plan_status else None
    exe = claude_cli.find_claude(settings)
    if not exe:
        out["error"] = "Claude Code isn't installed, or LibreOffice can't find it (see Settings)."
        return out
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(probe_command(exe), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=tempfile.gettempdir(), creationflags=flags)
        stdout, stderr = claude_cli._communicate(proc, probe_message(), PROBE_SECONDS, None, PROBE_SECONDS)
    except OSError as e:
        out["error"] = "Couldn't start Claude Code (%s): %s" % (exe, e)
        return out
    except ClaudeError:
        out["error"] = "Claude Code didn't answer in time; try again."
        return out
    text = stdout.decode("utf-8", "replace")
    usage, error = parse_probe(text)
    if usage is None:
        detail = (stderr.decode("utf-8", "replace") or text).strip()
        low = detail.lower()
        if error:
            out["error"] = error
        elif "login" in low or "log in" in low or "authenticat" in low:
            out["error"] = claude_cli.LOGIN_HELP
        else:
            out["error"] = UPDATE_HELP
        return out
    out["subscription"] = usage.get("subscription_type")
    if usage.get("rate_limits_available") and usage.get("rate_limits"):
        out["plan"] = plan_rows(usage["rate_limits"])
        out["extra"] = extra_usage(usage["rate_limits"])
    elif not usage.get("subscription_type"):
        out["error"] = ("Claude Code is signed in with an API key or another provider, not a Claude "
                        "subscription, so there are no plan limits to show.")
    else:
        out["error"] = "Your plan's usage isn't available right now; try again in a moment."
    return out


def snapshot(settings):
    import claude_api
    if settings.get("backend") == claude_api.API:
        return api_snapshot()
    return cli_snapshot(settings)
