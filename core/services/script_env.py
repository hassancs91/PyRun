"""
Per-script environment variables (2.5 Stage 1, ticket T09).

``Script.env_vars`` holds plain, non-secret settings as ``{NAME: value}``: a
region, a feature flag, a base URL. They are stored and shown as typed and are
never masked, so keys and tokens belong in Secrets.

The executor exports them into every run of the script. Precedence, lowest
first: the system environment < env vars < inputs (``INPUT_<NAME>``) <
secrets. A secret with the same name therefore always wins, and an input wins
over an env var named ``INPUT_<NAME>``. PyRun's own variables (the run ids,
``WEBHOOK_*``, ``INBOUND_*``, the AI provider's) are set after all of them.

- ``check_env_vars`` validates what the editor posts and returns the dict to
  store, or raises ``EnvVarsError`` listing every problem.
- ``usable_env_vars`` is the tolerant reader for the executor and for backup
  restore: it keeps the entries ``check_env_vars`` would accept and drops the
  rest, so a hand-edited row or backup can't break a run.
- ``secret_names_visible_to`` / ``editor_secret_names`` name the secrets a run
  of the script gets (names only, nothing is decrypted), for the editor's
  "a secret with this name wins" warning.
"""

import re

# Same width as Secret.key, so any secret name can be shadowed (and warned on).
NAME_MAX_LENGTH = 100
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAME_RULE = (
    "a name can only hold letters, digits and underscores, and can't start "
    "with a digit"
)
MAX_VARS = 100
# Names + values, UTF-8. Far below Linux's 128 KB limit on one variable.
MAX_BYTES = 64 * 1024

# Names a script must not replace: the process would stop finding Python, its
# packages or its home. Secret keys share the list (SecretForm.clean_key).
RESERVED_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "PWD",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }
)
# PyRun sets its own PYRUNNER_* variables after these, so an env var with the
# prefix would be silently replaced (or, worse, read by a helper as PyRun's).
RESERVED_PREFIX = "PYRUNNER_"


class EnvVarsError(ValueError):
    """Env vars that can't be saved. ``messages`` lists each problem."""

    def __init__(self, messages):
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


def name_problem(name):
    """Why ``name`` can't be an env var name, or None when it can."""
    if len(name) > NAME_MAX_LENGTH:
        return f"{name[:24]}…: names are at most {NAME_MAX_LENGTH} characters."
    if not NAME_RE.match(name):
        return f"{name}: {NAME_RULE}."
    upper = name.upper()
    if upper in RESERVED_NAMES:
        return f"{name} is a reserved environment variable name."
    if upper.startswith(RESERVED_PREFIX):
        return f"{name}: names starting with {RESERVED_PREFIX} are reserved for PyRun."
    return None


def _value_problem(name, value):
    if "\x00" in value:
        return f"{name}: the value can't contain a NUL character."
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return f"{name}: the value isn't valid text."
    return None


def _size(name, value):
    return len(name.encode("utf-8")) + len(value.encode("utf-8"))


def check_env_vars(value):
    """Validate env vars given as a dict or as ``(name, value)`` pairs.

    Names are stripped; values are kept exactly as typed (spaces included). A
    row with neither a name nor a value is dropped, so the editor's spare empty
    row never counts. Returns the ``{NAME: value}`` dict to store.
    """
    if value is None:
        return {}
    pairs = list(value.items()) if isinstance(value, dict) else list(value)
    result = {}
    errors = []
    for raw_name, raw_value in pairs:
        if raw_value is None:
            raw_value = ""
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            errors.append("Env var names and values must be text.")
            continue
        name = raw_name.strip()
        if not name and not raw_value:
            continue
        if not name:
            errors.append("A variable with a value needs a name.")
            continue
        problem = name_problem(name)
        if problem is None and name in result:
            problem = f"{name} is listed twice."
        if problem is None:
            problem = _value_problem(name, raw_value)
        if problem:
            errors.append(problem)
            continue
        result[name] = raw_value
    if len(result) > MAX_VARS:
        errors.append(f"A script can have at most {MAX_VARS} env vars.")
    total = sum(_size(k, v) for k, v in result.items())
    if total > MAX_BYTES:
        errors.append(
            f"Env vars are {total:,} bytes in all; the limit is {MAX_BYTES // 1024} KB."
        )
    if errors:
        raise EnvVarsError(errors)
    return result


def usable_env_vars(value):
    """The entries of a stored ``env_vars`` that ``check_env_vars`` would accept.

    Anything else (not a dict, a bad or reserved name, a non-text value) is
    dropped, and so is whatever goes past the count or size limit.
    """
    if not isinstance(value, dict):
        return {}
    result = {}
    total = 0
    for name, val in value.items():
        if not isinstance(name, str) or not isinstance(val, str) or name != name.strip():
            continue
        if not name or name_problem(name) or _value_problem(name, val):
            continue
        size = _size(name, val)
        if len(result) >= MAX_VARS or total + size > MAX_BYTES:
            break
        result[name] = val
        total += size
    return result


def secret_names_visible_to(script):
    """Names of the secrets a run of ``script`` gets, as saved.

    The same selection ``resolve_secrets_for_run`` makes (workspace, injection
    mode, attached and plugin-owned secrets), without decrypting anything.
    Empty when encryption isn't configured: no secret is injected then.
    """
    from core.executor import secrets_in_scope
    from core.services import EncryptionService

    if not EncryptionService.is_configured():
        return set()
    return {
        s.get_clean_name()
        for s in secrets_in_scope(
            script.workspace_id,
            mode=script.injection_mode,
            owner_plugin=script.owner_plugin,
            script_id=script.pk,
        )
    }


def editor_secret_names(script, workspace_id):
    """What the editor's live warning needs, names only.

    ``all``: the secrets *All* mode injects. ``owned``: the script's plugin's
    own secrets, which *Selected* mode adds to the attached ones (the editor
    reads those from its chips). ``visible``: what a run gets as the script is
    saved now (for a new script, what *All* gives).
    """
    from core.executor import secrets_in_scope
    from core.services import EncryptionService

    if not EncryptionService.is_configured():
        return {"all": [], "owned": [], "visible": []}
    all_names = {s.get_clean_name() for s in secrets_in_scope(workspace_id)}
    owner = getattr(script, "owner_plugin", None) if script is not None else None
    owned = set()
    if owner:
        owned = {
            s.get_clean_name()
            for s in secrets_in_scope(workspace_id, mode="selected", owner_plugin=owner)
        }
    if script is not None and not script._state.adding:
        visible = secret_names_visible_to(script)
    else:
        visible = all_names
    return {"all": sorted(all_names), "owned": sorted(owned), "visible": sorted(visible)}


def env_var_collisions(script):
    """Env var names of ``script`` that a secret it gets also has (the secret wins)."""
    names = set(usable_env_vars(script.env_vars))
    if not names:
        return []
    return sorted(names & secret_names_visible_to(script))
