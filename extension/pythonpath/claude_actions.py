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

_busy = threading.Lock()


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
        instruction = ui.ask_prompt(context.label)
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

    if not claude_api.resolve_api_key(settings):
        ui.message("Add your Anthropic API key first.")
        updated = ui.edit_settings(settings)
        if updated is None or not claude_api.resolve_api_key(updated):
            return None
        claude_api.save_settings(path, updated)
        settings = updated

    system = claude_office.system_prompt(context.kind, settings.get("extra_instructions"))
    user_text = "%s\n\n%s" % (context.text, instruction)
    try:
        text, truncated = ui.wait(lambda: claude_api.ask(settings, system, user_text))
    except claude_api.ClaudeError as e:
        ui.message(str(e), error=True)
        return None
    if not text:
        ui.message("Claude returned an empty reply.")
        return None

    choice, text = ui.show_result(text, context.kind, truncated)
    if choice == ui.REPLACE:
        claude_office.apply_result(doc, text, claude_office.REPLACE)
    elif choice == ui.INSERT:
        claude_office.apply_result(doc, text, claude_office.INSERT_AFTER)
    return choice


def wait_responsive(ctx, frame, fn, label="Asking Claude..."):
    """Run fn on a worker thread while keeping LibreOffice's UI painting."""
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
            time.sleep(0.05)
    finally:
        if indicator is not None:
            indicator.end()
    if "error" in result:
        raise result["error"]
    return result["value"]
