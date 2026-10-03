"""The actions Claude offers: menu prompts, the panel's quick actions, and where settings live."""

import claude_office

PROMPTS = {
    "improve": ("Improve the clarity, grammar and flow of the selected text. Keep its meaning, tone, "
                "language and approximate length. Reply with only the revised text."),
    "summarize": "Summarize this concisely.",
    "explain": ("Explain this clearly and briefly. For spreadsheet content, explain what the data and "
                "formulas do and point out any errors or inconsistencies."),
}

_REWRITE = " Keep the same language unless asked otherwise. Reply with only the new text."

# The panel's quick-action chips: (label, prompt, needs a selection). Writer's formal and
# my-voice rewrites are the separate "Rewrite as" toggle (claude_voice.py).
QUICK_ACTIONS = {
    claude_office.WRITER: [
        ("Improve", PROMPTS["improve"], True),
        ("Fix grammar", "Correct the spelling, grammar and punctuation of the selected text and change "
                        "nothing else." + _REWRITE, True),
        ("Make shorter", "Make the selected text noticeably shorter while keeping its key points." + _REWRITE, True),
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


def settings_path(ctx):
    import uno
    subst = ctx.ServiceManager.createInstanceWithContext("com.sun.star.util.PathSubstitution", ctx)
    url = subst.substituteVariables("$(user)/claude-for-libreoffice.json", True)
    return uno.fileUrlToSystemPath(url)
