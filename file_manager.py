"""Local Markdown file management for SI MarkdownMate.

Responsibilities: reading and writing ``.md`` files safely, recent files and
settings (JSON), crash recovery, and folder search. Nothing in here knows about
the terminal UI.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]

APP_DIR_NAME = "si-markdownmate"
MARKDOWN_SUFFIX = ".md"
SKIP_DIRECTORIES = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv"}
MAX_SEARCH_FILE_BYTES = 5 * 1024 * 1024
MAX_SEARCH_FILES = 5000


class FileManagerError(Exception):
    """A problem with a document that is safe to show to the user."""


class ExternalChangeError(FileManagerError):
    """The file changed on disk after it was opened."""


@dataclass(frozen=True)
class LoadedFile:
    path: Path
    text: str  # always uses "\n" line endings
    newline: str  # the file's own line ending, "\n" or "\r\n"
    mtime_ns: int


def default_config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


class FileManager:
    """Handles Markdown files, recent files, settings and crash recovery."""

    MAX_RECENT = 10

    def __init__(self, config_dir: Optional[PathLike] = None) -> None:
        directory = Path(config_dir).expanduser() if config_dir else default_config_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            directory = Path(tempfile.gettempdir()) / APP_DIR_NAME
            directory.mkdir(parents=True, exist_ok=True)

        self.config_dir = directory
        self.config_path = directory / "config.json"
        self.recovery_path = directory / "recovery.json"
        self.previous_recovery_path = directory / "recovery.previous.json"
        self.config = self._load_config()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def _default_config() -> dict:
        return {
            "recent_files": [],
            "default_folder": "",
            "autosave_debounce_seconds": 0.75,
            "last_file": None,
            "last_cursor": 0,
        }

    def _load_config(self) -> dict:
        config = self._default_config()
        raw: dict = {}
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            pass

        recent = raw.get("recent_files")
        if isinstance(recent, list):
            config["recent_files"] = [p for p in recent if isinstance(p, str)][: self.MAX_RECENT]

        if isinstance(raw.get("default_folder"), str):
            config["default_folder"] = raw["default_folder"]

        debounce = raw.get("autosave_debounce_seconds")
        if isinstance(debounce, (int, float)) and not isinstance(debounce, bool):
            if 0.1 <= debounce <= 60:
                config["autosave_debounce_seconds"] = float(debounce)

        if isinstance(raw.get("last_file"), str):
            config["last_file"] = raw["last_file"]

        cursor = raw.get("last_cursor")
        if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
            config["last_cursor"] = cursor

        for key, value in raw.items():
            config.setdefault(key, value)
        return config

    def save_config(self) -> bool:
        """Write settings. Best effort: a read-only config folder must not crash the editor."""
        try:
            self._atomic_write_bytes(
                self.config_path,
                json.dumps(self.config, indent=2, ensure_ascii=False).encode("utf-8"),
            )
            return True
        except OSError:
            return False

    def default_folder(self) -> Path:
        """Folder suggested for new documents."""
        configured = self.config.get("default_folder")
        if configured:
            folder = Path(configured).expanduser()
            if folder.is_dir():
                return folder
        documents = Path.home() / "Documents"
        return documents if documents.is_dir() else Path.cwd()

    # ------------------------------------------------------------------
    # Recent files and session
    # ------------------------------------------------------------------

    def add_recent_file(self, path: PathLike) -> None:
        resolved = str(Path(path).expanduser().resolve())
        recent = [p for p in self.config.get("recent_files", []) if p != resolved]
        recent.insert(0, resolved)
        self.config["recent_files"] = recent[: self.MAX_RECENT]
        self.save_config()

    def get_recent_files(self) -> list[str]:
        return [p for p in self.config.get("recent_files", []) if os.path.isfile(p)]

    def remember_session(self, path: Optional[PathLike], cursor: int = 0) -> None:
        """Remember the open document so the next start can reopen it."""
        self.config["last_file"] = str(Path(path).expanduser().resolve()) if path else None
        self.config["last_cursor"] = max(0, int(cursor))
        self.save_config()

    def last_session(self) -> tuple[Optional[Path], int]:
        last = self.config.get("last_file")
        if isinstance(last, str) and os.path.isfile(last):
            return Path(last), int(self.config.get("last_cursor", 0))
        return None, 0

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_markdown_path(raw: str, base: Optional[PathLike] = None) -> Path:
        """Turn user input into an absolute ``.md`` path.

        A missing extension becomes ``.md``; any other extension is refused.
        Raises ``ValueError`` with a message that is safe to show.
        """
        text = raw.strip().strip("\"'")
        if not text:
            raise ValueError("No file name given.")

        path = Path(text).expanduser()
        if not path.is_absolute():
            path = (Path(base) if base else Path.cwd()) / path
        path = path.resolve()

        if path.is_dir():
            raise ValueError(f"{path} is a folder, not a file.")
        if path.suffix == "":
            path = path.with_name(path.name + MARKDOWN_SUFFIX)
        if path.suffix.lower() != MARKDOWN_SUFFIX:
            raise ValueError("SI MarkdownMate works with .md files.")
        return path

    @staticmethod
    def get_mtime_ns(path: PathLike) -> Optional[int]:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_document(self, path: PathLike) -> LoadedFile:
        """Read a file, remembering its line-ending style and modification time."""
        resolved = Path(path).expanduser().resolve()

        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {resolved}")
        if not resolved.is_file():
            raise IsADirectoryError(f"Not a file: {resolved}")

        raw = resolved.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise FileManagerError(
                f"{resolved.name} is not valid UTF-8 text ({error.reason}); "
                "it was not opened so it cannot be damaged."
            ) from None

        crlf = raw.count(b"\r\n")
        lone_lf = raw.count(b"\n") - crlf
        newline = "\r\n" if crlf > lone_lf else "\n"
        return LoadedFile(
            resolved, text.replace("\r\n", "\n"), newline, resolved.stat().st_mtime_ns
        )

    def read_file(self, path: PathLike) -> str:
        return self.read_document(path).text

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def create_file(self, path: PathLike, initial_text: str = "") -> Path:
        resolved = Path(path).expanduser().resolve()
        if resolved.exists():
            raise FileExistsError(f"File already exists: {resolved}")
        self.write_file(resolved, initial_text)
        self.add_recent_file(resolved)
        return resolved

    def write_file(
        self,
        path: PathLike,
        text: str,
        *,
        newline: str = "\n",
        expected_mtime_ns: Optional[int] = None,
    ) -> int:
        """Write a document atomically and return its new modification time.

        If ``expected_mtime_ns`` is given and the file on disk has a different
        modification time, ``ExternalChangeError`` is raised and nothing is
        written (somebody else edited the file).
        """
        resolved = Path(path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)

        if expected_mtime_ns is not None:
            current = self.get_mtime_ns(resolved)
            if current is not None and current != expected_mtime_ns:
                raise ExternalChangeError(f"{resolved.name} changed on disk.")

        ending = newline if newline in ("\n", "\r\n") else "\n"
        data = text if ending == "\n" else text.replace("\n", ending)
        self._atomic_write_bytes(resolved, data.encode("utf-8"))
        return resolved.stat().st_mtime_ns

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """Write to a temporary file, flush it to disk, then swap it into place.

        ``os.replace`` is atomic, so a crash leaves either the old file or the
        new one, never half of each. It also works on Windows when the target
        already exists (``shutil.move`` / ``os.rename`` do not).
        """
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

            try:  # keep the original permissions (mkstemp makes files 0600)
                mode = stat.S_IMODE(os.stat(path).st_mode)
            except FileNotFoundError:
                mode = 0o666 & ~_current_umask()
            try:
                os.chmod(temporary_name, mode)
            except OSError:
                pass

            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

        FileManager._fsync_directory(directory)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def save_recovery_snapshot(
        self,
        text: str,
        original_path: Optional[str] = None,
        *,
        cursor: int = 0,
        base_mtime_ns: Optional[int] = None,
        newline: str = "\n",
    ) -> None:
        payload = {
            "version": 1,
            "original_path": original_path,
            "text": text,
            "cursor": max(0, int(cursor)),
            "base_mtime_ns": base_mtime_ns,
            "newline": newline if newline in ("\n", "\r\n") else "\n",
            "saved_at": time.time(),
        }
        self._atomic_write_bytes(
            self.recovery_path, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def load_recovery_snapshot(self) -> Optional[dict]:
        try:
            with self.recovery_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return None
        if not isinstance(payload.get("original_path"), (str, type(None))):
            payload["original_path"] = None
        if not isinstance(payload.get("cursor"), int):
            payload["cursor"] = 0
        return payload

    def clear_recovery_snapshot(self) -> None:
        try:
            self.recovery_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def archive_recovery_snapshot(self) -> None:
        """Keep a declined recovery copy as ``recovery.previous.json``."""
        try:
            os.replace(self.recovery_path, self.previous_recovery_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Folder search
    # ------------------------------------------------------------------

    def find_markdown_files(self, folder: PathLike) -> list[Path]:
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            return []

        found: list[Path] = []
        for directory, subdirectories, filenames in os.walk(root):
            subdirectories[:] = sorted(
                d for d in subdirectories if d not in SKIP_DIRECTORIES and not d.startswith(".")
            )
            for name in sorted(filenames):
                if name.lower().endswith(MARKDOWN_SUFFIX) and not name.startswith("."):
                    found.append(Path(directory) / name)
                    if len(found) >= MAX_SEARCH_FILES:
                        return found
        return found

    def search_folder(
        self, folder: PathLike, query: str, case_sensitive: bool = False
    ) -> dict[str, list[tuple[int, str]]]:
        """Search every Markdown file under ``folder``.

        Returns ``{path: [(line_number, line_text), ...]}`` for files with matches.
        """
        results: dict[str, list[tuple[int, str]]] = {}
        if not query:
            return results

        pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
        for path in self.find_markdown_files(folder):
            try:
                if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                with path.open("r", encoding="utf-8-sig") as handle:
                    for number, line in enumerate(handle, start=1):
                        if pattern.search(line):
                            results.setdefault(str(path), []).append(
                                (number, line.rstrip("\r\n"))
                            )
            except (OSError, UnicodeDecodeError):
                continue
        return results

    # ------------------------------------------------------------------
    # Rename / overwrite helpers
    # ------------------------------------------------------------------

    def rename_file(self, old_path: PathLike, new_path: PathLike) -> Path:
        old = Path(old_path).expanduser().resolve()
        new = Path(new_path).expanduser().resolve()

        if not old.exists():
            raise FileNotFoundError(f"File not found: {old}")
        if new.exists():
            raise FileExistsError(f"Destination already exists: {new}")

        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)

        recent = [str(new) if p == str(old) else p for p in self.config.get("recent_files", [])]
        self.config["recent_files"] = recent
        if self.config.get("last_file") == str(old):
            self.config["last_file"] = str(new)
        self.save_config()
        return new

    def confirm_overwrite(self, path: PathLike, confirm: Callable[[str], bool]) -> bool:
        """Ask before replacing an existing file. ``confirm`` returns a bool."""
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            return True
        return bool(confirm(f"Overwrite existing file?\n{resolved}"))
