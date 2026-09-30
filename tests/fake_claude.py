"""Stand-in for the `claude` CLI: records its arguments and stdin, answers per FAKE_CLAUDE_MODE."""

import json
import os
import sys


def main():
    log = os.environ.get("FAKE_CLAUDE_LOG")
    argv = sys.argv[1:]
    system = ""
    if "--system-prompt-file" in argv:
        with open(argv[argv.index("--system-prompt-file") + 1], encoding="utf-8") as f:
            system = f.read()
    stdin = sys.stdin.buffer.read().decode("utf-8")
    if log:
        with open(log, "w", encoding="utf-8") as f:
            json.dump({"argv": argv, "system": system, "stdin": stdin, "cwd": os.getcwd()}, f)
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    if mode == "delayed":
        import time
        time.sleep(1)
        mode = "ok"
    if mode == "ok":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": os.environ.get("FAKE_CLAUDE_REPLY", "fake reply")}))
    elif mode == "logged_out":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Not logged in · Please run /login"}))
        sys.exit(1)
    elif mode == "slow":
        import time
        time.sleep(60)
    elif mode == "crash":
        sys.stderr.write("something broke\n")
        sys.exit(2)


main()
