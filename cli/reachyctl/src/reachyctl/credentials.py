"""Where the groundstation credential comes from, and where it deliberately does not.

There is no `--credential` option and there will not be one. An argument is
visible in the process list to every user on the machine and lands in the shell
history, and neither is undone by the tool being careful afterwards — so the
credential is read from a file or from the environment, and the option that
exists names a *path*.

Reading the file is the one piece of input this module performs, and it is
reached through a parameter so that the resolution rules can be tested without
one. What is being tested is which source wins, which is where a mistake would
otherwise be silent: an operator who exports the variable and also passes a path
should know which of the two is in effect.

The value comes back as a `Credential`, which renders as a placeholder wherever
it is rendered. It is a `str` for exactly as long as it takes to strip the
newline a text editor put at the end of the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from reachy_session_client import Credential
from reachyctl.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "CREDENTIAL_FILE_VARIABLE",
    "CREDENTIAL_VARIABLE",
    "ENV_PREFIX",
    "URL_VARIABLE",
    "load_credential",
]

ENV_PREFIX: Final = "REACHYCTL_"

CREDENTIAL_VARIABLE: Final = f"{ENV_PREFIX}CREDENTIAL"
CREDENTIAL_FILE_VARIABLE: Final = f"{ENV_PREFIX}CREDENTIAL_FILE"
URL_VARIABLE: Final = f"{ENV_PREFIX}GROUNDSTATION_URL"


def _read(path: Path) -> str:
    """Read a credential file.

    Nothing but the read: what a failure means is `load_credential`'s to decide,
    so that the decision holds for whatever reader it was given rather than only
    for this one.

    Args:
        path: Where the credential is kept.

    Returns:
        The file's contents.

    Raises:
        OSError: If the file cannot be read.
    """
    return path.read_text(encoding="utf-8")


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def load_credential(
    environ: Mapping[str, str],
    credential_file: Path | None = None,
    read: Callable[[Path], str] = _read,
) -> Credential:
    """Resolve the credential to present to the groundstation.

    The order is most explicit first: the path given on the command line, then
    the path in the environment, then the value in the environment. A file is
    preferred to a value because a file has permissions and a variable is
    inherited by every process the shell starts.

    Args:
        environ: The environment to read.
        credential_file: A path given on the command line, if there was one.
        read: How to read a file. Injected so the resolution rules can be
            exercised without performing any input.

    Returns:
        The credential, in a type that will not print itself.

    Raises:
        ConfigurationError: If no source names one, if the source names an empty
            one, or if the file cannot be read. None of the three messages
            quotes anything it read — the reason a file could not be opened is
            the operating system's and is safe; its contents are not.
    """
    path = credential_file
    if path is None and (from_environment := environ.get(CREDENTIAL_FILE_VARIABLE)):
        path = Path(from_environment)

    if path is not None:
        try:
            content = read(path)
        except OSError as error:
            message = f"the credential file {path} could not be read: {error.strerror}"
            raise ConfigurationError(message) from error
        return _wrap(content.strip(), f"the credential file {path} is empty")

    value = environ.get(CREDENTIAL_VARIABLE, "")
    return _wrap(
        value.strip(),
        f"no groundstation credential: set {CREDENTIAL_VARIABLE}, set "
        f"{CREDENTIAL_FILE_VARIABLE}, or pass --credential-file. There is no "
        f"option that takes the credential itself, because an argument is "
        f"visible in the process list and lands in the shell history.",
    )


def _wrap(value: str, complaint: str) -> Credential:
    """Hold a resolved value, or explain that there was not one.

    Args:
        value: What the source produced, already stripped.
        complaint: What to say when it produced nothing.

    Returns:
        The credential.

    Raises:
        ConfigurationError: If the value is empty.
    """
    if not value:
        raise ConfigurationError(complaint)
    return Credential(value)
