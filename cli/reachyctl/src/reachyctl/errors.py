"""What a command raises when it cannot go on, and what that costs at the shell.

An error carries its exit status, so the mapping from "what went wrong" to "what
the shell sees" is decided where the failure is understood rather than in a
handler that has only a message to go on. See `reachyctl.exits` for what the
statuses mean.
"""

from __future__ import annotations

from reachyctl.exits import ExitCode

__all__ = ["CommandError", "ConfigurationError", "UnreachableError"]


class CommandError(Exception):
    """A command cannot do what it was asked, and knows what that is worth.

    Attributes:
        exit_code: What the process should exit with.
    """

    exit_code: ExitCode = ExitCode.FAILURE


class ConfigurationError(CommandError):
    """The command could not start with what it was given.

    A missing credential, a directory holding no frames, a camera that is not
    there. Nothing was asked of the robot, so this is not a diagnosis and a
    script must not read it as one.
    """

    exit_code: ExitCode = ExitCode.CONFIGURATION


class UnreachableError(CommandError):
    """The other end was not there, refused the session, or broke it.

    Also not a diagnosis: nothing has been learned about the robot, so a
    monitor reading this as "unhealthy" would be reporting on its own network.
    """

    exit_code: ExitCode = ExitCode.UNREACHABLE
