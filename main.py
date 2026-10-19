"""SI MarkdownMate application entry point."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from file_manager import FileManager, FileManagerError


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="SI MarkdownMate",
        description="A lightweight terminal-based Markdown editor.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Markdown file to open or create (.md is added if you leave it off).",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="Start with a blank document instead of reopening the last one.",
    )
    parser.add_argument(
        "--config-dir",
        help="Optional custom configuration directory.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("SI MarkdownMate needs an interactive terminal.")
        return 1

    file_manager = FileManager(args.config_dir)

    initial_path = None
    if args.file:
        try:
            initial_path = str(file_manager.normalize_markdown_path(args.file))
        except ValueError as error:
            print(error)
            return 2

    # Imported here so --help and argument errors work even before the
    # terminal library is installed.
    from editor import MarkdownEditor

    try:
        editor = MarkdownEditor(file_manager, initial_path, start_blank=args.new)
    except (FileManagerError, OSError) as error:
        print(f"Could not open the file: {error}")
        return 1

    try:
        editor.run()
    except KeyboardInterrupt:
        print("\nExited. Any unsaved text was kept safe.")
    except Exception as error:  # the editor already saved or snapshotted in run()
        print(f"Unexpected error: {error}")
        print("Your text was saved or kept as a recovery copy.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
