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
    if '"get_usage"' in stdin:
        request = json.loads(stdin.splitlines()[0])
        if mode == "usage_unsupported":
            response = {"subtype": "error", "request_id": request["request_id"],
                        "error": "Unsupported control request subtype: get_usage"}
        else:
            response = {"subtype": "success", "request_id": request["request_id"],
                        "response": json.loads(os.environ["FAKE_CLAUDE_USAGE"])}
        print(json.dumps({"type": "system", "subtype": "init"}))
        print(json.dumps({"type": "control_response", "response": response}))
        return
    if mode == "delayed":
        import time
        time.sleep(1)
        mode = "ok"
    if mode == "streaming":
        # Like a long answer: partial events trickle in for longer than the idle timeout in all.
        import time
        for i in range(6):
            print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                                "delta": {"type": "text_delta", "text": "x"}}}),
                  flush=True)
            time.sleep(0.4)
        mode = "ok"
    if mode == "ok":
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
                                                                          "rateLimitType": "five_hour"}}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": os.environ.get("FAKE_CLAUDE_REPLY", "fake reply"),
                          "usage": {"input_tokens": 120, "output_tokens": 30}, "total_cost_usd": 0.0012,
                          "modelUsage": {"claude-opus-5-5": {}}}))
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
