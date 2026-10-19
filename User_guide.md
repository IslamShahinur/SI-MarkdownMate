# Using SI MarkdownMate

## 1. Set it up (once)

Download & Unzip `SI-MarkdownMate.zip`, open a terminal in that folder, and run:

bash
==================================================================
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
==================================================================

You need Python 3.9 or newer. Run `python -m unittest` if you want to confirm everything works on your machine.

## 2. Start it

bash
=========================================================================
python main.py notes.md     # open notes.md, or create it if it doesn't exist
python main.py              # reopen your last document
python main.py --new        # start with a blank document
===================================================================

It opens full-screen in the terminal. Your text is in the middle, a shortcut bar and status bar are at the bottom, and the status bar shows the file name, **Saved** or **Unsaved**, your cursor position, and the number of Markdown issues.

## 3. Write and let it save

Just type. There is no need to save manually, because the app saves a moment after you stop typing. Press `Ctrl+S` to save immediately. If the document has no name yet, it asks for one (a `.md` ending is added for you), and until then your text is kept safe in a recovery copy.

## 4. Use suggestions

Type a Markdown trigger at the start of a line and a small menu appears:

| Type this | You get |
|---|---|
| `#` | headings |
| `-` | bullet list, task, completed task |
| `1.` | numbered list |
| `>` | quote |
| ` ``` ` | code block (cursor lands inside) |
| `\|` | table |
| `---` | divider |
| `[` or `![` | link or image |
| `/` | a menu of everything (type `/ta` to filter) |

Use **Up/Down** to choose and **Tab** to accept. Press **Esc** to dismiss the menu, which never changes your text. When no menu is showing, Tab simply indents.

## 5. Format text

- **Bold:** put the cursor on a word (or select text) and press `Ctrl+B`. Press it again to remove the bold.
- **Italic:** `Alt+I`.
- **Select text:** hold `Shift` and use the arrow keys. If your terminal doesn't support that, press `Ctrl+Space`, then use the arrows.
- **Color:** `Alt+C`, then type a number from the list (or a name like `red`, or `#ff8800`). Enter `0` to remove it.
- **Font size:** `Alt+S`, then a size from 8 to 96. Enter `0` to remove it.
- **Divider:** `Alt+Enter` (or `Alt+D` if `Alt+Enter` doesn't work in your terminal).

Color and size are inserted as HTML. Many Markdown viewers, GitHub included, ignore them, and the app reminds you of this when you use them. For sizes that work everywhere, use `#` headings.

On a Mac, turn on "Use Option as Meta key" in your terminal's keyboard settings, or the Alt shortcuts won't work.

## 6. Find things

- `Ctrl+F` finds text in the current document. `F3` jumps to the next match and `F4` to the previous.
- `Alt+F` searches every `.md` file in the same folder. Type the number of a result to open it at that line.
- `Alt+V` lists any Markdown problems, such as an unclosed code block, an empty link, or a broken table.

## 7. Manage files

- `Ctrl+N` starts a new document and saves the current one first.
- `Ctrl+O` opens a file. Type a number from the recent list, a file path (`Tab` completes it), or a folder name to see its `.md` files.
- `Alt+R` renames the current document.
- `Ctrl+Q` saves and quits. If the document is unnamed, it asks for a name, or you can press Enter to keep a recovery copy.

## 8. If something goes wrong

- **Crash or power loss:** on the next start the app offers to restore your unsaved text.
- **File changed by another program:** autosave pauses so it can't overwrite their changes, and `Ctrl+S` asks before replacing the file.
- **Forgot a shortcut:** press `F1` for the full list.
- **Esc to back out:** it closes any menu, prompt, or panel.
=====================================================================================
