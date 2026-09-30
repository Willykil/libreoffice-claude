"""Ask Claude through the user's own Claude Code install (`claude -p`).

Claude Code runs signed in with the user's Claude subscription, so requests
count against their plan instead of a separately billed API key. The
extension never sees or stores the user's Claude credentials; it only runs
the unmodified `claude` program and reads its answer.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from claude_api import Cancelled, ClaudeError, supports_effort

INSTALL_HELP = (
    "Claude Code isn't installed (or LibreOffice can't find it).\n\n"
    "To use your Claude subscription:\n"
    "1. Open PowerShell and run:  irm https://claude.ai/install.ps1 | iex\n"
    "2. Then run:  claude   and sign in with your Claude account.\n"
    "3. Restart LibreOffice.\n\n"
    "If it's installed somewhere unusual, set its path in Claude > Settings.")

LOGIN_HELP = ("Claude Code isn't signed in. Open a terminal, run  claude  and sign in "
              "with your Claude account, then try again.")


def find_claude(settings):
    configured = (settings.get("claude_path") or "").strip()
    if configured:
        return configured if os.path.isfile(configured) else None
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(home, ".claude", "local", "claude"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    return next((c for c in candidates if os.path.isfile(c)), None)


def build_command(exe, settings, system_file):
    cmd = [exe, "-p",
           "--output-format", "json",
           "--system-prompt-file", system_file,
           # A plain question-and-answer: no file, shell or MCP access.
           "--tools", "",
           "--disallowedTools", "mcp__*",
           "--strict-mcp-config",
           "--disable-slash-commands",
           "--no-session-persistence"]
    model = (settings.get("model") or "").strip()
    if model:
        cmd += ["--model", model]
    if settings.get("effort") and supports_effort(model):
        cmd += ["--effort", settings["effort"]]
    return cmd


def ask(settings, system, user_text, cancel=None):
    """Returns (text, truncated). Raises ClaudeError, or Cancelled if `cancel` gets set."""
    exe = find_claude(settings)
    if not exe:
        raise ClaudeError(INSTALL_HELP)
    timeout = float(settings.get("timeout_seconds") or 300)
    fd, system_file = tempfile.mkstemp(prefix="claude-lo-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system)
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(build_command(exe, settings, system_file), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=tempfile.gettempdir(), creationflags=flags)
        except OSError as e:
            raise ClaudeError("Couldn't start Claude Code (%s): %s" % (exe, e)) from None
        stdout, stderr = _communicate(proc, user_text.encode("utf-8"), timeout, cancel)
    finally:
        try:
            os.remove(system_file)
        except OSError:
            pass
    return parse_output(proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"))


def _communicate(proc, data, timeout, cancel):
    """proc.communicate, but give up on timeout or when `cancel` (a threading.Event) is set."""
    deadline = time.time() + timeout
    while True:
        try:
            return proc.communicate(data, timeout=0.2)
        except subprocess.TimeoutExpired:
            data = None          # already sent; retries must not pass it again
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                raise Cancelled() from None
            if time.time() > deadline:
                proc.kill()
                proc.communicate()
                raise ClaudeError("Claude Code didn't answer within %d seconds." % timeout) from None


def parse_output(returncode, stdout, stderr):
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        detail = (stderr or stdout).strip() or "exit code %d" % returncode
        raise ClaudeError(_explain(detail))
    text = (data.get("result") or "").strip()
    if data.get("is_error") or returncode != 0:
        raise ClaudeError(_explain(text or stderr.strip() or data.get("subtype") or "unknown error"))
    return text, False


def _explain(detail):
    low = detail.lower()
    if "login" in low or "log in" in low or "not logged" in low or "authenticat" in low:
        return LOGIN_HELP + "\n\n(" + detail[:300] + ")"
    if "limit" in low:
        return "Your Claude plan's usage limit was reached:\n\n" + detail[:500]
    return "Claude Code error:\n\n" + detail[:1000]
