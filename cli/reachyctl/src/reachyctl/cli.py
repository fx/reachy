"""The command surface: registration, global options, and what a command owes.

This layer is deliberately thin. It turns arguments into a plan, resolves the
credential, builds the source, hands all three to the command's own module and
turns whatever comes back into an exit status. Nothing about the session
protocol is decided here, and nothing about what a healthy installation is:
`doctor` runs the shared check registry in `reachy_checks`, which is what makes
it and the provisioning verification role assert the same conditions.

Three conventions are set here and inherited by every command added later:

- **Global options belong to the callback.** `--output` and `--verbose` are
  answered once, and a command receives a `Reporter` already configured with
  them. A command that grew its own copy of either would be a command a script
  has to special-case.
- **Everything a command reports goes through that `Reporter`.** Including the
  failures: a command raises `CommandError` carrying the exit status its kind of
  failure is worth, and the one handler here renders it — so a structured run
  gets a document for a failure exactly as it does for a success.
- **No option or argument takes a credential.** `--credential-file` takes a
  path, and `config set` refuses a setting the vocabulary marks secret for
  exactly the same reason: an argument is visible in the process list to every
  user on the machine and lands in the shell history, and no amount of care
  afterwards undoes either. A secret setting reaches the robot through
  `config apply --declaration`, which reads a file.

`bench` is registered and does nothing. The name is reserved here so that the
change which implements it adds a body rather than a command, and so that
`reachyctl --help` does not imply the tool has fewer parts than the spec says.
"""

from __future__ import annotations

import os
import tempfile
from importlib import metadata

# Imported at module level rather than under TYPE_CHECKING: Typer reads these
# annotations at run time to build the options, so a name only the type checker
# can see is a name the command surface cannot be built from.
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import typer

from reachy_contracts import SettingError, validate_settings
from reachy_session_client import validate_session_url
from reachyctl.application import execute_logs, execute_start, execute_stop
from reachyctl.configure import (
    execute_apply,
    execute_diff,
    execute_get,
    guard_secrets,
    known_setting_names,
    secret_setting_names,
)
from reachyctl.credentials import (
    CREDENTIAL_FILE_VARIABLE,
    ENV_PREFIX,
    URL_VARIABLE,
    load_credential,
)
from reachyctl.daemon import DaemonClient
from reachyctl.declaration import (
    DECLARATION_VARIABLE,
    INTENT_VARIABLE,
    load_declaration,
    load_intent,
)
from reachyctl.deploy import DeployPlan
from reachyctl.deploy import execute as execute_deploy
from reachyctl.doctor import MODELS_DIR_VARIABLE, DoctorPlan
from reachyctl.doctor import execute as execute_doctor
from reachyctl.errors import CommandError, ConfigurationError
from reachyctl.exits import ExitCode
from reachyctl.frames import (
    CameraCapture,
    CameraFrames,
    FrameSource,
    RecordedFrames,
    open_camera,
)
from reachyctl.managed import DEFAULT_DAEMON_UNIT
from reachyctl.output import OutputFormat, Report, Reporter, build_reporter
from reachyctl.probe import DEFAULT_CAPABILITIES, ProbePlan, execute, parse_capability
from reachyctl.provision import DIRECTORY_VARIABLE as PROVISIONING_DIRECTORY_VARIABLE
from reachyctl.provision import ProvisionPlan
from reachyctl.provision import execute as execute_provision
from reachyctl.provision import resolve_directory as resolve_provisioning_directory
from reachyctl.robot import (
    DEFAULT_APPLICATION,
    DEFAULT_DAEMON_CONTROL,
    DEFAULT_PYTHON,
    RemoteAccess,
    RobotLayout,
    RobotTarget,
    parse_robot,
)
from reachyctl.ssh import SshAccess
from reachyctl.wheels import Wheel, build_wheel, read_wheel

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from reachyctl.robot import Closer

__all__ = ["app", "main"]

# Where the robot is, when it is not given on the command line. An address, and
# never in a tracked file: this repository is public, and the value belongs to
# whoever is running the tool.
ROBOT_VARIABLE: Final = f"{ENV_PREFIX}ROBOT"

_DISTRIBUTION: Final = "reachyctl"

app = typer.Typer(
    name=_DISTRIBUTION,
    help=(
        "Operate a Reachy Mini running this stack: diagnose the chain end to "
        "end, exercise the groundstation, and — as later changes land — deploy "
        "and configure the robot."
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


# --- The options every robot-facing command shares ---------------------------
# Declared once as annotated aliases rather than restated per command. Four
# commands take the same eight options, and eight blocks of help text copied
# four times is eight opportunities for `deploy --robot` and `app --robot` to
# start meaning subtly different things.

RobotOption = Annotated[
    str | None,
    typer.Option(
        "--robot",
        envvar=ROBOT_VARIABLE,
        help=(
            "The robot, as user@host, user@host:port or user@[ipv6]:port. The "
            "account is not defaulted."
        ),
    ),
]
IdentityOption = Annotated[
    Path | None,
    typer.Option(
        "--identity-file",
        help="A private key to offer. Without one, the agent and the defaults.",
    ),
]
KnownHostsOption = Annotated[
    Path | None,
    typer.Option(
        "--known-hosts",
        help=(
            "A host-key file to verify the robot against. Without one, the "
            "client's own defaults. There is no option that stops verification."
        ),
    ),
]
SudoOption = Annotated[
    bool,
    typer.Option(
        "--sudo/--no-sudo",
        help=(
            "Whether privileged commands are run through a non-interactive "
            "sudo. Turn it off when the account already is root."
        ),
    ),
]
ApplicationOption = Annotated[
    str,
    typer.Option("--application", help="The distribution being operated."),
]
DaemonUnitOption = Annotated[
    str,
    typer.Option("--daemon-unit", help="The systemd unit carrying the environment."),
]
DaemonControlOption = Annotated[
    str,
    typer.Option(
        "--daemon-control",
        help="The module the daemon's application control is reached through.",
    ),
]
PythonOption = Annotated[
    str,
    typer.Option(
        "--python",
        help=(
            "The application environment's interpreter, used only when the "
            "daemon's unit does not say which one it runs."
        ),
    ),
]
PreviewOption = Annotated[
    bool,
    typer.Option(
        "--preview",
        help="Report the changes this would make, and make none of them.",
    ),
]


def _build_access(target: RobotTarget) -> RemoteAccess:
    """Open the way onto a robot.

    A module-level function rather than an argument, because every caller is a
    Typer command and an argument here would become an option on the command
    line. It is the one seam the command-level tests replace, which is how the
    step sequences are exercised against a robot that is not there.

    Args:
        target: Where the robot is.

    Returns:
        The remote-access link. Nothing is connected yet: the connection is
        opened by the first command that needs it, which is what makes "the
        robot was not contacted" an observable property rather than a claim.
    """
    return SshAccess(target)


def _target(
    robot: str | None,
    identity_file: Path | None,
    known_hosts: Path | None,
    sudo: bool,
) -> RobotTarget:
    """Read the robot options into a target.

    Args:
        robot: What `--robot` or the environment carried.
        identity_file: What `--identity-file` carried.
        known_hosts: What `--known-hosts` carried.
        sudo: Whether privileged commands are elevated.

    Returns:
        The target.

    Raises:
        ConfigurationError: If no robot was named, or the address is not one
            this can be read from. Nothing has been contacted.
    """
    if robot is None:
        message = (
            f"no robot: pass --robot with user@host, or set {ROBOT_VARIABLE}. "
            f"No address is defaulted, because an address belongs to whoever "
            f"is running this"
        )
        raise ConfigurationError(message)
    return parse_robot(
        robot,
        identity_file=identity_file,
        known_hosts=known_hosts,
        elevate=sudo,
    )


def _layout(
    application: str,
    daemon_unit: str,
    daemon_control: str,
    python: str,
) -> RobotLayout:
    """Read the layout options into a layout.

    Args:
        application: The distribution being operated.
        daemon_unit: The unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.

    Returns:
        The layout.
    """
    return RobotLayout(
        application=application,
        daemon_unit=daemon_unit,
        daemon_control=daemon_control,
        python=python,
    )


def _connect(
    target: RobotTarget,
    layout: RobotLayout,
    reporter: Reporter,
) -> tuple[DaemonClient, Closer]:
    """Build the daemon client and the thing that lets its link go.

    Args:
        target: Where the robot is.
        layout: Where things are on it.
        reporter: Where the client writes what is worth seeing and not worth
            failing over.

    Returns:
        The client, and the closer to hand to the command's `execute`.
    """
    access = _build_access(target)
    client = DaemonClient(
        access,
        layout,
        elevate=target.elevate,
        # Where the client says something worth seeing that is not worth
        # failing a command over — a staged file it could not remove. It goes
        # through the reporter, so it is scrubbed like every other line.
        complain=reporter.note,
    )
    return client, access.aclose


def _validated(declared: Mapping[str, str]) -> dict[str, str]:
    """Check a declaration against the shared vocabulary, before anything is sent.

    Args:
        declared: The settings by name.

    Returns:
        The declaration as it will be written to the robot.

    Raises:
        ConfigurationError: If anything in it is not a value the robot would
            accept. Raised here — above `_connect`, before any command builds a
            link — so that a refused value costs no round trip, which is what
            reachyctl REQ-053's scenario asks to be able to observe.
    """
    try:
        return validate_settings(declared)
    except SettingError as error:
        raise ConfigurationError(str(error)) from error


def _assignments(pairs: list[str]) -> dict[str, str]:
    """Read `NAME=VALUE` arguments into a mapping.

    Args:
        pairs: What was written on the command line.

    Returns:
        The settings by name.

    Raises:
        ConfigurationError: If an argument is not an assignment, names a setting
            the vocabulary marks secret, or names the same setting twice.

    **A malformed argument is reported by its position, never by its text.**
    `=hunter2` partitions into an empty name and a value, so a message that
    quoted "the argument" would print the whole of it — and it would do so
    before anything has seeded the redactor, because the redactor is seeded from
    the parsed settings this function produces. The position is enough to find
    the argument and carries nothing.
    """
    settings: dict[str, str] = {}
    for position, pair in enumerate(pairs, start=1):
        name, separator, value = pair.partition("=")
        if not separator or not name:
            message = (
                f"argument {position} is not an assignment; write NAME=VALUE. "
                f"It is not quoted back, because the text after an '=' is a "
                f"value. Declared settings: {', '.join(known_setting_names())}"
            )
            raise ConfigurationError(message)
        if name in secret_setting_names():
            message = (
                f"{name} holds a secret, and this command takes its values as "
                f"arguments — which are visible in the process list to every "
                f"user on this machine and land in the shell history. Put it in "
                f"a declaration document and use `config apply --declaration`, "
                f"which reads a file"
            )
            raise ConfigurationError(message)
        if name in settings:
            message = f"{name} was assigned twice in one invocation"
            raise ConfigurationError(message)
        settings[name] = value
    return settings


def _wheel_source(
    member: str | None, wheel: Path | None
) -> tuple[Callable[[], Wheel], str]:
    """Decide where the wheel a deploy sends comes from.

    Args:
        member: A workspace member to build, if that is what was asked for.
        wheel: A wheel to send, if that is what was asked for.

    Returns:
        How to obtain it, and one line saying where it came from.

    Raises:
        ConfigurationError: If neither or both were given. Both is refused
            rather than resolved by precedence, because a deploy that quietly
            ignored half of what it was told is a deploy nobody can trust — and
            this one ends by asserting a version.
    """
    if member is not None and wheel is not None:
        message = "--member and --wheel name two wheels; give exactly one"
        raise ConfigurationError(message)
    if wheel is not None:
        return (lambda: read_wheel(wheel)), f"from {wheel}"
    if member is None:
        message = (
            "no wheel: give --member with a workspace member to build, or "
            "--wheel with one to send"
        )
        raise ConfigurationError(message)

    def _built() -> Wheel:
        """Build the member and read what came out.

        Returns:
            The wheel, read into memory before the build directory goes.
        """
        with tempfile.TemporaryDirectory(prefix="reachyctl-deploy-") as directory:
            return read_wheel(build_wheel(member, Path(directory)))

    return _built, f"by building {member}"


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
def doctor(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            envvar=URL_VARIABLE,
            help=(
                "The groundstation's session endpoint, ws:// or wss://. "
                "Without one, the groundstation checks are skipped rather than "
                "reported as broken."
            ),
        ),
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
    models_dir: Annotated[
        Path | None,
        typer.Option(
            "--models-dir",
            envvar=MODELS_DIR_VARIABLE,
            help=(
                "The directory holding the pinned model files. Without one, "
                "the model check is skipped."
            ),
        ),
    ] = None,
    intent: Annotated[
        Path | None,
        typer.Option(
            "--intent",
            envvar=INTENT_VARIABLE,
            help=(
                "A JSON document declaring what the robot is supposed to be: "
                "'configuration' and 'announced_identity'. Without one, the "
                "configuration and identity checks are skipped."
            ),
        ),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            min=0.1,
            help=(
                "One budget, in seconds, for the whole groundstation exchange: "
                "opening the session, sending a frame and waiting for the "
                "result. Not per step."
            ),
        ),
    ] = 10.0,
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
    """Diagnose the chain from this machine to a working robot, link by link.

    Every check runs whatever the ones before it did, so a broken groundstation
    does not hide the state of the daemon, and the summary names the first link
    that is actually broken. A check whose prerequisites are absent is reported
    as skipped rather than failed: not having configured something is not the
    same as it being broken.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot, if one is to be diagnosed. Without one the robot-side
            checks are skipped rather than reported as broken.
        identity_file: A private key to offer the robot.
        known_hosts: A host-key file to verify the robot against.
        sudo: Whether privileged commands are elevated.
        application: The distribution the application checks are about.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        url: The groundstation's session endpoint, if one is configured.
        capability: What to offer during negotiation.
        models_dir: Where the pinned model files are.
        intent: A declaration of what the robot is supposed to be.
        timeout: One budget for the whole groundstation exchange.
        credential_file: Where the credential is kept.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    close: Closer | None = None
    try:
        # Cheapest and most local first, so a mistyped address or an intent
        # document that is not one is answered before anything is contacted.
        daemon = None
        addressed = None
        if robot is not None:
            target = _target(robot, identity_file, known_hosts, sudo)
            # The resolved target, not the option's text — the same value every
            # other command reports, so one robot is named one way whichever
            # command a script is reading.
            addressed = target.describe()
            daemon, close = _connect(
                target,
                _layout(application, daemon_unit, daemon_control, python),
                reporter,
            )
        plan = DoctorPlan(
            url=None if url is None else _session_url(url),
            capabilities=(
                DEFAULT_CAPABILITIES
                if not capability
                else tuple(parse_capability(text) for text in capability)
            ),
            daemon=daemon,
            robot=addressed,
            models_directory=models_dir,
            intent=None if intent is None else load_intent(intent),
            timeout=timeout,
        )
        credential = None
        if plan.url is not None:
            # A configured groundstation with no credential is a mistake in the
            # invocation and not a diagnosis of anything, so it is reported as
            # one rather than as a groundstation that is skipped or down.
            credential = load_credential(_environ(), credential_file)
            reporter.redactor.guard(credential.reveal())
            reporter.detail(f"credential resolved: {credential}")
        code = execute_doctor(plan, credential, reporter, close=close)
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("doctor", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


# --- deploy ------------------------------------------------------------------


@app.command()
def deploy(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: Annotated[
        str | None,
        typer.Option(
            "--application",
            help=(
                "The distribution the daemon knows this by. Defaults to the "
                "one the wheel carries, which is the only name that cannot be "
                "wrong about what was installed."
            ),
        ),
    ] = None,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    preview: PreviewOption = False,
    member: Annotated[
        str | None,
        typer.Option(
            "--member",
            help=(
                "A workspace member to build and send. Give this or --wheel, not both."
            ),
        ),
    ] = None,
    wheel: Annotated[
        Path | None,
        typer.Option("--wheel", help="A wheel already built, to send as it is."),
    ] = None,
) -> None:
    """Build a wheel, install it on the robot, and verify what is running.

    The command's answer is the last step's, not the install's: it asks the
    daemon, through the interpreter the daemon itself runs, what version is
    there after the restart. A package that installed successfully into an
    environment the daemon is not using exits zero at every step and leaves the
    robot running the previous version, and naming that version is the whole
    difference between a deploy tool and a sequence of remote commands.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot to deploy to.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: The distribution the daemon knows this by. Defaults to
            the one the wheel carries.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        preview: Report what this would do and do none of it.
        member: A workspace member to build.
        wheel: A wheel to send.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        # Everything local first: a missing wheel, an unreadable one, or an
        # address that is not one costs no connection.
        obtain, origin = _wheel_source(member, wheel)
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            # A placeholder until the wheel is read: `run_deploy` rebinds the
            # client to the distribution the wheel carries as its first act,
            # which is what stops a deploy verifying a name that came from
            # somewhere other than the thing being installed.
            _layout(
                application or DEFAULT_APPLICATION,
                daemon_unit,
                daemon_control,
                python,
            ),
            reporter,
        )
        code = execute_deploy(
            DeployPlan(
                obtain=obtain,
                origin=origin,
                application=application,
                preview=preview,
            ),
            daemon,
            reporter,
            target.describe(),
            close,
        )
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("deploy", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


# --- provision ---------------------------------------------------------------


@app.command()
def provision(
    ctx: typer.Context,
    directory: Annotated[
        Path | None,
        typer.Option(
            "--directory",
            envvar=PROVISIONING_DIRECTORY_VARIABLE,
            help=(
                "Where the playbook is. Defaults to provisioning/ansible under "
                "this directory."
            ),
        ),
    ] = None,
    inventory: Annotated[
        Path | None,
        typer.Option(
            "--inventory",
            help="An inventory to use, rather than the playbook's own.",
        ),
    ] = None,
    tags: Annotated[
        list[str] | None,
        typer.Option(
            "--tags",
            help=(
                "Apply one concern rather than all of them: daemon_env, "
                "app_install, groundstation_link, verify. Repeatable."
            ),
        ),
    ] = None,
    limit: Annotated[
        str,
        typer.Option("--limit", help="Which hosts in the inventory to run against."),
    ] = "",
    extra_vars: Annotated[
        list[str] | None,
        typer.Option(
            "--extra-vars",
            "-e",
            help=(
                "Passed to Ansible unchanged. A credential arrives this way as "
                "@path/to/vault.yml — a path, never a value: an argument is "
                "visible in the process list and lands in the shell history."
            ),
        ),
    ] = None,
    remove: Annotated[
        bool,
        typer.Option(
            "--remove",
            help=(
                "Run the removal path: undo everything provisioning applied and "
                "assert the robot is back to stock behaviour."
            ),
        ),
    ] = False,
    preview: PreviewOption = False,
) -> None:
    """Run the provisioning playbook, or report what it would do.

    A thin wrapper, deliberately. Provisioning owns durable machine state and
    `reachyctl` operates a robot already in it, so there is one description of
    what a robot is and this runs it rather than reimplementing it. Ansible's own
    output is the report; what this adds is finding the playbook, spelling
    preview the way every other mutating command here spells it, naming the
    removal path, and turning Ansible's exit status into this tool's.

    Args:
        ctx: The invocation, carrying the reporter.
        directory: Where the playbook is.
        inventory: An inventory to use.
        tags: The concerns to apply.
        limit: Which hosts to run against.
        extra_vars: Values passed to Ansible unchanged.
        remove: Whether to run the removal path.
        preview: Report the changes this would make and make none of them.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        code = execute_provision(
            ProvisionPlan(
                directory=resolve_provisioning_directory(directory),
                preview=preview,
                remove=remove,
                tags=tuple(tags or ()),
                limit=limit,
                inventory=inventory,
                extra_vars=tuple(extra_vars or ()),
                verbose=reporter.verbose,
            ),
            reporter,
        )
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("provision", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


# --- config ------------------------------------------------------------------

config = typer.Typer(
    name="config",
    help=(
        "Read and change the robot's daemon environment. The managed region is "
        "owned in full: `apply` makes it exactly the declaration, so a setting "
        "removed from the declaration is removed from the robot."
    ),
    no_args_is_help=True,
)
app.add_typer(config)


@config.command("get")
def config_get(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    name: Annotated[
        list[str] | None,
        typer.Option(
            "--name",
            help="A setting to report. Repeatable. Defaults to all of them.",
        ),
    ] = None,
) -> None:
    """Report the configuration the daemon is actually running with.

    The effective environment rather than the managed region, because the
    question an operator has is what is in force — a setting that is declared
    and silently inert is one of the two failures this tool was written for.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot to read.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: The distribution being operated.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        name: Which settings to report.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            _layout(application, daemon_unit, daemon_control, python),
            reporter,
        )
        code = execute_get(daemon, name or [], reporter, target.describe(), close)
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("config get", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


@config.command("diff")
def config_diff(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    declaration: Annotated[
        Path | None,
        typer.Option(
            "--declaration",
            envvar=DECLARATION_VARIABLE,
            help=(
                "The JSON document declaring what should be in force. The same "
                "document `doctor --intent` reads."
            ),
        ),
    ] = None,
) -> None:
    """Compare a declaration against the robot and report the difference.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot to compare against.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: The distribution being operated.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        declaration: Where the declaration is.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        desired = _validated(_declaration(declaration))
        guard_secrets(reporter, desired)
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            _layout(application, daemon_unit, daemon_control, python),
            reporter,
        )
        code = execute_diff(daemon, desired, reporter, target.describe(), close)
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("config diff", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


@config.command("apply")
def config_apply(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    preview: PreviewOption = False,
    declaration: Annotated[
        Path | None,
        typer.Option(
            "--declaration",
            envvar=DECLARATION_VARIABLE,
            help=(
                "The JSON document declaring what should be in force. The same "
                "document `doctor --intent` reads."
            ),
        ),
    ] = None,
) -> None:
    """Make the robot's managed region exactly this declaration.

    A setting removed from the declaration is removed from the robot. That is
    the difference between this verb and `set`, and it is why they are two
    verbs: the region is owned in full, so the only way to say "leave the rest
    alone" is to be explicit about it.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot to converge.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: The distribution being operated.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        preview: Report what this would do and do none of it.
        declaration: Where the declaration is.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        desired = _validated(_declaration(declaration))
        guard_secrets(reporter, desired)
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            _layout(application, daemon_unit, daemon_control, python),
            reporter,
        )
        code = execute_apply(
            "config apply",
            daemon,
            desired,
            reporter,
            target.describe(),
            preview=preview,
            close=close,
        )
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("config apply", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


@config.command("set")
def config_set(
    ctx: typer.Context,
    assignment: Annotated[
        list[str],
        typer.Argument(
            help="One or more NAME=VALUE settings to change. Nothing else moves.",
        ),
    ],
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    preview: PreviewOption = False,
) -> None:
    """Change some settings and leave the rest of the managed region alone.

    The region is still written whole, from what it carried plus what was
    asked for, so it stays exactly what this tool says it is. Removing a
    setting is `apply`'s job, which is why they are separate verbs.

    A setting the vocabulary marks secret is refused here. Its value would be an
    argument, and an argument is visible in the process list and lands in the
    shell history; `config apply --declaration` reads a file instead.

    Args:
        ctx: The invocation, carrying the reporter.
        assignment: The settings to change.
        robot: The robot to change.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: The distribution being operated.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        preview: Report what this would do and do none of it.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        # Validated before anything is built that could open a connection.
        # REQ-053's scenario is that the robot is not contacted at all, and
        # this line is where that is decided.
        changes = _validated(_assignments(assignment))
        guard_secrets(reporter, changes)
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            _layout(application, daemon_unit, daemon_control, python),
            reporter,
        )
        code = execute_apply(
            "config set",
            daemon,
            changes,
            reporter,
            target.describe(),
            preview=preview,
            merge=True,
            close=close,
        )
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("config set", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


def _declaration(path: Path | None) -> Mapping[str, str]:
    """Read the declaration, insisting there is one.

    Args:
        path: Where it is, or `None`.

    Returns:
        The settings it declares.

    Raises:
        ConfigurationError: If no declaration was named.
    """
    if path is None:
        message = (
            f"no declaration: pass --declaration with a JSON document, or set "
            f"{DECLARATION_VARIABLE}"
        )
        raise ConfigurationError(message)
    return load_declaration(path)


# --- app ---------------------------------------------------------------------

application_commands = typer.Typer(
    name="app",
    help="Start and stop the application, and read what it is saying.",
    no_args_is_help=True,
)
app.add_typer(application_commands)


@application_commands.command("start")
def app_start(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    preview: PreviewOption = False,
) -> None:
    """Start the application, and confirm the robot reports it running.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: The distribution to start.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        preview: Report what this would do and do none of it.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            _layout(application, daemon_unit, daemon_control, python),
            reporter,
        )
        code = execute_start(
            daemon,
            reporter,
            target.describe(),
            preview=preview,
            close=close,
        )
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("app start", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


@application_commands.command("stop")
def app_stop(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    preview: PreviewOption = False,
) -> None:
    """Stop the application, and confirm the robot reports it stopped.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: The distribution to stop.
        daemon_unit: The systemd unit carrying the environment.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        preview: Report what this would do and do none of it.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            _layout(application, daemon_unit, daemon_control, python),
            reporter,
        )
        code = execute_stop(
            daemon,
            reporter,
            target.describe(),
            preview=preview,
            close=close,
        )
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("app stop", str(error), error.exit_code),
        ) from error
    raise typer.Exit(code)


@application_commands.command("logs")
def app_logs(
    ctx: typer.Context,
    robot: RobotOption = None,
    identity_file: IdentityOption = None,
    known_hosts: KnownHostsOption = None,
    sudo: SudoOption = True,
    application: ApplicationOption = DEFAULT_APPLICATION,
    daemon_unit: DaemonUnitOption = DEFAULT_DAEMON_UNIT,
    daemon_control: DaemonControlOption = DEFAULT_DAEMON_CONTROL,
    python: PythonOption = DEFAULT_PYTHON,
    lines: Annotated[
        int,
        typer.Option("--lines", min=0, help="How many past lines to show first."),
    ] = 50,
    follow: Annotated[
        bool,
        typer.Option("--follow", help="Keep the stream open and show new lines."),
    ] = False,
    since: Annotated[
        str,
        typer.Option(
            "--since",
            help="A journal time expression to start from, such as '-1h'.",
        ),
    ] = "",
) -> None:
    """Stream the robot's journal, filtered to the application.

    The filter is a journal field rather than a search of the text, so what
    comes back is what the application wrote and not every line that mentions
    it.

    Args:
        ctx: The invocation, carrying the reporter.
        robot: The robot.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        sudo: Whether privileged commands are elevated.
        application: Whose lines to show.
        daemon_unit: The systemd unit the application logs under.
        daemon_control: The daemon's application-control module.
        python: The fallback interpreter.
        lines: How many past lines to show first.
        follow: Whether to keep the stream open.
        since: Where to start from.

    Raises:
        typer.Exit: Always, carrying the exit status the run earned.
    """
    reporter = _reporter(ctx)
    try:
        target = _target(robot, identity_file, known_hosts, sudo)
        daemon, close = _connect(
            target,
            _layout(application, daemon_unit, daemon_control, python),
            reporter,
        )
        code = execute_logs(
            daemon,
            reporter,
            target.describe(),
            lines=lines,
            follow=follow,
            since=since,
            close=close,
        )
    except CommandError as error:
        raise typer.Exit(
            reporter.failure("app logs", str(error), error.exit_code),
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
