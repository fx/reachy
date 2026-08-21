"""The committed baseline: recorded numbers, read as data rather than prose.

The benchmarks spec records the predecessor's measurements in a table, and a
table is not something a gate can read. This module is the same numbers as data,
plus the machinery for the two kinds of comparison the suite makes.

**Two kinds, because two kinds of quantity.**

A *size* does not depend on the machine that measured it. An image is 437 MiB on
a laptop and 437 MiB on a runner, so `artifacts` is one flat set of entries and
REQ-073's growth gate needs no notion of where the measurement came from.

A *timing* depends entirely on it. Continuous integration hardware is not
deployment hardware and varies between runs, which is why REQ-071 is relative
and why the spec says the baseline is "measured on the same class of runner".
So timings live under `profiles`, keyed by the host class in
`reachy_bench.context`, and a run is compared against the profile matching the
machine it ran on. A profile for a class nobody has measured yet is absent, and
`reachy_bench.compare` reports that as unbaselined rather than as passing.

**The predecessor's own numbers are a profile too, and it is not gated.** Its
host no longer exists and nothing this repository runs can be compared against
it by a machine. It is committed because it is what the rebuild is accountable
to — the whole reason the benchmarks spec exists — and it is what the pull
request reports its measurements beside.

**Updating any of this is a pull request.** That is REQ-071's scenario and the
change document's decision: a regression should require somebody to say so in a
review rather than being absorbed by an automatically refreshed baseline. So
the file is committed, the numbers in it are readable in a diff, and
`reachy_bench.cli record` prints exactly the block to paste rather than editing
anything itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Self

from reachy_bench.result import Unit
from reachy_bench.stats import finite

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from reachy_bench.result import RunResult

__all__ = [
    "BASELINE_FILENAME",
    "PREDECESSOR_PROFILE",
    "SCHEMA_VERSION",
    "Baseline",
    "BaselineEntry",
    "Profile",
    "profile_document",
]

# The document's shape. Changed when an entry's meaning changes, not when one is
# added.
SCHEMA_VERSION: Final = 1

# Where the committed baseline lives, relative to the `bench` member.
BASELINE_FILENAME: Final = "baseline.json"

# The profile holding the predecessor's hand-measured figures. Never gated: the
# machine is gone, so a comparison against it would be a comparison between two
# unrelated hosts dressed as a regression check.
PREDECESSOR_PROFILE: Final = "predecessor"

# What a tolerance defaults to when the document does not state one for a unit.
# Deliberately tight rather than lenient: an unstated tolerance should show up
# as a gate that fires, not as one that never does.
_DEFAULT_TOLERANCE: Final = 0.05


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineEntry:
    """One recorded figure.

    Attributes:
        value: The number, in the unit below.
        unit: What it is counted in. It must match the measurement's own unit —
            a millisecond baseline compared against a byte measurement is a
            comparison of two different things that would still produce a
            ratio.
        tolerance: How far this particular figure may drift, when the unit's
            own tolerance is wrong for it. `None` means the unit's. It exists
            because run-to-run variance is not uniform across measurements: a
            four-microsecond stage moves by half its value on clock granularity
            alone, and an inference sweep point above the knee contends for
            cores in a way the points below it do not. A widened tolerance is
            as visible in a diff as a changed number, and the note beside it is
            where the observation that justifies it is written down.
        note: Where the figure came from and anything a reviewer needs in order
            to judge a change to it.
    """

    value: float
    unit: Unit
    tolerance: float | None = None
    note: str = ""

    def as_document(self) -> dict[str, Any]:
        """Render the entry.

        Returns:
            A JSON-serialisable mapping.
        """
        document: dict[str, Any] = {"value": self.value, "unit": self.unit.value}
        if self.tolerance is not None:
            document["tolerance"] = self.tolerance
        if self.note:
            document["note"] = self.note
        return document

    @classmethod
    def from_document(cls, name: str, document: Mapping[str, Any]) -> Self:
        """Read one entry.

        Args:
            name: What it records, for the error message.
            document: The entry as committed.

        Returns:
            The entry.

        Raises:
            ValueError: If the entry is malformed. A baseline that half-parsed
                would gate on the half that did.
        """
        try:
            stated = document.get("tolerance")
            entry = cls(
                value=finite(document["value"]),
                unit=Unit(document["unit"]),
                tolerance=None if stated is None else finite(stated),
                note=str(document.get("note", "")),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            # `AttributeError` included deliberately: an entry committed as a
            # bare number rather than a mapping has no `get`, and a baseline
            # that half-parsed would gate on the half that did.
            message = f"baseline entry {name!r} is not one: {document!r}"
            raise ValueError(message) from error
        return entry


@dataclass(frozen=True, slots=True, kw_only=True)
class Profile:
    """The timings recorded for one class of machine.

    Attributes:
        name: The host class, as `reachy_bench.context.host_profile` spells it.
        gated: Whether a run on this class is judged against these numbers. The
            predecessor's profile is not, because its host is gone.
        description: What the machine was, in enough detail that somebody can
            decide whether their own is the same class.
        entries: Measurement name to recorded figure.
    """

    name: str
    gated: bool
    description: str
    entries: Mapping[str, BaselineEntry] = field(default_factory=dict)

    def as_document(self) -> dict[str, Any]:
        """Render the profile.

        Returns:
            A JSON-serialisable mapping.
        """
        return {
            "gated": self.gated,
            "description": self.description,
            "measurements": {
                name: entry.as_document()
                for name, entry in sorted(self.entries.items())
            },
        }

    @classmethod
    def from_document(cls, name: str, document: Mapping[str, Any]) -> Self:
        """Read one profile.

        Args:
            name: The host class it is keyed under.
            document: The profile as committed.

        Returns:
            The profile.

        Raises:
            ValueError: If it is malformed.
        """
        measurements = document.get("measurements", {})
        if not isinstance(measurements, dict):
            message = f"baseline profile {name!r} has no measurement mapping"
            raise ValueError(message)
        return cls(
            name=name,
            gated=bool(document.get("gated", True)),
            description=str(document.get("description", "")),
            entries={
                key: BaselineEntry.from_document(f"{name}.{key}", value)
                for key, value in measurements.items()
            },
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Baseline:
    """Everything recorded, and what counts as a departure from it.

    Attributes:
        tolerances: Unit to the fraction a measurement may grow by before it is
            a regression. Stated in the committed document rather than in code,
            so widening one is as visible in a diff as changing a number.
        artifacts: Sizes, which are host-independent and always gated.
        profiles: Timings and memory, by host class.
    """

    tolerances: Mapping[Unit, float]
    artifacts: Mapping[str, BaselineEntry]
    profiles: Mapping[str, Profile]

    def tolerance(self, entry: BaselineEntry) -> float:
        """How far one recorded figure may drift before it is a failure.

        Args:
            entry: The recorded figure.

        Returns:
            The tolerance as a fraction of the baseline: the entry's own when
            it states one, otherwise the unit's.
        """
        if entry.tolerance is not None:
            return entry.tolerance
        return self.tolerances.get(entry.unit, _DEFAULT_TOLERANCE)

    def profile(self, name: str) -> Profile | None:
        """Find the profile for one class of machine.

        Args:
            name: The host class.

        Returns:
            The profile, or `None` when nothing has been recorded for it.
        """
        return self.profiles.get(name)

    def as_document(self) -> dict[str, Any]:
        """Render the whole baseline.

        Returns:
            A JSON-serialisable mapping.
        """
        return {
            "schema": SCHEMA_VERSION,
            "tolerances": {
                unit.value: value for unit, value in sorted(self.tolerances.items())
            },
            "artifacts": {
                name: entry.as_document()
                for name, entry in sorted(self.artifacts.items())
            },
            "profiles": {
                name: profile.as_document()
                for name, profile in sorted(self.profiles.items())
            },
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Self:
        """Read a baseline.

        Args:
            document: The parsed committed document.

        Returns:
            The baseline.

        Raises:
            ValueError: If the document is not one this build reads — a schema
                it does not know, a section that is not a mapping, or an entry
                that is not one.
        """
        schema = document.get("schema")
        if schema != SCHEMA_VERSION:
            message = (
                f"baseline schema {schema!r} is not the {SCHEMA_VERSION} this "
                f"build reads"
            )
            raise ValueError(message)
        # Each section is checked to be a mapping before it is walked. A
        # section committed as a list or a number would otherwise raise
        # `AttributeError` out of `.items()`, and the command surface catches
        # `ValueError` — so the gate would exit with a traceback rather than
        # with the sentence it means to print.
        sections: dict[str, Mapping[str, Any]] = {}
        for section in ("tolerances", "artifacts", "profiles"):
            value = document.get(section, {})
            if not isinstance(value, dict):
                message = f"the baseline's {section!r} is a mapping, not {value!r}"
                raise ValueError(message)
            sections[section] = value
        tolerances: dict[Unit, float] = {}
        for key, stated in sections["tolerances"].items():
            try:
                tolerances[Unit(key)] = float(stated)
            except (TypeError, ValueError) as error:
                message = f"baseline tolerance {key!r} is not one: {stated!r}"
                raise ValueError(message) from error
        return cls(
            tolerances=tolerances,
            artifacts={
                name: BaselineEntry.from_document(name, entry)
                for name, entry in sections["artifacts"].items()
            },
            profiles={
                name: Profile.from_document(name, entry)
                for name, entry in sections["profiles"].items()
            },
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Read a baseline from JSON text.

        Args:
            text: The committed document.

        Returns:
            The baseline.

        Raises:
            ValueError: If the text is not a baseline document.
        """
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            message = f"the baseline is JSON: {error}"
            raise ValueError(message) from error
        if not isinstance(document, dict):
            message = "the baseline is a JSON object"
            raise ValueError(message)
        return cls.from_document(document)

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read the committed baseline off disk.

        Args:
            path: Where it is.

        Returns:
            The baseline.

        Raises:
            ValueError: If it is not a baseline document. A missing file raises
                `OSError` rather than being treated as an empty baseline: a
                comparison against nothing passes everything, which is the one
                outcome a gate must not produce by accident.
        """
        return cls.from_json(path.read_text(encoding="utf-8"))


def profile_document(
    run: RunResult,
    *,
    description: str,
    gated: bool = True,
) -> dict[str, Any]:
    """Render a run as the profile block that would record it.

    This is what makes adopting a new class of machine a reviewable diff: the
    run prints the block, somebody reads the numbers, and pasting it into the
    committed baseline is the pull request. Nothing here writes to the baseline.

    Sizes are left out. They are host-independent and live in `artifacts`, so
    including them in a profile would record the same number twice and let the
    two disagree.

    Args:
        run: The run to record.
        description: What the machine was.
        gated: Whether runs on this class are to be judged against it.

    Returns:
        The `{"<host class>": {...}}` block to paste into the baseline.
    """
    entries = {
        name: BaselineEntry(
            value=round(measurement.value, 3),
            unit=measurement.unit,
        )
        for name, measurement in run.by_name().items()
        if measurement.unit is not Unit.BYTES
    }
    profile = Profile(
        name=run.context.host.profile,
        gated=gated,
        description=description,
        entries=entries,
    )
    return {profile.name: profile.as_document()}
