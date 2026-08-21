"""The command surface: registration, global options, and what a command owes.

This layer is deliberately thin. It turns arguments into a plan, resolves the
credential, builds the source, hands all three to the command's own module and
turns whatever comes back into an exit status. Nothing about the session
protocol is decided here, which is what makes `doctor` in change 0008 and
`deploy` in 0009 additive rather than a second round of the same decisions.

Three conventions are set here and inherited by every command added later:

- **Global options belong to the callback.** `--output` and `--verbose` are
  answered once, and a command receives a `Reporter` already configured with
  them. A command that grew its own copy of either would be a command a script
  has to special-case.
- **Everything a command reports goes through that `Reporter`.** Including the
  failures: a command raises `CommandError` carrying the exit status its kind of
  failure is worth, and the one handler here renders it — so a structured run
  gets a document for a failure exactly as it does for a success.
- **No option takes a credential.** `--credential-file` takes a path. An
  argument is visible in the process list and lands in the shell history, and no
  amount of care afterwards undoes either.

`bench` is registered and does nothing. The name is reserved here so that the
change which implements it adds a body rather than a command, and so that
`reachyctl --help` does not imply the tool has fewer parts than the spec says.
"""

from __future__ import annotations

import os
from importlib import metadata

# Imported at module level rather than under TYPE_CHECKING: Typer reads these
# annotations at run time to build the options, so a name only the type checker
# can see is a name the command surface cannot be built from.
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import typer

from reachy_session_client import validate_session_url
from reachyctl.credentials import (
    CREDENTIAL_FILE_VARIABLE,
    URL_VARIABLE,
    load_credential,
)
from reachyctl.errors import CommandError, ConfigurationError
from reachyctl.exits import ExitCode
from reachyctl.frames import (
    CameraCapture,
    CameraFrames,
    FrameSource,
    RecordedFrames,
    open_camera,
)
from reachyctl.output import OutputFormat, Report, Reporter, build_reporter
from reachyctl.probe import DEFAULT_CAPABILITIES, ProbePlan, execute, parse_capability

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["app", "main"]

_DISTRIBUTION: Final = "reachyctl"

app = typer.Typer(
    name=_DISTRIBUTION,
    help=(
        "Operate a Reachy Mini running this stack: exercise the groundstation, "
        "and — as later changes land — deploy, configure and diagnose the robot."
    ),
    no_args_is_help=True,
    add_completion=False,
)


def _version() -> str:
    """Read this tool's version from its installed metadata.

    Returns:
        The version string. Every artifact in this repository carries the same
        one, so this is also the version of the protocol client it speaks with.
        A source checkout reached through `PYTHONPATH` rather than installed
        has no metadata to read, and says so rather than ending in a traceback
        — `--version` is the first thing anybody runs.
    """
    try:
        return metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return "unknown (running from a checkout that was not installed)"


def _show_version(show: bool) -> None:
    """Print the version and stop, when `--version` was given.

    Args:
        show: Whether the flag was present.

    Raises:
        typer.Exit: To stop before any command runs, which is what makes
            `--version` answerable without a credential or a groundstation.
    """
    if show:
        typer.echo(f"{_DISTRIBUTION} {_version()}")
        raise typer.Exit(ExitCode.OK)


@app.callback()
def main_options(
    ctx: typer.Context,
    output: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            "-o",
            help=(
                "How to render the result. 'text' is a table, rich only when "
                "attached to a terminal; 'json' is one document per run."
            ),
        ),
    ] = OutputFormat.TEXT,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Write progress detail to standard error while running.",
        ),
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the version and exit.",
            is_eager=True,
            callback=_show_version,
        ),
    ] = False,
) -> None:
    """Answer the options every command shares, once.

    Args:
        ctx: The invocation, which carries the reporter to the command.
        output: How to render the result.
        verbose: Whether to write the detail lines.
        version: Whether `--version` was given, handled by its callback before
            this body runs.
    """
    del version
    ctx.obj = build_reporter(output_format=output, verbose=verbose)


def _reporter(ctx: typer.Context) -> Reporter:
    """Take the reporter the callback built.

    Args:
        ctx: The invocation.

    Returns:
        The reporter this run writes through.

    Raises:
        RuntimeError: If the callback did not run, which cannot happen through
            the command surface.
    """
    reporter = ctx.obj
    # Typer runs the group callback before every command, so the branch below
    # cannot be reached through this tool's own surface — hence the pragma. The
    # check earns its place anyway: Typer types `ctx.obj` as `Any`, and this is
    # what turns it back into a `Reporter` under strict type checking rather
    # than letting `Any` spread into every command that reads it.
    if not isinstance(reporter, Reporter):  # pragma: no cover - see above
        message = "the reporter is missing: the group callback did not run"
        raise RuntimeError(message)
    return reporter


def _session_url(url: str) -> str:
    """Refuse an address that is not one a session can be opened on.

    The rule is the session client's, not a second copy of it: an address this
    tool accepted and the client then refused would be a message an operator
    could not act on, arriving from a constructor rather than from beside the
    option that carried it.

    Args:
        url: What was given on the command line or in the environment.

    Returns:
        The same address.

    Raises:
        ConfigurationError: If it is not a session URL. Nothing was contacted,
            so this is not a diagnosis of anything.
    """
    try:
        return validate_session_url(url)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error


def _source(
    frames: Path | None,
    camera: int | None,
    open_capture: Callable[[int], CameraCapture] = open_camera,
) -> FrameSource:
    """Build the frame source the operator asked for.

    Args:
        frames: A directory of recorded frames, if that is what was asked for.
        camera: A camera index, if that is what was asked for.
        open_capture: How to open a camera. Injected so that this function is
            exercised without a device — no test in this repository may require
            one.

    Returns:
        The source.

    Raises:
        ConfigurationError: If neither or both were given. Both is refused
            rather than resolved by precedence, because a run that quietly
            ignored half of what it was told is a diagnostic nobody can trust.
    """
    if frames is not None and camera is not None:
        message = "--frames and --camera name two sources; give exactly one"
        raise ConfigurationError(message)
    if frames is not None:
        return RecordedFrames(frames)
    if camera is not None:
        return CameraFrames(camera, open_capture)
    message = "no frames: give --frames with a directory, or --camera with an index"
    raise ConfigurationError(message)


@app.command()
def probe(
    ctx: typer.Context,
    url: Annotated[
        str,
        typer.Option(
            "--url",
            envvar=URL_VARIABLE,
            help="The groundstation's session endpoint, ws:// or wss://.",
        ),
    ],
    frames: Annotated[
        Path | None,
        typer.Option(
            "--frames",
            help="A directory of recorded frames to send, in name order.",
        ),
    ] = None,
    camera: Annotated[
        int | None,
        typer.Option("--camera", help="Send live frames from a local camera."),
    ] = None,
    capability: Annotated[
        list[str] | None,
        typer.Option(
            "--capability",
            help=(
                "A capability to offer, as 'name' or 'name:version'. Repeatable. "
                "Defaults to every capability this build knows about."
            ),
        ),
    ] = None,
    count: Annotated[
        int,
        typer.Option("--count", min=1, help="How many frames to send at most."),
    ] = 10,
    interval: Annotated[
        float,
        typer.Option("--interval", min=0.0, help="Seconds to wait between frames."),
    ] = 0.1,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0.1, help="Bound on the whole run, in seconds."),
    ] = 30.0,
    staleness: Annotated[
        float,
        typer.Option(
            "--staleness",
            min=0.1,
            help="How long to wait for results once the frames have run out.",
        ),
    ] = 2.0,
    credential_file: Annotated[
        Path | None,
        typer.Option(
            "--credential-file",
            envvar=CREDENTIAL_FILE_VARIABLE,
            help=(
                "A file holding the groundstation credential. There is no "
                "option that takes the credential itself."
            ),
        ),
    ] = None,
) -> None:
    """Open a real session to the groundstation and feed it frames.

    No robot is involved. The session is established with the same protocol
    client the robot application uses, so a groundstation that gets the protocol
    wrong fails here in the way it would fail the robot.

    Args:
        ctx: The invocation, carrying the reporter.
        url: The groundstation's session endpoint.
        frames: A directory of recorded frames.
        camera: A local camera index.
        capability: What to offer during negotiation.
        count: How many frames to send at most.
        interval: Seconds between frames.
        timeout: Bound on the whole run.
        staleness: How long to wait once the frames have run out.
        credential_file: Where the credential is kept.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        # Cheapest and most local first. Everything up to `_source` costs
        # nothing and touches nothing, so a mistyped address or capability is
        # answered before a directory is walked or a device is opened.
        credential = load_credential(_environ(), credential_file)
        reporter.redactor.guard(credential.reveal())
        # Saying that a credential was found is worth a verbose line, because
        # "which credential is in effect" is a question operators ask. Saying
        # which one is not: the value renders as a placeholder because of the
        # type it is held in, and the redactor above would catch it even if it
        # did not. Two guards on the same value, deliberately — this is the
        # path REQ-059's scenario is about.
        reporter.detail(f"credential resolved: {credential}")
        plan = ProbePlan(
            url=_session_url(url),
            capabilities=(
                DEFAULT_CAPABILITIES
                if not capability
                else tuple(parse_capability(text) for text in capability)
            ),
            count=count,
            interval=interval,
            timeout=timeout,
            staleness=staleness,
        )
        source = _source(frames, camera)
        code = execute(plan, source, credential, reporter)
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("probe", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


@app.command()
def bench(ctx: typer.Context) -> None:
    """Run the benchmark suite against a live installation.

    Not implemented. The name is registered so that the change which implements
    it adds a body rather than a command, and so that this tool's help does not
    describe a smaller thing than the spec does.

    Args:
        ctx: The invocation, carrying the reporter.

    Raises:
        typer.Exit: Always, with the status a command that did nothing earns.
    """
    reporter = _reporter(ctx)
    raise typer.Exit(
        reporter.emit(
            Report(
                command="bench",
                ok=False,
                summary="the benchmark suite is not implemented yet",
            ),
        ),
    )


def _environ() -> dict[str, str]:
    """Read the process environment.

    Returns:
        A copy, so that what a command resolved cannot be changed underneath it
        by anything else in the process.
    """
    return dict(os.environ)


def main() -> None:
    """Run the tool. This is the installed console script."""
    app()
