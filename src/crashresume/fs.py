"""File systems for crash testing.

``SimFS`` is an in-memory file system with an explicit durability model, so a
simulated power cut can lose or tear data that was written but never fsynced:

* file **data** is durable only after ``fsync(path)``;
* file **names** (create, rename, remove) are modelled as immediately durable
  and atomic - this is the optimistic POSIX-with-directory-fsync model, so a
  rename that "works" in the test can still lose data when the renamed file was
  never fsynced (the classic zero-length-file-after-crash bug);
* a write that was not fsynced is lost on a power cut (``power_loss()``), or
  torn to a prefix (``power_loss(torn=True)``).

With ``SimFS(dir_durability=True)`` (``conform(strict_dirs=True)``) names are *not*
durable until ``fsync_dir`` on the containing directory: a file created or renamed
and fsynced, but whose directory was never fsynced, vanishes (or reverts to its old
name) on a power cut. That is the stricter, ext4/XFS-style model.

Every mutating call (``write``, ``fsync``, ``rename``, ``remove``) is a crash
point, exactly like ``ctx.effect``. Reads are free.

``RealFS`` has the same API on a real directory. It is used by process mode
(``conform_process``), where the harness really kills the interpreter, so the
same subject code runs against both.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Union

Data = Union[bytes, str]


def _b(data: Data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    raise TypeError(f"fs data must be bytes or str, got {type(data).__name__}")


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _norm(path: str, portable: bool = False) -> str:
    """Normalise a sandbox-relative path. ``portable=True`` (RealFS on Windows) also
    rejects names Windows cannot store faithfully: reserved device names, ``:`` (NTFS
    alternate data streams), trailing dot or space, and ``<>"|?*``."""
    if not isinstance(path, str) or not path:
        raise ValueError("fs path must be a non-empty str")
    p = path.replace("\\", "/")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if "\x00" in p:
        raise ValueError(f"fs path must not contain NUL: {path!r}")
    if not parts or any(seg == ".." for seg in parts) or p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise ValueError(f"fs path must be relative and stay inside the sandbox: {path!r}")
    if portable:
        for seg in parts:
            if (seg.split(".")[0].rstrip(" ").upper() in _WIN_RESERVED or seg[-1] in ". "
                    or any(c in seg for c in '<>:"|?*')):
                raise ValueError(f"fs path is not portable to Windows: {path!r}")
    return "/".join(parts)


def _parent(norm_path: str) -> str:
    return norm_path.rsplit("/", 1)[0] if "/" in norm_path else ""


class _File:
    __slots__ = ("durable", "current")

    def __init__(self) -> None:
        self.durable = b""
        self.current = b""


class SimFS:
    """In-memory file system with fsync-aware durability. See module docstring."""

    def __init__(self, dir_durability: bool = False) -> None:
        self._files: Dict[str, _File] = {}
        self.dir_durability = dir_durability
        self._dnames: Dict[str, _File] = {}   # names that survive a power cut (dir_durability only)
        self._begin: Callable[[str], Any] = lambda kind: None
        self._end: Callable[[Any, str], None] = lambda armed, kind: None
        self._note: Callable[[str], None] = lambda text: None
        self.ops: List[str] = []  # human-readable trace of mutating calls (for debugging)

    # -- wiring ---------------------------------------------------------
    def _bind(self, begin: Callable[[str], Any], end: Callable[[Any, str], None],
              note: Optional[Callable[[str], None]] = None) -> None:
        self._begin, self._end = begin, end
        self._note = note or (lambda text: None)

    # -- reads (never crash points) -------------------------------------
    def exists(self, path: str) -> bool:
        return _norm(path) in self._files

    def read(self, path: str) -> bytes:
        p = _norm(path)
        if p not in self._files:
            raise FileNotFoundError(path)
        return self._files[p].current

    def read_text(self, path: str) -> str:
        return self.read(path).decode("utf-8", errors="replace")

    def listdir(self, prefix: str = "") -> List[str]:
        pre = "" if not prefix else _norm(prefix).rstrip("/") + "/"
        return sorted(p for p in self._files if p.startswith(pre))

    # -- mutations (crash points) ---------------------------------------
    def write(self, path: str, data: Data, *, append: bool = False) -> None:
        """Write (truncate+write, or append). Not durable until ``fsync``."""
        p, d = _norm(path), _b(data)
        self._note(f"write {p} ({len(d)}B{' append' if append else ''})")
        tok = self._begin("fs.write")
        f = self._files.get(p)
        if f is None:
            f = self._files[p] = _File()
            # a truncating write to an existing file keeps its old durable bytes until fsync
        f.current = (f.current + d) if append else d
        self.ops.append(f"write {p} ({len(d)}B{' append' if append else ''})")
        self._end(tok, "fs.write")

    def write_text(self, path: str, text: str, *, append: bool = False) -> None:
        self.write(path, text, append=append)

    def fsync(self, path: str) -> None:
        p = _norm(path)
        if p not in self._files:
            raise FileNotFoundError(path)
        self._note(f"fsync {p}")
        tok = self._begin("fs.fsync")
        self._files[p].durable = self._files[p].current
        self.ops.append(f"fsync {p}")
        self._end(tok, "fs.fsync")

    def rename(self, src: str, dst: str) -> None:
        """Atomic rename (name change is durable); data durability stays per-file."""
        s, d = _norm(src), _norm(dst)
        if s not in self._files:
            raise FileNotFoundError(src)
        self._note(f"rename {s} -> {d}")
        tok = self._begin("fs.rename")
        self._files[d] = self._files.pop(s)
        self.ops.append(f"rename {s} -> {d}")
        self._end(tok, "fs.rename")

    def remove(self, path: str) -> None:
        p = _norm(path)
        if p not in self._files:
            raise FileNotFoundError(path)
        self._note(f"remove {p}")
        tok = self._begin("fs.remove")
        del self._files[p]
        self.ops.append(f"remove {p}")
        self._end(tok, "fs.remove")

    def fsync_dir(self, path: str = "") -> None:
        """Make the names (creates, renames, removes) directly inside ``path`` durable.

        ``""`` is the root. A crash point in every mode (so subjects behave the same
        with and without ``strict_dirs``); it only changes what survives when
        ``dir_durability`` is on. Directories are implicit: any prefix is valid.
        """
        d = "" if path in ("", ".", "./") else _norm(path)
        self._note(f"fsync_dir {d or '.'}")
        tok = self._begin("fs.fsync_dir")
        if self.dir_durability:
            for name in [n for n in self._dnames if _parent(n) == d]:
                del self._dnames[name]
            for name, f in self._files.items():
                if _parent(name) == d:
                    self._dnames[name] = f
        self.ops.append(f"fsync_dir {d or '.'}")
        self._end(tok, "fs.fsync_dir")

    def write_atomic(self, path: str, data: Data) -> None:
        """The correct recipe: write a temp file, fsync it, rename over the target,
        and (under ``dir_durability``) fsync the directory so the new name sticks."""
        tmp = _norm(path) + ".tmp"
        self.write(tmp, data)
        self.fsync(tmp)
        self.rename(tmp, path)
        if self.dir_durability:
            self.fsync_dir(_parent(_norm(path)))

    # -- the fault ------------------------------------------------------
    def power_loss(self, *, torn: bool = False) -> None:
        """Drop un-fsynced data. ``torn=True`` keeps half of the un-synced tail
        instead, modelling a write that reached the disk partially. With
        ``dir_durability`` names whose directory was never fsynced are lost too."""
        if self.dir_durability:
            self._files = dict(self._dnames)
        for f in self._files.values():
            if f.current == f.durable:
                continue
            if torn:
                if f.current.startswith(f.durable):
                    tail = f.current[len(f.durable):]
                    f.current = f.durable + tail[: len(tail) // 2]
                else:
                    f.current = f.current[: len(f.current) // 2]
            else:
                f.current = f.durable
            f.durable = f.current


class RealFS:
    """Same API as ``SimFS`` on a real directory (used by process mode)."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.dir_durability = False   # a real SIGKILL loses no page cache, so names are never lost
        self._begin: Callable[[str], Any] = lambda kind: None
        self._end: Callable[[Any, str], None] = lambda armed, kind: None
        self._note: Callable[[str], None] = lambda text: None

    def _bind(self, begin: Callable[[str], Any], end: Callable[[Any, str], None],
              note: Optional[Callable[[str], None]] = None) -> None:
        self._begin, self._end = begin, end
        self._note = note or (lambda text: None)

    def _path(self, path: str) -> str:
        full = os.path.abspath(os.path.join(self.root, *_norm(path, os.name == "nt").split("/")))
        if os.path.commonpath([self.root, full]) != self.root:
            raise ValueError(f"fs path escapes the sandbox: {path!r}")
        return full

    def exists(self, path: str) -> bool:
        return os.path.isfile(self._path(path))

    def read(self, path: str) -> bytes:
        with open(self._path(path), "rb") as fh:
            return fh.read()

    def read_text(self, path: str) -> str:
        return self.read(path).decode("utf-8", errors="replace")

    def listdir(self, prefix: str = "") -> List[str]:
        out = []
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                rel = os.path.relpath(os.path.join(base, name), self.root).replace(os.sep, "/")
                out.append(rel)
        pre = "" if not prefix else _norm(prefix).rstrip("/") + "/"
        return sorted(p for p in out if p.startswith(pre))

    def write(self, path: str, data: Data, *, append: bool = False) -> None:
        full = self._path(path)
        self._note(f"write {_norm(path)} ({len(_b(data))}B{' append' if append else ''})")
        tok = self._begin("fs.write")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "ab" if append else "wb") as fh:
            fh.write(_b(data))
        self._end(tok, "fs.write")

    def write_text(self, path: str, text: str, *, append: bool = False) -> None:
        self.write(path, text, append=append)

    def fsync(self, path: str) -> None:
        full = self._path(path)
        self._note(f"fsync {_norm(path)}")
        tok = self._begin("fs.fsync")
        fd = os.open(full, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._end(tok, "fs.fsync")

    def rename(self, src: str, dst: str) -> None:
        a, b = self._path(src), self._path(dst)
        self._note(f"rename {_norm(src)} -> {_norm(dst)}")
        tok = self._begin("fs.rename")
        os.replace(a, b)
        self._end(tok, "fs.rename")

    def remove(self, path: str) -> None:
        full = self._path(path)
        self._note(f"remove {_norm(path)}")
        tok = self._begin("fs.remove")
        os.remove(full)
        self._end(tok, "fs.remove")

    def fsync_dir(self, path: str = "") -> None:
        """fsync a directory so its names are durable (a no-op where directories cannot be opened, e.g. Windows)."""
        d = "" if path in ("", ".", "./") else _norm(path)
        full = self.root if not d else self._path(d)
        self._note(f"fsync_dir {d or '.'}")
        tok = self._begin("fs.fsync_dir")
        if os.name != "nt":
            fd = os.open(full, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self._end(tok, "fs.fsync_dir")

    def write_atomic(self, path: str, data: Data) -> None:
        tmp = _norm(path) + ".tmp"
        self.write(tmp, data)
        self.fsync(tmp)
        self.rename(tmp, path)

    def power_loss(self, *, torn: bool = False) -> None:  # pragma: no cover - a real kill loses nothing
        raise NotImplementedError("RealFS cannot simulate power loss; use SimFS")

