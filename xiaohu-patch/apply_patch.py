#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""XiaoHu "Little" meow patcher for xiaozhi-esp32.

Applies the cat-meow modifications to a pristine xiaozhi-esp32 source tree:

  Step 1  Copy the 7 Opus .ogg meow sounds into ``main/assets/common/``.
  Step 2  C-1: replace ``Lang::Sounds::OGG_POPUP`` with
          ``Lang::Sounds::OGG_MEOW_GREET`` inside
          ``Application::HandleWakeWordDetectedEvent()`` and
          ``Application::StartListeningAudio()`` ONLY.
          The occurrence inside ``Application::StartNotification()`` MUST stay
          untouched (it is the "server pushed you a message" chime).
  Step 3  C-2: register the ``self.cat.meow`` MCP tool at the end of
          ``McpServer::AddCommonTools()`` in ``main/mcp_server.cc``.
  Step 4  Bump ``PROJECT_VER`` in the root ``CMakeLists.txt`` to ``99.0.0`` so
          an official OTA cannot silently overwrite the meow firmware.
  Step 5  Verify every step; exit non-zero with a readable reason otherwise.

The script is idempotent: running it twice produces the same tree.

Usage:
    python3 apply_patch.py [--repo PATH] [--assets PATH] [--snippet PATH]

Exit codes:
    0 = all steps applied and verified
    1 = a step failed (details printed to stdout/stderr)
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: The 7 meow sounds. ``greet`` doubles as the wake-up chime (C-1), so there are
#: 7 files even though the tool exposes 7 emotions and the wake word uses one of
#: them again.
MEOW_ASSETS: Tuple[str, ...] = (
    "meow_greet.ogg",
    "meow_happy.ogg",
    "meow_curious.ogg",
    "meow_comfort.ogg",
    "meow_sulk.ogg",
    "meow_sleepy.ogg",
    "meow_worry.ogg",
)

NEW_PROJECT_VER = "99.0.0"

#: OLD -> NEW symbol replaced by patch C-1.
OLD_SOUND = "Lang::Sounds::OGG_POPUP"
NEW_SOUND = "Lang::Sounds::OGG_MEOW_GREET"

#: Functions whose OGG_POPUP must be rewritten (located by NAME, never by line).
C1_TARGET_FUNCTIONS: Tuple[str, ...] = (
    "HandleWakeWordDetectedEvent",
    "StartListeningAudio",
)

#: Function whose OGG_POPUP must be preserved. Listed so we can log loudly.
C1_PROTECTED_FUNCTIONS: Tuple[str, ...] = ("StartNotification",)

C2_FUNCTION = "AddCommonTools"
C2_MARKER = "self.cat.meow"
C2_ANCHOR_COMMENT = "// Restore the original tools list to the end of the tools list"
C2_REQUIRED_INCLUDE = '#include "assets/lang_config.h"'

APPLICATION_CC = Path("main") / "application.cc"
MCP_SERVER_CC = Path("main") / "mcp_server.cc"
ROOT_CMAKELISTS = Path("CMakeLists.txt")
ASSET_DIR = Path("main") / "assets" / "common"


class PatchError(RuntimeError):
    """Raised when a patch step cannot be completed."""


# --------------------------------------------------------------------------- #
# Logging helpers (CI logs must be readable by a non-programmer)
# --------------------------------------------------------------------------- #


def read_source(path: Path) -> str:
    """Read a source file keeping its original line endings byte-for-byte."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_source(path: Path, text: str) -> None:
    """Write a source file without any newline translation."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def log(message: str = "") -> None:
    """Print a plain progress line."""
    print(f"[xiaohu-patch] {message}" if message else "", flush=True)


def step(number: int, title: str) -> None:
    """Print a step banner."""
    print("", flush=True)
    print(f"[xiaohu-patch] ===== Step {number}: {title} =====", flush=True)


def ok(message: str) -> None:
    """Print a success line."""
    print(f"[xiaohu-patch]   OK   {message}", flush=True)


def skip(message: str) -> None:
    """Print an 'already done / intentionally left alone' line."""
    print(f"[xiaohu-patch]   SKIP {message}", flush=True)


def fail(message: str) -> None:
    """Print a failure line."""
    print(f"[xiaohu-patch]   FAIL {message}", flush=True)


# --------------------------------------------------------------------------- #
# C/C++ brace scanning
# --------------------------------------------------------------------------- #


def _advance_past_literal(text: str, i: int) -> int:
    """Return the index just past the string literal starting at ``text[i]``."""
    n = len(text)
    quote = text[i]
    i += 1
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return n


def _advance_past_raw_literal(text: str, i: int) -> int:
    """Return the index just past the raw string literal ``R"delim(...)delim"``."""
    n = len(text)
    j = i + 2  # skip R"
    delim_start = j
    while j < n and text[j] != "(":
        j += 1
    delimiter = text[delim_start:j]
    closing = ")" + delimiter + '"'
    found = text.find(closing, j)
    if found < 0:
        return n
    return found + len(closing)


def find_function_body(text: str, class_name: str, func_name: str) -> Tuple[int, int]:
    """Locate a member function body by name and return ``(open_idx, close_idx)``.

    ``open_idx``  points at the ``{`` that starts the function body.
    ``close_idx`` points at the matching ``}``.

    Never relies on line numbers: the signature is found with a regex, the body
    with a brace counter that skips comments and string literals.
    """
    pattern = re.compile(
        r"(?m)^[^\n]*?" + re.escape(class_name) + r"::" + re.escape(func_name) + r"\s*\("
    )
    match = pattern.search(text)
    if match is None:
        raise PatchError(
            f"function {class_name}::{func_name}() not found in this file"
        )

    n = len(text)
    i = match.start()
    paren_depth = 0

    # Walk to the first '{' that is not inside the parameter list.
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == "R" and i + 1 < n and text[i + 1] == '"':
            i = _advance_past_raw_literal(text, i)
            continue
        if ch == '"':
            i = _advance_past_literal(text, i)
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == "{" and paren_depth <= 0:
            break
        i += 1

    if i >= n or text[i] != "{":
        raise PatchError(
            f"could not find the opening brace of {class_name}::{func_name}()"
        )

    open_idx = i
    depth = 0
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == "R" and i + 1 < n and text[i + 1] == '"':
            i = _advance_past_raw_literal(text, i)
            continue
        if ch == '"':
            i = _advance_past_literal(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
        i += 1

    raise PatchError(f"unbalanced braces in {class_name}::{func_name}()")


def line_of(text: str, index: int) -> int:
    """Return the 1-based line number of a character offset."""
    return text.count("\n", 0, index) + 1


# --------------------------------------------------------------------------- #
# Step 1 - copy the 7 meow sounds
# --------------------------------------------------------------------------- #


def resolve_assets_dir(explicit: Optional[Path], patch_dir: Path) -> Path:
    """Find the directory that holds the 7 ``meow_*.ogg`` files."""
    candidates: List[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(patch_dir / "assets")
    candidates.append(patch_dir)
    for candidate in candidates:
        if candidate.is_dir() and all(
            (candidate / name).is_file() for name in MEOW_ASSETS
        ):
            return candidate
    raise PatchError(
        "cannot find the 7 meow_*.ogg files. Looked in: "
        + ", ".join(str(c) for c in candidates)
    )


def copy_meow_assets(repo: Path, assets_dir: Path) -> None:
    """Copy the 7 meow sounds into ``main/assets/common/``."""
    target_dir = repo / ASSET_DIR
    if not target_dir.is_dir():
        raise PatchError(
            f"{target_dir} does not exist - is this really a xiaozhi-esp32 checkout?"
        )
    for name in MEOW_ASSETS:
        source = assets_dir / name
        if not source.is_file():
            raise PatchError(f"missing sound file: {source}")
        if source.stat().st_size == 0:
            raise PatchError(f"sound file is empty (0 bytes): {source}")
        destination = target_dir / name
        shutil.copyfile(source, destination)
        ok(f"copied {name} ({source.stat().st_size} bytes) -> {ASSET_DIR.as_posix()}/")


# --------------------------------------------------------------------------- #
# Step 2 - C-1: OGG_POPUP -> OGG_MEOW_GREET in 2 named functions only
# --------------------------------------------------------------------------- #


def apply_c1(repo: Path) -> None:
    """Rewrite the wake-up chime in exactly two functions, by function name."""
    path = repo / APPLICATION_CC
    if not path.is_file():
        raise PatchError(f"{path} not found")
    text = read_source(path)

    protected_counts = {}
    for func_name in C1_PROTECTED_FUNCTIONS:
        open_idx, close_idx = find_function_body(text, "Application", func_name)
        body = text[open_idx:close_idx]
        protected_counts[func_name] = (
            body.count(OLD_SOUND),
            line_of(text, open_idx),
        )
        skip(
            f"Application::{func_name}() (line {line_of(text, open_idx)}) keeps "
            f"{body.count(OLD_SOUND)} x {OLD_SOUND} - this is the 'server pushed "
            f"a message' chime, we must NOT meow here"
        )

    ranges: List[Tuple[int, int]] = []
    for func_name in C1_TARGET_FUNCTIONS:
        open_idx, close_idx = find_function_body(text, "Application", func_name)
        ranges.append((open_idx, close_idx))
        log(
            f"found Application::{func_name}() at line {line_of(text, open_idx)} "
            f"(chars {open_idx}-{close_idx})"
        )

    replaced_total = 0
    # Reverse order keeps earlier offsets valid while we rewrite the string.
    for open_idx, close_idx in sorted(ranges, reverse=True):
        segment = text[open_idx:close_idx]
        count = segment.count(OLD_SOUND)
        if count == 0:
            continue
        text = text[:open_idx] + segment.replace(OLD_SOUND, NEW_SOUND) + text[close_idx:]
        replaced_total += count

    if replaced_total == 0:
        log("no OGG_POPUP left to replace (already patched?)")
    write_source(path, text)
    ok(f"replaced {replaced_total} x {OLD_SOUND} -> {NEW_SOUND}")


def verify_c1(repo: Path) -> None:
    """Assert the two target functions meow and the protected one still pops."""
    text = read_source(repo / APPLICATION_CC)

    for func_name in C1_TARGET_FUNCTIONS:
        open_idx, close_idx = find_function_body(text, "Application", func_name)
        body = text[open_idx:close_idx]
        if NEW_SOUND not in body:
            fail(f"Application::{func_name}() does not contain {NEW_SOUND}")
            raise PatchError(
                f"C-1 failed: Application::{func_name}() still has no meow sound"
            )
        if OLD_SOUND in body:
            fail(f"Application::{func_name}() still contains {OLD_SOUND}")
            raise PatchError(f"C-1 incomplete in Application::{func_name}()")
        ok(f"Application::{func_name}() now plays {NEW_SOUND}")

    for func_name in C1_PROTECTED_FUNCTIONS:
        open_idx, close_idx = find_function_body(text, "Application", func_name)
        body = text[open_idx:close_idx]
        if OLD_SOUND not in body:
            raise PatchError(
                f"C-1 went too far: Application::{func_name}() lost its "
                f"{OLD_SOUND} - the device would meow on every incoming message"
            )
        if NEW_SOUND in body:
            raise PatchError(
                f"C-1 went too far: Application::{func_name}() now contains "
                f"{NEW_SOUND}"
            )
        ok(f"Application::{func_name}() untouched (still {OLD_SOUND})")


# --------------------------------------------------------------------------- #
# Step 3 - C-2: register self.cat.meow
# --------------------------------------------------------------------------- #


def _ensure_include(text: str, include_line: str) -> Tuple[str, bool]:
    """Insert ``include_line`` after the last top-of-file ``#include``."""
    if include_line in text:
        return text, False
    lines = text.split("\n")
    last_include = -1
    for index, line in enumerate(lines[:120]):
        if line.lstrip().startswith("#include"):
            last_include = index
    if last_include < 0:
        raise PatchError("no #include line found in main/mcp_server.cc")
    lines.insert(last_include + 1, include_line)
    return "\n".join(lines), True


def apply_c2(repo: Path, snippet_path: Path) -> None:
    """Insert the ``self.cat.meow`` tool at the end of ``AddCommonTools()``."""
    path = repo / MCP_SERVER_CC
    if not path.is_file():
        raise PatchError(f"{path} not found")
    if not snippet_path.is_file():
        raise PatchError(f"C-2 snippet not found: {snippet_path}")

    snippet = read_source(snippet_path).rstrip("\n")
    if C2_MARKER not in snippet:
        raise PatchError(f"C-2 snippet does not contain {C2_MARKER!r}")

    text = read_source(path)
    open_idx, close_idx = find_function_body(text, "McpServer", C2_FUNCTION)
    body = text[open_idx:close_idx]
    log(
        f"found McpServer::{C2_FUNCTION}() at line {line_of(text, open_idx)} "
        f"(chars {open_idx}-{close_idx})"
    )

    if C2_MARKER in body:
        skip(f"{C2_MARKER} already registered - not inserting twice")
    else:
        # Prefer the "Restore the original tools list" comment as anchor so the
        # meow tool stays in the front block that benefits from the prompt cache.
        # Fall back to the very end of the function body.
        marker_idx = body.find(C2_ANCHOR_COMMENT)
        if marker_idx >= 0:
            line_start = body.rfind("\n", 0, marker_idx) + 1
            at = open_idx + line_start
            text = text[:at] + snippet + "\n\n" + text[at:]
            ok(
                f"inserted {C2_MARKER} before the 'Restore the original tools "
                f"list' block (line {line_of(text, at)})"
            )
        else:
            text = text[:close_idx] + "\n\n" + snippet + "\n" + text[close_idx:]
            ok(f"inserted {C2_MARKER} at the end of McpServer::{C2_FUNCTION}()")

    text, inserted_include = _ensure_include(text, C2_REQUIRED_INCLUDE)
    if inserted_include:
        ok(f"added {C2_REQUIRED_INCLUDE}")
    else:
        skip(f"{C2_REQUIRED_INCLUDE} already present")

    write_source(path, text)


def verify_c2(repo: Path) -> None:
    """Assert the tool is registered inside ``AddCommonTools()``."""
    text = read_source(repo / MCP_SERVER_CC)
    open_idx, close_idx = find_function_body(text, "McpServer", C2_FUNCTION)
    body = text[open_idx:close_idx]
    if C2_MARKER not in body:
        raise PatchError(f"C-2 failed: {C2_MARKER} not inside {C2_FUNCTION}()")
    ok(f"{C2_MARKER} registered inside McpServer::{C2_FUNCTION}()")
    if C2_REQUIRED_INCLUDE not in text:
        raise PatchError(f"C-2 failed: {C2_REQUIRED_INCLUDE} missing")
    ok(f"{C2_REQUIRED_INCLUDE} present")
    for name in MEOW_ASSETS:
        symbol = "OGG_" + name[:-4].upper()
        if symbol not in body:
            raise PatchError(f"C-2 failed: {symbol} missing from the meow table")
    ok("all 7 meow symbols referenced in the tool")


# --------------------------------------------------------------------------- #
# Step 4 - bump PROJECT_VER
# --------------------------------------------------------------------------- #

PROJECT_VER_RE = re.compile(r'(set\(\s*PROJECT_VER\s*")([^"]*)(")')


def apply_version(repo: Path) -> None:
    """Set ``PROJECT_VER`` in the root ``CMakeLists.txt`` to ``99.0.0``."""
    path = repo / ROOT_CMAKELISTS
    if not path.is_file():
        raise PatchError(f"{path} not found")
    text = read_source(path)
    match = PROJECT_VER_RE.search(text)
    if match is None:
        raise PatchError("no set(PROJECT_VER \"...\") line in root CMakeLists.txt")
    if match.group(2) == NEW_PROJECT_VER:
        skip(f"PROJECT_VER already {NEW_PROJECT_VER}")
        return
    old = match.group(2)
    text = PROJECT_VER_RE.sub(r"\g<1>" + NEW_PROJECT_VER + r"\g<3>", text, count=1)
    write_source(path, text)
    ok(f"PROJECT_VER: {old} -> {NEW_PROJECT_VER} (blocks official OTA overwrite)")


def verify_version(repo: Path) -> None:
    """Assert ``PROJECT_VER`` is ``99.0.0``."""
    text = read_source(repo / ROOT_CMAKELISTS)
    match = PROJECT_VER_RE.search(text)
    if match is None or match.group(2) != NEW_PROJECT_VER:
        raise PatchError(f"PROJECT_VER is not {NEW_PROJECT_VER}")
    ok(f"PROJECT_VER == {NEW_PROJECT_VER}")


def verify_assets(repo: Path) -> None:
    """Assert all 7 sounds landed in ``main/assets/common/``."""
    target_dir = repo / ASSET_DIR
    for name in MEOW_ASSETS:
        candidate = target_dir / name
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise PatchError(f"missing/empty after copy: {candidate}")
    ok(f"all {len(MEOW_ASSETS)} meow sounds present in {ASSET_DIR.as_posix()}/")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def detect_repo_root(explicit: Optional[Path], patch_dir: Path) -> Path:
    """Return the xiaozhi-esp32 source root."""
    candidates: List[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(Path.cwd())
    candidates.append(patch_dir.parent)  # <repo>/xiaohu-patch/apply_patch.py
    candidates.append(patch_dir.parent.parent)
    for candidate in candidates:
        try:
            if (candidate / APPLICATION_CC).is_file():
                return candidate.resolve()
        except OSError:
            continue
    raise PatchError(
        "cannot locate the xiaozhi-esp32 root (no main/application.cc found). "
        "Pass it explicitly: python3 apply_patch.py --repo /path/to/src"
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Run every patch step and verify it. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description="Apply the XiaoHu cat-meow patch to xiaozhi-esp32."
    )
    parser.add_argument("--repo", type=Path, default=None,
                        help="xiaozhi-esp32 source root (auto-detected by default)")
    parser.add_argument("--assets", type=Path, default=None,
                        help="directory holding the 7 meow_*.ogg files")
    parser.add_argument("--snippet", type=Path, default=None,
                        help="path to c2_snippet.cpp")
    args = parser.parse_args(argv)

    patch_dir = Path(__file__).resolve().parent
    log("XiaoHu cat-meow patcher")
    log(f"patch dir: {patch_dir}")

    try:
        repo = detect_repo_root(args.repo, patch_dir)
        log(f"repo root: {repo}")

        step(1, f"copy {len(MEOW_ASSETS)} meow sounds into {ASSET_DIR.as_posix()}/")
        assets_dir = resolve_assets_dir(args.assets, patch_dir)
        log(f"assets dir: {assets_dir}")
        copy_meow_assets(repo, assets_dir)
        verify_assets(repo)

        step(2, "C-1: wake-up chime OGG_POPUP -> OGG_MEOW_GREET (2 functions only)")
        apply_c1(repo)
        verify_c1(repo)

        step(3, f"C-2: register {C2_MARKER} in McpServer::{C2_FUNCTION}()")
        snippet_path = args.snippet or (patch_dir / "c2_snippet.cpp")
        apply_c2(repo, snippet_path)
        verify_c2(repo)

        step(4, f"bump PROJECT_VER to {NEW_PROJECT_VER}")
        apply_version(repo)
        verify_version(repo)

        step(5, "final verification")
        verify_assets(repo)
        verify_c1(repo)
        verify_c2(repo)
        verify_version(repo)

    except PatchError as error:
        print("", flush=True)
        fail(str(error))
        print(
            "[xiaohu-patch] PATCH FAILED - the build is stopped on purpose so "
            "you do not flash a half-patched firmware.",
            flush=True,
        )
        return 1

    print("", flush=True)
    log("ALL PATCH STEPS APPLIED AND VERIFIED")
    log("next: idf.py set-target esp32s3 && idf.py build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
