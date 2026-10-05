# Claude for LibreOffice

A LibreOffice extension that puts Claude in LibreOffice's sidebar, next to Writer, Calc, Impress
and Draw, the way the Claude add-ins work in Word and Excel.

Click the **sparkle tab** on the right edge of the window (the sidebar's tab bar; *View > Sidebar*
if it's hidden), the sparkle toolbar button, or **Claude > Ask Claude...**. LibreOffice's window
is never moved or resized.

- **On Windows** the full panel (below) opens inside the sidebar. It's a chromeless Microsoft Edge
  window placed into the sidebar; this is new, so if it can't be placed, the sidebar shows the
  simple version instead, and *Settings > Advanced > Simple sidebar* keeps it that way.
- **The simple version** (Linux and macOS, or as the fallback) is made of LibreOffice's own
  controls: Rewrite as Formal / My voice, the quick actions, a message box, model and effort,
  Tracked, and Replace / Insert below / Copy. *Open full panel* shows the full panel in its own
  window.

The full panel follows your light/dark theme. It shows what Claude will read (your selection,
or the whole document / the sheet's filled area when nothing is selected) and offers:

- **Claude edits the document itself**, like the Word and Excel add-ins. Ask "highlight the key
  ideas in yellow", "underline the thesis and bold the connectors", "comment on the weak
  arguments", "replace *colour* with *color* everywhere", "make the headings Heading 2", or in
  Calc "add a totals row and make the header bold with a yellow fill". Claude makes the changes
  straight away and lists them in the panel, including anything it couldn't find. **Undo** (or
  Ctrl+Z once) takes all of them back; with *Edits as tracked changes* on, Writer records them as
  changes to accept or reject. Writer: bold, italic, underline, strikethrough, highlight, text
  color, font, size, alignment, paragraph styles, find and replace, inserting and deleting
  paragraphs, comments. Calc: values and formulas, bold/italic/underline, text and fill colors,
  number formats, alignment, wrapping, borders, comments, clearing cells, inserting and deleting
  rows and columns.
- **Rewrite as Formal | My voice** (Writer) - one click rewrites the selection formally, or the
  way *you* write (see *My voice* below).
- **Quick actions** - Writer: *Improve, Fix grammar, Make shorter, Simplify, Translate FR / EN,
  Summarize, Explain*. Calc: *Explain, Summarize, Find errors, Add totals, Clean up*. Or type any
  request; follow-ups keep the conversation ("shorter", "in English").
- **Rewrite cards** (Writer): *Changes* shows your text with Claude's edits struck through and
  highlighted; *Accept all*, *Reject*, or go **One by one** and keep only the changes you want.
  *Preview* shows the result on a page between its neighbouring paragraphs, with
  *Replace ¶N text* / *Insert below*.
- **One menu for model, effort and tracked changes** - the *Opus 5.5 · High* button under the
  message box: model (Opus 5.5, Sonnet 5.5, Haiku 4.5, Fable 5.1), effort Low to Max (Haiku has
  none), and *Edits as tracked changes* for Writer (shown as a **Tracked** tag when on).
- **Screenshots and pictures** - paste one with Ctrl+V, drop it on the message box, or use the picture
  button, and Claude looks at it with your question (up to 5 per message; big ones are scaled down).
  Full panel only.
- **Clickable citations** - answers point at paragraphs (¶12) or cells (B7, Costs!B2:B9);
  click one to jump there.
- **Claude reads the whole document or workbook** - every sheet in Calc (while under 50,000
  cells), plus comments and tracked changes in Writer, so "summarize the redlines" or "what do
  the comments ask for" work. Your selection is what it works on.
- **Impress and Draw** (read only for now) - Claude reads every slide or page: titles, bullets,
  text boxes, tables, pictures by their alternative text, speaker notes and comments. Quick
  actions: Impress *Summarize, Explain, Proofread, Review deck, Speaker notes*; Draw *Summarize,
  Explain, Proofread*. Answers cite slides (Slide 3) you can click; replies are copied, not
  written into the slides yet (LibreOffice can't undo edits made that way).
- **Calc** replies are previewed as a grid (`=` values become live formulas); *Write below* /
  *Write at selection*, with a warning before overwriting cells that have content.
- **Past conversations** (clock icon), kept on this computer only; clear them in Settings.
- **Settings** (gear): connection, *Your writing voice*, and standing instructions for Writer,
  Calc, Impress and Draw.

The **Claude** menu's *Improve Writing*, *Summarize* and *Explain Selection* open the sidebar and
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

**Updating:** *Tools > Extensions > Check for Updates*, *Install*, then restart LibreOffice. No
need to download the file again.

## Things to know

- **It uses your Claude subscription** through your own Claude Code install, the way Anthropic
  allows: the extension runs the unmodified `claude` program, which is signed in with your
  account. The extension never sees or stores your Claude credentials. Requests count toward
  your plan's usage limits like any other Claude Code use.
- **Or an API key instead:** *Settings > Connect with > Anthropic API key* uses
  [console.anthropic.com](https://console.anthropic.com/settings/keys) billing. That key is
  stored in plain text in `claude-for-libreoffice.json` in your LibreOffice user profile
  (`%APPDATA%\LibreOffice\4\user` on Windows), or set `ANTHROPIC_API_KEY`.
- **What is sent:** the document (or the workbook's sheets, or the slides with their notes), its comments and
  tracked changes,
  your selection, and your instruction. Nothing else. Claude gets no file, web or command access
  from here. Calc selections over 50,000 cells are refused rather than silently cut.
- **Always tell Claude in Writer / Calc / Impress / Draw** (Settings) adds standing instructions to every
  request in that app, e.g. "Write in Canadian French."
- **My voice** learns from your own writing, not from your Claude account: Claude doesn't pick
  up your style from past conversations, and the Claude app's memory and styles don't reach
  Claude Code. In *Settings > Your writing voice*, add things you wrote (the selection, the
  open document, or .odt/.docx/.doc/.rtf/.txt files) and press *Learn my style*; Claude writes a
  short description of how you write, which you can edit. The samples and that description stay
  in `claude-voice.json` in your LibreOffice profile and are sent to Claude only with My voice
  requests and when you press *Learn my style*. *Use my voice by default* makes requests you type
  in Writer come back in your voice too.
- **The full panel is a small local web page** shown in a chromeless Microsoft Edge window (Chrome
  also works; otherwise your default browser). It is served by LibreOffice itself on
  `127.0.0.1` only, and every request needs a random per-session key, so other websites and
  programs can't drive it.

## Develop

```
extension/                 the .oxt contents
  claude_extension.py      UNO component; menu URLs call service:org.willykil.claude.Job?<action>
  Addons.xcu               menu + toolbar
  Sidebar.xcu, Factories.xcu   the Claude sidebar tab and the factory that builds its panel
  icons/                   sparkle icon (sparkle.svg is the source)
  pythonpath/claude_cli.py       default connection: runs the user's `claude -p`
  pythonpath/claude_api.py       settings + API-key connection (raw HTTP, since
                                 LibreOffice's bundled Python can't pip install the SDK)
  pythonpath/claude_office.py    read selection / write reply for Writer and Calc;
                                 read Impress and Draw
  pythonpath/claude_edits.py     Claude's direct edits: the <edits> block it ends a reply with,
                                 applied through UNO in one undo step
  panel/                   the side panel page (index.html, style.css, app.js; diff.js compares
                           the selection with Claude's rewrite)
  pythonpath/claude_panel.py     local server for the panel + opening its own window
  pythonpath/claude_sidebar.py   the sidebar panel; its simple version from LibreOffice controls
  pythonpath/claude_embed.py     Windows: places the full panel (an Edge window) inside the sidebar
  pythonpath/claude_actions.py   prompts and quick actions
  pythonpath/claude_voice.py     My voice: samples, learning, the voice in rewrites
  pythonpath/claude_dialogs.py   the one native message box (if the panel can't open)
build.py                   zips extension/ into claude-for-libreoffice.oxt; writes update.xml
tests/test_api.py          API client against a local mock server
tests/test_cli.py          Claude Code connection against a fake `claude` (fake_claude.py)
tests/test_uno.py          installs the .oxt into a throwaway profile, starts headless
                           LibreOffice, and drives the panel's API on real Writer/Calc/Impress/Draw documents
                           (claude_embed.py needs Windows and isn't covered)
tests/test_edits.py        reading Claude's <edits> block (no LibreOffice needed)
tests/test_diff.js         the panel's word comparison (node tests/test_diff.js)
tests/test_build.py        the committed .oxt matches extension/
```

```
python3 build.py
cd tests && python3 -m unittest test_build test_api test_cli test_edits test_uno -v    # needs soffice + python3-uno
node test_diff.js                                                  # still in tests/
```

Rebuild the `.oxt` after changing anything under `extension/`, and raise `<version>` in
`extension/description.xml`: `build.py` also writes `update.xml`, the feed *Check for Updates*
reads from `main`, and LibreOffice only offers a higher version. CI (`.github/workflows/ci.yml`)
runs all of the above and fails if the `.oxt` or `update.xml` is stale, or if a pull request
changes `extension/` without raising the version.
