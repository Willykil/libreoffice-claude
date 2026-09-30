"""The one native dialog left: a message box for problems opening the panel."""

from com.sun.star.awt.MessageBoxButtons import BUTTONS_OK
from com.sun.star.awt.MessageBoxType import ERRORBOX, INFOBOX


def message(ctx, frame, text, title="Claude", error=False):
    toolkit = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    parent = frame.getContainerWindow() if frame else None
    box = toolkit.createMessageBox(parent, ERRORBOX if error else INFOBOX, BUTTONS_OK, title, text)
    box.execute()
    box.dispose()
