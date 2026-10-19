# SI MarkdownMate

A small, fast, keyboard-driven Markdown editor for the terminal, written in Python.

It saves as you type, suggests Markdown syntax in context, formats selected text,
searches your notes, and is built so that your text is never lost silently.
No AI service, no database, no network: everything stays on your machine.

## Install

Requires Python 3.9 or newer.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python main.py notes.md     # open or create a file (".md" is added if you leave it off)
python main.py              # reopen the last document (or restore unsaved text)
python main.py --new        # start with a blank document
```

A new file is created on disk when you start typing. A blank, unnamed document
is kept safe in a recovery copy until you name it with `Ctrl+S`.

## Keyboard shortcuts

Press **F1** in the editor for this list.

| Shortcut | Action |
|---|---|
| `Ctrl+S` | Save now (asks for a name if the document is untitled) |
| `Ctrl+N` | New document (saves the current one first) |
| `Ctrl+O` | Open: pick a recent file by number, type a path, or type a folder to list its `.md` files |
| `Alt+R` | Rename the document |
| `Ctrl+Q` | Save and quit |
| `Ctrl+F` | Find in the document; `F3` / `F4` jump to the next / previous match |
| `Alt+F` | Search every `.md` file in the document's folder, then open a result |
| `Ctrl+B` | Toggle **bold** on the selection (or the word at the cursor) |
| `Alt+I` | Toggle *italic* |
| `Alt+C` | Text color (HTML, see below) |
| `Alt+S` | Font size (HTML, see below) |
| `Alt+Enter` or `Alt+D` | Insert a `---` section divider |
| `Alt+V` | Check the Markdown for problems |
| `Tab` | Accept the highlighted suggestion; otherwise indent by 4 spaces |
| `Shift+Tab` | Outdent the current line |
| `Esc` | Close the suggestion menu, prompt or panel |
| `Up` / `Down` | Choose a suggestion (only while the menu is open) |
| `Shift+Arrows` | Select text. Without Shift-arrow support, use `Ctrl+Space`, then the arrow keys |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo |
| `F1` | Help |

**Why italic is `Alt+I` and not `Ctrl+I`:** in a terminal `Ctrl+I` and `Tab` are the
same key, so the editor cannot tell them apart.

**Alt keys on macOS Terminal / iTerm2:** enable "Use Option as Meta key" in the
terminal's keyboard settings. If `Alt+Enter` does not work in your terminal, use
`Alt+D`, or type `---` and accept the divider suggestion.

### Selecting text

Selection includes the character under the cursor (like vim's visual mode), and
the highlighted text is exactly what gets formatted. After `Ctrl+B` the text stays
selected, so you can immediately add italic, a color, and so on.

## Suggestions and autocomplete

Suggestions appear only after a clear Markdown trigger, never for ordinary prose
and never inside a code block. Dismissing a suggestion never changes your text.

| Type | Offers |
|---|---|
| `#` `##` ... on an empty line | headings |
| `-` `*` `+` | bullet, task, completed task |
| `1.` | numbered list item |
| `>` | blockquote |
| `` ``` `` | code block (Python, Bash, JSON, plain) with the cursor inside |
| `\|` | 2- or 3-column table |
| `--` `---` `***` | divider (a blank line is added so it is never read as a heading) |
| `[` / `![` | link / image template |
| `` ` `` / `**` / `*` after a space | inline code / bold / italic |
| `- [` | `[ ]` or `[x]` checkbox |
| `/` or `/ta` | menu of every template, filtered as you type |

## Automatic saving and data safety

* **Autosave** runs once you pause typing (default 0.75 s), so the final keystroke
  is always saved. The status bar shows *Saved* / *Unsaved*.
* **Atomic writes:** each save goes to a temporary file first and is swapped in
  with `os.replace`, so a crash or power cut leaves the old file or the new one,
  never half of each. File permissions and line endings (LF or CRLF) are kept.
* **Changed on disk:** if another program edits the file while it is open, autosave
  pauses instead of overwriting it. `Ctrl+S` then asks before replacing it.
* **Recovery:** if a save fails, the document is untitled, or the editor stops
  unexpectedly, your text is kept in `recovery.json` in the settings folder and
  you are offered it on the next start. Declining sets it aside as
  `recovery.previous.json` rather than deleting it.
* **No silent loss on exit:** `Ctrl+Q` saves, or asks you to name an untitled
  document, or keeps a recovery copy.
* Files that are not valid UTF-8 are refused (not opened) so they cannot be damaged.

## Markdown check (`Alt+V`)

The status bar shows an issue count. The check reports: an unclosed code fence,
links or images with empty or unfinished destinations, malformed tables (column
counts, missing separator row), unmatched `*` / `**` markers, and unclosed
`<span>` tags. Code blocks and inline code are never checked, and unusual but
valid Markdown is left alone.

## Colors and font sizes are HTML, not Markdown

Bold, italic, headings, lists, links, images, tables and code blocks are standard
Markdown and work almost everywhere. Arbitrary colors and font sizes are **not**
standard Markdown. The editor inserts HTML such as

```html
<span style="color: red;">Important text</span>
<span style="font-size: 24px;">Large text</span>
```

Some renderers show these; many (GitHub, for example) remove or ignore inline
styles. For sizes that work everywhere, use `#` headings. Colors accept the names
red, orange, green, blue, purple, gray, black, white or a `#rgb` / `#rrggbb` value.

## Settings

Stored in `~/.config/si-markdownmate/config.json` (`%APPDATA%\si-markdownmate` on
Windows, or the folder given with `--config-dir`):

| Key | Meaning |
|---|---|
| `autosave_debounce_seconds` | Pause before autosave, 0.1 to 60 (default 0.75) |
| `default_folder` | Folder suggested for new documents (default: Documents, else the current folder) |

Recent files and the last open document are also kept there.

## Tests

```bash
python -m unittest
```

Editor tests need `prompt_toolkit` installed and are skipped otherwise.

## Project layout

```text
main.py            entry point and command-line arguments
editor.py          terminal UI, key bindings, autosave, prompts
markdown_core.py   suggestions, formatting, validation, search (pure functions)
file_manager.py    safe writes, recent files, recovery, folder search
tests/             unit tests
```
