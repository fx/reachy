"""The repository-wide version and the value type that parses it.

Every artifact published from this repository carries the same version string,
so the version is a shared contract rather than a per-component detail. It is
declared here, in source, and the package metadata is derived from it — a
literal in `pyproject.toml` and a literal in the module would be two copies free
to disagree.

Derivation of the version from conventional commits is wired in
`docs/changes/0002-ci-and-hygiene-gates.md`; this module is where that machinery
will write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["VERSION", "SemanticVersion", "__version__"]

# Deliberately a bare assignment: the build backend reads this line with a
# regular expression to derive the distribution version, and a type annotation
# on it stops that regular expression matching. The trailing comment is what
# release automation looks for when it writes the derived version here; it sits
# after the closing quote, where the build backend's expression has already
# stopped reading.
__version__ = "0.1.0"  # x-release-please-version

# MAJOR.MINOR.PATCH with no leading zeros. Pre-release and build metadata are
# deliberately rejected: this repository releases from conventional commits and
# has no use for either, and accepting a suffix now would mean deciding how it
# orders before anything needs it to.
#
# Matched with `fullmatch` rather than anchored with `$`, which also matches
# immediately before a trailing newline and would accept "1.2.3\n".
_PATTERN: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


@dataclass(frozen=True, order=True, slots=True)
class SemanticVersion:
    """A three-part version, comparable and immutable.

    Attributes:
        major: The major component.
        minor: The minor component.
        patch: The patch component.
    """

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        """Reject negative components.

        Raises:
            ValueError: If any component is negative.
        """
        for name in ("major", "minor", "patch"):
            value: int = getattr(self, name)
            if value < 0:
                message = f"{name} must not be negative, got {value}"
                raise ValueError(message)

    @classmethod
    def parse(cls, text: str) -> SemanticVersion:
        """Parse a `MAJOR.MINOR.PATCH` string.

        Args:
            text: The string to parse.

        Returns:
            The parsed version.

        Raises:
            ValueError: If `text` is not exactly three dot-separated components
                of digits without leading zeros.
        """
        match = _PATTERN.fullmatch(text)
        if match is None:
            message = f"not a MAJOR.MINOR.PATCH version: {text!r}"
            raise ValueError(message)
        major, minor, patch = match.groups()
        return cls(int(major), int(minor), int(patch))

    def __str__(self) -> str:
        """Render the version back to its canonical string form."""
        return f"{self.major}.{self.minor}.{self.patch}"


VERSION: Final = SemanticVersion.parse(__version__)
