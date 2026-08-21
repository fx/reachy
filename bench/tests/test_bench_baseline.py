"""The committed baseline: how it parses, and what the committed one says.

Two kinds of test here, and the second kind is why the module carries the
filesystem marker.

The first kind is ordinary parsing, over documents built in memory: a malformed
entry is refused rather than half-read, a tolerance stated on one figure beats
the unit's, and `profile_document` renders a run as the block somebody pastes
into the baseline.

The second reads `bench/baseline.json` itself. That file is the data the gate
compares against — the bytes on disk are the contract, exactly as the golden
fixture corpus and the deployment files are — so a fake would check the
documentation against itself. Those tests carry `@pytest.mark.filesystem`, which
declares that they are contract tests rather than unit tests. It grants nothing.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest
import yaml
from bench_support import PROFILE, make_benchmark, make_run

from reachy_bench.baseline import (
    PREDECESSOR_PROFILE,
    SCHEMA_VERSION,
    Baseline,
    BaselineEntry,
    profile_document,
)
from reachy_bench.benchmarks import SUITE
from reachy_bench.result import Unit

# The committed baseline, found from this file rather than from the working
# directory so the test passes wherever pytest was started.
_BASELINE: Final = Path(__file__).resolve().parents[1] / "baseline.json"


def _document(**overrides: object) -> dict[str, object]:
    """Build a minimal baseline document.

    Args:
        overrides: Sections to replace.

    Returns:
        The document.
    """
    document: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "tolerances": {"ms": 1.0, "bytes": 0.02},
        "artifacts": {
            "footprint.wheel.reachyctl": {"value": 97451, "unit": "bytes"},
        },
        "profiles": {
            PROFILE: {
                "gated": True,
                "description": "an example machine",
                "measurements": {
                    "detect.face.threads.4": {"value": 1.9, "unit": "ms"},
                },
            },
        },
    }
    document.update(overrides)
    return document


def test_a_baseline_parses_into_artifacts_and_profiles() -> None:
    """Sizes are flat and timings are keyed by the class of machine."""
    baseline = Baseline.from_document(_document())

    assert set(baseline.artifacts) == {"footprint.wheel.reachyctl"}
    profile = baseline.profile(PROFILE)
    assert profile is not None
    assert profile.gated
    assert profile.entries["detect.face.threads.4"].value == 1.9


def test_a_profile_nobody_has_recorded_is_absent_rather_than_empty() -> None:
    """An empty profile would compare a run against nothing and pass it."""
    assert Baseline.from_document(_document()).profile("linux-aarch64-4c") is None


def test_a_tolerance_stated_on_one_figure_beats_the_units() -> None:
    """Run-to-run variance is not uniform across measurements."""
    baseline = Baseline.from_document(_document())
    entry = BaselineEntry(value=0.004, unit=Unit.MILLISECONDS, tolerance=2.0)

    assert baseline.tolerance(entry) == 2.0
    assert baseline.tolerance(BaselineEntry(value=1.0, unit=Unit.MILLISECONDS)) == 1.0


def test_a_unit_the_document_states_no_tolerance_for_gets_a_tight_one() -> None:
    """An unstated tolerance should show up as a gate that fires."""
    baseline = Baseline.from_document(_document())

    assert baseline.tolerance(BaselineEntry(value=1.0, unit=Unit.CORES)) == 0.05


def test_a_baseline_from_another_schema_is_refused() -> None:
    """Reading past a changed shape would gate on numbers that had moved."""
    with pytest.raises(ValueError, match="schema"):
        Baseline.from_document(_document(schema=SCHEMA_VERSION + 1))


def test_an_entry_with_no_value_is_refused() -> None:
    """A baseline that half-parsed would gate on the half that did."""
    with pytest.raises(ValueError, match="is not one"):
        Baseline.from_document(
            _document(artifacts={"footprint.x": {"unit": "bytes"}}),
        )


def test_a_tolerance_that_is_not_a_number_is_refused() -> None:
    """A widened tolerance has to be visible; an unreadable one is not."""
    with pytest.raises(ValueError, match="tolerance"):
        Baseline.from_document(_document(tolerances={"ms": "loose"}))


def test_a_profile_with_no_measurement_mapping_is_refused() -> None:
    """A profile is a set of recorded figures, not a note."""
    with pytest.raises(ValueError, match="no measurement mapping"):
        Baseline.from_document(
            _document(profiles={PROFILE: {"measurements": "several"}}),
        )


def test_a_baseline_that_is_not_json_is_refused() -> None:
    """A truncated file must not read as a baseline that gates nothing."""
    with pytest.raises(ValueError, match="is JSON"):
        Baseline.from_json("{")


def test_a_json_array_is_not_a_baseline() -> None:
    """The top level is an object."""
    with pytest.raises(ValueError, match="JSON object"):
        Baseline.from_json("[]")


def test_a_baseline_survives_being_rendered_and_read_back() -> None:
    """Recording a profile writes what a later run will read."""
    baseline = Baseline.from_document(_document())

    assert Baseline.from_document(baseline.as_document()) == baseline


def test_recording_a_run_renders_the_block_to_paste() -> None:
    """Adopting a class of machine is a reviewable diff, not an automatic write."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 1.9})])

    block = profile_document(run, description="an example machine")

    assert set(block) == {PROFILE}
    profile = block[PROFILE]
    assert profile["gated"] is True
    assert profile["measurements"]["detect.face.threads.4"] == {
        "value": 1.9,
        "unit": "ms",
    }


def test_recording_a_run_leaves_the_sizes_out_of_the_profile() -> None:
    """A size is host-independent, and two copies would be free to disagree."""
    run = make_run(
        [
            make_benchmark("detect", {"detect.face.threads.4": 1.9}),
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 458_535_567.0},
                unit=Unit.BYTES,
            ),
        ],
    )

    profile = profile_document(run, description="")[PROFILE]

    assert set(profile["measurements"]) == {"detect.face.threads.4"}


# --- the committed baseline --------------------------------------------------


@pytest.mark.filesystem
def test_the_committed_baseline_parses() -> None:
    """The bytes on disk are what the gate reads, so they are the contract."""
    baseline = Baseline.load(_BASELINE)

    assert baseline.artifacts
    assert baseline.profiles


@pytest.mark.filesystem
def test_the_committed_baseline_records_the_predecessors_figures() -> None:
    """The rebuild is accountable to them, which is why the spec records them."""
    profile = Baseline.load(_BASELINE).profiles[PREDECESSOR_PROFILE]

    assert not profile.gated
    entries = profile.entries
    assert entries["detect.face.threads.4"].value == 38.0
    assert entries["detect.face.threads.1"].value == 93.0
    assert entries["detect.face.threads.6"].value == 55.0
    assert entries["pipeline.decode"].value == 2.0
    assert entries["session.round_trip"].value == 54.0
    assert entries["session.connect"].value == 378.0
    assert entries["footprint.resident_memory"].value == 205.0
    assert entries["robot-load.cpu_cores"].value == 1.52


@pytest.mark.filesystem
def test_every_recorded_figure_carries_a_note_saying_where_it_came_from() -> None:
    """A reviewer judging a change to a number needs to know what it is."""
    baseline = Baseline.load(_BASELINE)

    unexplained = [name for name, entry in baseline.artifacts.items() if not entry.note]
    unexplained.extend(
        f"{profile.name}.{name}"
        for profile in baseline.profiles.values()
        for name, entry in profile.entries.items()
        if not entry.note
    )

    assert unexplained == []


@pytest.mark.filesystem
def test_every_gated_figure_belongs_to_a_benchmark_the_suite_declares() -> None:
    """A figure whose benchmark no longer exists would never be measured again.

    The comparison attributes a recorded figure to a benchmark by the leading
    segment of its name, so one naming a benchmark that has gone is a figure
    nothing will ever produce — and it would be reported as a missing
    measurement forever, or not at all.
    """
    baseline = Baseline.load(_BASELINE)
    known = {spec.name for spec in SUITE}

    names = list(baseline.artifacts)
    names.extend(
        name for profile in baseline.profiles.values() for name in profile.entries
    )

    assert {name.partition(".")[0] for name in names} <= known


@pytest.mark.filesystem
def test_the_committed_baseline_states_a_tolerance_for_every_unit() -> None:
    """An unstated tolerance falls back to a default nobody argued for."""
    baseline = Baseline.load(_BASELINE)

    assert set(baseline.tolerances) == set(Unit)


@pytest.mark.filesystem
def test_the_committed_baseline_is_indented_json_a_reviewer_can_diff() -> None:
    """REQ-071's scenario is a diff of recorded numbers."""
    text = _BASELINE.read_text(encoding="utf-8")

    assert text.endswith("\n")
    assert json.loads(text)
    assert "\n  " in text


@pytest.mark.filesystem
def test_the_recorded_sizes_are_exactly_the_artifacts_this_repository_builds() -> None:
    """The completeness check the comparison deliberately does not make.

    A size is measured by the change that produces it, one artifact at a time,
    so a run is normally given one document and a comparison cannot tell a
    partial run from a measurement that went missing. This is what keeps the
    recorded set honest instead: it reads the image workflow's own matrix and
    the `wheels` recipe's own member list, so an artifact that stops being built
    — or one that starts being built and is never weighed — fails here.
    """
    root = _BASELINE.resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github" / "workflows" / "images.yml").read_text(encoding="utf-8"),
    )
    built = workflow["jobs"]["verify"]["strategy"]["matrix"]["include"]
    expected = {
        f"footprint.image.{entry['variant']}.linux-{entry['arch']}" for entry in built
    }

    recipe = (root / "Justfile").read_text(encoding="utf-8")
    members = re.search(
        r"^wheels out_dir=\"dist\":.*?for member in (.*?); do$",
        recipe,
        re.MULTILINE | re.DOTALL,
    )
    assert members is not None, "the `wheels` recipe no longer names its members"
    expected.update(
        f"footprint.wheel.{name}"
        for name in members.group(1).replace("\\\n", " ").split()
    )

    assert set(Baseline.load(_BASELINE).artifacts) == expected


def test_a_recorded_figure_renders_its_tolerance_and_its_note() -> None:
    """Both are what a reviewer reads when judging a change to a number."""
    entry = BaselineEntry(
        value=0.004,
        unit=Unit.MILLISECONDS,
        tolerance=1.0,
        note="clock granularity dominates a four-microsecond stage",
    )

    assert entry.as_document() == {
        "value": 0.004,
        "unit": "ms",
        "tolerance": 1.0,
        "note": "clock granularity dominates a four-microsecond stage",
    }


def test_a_recorded_figure_with_neither_renders_neither() -> None:
    """An absent tolerance means the unit's, and an absent note means none."""
    entry = BaselineEntry(value=1.0, unit=Unit.CORES)

    assert entry.as_document() == {"value": 1.0, "unit": "cores"}


@pytest.mark.parametrize("section", ["tolerances", "artifacts", "profiles"])
def test_a_section_that_is_not_a_mapping_is_refused(section: str) -> None:
    """The command surface catches `ValueError`, so nothing else may escape.

    A section committed as a list would otherwise raise `AttributeError` out of
    `.items()`, and the gate would exit with a traceback rather than with the
    sentence it means to print.

    Args:
        section: The section to commit as something that is not a mapping.
    """
    with pytest.raises(ValueError, match="is a mapping"):
        Baseline.from_document(_document(**{section: ["several"]}))


def test_a_recorded_figure_committed_as_a_bare_number_is_refused() -> None:
    """An entry with no `get` would raise `AttributeError` from inside it."""
    with pytest.raises(ValueError, match="is not one"):
        Baseline.from_document(_document(artifacts={"footprint.x": 97451}))
