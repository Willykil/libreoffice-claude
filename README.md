# Claude for LibreOffice

A LibreOffice extension that adds a **Claude** menu (and an **Ask Claude** toolbar button) to
Writer and Calc, the way the Claude add-ins work in Word and Excel. In the Tabbed interface the
buttons are on the **Extension** tab.

| Menu item | Writer | Calc |
| --- | --- | --- |
| **Ask Claude...** | Ask anything about the selection (or the whole document if nothing is selected) | Ask about the selected range (or the sheet's used area if one cell is selected) - e.g. "add a total row", "fill in the missing categories", "write a formula for growth %" |
| **Improve Writing** | Rewrites the selected text | - |
| **Summarize** | Summarizes the selection / document | Summarizes the data |
| **Explain Selection** | Explains the text | Explains the data and formulas, flags errors |
| **Settings...** | API key, model, effort, max reply length, standing instructions | same |

Claude's reply opens in a window where you can edit it, then:

- **Writer:** *Replace selection* or *Insert after* (paragraphs are kept).
- **Calc:** *Write at selection* or *Write below selection*. Tab-separated rows and Markdown tables
  are split into cells; `=` values become live formulas; numbers become numbers.

Every insertion is a single undo step (Ctrl+Z).

## Install

1. Download [`claude-for-libreoffice.oxt`](claude-for-libreoffice.oxt) (on GitHub: open the file, then the download button).
2. In LibreOffice: **Tools > Extensions > Add...**, pick the file, then restart LibreOffice.
   (Or double-click the `.oxt`.)
3. Open **Claude > Settings...** and paste an API key from
   [console.anthropic.com](https://console.anthropic.com/settings/keys).
   Alternatively set the `ANTHROPIC_API_KEY` environment variable.

Requires LibreOffice 7.0 or newer. On Windows and macOS its bundled Python is used, so nothing
else to install. On Linux distro builds, install `python3-uno` if the Claude menu doesn't appear.

## Things to know

- **It uses the Anthropic API, billed to your API key** - not your Claude.ai subscription.
  The official Word/Excel add-ins sign in with your Claude account; LibreOffice has no official
  add-in, so this one calls the API directly.
- **What is sent:** the selection (or whole document / used sheet area when nothing is
  selected - the Ask dialog tells you which) plus your instruction. Nothing else.
  Calc selections over 50,000 cells are refused rather than silently cut.
- **The key is stored in plain text** in `claude-for-libreoffice.json` in your LibreOffice
  user profile (`%APPDATA%\LibreOffice\4\user` on Windows). Use the environment variable
  if you'd rather not keep it there.
- Default model is `claude-opus-5-5` at `medium` effort; switch to `claude-sonnet-5-5` or
  `claude-haiku-4-5` in Settings for faster, cheaper replies. Requests opt into the API's
  server-side refusal fallback so an over-cautious decline is retried automatically.
- LibreOffice stays responsive while Claude works; progress shows in the status bar.

## Develop

```
extension/                 the .oxt contents
  claude_extension.py      UNO component; menu URLs call service:org.willykil.claude.Job?<action>
  Addons.xcu               menu + toolbar
  pythonpath/claude_api.py       Messages API client + settings (no UNO; raw HTTP, since
                                 LibreOffice's bundled Python can't pip install the SDK)
  pythonpath/claude_office.py    read selection / write reply for Writer and Calc
  pythonpath/claude_actions.py   the menu actions
  pythonpath/claude_dialogs.py   dialogs, built in code
build.py                   zips extension/ into claude-for-libreoffice.oxt
tests/test_api.py          API client against a local mock server
tests/test_uno.py          installs the .oxt into a throwaway profile, starts headless
                           LibreOffice, and runs every action on real Writer/Calc documents
```

```
python3 build.py
cd tests && python3 -m unittest test_api test_uno -v    # needs soffice + python3-uno
```

Rebuild the `.oxt` after changing anything under `extension/`.
