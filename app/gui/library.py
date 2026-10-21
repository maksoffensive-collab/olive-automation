"""A simple local macro folder: one deterministic JSON file per macro (`<slug>.macro.json`). No database.

Macros may sit in the root or in ONE level of user-named sub-folders ("Daily tasks", "Тесты", ...), which are
real directories on disk -- the folder a macro is in IS where its file lives, so nothing can disagree with the
file system and a folder can be opened in the file manager. Deliberately one level only: a tree adds ambiguity
(drag targets, breadcrumbs, moves into descendants) for no benefit at this size.

Only files this class lists can be loaded, moved, renamed or deleted, and only inside its own directory. Saving
never overwrites an existing file unless the caller passes `overwrite=True` (the GUI asks the user first). Files
are written to a temporary name and moved into place, so a failed save never leaves a half-written macro. The
stored format is the existing `automation-macro` JSON (no handles, no wall-clock times, no secrets).

`MacroInfo.key` is the macro's path relative to this folder, in posix form ("Daily tasks/morning.macro.json"). It is
the stable name other GUI-side stores (favourites, descriptions, run history) use to refer to a macro, and it is
kept in step by `move` / `rename`.

Both macro versions live here side by side. Every file is read through `app.decide.format`, which hands a v1 file
to the unchanged v1 code, so a v1 macro is listed, loaded and saved exactly as before. A v2 macro may own a folder
of template images next to it (`<stem>.assets/`, see `app.decide.format`); that folder belongs to the macro and is
never shown as a folder of its own. It travels with its macro: moving, renaming or deleting the macro does the
same to it, and if the pair cannot be handled as a whole the operation is refused or rolled back rather than
leaving a macro without its templates. An asset folder that holds anything other than template files is never
moved or deleted -- the operation is refused, because what is in there is not the platform's to throw away.

Listing never opens a template: `summarize_macro_text` validates each file by its text alone. Templates are read
and hash-checked only when one macro is opened (`load`).
"""
from __future__ import annotations

import os
import re
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from app.decide import ConditionalMacro
from app.decide.format import (ASSET_DIR_SUFFIX, ASSET_NAME, asset_dir_for, conditional_to_json, load_macro_file,
                               referenced_assets, save_macro_file, summarize_macro_text, validate_macro)
from app.playback.format import MacroFormatError, macro_to_json
from app.playback.macro import Macro

SUFFIX = ".macro.json"
MAX_SLUG = 60
MAX_FOLDER = 60
_FOLDER_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
AnyMacro = Macro | ConditionalMacro


def _is_asset_folder(name: str) -> bool:
    return name.casefold().endswith(ASSET_DIR_SUFFIX)


class LibraryError(Exception):
    """A macro file could not be saved, loaded or removed. The message is meant for the user, so it also
    carries the translation key of that same sentence (`key`/`params`); the English text stays as the log
    line and as the fallback."""

    def __init__(self, message: str, key: str = "", **params) -> None:
        super().__init__(message)
        self.key, self.params = key, params


class MacroExists(LibraryError):
    """Saving would overwrite an existing file and the caller did not allow it."""


@dataclass(frozen=True)
class MacroInfo:
    path: Path
    name: str
    provider_id: str         # v1 only: what it was recorded for ("" for v2, which is bound to no target)
    target_id: str
    events: int
    actions: int             # the macro's size in its own unit: actions for v1, steps for v2
    error: str = ""          # non-empty: the file is not a valid macro (it is listed, never loaded)
    folder: str = ""         # "" = the root of the macro folder
    modified: float = 0.0    # file mtime, for "last changed" and sorting
    key: str = ""            # path relative to the macro folder, posix form; stable id for favourites/history
    version: int = 1         # 1 or 2; 0 when the file could not be read at all
    stamp: tuple | None = None   # file_stamp() when listed: WHICH version of the file this row (and its details) is

    @property
    def valid(self) -> bool:
        return not self.error

    @property
    def is_v2(self) -> bool:
        """A script (file version 2, or 3 since nested branches): not a recorded macro."""
        return self.version >= 2


def _stamp_of(status: os.stat_result) -> tuple:
    return status.st_size, status.st_mtime_ns, status.st_ino


def file_stamp(path: str | Path) -> tuple | None:
    """Which version of a file this is, cheaply: its size, change time and file id. Every rewrite -- a save, a copy
    over it in the file manager, a delete and a new file of the same name -- gives a different stamp. None when the
    file cannot be read."""
    try:
        return _stamp_of(os.stat(path))
    except OSError:
        return None


def slugify(name: str) -> str:
    """File-name stem of a macro name. Letters and digits of ANY script are kept (so "Åventyr" and "Фарм" stay
    distinct and readable); everything else, including every path separator, becomes "_"."""
    text = unicodedata.normalize("NFC", name.strip()).lower()
    return re.sub(r"[^\w-]+", "_", text).strip("_")[:MAX_SLUG]


def clean_folder(name: str) -> str:
    """A folder name that is safe as a single directory name, or "" for the root. Raises for anything that could
    leave the macro folder or that Windows cannot store."""
    folder = unicodedata.normalize("NFC", str(name)).strip()
    if not folder:
        return ""
    if folder in (".", "..") or _FOLDER_BAD.search(folder) or folder.endswith((".", " ")):
        raise LibraryError("A folder name cannot contain \\ / : * ? \" < > | or end with a dot or space.",
                           "err_folder_chars")
    if len(folder) > MAX_FOLDER:
        raise LibraryError(f"A folder name can be at most {MAX_FOLDER} characters.", "err_folder_long",
                           count=MAX_FOLDER)
    if _is_asset_folder(folder):
        raise LibraryError(f"A folder name cannot end with “{ASSET_DIR_SUFFIX}”: that ending marks the template "
                           "images of a macro.", "err_folder_reserved", suffix=ASSET_DIR_SUFFIX)
    return folder


class MacroLibrary:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    # ---- locations ------------------------------------------------------------------------------------
    def _root(self) -> Path:
        return self.directory.resolve()

    def folder_path(self, folder: str) -> Path:
        folder = clean_folder(folder)
        return self.directory / folder if folder else self.directory

    def path_for(self, name: str, folder: str = "") -> Path:
        slug = slugify(name)
        if not slug:
            raise LibraryError("Give the macro a name (letters or digits).", "err_macro_name")
        return self.folder_path(folder) / f"{slug}{SUFFIX}"

    def key_of(self, path: str | Path) -> str:
        try:
            return Path(path).resolve().relative_to(self._root()).as_posix()
        except (ValueError, OSError):
            return Path(path).name

    def _owned(self, path: str | Path) -> Path:
        """The path, proven to be a macro file inside this folder (root or one of its sub-folders)."""
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
            inside = resolved.is_relative_to(self._root())
        except OSError:
            inside = False
        if not inside or not candidate.name.endswith(SUFFIX):
            raise LibraryError("That file is not part of the macro folder.", "err_not_ours")
        return candidate

    # ---- folders --------------------------------------------------------------------------------------
    def folders(self) -> list[str]:
        """User folders only: a macro's own template folder (`<stem>.assets`) is part of that macro, not a place
        to keep macros in, so it is never offered as one."""
        if not self.directory.is_dir():
            return []
        return sorted((p.name for p in self.directory.iterdir() if p.is_dir() and not _is_asset_folder(p.name)),
                      key=str.casefold)

    def create_folder(self, name: str) -> str:
        folder = clean_folder(name)
        if not folder:
            raise LibraryError("Give the folder a name.", "err_folder_name")
        path = self.directory / folder
        if path.exists():
            raise LibraryError(f"A folder named “{folder}” already exists.", "err_folder_exists", name=folder)
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            raise LibraryError(f"The folder could not be created: {exc.strerror or type(exc).__name__}.",
                               "err_folder_create", reason=(exc.strerror or type(exc).__name__)) from None
        return folder

    def rename_folder(self, old: str, new: str) -> str:
        source, target = self.folder_path(old), self.folder_path(new)
        if source == self.directory or target == self.directory:
            raise LibraryError("Give the folder a name.", "err_folder_name")
        if target.exists():
            raise LibraryError(f"A folder named “{target.name}” already exists.", "err_folder_exists",
                               name=target.name)
        try:
            source.rename(target)
        except OSError as exc:
            raise LibraryError(f"The folder could not be renamed: {exc.strerror or type(exc).__name__}.") from None
        return target.name

    def delete_folder(self, name: str) -> None:
        """Only an EMPTY folder is removed: deleting a folder must never delete someone's macros as a side
        effect. The caller moves or deletes the macros first, deliberately."""
        path = self.folder_path(name)
        if path == self.directory:
            raise LibraryError("Give the folder a name.", "err_folder_name")
        if not path.is_dir():
            raise LibraryError("That folder no longer exists.", "err_folder_gone")
        if any(path.iterdir()):
            raise LibraryError("This folder is not empty. Move or delete the macros in it first.",
                               "err_folder_not_empty")
        try:
            path.rmdir()
        except OSError as exc:
            raise LibraryError(f"The folder could not be deleted: {exc.strerror or type(exc).__name__}.") from None

    # ---- macros ---------------------------------------------------------------------------------------
    def _read(self, path: Path, folder: str) -> MacroInfo:
        try:
            status = path.stat()             # BEFORE the read: a change after it shows as a newer stamp, never hides
            modified, stamp = status.st_mtime, _stamp_of(status)
        except OSError:
            modified, stamp = 0.0, None
        key = self.key_of(path)
        try:
            summary = summarize_macro_text(path.read_text(encoding="utf-8"))
            return MacroInfo(path, summary.name or path.name[:-len(SUFFIX)], summary.provider_id, summary.target_id,
                             summary.events, summary.size, "", folder, modified, key, summary.version, stamp)
        except (MacroFormatError, OSError, UnicodeError) as exc:
            return MacroInfo(path, path.name[:-len(SUFFIX)], "", "", 0, 0, str(exc), folder, modified, key, 0, stamp)

    def list(self) -> list[MacroInfo]:
        """Every macro in the root and in each sub-folder. Unreadable files are listed with their error, never
        hidden: a file the user can see in Explorer must be visible (and removable) here too."""
        if not self.directory.is_dir():
            return []
        out = [self._read(p, "") for p in sorted(self.directory.glob(f"*{SUFFIX}"))]
        for folder in self.folders():
            out += [self._read(p, folder) for p in sorted((self.directory / folder).glob(f"*{SUFFIX}"))]
        return out

    # ---- a macro's template folder ---------------------------------------------------------------------
    @staticmethod
    def _assets_of(path: Path) -> Path | None:
        """The macro's own asset folder if there is one, proven to hold nothing but template files. Anything else
        in it -- a sub-folder, a link, a file the platform did not write -- makes the operation refuse: moving,
        renaming or deleting a macro must never carry off or destroy something that is not a template."""
        folder = asset_dir_for(path)
        linked = folder.is_symlink() or folder.is_junction()
        if not linked and not folder.exists():
            return None
        if linked or not folder.is_dir():
            raise LibraryError(f"“{folder.name}” is not a plain folder of templates; it was left alone.",
                               "err_assets_foreign", name=folder.name)
        for child in folder.iterdir():
            if child.is_symlink() or not child.is_file() or not ASSET_NAME.match(child.name):
                raise LibraryError(f"“{folder.name}” holds “{child.name}”, which is not a template of this macro. "
                                   "Move it out first; nothing was changed.", "err_assets_foreign_file",
                                   name=folder.name, file=child.name)
        return folder

    @staticmethod
    def _occupied(folder: Path) -> bool:
        return folder.exists() or folder.is_symlink() or folder.is_junction()

    @staticmethod
    def _discard(folder: Path) -> None:
        """Remove a template folder that no macro refers to any more. Best effort: the macro is already gone,
        so a file that cannot be removed right now is only left-over space, never a broken macro."""
        for child in folder.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass

    @staticmethod
    def _prune(folder: Path, keep: frozenset[str]) -> None:
        """After a successful save: drop the templates the saved macro no longer refers to. Only files with a
        template name are ever touched."""
        if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
            return
        for child in folder.iterdir():
            if ASSET_NAME.match(child.name) and child.name not in keep and child.is_file() \
                    and not child.is_symlink():
                try:
                    child.unlink()
                except OSError:
                    pass
        try:
            if not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass

    # ---- saving and loading ------------------------------------------------------------------------------
    def save(self, macro: AnyMacro, *, folder: str = "", overwrite: bool = False) -> Path:
        if not macro.name.strip():
            raise LibraryError("Give the macro a name (letters or digits).", "err_macro_name")
        return self._write(macro, self.path_for(macro.name, folder), overwrite)

    def save_over(self, macro: AnyMacro, path: Path) -> Path:
        """Replace exactly the listed file `path` -- the one being edited -- whatever the name inside it says. A file
        copied in the file manager keeps the name of what it was copied from; saving the copy must change the copy,
        never the file whose name it carries."""
        path = self._owned(path)
        if not macro.name.strip():
            raise LibraryError("Give the macro a name (letters or digits).", "err_macro_name")
        return self._write(macro, path, True)

    def _write(self, macro: AnyMacro, path: Path, overwrite: bool) -> Path:
        if path.exists() and not overwrite:
            raise MacroExists(f"A macro named “{path.name[:-len(SUFFIX)]}” already exists.", "err_macro_exists",
                              name=path.name[:-len(SUFFIX)])
        if isinstance(macro, ConditionalMacro):
            return self._save_v2(macro, path, overwrite)
        tmp_name = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = macro_to_json(macro)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="save-", suffix=".tmp")   # unique per save
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            if overwrite:
                os.replace(tmp_name, path)
            else:
                try:
                    os.link(tmp_name, path)          # fails if the name exists NOW: never overwrites, even in a race
                except FileExistsError:
                    raise MacroExists(f"A macro named “{path.name[:-len(SUFFIX)]}” already exists.", "err_macro_exists",
                              name=path.name[:-len(SUFFIX)]) from None
        except OSError as exc:
            raise LibraryError(f"The macro could not be saved: {exc.strerror or type(exc).__name__}.",
                               "err_macro_save", reason=(exc.strerror or type(exc).__name__)) from None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)              # nothing is ever left behind, whatever happened
                except FileNotFoundError:
                    pass
        return path

    def _save_v2(self, macro: ConditionalMacro, path: Path, overwrite: bool) -> Path:
        """Through the v2 serializer and nothing else: it validates the whole macro, writes the templates first
        (content-named, so an existing one is never changed) and then the file itself by temporary name + move,
        so a failure leaves the previous version of the file as it was."""
        assets = asset_dir_for(path)
        if assets.is_symlink() or assets.is_junction() or (assets.exists() and not assets.is_dir()):
            raise LibraryError(f"“{assets.name}” is not a plain folder of templates; nothing was saved.",
                               "err_assets_foreign", name=assets.name)
        try:
            validate_macro(macro)                    # what is written must read back as exactly this macro
            path.parent.mkdir(parents=True, exist_ok=True)
            save_macro_file(macro, path, overwrite=overwrite)
        except MacroFormatError as exc:
            if not overwrite and path.exists():
                raise MacroExists(f"A macro named “{path.name[:-len(SUFFIX)]}” already exists.", "err_macro_exists",
                                  name=path.name[:-len(SUFFIX)]) from None
            raise LibraryError(f"The macro could not be saved: {exc}", "err_macro_save", reason=str(exc)) from None
        except OSError as exc:
            raise LibraryError(f"The macro could not be saved: {exc.strerror or type(exc).__name__}.",
                               "err_macro_save", reason=(exc.strerror or type(exc).__name__)) from None
        self._prune(assets, referenced_assets(macro))
        return path

    def load(self, path: Path) -> AnyMacro:
        """A v1 `Macro` or a v2 `ConditionalMacro`, exactly as `app.decide.format.load_macro_file` reads it: a v2
        macro's templates are read here, once, and each must hash to its own name."""
        path = self._owned(path)
        try:
            return load_macro_file(path)
        except MacroFormatError as exc:
            raise LibraryError(f"Not a valid macro: {exc}", "err_macro_bad_file", reason=str(exc)) from None
        except OSError as exc:
            raise LibraryError(f"The macro could not be read: {exc.strerror or type(exc).__name__}.",
                               "err_macro_read", reason=(exc.strerror or type(exc).__name__)) from None

    # ---- delete, move, rename: the macro and its templates together ---------------------------------------
    def delete(self, path: Path) -> None:
        path = self._owned(path)
        assets = self._assets_of(path)
        parked = None
        try:
            if assets is not None:
                # Out of the way first, under a name no macro can have: if the file then cannot be removed, the
                # folder goes straight back and nothing has changed.
                parked = assets.with_name(f".deleting-{uuid.uuid4().hex}{ASSET_DIR_SUFFIX}")
                assets.rename(parked)
            try:
                path.unlink()
            except OSError:
                if parked is not None:
                    parked.rename(assets)
                raise
        except OSError as exc:
            raise LibraryError(f"The macro could not be deleted: {exc.strerror or type(exc).__name__}.",
                               "err_macro_delete", reason=(exc.strerror or type(exc).__name__)) from None
        if parked is not None:
            self._discard(parked)

    def _relocate(self, path: Path, destination: Path, *, error_key: str = "err_macro_move") -> Path:
        if destination == path:
            return path
        if destination.exists():
            raise MacroExists(f"A macro named “{destination.name[:-len(SUFFIX)]}” already exists there.",
                              "err_macro_exists_there", name=destination.name[:-len(SUFFIX)])
        assets = self._assets_of(path)
        target_assets = asset_dir_for(destination)
        if assets is not None and self._occupied(target_assets):
            raise LibraryError(f"“{target_assets.name}” already exists there; nothing was moved.",
                               "err_assets_exist_there", name=target_assets.name)
        moved = False
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if assets is not None:
                assets.rename(target_assets)
                moved = True
            try:
                os.link(path, destination)           # never replaces an existing name, even in a race
            except BaseException:
                if moved:
                    target_assets.rename(assets)
                raise
            try:
                path.unlink()
            except BaseException:
                destination.unlink()
                if moved:
                    target_assets.rename(assets)
                raise
        except FileExistsError:
            raise MacroExists(f"A macro named “{destination.name[:-len(SUFFIX)]}” already exists there.",
                              "err_macro_exists_there", name=destination.name[:-len(SUFFIX)]) from None
        except OSError as exc:
            raise LibraryError(f"The macro could not be moved: {exc.strerror or type(exc).__name__}.",
                               error_key, reason=(exc.strerror or type(exc).__name__)) from None
        return destination

    def move(self, path: Path, folder: str) -> Path:
        """Move a macro into `folder` ("" = the root). The file keeps its name; its templates go with it."""
        path = self._owned(path)
        return self._relocate(path, self.folder_path(folder) / path.name)

    def rename(self, path: Path, new_name: str) -> Path:
        """Rename a macro: the name INSIDE the file, its file name and its template folder are changed together,
        so the list and the file manager never disagree. The macro stays in its folder."""
        path = self._owned(path)
        macro = self.load(path)
        name = new_name.strip()
        try:
            renamed = (Macro(macro.recording, name) if isinstance(macro, Macro)
                       else replace(macro, name=name))          # a script keeps all but its name: delay, places
        except ValueError as exc:
            raise LibraryError(str(exc)) from None
        destination = path.parent / self.path_for(name).name
        same = destination == path
        if not same and destination.exists():
            raise MacroExists(f"A macro named “{name}” already exists in this folder.", "err_macro_exists",
                              name=name)
        assets = self._assets_of(path)
        target_assets = asset_dir_for(destination)
        if not same and assets is not None and self._occupied(target_assets):
            raise LibraryError(f"“{target_assets.name}” already exists in this folder; nothing was renamed.",
                               "err_assets_exist_there", name=target_assets.name)
        tmp_name = None
        moved = linked = False
        try:
            text = macro_to_json(renamed) if isinstance(renamed, Macro) else conditional_to_json(renamed)[0]
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="save-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            if same:
                os.replace(tmp_name, destination)
            else:
                if assets is not None:
                    assets.rename(target_assets)
                    moved = True
                os.link(tmp_name, destination)       # never replaces a file that appeared meanwhile
                linked = True
                path.unlink()
        except (OSError, MacroFormatError) as exc:
            if linked:
                try:
                    destination.unlink()
                except OSError:
                    pass
            if moved:
                try:
                    target_assets.rename(assets)
                except OSError:
                    pass
            reason = exc.strerror or type(exc).__name__ if isinstance(exc, OSError) else str(exc)
            raise LibraryError(f"The macro could not be renamed: {reason}.", "err_macro_rename",
                               reason=reason) from None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        return destination
