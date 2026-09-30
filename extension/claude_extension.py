"""UNO entry point. Menu items call service:org.willykil.claude.Job?<action>."""

import traceback

import unohelper
from com.sun.star.task import XJobExecutor

import claude_actions
import claude_dialogs


class _DialogUI:
    REPLACE, INSERT, REFINE = claude_dialogs.REPLACE, claude_dialogs.INSERT, claude_dialogs.REFINE

    def __init__(self, ctx, frame):
        self.ctx, self.frame = ctx, frame

    def message(self, text, error=False):
        claude_dialogs.message(self.ctx, self.frame, text, error=error)

    def ask_prompt(self, context, quick_actions, settings):
        return claude_dialogs.ask_prompt(self.ctx, context, quick_actions, settings)

    def ask_refine(self):
        return claude_dialogs.ask_refine(self.ctx)

    def show_result(self, text, kind, truncated):
        return claude_dialogs.show_result(self.ctx, text, kind, truncated)

    def edit_settings(self, settings):
        return claude_dialogs.edit_settings(self.ctx, settings)

    def wait(self, fn, settings):
        busy = claude_dialogs.Busy(self.ctx, self.frame, settings)
        try:
            return claude_actions.wait_responsive(self.ctx, self.frame, fn, busy)
        finally:
            busy.close()


class ClaudeJob(unohelper.Base, XJobExecutor):
    def __init__(self, ctx):
        self.ctx = ctx

    def trigger(self, action):
        desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
        frame = desktop.getCurrentFrame()
        ui = _DialogUI(self.ctx, frame)
        try:
            claude_actions.run(self.ctx, desktop.getCurrentComponent(), action, ui)
        except Exception:
            ui.message("Unexpected error:\n\n" + traceback.format_exc(), error=True)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(ClaudeJob, "org.willykil.claude.Job", ("com.sun.star.task.Job",))
