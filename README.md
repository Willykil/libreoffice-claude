# Claude for LibreOffice

A LibreOffice extension that adds a **Claude** menu (and an **Ask Claude** toolbar button) to
Writer and Calc, the way the Claude add-ins work in Word and Excel. In the Tabbed interface the
buttons are on the **Extension** tab.

Click the sparkle button (or **Claude > Ask Claude...**) and pick a quick action - *Improve*,
*Fix grammar*, *Make shorter*, *More formal*, *Simplify*, *Translate FR / EN*, *Summarize*,
*Explain* in Writer; *Explain*, *Summarize*, *Find errors*, *Add totals*, *Clean up* in Calc - or
type your own request. Nothing selected? Claude reads the whole document (Writer) or the sheet's
filled area (Calc); the Ask window says which.

Claude's reply opens in a window where you can edit it, **Refine...** it ("shorter",
"in English"...), then:

- **Writer:** *Replace selection* or *Insert after* (paragraphs are kept).
- **Calc:** *Write at selection* or *Write below selection*. Tab-separated rows and Markdown tables
  are split into cells; `=` values become live formulas; numbers become numbers.

Every insertion is a single undo step (Ctrl+Z). The **Claude** menu also has direct *Improve
Writing*, *Summarize* and *Explain Selection* items, and *Settings...*.

## Install

1. **Claude Code** (this is what connects to your Claude subscription). Skip if you already
   use it. In PowerShell:

   ```
   irm https://claude.ai/install.ps1 | iex
   claude
   ```

   `claude` opens once so you can sign in with your Claude account; then close it.
2. Download [`claude-for-libreoffice.oxt`](claude-for-libreoffice.oxt) (on GitHub: open the file,
   then the download button).
3. In LibreOffice: **Tools > Extensions > Add...**, pick the file, then restart LibreOffice.
   (Or double-click the `.oxt`.)

That's it - no API key needed. Requires LibreOffice 7.0 or newer. On Linux distro builds, install
`python3-uno` if the Claude menu doesn't appear.

## Things to know

- **It uses your Claude subscription** through your own Claude Code install, the way Anthropic
  allows: the extension runs the unmodified `claude` program, which is signed in with your
  account. The extension never sees or stores your Claude credentials. Requests count toward
  your plan's usage limits like any other Claude Code use.
- **Or an API key instead:** *Settings > Connect with > Anthropic API key* uses
  [console.anthropic.com](https://console.anthropic.com/settings/keys) billing. That key is
  stored in plain text in `claude-for-libreoffice.json` in your LibreOffice user profile
  (`%APPDATA%\LibreOffice\4\user` on Windows), or set `ANTHROPIC_API_KEY`.
- **What is sent:** the selection (or whole document / used sheet area when nothing is
  selected) plus your instruction. Nothing else. Claude gets no file, web or command access
  from here. Calc selections over 50,000 cells are refused rather than silently cut.
- **Always tell Claude** in Settings adds standing instructions to every request, e.g.
  "Write in Canadian French."
- While Claude works, a small *Claude is thinking* window shows elapsed time with a Cancel
  button.

## Develop

```
extension/                 the .oxt contents
  claude_extension.py      UNO component; menu URLs call service:org.willykil.claude.Job?<action>
  Addons.xcu               menu + toolbar
  icons/                   sparkle icon (sparkle.svg is the source)
  pythonpath/claude_cli.py       default connection: runs the user's `claude -p`
  pythonpath/claude_api.py       settings + API-key connection (raw HTTP, since
                                 LibreOffice's bundled Python can't pip install the SDK)
  pythonpath/claude_office.py    read selection / write reply for Writer and Calc
  pythonpath/claude_actions.py   the menu actions
  pythonpath/claude_dialogs.py   dialogs, built in code
build.py                   zips extension/ into claude-for-libreoffice.oxt
tests/test_api.py          API client against a local mock server
tests/test_cli.py          Claude Code connection against a fake `claude` (fake_claude.py)
tests/test_uno.py          installs the .oxt into a throwaway profile, starts headless
                           LibreOffice, and runs every action on real Writer/Calc documents
```

```
python3 build.py
cd tests && python3 -m unittest test_api test_cli test_uno -v    # needs soffice + python3-uno
```

Rebuild the `.oxt` after changing anything under `extension/`.
