# Claude for LibreOffice

A LibreOffice extension that puts Claude in a side panel next to Writer and Calc, the way the
Claude add-ins work in Word and Excel.

Click the sparkle button (toolbar, **Extension** tab in the Tabbed interface, or
**Claude > Ask Claude...**). The panel docks on the right of the screen, LibreOffice moves over to
make room, and it follows your light/dark Windows theme. It shows what Claude will read (your
selection, or the whole document / the sheet's filled area when nothing is selected) and offers:

- **Quick actions** - Writer: *Improve, Fix grammar, Make shorter, More formal, Simplify,
  Translate FR / EN, Summarize, Explain*. Calc: *Explain, Summarize, Find errors, Add totals,
  Clean up*. Or type any request; follow-ups keep the conversation ("shorter", "in English").
- **Reply cards** with *Replace selection* / *Insert below* (Writer) or *Write at selection* /
  *Write below* (Calc; tab-separated rows and Markdown tables become cells, `=` values live
  formulas, shown as a table preview first), plus *Copy*. Each insertion is one Ctrl+Z.
- **Stop** while Claude is working, and **Settings** (gear icon).

The **Claude** menu's *Improve Writing*, *Summarize* and *Explain Selection* open the panel and
run straight away.

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
- **The panel is a small local web page** shown in a chromeless Microsoft Edge window (Chrome
  also works; otherwise your default browser). It is served by LibreOffice itself on
  `127.0.0.1` only, and every request needs a random per-session key, so other websites and
  programs can't drive it. Closing the panel window is fine; the sparkle button reopens it.

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
  panel/                   the side panel page (index.html, style.css, app.js)
  pythonpath/claude_panel.py     local server for the panel + opening/docking its window
  pythonpath/claude_actions.py   prompts and quick actions
  pythonpath/claude_dialogs.py   the one native message box (if the panel can't open)
build.py                   zips extension/ into claude-for-libreoffice.oxt
tests/test_api.py          API client against a local mock server
tests/test_cli.py          Claude Code connection against a fake `claude` (fake_claude.py)
tests/test_uno.py          installs the .oxt into a throwaway profile, starts headless
                           LibreOffice, and drives the panel's API on real Writer/Calc documents
```

```
python3 build.py
cd tests && python3 -m unittest test_api test_cli test_uno -v    # needs soffice + python3-uno
```

Rebuild the `.oxt` after changing anything under `extension/`.
