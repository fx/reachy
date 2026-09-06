"""Publish the committed application source as a Hugging Face Space.

The Reachy Mini daemon installs an application by downloading a Space and
running `uv pip install` over the directory it downloaded. `just wheels` builds
the wheel and the release workflow attaches it to a GitHub release;
`apps/ha-satellite/app-source/` is the directory that names that wheel, and this
is what puts the directory where the daemon can fetch it. Nothing about the
application is duplicated: the Space is metadata pointing at one artifact.

**Every refusal happens before the Space is created or written to**, and all but
one of them before anything at all is contacted. In the order they are decided:

- **No token.** Publishing needs one; the runbook says how to make it. Decided
  locally.
- **No target, or the wrong one.** The Space's repository name has to be the
  application's entry-point name. The daemon saves an installed application's
  metadata under the Space's name and reads it back under the entry-point name,
  so a Space called anything else installs and then loses its own metadata.
  Decided locally.
- **A source that does not agree with this checkout.** Publishing a source
  naming a version this repository has not released produces a Space that
  installs nothing, and the operator finds out on the robot. Decided locally,
  by reading the files.
- **A source that is not what is committed.** The whole claim this route rests
  on is that what a robot installs is reviewable in this repository, so a
  modified, deleted, staged or untracked file under the directory is refused.
  An *ignored* one is not — a `build/` or an `.egg-info/` left behind by
  installing the source locally is exactly what the deployment reference tells
  a maintainer to produce, and refusing over something that provably cannot
  reach the Space would be noise. What keeps it off the Space is the other
  half: the upload is an allow-list of the files git tracks, because
  `upload_folder` reads no `.gitignore` and would otherwise send the lot.
  Decided locally, by asking git.
- **A wheel from another repository.** The URL is required to name the releases
  of the repository this checkout publishes from, read from its `origin`
  remote. A version is not an identity: another repository's release under the
  same tag passes every other check here and installs somebody else's code on
  every robot that takes the Space.
- **A release that does not carry the wheel the source names.** This one *does*
  reach the network — a `HEAD` request to the release asset, and the redirect
  GitHub answers it with, followed as a `HEAD` so that nothing is ever
  downloaded. It is the only network any refusal touches. The alternative is a
  Space that downloads, builds, and then fails to resolve its one requirement,
  minutes later, on the robot.

There is no Hugging Face account, token or network in this repository's
development environment, so the refusals are what can be — and are — covered by
`scripts/tests/test_publish_app_source.py` without one: the local ones directly,
and the release-asset one through the opener it is handed. What cannot be covered
there is the upload itself, which is why it is the last thing this file does and
why `--dry-run` stops immediately before it.

`--dry-run` performs all of it, including asking the release for the wheel, and
stops before creating or writing to the Space.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Final, Protocol

from reachy_contracts import __version__

# Where the directory this publishes lives, relative to this script.
_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY: Final = _REPOSITORY_ROOT / "apps" / "ha-satellite" / "app-source"
APPLICATION_MANIFEST: Final = (
    _REPOSITORY_ROOT / "apps" / "ha-satellite" / "pyproject.toml"
)

# The entry-point group the daemon enumerates to find an installed application.
ENTRY_POINT_GROUP: Final = "reachy_mini_apps"

# Where the token is read from. Two names because `huggingface_hub` itself reads
# both, and an operator who has already exported one for its own command-line
# tool should not have to learn a third.
TOKEN_VARIABLES: Final = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")

# Where the target Space is read from. It is an environment variable rather than
# a committed default because it names somebody's account, and this repository
# is public — see the root `AGENTS.md`.
SPACE_VARIABLE: Final = "REACHY_APP_SPACE_ID"

# `owner/name`, both halves non-empty and neither carrying a separator or
# whitespace. Hugging Face accepts more than this in a repository name; what
# matters here is refusing the shapes that are obviously not a Space id before
# a request is made with one.
_SPACE_ID: Final = re.compile(r"\A(?P<owner>[^/\s]+)/(?P<name>[^/\s]+)\Z")

# A GitHub release asset, which is the only shape the requirement may take: the
# tag names the version and so does the wheel. Anything else — an index lookup,
# a branch archive, a file on somebody's machine — is a source that installs
# something this repository did not release.
_RELEASE_ASSET: Final = re.compile(
    r"\Ahttps://github\.com/(?P<repository>[^/\s]+/[^/\s]+)/releases/download/"
    r"v(?P<tag_version>[^/\s]+)/"
    r"reachy_mini_ha_satellite-(?P<wheel_version>[^-\s]+)-py3-none-any\.whl\Z",
)

# A git remote, in the two spellings a checkout of a GitHub repository has. The
# `owner/name` it yields is what the released wheel has to come from — see
# `check_agrees_with_repository`.
_REMOTE: Final = re.compile(
    r"\A(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
    r"(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?/?\Z",
)

# How long git is given to answer a question about this checkout. Generous: it
# is reading an index, and a bound only exists so a wedged command fails rather
# than hanging a publish.
_GIT_TIMEOUT_SECONDS: Final = 30

# The one status that means the release carries the wheel. A redirect is not
# it — `KeepHeadOnRedirect` follows those, as `HEAD` — so anything else arriving
# here is the asset being somewhere other than where the source says.
_FOUND: Final = 200

# The requirement's `name @ url` form, split rather than parsed with a
# dependency library: this script runs from the workspace environment and has no
# use for one, and the shape being this narrow is itself part of what is checked.
_REQUIREMENT: Final = re.compile(r"\A(?P<name>[A-Za-z0-9._-]+) @ (?P<url>\S+)\Z")


class PublishRefusalError(Exception):
    """The publish was refused, with a reason an operator can act on."""


class Opener(Protocol):
    """Answers whether a URL is there, without reading it.

    A protocol so the release-asset check is exercised without a network: the
    real implementation asks with `HEAD` and reads no body, and a test supplies
    a function that returns a status.
    """

    def __call__(self, url: str, /) -> int:
        """Return the status the URL answered with."""


@dataclass(frozen=True)
class AppSource:
    """What the committed application source says it installs."""

    version: str
    """The version the source's own project metadata declares."""

    wheel_url: str
    """The release asset the source's one requirement names."""


def entry_point_name(manifest: Path) -> str:
    """Read the application's single `reachy_mini_apps` entry-point name.

    The Space's repository name has to be this, so it is read from the
    application's manifest rather than repeated here — a rule keyed to a copy of
    the value it is about is a rule that stops applying the day the value moves.
    """
    declared = tomllib.loads(manifest.read_text(encoding="utf-8"))
    entry_points = declared.get("project", {}).get("entry-points", {})
    names = list(entry_points.get(ENTRY_POINT_GROUP, {}))
    if len(names) != 1:
        raise PublishRefusalError(
            f"{manifest} declares {len(names)} {ENTRY_POINT_GROUP} entry points, "
            f"and the Space's name has to be the one the daemon will list",
        )
    return str(names[0])


def resolve_token(environ: dict[str, str]) -> str:
    """Read the Hugging Face token, or refuse saying how to supply one."""
    for variable in TOKEN_VARIABLES:
        token = environ.get(variable, "").strip()
        if token:
            return token
    names = " or ".join(TOKEN_VARIABLES)
    raise PublishRefusalError(
        f"no Hugging Face token: set {names} to a token with write access to the "
        f"Space. Nothing is contacted without one",
    )


def resolve_space_id(environ: dict[str, str], expected_name: str) -> str:
    """Read the target Space, or refuse saying what a usable one looks like."""
    space_id = environ.get(SPACE_VARIABLE, "").strip()
    if not space_id:
        raise PublishRefusalError(
            f"no target Space: set {SPACE_VARIABLE} to <owner>/{expected_name}. "
            f"It is not committed because it names an account and this "
            f"repository is public",
        )
    match = _SPACE_ID.fullmatch(space_id)
    if match is None:
        raise PublishRefusalError(
            f"{SPACE_VARIABLE} is {space_id!r}, which is not <owner>/<name>",
        )
    if match["name"] != expected_name:
        raise PublishRefusalError(
            f"{SPACE_VARIABLE} names the Space {match['name']!r}, and it has to "
            f"be {expected_name!r}: the daemon saves an installed application's "
            f"metadata under the Space's name and reads it back under the "
            f"entry-point name, so any other name installs and then cannot find "
            f"what it recorded",
        )
    return space_id


def read_source(directory: Path) -> AppSource:
    """Read what the committed source declares, or refuse saying what is wrong."""
    manifest = directory / "pyproject.toml"
    try:
        declared = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PublishRefusalError(
            f"there is no application source at {manifest}"
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise PublishRefusalError(
            f"{manifest} is not readable TOML: {error}"
        ) from error

    project = declared.get("project", {})
    version = project.get("version", "")
    requirements = project.get("dependencies", [])
    if len(requirements) != 1:
        raise PublishRefusalError(
            f"{manifest} declares {len(requirements)} requirements, and the "
            f"source's whole job is to name exactly one released wheel",
        )
    match = _REQUIREMENT.fullmatch(requirements[0])
    if match is None:
        raise PublishRefusalError(
            f"{manifest} declares the requirement {requirements[0]!r}, which is "
            f"not the `<name> @ <url>` form a release asset takes",
        )
    return AppSource(version=version, wheel_url=match["url"])


def check_agrees_with_repository(
    source: AppSource,
    version: str,
    repository: str,
) -> str:
    """Refuse a source that names anything but this checkout's released wheel.

    Returns the version everything agreed on, so a caller reports one value
    rather than choosing between three.
    """
    if source.version != version:
        raise PublishRefusalError(
            f"the application source declares version {source.version!r} and "
            f"this repository is at {version!r}. Release automation moves both; "
            f"a source that has drifted from the wheel it names installs the "
            f"wrong one",
        )
    asset = _RELEASE_ASSET.fullmatch(source.wheel_url)
    if asset is None:
        raise PublishRefusalError(
            f"the application source names {source.wheel_url}, which is not a "
            f"release asset of the form "
            f"https://github.com/<owner>/<repository>/releases/download/"
            f"v<version>/reachy_mini_ha_satellite-<version>-py3-none-any.whl",
        )
    named = {asset["tag_version"], asset["wheel_version"]}
    if named != {version}:
        raise PublishRefusalError(
            f"the application source names the release tag v{asset['tag_version']} "
            f"and the wheel {asset['wheel_version']}, and this repository is at "
            f"{version}. All three move together",
        )
    if asset["repository"] != repository:
        raise PublishRefusalError(
            f"the application source installs a wheel from "
            f"{asset['repository']}, and this checkout releases from "
            f"{repository}. A version is not an identity: another repository's "
            f"release under the same tag would install somebody else's code on "
            f"every robot that took this Space",
        )
    return version


class KeepHeadOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect with the method the original request used.

    `HTTPRedirectHandler.redirect_request` builds the follow-up request without
    a method, so `urlopen` retries a redirected `HEAD` as a `GET`. A GitHub
    release asset is *always* a redirect to a content host, so without this the
    question "is that wheel published" would answer itself by downloading the
    wheel — several megabytes, from a `--dry-run`, in a command whose whole
    contract is that it reads nothing.

    `test_publish_app_source.py` pins both halves: that this preserves the
    method, and that the base class does not. If the second ever fails, the
    standard library has fixed it and this class can go.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Return the base class's follow-up request, with the method restored."""
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.method = req.get_method()
        return redirected


# Built once. `build_opener` drops the default redirect handler in favour of a
# subclass of it, which is exactly what this is.
_OPENER: Final = urllib.request.build_opener(KeepHeadOnRedirect())


def _head(url: str) -> int:
    """Ask a URL for its status, following redirects as `HEAD`, and read nothing.

    The suppression below is the audited-URL rule, and what audits this one is
    `check_agrees_with_repository`: nothing reaches here that `_RELEASE_ASSET`
    has not already matched, so the scheme is `https` and the host is
    `github.com` by construction rather than by trust.
    """
    request = urllib.request.Request(url, method="HEAD")  # noqa: S310  # see above
    with _OPENER.open(request, timeout=30) as answer:
        status: int = answer.status
        return status


def check_release_asset(url: str, opener: Opener = _head) -> None:
    """Refuse to publish a source pointing at a wheel that is not published yet.

    The order this catches matters more than it looks. Releasing and publishing
    are two commands run by a person, and the wrong order produces a Space that
    downloads, builds and then fails to resolve its one requirement — on the
    robot, minutes later, with the reason buried in an install log.
    """
    try:
        status = opener(url)
    except urllib.error.HTTPError as error:
        raise PublishRefusalError(
            f"{url} answered {error.code}: the release this source names does "
            f"not carry that wheel yet. Publish the Space after the release, "
            f"not before",
        ) from error
    except OSError as error:
        raise PublishRefusalError(f"{url} could not be reached: {error}") from error
    if status != _FOUND:
        raise PublishRefusalError(f"{url} answered {status} rather than 200")


def repository_from_remote(remote_url: str) -> str:
    """The `owner/name` a git remote URL points at, in either spelling.

    What the published source installs has to come from the releases of the
    repository it is published from. Deriving that from the checkout rather than
    hard-coding it means a fork publishes its own wheel and this stays one rule
    rather than a literal in two files.
    """
    match = _REMOTE.fullmatch(remote_url.strip())
    if match is None:
        raise PublishRefusalError(
            f"the `origin` remote is {remote_url.strip()!r}, which is not a "
            f"GitHub repository this can read an owner and a name out of. The "
            f"released wheel has to come from the repository this publishes "
            f"from, and that is what says which one it is",
        )
    return match["repository"]


def uncommitted_changes(status: str, directory: Path) -> None:
    """Refuse a source that is not what this checkout has committed.

    `git status --porcelain` over the directory, handed in rather than run here
    so the judgement is testable. Anything in it — a modification, a deletion, a
    staged-but-uncommitted addition, an untracked file — means the bytes on disk
    are not the bytes anybody reviewed, and this whole route rests on those
    being the same thing.

    What that command does NOT report is an ignored file, and that is deliberate
    rather than overlooked: a `build/` or an `.egg-info/` is what installing the
    source locally leaves behind, which the deployment reference asks a
    maintainer to do, and it cannot reach the Space because the upload is an
    allow-list of tracked files. Refusing over it would be a refusal about
    something harmless.
    """
    if status.strip():
        raise PublishRefusalError(
            f"{directory} has uncommitted changes, and what is published has to "
            f"be what is committed — that is what makes the Space reviewable "
            f"here:\n{status.rstrip()}",
        )


def committed_names(listed: str, directory: Path) -> list[str]:
    """The tracked files under the source, relative to it and sorted.

    `git ls-files -z` output, handed in for the same reason. These become the
    upload's allow-list, which is the half of "the Space is what is committed"
    that `uncommitted_changes` does not cover: it says nothing about an ignored
    file, and `upload_folder` does not read `.gitignore`, so a `build/` or an
    `.egg-info/` left behind by somebody installing the source locally would
    otherwise be published to a public Space along with it.
    """
    within = directory.relative_to(_REPOSITORY_ROOT)
    names = sorted(
        str(Path(path).relative_to(within)) for path in listed.split("\0") if path
    )
    if not names:
        raise PublishRefusalError(f"git tracks no file under {directory}")
    return names


def _git(*arguments: str) -> str:
    """Run one read-only git command in this checkout and return its output."""
    completed = subprocess.run(  # noqa: S603  # a fixed argument vector of this module's own literals and paths it computed itself; no shell, and nothing a caller supplies reaches it
        ["git", "-C", str(_REPOSITORY_ROOT), *arguments],  # noqa: S607  # `git` is taken from PATH deliberately: this runs inside a checkout, where the git that made it is the right one
        capture_output=True,
        check=False,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise PublishRefusalError(
            f"`git {' '.join(arguments)}` failed: {completed.stderr.strip()}",
        )
    return completed.stdout


def check_source_is_committed(directory: Path) -> list[str]:
    """Refuse anything but the committed source, and say what that is."""
    uncommitted_changes(_git("status", "--porcelain", "--", str(directory)), directory)
    return committed_names(_git("ls-files", "-z", "--", str(directory)), directory)


def publish(
    space_id: str,
    token: str,
    directory: Path,
    version: str,
    names: list[str],
) -> None:
    """Create the Space if it is not there and make it the committed source.

    `huggingface_hub` is imported here rather than at module level so that every
    refusal above runs in an environment without it — which is every environment
    that has not asked for the `publish` dependency group.
    """
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(
        repo_id=space_id,
        repo_type="space",
        space_sdk="static",
        exist_ok=True,
    )
    # Two halves of one property. `allow_patterns` is what stops anything the
    # directory happens to hold — a build tree, an editor's backup, anything
    # untracked — from reaching a public Space, because `upload_folder` reads no
    # `.gitignore` and would otherwise send the lot. `delete_patterns` makes the
    # Space exactly that set rather than the union of every publish, so a file
    # withdrawn here is withdrawn there.
    api.upload_folder(
        folder_path=str(directory),
        repo_id=space_id,
        repo_type="space",
        allow_patterns=names,
        delete_patterns="*",
        commit_message=f"Publish the Reachy Mini HA satellite application source {version}",
    )


def main(argv: list[str]) -> int:
    """Validate, then publish, reporting a refusal as one actionable line."""
    parser = argparse.ArgumentParser(
        prog="publish-app-source",
        description="Publish the committed application source as a Hugging Face Space.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check, including asking the release for the wheel, and "
        "stop before creating or writing to the Space",
    )
    arguments = parser.parse_args(argv)

    environ = dict(os.environ)
    try:
        expected_name = entry_point_name(APPLICATION_MANIFEST)
        token = resolve_token(environ)
        space_id = resolve_space_id(environ, expected_name)
        source = read_source(SOURCE_DIRECTORY)
        repository = repository_from_remote(_git("remote", "get-url", "origin"))
        version = check_agrees_with_repository(source, __version__, repository)
        names = check_source_is_committed(SOURCE_DIRECTORY)
        check_release_asset(source.wheel_url)
    except PublishRefusalError as refusal:
        sys.stderr.write(f"publish-app-source: {refusal}\n")
        return 1

    if arguments.dry_run:
        print(
            f"publish-app-source: {SOURCE_DIRECTORY.name} at {version} would be "
            f"published to https://huggingface.co/spaces/{space_id} as "
            f"{', '.join(names)}, installing {source.wheel_url}",
        )
        return 0

    publish(space_id, token, SOURCE_DIRECTORY, version, names)
    print(
        f"publish-app-source: published {version} to "
        f"https://huggingface.co/spaces/{space_id}. Install it on a robot with "
        f"the Space id and no shell — see docs/ops/satellite-deployment.md",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main(sys.argv[1:]))
