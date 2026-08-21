"""What this tool's exit status means.

reachyctl REQ-058 asks that the process exit status reflect whether what was
asked for succeeded, and this is the whole of that convention. It lands here,
with the first command, rather than being decided again per command: an operator
scripting `doctor` on a timer and `deploy` in a pipeline should not have to
learn two schemes, and a status invented later would either collide with one of
these or mean something subtly different.

The distinction that earns its keep is between the three ways of not succeeding.
A command that ran and reported a negative answer (`FAILURE`) is what a health
check returns when something is wrong, and a script reacts to it. A command that
could not run because of what it was given (`CONFIGURATION`) or because the
other end was not there (`UNREACHABLE`) has reported nothing about the robot at
all, and a script that treated those as "unhealthy" would page somebody about a
missing credential.

`USAGE` is Click's own status for a bad invocation and is listed here so that
nothing else claims the number.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["ExitCode"]


class ExitCode(IntEnum):
    """The statuses every `reachyctl` command exits with.

    Attributes:
        OK: What was asked for succeeded.
        FAILURE: The command ran and its answer was negative.
        USAGE: The invocation itself was wrong. Click exits with this.
        CONFIGURATION: The command could not start with what it was given —
            a missing credential, a directory with no frames in it, an address
            that is not a session URL.
        UNREACHABLE: The groundstation could not be reached, refused the
            session, or broke it.
    """

    OK = 0
    FAILURE = 1
    USAGE = 2
    CONFIGURATION = 3
    UNREACHABLE = 4
