"""The wheel guard, exercised against every way it is meant to fail.

A wheel that builds is not a wheel that works, and every way this one can be
wrong is invisible to a successful build: a missing `reachy_mini_apps` entry
point produces a wheel that installs perfectly and never appears in the daemon's
application list, an entry point whose *module* has no execution path produces
one the daemon finds, launches and reports as finished within seconds, and an
asset shipping without its registry entry ships somebody else's file under terms
nobody agreed to. A guard nobody has watched fail is a guard that does not
exist, so each of those is provoked deliberately below.

Every wheel here is assembled in memory, so none has to have been built first.
Whether a test reads disk turns on **which fixture it uses**, and the two are
kept apart on purpose:

| Class | Fixture | Reads disk |
|---|---|---|
| `TestACorrectWheelPasses` | `_wheel` | yes — `@pytest.mark.filesystem` |
| `TestTheAssets` | `_wheel` | yes — `@pytest.mark.filesystem` |
| `TestTheEntryPoint` | `_metadata_only_wheel` | no |
| `TestTheLaunch` | `fs`, where a path is resolved | no — see its own docstring |
| `TestTheCommandLine` | neither | no — see its own docstring |

`_wheel` reads the committed assets through `_preimage`, and it has to: the
registry pins each one by digest, nothing synthetic hashes to a pinned digest,
and a fixture carrying invented bytes while claiming to be the shipped ones
would be testing the fixture. The bytes on disk are the thing a correct wheel has
to carry, which is exactly the case the root `AGENTS.md` says that marker exists
for. It is read once and cached.

`_metadata_only_wheel` carries nothing but the members a test names, so the
entry-point cases touch no disk at all and are ordinary unit tests. That is a
choice rather than an accident: what those check is which file an installer
would read and what it says, and the shipped assets have no bearing on either —
so reading them would be input the rule forbids and the test does not need.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
import verify_satellite_wheel
from verify_satellite_wheel import (
    ENTRY_POINT_GROUP,
    ENTRY_POINT_MODULE,
    ENTRY_POINT_TARGET,
    EX_CONFIG,
    REQUIRED_TEXTS,
    check,
)

from reachy_mini_ha_satellite.assets.registry import ASSETS

_PACKAGE = "reachy_mini_ha_satellite"
_METADATA = f"{_PACKAGE}-0.1.0.dist-info"


def _bytes_that_are_not(digest: str) -> bytes:
    """Build a payload that deliberately does not hash to a registered digest.

    This is the *substitution* case: an asset whose bytes changed between the
    registry and the build. Producing bytes that DO hash to a given digest is
    not something a fixture can do, which is why the matching case reads the
    committed file instead — see `_preimage`.

    The digest is woven into the payload only so that two assets do not produce
    identical wrong bytes; nothing reads it back.

    Args:
        digest: The registered digest this payload must not match.

    Returns:
        Bytes that do not hash to it.
    """
    return f"not the file whose digest is {digest}".encode()


def _wheel(
    *,
    assets: bool = True,
    texts: bool = True,
    entry_points: str | None = None,
    extra: dict[str, bytes] | None = None,
    corrupt: bool = False,
) -> zipfile.ZipFile:
    """Build a wheel in memory with whichever parts a test wants missing.

    Args:
        assets: Whether the registered assets are present.
        texts: Whether the licence texts are present.
        entry_points: What `entry_points.txt` says, or `None` for the correct
            declaration. An empty string leaves the file out entirely.
        extra: Anything else to put in the wheel.
        corrupt: Whether the assets carry bytes that do not match the registry.

    Returns:
        The wheel, open for reading.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writing:
        if assets:
            for asset in ASSETS:
                payload = _asset_bytes(asset.path, asset.sha256, corrupt=corrupt)
                writing.writestr(f"{_PACKAGE}/assets/{asset.path}", payload)
        if texts:
            for text in REQUIRED_TEXTS:
                writing.writestr(text, b"licence text")
        declared = (
            f"[{ENTRY_POINT_GROUP}]\nreachy-mini-ha-satellite = {ENTRY_POINT_TARGET}\n"
            if entry_points is None
            else entry_points
        )
        if declared:
            writing.writestr(f"{_METADATA}/entry_points.txt", declared)
        for name, payload in (extra or {}).items():
            writing.writestr(name, payload)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def _asset_bytes(path: str, digest: str, *, corrupt: bool) -> bytes:
    """Produce bytes for one asset, matching its digest or deliberately not.

    Args:
        path: Which asset.
        digest: What it should hash to.
        corrupt: Whether to hand back something else.

    Returns:
        The bytes.
    """
    if corrupt:
        return _bytes_that_are_not(digest)
    return _preimage(path, digest)


_PREIMAGES: dict[str, bytes] = {}


def _preimage(path: str, digest: str) -> bytes:
    """Read the real asset, so its digest matches what the registry records.

    The registry pins a digest, and no synthetic payload can be made to hash to
    it — so the fixture reads the file this repository ships. That makes these
    the one place in this module that touches a real path, and it is read once
    and cached.

    Args:
        path: The asset's path, relative to the asset directory.
        digest: The registered digest, checked so a mismatch here is reported
            as a fixture problem rather than as a wheel problem.

    Returns:
        The asset's bytes.
    """
    if path not in _PREIMAGES:
        from reachy_mini_ha_satellite.assets.registry import assets_dir

        payload = (assets_dir() / path).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            message = f"{path} on disk does not match the registry"
            raise AssertionError(message)
        _PREIMAGES[path] = payload
    return _PREIMAGES[path]


@pytest.mark.filesystem
class TestACorrectWheelPasses:
    """The shape `just wheels` actually produces.

    Marked because the fixtures read the shipped assets: the registry pins a
    digest and nothing synthetic hashes to it, so the bytes on disk are what a
    correct wheel has to carry.
    """

    def test_nothing_is_reported(self) -> None:
        """Which is the only outcome that lets a release proceed."""
        assert check(_wheel()) == []


@pytest.mark.filesystem
class TestTheAssets:
    """What ships, and under whose terms."""

    def test_a_missing_asset_is_reported(self) -> None:
        """A wheel with no wake word is a satellite that cannot be woken."""
        problems = check(_wheel(assets=False))

        assert len(problems) >= len(ASSETS)

    def test_an_asset_whose_bytes_changed_is_reported(self) -> None:
        """A substitution between the registry and the build."""
        problems = check(_wheel(corrupt=True))

        assert any("does not match the registered" in problem for problem in problems)

    def test_a_missing_licence_text_is_reported(self) -> None:
        """CC BY 4.0 and Apache-2.0 both require the text travel with the files."""
        problems = check(_wheel(texts=False))

        for text in REQUIRED_TEXTS:
            assert any(text in problem for problem in problems)

    def test_an_unregistered_asset_is_reported(self) -> None:
        """It shipped without anyone having agreed to its terms."""
        problems = check(
            _wheel(extra={f"{_PACKAGE}/assets/sounds/smuggled.flac": b"audio"}),
        )

        assert any("not in the asset registry" in problem for problem in problems)

    def test_the_exemption_list_is_the_same_closed_literal(self) -> None:
        """Adding to it is a licensing decision made in two files, not one."""
        problems = check(
            _wheel(extra={f"{_PACKAGE}/assets/registry.py": b"# code, not an asset"}),
        )

        assert problems == []


class TestTheCommandLine:
    """What `just wheel-verify` runs.

    `main` asks whether its argument is a file, which is a stat against the
    real filesystem. The second test therefore runs on `pyfakefs`, so the
    question is asked of an in-memory filesystem and nothing here performs real
    input — which is why it carries no marker, and why that is not an omission.
    """

    def test_it_takes_exactly_one_wheel(self) -> None:
        """Two would leave which one was verified up to the sort order."""
        assert verify_satellite_wheel.main([]) == 2

    @pytest.mark.usefixtures("fs")
    def test_a_path_that_is_not_a_file_is_reported(self) -> None:
        """Rather than a traceback out of the zip reader."""
        assert verify_satellite_wheel.main(["/nowhere/reachy.whl"]) == 2


class TestTheEntryPoint:
    """ha-satellite REQ-041, which is one line of metadata away from silence.

    Every case here builds a wheel carrying **only** the members it names, so
    none of them reads a byte from disk and all of them are ordinary unit tests.
    That is deliberate rather than incidental: what is under test is which file
    an installer would read and what it says, and the shipped assets have no
    bearing on either. `_entry_point_problems_only` drops the asset findings a
    metadata-only wheel produces by construction, which are true and are not
    what these are about.
    """

    def test_a_wheel_with_no_entry_points_at_all_is_reported(self) -> None:
        """It would install perfectly and never be found."""
        problems = check(_metadata_only_wheel({}))

        assert any("no .dist-info/entry_points.txt" in problem for problem in problems)

    def test_a_wheel_declaring_another_group_is_reported(self) -> None:
        """A console script is not a daemon application."""
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_METADATA}/entry_points.txt": (
                        b"[console_scripts]\nsatellite = a:b\n"
                    ),
                },
            ),
        )

        assert any(ENTRY_POINT_GROUP in problem for problem in problems)

    def test_an_entry_point_naming_something_else_is_reported(self) -> None:
        """The daemon instantiates whatever it resolves to."""
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_METADATA}/entry_points.txt": (
                        f"[{ENTRY_POINT_GROUP}]\nsatellite = wrong:Thing\n"
                    ).encode(),
                },
            ),
        )

        assert any(ENTRY_POINT_TARGET in problem for problem in problems)

    def test_the_right_target_in_the_wrong_group_is_reported(self) -> None:
        """It would install a console script and no daemon application.

        A substring check passes here, which is why the declaration is parsed.
        """
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_METADATA}/entry_points.txt": (
                        f"[console_scripts]\nsatellite = {ENTRY_POINT_TARGET}\n"
                        f"[{ENTRY_POINT_GROUP}]\nsatellite = somewhere.else:Thing\n"
                    ).encode(),
                },
            ),
        )

        assert any(ENTRY_POINT_TARGET in problem for problem in problems)

    def test_the_right_target_in_the_right_group_passes(self) -> None:
        """Even with other groups declared beside it."""
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_METADATA}/entry_points.txt": (
                        f"[console_scripts]\nsomething = elsewhere:main\n"
                        f"[{ENTRY_POINT_GROUP}]\n"
                        f"reachy-mini-ha-satellite = {ENTRY_POINT_TARGET}\n"
                    ).encode(),
                },
            ),
        )

        assert _entry_point_problems_only(problems) == []

    def test_a_malformed_file_reads_as_no_declaration(self) -> None:
        """Which is what the daemon would see, and so what is reported.

        The real reader is lenient where `configparser` is strict: it raises on
        nothing and simply yields no entry point for a line it cannot make
        sense of. So there is no "unparseable" outcome to report — a file the
        daemon can extract nothing from is a wheel the daemon would not
        discover, and that is the finding, arrived at by asking the same
        parser rather than by guessing what a stricter one would have said.
        """
        problems = _entry_point_problems_only(
            check(
                _metadata_only_wheel(
                    {f"{_METADATA}/entry_points.txt": b"not = an ini\n[unclosed\n"},
                ),
            ),
        )

        assert problems == [
            "the wheel declares no reachy_mini_apps entry point, so the daemon "
            "would not discover it",
        ]

    def test_a_value_containing_a_percent_sign_is_not_a_parse_failure(
        self,
    ) -> None:
        """`entry_points.txt` has no interpolation, so `%` is ordinary text.

        A `configparser` on its default `BasicInterpolation` raises on this,
        which would report a perfectly installable wheel as broken — a false
        failure in the gate that stands behind REQ-041. It is also the lazy
        kind of raise, landing on `items()` rather than on the read, so it
        escaped the handler meant to catch it and surfaced as a traceback.
        """
        declaration = (
            f"[{ENTRY_POINT_GROUP}]\n"
            f"satellite = {ENTRY_POINT_TARGET}\n"
            f"other = pkg.mod:Thing%20suffix\n"
        ).encode()

        problems = _entry_point_problems_only(
            check(_metadata_only_wheel({f"{_METADATA}/entry_points.txt": declaration})),
        )

        assert problems == []

    def test_a_doubled_percent_sign_is_not_rewritten(self) -> None:
        """Interpolation would silently turn `%%` into `%` before comparing.

        The target would then be judged on a spelling nothing ever wrote,
        which is the same defect as the raise above pointing the other way.
        """
        target = f"{ENTRY_POINT_TARGET}%%x"
        declaration = f"[{ENTRY_POINT_GROUP}]\nsatellite = {target}\n".encode()

        problems = _entry_point_problems_only(
            check(_metadata_only_wheel({f"{_METADATA}/entry_points.txt": declaration})),
        )

        assert problems == [
            f"the {ENTRY_POINT_GROUP} entry point resolves to ['{target}'] "
            f"rather than to {ENTRY_POINT_TARGET}",
        ]

    def test_the_right_target_under_default_does_not_stand_in(self) -> None:
        """The false *pass* — the direction that ships a broken wheel.

        `configparser` merges `[DEFAULT]` into every section, so it reads this
        as a correct declaration under `reachy_mini_apps`. `importlib.metadata`
        has no `[DEFAULT]` semantics at all and puts it in a group of that
        name, which means the daemon enumerating `reachy_mini_apps` finds
        nothing. The wheel installs and the application never appears.
        """
        declaration = (
            f"[DEFAULT]\nsmuggled = {ENTRY_POINT_TARGET}\n\n[{ENTRY_POINT_GROUP}]\n"
        ).encode()

        problems = _entry_point_problems_only(
            check(_metadata_only_wheel({f"{_METADATA}/entry_points.txt": declaration})),
        )

        assert problems == [
            "the wheel declares no reachy_mini_apps entry point, so the daemon "
            "would not discover it",
        ]

    def test_a_decoy_shipped_as_package_data_does_not_stand_in(self) -> None:
        """No installer reads it, so it must not satisfy the check either.

        This is the failure the old suffix match allowed: a wheel with no real
        declaration passing on the strength of a file the daemon never sees.
        """
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_PACKAGE}/entry_points.txt": (
                        f"[{ENTRY_POINT_GROUP}]\n"
                        f"reachy-mini-ha-satellite = {ENTRY_POINT_TARGET}\n"
                    ).encode(),
                },
            ),
        )

        assert any("no .dist-info/entry_points.txt" in problem for problem in problems)

    def test_the_real_declaration_is_found_beside_a_decoy(self) -> None:
        """And a wheel that carries both is still judged on the real one."""
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_METADATA}/entry_points.txt": (
                        f"[{ENTRY_POINT_GROUP}]\n"
                        f"reachy-mini-ha-satellite = {ENTRY_POINT_TARGET}\n"
                    ).encode(),
                    f"{_PACKAGE}/entry_points.txt": (
                        f"[{ENTRY_POINT_GROUP}]\nsatellite = a decoy:Thing\n"
                    ).encode(),
                },
            ),
        )

        assert _entry_point_problems_only(problems) == []

    def test_a_nested_dist_info_does_not_count(self) -> None:
        """The specification puts it at the root of the archive, not below one."""
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_PACKAGE}/data/{_METADATA}/entry_points.txt": (
                        f"[{ENTRY_POINT_GROUP}]\n"
                        f"reachy-mini-ha-satellite = {ENTRY_POINT_TARGET}\n"
                    ).encode(),
                },
            ),
        )

        assert any("no .dist-info/entry_points.txt" in problem for problem in problems)

    def test_two_metadata_directories_are_refused_rather_than_chosen_between(
        self,
    ) -> None:
        """Picking either would be picking arbitrarily between two answers.

        And the failure would not reproduce.
        """
        declaration = (
            f"[{ENTRY_POINT_GROUP}]\nreachy-mini-ha-satellite = {ENTRY_POINT_TARGET}\n"
        ).encode()
        problems = check(
            _metadata_only_wheel(
                {
                    f"{_METADATA}/entry_points.txt": declaration,
                    f"{_PACKAGE}-0.2.0.dist-info/entry_points.txt": declaration,
                },
            ),
        )

        assert any("is not a question with one answer" in p for p in problems)

    def test_the_refusal_names_both_of_them_in_a_stable_order(self) -> None:
        """A failure nobody can reproduce is a failure nobody fixes."""
        declaration = f"[{ENTRY_POINT_GROUP}]\nsatellite = a:b\n".encode()
        wheel = _metadata_only_wheel(
            {
                f"{_PACKAGE}-0.2.0.dist-info/entry_points.txt": declaration,
                f"{_METADATA}/entry_points.txt": declaration,
            },
        )

        reported = next(p for p in check(wheel) if "one answer" in p)

        assert reported.index(_METADATA) < reported.index(f"{_PACKAGE}-0.2.0")


class TestTheLaunch:
    """REQ-041's other half: the daemon has to be able to *run* what it found.

    The entry point resolving is not the daemon starting the application. The
    daemon takes the module half of the entry point and launches
    `python -u -m <module>`, so a module with no `if __name__ == "__main__":`
    block imports, does nothing and exits 0 — and the daemon reports the
    application as finished, successfully, with no output at all. That is a
    wheel this gate has to refuse, and every case below is one way of telling
    that outcome apart from a working one.

    `check_launch` itself extracts a wheel and starts two subprocesses, which a
    unit test must not do. What it decides on the strength of, though, is two
    pure functions over a finished process, and those are what is exercised
    here — with results handed to them directly, so the whole class performs no
    input or output. `just wheel-verify` runs the subprocess half against the
    real built wheel, which is where it belongs.

    The tests that compare a resolved path take `fs`, and only those. Deciding
    whether two paths name one file goes through `Path.resolve`, which reads
    the real filesystem to follow symbolic links; on `pyfakefs` it asks an
    in-memory one instead, so those tests perform no input either. The rest
    hand the verdict function a finished process and touch no path at all.
    """

    def test_exiting_zero_having_done_nothing_is_the_finding(self) -> None:
        """The defect itself: a module the daemon can find and cannot run."""
        problems = verify_satellite_wheel._execution_problems(
            ENTRY_POINT_MODULE,
            _finished(returncode=0),
        )

        assert len(problems) == 1
        assert '`if __name__ == "__main__":` block' in problems[0]

    def test_refusing_an_empty_configuration_is_a_pass(self) -> None:
        """It is the evidence, not a disappointment.

        Reaching that refusal means the guard ran, called `main`, and got as
        far as reading a configuration — none of which needs a robot.
        """
        problems = verify_satellite_wheel._execution_problems(
            ENTRY_POINT_MODULE,
            _finished(returncode=EX_CONFIG, stderr="DEVICE_NAME is not set\n"),
        )

        assert problems == []

    def test_refusing_without_saying_why_is_reported(self) -> None:
        """An operator reading the daemon's log has to be told what to set."""
        problems = verify_satellite_wheel._execution_problems(
            ENTRY_POINT_MODULE,
            _finished(returncode=EX_CONFIG),
        )

        assert any("without saying why" in problem for problem in problems)

    def test_any_other_status_is_reported_with_what_it_said(self) -> None:
        """A wheel that cannot import exits non-zero too.

        Accepting "well, it exited non-zero" would pass one, which is why the
        expected status is the specific one and not merely a truthy value.
        """
        problems = verify_satellite_wheel._execution_problems(
            ENTRY_POINT_MODULE,
            _finished(returncode=1, stderr="ModuleNotFoundError: no numpy\n"),
        )

        assert any("ModuleNotFoundError" in problem for problem in problems)

    def test_a_launch_that_said_nothing_at_all_is_still_legible(self) -> None:
        """`(it said nothing)` rather than a finding trailing off into space."""
        problems = verify_satellite_wheel._execution_problems(
            ENTRY_POINT_MODULE,
            _finished(returncode=1),
        )

        assert any("(it said nothing)" in problem for problem in problems)

    @pytest.mark.usefixtures("fs")
    def test_the_module_that_runs_has_to_be_the_wheel_s_own(self) -> None:
        """Otherwise the launch below is a check of this checkout.

        Every member here is installed editable, so an interpreter can reach
        the source tree as well as the extraction directory — and a launch that
        resolved to the source would pass on a wheel built before the fix.
        """
        problems = verify_satellite_wheel._resolution_problems(
            ENTRY_POINT_MODULE,
            Path("/extracted"),
            _finished(
                returncode=0,
                stdout=_probe(origin="/checkout/src/daemon_app.py"),
            ),
        )

        assert any("rather than the wheel's own" in problem for problem in problems)

    @pytest.mark.usefixtures("fs")
    def test_the_wheel_s_own_copy_resolves_cleanly(self) -> None:
        """The arrangement `just wheels` actually produces."""
        origin = "/extracted/reachy_mini_ha_satellite/daemon_app.py"

        problems = verify_satellite_wheel._resolution_problems(
            ENTRY_POINT_MODULE,
            Path("/extracted"),
            _finished(returncode=0, stdout=_probe(origin=origin)),
        )

        assert problems == []

    @pytest.mark.usefixtures("fs")
    def test_a_package_would_run_its_main_submodule_instead(self) -> None:
        """Which is a different file from the one the entry point names.

        This package has a `__main__.py`, so an entry point naming the package
        rather than the module would run that file — and pass a launch check
        that only asked whether *something* ran, while the module the daemon's
        own lookup returns still had no execution path.
        """
        problems = verify_satellite_wheel._resolution_problems(
            "reachy_mini_ha_satellite",
            Path("/extracted"),
            _finished(
                returncode=0,
                stdout=_probe(
                    origin="/extracted/reachy_mini_ha_satellite/__init__.py",
                    package=True,
                ),
            ),
        )

        assert any("__main__ submodule" in problem for problem in problems)

    def test_a_module_the_wheel_does_not_carry_is_reported(self) -> None:
        """The entry point names something that is not in the distribution."""
        problems = verify_satellite_wheel._resolution_problems(
            ENTRY_POINT_MODULE,
            Path("/extracted"),
            _finished(returncode=0, stdout=_probe(origin=None)),
        )

        assert any("is not in the wheel" in problem for problem in problems)

    def test_a_probe_that_failed_is_reported_rather_than_read(self) -> None:
        """Reading its empty output would report a missing module instead."""
        problems = verify_satellite_wheel._resolution_problems(
            ENTRY_POINT_MODULE,
            Path("/extracted"),
            _finished(returncode=1, stderr="the interpreter refused\n"),
        )

        assert any("the interpreter refused" in problem for problem in problems)

    def test_a_probe_that_said_something_unreadable_is_reported(self) -> None:
        """Rather than a `JSONDecodeError` out of the middle of the gate."""
        problems = verify_satellite_wheel._resolution_problems(
            ENTRY_POINT_MODULE,
            Path("/extracted"),
            _finished(returncode=0, stdout="not json\n"),
        )

        assert any("nothing usable" in problem for problem in problems)


def _finished(
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Stand in for a process that has already run.

    Args:
        returncode: The status it exited with.
        stdout: What it printed.
        stderr: What it complained about.

    Returns:
        The finished process, in the shape `subprocess.run` returns one.
    """
    return subprocess.CompletedProcess(
        args=["python"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _probe(*, origin: str | None, package: bool = False) -> str:
    """Write what the resolution probe prints.

    Args:
        origin: The file `python -m` would run, or `None` for a name that
            resolves to nothing.
        package: Whether that name is a package.

    Returns:
        The probe's output.
    """
    return json.dumps({"origin": origin, "package": package})


def _metadata_only_wheel(members: dict[str, bytes]) -> zipfile.ZipFile:
    """Build a wheel carrying nothing but the members a test names.

    Args:
        members: What to put in it.

    Returns:
        The wheel, open for reading.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writing:
        for name, payload in members.items():
            writing.writestr(name, payload)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def _entry_point_problems_only(problems: list[str]) -> list[str]:
    """Keep the problems this class is about, discarding the asset ones.

    A metadata-only wheel is missing every asset by construction, which is a
    true finding and not the one under test here.

    Args:
        problems: Everything `check` reported.

    Returns:
        The entry-point findings alone.
    """
    return [
        problem
        for problem in problems
        if "entry_points" in problem or ENTRY_POINT_GROUP in problem
    ]
