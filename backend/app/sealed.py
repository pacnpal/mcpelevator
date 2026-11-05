"""Secret env values: write-only and encrypted at rest, chosen per key.

A secret value (an env var, or a header for the remote, openapi and graphql runners) is stored as
a Fernet token behind ``PREFIX``, so a value is secret exactly when it's sealed. The key
is ``<data_dir>/env.key`` (0600, beside the OAuth tokens and the config-hash salt, never
in the DB). The API never hands a secret value back: it reads ``HIDDEN``, and a client
sends ``HIDDEN`` to keep what's stored. ``runners.build_spec`` unseals them for the
bridge, the only consumer of the plaintext.

Anyone who can edit a server could point it somewhere that captures its env (a
``printenv`` command, a URL on their own host), so ``resolve`` refuses to carry a hidden
value through a change of what the server runs, or through making it visible: the save
has to type those values in again. Losing ``env.key`` leaves the stored values
unreadable; the server then fails to start with an error naming the keys to re-enter.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.util import secret_file

PREFIX = "mcpe-sealed:v1:"
HIDDEN = "<hidden>"


def _valid_key(key: bytes) -> bool:
    try:
        Fernet(key)
        return True
    except ValueError:
        return False


@lru_cache
def _fernet() -> Fernet:
    return Fernet(
        secret_file(get_settings().data_dir / "env.key", Fernet.generate_key, _valid_key)
    )


def is_sealed(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value: str) -> str:
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def _opened(token: str) -> str | None:
    """A sealed value's plaintext, or None when the key can't decrypt it."""
    try:
        return _fernet().decrypt(token[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return None


def unseal_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with every sealed value decrypted. Raises ``ValueError`` naming the keys
    that can't be decrypted (the key file is missing or was replaced)."""
    out: dict[str, str] = {}
    lost: list[str] = []
    for key, value in env.items():
        if is_sealed(value):
            value = _opened(value)
            if value is None:
                lost.append(key)
                continue
        out[key] = value
    if lost:
        raise ValueError(
            f"can't decrypt the hidden env values {', '.join(lost)}: env.key in the data "
            "directory is missing or was replaced, so enter those values again"
        )
    return out


def masked(env: dict[str, str]) -> dict[str, str]:
    """``env`` as the API shows it: each sealed value replaced by ``HIDDEN``."""
    return {key: HIDDEN if is_sealed(value) else value for key, value in env.items()}


def secret_keys(env: dict[str, str]) -> list[str]:
    """The keys whose values are stored sealed: the server's secret env."""
    return sorted(key for key, value in env.items() if is_sealed(value))


def resolve(
    stored: dict[str, str],
    incoming: dict[str, str],
    *,
    secret: set[str] | None,
    retargeted: bool,
    env_runs_code: bool,
) -> dict[str, str]:
    """The env to store after a save.

    ``stored`` is the row's env before the save, ``incoming`` what the save sends: a
    value equal to ``HIDDEN`` (or to the stored token, when the save didn't send env at
    all) keeps that key's sealed value. Everything else is plaintext, sealed when its key
    is in ``secret`` (``None``: each key stays as secret as it already was). A kept hidden
    value is refused when the save makes it visible, or ``retargeted`` the server (changed
    what it runs), so it can't be carried somewhere that reads it back out. When
    ``env_runs_code`` (a local process reads this env at startup), adding, changing or
    removing any other value counts as retargeting too: ``NODE_OPTIONS``, ``PYTHONSTARTUP``,
    ``LD_PRELOAD`` and their many cousins change what runs as surely as the command does,
    and no list of such names would be complete.
    """
    unknown = sorted((secret or set()) - incoming.keys())
    if unknown:
        # Silently storing the value plaintext would be worse: "authorization" for an
        # "Authorization" header would look secret to the caller and be readable.
        raise ValueError(f"secret_env names keys that aren't in env: {', '.join(unknown)}")
    out: dict[str, str] = {}
    kept: list[str] = []
    shown: list[str] = []
    changed = False
    for key, value in incoming.items():
        old = stored.get(key)
        is_secret = key in secret if secret is not None else is_sealed(old)
        if is_sealed(old) and value in (HIDDEN, old):
            kept.append(key)
            if not is_secret:
                shown.append(key)
            out[key] = old
            continue
        if value == HIDDEN:
            raise ValueError(f"env {key}: no hidden value is stored for it, enter the value")
        if is_sealed(value):
            raise ValueError(f"env {key}: a value can't start with {PREFIX!r}")
        if is_secret and is_sealed(old) and _opened(old) == value:
            # Typed in again unchanged: that's a re-entry, but keep the stored token, or a
            # fresh one would change config_hash and restart the server for nothing.
            out[key] = old
            continue
        changed = changed or is_sealed(old) or value != old
        out[key] = seal(value) if is_secret else value
    # Removing a key changes what runs as well: code that falls back to a default when
    # API_URL is unset would carry the kept secret to that default.
    changed = changed or bool(stored.keys() - incoming.keys())
    if shown:
        raise ValueError(
            f"re-enter {', '.join(shown)}: making a secret env value visible needs it "
            "typed in again"
        )
    if kept and (retargeted or (env_runs_code and changed)):
        raise ValueError(
            f"re-enter the secret env values {', '.join(kept)}: changing what this server "
            "runs, including any other env value it starts with, needs them typed in again"
        )
    return out


# --- hiding secret values in captured output ----------------------------------------
#
# Output a server's process writes (its stderr attached to a failed call, a health
# command's output quoted into last_error) can echo a secret env value bare, where no
# credential-shaped scrubber would recognize it. These hide the plaintext values themselves,
# over the raw byte STREAM, before the output is split into lines or cut: a value spanning
# lines (a PEM key), read chunks, or any later cap is still matched whole.


def hide_values(
    raw: bytes,
    values,
    start: int = 0,
    end: int | None = None,
    *,
    open_start: bool = False,
    open_end: bool = False,
) -> str:
    """``raw[start:end]`` decoded, with every byte belonging to an occurrence of a value
    found anywhere in ``raw`` (so one crossing ``start`` or ``end`` too) shown as
    :data:`HIDDEN`. ``open_end``: the stream may continue past the end of ``raw`` (or was
    cut there), so a proper prefix of a value ending there is hidden too; ``open_start``
    likewise for a proper suffix at the start of ``raw`` (the stream's head was dropped). All matching is on the raw
    BYTES, before decoding, so an edge that splits a multibyte character inside a value
    can't turn its bytes into replacement characters that no longer match. Only
    prefixes/suffixes of 4+ bytes count: shorter ones are indistinguishable from text."""
    end = len(raw) if end is None else end
    needles = [v.encode() for v in values if v]
    spans = []
    for needle in needles:
        at = raw.find(needle)
        while at >= 0:
            spans.append((at, at + len(needle)))
            at = raw.find(needle, at + 1)
    # Anchored on raw's own edges, where the stream was cut, not on the window's.
    if open_end:
        spans.extend(_open_end_spans(raw, needles, 0, len(raw)))
    if open_start:
        spans.extend(_open_start_spans(raw, needles, 0, len(raw)))
    pieces, pos = [], start
    for lo, hi in sorted(spans):
        if hi <= pos or lo >= end:
            continue
        if lo > pos:
            pieces.append(raw[pos:lo].decode("utf-8", "replace"))
        pieces.append(HIDDEN)
        pos = max(pos, hi)
    if pos < end:
        pieces.append(raw[pos:end].decode("utf-8", "replace"))
    return "".join(pieces)


_MIN_PARTIAL = 4  # bytes


def _open_end_spans(raw: bytes, needles, start: int, end: int):
    """The span of the longest trailing proper prefix of each value in ``raw[start:end]``."""
    if end <= start:
        return
    for needle in needles:
        for k in range(min(len(needle) - 1, end - start), _MIN_PARTIAL - 1, -1):
            if raw[end - 1] == needle[k - 1] and raw.endswith(needle[:k], start, end):
                yield (end - k, end)
                break


def _open_start_spans(raw: bytes, needles, start: int, end: int):
    """The span of the longest leading proper suffix of each value in ``raw[start:end]``."""
    if end <= start:
        return
    for needle in needles:
        for k in range(min(len(needle) - 1, end - start), _MIN_PARTIAL - 1, -1):
            if raw[start] == needle[-k] and raw.startswith(needle[-k:], start, end):
                yield (start, start + k)
                break
