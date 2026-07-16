"""Desired-state CRUD for MCP servers.

Sits above the repo: generates identity (id/slug), computes the idempotency
``config_hash``, validates the runner, and owns the import/export of the standard
``mcpServers`` JSON shape. Never spawns processes — that's the reconciler's job.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shlex
import tempfile
import threading
from contextlib import contextmanager
from functools import lru_cache, wraps
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlmodel import Session

from app.config import get_settings
from app.db import repo
from app.db.models import RUNNERS, Server
from app.registry import settings as runtime_settings
from app.runners import remote as remote_runner
from app.runners.docker import is_forbidden_container_env, is_reserved_docker_env
from app.runners.remote import canonical_transport
from app.util import config_hash, config_hash_tag, new_id, slugify

logger = logging.getLogger(__name__)


def _launcher_basename(command: str) -> str:
    """Basename of a launcher path, splitting on BOTH separators regardless of platform.

    ``os.path.basename`` on POSIX leaves a Windows path (``C:\\…\\docker.exe``) intact, so a
    Claude-Desktop-on-Windows config would miss the launcher tables. Normalize both slashes."""
    return command.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()


# --- remote (HTTP/SSE upstream) normalization ----------------------------------
# The transport vocabulary lives in one place (app.runners.remote); we canonicalize
# through it so the stored value is always canonical and config_hash (and therefore
# reconcile) is deterministic regardless of input spelling.
def normalize_remote(command: str, args: Optional[list[str]]) -> tuple[str, list[str]]:
    """Validate + canonicalize a remote server's (url, [transport]).

    ``command`` must be a well-formed ``http(s)://`` URL with a host; ``args[0]`` (if
    any) is the transport, defaulting to ``streamable-http``. Returns the canonical
    pair so the row — and its ``config_hash`` — is deterministic. Raises ``ValueError``
    on a malformed URL or unsupported transport.
    """
    url = (command or "").strip()
    # Parse rather than prefix-match: reject schemeless, hostless (https://), or
    # whitespace-bearing values that would only fail later when the bridge connects.
    # urlsplit lowercases the scheme, so an uppercase HTTPS:// is accepted.
    parsed = urlsplit(url)
    # Check hostname, not just netloc: "https://:443/mcp" has a truthy netloc (":443")
    # but no host, and would only fail later when the bridge tries to connect. Also
    # validate the port — `parsed.port` raises ValueError on a malformed one (":bad").
    try:
        parsed.port  # noqa: B018 — property access validates the port
        valid_port = True
    except ValueError:
        valid_port = False
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not valid_port
        or any(c.isspace() for c in url)
    ):
        raise ValueError("a remote server's command must be an http(s):// URL with a host")
    transport = canonical_transport(args[0] if args else None)
    if transport is None:
        raise ValueError(
            f"remote transport must be one of {list(remote_runner.TRANSPORTS)} "
            f"(got {args[0]!r})"
        )
    return url, [transport]


def normalize_oauth(
    runner: str,
    oauth: bool,
    scopes: Optional[str],
    client_id: Optional[str],
    client_secret: Optional[str],
) -> tuple[bool, str, Optional[str], Optional[str]]:
    """Canonicalize a server's upstream-OAuth config.

    OAuth only applies to the ``remote`` runner (there's no upstream URL to authenticate
    against otherwise), so it is forced off for every other runner and the fields are
    cleared — keeping ``config_hash`` stable and preventing a stray secret from riding
    along on a local server. Blank strings collapse to ``""`` / ``None`` so the stored
    shape is deterministic. A client secret without a client id is meaningless (there's
    no static client to authenticate) — reject it rather than silently drop it.
    """
    if runner != "remote" or not oauth:
        return False, "", None, None
    scopes = (scopes or "").strip()
    client_id = (client_id or "").strip() or None
    # A client secret is an OPAQUE credential — never .strip() it. A provider-issued
    # secret can legitimately begin or end with whitespace, and trimming would store a
    # different value, so the token exchange would authenticate with the wrong secret and
    # be rejected. Only an empty string counts as "absent".
    client_secret = client_secret or None
    if client_secret and not client_id:
        raise ValueError("an OAuth client secret requires a client id")
    return True, scopes, client_id, client_secret


# --- docker (OCI image) normalization ------------------------------------------
# The canonical stored shape for a docker server is minimal (SSOT): command = image
# reference, args = the CONTAINER's own arguments, env = the env map. A pasted
# mcpServers entry, though, gives a full `docker run …` invocation; normalize_docker is
# the single place that parses that into the canonical shape so the row (and its
# config_hash) is deterministic and the docker runner can synthesize its own hardened argv.
# Only real docker launchers: the docker runner always execs `docker` (DOCKER_BIN), so we
# must NOT silently reclassify a `podman …` config as this runner — it would run against a
# different daemon (or fail). A podman config instead falls through to the `command` runner,
# which launches it verbatim.
_DOCKER_LAUNCHERS = {"docker", "docker.exe"}

# Runners that execute ``command`` verbatim as a local process (passthrough, no hardening,
# full env). If any of these is pointed straight at the docker CLI it IS the docker runner and
# must be routed through it — otherwise it would launch containers ungated/unhardened with the
# control plane's full environment. (``remote`` is excluded: its command is a URL.)
_LOCAL_EXEC_RUNNERS = {"npx", "uvx", "command"}


def _is_docker_launcher(command: str) -> bool:
    """True only when ``command`` invokes the docker CLI itself.

    A launcher is a bare ``docker``/``docker.exe`` or a filesystem path to it
    (``/usr/local/bin/docker``, ``./docker``, ``~/bin/docker``,
    ``C:\\Program Files\\Docker\\docker.exe``). An OCI image reference whose final path
    segment is literally ``docker`` (the official ``docker`` image, ``docker.io/library/docker``,
    ``ghcr.io/acme/docker``) ALSO has basename ``docker`` but is NOT a launcher — reclassifying it
    or parsing it as a full ``docker run`` invocation would drop the real image and misread the
    container's own args. A registry ref is distinguished from a path because it is neither
    absolute, relative, home-anchored, nor a Windows drive path."""
    c = command.strip()
    if _launcher_basename(c) not in _DOCKER_LAUNCHERS:
        return False
    norm = c.replace("\\", "/")
    if "/" not in norm:
        return True  # a bare launcher name (no path separator) is the CLI
    # A path to the CLI: absolute/relative/home-anchored (POSIX), or a Windows drive path.
    return norm[0] in "/.~" or (len(c) >= 2 and c[1] == ":")


def _is_docker_command(command: str) -> bool:
    """Like ``_is_docker_launcher`` but for a SHELL command-word position, where any path whose
    basename is ``docker`` is unambiguously an executable — including a bare relative path like
    ``bin/docker`` that the image-field check treats as an OCI reference."""
    if _is_docker_launcher(command):
        return True
    return "/" in command.strip().replace("\\", "/") and _launcher_basename(command) in _DOCKER_LAUNCHERS


def _split_top_commas(text: str) -> list[str]:
    """Split ``text`` on commas that are NOT inside a nested ``{…}`` (for brace-list expansion)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _brace_expand(word: str, _depth: int = 0) -> tuple[list[str], bool]:
    """Best-effort Bash brace-list expansion of a single word, bounded. Returns
    ``(expansions, truncated)`` — ``truncated`` is True when the depth or result cap was hit, so a
    later alternative may be missing and the caller must fail closed.

    Handles ``{a,b,c}`` comma lists (with nesting) — the form that hides a launcher, e.g.
    ``{docker,}`` expands to ``docker`` (and ``''``). Sequence braces (``{1..3}``) and ``${VAR}``
    (no top-level comma) are left alone. Always includes the original word."""
    if "{" not in word:
        return [word], False
    if _depth > 8:  # depth cap hit while braces remain — gave up before fully expanding
        return [word], True
    start = word.find("{")
    depth, end = 0, -1
    for j in range(start, len(word)):
        if word[j] == "{":
            depth += 1
        elif word[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end == -1:
        return [word], False
    parts = _split_top_commas(word[start + 1:end])
    if len(parts) < 2:  # no top-level comma (e.g. ${VAR}, {1..3}) — nothing to expand
        return [word], False
    prefix, suffix = word[:start], word[end + 1:]
    results = [word]
    truncated = False
    for part in parts:
        sub, sub_trunc = _brace_expand(prefix + part + suffix, _depth + 1)
        truncated = truncated or sub_trunc
        for expanded in sub:
            results.append(expanded)
            if len(results) >= 64:
                return results, True  # result cap hit — a later alternative may be missing
    return results, truncated


# Thin wrappers that stand in FRONT of the real command without being the command themselves:
# they exec their remaining argv (``sudo docker …`` is a docker launch; ``python --backend docker``
# is not). Several also accept leading ``NAME=VALUE`` assignments. Peeling these keeps detection
# focused on the actual command word. ``eval`` is handled separately (its args ARE shell input).
_SHELL_CMD_PREFIXES = {"exec", "sudo", "doas", "env", "nohup", "setsid", "command", "builtin",
                       "nice", "ionice", "taskset", "unshare", "prlimit", "timeout", "xargs",
                       "stdbuf", "flock", "chroot", "runuser", "su", "watch"}

# Builtins that BOTH persist a ``NAME=VALUE`` assignment to the shell and are a command word
# (``export D=docker`` / ``declare D=docker``). ``_segment_assignments`` mines their operands so a
# later ``$D`` command word resolves.
_ASSIGNMENT_BUILTINS = {"export", "declare", "typeset", "readonly", "local"}

# Wrappers whose run form accepts leading ``NAME=VALUE`` assignments before the command.
_WRAPPERS_ACCEPTING_ASSIGNMENTS = {"env", "sudo", "doas"}

# Wrappers whose first bare operand is NOT the command but a positional value the wrapper consumes
# (``timeout DURATION COMMAND …`` / ``flock FILE COMMAND …`` / ``chroot NEWROOT COMMAND …``).
_WRAPPERS_WITH_LEADING_OPERAND = {"timeout", "flock", "chroot"}

# Generous bound on wrapper-nesting depth. Real configs nest a couple; hitting this many is
# pathological (an ``env env … env docker`` stack) and is resolved conservatively (see below).
_MAX_WRAPPER_PEELS = 64

# Shells whose ``-c STRING`` argument is itself a command line we must look inside. ``rbash`` is
# Bash in restricted mode and still honors ``-c``.
_SHELL_LAUNCHERS = {"sh", "bash", "rbash", "dash", "ash", "zsh", "ksh"}

# Builtins that execute a FILE argument as shell script in the current shell. Pointed at a stdin
# path (below) they run whatever a heredoc/here-string feeds them.
_SOURCE_BUILTINS = {"source", "."}

# Paths that resolve to standard input — a heredoc redirected here becomes the sourced/executed
# script (``source /dev/stdin <<EOF``).
_STDIN_PATHS = {"/dev/stdin", "-", "/proc/self/fd/0", "/dev/fd/0"}

# Shell reserved words / keywords that can PRECEDE the real command in a simple-command position
# (``if docker …``, ``! docker …``). Skipping them keeps the scan on the command the shell actually
# executes. ``time`` and ``coproc`` are handled explicitly (they take options / an optional name);
# ``for``/``select``/``case`` are in _SHELL_DECL_KEYWORDS (their next word is a name/subject).
_SHELL_RESERVED_WORDS = {
    "if", "then", "elif", "else", "fi", "while", "until", "do", "done", "for", "case", "esac",
    "select", "function", "!", "{", "}", "[[", "]]", "in",
}

# Keywords immediately followed by a NAME (loop variable) or SUBJECT (case value) — not a command.
_SHELL_DECL_KEYWORDS = {"for", "select", "case"}

# Per-wrapper VALUE-taking options, listed as long forms and as bare short letters (for clustered
# short options like ``-Eu``). Arity is wrapper-specific: ``nice -n 10`` / ``exec -a name`` take a
# value but ``sudo -n`` (``--non-interactive``) does NOT — a shared table would swallow the real
# ``docker`` after ``sudo -n``. env's ``-S``/``--split-string`` is value-bearing too but handled
# specially (its value is itself a command line), so it is NOT listed here.
_WRAPPER_VALUE_LONG = {
    "sudo": {"--user", "--group", "--prompt", "--close-from", "--host", "--role", "--type",
             "--other-user", "--chroot", "--command-timeout", "--chdir"},
    "doas": {"--user"},
    "env": {"--unset", "--chdir"},
    "nice": {"--adjustment"},
    "ionice": {"--class", "--classdata", "--pid"},
    "taskset": {"--cpu-list"},
    # unshare's namespace flags (``-m``/``-p``/…) take only an OPTIONAL ``=file`` arg (never a
    # space-separated one), so they don't swallow the command; only these take a required value.
    "unshare": {"--map-user", "--map-group", "--map-users", "--map-groups", "--setgroups",
                "--propagation", "--setuid", "--setgid", "--root", "--wd"},
    # prlimit's ``--<resource>=<limit>`` options are inline ``=`` forms (no swallowed operand); only
    # ``--pid``/``--output`` take a separate value. ``--pid`` targets an existing process (no child).
    "prlimit": {"--pid", "--output"},
    "exec": set(),
    "timeout": {"--signal", "--kill-after"},
    "xargs": {"--replace", "--max-args", "--max-procs", "--max-chars", "--delimiter",
              "--arg-file", "--max-lines", "--eof", "--process-slot-var"},
    "stdbuf": {"--input", "--output", "--error"},
    "flock": {"--timeout", "--wait", "--conflict-exit-code"},
    "chroot": {"--userspec", "--groups"},
    "runuser": {"--user", "--group", "--supp-group", "--shell", "--session-command", "--login"},
    "su": {"--group", "--supp-group", "--shell", "--session-command"},
    "watch": {"--interval"},
}
_WRAPPER_VALUE_SHORT = {
    "sudo": set("ugpChrtURTD"),
    "doas": set("uC"),
    "env": set("uC"),
    "nice": set("n"),
    "ionice": set("cnp"),  # -c class, -n classdata, -p pid
    "taskset": set("cp"),  # -c cpu-list, -p pid
    "unshare": set("SGRw"),  # -S setuid, -G setgid, -R root, -w wd (namespace flags are boolean)
    "prlimit": set("po"),    # -p pid, -o output
    "exec": set("a"),
    "timeout": set("sk"),
    # -I replstr, -n num, -P procs, -s size, -d delim, -a file, -E eof, -L/-l num
    "xargs": set("InPsdaELl"),
    "stdbuf": set("ioe"),  # -i/-o/-e BUFMODE
    "flock": set("wE"),    # -w timeout, -E exit-code
    "runuser": set("ugGs"),  # -u user, -g group, -G supp-group, -s shell (-c handled specially)
    "su": set("gGs"),        # su takes the user as a positional, not -u
    "watch": set("n"),       # -n interval
}

# Wrappers whose ``-c``/``--command`` option runs its value through a shell (inspect it as script).
_WRAPPERS_WITH_SHELL_C = {"runuser", "su"}

# Characters that end a simple command and (re)open a command position when UNQUOTED: control
# operators (``;`` ``&`` ``|`` newline) and subshell parens. Command substitution (``$(…)`` and
# backticks) is extracted separately so it is caught even inside double quotes.
_SHELL_OPERATOR_CHARS = ";&|\n()"


def _looks_like_assignment(word: str) -> bool:
    """True when ``word`` is a shell ``NAME=VALUE`` assignment (a valid identifier before ``=``).

    Distinguishes ``FOO=bar`` (a leading assignment the shell applies, then runs the next word)
    from an option like ``--backend=docker`` (not an assignment — the command is elsewhere)."""
    eq = word.find("=")
    if eq <= 0:
        return False
    name = word[:eq]
    return (name[0].isalpha() or name[0] == "_") and all(c.isalnum() or c == "_" for c in name)


def _is_shell_name(word: str) -> bool:
    """True when ``word`` is a valid POSIX identifier (a shell function/variable name)."""
    return bool(word) and (word[0].isalpha() or word[0] == "_") and all(
        c.isalnum() or c == "_" for c in word)


# GNU ``env -S`` escape sequences that differ from ordinary shell/``shlex`` handling: ``\_`` is a
# SEPARATOR (a space that env splits on), the whitespace escapes act as separators too, and ``\c``
# ends processing. Normalizing them before splitting means ``env -S 'docker\_run'`` is seen as the
# two words env would exec, not the single token ``docker_run``.
_ENV_S_ESCAPES = {"_": " ", "t": " ", "n": " ", "r": " ", "f": " ", "v": " ",
                  "\\": "\\", "#": "#", "$": "$"}


def _normalize_env_split(value: str) -> str:
    """Apply GNU ``env -S`` escape handling (``\\_`` → separator, ``\\c`` → stop, …) so the value can
    be tokenized the way env itself would split it."""
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt == "c":  # \c ends processing — drop the rest
                break
            out.append(_ENV_S_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_string_command(value: str, rest: list[str]) -> tuple[str, list[str]]:
    """Parse env ``-S``'s split-string value (a whole command line env splits and execs) into
    (command, args), appending any tokens that followed the ``-S`` option.

    The split words are env's OWN remaining argv, so they re-enter env's grammar: a leading ``--``
    or ``NAME=VALUE`` assignment must be honored (``env -S '-- docker …'`` /
    ``env -S 'FOO=bar docker …'`` both exec docker). Returning ``("env", words)`` lets the wrapper
    peel loop reprocess them rather than taking ``words[0]`` as the command verbatim."""
    normalized = _normalize_env_split(value)
    try:
        words = shlex.split(normalized)
    except ValueError:
        words = normalized.split()
    return "env", list(words) + list(rest)


def _heredoc_delimiters(line: str) -> list[tuple[str, bool, bool]]:
    """Find here-document markers (``<<WORD`` / ``<<-WORD`` / ``<<"WORD"``) on a line, honoring
    quotes and skipping here-strings (``<<<``). Returns (delimiter, strip_leading_tabs, quoted) per
    marker, in order — bash allows several on one line. ``quoted`` is True when the delimiter word
    was quoted (``<<'EOF'``), meaning the body is literal (no expansions)."""
    delims: list[tuple[str, bool, bool]] = []
    i, n = 0, len(line)
    quote: Optional[str] = None
    while i < n:
        ch = line[i]
        if quote is not None:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break  # a comment starts here — any `<<` after it is not a real heredoc marker
        if ch == "<" and line[i:i + 2] == "<<" and line[i:i + 3] != "<<<":
            j = i + 2
            strip_tabs = False
            if j < n and line[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            word: list[str] = []
            was_quoted = False
            while j < n and line[j] not in " \t\n;&|<>()":
                c = line[j]
                if c in ("'", '"'):
                    was_quoted = True
                    j += 1
                    while j < n and line[j] != c:
                        word.append(line[j])
                        j += 1
                    j += 1
                    continue
                if c == "\\" and j + 1 < n:
                    was_quoted = True
                    word.append(line[j + 1])
                    j += 2
                    continue
                word.append(c)
                j += 1
            if word:
                delims.append(("".join(word), strip_tabs, was_quoted))
            i = j
            continue
        i += 1
    return delims


def _line_feeds_shell(line: str) -> bool:
    """True when the command on a here-document marker line will EXECUTE the body as shell script
    rather than read it as ordinary stdin data. Two shapes qualify: a shell launcher reading the
    body (``bash <<EOF``), and ``source``/``.`` pointed at a stdin path (``source /dev/stdin <<EOF``
    executes the heredoc in the current shell)."""
    head = line.split("<<", 1)[0]
    try:
        words = shlex.split(head)
    except ValueError:
        words = head.split()
    idx = 0
    while idx < len(words) and _looks_like_assignment(words[idx]):  # leading FOO=bar assignments
        idx += 1
    if idx >= len(words):
        return False
    cmd, rest = _strip_wrappers(words[idx], words[idx + 1:])
    base = _launcher_basename(cmd)
    if base in _SHELL_LAUNCHERS:
        return True
    # ``source FILE`` / ``. FILE`` run FILE as script in the current shell; when FILE names stdin
    # and a heredoc feeds it, the body is the sourced script — inspect it, don't drop it as data.
    return base in _SOURCE_BUILTINS and any(str(a) in _STDIN_PATHS for a in (rest or []))


def _strip_heredocs(command_string: str) -> str:
    """Drop here-document BODIES (input data, not commands) so their lines aren't parsed as shell
    commands. The marker line (``cat <<EOF``) is kept; the body up to and including the delimiter
    line is removed. Two exceptions keep executable content: when the marker line feeds a shell
    (``bash <<EOF`` — the body IS a script) the whole body is kept for inspection; for an UNQUOTED
    delimiter the shell expands command substitutions in the body, so those are preserved."""
    if "<<" not in command_string:
        return command_string
    lines = command_string.split("\n")
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        out.append(line)
        idx += 1
        feeds_shell = _line_feeds_shell(line)
        for delim, strip_tabs, quoted in _heredoc_delimiters(line):
            body_lines: list[str] = []
            while idx < len(lines):
                body = lines[idx]
                idx += 1
                candidate = body.lstrip("\t") if strip_tabs else body
                if candidate == delim:
                    break
                body_lines.append(body)
            if feeds_shell:  # the body is a script executed by the shell — inspect it verbatim
                out.extend(body_lines)
            elif not quoted:  # unquoted delimiter: keep the body's command substitutions
                preserved = _preserve_substitutions("\n".join(body_lines))
                if preserved:
                    out.append(preserved)
    return "\n".join(out)


_ANSI_C_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
                  "\\": "\\", "'": "'", '"': '"', "?": "?", "e": "\x1b", "E": "\x1b"}
_HEXDIGITS = set("0123456789abcdefABCDEF")


def _find_unquoted(s: str, start: int, needle: str) -> int:
    """Index of the first ``needle`` in ``s`` at/after ``start`` that is NOT inside single/double
    quotes, or -1. Used to locate a ``[[ … ]]`` terminator without stopping on a quoted ``]]``."""
    i, n = start, len(s)
    quote: Optional[str] = None
    while i < n:
        ch = s[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if s.startswith(needle, i):
            return i
        i += 1
    return -1


def _decode_ansi_c(body: str) -> str:
    """Decode bash ANSI-C ``$'…'`` escapes (``\\xHH``, ``\\NNN`` octal, ``\\uHHHH``, ``\\n`` …) to the
    literal characters bash would produce, so ``doc$'\\x6b\\x65\\x72'`` resolves to ``docker``."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[nxt])
            i += 2
        elif nxt == "x":
            j, hexs = i + 2, ""
            while j < n and len(hexs) < 2 and body[j] in _HEXDIGITS:
                hexs += body[j]
                j += 1
            if hexs:
                out.append(chr(int(hexs, 16)))
                i = j
            else:
                out.append(nxt)
                i += 2
        elif nxt in "01234567":
            j, octs = i + 1, ""
            while j < n and len(octs) < 3 and body[j] in "01234567":
                octs += body[j]
                j += 1
            out.append(chr(int(octs, 8) & 0xFF))
            i = j
        elif nxt in ("u", "U"):
            width = 4 if nxt == "u" else 8
            j, hexs = i + 2, ""
            while j < n and len(hexs) < width and body[j] in _HEXDIGITS:
                hexs += body[j]
                j += 1
            if hexs:
                try:
                    out.append(chr(int(hexs, 16)))
                except (ValueError, OverflowError):
                    pass
                i = j
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def _param_expansion_default(inner: str) -> str:
    """For a ``${…}`` body, return the word a default/alternate operator would substitute
    (``VAR:-docker`` → ``docker``), or ``""`` when there is no such operator (plain ``${VAR}``) or it
    is an error form (``:?``). A script that unsets the var guarantees the default, so this word is a
    candidate command position."""
    for op in (":-", ":=", ":+", "-", "=", "+"):
        idx = inner.find(op)
        if idx > 0:  # operator must come after a (non-empty) parameter name
            return inner[idx + len(op):]
    return ""


def _preprocess_shell_string(command_string: str) -> str:
    """Apply shell pre-tokenization rewrites the parser can't see through otherwise, quote-aware:

    - line continuations (``\\`` immediately before a newline) are removed, as POSIX shells do
      before tokenizing, so ``docker\\<newline> run`` reads as ``docker run``;
    - the ``$`` before ``$'…'`` (bash ANSI-C) / ``$"…"`` (locale) quoting is dropped so the quoted
      fragment concatenates onto its neighbours (``doc$'ker'`` → ``docker``);
    - ``#`` comments (at a word boundary) are dropped to end-of-line, so a docker example in a
      comment isn't mistaken for a command;
    - ``$((…))`` arithmetic expansion is dropped — its parens are not a subshell and its contents
      are not commands (``echo $((docker + 1))`` references a variable, it doesn't run docker).

    All applied only outside single quotes, where these are literal."""
    out: list[str] = []
    i, n = 0, len(command_string)
    quote: Optional[str] = None
    at_boundary = True  # start of string / just after an unquoted separator = a command position
    while i < n:
        ch = command_string[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n and command_string[i + 1] == "\n":
            i += 2  # line continuation — removed
            continue
        if ch == "\\" and i + 1 < n:  # keep any other escape intact (both chars)
            out.append(ch)
            out.append(command_string[i + 1])
            i += 2
            at_boundary = False
            continue
        if quote == '"':
            out.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        # --- unquoted ---
        if ch == "#" and at_boundary:  # comment — drop to end of line
            while i < n and command_string[i] != "\n":
                i += 1
            continue
        if (at_boundary and command_string[i:i + 2] == "[["
                and (i + 2 >= n or command_string[i + 2] in " \t")):
            # a ``[[ … ]]`` conditional: its words are operands, not commands — drop the span, but
            # KEEP any command substitutions (they execute before the test, so `[[ $(docker …) ]]`
            # must still be inspected). Scan for the real UNQUOTED ``]]`` (a quoted ``]]`` is an
            # operand, not the terminator).
            close = _find_unquoted(command_string, i + 2, "]]")
            if close != -1:
                out.append(" " + _preserve_substitutions(command_string[i + 2:close]) + " ")
                at_boundary = True
                i = close + 2
                continue
        if ch == "$" and i + 2 < n and command_string[i + 1] == "(" and command_string[i + 2] == "(":
            body, end = _read_delimited(command_string, i + 2, ")")  # $((…)) arithmetic
            if "$(" in body or "`" in body:
                # the shell still runs command substitutions inside arithmetic — keep the span so
                # ``echo $(( $(docker run) + 1))`` is inspected, not erased.
                out.append(command_string[i:end])
                at_boundary = False
            else:
                out.append(" ")  # pure arithmetic (just operands/operators) — safe to drop
                at_boundary = True
            i = end
            continue
        if command_string.startswith("$IFS", i) and (
                i + 4 >= n or not (command_string[i + 4].isalnum() or command_string[i + 4] == "_")):
            # bare ``$IFS`` field-splits: ``docker$IFS run`` executes ``docker run`` — emit a space.
            out.append(" ")
            at_boundary = True
            i += 4
            continue
        if command_string.startswith("${", i):
            # ``${…}`` parameter expansion: emit a default/alternate word (``${VAR:-docker}`` →
            # ``docker``) so the guaranteed-default command position is seen; ``${IFS}`` / an unknown
            # ``${VAR}`` become a separator.
            depth, j = 1, i + 2
            while j < n and depth:
                if command_string[j] == "{":
                    depth += 1
                elif command_string[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = command_string[i + 2:j]
            default = _param_expansion_default(inner)
            if default:
                out.append(default)
                at_boundary = False
            elif _is_shell_name(inner) and inner != "IFS":
                # a plain ``${NAME}`` — keep it intact so the later assignment/env pass
                # (``_expand_known_vars``) can resolve ``D=docker; ${D} run``. ``${IFS}`` stays a
                # separator (field split), and complex forms (``${#x}``, ``${1}``) fall through.
                out.append("${" + inner + "}")
                at_boundary = False
            else:
                out.append(" ")
                at_boundary = True
            i = j + 1
            continue
        if ch == "$" and i + 1 < n and command_string[i + 1] == "'":
            # bash ANSI-C quoting: read to the closing ', decode escapes, and re-emit shell-safe
            # single-quoted so the (decoded) fragment stays one word (``doc$'\x6b…'`` → docker).
            j, buf = i + 2, []
            while j < n and command_string[j] != "'":
                if command_string[j] == "\\" and j + 1 < n:
                    buf.append(command_string[j])
                    buf.append(command_string[j + 1])
                    j += 2
                    continue
                buf.append(command_string[j])
                j += 1
            decoded = _decode_ansi_c("".join(buf))
            out.append("'" + decoded.replace("'", "'\\''") + "'")
            at_boundary = False
            i = j + 1  # past the closing '
            continue
        if ch == "$" and i + 1 < n and command_string[i + 1] == '"':
            i += 1  # locale $"…": drop the $, let the quote open on the next iteration
            continue
        if command_string[i:i + 3] == "<<<":
            # here-string ``cmd <<< WORD``: keep ``<<<`` as one token (space-isolated) so the
            # segment scanner can pair it with its payload.
            out.append(" <<< ")
            at_boundary = True
            i += 3
            continue
        if ch in "<>":
            # a redirection can be glued to the command word (``docker</dev/null``); split it off so
            # the command word is seen. Don't split when preceded by a digit (an fd number like
            # ``2>``) so the redirection token stays intact for the segment scanner.
            if out and out[-1] not in " \t" and not out[-1].isdigit():
                out.append(" ")
            out.append(ch)
            at_boundary = True
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            at_boundary = False
            i += 1
            continue
        out.append(ch)
        at_boundary = ch in " \t\n;&|()"
        i += 1
    return "".join(out)


def _read_delimited(command_string: str, start: int, closer: str) -> tuple[str, int]:
    """Read a substitution body from ``start`` until the matching UNQUOTED ``closer`` (``)`` for
    ``$(…)`` — balancing nested parens — or the next backtick), honoring single/double quotes so a
    quoted delimiter inside the body (``$(printf ')' ; docker …)``) does not end it early. Returns
    (inner_text, index_past_closer)."""
    depth = 1
    j, n = start, len(command_string)
    quote: Optional[str] = None
    while j < n:
        ch = command_string[j]
        if quote is not None:
            if ch == "\\" and quote == '"' and j + 1 < n:
                j += 2
                continue
            if ch == quote:
                quote = None
            j += 1
            continue
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            j += 1
            continue
        if closer == ")":
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return command_string[start:j], j + 1
        elif ch == closer:  # backtick
            return command_string[start:j], j + 1
        j += 1
    return command_string[start:j], j


def _command_substitutions(command_string: str) -> list[str]:
    """Return the inner text of every ``$(…)`` and backtick command substitution the shell would
    execute — i.e. NOT inside single quotes (double quotes still run substitutions). Quotes inside a
    substitution body are honored while balancing. Lets the guard see ``echo "$(docker run …)"``
    where the substitution runs docker before the visible command does."""
    subs: list[str] = []
    i, n = 0, len(command_string)
    quote: Optional[str] = None
    while i < n:
        ch = command_string[i]
        if quote == "'":  # single quotes suppress substitution entirely
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            # inside double quotes: fall through — $(…)/backticks still execute
        else:
            if ch == "\\" and i + 1 < n:  # escaped: never opens a substitution
                i += 2
                continue
            if ch == "'":
                quote = "'"
                i += 1
                continue
            if ch == '"':
                quote = '"'
                i += 1
                continue
        if ch == "$" and i + 1 < n and command_string[i + 1] == "(":
            body, i = _read_delimited(command_string, i + 2, ")")
            subs.append(body)
            continue
        if ch == "`":
            body, i = _read_delimited(command_string, i + 1, "`")
            subs.append(body)
            continue
        i += 1
    return subs


def _process_subst_invokes_docker(command_string: str,
                                  env: Optional[dict[str, str]] = None) -> bool:
    """True when a process substitution ``<(…)``/``>(…)`` reaches docker: its inner command runs
    (``<(docker run)``), or — when it feeds a shell as a script file (``bash <(printf 'docker …')``)
    — the inner's static output is the script that shell executes."""
    i, n = 0, len(command_string)
    quote: Optional[str] = None
    while i < n:
        ch = command_string[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in ("<", ">") and i + 1 < n and command_string[i + 1] == "(":
            inner, end = _read_delimited(command_string, i + 2, ")")
            if _shell_command_invokes_docker(inner, env):  # the substituted command itself runs
                return True
            literal = _static_command_output(inner)  # its static output, if fed to a shell, is script
            if literal is not None and _preceding_command_is_shell(command_string, i):
                if _shell_command_invokes_docker(literal, env):
                    return True
            i = end
            continue
        i += 1
    return False


def _preceding_command_is_shell(command_string: str, pos: int) -> bool:
    """True when the simple command containing ``pos`` executes a script FILE as shell input — a
    shell launcher (``bash <(…)``) or ``source``/``.`` (``source <(…)``). Used to tell whether a
    process substitution at ``pos`` is a script run by that command."""
    segment = _split_shell_commands(command_string[:pos])[-1]
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    idx = 0
    while idx < len(words) and (_looks_like_assignment(words[idx])
                                or _redirection_span(words[idx])):
        idx += 1
    if idx >= len(words):
        return False
    cmd, _ = _strip_wrappers(words[idx], words[idx + 1:])
    base = _launcher_basename(cmd)
    return base in _SHELL_LAUNCHERS or base in _SOURCE_BUILTINS


def _preserve_substitutions(text: str) -> str:
    """Return just the command substitutions of ``text`` (re-wrapped as ``$(…)``), dropping the
    surrounding operands/data. Used to keep the executable substitutions of a construct we otherwise
    discard — a ``[[ … ]]`` condition or an unquoted here-document body — so ``[[ $(docker …) == x ]]``
    is still inspected while its plain operands are not."""
    return " ".join("$(" + inner + ")" for inner in _command_substitutions(text))


def _split_shell_commands(command_string: str) -> list[str]:
    """Split a shell command line into simple-command segments at unquoted operators.

    A hand-rolled scan (not ``shlex``) so glued operators split correctly: ``true&&docker`` and
    ``x;docker`` must yield a ``docker`` segment, where ``shlex.split`` alone leaves them as one
    word. Single/double quotes and backslash escapes are honored so an operator inside a quoted
    argument is NOT a boundary. Empty segments are harmless (they resolve to no command)."""
    segments: list[str] = []
    buf: list[str] = []
    quote: Optional[str] = None
    i, n = 0, len(command_string)
    while i < n:
        ch = command_string[i]
        if quote is not None:
            # Inside double quotes a backslash escapes the next char (so an escaped " doesn't close
            # the quote); single quotes are fully literal.
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(ch)
                buf.append(command_string[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:  # an escaped char is literal, never an operator
            buf.append(ch)
            buf.append(command_string[i + 1])
            i += 2
            continue
        if ch == "(" and _is_shell_name("".join(buf).strip()):
            # POSIX function definition ``NAME ()`` — keep the ``()`` attached to the name so the
            # segment stays a recognizable declaration instead of splitting into a bare ``NAME``
            # command at the paren (which would misread ``docker() { … }`` as a docker launch).
            j = i + 1
            while j < n and command_string[j] == " ":
                j += 1
            if j < n and command_string[j] == ")":
                buf.append("()")
                i = j + 1
                continue
        if ch in _SHELL_OPERATOR_CHARS:
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments


def _strip_wrappers(command: str, args: Optional[list[str]],
                    collected_assignments: Optional[dict[str, str]] = None
                    ) -> tuple[str, Optional[list[str]]]:
    """Peel thin wrappers (``env``/``sudo``/``doas``/``nice``/``exec``/…) off the front so the real
    command underneath is what gets inspected. Honors leading ``NAME=VALUE`` assignments (env/sudo/
    doas), per-wrapper value options — including clustered short options (``sudo -Eu root``) and env
    ``-S``/``-vS`` split strings — and ``--`` end-of-options, so ``sudo -u root docker`` /
    ``nice -n 10 docker`` / ``exec -a x docker`` aren't misread. Loops so nested wrappers
    (``sudo env docker``) fully peel; if the (generous) nesting bound is hit while still on a
    wrapper — pathological ``env env … env docker`` stacking — it resolves conservatively to a
    docker launcher rather than giving up and reporting the outer wrapper as safe.

    When ``collected_assignments`` is supplied, ``NAME=VALUE`` prefixes the wrapper applies to the
    child (``env D=docker sh -c '"$D" …'``) are recorded into it, so the child shell's expansion of
    ``$D`` can be resolved by the caller."""
    for _ in range(_MAX_WRAPPER_PEELS):
        base = _launcher_basename(command)
        if base not in _SHELL_CMD_PREFIXES:
            return command, args
        tokens = list(args or [])
        peeled: Optional[tuple[str, Optional[list[str]]]] = None
        # ``watch`` runs its operand string through ``sh -c`` by default, but ``-x``/``--exec``
        # passes the argv straight to ``exec`` instead. Track which form applies to this wrapper.
        watch_exec = False
        # ``sudo -s``/``-i`` (``--shell``/``--login``) pass any trailing command to the target
        # user's shell via ``-c`` (joining the operands), so it is a shell command line, not argv.
        sudo_shell = False
        # ``taskset`` runs ``taskset MASK COMMAND`` (a leading affinity-mask operand) unless
        # ``-c``/``--cpu-list`` supplies the affinity instead (then the first operand IS the
        # command); ``-p``/``--pid`` operates on an existing process and launches nothing.
        taskset_cpulist = False
        taskset_pid = False
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if not isinstance(tok, str):
                return command, args
            if base in _WRAPPERS_WITH_SHELL_C and (
                tok in ("-c", "--command") or tok.startswith("--command=")):
                # ``runuser/su -c COMMAND`` runs COMMAND through the target user's shell.
                value = tok.split("=", 1)[1] if "=" in tok else (
                    tokens[i + 1] if i + 1 < len(tokens) else "")
                peeled = ("sh", ["-c", value])
                break
            if tok == "--":  # end of the wrapper's options — the next token is the command
                if base in _WRAPPERS_WITH_LEADING_OPERAND:
                    # ``timeout -- DURATION COMMAND``: -- ends options, but the required positional
                    # (duration) still precedes the command.
                    peeled = (tokens[i + 2], list(tokens[i + 3:])) if i + 2 < len(tokens) else None
                elif i + 1 < len(tokens):
                    peeled = (tokens[i + 1], list(tokens[i + 2:]))
                break
            if tok.startswith("--"):  # long option
                name = tok.split("=", 1)[0]
                if base == "env" and name == "--split-string":
                    if "=" in tok:
                        peeled = _split_string_command(tok.split("=", 1)[1], tokens[i + 1:])
                    else:
                        value = tokens[i + 1] if i + 1 < len(tokens) else ""
                        peeled = _split_string_command(value, tokens[i + 2:])
                    break
                if base == "watch" and name == "--exec":
                    watch_exec = True
                if base == "sudo" and name in ("--shell", "--login"):
                    sudo_shell = True
                if base == "taskset" and name == "--cpu-list":
                    taskset_cpulist = True
                if base == "taskset" and name == "--pid":
                    taskset_pid = True
                if "=" not in tok and name in _WRAPPER_VALUE_LONG.get(base, frozenset()):
                    i += 2  # separate value
                else:
                    i += 1  # boolean, or inline --opt=value
                continue
            if tok.startswith("-") and tok != "-":  # short-option cluster, e.g. -Eu / -vS
                letters = tok[1:]
                # ``command -v``/``-V`` only PRINT information about a name — they do not execute it,
                # so ``command -v docker`` is a lookup, not a launch. Resolve to a non-launcher.
                if base == "command" and ("v" in letters or "V" in letters):
                    return "", None
                if base == "watch" and "x" in letters:
                    watch_exec = True
                if base == "sudo" and ("s" in letters or "i" in letters):
                    sudo_shell = True
                if base == "taskset" and "c" in letters:
                    taskset_cpulist = True
                if base == "taskset" and "p" in letters:
                    taskset_pid = True
                short_vals = _WRAPPER_VALUE_SHORT.get(base, frozenset())
                handled = False
                for k, c in enumerate(letters):
                    if base == "env" and c == "S":  # split-string: value is rest-of-cluster or next
                        inline = letters[k + 1:]
                        if inline:
                            peeled = _split_string_command(inline, tokens[i + 1:])
                        else:
                            value = tokens[i + 1] if i + 1 < len(tokens) else ""
                            peeled = _split_string_command(value, tokens[i + 2:])
                        handled = True
                        break
                    if c in short_vals:
                        i += 1 if letters[k + 1:] else 2  # value inline in cluster, else next token
                        handled = True
                        break
                if peeled is not None:
                    break
                if not handled:
                    i += 1  # boolean-only cluster
                continue
            if base in _WRAPPERS_ACCEPTING_ASSIGNMENTS and _looks_like_assignment(tok):
                if collected_assignments is not None:  # env/sudo apply this to the child's env
                    name, value = tok.split("=", 1)
                    collected_assignments[name] = value
                i += 1
                continue
            if sudo_shell:  # ``sudo -s COMMAND …`` — the operands are a shell command line
                operands = tokens[i:]
                peeled = ("sh", ["-c", " ".join(str(t) for t in operands)])
                break
            if base == "taskset":
                if taskset_pid:  # ``taskset -p … PID`` — no command is launched
                    break  # peeled stays None → resolve to the (non-launcher) taskset itself
                operands = tokens[i:]
                # Without ``-c``/``--cpu-list`` the first operand is the affinity MASK and the
                # command follows it; with it, the first operand IS the command.
                start = 0 if taskset_cpulist else 1
                peeled = (operands[start], list(operands[start + 1:])) if len(
                    operands) > start else None
                break
            if base in _WRAPPERS_WITH_LEADING_OPERAND:
                # ``timeout DURATION COMMAND …`` / ``flock FILE COMMAND …``: this bare token is the
                # positional the wrapper consumes, so the command is the NEXT token.
                rest = tokens[i + 1:]
                if base == "flock" and rest and rest[0] in ("-c", "--command"):
                    # ``flock FILE -c COMMAND`` runs COMMAND through the shell — inspect it as such.
                    peeled = ("sh", ["-c"] + list(rest[1:2]))
                else:
                    peeled = (rest[0], list(rest[1:])) if rest else None
                break
            if base == "watch":
                # ``watch COMMAND …`` joins its operands and runs them through ``sh -c`` — so the
                # single-string form ``watch 'docker run …'`` still reaches the CLI. With
                # ``-x``/``--exec`` watch execs the argv directly, so the first operand is the command.
                operands = tokens[i:]
                if watch_exec:
                    peeled = (operands[0], list(operands[1:])) if operands else None
                else:
                    peeled = ("sh", ["-c", " ".join(str(t) for t in operands)])
                break
            peeled = (tok, list(tokens[i + 1:]))  # first bare token is the wrapped command
            break
        if peeled is None:
            return command, args
        command, args = peeled
    # Bound exhausted while still peeling wrappers — treat the chain as reaching docker.
    return "docker", list(args or [])


def _references_positional_params(command_string: str) -> bool:
    """True when a shell command string references positional parameters (``$@``, ``$*``, ``$1``…,
    ``${@}``, ``${1}``) somewhere they EXPAND — i.e. not inside single quotes and not backslash
    escaped (double quotes still expand). ``sh -c 'exec "$@"' $0 docker run`` executes those
    positionals; ``printf '%s' '$@'`` prints a literal and must not trigger inspection."""
    i, n = 0, len(command_string)
    quote: Optional[str] = None
    while i < n:
        ch = command_string[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:  # escaped: the next char is literal (incl. an escaped $)
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = None
                i += 1
                continue
            # fall through — $ still expands inside double quotes
        elif ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "$" and i + 1 < n:
            nxt = command_string[i + 1]
            if nxt in "@*" or nxt.isdigit():
                return True
            if nxt == "{" and i + 2 < n and (command_string[i + 2] in "@*"
                                             or command_string[i + 2].isdigit()):
                return True
        i += 1
    return False


def _find_exec_invokes_docker(args: Optional[list[str]]) -> bool:
    """True when a ``find … -exec/-execdir/-ok/-okdir COMMAND …`` action launches the docker CLI.
    The command runs from ``-exec`` up to the terminating ``;`` or ``+``."""
    tokens = list(args or [])
    k = 0
    while k < len(tokens):
        if tokens[k] in ("-exec", "-execdir", "-ok", "-okdir"):
            k += 1
            cmd_tokens: list[str] = []
            while k < len(tokens) and tokens[k] not in (";", "+", "\\;"):
                cmd_tokens.append(tokens[k])
                k += 1
            if cmd_tokens and _shell_invokes_docker(cmd_tokens[0], cmd_tokens[1:]):
                return True
            continue
        k += 1
    return False


def _redirection_span(word: str) -> Optional[str]:
    """Classify ``word`` as a shell redirection: ``"attached"`` (target glued, e.g. ``>/dev/null``,
    ``2>err``), ``"bare"`` (operator only, target is the NEXT token, e.g. ``>``, ``2>``, ``>&``), or
    ``None`` (not a redirection). Lets ``>/dev/null docker run`` reach ``docker``."""
    k = 0
    while k < len(word) and word[k].isdigit():
        k += 1
    if k < len(word) and word[k] == "&" and k + 1 < len(word) and word[k + 1] in "<>":
        k += 1  # &> / &>> form
    if k >= len(word) or word[k] not in "<>":
        return None
    while k < len(word) and word[k] in "<>&":
        k += 1
    return "attached" if k < len(word) else "bare"


def _declared_function_name(words: list[str], idx: int) -> Optional[str]:
    """The shell function NAME a declaration at command position ``idx`` defines, else ``None``.

    Covers bash's ``function NAME`` keyword form and the POSIX ``NAME () { … }`` form — whose ``()``
    may be a token glued to the name (``docker()``), split off (``docker`` ``()``), or half-glued
    (``docker(``) under ``shlex``. A declaration is not a launch, and the NAME then shadows the CLI
    for later commands."""
    if idx >= len(words):
        return None
    w = words[idx]
    if w == "function":
        if idx + 1 < len(words):
            name = words[idx + 1].rstrip("(){}")
            return name if _is_shell_name(name) else None
        return None
    if "(" in w:  # ``NAME()`` / ``NAME(`` glued to the name
        name = w[: w.index("(")]
        return name if _is_shell_name(name) else None
    if _is_shell_name(w) and idx + 1 < len(words) and words[idx + 1].startswith("("):
        return w  # ``NAME ()`` — the ``()`` is a separate token
    return None


def _segment_declares_function(segment: str) -> Optional[str]:
    """The function name declared by ``segment`` in command position (bash ``function NAME`` or the
    POSIX ``NAME () { … }`` form), or ``None``. (``echo function docker`` is an argument, not a
    declaration.)"""
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    idx = 0
    while idx < len(words):  # reach the command word (skip redirections/assignments)
        span = _redirection_span(words[idx])
        if span == "attached":
            idx += 1
        elif span == "bare":
            idx += 2
        elif _looks_like_assignment(words[idx]):
            idx += 1
        else:
            break
    return _declared_function_name(words, idx)


def _segment_invokes_docker(segment: str, funcs: frozenset[str] = frozenset(),
                            env: Optional[dict[str, str]] = None,
                            func_bodies: Optional[dict[str, str]] = None,
                            inspecting: frozenset[str] = frozenset()) -> bool:
    """True when a single simple-command segment launches the docker CLI as its command word.

    Leading redirections, ``NAME=VALUE`` assignments, and shell reserved words are skipped to reach
    the real command; ``eval``/``trap`` re-parse their argument as fresh shell input; ``coproc`` may
    carry an optional name; ``builtin`` re-inspects its operand. A command word that names a defined
    shell function (``funcs``) is not the CLI directly — but its recorded body (``func_bodies``) is
    inspected, since executing the call runs it (``f() { docker run; }; f``); ``inspecting`` guards
    against recursion. ``env`` carries deterministic variable values for nested shell inputs."""
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    idx = 0
    inline_env: dict[str, str] = {}
    while idx < len(words):
        w = words[idx]
        span = _redirection_span(w)
        if span == "attached":  # e.g. >/dev/null (target glued) — skip the redirection
            idx += 1
            continue
        if span == "bare":  # e.g. > /dev/null (operator, target is the next token) — skip both
            idx += 2
            continue
        if w == "in":
            # after ``in`` (of ``for``/``case``) the rest of this segment is a word-list / case
            # pattern, not a command — ``case docker in docker) …`` / ``for x in docker …``.
            return False
        if w == "function":  # `function NAME [()] { … }` — NAME is a declaration, not a command
            idx += 2
            continue
        if w in _SHELL_DECL_KEYWORDS:  # for/select/case: the NEXT word is a var/subject, not a cmd
            idx += 2
            continue
        if w == "time":  # `time [-p] pipeline` — skip the keyword and its options
            idx += 1
            while idx < len(words) and words[idx] in ("-p", "--portability", "--"):
                idx += 1
            break
        if _looks_like_assignment(w):  # ``D=docker cmd`` scopes D to cmd's environment — carry it
            name, value = w.split("=", 1)
            inline_env[name] = value
            idx += 1
            continue
        if w in _SHELL_RESERVED_WORDS:
            idx += 1
            continue
        break
    if idx >= len(words):
        return False
    if inline_env:  # a leading ``NAME=VALUE`` prefix reaches the command's environment (e.g. a shell)
        env = {**(env or {}), **inline_env}
    if _declared_function_name(words, idx) is not None:
        return False  # a POSIX ``NAME () { … }`` declaration in command position — not a launch
    cmd = words[idx]
    if cmd in funcs:  # a call to a locally-defined shell function
        bodies = func_bodies or {}
        if cmd in bodies and cmd not in inspecting:
            # executing the call runs the function's body — inspect it, keeping the function's own
            # name shadowed (a self-call recurses, it doesn't reach the CLI). ``command docker``
            # inside the body still bypasses the shadow (handled by the ``command`` branch).
            return _shell_command_invokes_docker(
                bodies[cmd], env, seed_funcs=funcs, func_bodies=bodies,
                inspecting=inspecting | {cmd})
        return False  # no recorded body, or already being inspected (recursion) — not the CLI
    if _launcher_basename(cmd) in _SOURCE_BUILTINS:
        # ``source``/``.`` reading a stdin path executes whatever feeds fd 0; a ``<<<`` here-string
        # is that stdin, so ``source /dev/stdin <<< 'docker run'`` runs the payload as shell script.
        # (A heredoc feeding source is preserved earlier by ``_line_feeds_shell``/``_strip_heredocs``.)
        rest = words[idx + 1:]
        if "<<<" in rest and any(str(a) in _STDIN_PATHS for a in rest):
            k = rest.index("<<<")
            if k + 1 < len(rest):
                return _shell_command_invokes_docker(str(rest[k + 1]), env)
        return False
    if cmd == "eval":  # eval joins its args and executes them as shell input
        return _shell_command_invokes_docker(
            " ".join(words[idx + 1:]), env, seed_funcs=funcs, func_bodies=func_bodies or {},
            inspecting=inspecting)
    if cmd == "command":  # `command [-pvV] name …` — -v/-V only look up (no launch); skip -p/--
        rest = words[idx + 1:]
        j = 0
        while j < len(rest) and rest[j].startswith("-") and rest[j] != "--":
            if set(rest[j][1:]) & {"v", "V"}:
                return False
            j += 1
        if j < len(rest) and rest[j] == "--":
            j += 1
        # ``command`` explicitly SUPPRESSES shell-function lookup, so a same-named function does not
        # shadow the CLI here: ``function docker { :; }; command docker run`` runs the real docker.
        return bool(rest[j:]) and _segment_invokes_docker(" ".join(rest[j:]), frozenset(), env)
    if cmd == "builtin" and words[idx + 1:]:  # run the named builtin
        return _segment_invokes_docker(
            " ".join(words[idx + 1:]), funcs, env, func_bodies, inspecting)
    if cmd == "trap":  # `trap [OPTS] ACTION [SIG…]` — the first non-option arg is shell input
        for arg in words[idx + 1:]:
            if arg.startswith("-"):
                continue
            return _shell_command_invokes_docker(arg, env)
        return False
    if cmd == "coproc":  # `coproc [NAME] command …` — command is at +1 (no name) or +2 (named)
        rest = words[idx + 1:]
        if rest and _shell_invokes_docker(rest[0], rest[1:], env):
            return True
        return len(rest) >= 2 and _shell_invokes_docker(rest[1], rest[2:], env)
    return _shell_invokes_docker(cmd, words[idx + 1:], env)


def _segment_assignments(segment: str) -> dict[str, str]:
    """``NAME=VALUE`` assignments a segment persists to the shell so a later ``$NAME`` command word
    resolves. Two shapes persist: a segment that is PURELY assignments (``D=docker``), and an
    assignment builtin (``export``/``declare``/… ``D=docker``) which both runs and persists. A plain
    ``D=docker cmd`` scopes ``D`` to ``cmd``'s environment only, so it persists nothing."""
    try:
        words = shlex.split(segment)
    except ValueError:
        return {}
    if words and words[0] in _ASSIGNMENT_BUILTINS:
        words = words[1:]  # ``export``/``declare`` NAME=VALUE … — mine the assignment operands
        result: dict[str, str] = {}
        for w in words:
            if _looks_like_assignment(w):
                name, value = w.split("=", 1)
                result[name] = value
            # bare names (``export D``) and options (``-x``) carry no literal value — skip them
        return result
    result = {}
    for w in words:
        if not _looks_like_assignment(w):
            return {}  # a command word is present — the assignments are env-scoped, not persisted
        name, value = w.split("=", 1)
        result[name] = value
    return result


def _collect_aliases(segment: str) -> dict[str, str]:
    """The aliases an ``alias NAME=VALUE …`` segment defines, so a later command word that is an
    alias name resolves (``alias d=docker`` → a later ``d run`` launches docker)."""
    try:
        words = shlex.split(segment)
    except ValueError:
        return {}
    if not words or words[0] != "alias":
        return {}
    result: dict[str, str] = {}
    for w in words[1:]:
        if w.startswith("-"):  # options like ``-p`` (print) carry no definition
            continue
        if "=" in w:
            name, value = w.split("=", 1)
            if _is_shell_name(name):
                result[name] = value
    return result


def _expand_alias(segment: str, aliases: dict[str, str], _depth: int = 0) -> str:
    """Rewrite a segment whose command word is an alias to its definition (``d run`` → ``docker
    run``), following chained aliases up to a small depth bound."""
    if not aliases or _depth > 8:
        return segment
    try:
        words = shlex.split(segment)
    except ValueError:
        return segment
    idx = 0
    while idx < len(words):
        span = _redirection_span(words[idx])
        if span == "attached":
            idx += 1
        elif span == "bare":
            idx += 2
        elif _looks_like_assignment(words[idx]):
            idx += 1
        else:
            break
    if idx >= len(words) or words[idx] not in aliases:
        return segment
    prefix = " ".join(words[:idx])
    suffix = " ".join(words[idx + 1:])
    rebuilt = f"{prefix} {aliases[words[idx]]} {suffix}".strip()
    return _expand_alias(rebuilt, aliases, _depth + 1)


_FUNC_HEADER_RE = re.compile(
    r"(?:\bfunction\s+([A-Za-z_]\w*)\s*(?:\(\s*\))?|([A-Za-z_]\w*)\s*\(\s*\))\s*\{")


def _extract_function_bodies(command_string: str) -> dict[str, str]:
    """Map each shell function NAME to its brace-delimited body text (bash ``function NAME { … }`` or
    POSIX ``NAME () { … }``), so a call to the function can be inspected — its body may launch docker
    (``f() { docker run; }; f``). Quote- and nesting-aware brace matching."""
    bodies: dict[str, str] = {}
    for m in _FUNC_HEADER_RE.finditer(command_string):
        name = m.group(1) or m.group(2)
        brace = m.end() - 1
        depth, j, n = 0, brace, len(command_string)
        quote: Optional[str] = None
        while j < n:
            ch = command_string[j]
            if quote is not None:
                if ch == "\\" and quote == '"' and j + 1 < n:
                    j += 2
                    continue
                if ch == quote:
                    quote = None
                j += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                j += 1
                continue
            if ch == "\\" and j + 1 < n:
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    bodies[name] = command_string[brace + 1:j]
                    break
            j += 1
    return bodies


def _set_positionals(segment: str) -> Optional[list[str]]:
    """The positional parameters a ``set`` command installs (``set -- docker run`` → ``[docker,
    run]``), or ``None`` when the segment isn't such a ``set``. These become ``$1``/``$@`` for the
    commands that follow, so ``set -- docker run; "$@"`` launches docker."""
    try:
        words = shlex.split(segment)
    except ValueError:
        return None
    idx = 0
    while idx < len(words) and (_looks_like_assignment(words[idx])
                                or _redirection_span(words[idx])):
        idx += 1
    if idx >= len(words) or words[idx] != "set":
        return None
    rest = words[idx + 1:]
    if "--" in rest:  # explicit end-of-options: everything after is positional
        return rest[rest.index("--") + 1:]
    k = 0  # no ``--``: skip option flags, then the remainder are positional words
    while k < len(rest) and rest[k][:1] in ("-", "+"):
        if rest[k] in ("-o", "+o"):  # ``set -o pipefail`` consumes a value
            k += 2
        else:
            k += 1
    return rest[k:]


def _expand_positionals(segment: str, positionals: list[str]) -> str:
    """Replace the whole-list positional forms (``$@`` ``$*`` ``${@}`` ``${*}``, quoted or not) with
    the ``set --`` positionals as SEPARATE words — ``"$@"`` word-splits in bash, so ``"$@"`` with
    ``[docker, run]`` must yield a ``docker`` command word, not a single ``"docker run"`` token.
    Single-quoted spans are left literal."""
    joined = " ".join(shlex.quote(p) for p in positionals)
    forms = ('"$@"', '"$*"', '"${@}"', '"${*}"', "$@", "$*", "${@}", "${*}")
    out: list[str] = []
    i, n = 0, len(segment)
    in_single = False
    while i < n:
        ch = segment[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(segment[i + 1])
            i += 2
            continue
        match = next((f for f in forms if segment.startswith(f, i)), None)
        if match is not None:
            out.append(joined)
            i += len(match)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _expand_known_vars(segment: str, assignments: dict[str, str],
                       positionals: Optional[list[str]] = None) -> str:
    """Substitute ``$VAR``/``${VAR}`` with a known literal assignment so a deterministic
    ``D=docker; $D run`` resolves its command word. Single-quoted spans are left literal (no
    expansion); double quotes expand. An unknown plain ``${NAME}`` collapses to empty (bash treats
    an unset variable as ``""``) so ``${EMPTY}docker`` still resolves to a ``docker`` command word;
    a bare unknown ``$NAME`` is left intact (never a launcher on its own). ``positionals`` (from a
    prior ``set --``) resolve ``$@``/``$*``/``$1``… — but only when non-empty, so an as-yet-unset
    ``$@`` is left for the argv-based positional check."""
    positionals = positionals or []
    if not assignments and not positionals and "${" not in segment:
        return segment
    if positionals:  # resolve ``$@``/``$*`` (word-splitting forms) before the scalar pass below
        segment = _expand_positionals(segment, positionals)
    out: list[str] = []
    i, n = 0, len(segment)
    quote: Optional[str] = None
    while i < n:
        ch = segment[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:  # escaped char is literal (an escaped $ does not expand)
            out.append(ch)
            out.append(segment[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = None if ch == quote else (quote or ch)
            out.append(ch)
            i += 1
            continue
        if ch == "$" and i + 1 < n:
            if segment[i + 1] == "{":
                j = segment.find("}", i + 2)
                if j != -1:
                    name = segment[i + 2:j]
                    if name in assignments:
                        out.append(assignments[name])
                        i = j + 1
                        continue
                    if positionals and name.isdigit():
                        p = int(name)
                        out.append(positionals[p - 1] if 1 <= p <= len(positionals) else "")
                        i = j + 1
                        continue
                    if _is_shell_name(name) and name != "IFS":  # unknown plain ${NAME} → unset = ""
                        i = j + 1
                        continue
            elif positionals and segment[i + 1].isdigit():  # $1..$9 → a specific positional
                p = int(segment[i + 1])
                out.append(positionals[p - 1] if 1 <= p <= len(positionals) else "")
                i += 2
                continue
            else:
                j = i + 1
                while j < n and (segment[j].isalnum() or segment[j] == "_"):
                    j += 1
                name = segment[i + 1:j]
                if name and name in assignments:
                    out.append(assignments[name])
                    i = j
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_pipelines(command_string: str) -> list[list[str]]:
    """Split a command line into pipelines, each a list of its ``|``-separated stages. Boundaries
    that are NOT a pipe (``;`` ``&`` ``&&`` ``||`` newline) start a fresh pipeline. Quote-aware, so a
    ``|`` inside quotes is not a boundary."""
    pipelines: list[list[str]] = []
    current: list[str] = []
    buf: list[str] = []
    quote: Optional[str] = None
    i, n = 0, len(command_string)
    while i < n:
        ch = command_string[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(ch)
                buf.append(command_string[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command_string[i + 1])
            i += 2
            continue
        if ch == "|":
            current.append("".join(buf))
            buf = []
            if i + 1 < n and command_string[i + 1] == "|":  # ``||`` — a logical break, not a pipe
                pipelines.append(current)
                current = []
                i += 2
                continue
            i += 2 if (i + 1 < n and command_string[i + 1] == "&") else 1  # ``|`` or ``|&``
            continue
        if ch in ";&\n":
            current.append("".join(buf))
            buf = []
            pipelines.append(current)
            current = []
            i += 2 if (ch == "&" and i + 1 < n and command_string[i + 1] == "&") else 1
            continue
        buf.append(ch)
        i += 1
    current.append("".join(buf))
    pipelines.append(current)
    return pipelines


def _is_stdin_reading_shell(stage: str) -> bool:
    """True when ``stage`` runs a shell that reads its SCRIPT from stdin — a launcher with no ``-c``
    and no script-file operand (``… | sh`` / ``… | bash -s``). Such a shell executes whatever the
    pipe feeds it."""
    try:
        words = shlex.split(stage)
    except ValueError:
        return False
    idx = 0
    while idx < len(words) and _looks_like_assignment(words[idx]):
        idx += 1
    if idx >= len(words):
        return False
    cmd, rest = _strip_wrappers(words[idx], words[idx + 1:])
    if _launcher_basename(cmd) not in _SHELL_LAUNCHERS:
        return False
    j = 0
    tokens = list(rest or [])
    while j < len(tokens):
        tok = tokens[j]
        if tok == "-c" or (tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]):
            return False  # a ``-c`` command string, not stdin
        if tok in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            j += 2
            continue
        if tok == "--" or not tok.startswith("-"):
            return False  # a script-file operand — the shell reads the file, not stdin
        j += 1
    return True  # a shell launcher with neither -c nor a script file reads its program from stdin


def _static_command_output(stage: str) -> Optional[str]:
    """The literal text a static ``echo``/``printf`` stage writes to stdout (so a following piped
    shell's script is known), or ``None`` when the stage's output isn't statically known."""
    try:
        words = shlex.split(stage)
    except ValueError:
        return None
    idx = 0
    while idx < len(words) and _looks_like_assignment(words[idx]):
        idx += 1
    if idx >= len(words):
        return None
    cmd = _launcher_basename(words[idx])
    rest = words[idx + 1:]
    if cmd == "echo":
        body = [w for w in rest if not (len(w) >= 2 and w[0] == "-" and set(w[1:]) <= set("neE"))]
        return _decode_ansi_c(" ".join(body))
    if cmd == "printf":
        if not rest:
            return None
        return _printf_output(rest[0], rest[1:])
    return None


def _printf_output(fmt: str, args: list[str]) -> str:
    """Best-effort ``printf`` emulation for the static-pipeline guard: substitute conversion specs
    (``%s``/``%d``/…) with the operands, repeating the format until the operands are exhausted (as
    printf does), so ``printf '%s ' docker run alpine`` yields ``docker run alpine``."""
    fmt = _decode_ansi_c(fmt)
    if "%" not in fmt or not args:
        return fmt
    out: list[str] = []
    ai = 0
    while True:
        consumed_before = ai
        i, n = 0, len(fmt)
        while i < n:
            c = fmt[i]
            if c == "%" and i + 1 < n:
                j = i + 1
                while j < n and fmt[j] in "-+ 0#123456789.*":  # flags/width/precision
                    j += 1
                if j < n:
                    conv = fmt[j]
                    if conv == "%":
                        out.append("%")
                    else:
                        out.append(args[ai] if ai < len(args) else "")
                        ai += 1
                    i = j + 1
                    continue
            out.append(c)
            i += 1
        if ai >= len(args) or ai == consumed_before:  # all operands used, or no conversion consumed
            break
    return "".join(out)


def _is_passthrough_filter(stage: str) -> bool:
    """True when ``stage`` forwards its stdin to stdout unchanged (``cat`` with no file, ``tee``), so
    a static literal survives it on the way to a stdin-reading shell (``printf … | cat | sh``)."""
    try:
        words = shlex.split(stage)
    except ValueError:
        return False
    idx = 0
    while idx < len(words) and _looks_like_assignment(words[idx]):
        idx += 1
    if idx >= len(words):
        return False
    base = _launcher_basename(words[idx])
    rest = words[idx + 1:]
    if base == "tee":  # tee always copies stdin to stdout (plus its file args)
        return True
    if base == "cat":  # cat forwards stdin only with no real file operand (``-`` is stdin)
        return all(w == "-" or w.startswith("-") for w in rest)
    return False


def _pipe_into_stdin_shell_invokes_docker(command_string: str,
                                          env: Optional[dict[str, str]] = None) -> bool:
    """True when a static ``echo``/``printf`` is piped into a stdin-reading shell that then launches
    docker (``printf 'docker run\\n' | sh``, even through pass-through filters like
    ``… | cat | sh``) — the piped literal IS the shell's script."""
    for pipeline in _split_pipelines(command_string):
        for k in range(1, len(pipeline)):
            if _is_stdin_reading_shell(pipeline[k]):
                j = k - 1  # walk back through pass-through filters to the static source
                while j > 0 and _is_passthrough_filter(pipeline[j]):
                    j -= 1
                literal = _static_command_output(pipeline[j])
                if literal is not None and _shell_command_invokes_docker(literal, env):
                    return True
    return False


def _shell_command_invokes_docker(command_string: str, env: Optional[dict[str, str]] = None,
                                  seed_funcs: frozenset[str] = frozenset(),
                                  func_bodies: Optional[dict[str, str]] = None,
                                  inspecting: frozenset[str] = frozenset()) -> bool:
    """True when any simple command in a shell ``-c`` command string launches the docker CLI.

    The string is first normalized for line continuations and ANSI-C/locale ``$'…'`` quoting.
    Command/process substitutions (``$(…)``/backticks/``<(…)``, executed even inside double quotes)
    are inspected next, then the string is split into segments at unquoted shell operators and each
    segment's command word is checked. This catches ``foo && docker run`` / ``echo "$(docker …)"``
    while still allowing ``docker`` to appear merely as an argument (``python -m srv --backend
    docker``). ``env`` seeds deterministic variable values (the configured server environment merged
    into the child by the bridge), so ``"$D" run`` with ``env={"D": "docker"}`` resolves to a launch.
    ``seed_funcs``/``func_bodies``/``inspecting`` carry the enclosing scope when inspecting a called
    function's body."""
    command_string = _preprocess_shell_string(_strip_heredocs(command_string))
    if any(_shell_command_invokes_docker(sub, env)
           for sub in _command_substitutions(command_string)):
        return True
    if _pipe_into_stdin_shell_invokes_docker(command_string, env):
        return True
    if _process_subst_invokes_docker(command_string, env):
        return True
    # Walk segments in order, growing the declared shell functions (and their bodies), aliases,
    # simple variable assignments, and ``set --`` positionals as we go: a ``function docker { … }``
    # only shadows the CLI for commands AFTER it, ``D=docker`` only resolves ``$D`` for later
    # segments, and ``set --`` installs ``$@`` for what follows — so an earlier ``docker run`` is
    # still a launch. Function bodies (present anywhere in the string) let a call be inspected.
    funcs: set[str] = set(seed_funcs)
    bodies: dict[str, str] = {**(func_bodies or {}), **_extract_function_bodies(command_string)}
    aliases: dict[str, str] = {}
    assignments: dict[str, str] = {k: v for k, v in (env or {}).items() if isinstance(v, str)}
    positionals: list[str] = []
    for seg in _split_shell_commands(command_string):
        expanded = _expand_alias(_expand_known_vars(seg, assignments, positionals), aliases)
        if _segment_invokes_docker(expanded, frozenset(funcs), env, bodies, inspecting):
            return True
        declared = _segment_declares_function(seg)
        if declared:
            funcs.add(declared)
        aliases.update(_collect_aliases(seg))
        assignments.update(_segment_assignments(seg))
        set_pos = _set_positionals(seg)
        if set_pos is not None:
            positionals = set_pos
    return False


def _npm_call_invokes_docker(base: str, tokens: list[str],
                             env: Optional[dict[str, str]] = None) -> bool:
    """True when ``npx``/``npm exec`` runs a docker CLI via its ``-c``/``--call`` option, whose value
    is a command string executed through a shell (``npx -c 'docker run …'`` == ``npm exec -c``)."""
    i = 0
    if base == "npm":  # the call form is ``npm exec``/``npm x`` — require that subcommand
        # Global options (``--prefix /tmp``, ``--registry …``) may precede ``exec`` and some consume
        # a following token, so scan for the subcommand token anywhere rather than assuming arity.
        try:
            i = next(k for k, t in enumerate(tokens) if t in ("exec", "x")) + 1
        except StopIteration:
            return False
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-c", "--call"):
            return i + 1 < len(tokens) and _shell_command_invokes_docker(str(tokens[i + 1]), env)
        if tok.startswith("--call="):
            return _shell_command_invokes_docker(tok.split("=", 1)[1], env)
        if tok.startswith("-c") and not tok.startswith("--"):  # ``-c=cmd`` / ``-ccmd`` inline value
            return _shell_command_invokes_docker(tok[3:] if tok[2:3] == "=" else tok[2:], env)
        i += 1
    return False


def _shell_invokes_docker(command: str, args: Optional[list[str]],
                          env: Optional[dict[str, str]] = None) -> bool:
    """Best-effort guard for shell-wrapped Docker CLI invocations.

    Direct docker launchers are canonicalized to the docker runner. A shell wrapper cannot be
    safely normalized without changing its semantics, but it also must not run as an ordinary
    local command because that bypasses the docker gate, hardening, and minimal environment.
    Detect the common /bin/sh -c / bash -lc shape (and thin-wrapper variants) and block it at
    enable/start time. Mutually recursive with the ``-c`` string inspectors above; recursion always
    shrinks the input (a ``-c`` string is a proper substring), so it terminates.

    BEST-EFFORT BY DESIGN — and deliberately bounded. A ``command``/``npx``/``uvx`` runner executes
    arbitrary code with the control-plane environment already; this guard closes the *static,
    recognizable* ways a config reaches the docker CLI (wrappers, assignments, reserved words,
    command substitution, ``eval`` of a literal, …), not the ones that need execution to resolve
    (``eval "$(some_cmd)"``, a helper script that shells out to docker, base64-decode-pipe-to-sh).
    Those are out of reach of any parser and out of scope: the real containment for a hostile
    local-exec config is not enabling untrusted ``command`` servers, not this string analysis."""
    wrapper_env: dict[str, str] = {}
    command, args = _strip_wrappers(command, args, wrapper_env)
    if wrapper_env:  # ``env D=docker sh -c …`` — the wrapper sets these in the child's environment
        env = {**(env or {}), **wrapper_env}
    # Bash brace expansion can hide the launcher (``{docker,} run`` runs ``docker``): check every
    # brace-list expansion of the command word. Fail closed if expansion was truncated (a later
    # ``docker`` alternative could have been dropped by the cap). ``_is_docker_command`` also treats
    # a relative path like ``bin/docker`` as a launcher (this is a command position, not an image).
    candidates, brace_truncated = _brace_expand(command)
    if brace_truncated or any(_is_docker_command(c) for c in candidates):
        return True
    base = _launcher_basename(command)
    if base == "find":  # `find … -exec docker …` launches its child command
        return _find_exec_invokes_docker(args)
    if base in ("npx", "npm"):  # `npx -c '<cmd>'` / `npm exec -c '<cmd>'` run a string via a shell
        return _npm_call_invokes_docker(base, list(args or []), env)
    if base not in _SHELL_LAUNCHERS:
        return False
    tokens = list(args or [])
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not isinstance(tok, str):
            return False
        # POSIX shells take the command string after -c. Options may be combined (e.g. -lc), but a
        # long option that merely contains 'c' (--norc, --noprofile) is NOT -c: only single-dash
        # option groups carry -c (sh/bash have no other 'c' short option). The FIRST -c wins — the
        # shell reads exactly one command string; a later -c is a positional ($0) whose args are
        # never executed (so ``sh -c 'echo ok' -c 'docker …'`` isn't false-rejected).
        if tok == "-c" or (tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]):
            if i + 1 >= len(tokens):
                return False
            cmd_str = str(tokens[i + 1])
            if _shell_command_invokes_docker(cmd_str, env):
                return True
            # Args after the command string are $0, $1, … — a script that runs "$@"/"$0"/"$N"
            # executes them. When the string references positionals, inspect EACH as a potential
            # command word (``exec "$0"`` / ``exec "$@"`` / ``exec "$2"``), with the following
            # positionals as its args — a higher positional (``$2``) must not be missed.
            if _references_positional_params(cmd_str):
                for start in range(i + 2, len(tokens)):
                    pos = tokens[start:]
                    if pos and _shell_invokes_docker(str(pos[0]), list(pos[1:]), env):
                        return True
            return False
        if tok == "<<<":  # here-string: the shell executes the following word as script on stdin
            return i + 1 < len(tokens) and _shell_command_invokes_docker(str(tokens[i + 1]), env)
        # ``-o option`` / ``-O shopt`` (and their long forms) consume the NEXT token as a value, so
        # skip both — otherwise that value would be mistaken for the script operand below.
        if tok in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        # The first non-option operand is the SCRIPT FILE (``bash script.sh …``); everything after
        # it is the script's own argv, not a shell command string — so any later -c is irrelevant.
        # ``--`` explicitly ends options; the next token is then the script file.
        if tok == "--" or not tok.startswith("-"):
            return False
        i += 1
    return False


def local_exec_invokes_docker(runner: str, command: str, args: Optional[list[str]],
                              env: Optional[dict[str, str]] = None) -> bool:
    """True when a local-exec server would invoke the Docker CLI outside the docker runner. ``env``
    is the configured server environment (merged into the child by the bridge), so a command word
    resolved from a deterministic variable (``"$D" run`` with ``D=docker``) is caught."""
    if runner not in _LOCAL_EXEC_RUNNERS:
        return False
    try:
        return _shell_invokes_docker(command, args, env)
    except RecursionError:
        # Pathologically nested ``$(…)``/``sh -c`` could exhaust the recursion limit. Fail closed —
        # reject the (malformed) config rather than letting the exception 500 an API call or abort a
        # reconcile pass.
        return True


def setup_script_invokes_docker(runner: str, setup_script: Optional[str],
                                env: Optional[dict[str, str]] = None) -> bool:
    """True when a local-exec server's setup script would invoke the Docker CLI. The script runs as
    ``/bin/sh -e -c <script>`` with the passthrough child environment (``ServerUnit._run_setup``),
    so it bypasses the docker gate/hardening exactly like a shell-wrapped command."""
    if runner not in _LOCAL_EXEC_RUNNERS or not setup_script:
        return False
    try:
        return _shell_command_invokes_docker(setup_script, env)
    except RecursionError:
        return True


# `docker run` flags that CONSUME the next token as their value (so we skip both). Kept
# reasonably complete for real MCP configs; an unknown value-taking flag is the accepted
# edge (it'd be read as a boolean and its value mistaken for the image — rare in practice).
_DOCKER_VALUE_FLAGS = frozenset({
    "-e", "--env", "--env-file",
    "-a", "--attach",
    "-v", "--volume", "--mount", "--tmpfs",
    "-p", "--publish", "--expose",
    "-w", "--workdir",
    "--name", "--hostname", "-h",
    "--network", "--net", "--network-alias", "--ip", "--ip6", "--link", "--link-local-ip",
    "--add-host", "--dns", "--dns-search", "--dns-option", "--mac-address", "--domainname",
    "--label", "-l", "--label-file",
    "-m", "--memory", "--memory-swap", "--memory-reservation", "--memory-swappiness",
    "--kernel-memory", "--cpus", "--cpuset-cpus", "--cpuset-mems", "--cpu-shares", "-c",
    "--cpu-period", "--cpu-quota", "--cpu-rt-period", "--cpu-rt-runtime", "--blkio-weight",
    "-u", "--user", "--userns", "--group-add", "--cgroup-parent", "--cgroupns",
    "--entrypoint",
    "--platform", "--pull", "--isolation", "--pid", "--ipc", "--uts", "--cidfile",
    "--stop-timeout", "--stop-signal", "--restart", "--detach-keys",
    "--device", "--device-cgroup-rule", "--device-read-bps", "--device-write-bps",
    "--device-read-iops", "--device-write-iops", "--volumes-from", "--volume-driver",
    "--ulimit", "--shm-size", "--pids-limit", "--sysctl", "--storage-opt", "--annotation",
    "--security-opt", "--cap-add", "--cap-drop", "--oom-score-adj",
    "--health-cmd", "--health-interval", "--health-timeout", "--health-retries",
    "--health-start-period", "--health-start-interval",
    "--log-driver", "--log-opt", "--gpus", "--runtime",
})


_ENV_FILE_WARNING = (
    "--env-file is not read — add its variables under Environment so they reach the container."
)

# Mount-family flags: dropped by the hardened runner (it doesn't bind host paths). Warn so a
# config that depended on a mount isn't silently imported as a broken server.
_DOCKER_MOUNT_FLAGS = frozenset({"-v", "--volume", "--mount", "--tmpfs"})

# --entrypoint is dropped too (the runner owns the invocation and can't override ENTRYPOINT),
# so a config relying on a custom entrypoint would silently start the image's default process.
_ENTRYPOINT_WARNING = (
    "--entrypoint is dropped — the docker runner uses the image's default entrypoint, so the "
    "server may start the wrong process."
)


def _mount_warning(flag: str) -> str:
    return (
        f"{flag} (a host mount) is dropped — the docker runner doesn't bind host paths; the "
        f"server may miss files/data it expected."
    )


# More host-side `docker run` flags the hardened runner owns (and therefore drops on import).
# Each silently changes behavior the operator likely intended, so surface a review warning
# rather than importing a quietly-wrong server.
_WORKDIR_FLAGS = frozenset({"-w", "--workdir"})
_NETWORK_FLAGS = frozenset({"--network", "--net"})
_USER_FLAGS = frozenset({"-u", "--user"})
_WORKDIR_WARNING = (
    "-w/--workdir is dropped — the docker runner uses the image's own WORKDIR, so a relative "
    "command/entrypoint may run from the wrong directory."
)
_NETWORK_WARNING = (
    "--network is dropped — the docker runner uses Docker's default bridge (egress ON), so a "
    "config that set --network none (isolation) or a custom/attached network no longer has it."
)
_PLATFORM_WARNING = (
    "--platform is dropped — the docker runner uses the host architecture, so a config pinning a "
    "platform (e.g. linux/amd64 on arm64) may pull the wrong image or fail with exec-format."
)
_PULL_WARNING = (
    "--pull is dropped — the docker runner uses Docker's default pull policy, so a config that set "
    "--pull always (refreshed tag) or --pull never (offline/reproducible) no longer applies."
)
_USER_WARNING = (
    "-u/--user is dropped — the docker runner runs as the image's default user, so a config that "
    "pinned a UID/GID (least-privilege or file permissions) no longer applies."
)
# --read-only is a BOOLEAN flag (no value), so it's caught in the boolean-skip path, not the
# value-flag branch — but dropping it silently weakens a config that hardened the rootfs.
_READ_ONLY_WARNING = (
    "--read-only is dropped — the hardened runner leaves the container root filesystem writable "
    "(Docker's default), so a config that made it read-only no longer does."
)


def _dropped_flag_warning(flag: str) -> Optional[str]:
    """Review warning for a ``docker run`` flag the hardened runner drops on import, or ``None``.

    The runner owns the whole invocation (it stores only image + container args), so every
    host-side run flag in a pasted config is dropped. Most are benign — they duplicate a
    hardening default or are meaningless under stdio — but a handful silently change intended
    behavior (a host mount, a custom entrypoint, an env-file, a workdir, network isolation, a
    pinned platform, a pull policy, a pinned user, a read-only rootfs); surface those so the
    imported server isn't quietly broken or silently weakened."""
    if flag in _DOCKER_MOUNT_FLAGS:
        return _mount_warning(flag)
    if flag == "--entrypoint":
        return _ENTRYPOINT_WARNING
    if flag == "--env-file":
        return _ENV_FILE_WARNING
    if flag in _WORKDIR_FLAGS:
        return _WORKDIR_WARNING
    if flag in _NETWORK_FLAGS:
        return _NETWORK_WARNING
    if flag == "--platform":
        return _PLATFORM_WARNING
    if flag == "--pull":
        return _PULL_WARNING
    if flag in _USER_FLAGS:
        return _USER_WARNING
    if flag == "--read-only":
        return _READ_ONLY_WARNING
    return None


# Global docker flags (BEFORE the subcommand) that consume the NEXT token as their value. Walking
# these by arity is what lets us find the real `run` subcommand even when a flag's value is itself
# the word "run" (e.g. a one-off context named "run": `docker --context run run img`).
_DOCKER_GLOBAL_VALUE_FLAGS = frozenset({
    "-H", "--host", "-l", "--log-level", "-c", "--context", "--config",
    "--tlscacert", "--tlscert", "--tlskey",
})


def _docker_run_index(tokens: list[str]) -> Optional[int]:
    """Index just AFTER the ``run`` subcommand in a docker arg list, or ``None`` if this isn't
    a ``docker run`` invocation.

    Recognizes ``docker run …``, the ``docker container run …`` long form, and leading global
    flags (``docker --context X run …``) — everything before ``run`` configures the CLI/daemon,
    not the container, so it's dropped. The pre-subcommand global flags are walked by VALUE ARITY
    (not a naive ``tokens.index("run")``) so a flag whose value is literally ``run`` — e.g. a
    context named ``run`` — isn't mistaken for the subcommand. Returns ``None`` (→ treat the
    command as a bare image ref) when the first non-flag token isn't ``run``/``container``, so an
    image whose basename is "docker" with container args isn't misparsed as a launcher."""
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if not isinstance(tok, str):
            return None  # malformed token — caller treats command as a bare image ref
        if tok.startswith("-"):
            # A leading GLOBAL flag: skip it, plus its value when it takes one in the separated
            # form (inline ``--flag=value`` carries its own value, so it never eats the next token).
            if "=" not in tok and tok in _DOCKER_GLOBAL_VALUE_FLAGS:
                i += 2
            else:
                i += 1
            continue
        # First non-flag token is the subcommand.
        if tok == "container" and i + 1 < n and tokens[i + 1] == "run":
            return i + 2
        if tok == "run":
            return i + 1
        return None  # a plain word that isn't run/container — not a `docker run` (bare image ref)
    return None


# Global CLI flags (before `run`) that RETARGET which Docker daemon the command talks to. The
# runner always uses mcpelevator's own configured daemon (DOCKER_HOST), so these are dropped —
# but silently switching daemons is exactly the kind of change to surface for review.
_DAEMON_SELECT_FLAGS = frozenset({"-H", "--host", "-c", "--context"})
_DAEMON_WARNING = (
    "a daemon-selection flag (--context/-c/-H/--host) before `run` is dropped — the docker runner "
    "always targets mcpelevator's own configured Docker daemon (DOCKER_HOST), so the server may "
    "run on a different daemon than the pasted config selected."
)
# --config selects the CLI config DIRECTORY (which can hold registry credentials); dropping it
# means a private-image pull falls back to mcpelevator's own docker config and may fail.
_CONFIG_SELECT_FLAGS = frozenset({"--config"})
_CONFIG_WARNING = (
    "--config (a docker CLI config dir) before `run` is dropped — the docker runner uses "
    "mcpelevator's own docker config, so registry credentials in that config aren't used and a "
    "private-image pull may fail."
)


def _pre_run_flag_present(pre_run_tokens: list[str], flags: frozenset[str]) -> bool:
    """True if the tokens before ``run`` include any of ``flags`` (exact ``--context prod`` /
    ``-H tcp://…`` or inline ``--context=prod`` / ``--config=…``)."""
    return any(tok.split("=", 1)[0] in flags for tok in pre_run_tokens)


def _capture_env(token: str, env: dict[str, str], warnings: list[str]) -> None:
    """Fold a ``-e`` argument into the env map.

    ``VAR=value`` provides a value (the explicit env map still wins, via setdefault).
    A bare ``VAR`` (name-only passthrough) relied on a host env var in the original config;
    we can't read that, so we **scaffold** it as ``VAR=""`` and warn — this keeps the key
    present (the builder emits ``-e VAR`` and it shows up in the review form to fill in),
    rather than silently dropping a required secret so the server starts without it."""
    name, sep, val = token.partition("=")
    if not name:
        return
    if sep:
        env.setdefault(name, val)
    elif name not in env:
        env[name] = ""  # scaffold for review; the operator fills the value before enabling
        warnings.append(
            f"-e {name} relied on a host environment variable — set {name}'s value under "
            f"Environment before starting."
        )


def normalize_docker(
    command: str, args: Optional[list[str]], env: Optional[dict[str, str]]
) -> tuple[str, list[str], dict[str, str], list[str]]:
    """Normalize a docker server to the canonical (image, container_args, env) shape.

    Accepts either a full invocation (``command`` basename in docker/podman, ``args`` the
    ``run …`` line) or an already-bare image ref (``command`` = image, ``args`` = container
    args). Returns ``(image, container_args, env, warnings)``. Raises ``ValueError`` when no
    image can be found. Pure and deterministic — the same input always yields the same shape.
    """
    env = dict(env or {})
    warnings: list[str] = []
    tokens = list(args or [])
    # ``args`` can carry non-string items from a pasted/legacy JSON config (e.g. ["run", 123, …]);
    # the token-walk below calls string methods, so reject a malformed list as a ValueError rather
    # than letting an AttributeError escape. Callers treat ValueError as "skip/leave untouched" —
    # importantly the boot migration, so one bad stored row can't abort startup.
    if any(not isinstance(t, str) for t in tokens):
        raise ValueError("a docker server's args must all be strings")

    # Treat this as a full `docker run …` invocation only when the command actually invokes the
    # docker CLI (bare name or a filesystem path — see _is_docker_launcher) AND the args are a
    # recognized `run` invocation (see _docker_run_index). Otherwise the command IS an image ref
    # whose basename merely happens to be "docker" (the official `docker` image,
    # `ghcr.io/acme/docker`) with container args — preserve it rather than dropping it.
    run_idx = _docker_run_index(tokens) if _is_docker_launcher(command or "") else None
    if run_idx is None:
        image = (command or "").strip()
        if not image:
            raise ValueError("a docker server needs an image reference")
        return image, tokens, env, warnings

    # Leading global flags (before `run`) are dropped — they configure the CLI/daemon, not the
    # container. Most are inert, but a daemon/context selector (--context/-c/-H/--host) or a
    # config-dir selector (--config, which can carry registry creds) silently changes behavior,
    # so warn: the runner always uses mcpelevator's own daemon + docker config.
    pre_run = tokens[: run_idx - 1]
    if _pre_run_flag_present(pre_run, _DAEMON_SELECT_FLAGS):
        warnings.append(_DAEMON_WARNING)
    if _pre_run_flag_present(pre_run, _CONFIG_SELECT_FLAGS):
        warnings.append(_CONFIG_WARNING)

    # Full invocation: start after the `run` subcommand (leading global flags / `container` are
    # dropped — they configure the CLI/daemon, not the container), then token-walk the run flags.
    i = run_idx

    image: Optional[str] = None
    container_args: list[str] = []
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":  # explicit end of options: next token is the image
            i += 1
            if i < n:
                image = tokens[i]
                container_args = tokens[i + 1:]
            break
        if tok.startswith("-"):
            # Attached short form: `-eNAME` / `-eNAME=val` (docker allows `-e` with no space,
            # and reads `-eNAME` as `-e NAME`). Handle before the generic flag branches so the
            # variable isn't dropped. `-e` alone and `-e=…` fall through to the branches below.
            if tok.startswith("-e") and not tok.startswith(("--", "-e=")) and tok != "-e":
                _capture_env(tok[2:], env, warnings)
                i += 1
                continue
            if "=" in tok:  # inline value form, e.g. --env=VAR / --network=none / --memory=1g
                name, _, val = tok.partition("=")
                if name in ("-e", "--env"):
                    _capture_env(val, env, warnings)
                else:
                    warn = _dropped_flag_warning(name)
                    if warn:
                        warnings.append(warn)
                i += 1
                continue
            if tok in ("-e", "--env"):
                if i + 1 < n:
                    _capture_env(tokens[i + 1], env, warnings)
                    i += 2
                else:
                    i += 1
                continue
            if tok in ("-d", "--detach"):
                warnings.append("-d/--detach is incompatible with stdio and is ignored.")
                i += 1
                continue
            if tok in ("--privileged",):
                warnings.append("--privileged is dropped by the hardened docker runner.")
                i += 1
                continue
            if tok in _DOCKER_VALUE_FLAGS:
                # Skip the flag and its value; warn for the ones whose loss silently changes
                # behavior (mount, entrypoint, env-file, workdir, network, platform).
                warn = _dropped_flag_warning(tok)
                if warn:
                    warnings.append(warn)
                i += 2
                continue
            # A boolean flag (-i, -t, -it, --rm, --init, …); skip it. A few booleans still warn
            # when dropped changes intended behavior (e.g. --read-only weakens the rootfs).
            warn = _dropped_flag_warning(tok)
            if warn:
                warnings.append(warn)
            i += 1
            continue
        # First non-flag token is the image; everything after it is the container's args.
        image = tok
        container_args = tokens[i + 1:]
        break

    if not image:
        raise ValueError("a docker server's args must include an image reference")
    return image, container_args, env, warnings


def _validate_docker_env(env: dict[str, str]) -> None:
    """Reject env names that would break the docker runner's no-values-in-argv guarantee.

    The docker builder emits ``-e KEY`` (name only); a key containing ``=`` or whitespace
    would become ``-e KEY=value`` in the argv (leaking the value into ``ps``/``inspect``) or
    an invalid child-env name. A key that collides with the bridge's reserved docker
    connection vars (``DOCKER_HOST`` etc.) would pass the CONTROL daemon endpoint into the
    untrusted container (name-only ``-e`` reads it from the CLI's own env). Reject both at
    the boundary so they never persist."""
    for key in env:
        if "=" in key or any(c.isspace() for c in key):
            raise ValueError(f"invalid environment variable name {key!r} for a docker server")
        if is_reserved_docker_env(key):
            raise ValueError(
                f"{key!r} is reserved for the docker runner and can't be a container env var"
            )
        if is_forbidden_container_env(key):  # a Go proxy var (is_reserved handled just above)
            raise ValueError(
                f"{key!r} can't be a docker container env var: it would alter the docker CLI's own "
                f"HTTP proxy — use the docker CLI's `proxies` config to proxy launched containers"
            )


def _validate_docker_image(image: str) -> None:
    """Reject an image reference that could inject `docker run` options.

    ``build()`` emits the image as a positional after ``--`` (which neutralizes it), but a
    leading-dash image is invalid anyway and, on a legacy row without the ``--`` guard, would
    be parsed by docker as an extra option (``--volume=/:/host``, ``--privileged``, …) —
    bypassing every hardening flag. Reject it at the boundary so it can never persist."""
    if not image or image.startswith("-"):
        raise ValueError("a docker image reference can't be empty or start with '-'")


def _normalize_validate_docker(
    command: str, args: Optional[list[str]], env: Optional[dict[str, str]], *, name: str
) -> tuple[str, list[str], dict[str, str], list[str]]:
    """Shared docker canonicalization for ``create_server`` / ``update_server``: parse a pasted
    invocation to the canonical (image, container_args, env) shape, validate the image + env, and
    log the dropped-flag warnings. Returns ``(command, args, env, warnings)``. The callers own the
    parts that differ per path — the host-cwd reset, the ``warnings_sink`` surfacing (create), and
    the enabled-transition gate (create vs. the rollback-on-deny convert gate in update) — so this
    single seam keeps the two entry points from drifting on any future hardening check."""
    command, args, env, warnings = normalize_docker(command, args, env)
    _validate_docker_image(command)
    _validate_docker_env(env)
    for w in warnings:  # the hardened parser altered the invocation — surface it, don't do it silently
        logger.warning("docker server %r: %s", name, w)
    return command, args, env, warnings


def _require_docker_enabled(session: Session) -> None:
    """Gate the root-equivalent docker runner behind the opt-in ``docker_runner`` setting.

    Raised as ``ValueError`` so the API surfaces a 400. Enforced on the transition to
    *enabled* (create-with-enabled, enable, update-while-enabled); importing/creating a
    disabled docker server is always allowed so a paste can be reviewed first."""
    if not runtime_settings.docker_runner(session):
        raise ValueError(
            "Docker runner is disabled — enable it in Settings first (it is root-equivalent)."
        )


@lru_cache
def _config_hash_salt() -> bytes:
    """Random per-install salt keying ``config_hash``, persisted 0600 in the data dir
    (like the OAuth token store: secret material lives off the DB). Without this file a
    leaked anchor can't be dictionary-attacked for config secrets at all. Losing the
    file is harmless — the boot backfill just rehashes every row once under a fresh salt."""
    path = get_settings().data_dir / "config_hash.salt"
    try:
        salt = path.read_bytes()
        if len(salt) >= 16:
            return salt
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    # Atomic + 0600 from birth (mkstemp), same idiom as the OAuth token store.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="config_hash.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(salt)
    except BaseException:
        os.unlink(tmp_name)
        raise
    os.replace(tmp_name, path)
    return salt


def _hash_payload(server: Server) -> dict[str, Any]:
    return {
        "runner": server.runner,
        "command": server.command,
        "args": server.args,
        "env": server.env,
        "cwd": server.cwd,
        "setup_script": server.setup_script or "",
        "mcp_http": server.mcp_http,
        "rest_openapi": server.rest_openapi,
        # OAuth config drives how the bridge authenticates upstream, so it IS part
        # of the launch spec — a change must restart the bridge. (The tokens live in
        # a file store, not the row, so *authenticating* leaves the hash untouched.)
        # The client SECRET is deliberately NOT read here: it's a credential that
        # doesn't belong in the anchor at all, and the bridge doesn't consume it from the
        # spec anyway (it reads the DCR/static client_info from the token store, and a
        # secret change re-runs auth via the API which clears the tokens). The static
        # client is already tracked by the non-sensitive client_id below.
        "oauth": server.oauth,
        "oauth_scopes": server.oauth_scopes,
        "oauth_client_id": server.oauth_client_id,
        # auth_provider is intentionally excluded: it's enforced at the proxy
        # per-request, so changing it must NOT restart the bridge process.
    }


def compute_hash(server: Server) -> str:
    return config_hash(_hash_payload(server), salt=_config_hash_salt())


def backfill_config_hashes(session: Session) -> int:
    """Recompute ``config_hash`` for stored servers so rows written by an older
    version (with a different hash-input shape — e.g. ``auth_provider`` was once
    included) are rehashed to the current shape. Without this, the first
    non-hash-affecting PATCH on an upgraded server would change the stored hash and
    trigger a spurious bridge restart. Idempotent — only writes rows whose hash
    actually changed. Returns how many were updated.

    Rows already carrying the current scheme tag (see ``config_hash_tag``) are
    trusted as-is — every config write recomputes the hash, so a current-scheme row
    can't be stale — which keeps steady-state boots from paying one scrypt
    derivation per stored server."""
    changed = 0
    salt = _config_hash_salt()
    for server in repo.list_servers(session):
        tag = config_hash_tag(_hash_payload(server), salt=salt)
        # `or ""`: a legacy/hand-edited row can hold NULL — treat it as stale, not a crash.
        if (server.config_hash or "").startswith(f"{tag}."):
            continue
        new_hash = compute_hash(server)
        if new_hash != server.config_hash:
            repo.set_config_hash(session, server.id, new_hash)
            changed += 1
    return changed


def _scrub_docker_env(env: dict[str, str]) -> dict[str, str]:
    """Drop env keys that ``_validate_docker_env`` would reject (malformed names, the bridge's
    reserved DOCKER_*/PATH connection vars, or a Go proxy var). Used by the boot migration,
    which can't raise on a legacy row the way create/update can."""
    return {
        k: v for k, v in env.items()
        if "=" not in k and not any(c.isspace() for c in k) and not is_forbidden_container_env(k)
    }


def normalize_docker_servers(session: Session) -> int:
    """Canonicalize legacy docker rows so enabling one is gated, hardened, and launches the
    right image.

    Two legacy shapes from a prior release (where the docker runner only raised):
    (1) ``runner="docker"`` rows stored verbatim (``command="docker"``, ``args=["run", …]``);
    (2) rows stored under a local-exec runner (``command``/``npx``/``uvx``) because the old
        ``_infer_runner`` matched only the literal ``"docker"`` — so ``command="/usr/local/bin/docker"``
        imports slipped through as generic passthrough servers that would bypass the docker gate +
        hardening. An enabled ``runner="npx"`` row with ``command="/usr/bin/docker"`` would otherwise
        never hit the ``sv.runner == "docker"`` reconcile gate and launch ungated with the full env.
    ANY local-exec row whose command is the docker CLI is converted — not only a recognized
    ``docker run`` (matching create/update, which reclassify on the launcher alone). A row like
    ``command="/usr/bin/docker", args=["compose", "run", …]`` isn't a supported ``docker run``, but
    leaving it as a ``command`` runner would let it talk to the daemon with the full environment
    while the gate is off; converting it to ``runner="docker"`` gates it (it then fails to launch a
    real image, which is correct — that shape was never a working MCP server).
    Both are re-normalized to the canonical (image, container_args, env) shape, converted to
    ``runner="docker"``, and have reserved/malformed env keys scrubbed. A row that's already
    canonical re-normalizes to itself (no write). Idempotent. Returns the count changed. A
    row that can't be parsed (no image) is left untouched — enabling it surfaces the error."""
    changed = 0
    for server in repo.list_servers(session):
        # Gate on the LAUNCHER alone (not a recognized `docker run`): a docker-CLI command with
        # any args must be routed through the gated runner, else it runs ungated as passthrough.
        is_docker_cmd = _is_docker_launcher(server.command or "")
        if server.runner == "docker":
            pass  # always re-normalize existing docker rows (idempotent for canonical ones)
        elif server.runner in _LOCAL_EXEC_RUNNERS and is_docker_cmd:
            pass  # a docker-CLI command misclassified as a passthrough runner — convert + gate it
        else:
            continue
        try:
            image, args, env, _ = normalize_docker(server.command, server.args, server.env)
        except ValueError:
            continue
        env = _scrub_docker_env(env)
        if (
            server.runner != "docker"
            or image != server.command
            or args != list(server.args or [])
            or env != dict(server.env or {})
            or server.cwd is not None
        ):
            server.runner = "docker"
            server.command, server.args, server.env, server.cwd = image, args, env, None
            server.config_hash = compute_hash(server)
            repo.save_server(session, server)
            changed += 1
    return changed


_AUTH_PROVIDERS = {"inherit", "none", "bearer", "oauth"}


def normalize_auth_providers(session: Session) -> int:
    """Canonicalize stored ``auth_provider`` values from older versions (the old API
    schema accepted any ``str``): ``"Bearer"`` / ``"bearer "`` -> ``"bearer"``, and an
    unresolvable value -> ``"inherit"`` (the admin-controlled default). Without this,
    the dashboard/copy snippets advertise a usable endpoint while ``resolve()`` 403s
    the raw value on every ``/s/...`` request. Idempotent. Returns the count changed."""
    changed = 0
    for server in repo.list_servers(session):
        norm = (server.auth_provider or "").strip().lower()
        if norm not in _AUTH_PROVIDERS:
            norm = "inherit"
        if norm != server.auth_provider:
            repo.set_auth_provider(session, server.id, norm)
            changed += 1
    return changed


def normalize_reserved_slugs(session: Session) -> int:
    """Rename servers whose slug has since become reserved (e.g. ``summary``, which would
    shadow the static ``/api/health/summary`` route) via the standard creation-time
    disambiguation (``summary`` -> ``summary-2``). Without this a pre-existing row would
    be silently shadowed by the sibling literal route. Slug is excluded from
    ``config_hash``, so the rename never bounces a running bridge; the reconciler
    converges the routing key within one pass. Idempotent. Returns the count changed."""
    changed = 0
    for server in repo.list_servers(session):
        if server.slug not in _RESERVED_SLUGS:
            continue
        old = server.slug
        server.slug = _unique_slug(session, server.slug)
        repo.save_server(session, server)
        print(
            f"[mcpelevator] slug {old!r} is now reserved — "
            f"server {server.name!r} renamed to {server.slug!r}",
            flush=True,
        )
        changed += 1
    return changed


# Slugs that would collide with a sibling literal segment on the proxy/API routes and
# shadow it. A server slugged "summary" would capture GET /api/health/summary (the
# per-server-readiness aggregate for load balancers) so its own /api/health/{slug}
# could never be reached, and a load balancer would read that summary instead of the
# server's status. Reserved here so such a name is disambiguated (e.g. "summary" ->
# "summary-2") at creation. Note: "all" is NOT reserved — group endpoints live under
# their own /g/<name> prefix, so a server may be slugged "all" and served at /s/all.
_RESERVED_SLUGS = frozenset({"summary"})

# Registry writes were implicitly serialized when the sync service ran inline on the
# event loop; now that the API handlers run them in the threadpool (to keep the scrypt
# config_hash derivation off the loop) they can genuinely interleave, and the write
# paths are read-modify-write: ``_unique_slug`` is check-then-insert, and a concurrent
# partial PATCH could commit a hash computed from a snapshot that no longer describes
# the final row. One process-wide lock restores the old serialization — config writes
# are rare admin actions, so holding it across a ~70ms derivation is irrelevant.
# RLock because ``import_mcp_servers`` re-enters ``create_server``.
_write_lock = threading.RLock()


def _serialized_write(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _write_lock:
            return fn(*args, **kwargs)

    return wrapper


@contextmanager
def config_write_lock():
    """Public access to the process-wide config write lock so a sibling registry (the
    group registry, ``app.groups.registry``) can serialize its referential
    validate-then-write against server create/update/delete. Without it a server delete
    can land between a group write's ``validate_members`` and its commit, persisting a
    group that references a now-deleted server — which the startup validation would then
    refuse to boot on. The lock is an ``RLock``, so re-entering it (same thread) is safe."""
    with _write_lock:
        yield


def _unique_slug(session: Session, name: str) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while slug in _RESERVED_SLUGS or repo.get_server_by_slug(session, slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _validate_slug(session: Session, raw: str, *, current_id: str) -> str:
    """Normalize and validate an operator-chosen slug for an existing server.

    Unlike ``_unique_slug`` (which silently disambiguates at creation), an explicit
    rename must surface a conflict rather than guess: the operator picked this URL,
    so a reserved word or a slug already taken by *another* server is a hard error.
    Re-using the server's own current slug is a no-op and allowed.
    """
    slug = slugify(raw)
    if slug in _RESERVED_SLUGS:
        raise ValueError(f"slug {slug!r} is reserved")
    existing = repo.get_server_by_slug(session, slug)
    if existing is not None and existing.id != current_id:
        raise ValueError(f"slug {slug!r} is already in use")
    return slug


def _normalize_setup_script(runner: str, setup_script: str) -> str:
    if not setup_script.strip():
        return ""
    if runner == "docker":
        raise ValueError("Setup scripts are not supported for Docker servers; add setup to the Docker image.")
    if runner not in _LOCAL_EXEC_RUNNERS:
        raise ValueError("Setup scripts are supported only by the npx, uvx, and command local runners.")
    return setup_script


@_serialized_write
def create_server(
    session: Session,
    *,
    name: str,
    runner: str,
    command: str,
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    setup_script: str = "",
    mcp_http: bool = True,
    rest_openapi: bool = False,
    auth_provider: str = "inherit",
    oauth: bool = False,
    oauth_scopes: str = "",
    oauth_client_id: Optional[str] = None,
    oauth_client_secret: Optional[str] = None,
    enabled: bool = False,
    source: str = "manual",
    warnings_sink: Optional[list[str]] = None,
) -> Server:
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r}; must be one of {RUNNERS}")
    if not command.strip():
        raise ValueError("command is required")
    # A local-exec runner (npx/uvx/command) pointed at the docker CLI IS the docker runner —
    # route it through the docker machinery (normalize + validate + gate + hardening +
    # minimal_env) so it can't launch containers ungated/unhardened with the full control-plane
    # env (choosing a different runner string must not sidestep the root-equivalent gate).
    if runner in _LOCAL_EXEC_RUNNERS and _is_docker_launcher(command):
        runner = "docker"
    elif enabled and local_exec_invokes_docker(runner, command, args, env):
        raise ValueError("Docker CLI invocations require the docker runner")
    setup_script = _normalize_setup_script(runner, setup_script)
    if enabled and setup_script_invokes_docker(runner, setup_script, env):
        raise ValueError("Docker CLI invocations require the docker runner")
    # A remote server reuses command/args for the upstream URL + transport; canonicalize
    # them up front so the persisted row (and config_hash) is deterministic. There is no
    # local process, so a working directory is meaningless — drop it.
    if runner == "remote":
        command, args = normalize_remote(command, args)
        cwd = None
    # OAuth applies only to remote; normalize (and force-off elsewhere) before hashing.
    oauth, oauth_scopes, oauth_client_id, oauth_client_secret = normalize_oauth(
        runner, oauth, oauth_scopes, oauth_client_id, oauth_client_secret
    )
    # A docker server is stored in canonical (image, container_args, env) shape; a pasted
    # `docker run …` invocation is parsed down to it. The gate bites only when the server
    # is created already enabled — a disabled import stays reviewable.
    if runner == "docker":
        command, args, env, warnings = _normalize_validate_docker(command, args, env, name=name)
        cwd = None  # a container has its own filesystem; a host cwd is meaningless
        if warnings_sink is not None:  # let callers (import) surface these to the operator, not just logs
            warnings_sink.extend(warnings)
        if enabled:
            _require_docker_enabled(session)

    server = Server(
        id=new_id(),
        slug=_unique_slug(session, name),
        name=name.strip() or "server",
        runner=runner,
        command=command.strip(),
        args=list(args or []),
        env=dict(env or {}),
        cwd=cwd,
        setup_script=setup_script,
        mcp_http=mcp_http,
        rest_openapi=rest_openapi,
        auth_provider=auth_provider,
        oauth=oauth,
        oauth_scopes=oauth_scopes,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
        enabled=enabled,
        source=source,
    )
    server.config_hash = compute_hash(server)
    return repo.create_server(session, server)


_MUTABLE_FIELDS = {
    "name",
    "runner",
    "command",
    "args",
    "env",
    "cwd",
    "setup_script",
    "mcp_http",
    "rest_openapi",
    "auth_provider",
    "oauth",
    "oauth_scopes",
    "oauth_client_id",
    "oauth_client_secret",
}


@_serialized_write
def update_server(session: Session, server_id: str, changes: dict[str, Any]) -> Server:
    server = repo.get_server(session, server_id)
    if server is None:
        raise KeyError(server_id)
    # The API handler pre-reads this row (for its OAuth signature) BEFORE entering the
    # lock, priming the request session's identity map — so a concurrent PATCH committed
    # while we waited would otherwise be invisible here, and the merge + config_hash
    # below would run on a stale snapshot that no longer describes the final row.
    # Re-read from the DB now that we hold the write lock.
    session.refresh(server)
    # Pre-edit runner: converting a NON-docker server INTO a docker one newly grants the
    # root-equivalent runner, so it must be gated like create/enable. Merely editing a row that
    # was ALREADY docker is not gated (so a broken image/env can be fixed while the runner is
    # off). Captured before the mutation/reclassify below changes server.runner.
    was_docker = server.runner == "docker"
    # slug is identity/routing, not launch config: validated separately and excluded
    # from config_hash, so a rename re-routes the proxy without bouncing the bridge.
    if "slug" in changes:
        server.slug = _validate_slug(session, changes["slug"], current_id=server.id)
    for key, value in changes.items():
        if key in _MUTABLE_FIELDS:
            setattr(server, key, value)
    if server.runner not in RUNNERS:
        raise ValueError(f"unknown runner {server.runner!r}")
    # A local-exec runner (npx/uvx/command) pointed at the docker CLI IS the docker runner (see
    # create) — reclassify so it can't launch containers ungated/unhardened via passthrough.
    if server.runner in _LOCAL_EXEC_RUNNERS and _is_docker_launcher(server.command):
        server.runner = "docker"
    elif server.enabled and local_exec_invokes_docker(
            server.runner, server.command, server.args, server.env):
        # The tracked ORM row is already mutated above, but this branch raises before any query
        # autoflushes it, so the edits are still purely in-memory. Expire (not rollback) discards
        # just this instance's staged edits — so the DENIED change can't be flushed by a later
        # commit on this session — without tearing down unrelated work in the same transaction.
        session.expire(server)
        raise ValueError("Docker CLI invocations require the docker runner")
    try:
        server.setup_script = _normalize_setup_script(server.runner, server.setup_script or "")
    except ValueError:
        session.rollback()
        raise
    if server.enabled and setup_script_invokes_docker(
            server.runner, server.setup_script, server.env):
        session.expire(server)  # raises before any autoflush — discard just this instance's edits
        raise ValueError("Docker CLI invocations require the docker runner")
    if server.runner == "remote":
        server.command, server.args = normalize_remote(server.command, server.args)
        # Converting a local server to remote: PATCH drops the form's cwd:null, so clear
        # the stale working directory here (remote has no process) to keep the row canonical.
        server.cwd = None
    # Normalize OAuth (and force it off for any non-remote runner) so a stray secret can't
    # ride along on a converted server and the hash stays deterministic.
    server.oauth, server.oauth_scopes, server.oauth_client_id, server.oauth_client_secret = (
        normalize_oauth(
            server.runner,
            bool(server.oauth),
            server.oauth_scopes,
            server.oauth_client_id,
            server.oauth_client_secret,
        )
    )
    if server.runner == "docker":
        server.command, server.args, server.env, _ = _normalize_validate_docker(
            server.command, server.args, server.env, name=server.name
        )
        # Converting a local server (with a cwd) to docker: PATCH drops the form's cwd:null,
        # so clear the now-meaningless working directory here to keep the row canonical.
        server.cwd = None
        # Gate a non-docker -> docker CONVERSION on an already-ENABLED row. PATCH can't set
        # enabled=false, so the row stays enabled; without this gate it would start unreviewed
        # the moment the global docker_runner setting is toggled on (the supervisor marks it
        # failed only while the setting is off). Editing a row that was already docker stays
        # ungated (fix a broken image/env offline); enabling a disabled row is gated in
        # set_enabled; creating enabled is gated in create_server.
        if server.enabled and not was_docker:
            try:
                _require_docker_enabled(session)
            except ValueError:
                # The tracked ORM row is already mutated (reclassified/canonicalized) above AND
                # ``_require_docker_enabled`` runs a query that autoflushes it to the DB, so expiring
                # the instance would just reload the flushed runner=docker. Only a rollback undoes
                # the flushed-but-uncommitted conversion — mandatory here (unlike the shell-wrapped
                # gate above, which raises before any flush).
                session.rollback()
                raise
    server.config_hash = compute_hash(server)  # recompute -> drives idempotent reconcile
    return repo.save_server(session, server)


@_serialized_write
def clone_server(session: Session, server_id: str, *, name: Optional[str] = None) -> Server:
    """Create a new server from an existing one's launch + exposure config.

    The clone gets a fresh id and a unique slug derived from its name, and is always
    created disabled (the operator reviews, then enables) so two identical servers
    never race to bind/serve. Pass ``name`` to label the copy; defaults to
    ``"<source> copy"``. Raises ``KeyError`` if the source doesn't exist.
    """
    src = repo.get_server(session, server_id)
    if src is None:
        raise KeyError(server_id)
    new_name = (name or "").strip() or f"{src.name} copy"
    return create_server(
        session,
        name=new_name,
        runner=src.runner,
        command=src.command,
        # Tolerate a NULL JSON column from a legacy/hand-edited row (the model
        # types these non-optional, but the DB can still hold null).
        args=list(src.args or []),
        env=dict(src.env or {}),
        cwd=src.cwd,
        setup_script=src.setup_script or "",
        mcp_http=src.mcp_http,
        rest_openapi=src.rest_openapi,
        auth_provider=src.auth_provider,
        oauth=bool(src.oauth),
        oauth_scopes=src.oauth_scopes or "",
        oauth_client_id=src.oauth_client_id,
        oauth_client_secret=src.oauth_client_secret,
        enabled=False,
        source="clone",
    )


@_serialized_write
def set_enabled(session: Session, server_id: str, enabled: bool) -> Server:
    server = repo.get_server(session, server_id)
    if server is None:
        raise KeyError(server_id)
    # Enabling a docker server is the point the root-equivalent gate bites (import/create
    # left it disabled and reviewable).
    if enabled and server.runner == "docker":
        _require_docker_enabled(session)
    elif enabled and (
            local_exec_invokes_docker(server.runner, server.command, server.args, server.env)
            or setup_script_invokes_docker(server.runner, server.setup_script, server.env)):
        raise ValueError("Docker CLI invocations require the docker runner")
    server.enabled = enabled
    return repo.save_server(session, server)


@_serialized_write
def delete_server(session: Session, server_id: str) -> bool:
    """Thin serialized wrapper over ``repo.delete_server``: a delete racing a threaded
    update that already loaded the row would otherwise make the update's commit blow up
    with a StaleDataError (UPDATE matching 0 rows) instead of ordering deterministically."""
    return repo.delete_server(session, server_id)


# Node/Python launchers we recognize so the runner badge is meaningful. Anything
# else is stored as a generic `command` (still launched verbatim).
_NPX_LAUNCHERS = {"npx", "npx.cmd", "bunx", "pnpm", "node"}
_UVX_LAUNCHERS = {"uvx", "uv"}


def _infer_runner(command: str) -> str:
    # Match on the basename so an absolute launcher path (e.g. "/usr/local/bin/docker",
    # as Claude Desktop configs commonly write) infers the right runner, not "command".
    base = _launcher_basename(command)
    if base in _NPX_LAUNCHERS:
        return "npx"
    if base in _UVX_LAUNCHERS:
        return "uvx"
    if _is_docker_launcher(command):
        return "docker"
    return "command"


@_serialized_write
def import_mcp_servers(
    session: Session, data: dict
) -> tuple[list[Server], list[dict], list[dict]]:
    """Create servers from the standard Claude-Desktop ``mcpServers`` JSON shape.

    Accepts either ``{"mcpServers": {...}}`` or a bare ``{name: {...}}`` map.
    Stdio entries (``command`` + ``args`` + ``env``) become local servers; remote
    entries (``url`` / ``type: sse|streamable-http|http``) become ``remote`` servers
    that proxy the upstream URL. All are stored verbatim and disabled (the user
    reviews, then enables).

    Returns ``(created, skipped, warnings)``. ``warnings`` is a list of
    ``{"name", "warnings": [...]}`` — non-fatal notes for a created (disabled) server the
    operator should see BEFORE enabling, chiefly a docker ``run`` option the hardened runner
    dropped (mount, ``--network none``, ``--env-file``, …). These are also logged, but the
    import response surfaces them so the reviewer isn't blind to the transformation.
    """
    servers_map = data.get("mcpServers") if isinstance(data, dict) and "mcpServers" in data else data
    if not isinstance(servers_map, dict):
        raise ValueError("expected an object with an 'mcpServers' map of servers")

    created: list[Server] = []
    skipped: list[dict] = []
    warnings: list[dict] = []
    for name, entry in servers_map.items():
        if not isinstance(entry, dict):
            skipped.append({"name": str(name), "reason": "entry is not an object"})
            continue
        # Accept the URL under "url" or Gemini CLI's "httpUrl" (its Streamable-HTTP shape,
        # which our own install snippets emit — see frontend/src/lib/install.ts).
        url = entry.get("url") or entry.get("httpUrl")
        # mcpServers configs spell the remote transport as either "type" or "transport";
        # a bare httpUrl implies streamable-http.
        etype = entry.get("type") or entry.get("transport")
        if etype is None and entry.get("httpUrl") and not entry.get("url"):
            etype = "streamable-http"
        if url or etype in ("sse", "streamable-http", "http"):
            # A remote (already-HTTP) MCP server: elevate it as a proxied remote runner.
            if not url:
                skipped.append({"name": str(name), "reason": "remote entry has no url"})
                continue
            try:
                created.append(
                    create_server(
                        session,
                        name=str(name),
                        runner="remote",
                        command=str(url),
                        args=[str(etype)] if etype else [],
                        # mcpServers remote entries carry auth as `headers`; fall back to
                        # `env` for tools that (incorrectly) reuse it for header values.
                        env=dict(entry.get("headers") or entry.get("env") or {}),
                        source="import",
                        enabled=False,
                    )
                )
            # A malformed entry (e.g. non-mapping `headers`/`env` → dict() raises
            # TypeError, or a bad URL/transport → ValueError) is skipped, never fatal.
            except (ValueError, TypeError) as exc:
                skipped.append({"name": str(name), "reason": str(exc)})
            continue
        command = entry.get("command")
        if not command:
            skipped.append({"name": str(name), "reason": "no command to launch"})
            continue
        sink: list[str] = []
        try:
            created.append(
                create_server(
                    session,
                    name=str(name),
                    runner=_infer_runner(command),
                    command=str(command),
                    args=list(entry.get("args") or []),
                    env=dict(entry.get("env") or {}),
                    source="import",
                    enabled=False,
                    warnings_sink=sink,
                )
            )
            if sink:  # a docker paste whose dropped run-options the operator must know about
                warnings.append({"name": str(name), "warnings": sink})
        except (ValueError, TypeError) as exc:
            skipped.append({"name": str(name), "reason": str(exc)})
    return created, skipped, warnings
