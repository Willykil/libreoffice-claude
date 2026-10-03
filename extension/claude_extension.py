"""UNO entry point. Menu items call service:org.willykil.claude.Job?<action>.

Every action opens the Claude tab in LibreOffice's sidebar (pythonpath/claude_sidebar.py, built
by PanelFactory); "improve", "summarize" and "explain" also run straight away in it.
"""

import traceback

import unohelper
from com.sun.star.task import XJobExecutor

import claude_dialogs
import claude_sidebar


class ClaudeJob(unohelper.Base, XJobExecutor):
    def __init__(self, ctx):
        self.ctx = ctx

    def trigger(self, action):
        try:
            claude_sidebar.show(self.ctx, None if action == "ask" else action)
        except Exception:
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            claude_dialogs.message(self.ctx, desktop.getCurrentFrame(),
                                   "Couldn't open the Claude panel:\n\n" + traceback.format_exc(), error=True)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(ClaudeJob, "org.willykil.claude.Job", ("com.sun.star.task.Job",))
g_ImplementationHelper.addImplementation(claude_sidebar.PanelFactory, "org.willykil.claude.PanelFactory",
                                         ("com.sun.star.ui.UIElementFactory",))
