"""Where Snowflake credentials come from (v1.45).

Until now this kit read a bespoke ``key=value`` file at ``~/.ssh/sf_config``
— a shape inherited from an internal tool and shared with nothing. That was
fine when three people ran it and actively hostile the moment forty
attendees arrive, because most of them already have a working Snowflake
connection on disk and none of them have that file.

So the source of truth is now the **standard** Snowflake client config:
``connections.toml`` / ``config.toml`` under ``~/.snowflake``, the same
file the Snowflake CLI, the VS Code extension and the Python connector
read. Someone who has ever run ``snow connection add`` is already
configured, and a connection they fix here is fixed for every Snowflake
tool at once rather than only for this game.

Resolution order, matching the CLI's documented behaviour so we never
disagree with ``snow connection test``:

  1. ``SNOWFLAKE_HOME`` if set, else ``~/.snowflake`` if it exists, else
     the OS-specific fallback directory.
  2. Within that directory ``connections.toml`` wins over ``config.toml``
     when both exist — that is the CLI's own precedence, not ours.
  3. The section is ``SOC_SNOWFLAKE_CONNECTION`` (ours, so a player can
     point the game at one connection while their shell default points
     elsewhere), else ``SNOWFLAKE_DEFAULT_CONNECTION_NAME``, else the
     file's own ``default_connection_name``, else a lone section if there
     is exactly one, else ``[default]``.

The legacy ``~/.ssh/sf_config`` is still read, last, and only when the
standard file yields nothing. It is deprecated rather than deleted
because the machines that run the event already depend on it, and having
an install break the week of the hackathon is a worse outcome than
carrying twenty lines of compatibility.

Everything downstream consumes :func:`load_props`, which returns the same
flat lowercase dict both shapes used to produce, so call sites did not
have to learn TOML.
"""

from __future__ import annotations

import os
import sys

try:
    import tomllib  # stdlib on 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # 3.10 backport
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "load_props",
    "resolve_source",
    "LEGACY_CONFIG_ENV",
    "CONNECTION_ENV",
    "legacy_config_path",
]

#: Ours: point the game at one connection without moving a shell default.
CONNECTION_ENV = "SOC_SNOWFLAKE_CONNECTION"
#: Snowflake's own: honoured so we agree with `snow` and the connector.
STD_CONNECTION_ENV = "SNOWFLAKE_DEFAULT_CONNECTION_NAME"
#: Snowflake's own: relocates the whole config directory.
SNOWFLAKE_HOME_ENV = "SNOWFLAKE_HOME"
#: Legacy escape hatch, kept so existing setups keep working.
LEGACY_CONFIG_ENV = "SF_CONFIG_FILE"

_LEGACY_DEFAULT = "~/.ssh/sf_config"


def legacy_config_path() -> str:
    return os.environ.get(LEGACY_CONFIG_ENV, os.path.expanduser(_LEGACY_DEFAULT))


def _config_dir() -> Optional[Path]:
    """The directory the Snowflake clients agree on, or None.

    Mirrors the CLI: an explicit ``SNOWFLAKE_HOME`` beats everything, a
    real ``~/.snowflake`` beats the OS fallback, and the OS fallback is
    only consulted when ``~/.snowflake`` does not exist at all.
    """
    home = os.environ.get(SNOWFLAKE_HOME_ENV)
    if home:
        p = Path(os.path.expanduser(home))
        return p if p.is_dir() else None

    dotdir = Path.home() / ".snowflake"
    if dotdir.is_dir():
        return dotdir

    if sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "snowflake"
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(base) / "snowflake"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        p = Path(base) / "snowflake"
    return p if p.is_dir() else None


def _read_toml(path: Path) -> Dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:
        # A malformed config is the user's to fix with `snow connection
        # test`, which says far more about it than we could. Treat it as
        # absent so we fall through rather than crash a game boot.
        return {}


def _sections(doc: Dict[str, Any], *, is_connections_file: bool) -> Dict[str, Any]:
    """Connection tables, whichever of the two layouts the file uses.

    ``connections.toml`` puts each connection at the top level;
    ``config.toml`` nests them under ``[connections]``.
    """
    if is_connections_file:
        return {k: v for k, v in doc.items() if isinstance(v, dict)}
    conns = doc.get("connections")
    return {k: v for k, v in conns.items() if isinstance(v, dict)} if isinstance(conns, dict) else {}


def _pick_name(
    sections: Dict[str, Any], doc: Dict[str, Any]
) -> Tuple[Optional[str], str]:
    """``(section_name, complaint)``.

    A name someone typed by hand and got wrong must never resolve
    silently to a different account — that is how you spend an afternoon
    debugging permissions on a warehouse you were never pointed at. An
    asked-for-but-absent connection is reported, and the complaint
    survives all the way to ``soc doctor``.
    """
    asked = os.environ.get(CONNECTION_ENV) or os.environ.get(STD_CONNECTION_ENV)
    if asked:
        if asked in sections:
            return asked, ""
        known = ", ".join(sorted(sections)) or "none"
        return None, f"no connection named {asked!r} (found: {known})"

    default = doc.get("default_connection_name")
    if default and default in sections:
        return str(default), ""
    if len(sections) == 1:
        return next(iter(sections)), ""
    if "default" in sections:
        return "default", ""
    return None, ""


def _normalise(raw: Dict[str, Any]) -> Dict[str, str]:
    """Flatten a TOML connection to the flat dict the call sites expect.

    The one piece of real translation is the token. A PAT can legally sit
    in ``token``, or in ``password`` alongside a PAT authenticator, and
    the old file called it ``pat``. Downstream only ever wants "the
    bearer token, if there is one", so settle it here rather than in
    three places.
    """
    props: Dict[str, str] = {
        k.lower(): ("" if v is None else str(v))
        for k, v in raw.items()
        if not isinstance(v, (dict, list))
    }

    auth = props.get("authenticator", "").strip().lower()
    pat = props.get("token") or props.get("pat") or props.get("access_token") or ""
    if not pat and auth in {
        "programmatic_access_token",
        "oauth",
    }:
        pat = props.get("password", "")
    if pat:
        props["pat"] = pat

    if props.get("private_key_file"):
        props["private_key_file"] = os.path.expanduser(props["private_key_file"])
    return props


def _load_legacy(path: str) -> Dict[str, str]:
    props: Dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                props[k.lower().strip()] = v.strip()
    except OSError:
        return {}
    if props.get("private_key_file"):
        props["private_key_file"] = os.path.expanduser(props["private_key_file"])
    return props


def resolve_source() -> Tuple[Dict[str, str], str]:
    """``(props, human_description_of_where_they_came_from)``.

    The description exists for ``soc doctor``: "no credentials" and "the
    wrong connection out of the right file" are very different problems
    and used to look identical.
    """
    # An explicitly exported SF_CONFIG_FILE is a deliberate act, so it
    # outranks the standard store. Merely *having* a legacy file at the
    # default path does not — that is the deprecated case, handled last.
    explicit_legacy = os.environ.get(LEGACY_CONFIG_ENV)
    if explicit_legacy:
        path = os.path.expanduser(explicit_legacy)
        props = _load_legacy(path)
        if props:
            return props, f"{path} (SF_CONFIG_FILE)"
        return {}, f"SF_CONFIG_FILE points at {path}, which is missing or empty"

    directory = _config_dir()
    if directory is not None:
        for fname in ("connections.toml", "config.toml"):
            path = directory / fname
            if not path.is_file():
                continue
            doc = _read_toml(path)
            sections = _sections(doc, is_connections_file=(fname == "connections.toml"))
            name, complaint = _pick_name(sections, doc)
            if complaint:
                # Stop here rather than fall through to another file or to
                # the legacy path: they asked for something specific.
                return {}, f"{path}: {complaint}"
            if name:
                return _normalise(sections[name]), f"{path} [{name}]"

    legacy = legacy_config_path()
    if os.path.exists(legacy):
        props = _load_legacy(legacy)
        if props:
            return props, f"{legacy} (legacy sf_config — see docs/SNOWFLAKE_SETUP.md)"

    return {}, "no Snowflake connection configured"


def resolve_cortex() -> Tuple[str, str, str]:
    """``(account, pat, where)`` for the Cortex REST path.

    The PAT gets its own resolution because it is the one credential that
    routinely lives somewhere other than the connection you persist with.
    A key-pair connection in ``connections.toml`` is complete and correct
    and still carries no bearer token, so insisting the token come from
    the resolved connection would break every setup that keeps the two
    apart — including the one this kit was developed on.

    Account and token travel together on purpose: a PAT is scoped to the
    account that issued it, and pairing a token from one file with an
    account from another fails as a 401 that tells you nothing.

    Order: the environment (a token that is not on disk is the better
    habit), then the resolved connection, then any legacy sf_config.
    """
    props, where = resolve_source()

    env = os.environ.get("SNOWFLAKE_PAT", "").strip()
    if env:
        return props.get("account", ""), env, "SNOWFLAKE_PAT env var"

    if props.get("pat"):
        return props.get("account", ""), props["pat"], where

    legacy = legacy_config_path()
    if os.path.exists(legacy):
        lp = _load_legacy(legacy)
        token = lp.get("pat") or lp.get("access_token") or ""
        if token:
            return (
                lp.get("account", "") or props.get("account", ""),
                token,
                f"{legacy} (legacy sf_config)",
            )

    return props.get("account", ""), "", where


def load_props(path: Optional[str] = None) -> Dict[str, str]:
    """Credentials as a flat dict, standard file first.

    ``path`` forces one specific legacy file and is only honoured for the
    old call sites that passed one; everything new should pass nothing
    and let resolution happen.
    """
    if path:
        props = _load_legacy(path)
        if props:
            return props
    return resolve_source()[0]
