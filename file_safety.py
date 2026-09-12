"""
Path-traversal-safe file resolution for the /download/{step,stl} routes.

The original handlers did `Path("./output") / filename` with `filename`
taken directly from the URL path parameter, then served whatever existed
at that path. FastAPI path parameters can contain encoded slashes and
`..` segments, so a request like `/download/step/..%2F..%2F..%2Fetc%2Fpasswd`
resolves outside `./output` entirely - a classic path traversal, and the
one concrete security bug in this codebase worth calling out by name
rather than folding into the general "add rate limiting" pass.

`safe_output_path()` is the fix: it takes only the basename (discarding
any directory components the client tried to smuggle in) and then
confirms the *resolved* path is still inside the output directory before
returning it - defense in depth in case some future change reintroduces
a symlink or `..` some other way.
"""

from __future__ import annotations

from pathlib import Path

from exceptions import NitocadError


class InvalidFilenameError(NitocadError):
    status_code = 400


def safe_output_path(output_dir: Path, filename: str) -> Path:
    """Resolve `filename` inside `output_dir`, rejecting anything that
    would escape it. Raises InvalidFilenameError (400) rather than
    silently truncating, so a client sending something suspicious gets a
    clear error instead of a confusing 404 for an unrelated file."""
    if not filename or filename in {".", ".."}:
        raise InvalidFilenameError("Invalid filename")

    # Path(...).name strips any directory components (both "/" and,
    # cross-platform, "\\") - "../../etc/passwd" becomes "passwd".
    basename = Path(filename).name
    if basename != filename:
        raise InvalidFilenameError("Filename must not contain path separators")

    candidate = (output_dir / basename).resolve()
    resolved_root = output_dir.resolve()
    if resolved_root not in candidate.parents and candidate != resolved_root:
        raise InvalidFilenameError("Resolved path escapes the output directory")

    return candidate
