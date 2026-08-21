"""A real wheel, built in memory, with no application in it at all.

`deploy` is defined over a wheel rather than over the satellite, and this is what
makes that testable today: a valid wheel — a zip with a `.dist-info/METADATA`
carrying a name and a version — assembled from bytes, so the deploy sequence can
be driven end to end before the application it will eventually carry exists.

Built rather than committed, and built with `zipfile` rather than with a build
backend. A wheel *is* a zip with a metadata directory, so assembling one takes no
build, no subprocess and no filesystem: a unit test can ask for a wheel of any
name and version, including two versions of the same distribution, which is
exactly what the version-mismatch scenario needs.

Nothing here is a fixture in pytest's sense; a test calls it and gets bytes.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from base64 import urlsafe_b64encode
from typing import Final

__all__ = ["FIXTURE_DISTRIBUTION", "FIXTURE_VERSION", "fixture_wheel"]

# A distribution name that is obviously this suite's own and is not a real
# project: `deploy` is exercised with no application present, which is the whole
# point of it being defined over a wheel.
FIXTURE_DISTRIBUTION: Final = "reachy-deploy-fixture"
FIXTURE_VERSION: Final = "1.2.3"

_WHEEL_METADATA: Final = "Wheel-Version: 1.0\nGenerator: reachyctl-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def _record_line(name: str, content: bytes) -> str:
    """Render one `RECORD` entry.

    Args:
        name: The archive member's name.
        content: Its bytes.

    Returns:
        The line, with the digest and the size a real wheel carries. Included
        because a wheel this tool refuses for being malformed should be one it
        refuses on purpose, not one it refuses because the fixture was thin.
    """
    digest = urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(content)}\n"


def fixture_wheel(
    distribution: str = FIXTURE_DISTRIBUTION,
    version: str = FIXTURE_VERSION,
    *,
    file_name: str | None = None,
    metadata_name: str | None = None,
    metadata_version: str | None = None,
) -> tuple[str, bytes]:
    """Build a wheel in memory.

    Args:
        distribution: The distribution name.
        version: The version.
        file_name: Override the archive's file name, for the tests that check a
            name and a metadata that disagree.
        metadata_name: Override the name inside the metadata, for the same
            reason.
        metadata_version: Override the version inside the metadata.

    Returns:
        The file name and the archive's bytes.
    """
    module = distribution.replace("-", "_")
    inside = module.replace(".", "_")
    declared_name = distribution if metadata_name is None else metadata_name
    declared_version = version if metadata_version is None else metadata_version
    dist_info = f"{inside}-{version}.dist-info"
    metadata = (
        f"Metadata-Version: 2.1\n"
        f"Name: {declared_name}\n"
        f"Version: {declared_version}\n"
        f"Summary: A wheel with nothing in it, for exercising deployment.\n"
        f"\n"
    ).encode()
    members = {
        f"{inside}/__init__.py": b'"""Nothing. This wheel exists to be deployed."""\n',
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": _WHEEL_METADATA.encode(),
    }
    record = "".join(_record_line(name, body) for name, body in members.items())
    record += f"{dist_info}/RECORD,,\n"
    members[f"{dist_info}/RECORD"] = record.encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    named = (
        file_name if file_name is not None else f"{inside}-{version}-py3-none-any.whl"
    )
    return named, buffer.getvalue()
