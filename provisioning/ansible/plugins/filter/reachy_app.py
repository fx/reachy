"""What the roles need to know about the robot's application environment.

Two questions: which interpreter that environment is, and what installing a
given wheel into it would put there.

The role installs **a wheel from a configured source** — a release artifact
reached by URL, or a path on the machine running the playbook. It does not name
the satellite anywhere, and that is not a placeholder waiting for change 0013: a
robot runs one application today and the vendor's daemon will run others, and a
role that hard-coded the distribution would have to be edited to install any of
them. `reachyctl deploy` made the same choice for the same reason — it is defined
over a wheel, not over the satellite.

A wheel's file name is `{distribution}-{version}(-{build})?-{tags}.whl`, which is
enough to decide **whether** to install. It is deliberately not enough to decide
that an install worked: a file name is a claim, and the failure this whole stack
is written against is a package that installed successfully into an environment
the running daemon was not using. So the role asks the daemon's own interpreter
what it now holds, and this module only supplies the version to compare that
answer against.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["FilterModule", "interpreter", "wheel_release"]

# systemd renders a command as `{ path=/usr/bin/x ; argv[]=... ; ... }`, one such
# block per `ExecStart=` the unit declares. The first block's path is the
# interpreter the daemon runs.
_EXEC_PATH: Final = re.compile(r"path=(\S+)")


def interpreter(exec_start: str, fallback: str) -> str:
    """Say which interpreter the daemon runs, asking systemd rather than assuming.

    Installing into a configured path and then verifying against the same
    configured path agrees with itself no matter which environment the daemon is
    really using, which is the shape of the original failure rather than a check
    on it. So the unit is asked, and the configured path is used only when the
    unit declares no command at all — a unit that is not installed reports an
    empty property — never to paper over a command that failed.

    Args:
        exec_start: The unit's `ExecStart` property, as `systemctl show
            --value` printed it.
        fallback: The configured interpreter, for a unit that declares none.

    Returns:
        The interpreter's path.
    """
    found = _EXEC_PATH.search(exec_start)
    return found.group(1) if found is not None else fallback


# PEP 427's file-name grammar, as far as this needs it: the distribution, the
# version, an optional build tag, and the three compatibility tags. The
# distribution and the version are escaped forms — runs of unsafe characters
# become an underscore — so they are matched as "anything but a hyphen" rather
# than by a name pattern this would then have to keep in step with packaging's.
_WHEEL_NAME: Final = re.compile(
    r"^(?P<distribution>[^-]+)-(?P<version>[^-]+)"
    r"(?:-(?P<build>[0-9][^-]*))?"
    r"-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$",
)


def wheel_release(file_name: str) -> dict[str, Any]:
    """Read the distribution and version a wheel's file name claims.

    Total rather than raising, for the reason `reachy_managed.region_state` is:
    the caller is a playbook, and a record lets the role fail with a sentence
    instead of a templating traceback.

    Args:
        file_name: The wheel's file name, with no directory part.

    Returns:
        A record carrying `ok`, the `distribution` in its normalised form —
        underscores folded to hyphens and lowercased, which is what
        `importlib.metadata` answers to — the `version`, and a `complaint` when
        the name is not a wheel's.
    """
    match = _WHEEL_NAME.match(file_name)
    if match is None:
        return {
            "ok": False,
            "distribution": "",
            "version": "",
            "complaint": (
                f"{file_name!r} is not a wheel file name; a wheel is named "
                f"distribution-version(-build)-python-abi-platform.whl"
            ),
        }
    return {
        "ok": True,
        "distribution": match.group("distribution").replace("_", "-").lower(),
        "version": match.group("version"),
        "complaint": "",
    }


class FilterModule:
    """Expose this module's functions to Jinja, which is how the roles reach them."""

    def filters(self) -> dict[str, Callable[..., Any]]:
        """List the filters this plugin provides.

        Returns:
            The filters by the name a template writes.
        """
        return {
            "reachy_interpreter": interpreter,
            "reachy_wheel_release": wheel_release,
        }
