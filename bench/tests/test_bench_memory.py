"""Reading a resident set out of a process status dump.

The measurement itself starts a real service and is excluded from coverage where
it is defined; what is tested here is the part with logic in it — turning
`/proc/<pid>/status` into a number, and refusing to turn a platform that
publishes no such thing into a zero, because a footprint of zero passes every
tolerance there is.

No test here performs any input or output: the dump is a string.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest

from reachy_bench.memory import read_resident_mebibytes

# A trimmed `/proc/<pid>/status`. `VmRSS` is in kibibytes, and the lines around
# it are the ones a careless reader would pick up instead.
_STATUS = """Name:\tpython3
State:\tS (sleeping)
VmPeak:\t  900000 kB
VmSize:\t  850000 kB
VmRSS:\t  122880 kB
VmData:\t  400000 kB
Threads:\t9
"""


def test_the_resident_set_is_read_in_mebibytes() -> None:
    """The dump is in kibibytes and the recorded figure is in mebibytes."""
    assert read_resident_mebibytes(_STATUS) == 120


def test_the_peak_and_the_virtual_size_are_not_mistaken_for_it() -> None:
    """Three lines start with `Vm` and only one of them is the resident set."""
    assert read_resident_mebibytes(_STATUS) != 900000 // 1024
    assert read_resident_mebibytes(_STATUS) != 850000 // 1024


@pytest.mark.parametrize(
    "status",
    ["", "Name:\tpython3\n", "VmRSS:\tplenty\n"],
)
def test_a_dump_with_no_readable_resident_set_is_refused(status: str) -> None:
    """A footprint of zero would pass every tolerance there is.

    Args:
        status: A dump that does not carry a readable `VmRSS`.
    """
    with pytest.raises(ValueError, match="no VmRSS"):
        read_resident_mebibytes(status)
