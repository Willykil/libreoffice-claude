"""UNO entry point. Menu items call service:org.willykil.claude.Job?<action>.

Every action opens the Claude side panel (see pythonpath/claude_panel.py);
"improve", "summarize" and "explain" also run straight away in it.
"""

import traceback

import unohelper
from com.sun.star.task import XJobExecutor

import claude_dialogs
import claude_panel


class ClaudeJob(unohelper.Base, XJobExecutor):
    def __init__(self, ctx):
        self.ctx = ctx

    def trigger(self, action):
        try:
            claude_panel.open_panel(self.ctx, None if action == "ask" else action)
        except Exception:
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            claude_dialogs.message(self.ctx, desktop.getCurrentFrame(),
                                   "Couldn't open the Claude panel:\n\n" + traceback.format_exc(), error=True)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(ClaudeJob, "org.willykil.claude.Job", ("com.sun.star.task.Job",))
