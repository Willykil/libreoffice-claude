"""Ask Claude through the user's own Claude Code install (`claude -p`).

Claude Code runs signed in with the user's Claude subscription, so requests
count against their plan instead of a separately billed API key. The
extension never sees or stores the user's Claude credentials; it only runs
the unmodified `claude` program and reads its answer.
"""

import glob
import json
import os
import re
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
    "Already installed? In PowerShell,  where.exe claude  shows where it is; put that path in "
    "Settings > Advanced > Claude Code location.")

LOGIN_HELP = ("Claude Code isn't signed in. Open a terminal, run  claude  and sign in "
              "with your Claude account, then try again.")


def find_claude(settings):
    configured = (settings.get("claude_path") or "").strip()
    if configured:
        return configured if os.path.isfile(configured) else None
    found = shutil.which("claude")
    if found:
        return found
    # LibreOffice keeps the PATH it started with, which can predate installing Claude Code
    # (Windows only hands the new PATH to programs started afterwards), so read the current one.
    path = _registry_path()
    found = shutil.which("claude", path=path) if path else None
    return found or next((c for c in candidates() if os.path.isfile(c)), None)


def candidates():
    home = os.path.expanduser("~")
    appdata, local = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
    out = [
        os.path.join(home, ".local", "bin", "claude.exe"),       # the native installer
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(home, ".claude", "local", "claude.exe"),
        os.path.join(home, ".claude", "local", "claude"),
        os.path.join(appdata, "npm", "claude.cmd"),               # npm install -g
        os.path.join(local, "Microsoft", "WinGet", "Links", "claude.exe"),
        os.path.join(local, "Programs", "claude", "claude.exe"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    # The copy of Claude Code that the Claude desktop app keeps, newest first.
    bundled = glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe")) if appdata else []
    bundled += glob.glob(os.path.join(home, "Library", "Application Support", "Claude", "claude-code", "*", "claude"))
    return out + sorted(bundled, key=_version_key, reverse=True)


def _version_key(path):
    version = os.path.basename(os.path.dirname(path))
    return [int(n) for n in re.findall(r"\d+", version)]


def _registry_path():
    if sys.platform != "win32":
        return None
    import winreg
    parts = []
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        except OSError:
            pass
    return os.pathsep.join(parts) or None


def build_command(exe, settings, system_file):
    cmd = [exe, "-p",
           # Streamed, so a long answer shows it's still coming instead of looking stuck.
           "--output-format", "stream-json", "--verbose", "--include-partial-messages",
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


# Claude Code gets this long without printing anything before we give up on it; a long
# answer keeps streaming, so it can run for up to MAX_SECONDS.
MAX_SECONDS = 30 * 60


def ask(settings, system, user_text, cancel=None):
    """Returns (text, truncated). Raises ClaudeError, or Cancelled if `cancel` gets set."""
    exe = find_claude(settings)
    if not exe:
        raise ClaudeError(INSTALL_HELP)
    idle = float(settings.get("timeout_seconds") or 300)
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
        stdout, stderr = _communicate(proc, user_text.encode("utf-8"), idle, cancel)
    finally:
        try:
            os.remove(system_file)
        except OSError:
            pass
    return parse_output(proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"))


def _communicate(proc, data, idle, cancel, max_seconds=None):
    """Feed stdin, collect stdout/stderr. Give up when nothing arrives for `idle` seconds, after
    max_seconds in all, or when `cancel` (a threading.Event) is set."""
    import threading
    out, err = [], []
    last = [time.time()]

    def feed():
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except OSError:
            pass

    def read(stream, into):
        for chunk in iter(lambda: stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536), b""):
            into.append(chunk)
            last[0] = time.time()

    workers = [threading.Thread(target=feed, daemon=True),
               threading.Thread(target=read, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=read, args=(proc.stderr, err), daemon=True)]
    for w in workers:
        w.start()
    start = time.time()
    limit = max_seconds or MAX_SECONDS
    while proc.poll() is None or workers[1].is_alive() or workers[2].is_alive():
        if proc.poll() is not None:
            workers[1].join(0.2)
            workers[2].join(0.2)
            continue
        problem = None
        if cancel is not None and cancel.is_set():
            problem = Cancelled()
        elif time.time() - last[0] > idle:
            problem = ClaudeError("Claude Code stopped responding (nothing for %d seconds)." % idle)
        elif time.time() - start > limit:
            problem = ClaudeError("Claude Code was still answering after %d minutes; ask for less at once."
                                  % (limit // 60))
        if problem is not None:
            proc.kill()
            proc.wait()
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            raise problem
        time.sleep(0.1)
    for stream in (proc.stdout, proc.stderr):
        stream.close()
    return b"".join(out), b"".join(err)


def parse_output(returncode, stdout, stderr):
    """The answer from Claude Code's output: one JSON result, or a stream of JSON lines ending in one."""
    data = None
    for line in reversed(stdout.strip().splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type", "result") == "result":
            data = event
            break
    if data is None:
        try:
            data = json.loads(stdout)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        detail = (stderr or stdout).strip() or "exit code %d" % returncode
        raise ClaudeError(_explain(detail[-2000:]))
    text = (data.get("result") or "").strip()
    if data.get("is_error") or returncode != 0:
        raise ClaudeError(_explain(text or stderr.strip() or data.get("subtype") or "unknown error"))
    return text, data.get("stop_reason") == "max_tokens"


def _explain(detail):
    low = detail.lower()
    if "login" in low or "log in" in low or "not logged" in low or "authenticat" in low:
        return LOGIN_HELP + "\n\n(" + detail[:300] + ")"
    if "limit" in low:
        return "Your Claude plan's usage limit was reached:\n\n" + detail[:500]
    return "Claude Code error:\n\n" + detail[:1000]
