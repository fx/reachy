"""Where the credential comes from, and what happens when it does not.

Which source wins is where a mistake is silent: an operator who exports the
variable and also passes a path gets one of the two, and finding out which by
watching a session fail is not finding out. So the order is asserted rather than
documented.

Reading is reached through a parameter, so these are unit tests that perform no
input. The last one exercises the default reader instead, over `pyfakefs`'s
in-memory filesystem — which is what that development dependency is here for,
and why it stays a unit test and carries no `filesystem` marker: nothing real is
read.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_support import CREDENTIAL

from reachyctl.credentials import (
    CREDENTIAL_FILE_VARIABLE,
    CREDENTIAL_VARIABLE,
    load_credential,
)
from reachyctl.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyfakefs.fake_filesystem import FakeFilesystem

FROM_FILE: Final = "example-credential-from-a-file"
FROM_OPTION: Final = "example-credential-from-the-option"

OPTION_PATH: Final = Path("/etc/reachyctl/option.secret")
ENVIRONMENT_PATH: Final = Path("/etc/reachyctl/environment.secret")


def reading(**files: str) -> Callable[[Path], str]:
    """Build a reader that answers from a mapping rather than from a disk.

    Args:
        files: What each path holds, keyed by the path as text.

    Returns:
        Something to pass as `read`.
    """

    def read(path: Path) -> str:
        """Answer for one path.

        Args:
            path: What was asked for.

        Returns:
            What that path holds.

        Raises:
            AssertionError: If a path nobody prepared was read, which means the
                resolution went somewhere the test did not describe.
            OSError: When the prepared answer is the marker for one.
        """
        try:
            content = files[str(path)]
        except KeyError:
            message = f"the credential was read from an unexpected path: {path}"
            raise AssertionError(message) from None
        if content == "<unreadable>":
            raise OSError(13, "Permission denied")
        return content

    return read


def test_the_option_wins_over_both_environment_variables() -> None:
    """Most explicit first, so what an operator typed is what is used."""
    held = load_credential(
        {
            CREDENTIAL_VARIABLE: CREDENTIAL,
            CREDENTIAL_FILE_VARIABLE: str(ENVIRONMENT_PATH),
        },
        OPTION_PATH,
        reading(**{str(OPTION_PATH): FROM_OPTION}),
    )

    assert held.reveal() == FROM_OPTION


def test_a_path_in_the_environment_wins_over_a_value_in_it() -> None:
    """A file has permissions; a variable is inherited by every child process."""
    held = load_credential(
        {
            CREDENTIAL_VARIABLE: CREDENTIAL,
            CREDENTIAL_FILE_VARIABLE: str(ENVIRONMENT_PATH),
        },
        None,
        reading(**{str(ENVIRONMENT_PATH): FROM_FILE}),
    )

    assert held.reveal() == FROM_FILE


def test_a_value_in_the_environment_is_the_last_resort() -> None:
    """Which is what makes a one-off run possible without writing a file."""
    held = load_credential({CREDENTIAL_VARIABLE: CREDENTIAL})

    assert held.reveal() == CREDENTIAL


def test_the_newline_a_text_editor_left_on_the_end_is_not_the_credential() -> None:
    """A trailing newline is the commonest reason a correct credential is refused."""
    held = load_credential(
        {},
        OPTION_PATH,
        reading(**{str(OPTION_PATH): f"{FROM_FILE}\n"}),
    )

    assert held.reveal() == FROM_FILE


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def test_no_source_at_all_says_what_to_set_and_why_there_is_no_option() -> None:
    """The absent option is a decision, so the message says so rather than nothing."""
    with pytest.raises(ConfigurationError) as raised:
        load_credential({})

    message = str(raised.value)
    assert CREDENTIAL_VARIABLE in message
    assert CREDENTIAL_FILE_VARIABLE in message
    assert "process list" in message


def test_an_empty_file_is_not_a_credential() -> None:
    """Nothing configured fails here rather than at an authentication check."""
    with pytest.raises(ConfigurationError, match="is empty"):
        load_credential(
            {},
            OPTION_PATH,
            reading(**{str(OPTION_PATH): "   \n"}),
        )


def test_a_file_that_cannot_be_read_reports_why_and_not_what() -> None:
    """The reason is the operating system's; the contents are not quoted."""
    with pytest.raises(ConfigurationError) as raised:
        load_credential(
            {},
            OPTION_PATH,
            reading(**{str(OPTION_PATH): "<unreadable>"}),
        )

    assert "Permission denied" in str(raised.value)


def test_the_real_reader_reads_the_file_the_option_names(fs: FakeFilesystem) -> None:
    """The default reader is what production uses, so it is exercised too.

    Args:
        fs: An in-memory filesystem, so this performs no input on a real one.
    """
    fs.create_file(str(OPTION_PATH), contents=f"{FROM_FILE}\n")

    held = load_credential({}, OPTION_PATH)

    assert held.reveal() == FROM_FILE
