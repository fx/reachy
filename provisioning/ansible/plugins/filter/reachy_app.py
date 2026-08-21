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

A wheel's file name is `{distribution}-{version}(-{build})?-{tags}.whl`, and
`wheel_release` reads it — but only far enough to answer "is this a wheel at
all", cheaply and locally, before anything is transferred to a robot. **It is
deliberately not the authority on what installing the wheel would put there.** A
file name is a claim; `.dist-info/METADATA` is what pip records, and what the
daemon's own interpreter reports afterwards. Deciding whether to install from one
and verifying against the other would be answering two different questions, which
is exactly the failure this whole stack is written against — a package that
installed successfully into an environment the running daemon was not using.
`reachyctl deploy` reads the wheel rather than its name for the same reason.

The distribution part *is* escaped — PEP 427 replaces every run of unsafe
characters with an underscore, so `example.tool` and `example-tool` are both
`example_tool` — which is why the comparison folds both sides through PEP 503
normalisation. The version part is not usefully escaped: `packaging` refuses a
wheel whose version segment is not a valid PEP 440 version, so a local version
appears verbatim as `1.0+local` and no tool produces `1.0_local`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["FilterModule", "distribution_name", "interpreter", "wheel_release"]

# PEP 503: a distribution name is compared with runs of `-`, `_` and `.` folded
# to a single `-`, lowercased. A wheel's file name carries the *escaped* form,
# where every unsafe character became an underscore, so `example.tool` and
# `example-tool` both arrive as `example_tool` and neither is spelled the way
# the declaration spells it. Comparing the two normalised is what makes the role
# accept a wheel for the distribution it was told to install rather than
# refusing it over punctuation.
_UNNORMALISED: Final = re.compile(r"[-_.]+")

# systemd renders a command as `{ path=/usr/bin/x ; argv[]=... ; ... }`, one such
# block per `ExecStart=` the unit declares. The first block's path is the
# interpreter the daemon runs.
_EXEC_PATH: Final = re.compile(r"path=(\S+)")


def distribution_name(name: str) -> str:
    """Fold a distribution name the way everything that compares them folds it.

    Args:
        name: The name as a declaration or a wheel's file name spells it.

    Returns:
        The PEP 503 normalised form, which is also what
        `importlib.metadata.version` and `pip` answer to.
    """
    return _UNNORMALISED.sub("-", name).lower()


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
        A record carrying `ok`, the `distribution` in its PEP 503 normalised
        form — which is what `importlib.metadata` and `pip` answer to — the
        `version` exactly as the file name spells it, and a `complaint` when
        the name is not a wheel's. The version is reported and not trusted: see
        the module documentation on why the role reads the wheel's own metadata
        for every decision it makes.
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
        "distribution": distribution_name(match.group("distribution")),
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
            "reachy_distribution_name": distribution_name,
            "reachy_interpreter": interpreter,
            "reachy_wheel_release": wheel_release,
        }
