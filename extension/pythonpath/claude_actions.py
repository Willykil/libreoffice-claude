"""Menu actions: gather context, ask Claude, put the reply where the user chooses.

The UI is passed in so tests can drive the whole flow without dialogs.
"""

import threading
import time

import claude_api
import claude_office

PROMPTS = {
    "improve": ("Improve the clarity, grammar and flow of the selected text. Keep its meaning, tone, "
                "language and approximate length. Reply with only the revised text."),
    "summarize": "Summarize this concisely.",
    "explain": ("Explain this clearly and briefly. For spreadsheet content, explain what the data and "
                "formulas do and point out any errors or inconsistencies."),
}

_REWRITE = " Keep the same language unless asked otherwise. Reply with only the new text."

# Buttons in the Ask dialog: (label, prompt, needs a selection)
QUICK_ACTIONS = {
    claude_office.WRITER: [
        ("Improve", PROMPTS["improve"], True),
        ("Fix grammar", "Correct the spelling, grammar and punctuation of the selected text and change "
                        "nothing else." + _REWRITE, True),
        ("Make shorter", "Make the selected text noticeably shorter while keeping its key points." + _REWRITE, True),
        ("More formal", "Rewrite the selected text in a more formal, professional tone." + _REWRITE, True),
        ("Simplify", "Rewrite the selected text so it is simpler and easier to read." + _REWRITE, True),
        ("Translate FR \u2194 EN", "If the selected text is in French, translate it into English; otherwise "
                                    "translate it into French. Reply with only the translation.", True),
        ("Summarize", PROMPTS["summarize"], False),
        ("Explain", PROMPTS["explain"], False),
    ],
    claude_office.CALC: [
        ("Explain", PROMPTS["explain"], False),
        ("Summarize", "Summarize what this data shows: key figures, trends and anything unusual.", False),
        ("Find errors", "Check this data and its formulas for errors, inconsistencies and outliers. "
                        "List each problem with its cell reference.", False),
        ("Add totals", "Add a totals row for the numeric columns, using formulas. Reply with only the "
                       "tab-separated row to write below the selection, starting with the label.", True),
        ("Clean up", "Clean up the selected data: trim spaces, consistent capitalization, dates and number "
                     "formats, obvious typos. Reply with only the cleaned tab-separated grid, same shape "
                     "and order.", True),
    ],
}

_busy = threading.Lock()


class Cancelled(Exception):
    pass


def settings_path(ctx):
    import uno
    subst = ctx.ServiceManager.createInstanceWithContext("com.sun.star.util.PathSubstitution", ctx)
    url = subst.substituteVariables("$(user)/claude-for-libreoffice.json", True)
    return uno.fileUrlToSystemPath(url)


def run(ctx, doc, action, ui, path=None):
    path = path or settings_path(ctx)
    settings = claude_api.load_settings(path)

    if action == "settings":
        updated = ui.edit_settings(settings)
        if updated is not None:
            claude_api.save_settings(path, updated)
        return None

    if not _busy.acquire(blocking=False):
        ui.message("Claude is still working on the previous request.")
        return None
    try:
        return _run_prompt(doc, action, ui, settings, path)
    finally:
        _busy.release()


def _run_prompt(doc, action, ui, settings, path):
    try:
        context = claude_office.get_context(doc)
    except claude_office.OfficeError as e:
        ui.message(str(e))
        return None

    if action == "ask":
        instruction = ui.ask_prompt(context, QUICK_ACTIONS[context.kind], settings)
        if not instruction:
            return None
    elif action in PROMPTS:
        if action == "improve" and not context.has_selection:
            ui.message("Select the text you want Claude to improve first.")
            return None
        instruction = PROMPTS[action]
    else:
        ui.message("Unknown Claude action: %s" % action)
        return None

    if settings.get("backend") == claude_api.API and not claude_api.resolve_api_key(settings):
        ui.message("Add your Anthropic API key first, or switch the connection to Claude Code.")
        updated = ui.edit_settings(settings)
        if updated is None or (updated.get("backend") == claude_api.API
                               and not claude_api.resolve_api_key(updated)):
            return None
        claude_api.save_settings(path, updated)
        settings = updated

    system = claude_office.system_prompt(context.kind, settings.get("extra_instructions"))
    user_text = "%s\n\n%s" % (context.text, instruction)
    while True:
        try:
            text, truncated = ui.wait(lambda: claude_api.ask(settings, system, user_text), settings)
        except Cancelled:
            return None
        except claude_api.ClaudeError as e:
            ui.message(str(e), error=True)
            return None
        if not text:
            ui.message("Claude returned an empty reply.")
            return None

        choice, text = ui.show_result(text, context.kind, truncated)
        if choice != ui.REFINE:
            break
        change = ui.ask_refine()
        if not change:
            return None
        user_text = refine_prompt(context.text, instruction, text, change)

    if choice == ui.REPLACE:
        claude_office.apply_result(doc, text, claude_office.REPLACE)
    elif choice == ui.INSERT:
        claude_office.apply_result(doc, text, claude_office.INSERT_AFTER)
    return choice


def refine_prompt(context_text, instruction, previous_reply, change):
    return ("%s\n\n%s\n\nYour previous reply was:\n<previous_reply>\n%s\n</previous_reply>\n\n"
            "Revise that reply as follows: %s" % (context_text, instruction, previous_reply, change))


def wait_responsive(ctx, frame, fn, busy=None, label="Asking Claude..."):
    """Run fn on a worker thread while keeping LibreOffice's UI painting.

    busy: optional progress window with tick() and a `cancelled` flag; on cancel
    this raises Cancelled and the worker's eventual result is discarded.
    """
    result = {}

    def work():
        try:
            result["value"] = fn()
        except BaseException as e:
            result["error"] = e

    toolkit = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    indicator = None
    try:
        indicator = frame.createStatusIndicator()
        indicator.start(label, 0)
    except Exception:
        indicator = None
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            toolkit.reschedule()
            if busy is not None:
                if busy.cancelled:
                    raise Cancelled()
                busy.tick()
            time.sleep(0.05)
    finally:
        if indicator is not None:
            indicator.end()
    if "error" in result:
        raise result["error"]
    return result["value"]
