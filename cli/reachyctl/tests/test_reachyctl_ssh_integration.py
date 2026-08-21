"""The robot's remote-access transport, driven against a real SSH server.

Every test here opens a socket and says so with `@pytest.mark.enable_socket`.
The reason is the standing rule that an integration test exercises a real
transport in-process rather than mocking it: `asyncssh` ships a server as well
as a client, so the thing under test is a real SSH session — a real key
exchange, a real authentication, a real channel — rather than a description of
one.

That matters more here than it usually would. There is no robot in this
repository, so everything above this layer is exercised against a fake robot;
this module is the one place the transport itself is real. What it does *not*
prove is anything about the robot: the server below executes whatever it is
sent, so `echo` really runs, but it is not a Reachy Mini daemon — the commands
this tool composes and the answers it reads back are still pending the hardware
session.

The host key and the client key are generated in the test process and never
leave it. Nothing here is anybody's credential; see the root `AGENTS.md`.

One helper below reads `SshAccess._connection` directly. What it needs is a
link that is already open and then abruptly is not, which is what a robot losing
power looks like from here, and no public call produces that without also
closing the link tidily.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

import asyncssh
import pytest
import pytest_asyncio

from reachyctl.robot import RobotAccessError, RobotTarget
from reachyctl.ssh import SshAccess, _text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# Loopback only. Nothing here listens on a routable address, and the port is
# ephemeral so two runs on one machine cannot collide.
# Every test in this module writes real files — the generated keys and the
# known-hosts file the fixture below produces — so the whole module declares it.
# The marker grants nothing: it says these are not unit tests, which they are
# not, and `enable_socket` says the same about the socket each of them opens.
pytestmark = pytest.mark.filesystem

HOST: Final = "127.0.0.1"

ACCOUNT: Final = "operator"


class _Server(asyncssh.SSHServer):
    """A server that accepts one public key and nothing else."""

    def __init__(self, authorised: asyncssh.SSHKey) -> None:
        """Remember which key is allowed.

        Args:
            authorised: The public key to accept.
        """
        self._authorised = authorised

    def begin_auth(self, username: str) -> bool:
        """Require authentication for every account.

        Args:
            username: Who is connecting.

        Returns:
            True, meaning authentication is required.
        """
        del username
        return True

    def public_key_auth_supported(self) -> bool:
        """Offer public-key authentication.

        Returns:
            True.
        """
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        """Accept the one key this server was built with.

        Args:
            username: Who is connecting.
            key: What they offered.

        Returns:
            True when it is the expected key.
        """
        del username
        return bool(key == self._authorised)


async def _run_command(process: asyncssh.SSHServerProcess[str]) -> None:
    """Run what the client asked for, and hand back its status and streams.

    The server side really does execute the command, so the exit status and the
    two streams this test asserts on are a process's rather than a script's
    idea of one. It is not a robot: the commands below are `echo` and `cat`, and
    what is being exercised is this repository's transport.

    Args:
        process: The channel the client opened.
    """
    if process.command is None:
        process.exit(1)
        return
    try:
        child = await asyncio.create_subprocess_exec(
            *shlex.split(process.command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        process.stderr.write(f"{error}\n")
        process.exit(127)
        return
    out, err = await child.communicate()
    process.stdout.write(out.decode("utf-8", errors="replace"))
    process.stderr.write(err.decode("utf-8", errors="replace"))
    process.exit(child.returncode if child.returncode is not None else 1)


@pytest_asyncio.fixture
async def robot(tmp_path: Path) -> AsyncIterator[RobotTarget]:
    """Run a real SSH server on the loopback interface for one test.

    The server executes what it is sent, so a command's status and its two
    streams really are a process's. What is being exercised is this
    repository's transport, not a robot.

    Args:
        tmp_path: Where the generated keys and the known-hosts file are written.

    Yields:
        A target addressing the running server.
    """
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    identity = tmp_path / "identity"
    identity.write_bytes(client_key.export_private_key())
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"

    server = await asyncssh.create_server(
        lambda: _Server(client_key.convert_to_public()),
        HOST,
        0,
        server_host_keys=[host_key],
        process_factory=_run_command,
        sftp_factory=True,
    )
    port = next(iter(server.sockets)).getsockname()[1]
    known_hosts.write_text(
        f"[{HOST}]:{port} {host_key.export_public_key().decode().strip()}\n",
        encoding="utf-8",
    )
    try:
        yield RobotTarget(
            host=HOST,
            user=ACCOUNT,
            port=port,
            identity_file=identity,
            known_hosts=known_hosts,
            elevate=False,
            timeout=10.0,
        )
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.enable_socket(reason="the SSH transport is the thing under test")
@pytest.mark.asyncio
async def test_a_command_that_succeeds_comes_back_with_its_output(
    robot: RobotTarget,
) -> None:
    """A real session, a real channel, and a real exit status.

    Args:
        robot: The running server.
    """
    access = SshAccess(robot)
    try:
        outcome = await access.run(["echo", "hello from the robot"])
    finally:
        await access.aclose()

    assert outcome.ok is True
    assert outcome.stdout.strip() == "hello from the robot"
    assert outcome.command == "echo 'hello from the robot'"


@pytest.mark.enable_socket(reason="the SSH transport is the thing under test")
@pytest.mark.asyncio
async def test_a_command_that_fails_is_an_outcome_rather_than_an_exception(
    robot: RobotTarget,
) -> None:
    """Most non-zero statuses are answers, which is why they are not raised.

    Args:
        robot: The running server.
    """
    access = SshAccess(robot)
    try:
        outcome = await access.run(["cat", "/this/path/is/not/there"])
    finally:
        await access.aclose()

    assert outcome.ok is False
    assert outcome.exit_status != 0
    assert "No such file" in outcome.complaint()


@pytest.mark.enable_socket(reason="the SSH transport is the thing under test")
@pytest.mark.asyncio
async def test_an_argument_containing_a_space_stays_one_argument(
    robot: RobotTarget,
) -> None:
    """Quoted once, by the transport, which is why a command is a list here.

    Args:
        robot: The running server.
    """
    access = SshAccess(robot)
    try:
        outcome = await access.run(["printf", "%s\\n", "one argument with spaces"])
    finally:
        await access.aclose()

    assert outcome.stdout.strip() == "one argument with spaces"


@pytest.mark.enable_socket(reason="the SFTP transfer is the thing under test")
@pytest.mark.asyncio
async def test_bytes_are_transferred_and_read_back_unchanged(
    robot: RobotTarget,
    tmp_path: Path,
) -> None:
    """A wheel is transferred whole, so the transfer has to be byte-exact.

    Args:
        robot: The running server.
        tmp_path: Somewhere on this machine the server can write, since the
            server is this machine.
    """
    payload = bytes(range(256)) * 8
    destination = tmp_path / "transferred.bin"

    access = SshAccess(robot)
    try:
        await access.upload(payload, PurePosixPath(destination))
    finally:
        await access.aclose()

    assert destination.read_bytes() == payload


@pytest.mark.enable_socket(reason="the SSH transport is the thing under test")
@pytest.mark.asyncio
async def test_output_arrives_a_line_at_a_time_while_the_command_runs(
    robot: RobotTarget,
) -> None:
    """Which is what `app logs --follow` is, and what a stub could not show.

    Args:
        robot: The running server.
    """
    access = SshAccess(robot)
    try:
        lines = [
            line
            async for line in access.stream(["printf", "%s\\n%s\\n", "first", "second"])
        ]
    finally:
        await access.aclose()

    assert lines[:2] == ["first", "second"]


@pytest.mark.enable_socket(reason="the refusal is the thing under test")
@pytest.mark.asyncio
async def test_a_host_key_that_does_not_verify_is_refused(
    robot: RobotTarget,
    tmp_path: Path,
) -> None:
    """There is no option that turns this off, so the refusal is the whole behaviour.

    Args:
        robot: The running server.
        tmp_path: Where a known-hosts file naming the wrong key is written.
    """
    other = asyncssh.generate_private_key("ssh-ed25519")
    wrong = tmp_path / "wrong_known_hosts"
    wrong.write_text(
        f"[{HOST}]:{robot.port} {other.export_public_key().decode().strip()}\n",
        encoding="utf-8",
    )
    access = SshAccess(
        RobotTarget(
            host=robot.host,
            user=robot.user,
            port=robot.port,
            identity_file=robot.identity_file,
            known_hosts=wrong,
            elevate=False,
            timeout=10.0,
        ),
    )

    with pytest.raises(RobotAccessError, match="cannot reach the robot"):
        await access.run(["true"])
    await access.aclose()


@pytest.mark.enable_socket(reason="the refusal is the thing under test")
@pytest.mark.asyncio
async def test_a_key_the_robot_does_not_accept_is_refused(
    robot: RobotTarget,
    tmp_path: Path,
) -> None:
    """And the message names the robot rather than quoting anything read from it.

    Args:
        robot: The running server.
        tmp_path: Where the wrong private key is written.
    """
    other = tmp_path / "other_identity"
    other.write_bytes(asyncssh.generate_private_key("ssh-ed25519").export_private_key())
    other.chmod(0o600)
    access = SshAccess(
        RobotTarget(
            host=robot.host,
            user=robot.user,
            port=robot.port,
            identity_file=other,
            known_hosts=robot.known_hosts,
            elevate=False,
            timeout=10.0,
        ),
    )

    with pytest.raises(RobotAccessError) as raised:
        await access.run(["true"])
    await access.aclose()

    assert f"{ACCOUNT}@{HOST}:{robot.port}" in str(raised.value)


@pytest.mark.enable_socket(reason="the refusal is the thing under test")
@pytest.mark.asyncio
async def test_a_robot_that_is_not_listening_is_unreachable_rather_than_a_traceback(
    tmp_path: Path,
) -> None:
    """A closed port is the ordinary way a robot is off, and it costs UNREACHABLE.

    Args:
        tmp_path: Where an empty known-hosts file is written.
    """
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    # A port nothing is listening on: bound, read, and released before the
    # attempt, so the connection is refused rather than answered by something
    # else that happened to be there.
    listener = await asyncio.start_server(lambda _r, _w: None, HOST, 0)
    port = next(iter(listener.sockets)).getsockname()[1]
    listener.close()
    await listener.wait_closed()

    access = SshAccess(
        RobotTarget(
            host=HOST,
            user=ACCOUNT,
            port=port,
            known_hosts=known_hosts,
            elevate=False,
            timeout=5.0,
        ),
    )

    with pytest.raises(RobotAccessError, match="cannot reach the robot"):
        await access.run(["true"])
    await access.aclose()


@pytest.mark.enable_socket(reason="the SSH transport is the thing under test")
@pytest.mark.asyncio
async def test_one_connection_serves_every_command_and_is_let_go_once(
    robot: RobotTarget,
) -> None:
    """An SSH handshake is several round trips on a link measured in hundreds of ms.

    Args:
        robot: The running server.
    """
    access = SshAccess(robot)
    try:
        first = await access.run(["echo", "one"])
        second = await access.run(["echo", "two"])
    finally:
        await access.aclose()

    assert first.stdout.strip() == "one"
    assert second.stdout.strip() == "two"
    # Closing twice is what a command that failed part way through does.
    await access.aclose()


def test_a_stream_read_as_bytes_is_decoded_leniently() -> None:
    """A robot's output on its way into a message, not a place to raise.

    `asyncssh` types a stream as text or bytes depending on the encoding
    configured, and this client configures one — but a byte sequence that is not
    valid UTF-8 should cost a replacement character rather than an exception on
    top of whatever already went wrong.
    """
    assert _text(b"already bytes") == "already bytes"
    assert _text(b"\xff not utf-8") == "� not utf-8"
    assert _text(None) == ""


async def _broken(robot: RobotTarget) -> SshAccess:
    """Open a link and then break it, the way losing power breaks one.

    Reaching into the private attribute is deliberate: the whole point is a
    link that is already open and then is not, and no public call does that
    without also closing it tidily.

    Args:
        robot: The running server.

    Returns:
        The access, with its connection aborted.
    """
    access = SshAccess(robot)
    await access.connect()
    connection = access._connection
    assert connection is not None
    connection.abort()
    await connection.wait_closed()
    return access


@pytest.mark.enable_socket(reason="the SSH transport is the thing under test")
@pytest.mark.asyncio
async def test_a_command_in_flight_when_the_link_goes_is_unreachable(
    robot: RobotTarget,
) -> None:
    """Rather than an `asyncssh` exception reaching the command surface.

    Args:
        robot: The running server.
    """
    access = await _broken(robot)

    with pytest.raises(RobotAccessError, match=ACCOUNT):
        await access.run(["true"])

    await access.aclose()


@pytest.mark.enable_socket(reason="the SFTP transfer is the thing under test")
@pytest.mark.asyncio
async def test_a_transfer_in_flight_when_the_link_goes_is_unreachable(
    robot: RobotTarget,
    tmp_path: Path,
) -> None:
    """The transfer is the longest step of a deploy, so it is the likeliest one.

    Args:
        robot: The running server.
        tmp_path: Somewhere to aim a transfer that will not happen.
    """
    access = await _broken(robot)

    with pytest.raises(RobotAccessError, match=ACCOUNT):
        await access.upload(b"never sent", PurePosixPath(tmp_path / "never"))

    await access.aclose()


@pytest.mark.enable_socket(reason="the SSH transport is the thing under test")
@pytest.mark.asyncio
async def test_a_stream_open_when_the_link_goes_is_unreachable(
    robot: RobotTarget,
) -> None:
    """`app logs --follow` holds one open for as long as an operator watches.

    Args:
        robot: The running server.
    """
    access = await _broken(robot)

    with pytest.raises(RobotAccessError, match=ACCOUNT):
        async for _line in access.stream(["true"]):
            pass  # pragma: no cover - the stream never yields; it raises

    await access.aclose()
