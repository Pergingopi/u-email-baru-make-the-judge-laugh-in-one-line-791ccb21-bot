"""One-line joke entry for the "Make the Judge Laugh in One Line" bounty.

The program writes a single fixed joke to ``entry.txt`` inside the
directory named by ``MAKEFUN_OUTPUT_DIR`` (``/output`` when the
variable is unset or blank). The same line is produced on every run;
there is no randomness, no clock dependence, and no input to read.

The sealed scorer only accepts a UTF-8 file of 1 to 200 characters
that is not whitespace alone. This module is deliberately stricter
than that: it rejects every control character (tabs included),
every Unicode line or paragraph separator, every invisible format
character, and any leading or trailing whitespace, so the file that
lands on disk is exactly one clean line.

Publication is careful rather than clever. The payload is staged in
a temporary file inside the output directory, flushed and fsynced,
read back and re-checked, then moved into place with an atomic
rename. The directory itself is fsynced so the rename survives a
crash, and the published file is read back a second time together
with a mode check. Temp files abandoned by a crashed earlier run
are swept up first, but only files this program could have made,
and only once they are old enough that no live run could own them.
"""

import os
import stat
import tempfile
import time
import unicodedata

ENTRY_LINE = (
    "My smart fridge just emailed me: 'We need to talk about "
    "your 2 a.m. decisions.'"
)

FALLBACK_OUTPUT_DIR = "/output"
STAGING_PREFIX = ".entry-"
STAGING_SUFFIX = ".tmp"
# A live run holds its staging file for milliseconds, so anything
# this old can only be the leavings of a run that died mid-write.
# The age floor also protects a concurrent run: its fresh staging
# file is never old enough to be swept by a neighbour.
ABANDONED_STAGING_MIN_AGE = 60.0
PUBLISHED_MODE = 0o644


def _output_directory():
    """Pick the output directory from the environment.

    A missing variable, an empty value, or a value that is only
    whitespace all fall back to the default directory.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    if raw is None:
        return FALLBACK_OUTPUT_DIR
    cleaned = raw.strip()
    return cleaned or FALLBACK_OUTPUT_DIR


def _is_forbidden_codepoint(ch):
    """True when a single character may never appear in the entry.

    Forbidden means: any control, format, surrogate, line-separator
    or paragraph-separator character (Unicode categories Cc, Cf,
    Cs, Zl, Zp), plus every Unicode noncharacter -- U+FDD0 through
    U+FDEF and any code point ending in FFFE or FFFF.
    """
    if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
        return True
    code = ord(ch)
    if 0xFDD0 <= code <= 0xFDEF:
        return True
    return (code & 0xFFFE) == 0xFFFE


def _passes_gate(text):
    """Strict check that a candidate line may be published.

    The line must be a string of 1 to 200 characters, contain
    something besides whitespace, have no leading or trailing
    whitespace, encode cleanly as UTF-8 (a lone surrogate cannot),
    and hold no forbidden code point.
    """
    if not isinstance(text, str):
        return False
    if not 1 <= len(text) <= 200:
        return False
    if not text.strip():
        return False
    if text != text.strip():
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(_is_forbidden_codepoint(ch) for ch in text)


def _clear_abandoned_staging_files(out_dir):
    """Delete staging files left behind by runs that crashed.

    Only files wearing this program's prefix and suffix are touched,
    and only once they pass the minimum age. Any error is swallowed:
    a failed sweep must never stop the run. Returns how many files
    were removed.
    """
    now = time.time()
    try:
        names = os.listdir(out_dir)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not (name.startswith(STAGING_PREFIX)
                and name.endswith(STAGING_SUFFIX)):
            continue
        path = os.path.join(out_dir, name)
        try:
            if now - os.path.getmtime(path) < ABANDONED_STAGING_MIN_AGE:
                continue
            os.unlink(path)
            removed += 1
        except OSError:
            continue
    return removed


def _decode_checked(raw_bytes, path):
    """Decode bytes as UTF-8, failing loudly on bad input."""
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit("read-back is not valid UTF-8 in %s" % path)


def _verify_published_bytes(path, expected_bytes):
    """Read a file back and demand it match the payload exactly.

    The bytes must be identical, and the decoded text must pass the
    gate check. Returns the decoded text.
    """
    with open(path, "rb") as handle:
        back = handle.read()
    if back != expected_bytes:
        raise SystemExit("read-back mismatch in %s" % path)
    text = _decode_checked(back, path)
    if not _passes_gate(text):
        raise SystemExit("read-back fails the gate check in %s" % path)
    return text


def _prepare_payload():
    """Validate the fixed entry line and encode it for writing."""
    if not _passes_gate(ENTRY_LINE):
        raise SystemExit("entry line fails the gate check")
    return ENTRY_LINE.encode("utf-8")


def _write_staging_file(out_dir, payload):
    """Write the payload to a staging file and fsync it.

    Returns the staging path. The file is created with mode 0644
    before and after the write so no umask can leave it wider.
    """
    fd, staging_path = tempfile.mkstemp(
        prefix=STAGING_PREFIX, suffix=STAGING_SUFFIX, dir=out_dir
    )
    # From here on the open descriptor belongs to the ``with`` block
    # below; the except clause only runs before that handover, when
    # the descriptor is still ours to clean up.
    try:
        os.chmod(staging_path, PUBLISHED_MODE)
        with os.fdopen(fd, "wb") as handle:
            written = handle.write(payload)
            if written != len(payload):
                raise SystemExit(
                    "short write: %d of %d bytes" % (written, len(payload))
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staging_path, PUBLISHED_MODE)
    except BaseException:
        try:
            os.unlink(staging_path)
        except OSError:
            pass
        raise
    return staging_path


def _fsync_directory(out_dir):
    """Fsync the output directory so the rename is durable."""
    dir_fd = os.open(out_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _publish_atomically(out_dir, payload):
    """Stage, verify, rename into place, and verify the target."""
    _clear_abandoned_staging_files(out_dir)
    staging_path = _write_staging_file(out_dir, payload)
    try:
        # Verify the staged copy before it becomes the real file.
        _verify_published_bytes(staging_path, payload)

        target = os.path.join(out_dir, "entry.txt")
        os.replace(staging_path, target)
        os.chmod(target, PUBLISHED_MODE)
        _fsync_directory(out_dir)

        # Verify the published file itself, and its mode.
        _verify_published_bytes(target, payload)
        mode = stat.S_IMODE(os.stat(target).st_mode)
        if mode != PUBLISHED_MODE:
            raise SystemExit("output mode is not 0644: %o" % mode)
    finally:
        if os.path.exists(staging_path):
            os.unlink(staging_path)


def main():
    out_dir = _output_directory()
    os.makedirs(out_dir, exist_ok=True)
    payload = _prepare_payload()
    _publish_atomically(out_dir, payload)


if __name__ == "__main__":
    main()
