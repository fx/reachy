"""The settings interface: read every setting, change every setting, no shell.

ha-satellite REQ-049 asks for two things that pull in opposite directions. Every
operator-facing setting has to be changeable here, and a setting marked secret
has to be *reportable* without being *readable* — so the page is usable for
rotating a credential and useless for learning one.

The resolution is the same one `config.py` uses everywhere else: a secret is
turned into `<set>` or `<unset>` by `resolved_configuration` before it reaches
anything that *renders*, its input field is submitted empty by a browser nobody
typed into, and an empty submission means "leave it alone". Unsetting one
therefore needs a control that says so, which is the `clear.` checkbox beside it.

**This module does handle the raw value**, and it is worth stating plainly rather
than claiming a purity it has not got. `base_form_values` asks `canonical_string`
for the layer below and gets a secret back unchanged, and `_overrides_from`
compares a submission against it. Both are comparisons, and they are what stops
an unchanged credential being re-pinned as an override and what makes rotation
mean rotation. The value goes on to `OverrideStore.save` and nowhere else — never
to a template, a log line, a redirect or a response. `web/render.py` is where
rendering happens, and it never receives a secret at all: `form_value` returns
the empty string for one before it reads the field.

**Where a change goes.** Into an overrides file in the application's state
directory, which is outside the wheel — so reinstalling the application keeps
it, and re-imaging the robot does not. Overrides sit *above* the environment
rather than below it, and that is what makes REQ-049 true rather than
approximately true: a layer the environment overrode would silently ignore a
change to any setting anybody had ever exported. The page shows which layer each
value came from, and saving a value back to what the environment says removes the
override rather than pinning a duplicate of it.

**What a change does.** The settings in `config.LIVE_SETTINGS` are swapped into
the running application; the rest are read while something is being built — a
socket bound, a session opened, a detector loaded, an identity announced — so
they take effect at the next start. The page says which is which per setting,
and offers to stop the application rather than pretending.

**Stopping is not restarting, and the page says so.** The daemon marks a
cleanly-exited application `done` and leaves it stopped; nothing relaunches it.
So the button stops the satellite and the operator starts it again from the
daemon's own dashboard — which is a web interface, so REQ-049's "without a
shell" holds either way. Claiming a restart the daemon does not perform would
leave an operator looking at a robot that had gone quiet.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from reachy_mini_ha_satellite.config import (
    BOOTSTRAP_SETTINGS,
    LIVE_SETTINGS,
    SECRET_SETTINGS,
    ConfigurationError,
    Resolution,
    Settings,
    canonical_string,
    configuration_report,
    load_settings,
    resolved_configuration,
    setting_names,
    variable_for,
)
from reachy_mini_ha_satellite.web.render import CLEAR_PREFIX, render_settings_page

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from reachy_mini_ha_satellite.config import OverrideStore

__all__ = ["SettingsHost", "base_form_values", "create_app"]

# What a form submission is allowed to be. A settings page is not an upload
# endpoint, and a body larger than this is either a mistake or an attempt.
_MAX_BODY_BYTES: Final = 64 * 1024

# What `Sec-Fetch-Site` may say for a request that changes something. A browser
# sets it on every request it makes and never lets a page forge it, so it is the
# reliable half of the check below; `none` is what a typed address or a bookmark
# produces.
_OWN_ORIGIN: Final[frozenset[str]] = frozenset({"same-origin", "none"})


class SettingsHost(Protocol):
    """What the settings interface needs from the running application.

    Three methods rather than the application itself, and deliberately: `web/`
    knowing about `main.py` would make the two mutually importable, and this
    states exactly what a settings page is allowed to do to a running robot.
    """

    def status(self) -> dict[str, object]:
        """Say what the robot is doing.

        Returns:
            The pipeline state and why the head is where it is.
        """
        ...

    def apply_live(self, settings: Settings) -> None:
        """Adopt the settings that can be changed without a restart.

        Args:
            settings: The newly resolved settings.
        """
        ...

    def request_stop(self) -> None:
        """Ask the application to shut down. It is not started again from here."""
        ...


def _default_string(name: str) -> str:
    """Render a setting's own default the way a form field would carry it.

    Args:
        name: Which setting.

    Returns:
        The default as a string, or the empty string for the one setting that
        has no default and for every secret — whose value is never rendered.
    """
    if name in SECRET_SETTINGS:
        return ""
    default = Settings.model_fields[name].default
    if default is None or repr(default) == "PydanticUndefined":
        return ""
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(getattr(default, "value", default))


def base_form_values(environ: Mapping[str, str]) -> dict[str, str]:
    """Say what every setting would be without the overrides.

    This is what a submission is compared against, and it is what makes
    "changing a value back to what the environment says" remove the override
    rather than pin a second copy of it.

    Args:
        environ: The environment the application was started with.

    Returns:
        Setting name to the string the layers below the overrides supply.
    """
    values: dict[str, str] = {}
    for name in setting_names():
        variable = variable_for(name)
        raw = environ.get(variable, _default_string(name))
        # Canonical, because the form renders each field from the *parsed*
        # settings. Without this a variable written `TRUE`, `09000` or `0.10`
        # differs from what the browser submits for it, and saving one unrelated
        # setting would pin an override for every such value — after which the
        # environment could no longer change them at all.
        values[name] = canonical_string(name, raw)
    return values


def _from_this_page(request: Request) -> bool:
    """Whether a state-changing request came from this interface's own page.

    **This is not authentication and does not pretend to be.** The interface is
    unauthenticated, deliberately and in company: the ESPHome API this
    application serves announces `uses_password=False`, and the daemon's own
    dashboard is reachable by anything that can reach the robot. The trust
    boundary is the network the robot is on, and the deployment runbook says so
    rather than leaving an operator to infer it.

    What this *does* close is the one exposure that does not need a peer on that
    network at all: any page an operator's browser visits can submit a form to
    any address that browser can reach, so without this check a web page
    anywhere could stop the robot or replace its groundstation credential the
    moment somebody with a laptop on the same network opened it. A request that
    a browser says came from somewhere else is refused.

    Two signals, in order of reliability. `Sec-Fetch-Site` is set by the browser
    and cannot be forged by a page; `Origin` is the fallback for one that does
    not send it. A request carrying neither is not from a browser — `curl`, a
    script, a deployment check — and is allowed, because a cross-site form post
    is precisely the thing that cannot happen without a browser attaching one.

    Args:
        request: What arrived.

    Returns:
        True when the request may change something.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site.lower() in _OWN_ORIGIN

    origin = request.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.headers.get("host", "")


def _submitted(body: bytes) -> dict[str, str]:
    """Parse a form submission.

    Parsed here rather than through the framework's own form support, which
    reaches for a multipart parser this application has no other use for. A
    settings form is `application/x-www-form-urlencoded` and nothing else.

    Args:
        body: The request body.

    Returns:
        Field name to value, with blank values kept — a blank password field
        is the whole of "leave this secret alone".
    """
    return dict(
        parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    )


def _names_from(query: str, key: str) -> tuple[str, ...]:
    """Read a list of setting names out of a redirect's query string.

    Filtered against the real settings rather than trusted, so that a
    hand-written query cannot put arbitrary text on the page.

    Args:
        query: The raw query string.
        key: Which parameter to read.

    Returns:
        The names, in declaration order.
    """
    known = set(setting_names())
    wanted: set[str] = set()
    for parameter, value in parse_qsl(query):
        if parameter == key:
            wanted.update(part for part in value.split(",") if part in known)
    return tuple(name for name in setting_names() if name in wanted)


#:= docs/specs/ha-satellite/index.md#req-049-settings-are-changeable-without-a-shell
#:% Every operator-facing setting MUST be changeable through the application's own
#:% web interface, and MUST be readable there except where the setting is marked
#:% secret, which is reported as set or unset without its value.
#
#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
def create_app(
    *,
    resolution: Resolution,
    store: OverrideStore,
    application: SettingsHost | None = None,
    environ: Mapping[str, str] | None = None,
) -> Starlette:
    """Build the settings interface.

    Args:
        resolution: The settings in effect at startup, and where each came
            from.
        store: Where a change is written.
        application: The running application, told about a change it can adopt
            and asked to stop for one it cannot. `None` serves a page that
            reads and writes but cannot stop anything, which is what a test
            of the rendering wants.
        environ: The environment the application was started with. Defaults to
            the process environment.

    Returns:
        The application, ready to be served.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    current = _Current(resolution)

    async def index(request: Request) -> Response:
        """Serve the settings page.

        Args:
            request: The request, whose query string carries what the last
                submission did.

        Returns:
            The page.
        """
        query = request.url.query
        return HTMLResponse(
            _page(
                current.resolution,
                store,
                application,
                saved=_names_from(query, "saved"),
                restart_needed=_names_from(query, "restart"),
            ),
        )

    async def save(request: Request) -> Response:
        """Apply a submission, or explain why none of it was applied.

        Args:
            request: The form submission.

        Returns:
            A redirect on success, or the page again with the refusal on it.
        """
        if not _from_this_page(request):
            return _refuse_cross_site()
        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            return PlainTextResponse("that is too large to be a settings form", 413)
        fields = _submitted(body)

        base = base_form_values(source)
        try:
            previous = store.load()
            wanted = _overrides_from(fields, base=base, previous=previous)
            resolved = load_settings(source, wanted)
            store.save(wanted)
        except ConfigurationError as error:
            # Every way this can refuse ends here, and every one of them ends
            # with the operator reading why rather than a traceback. A file that
            # cannot be written is the one worth naming: a change that appears
            # to have been accepted and was not is the worst outcome available.
            return HTMLResponse(
                _page(current.resolution, store, application, error=str(error)),
                status_code=400,
            )

        changed = tuple(
            name
            for name in setting_names()
            if previous.get(name, base[name]) != wanted.get(name, base[name])
        )
        current.resolution = resolved
        if application is not None:
            application.apply_live(resolved.settings)

        return _redirect_after(changed)

    async def reset(request: Request) -> Response:
        """Discard every override and go back to the environment.

        Args:
            request: The form submission, read only for where it came from.

        Returns:
            A redirect to the page, or the page again with the refusal on it —
            which is what happens when the environment on its own is not usable,
            because the announced identity was only ever set from here. Nothing
            is written in that case: the resolve comes first.
        """
        if not _from_this_page(request):
            return _refuse_cross_site()
        try:
            previous = store.load()
            resolved = load_settings(source, {})
            store.save({})
        except ConfigurationError as error:
            return HTMLResponse(
                _page(current.resolution, store, application, error=str(error)),
                status_code=400,
            )
        current.resolution = resolved
        if application is not None:
            application.apply_live(resolved.settings)
        return _redirect_after(tuple(sorted(previous)))

    async def stop(request: Request) -> Response:
        """Stop the application, so a restart-required change takes effect.

        Args:
            request: Unused.

        Returns:
            A plain acknowledgement, because the page it would redirect to is
            about to stop being served — and one that says what happens next,
            since nothing starts the application again on its own.
        """
        if not _from_this_page(request):
            return _refuse_cross_site()
        if application is None:
            return PlainTextResponse("nothing is running to stop", 503)
        application.request_stop()
        return PlainTextResponse(
            "stopping. Start it again from the robot dashboard: the daemon "
            "leaves a cleanly-stopped application stopped.",
            202,
        )

    async def configuration(request: Request) -> Response:
        """Report the resolved configuration, secrets redacted.

        Args:
            request: Unused.

        Returns:
            The same rendering the boot log emits, as JSON.
        """
        del request
        resolved = current.resolution
        return JSONResponse(
            {
                "settings": resolved_configuration(resolved.settings),
                "sources": {
                    name: value.value for name, value in resolved.sources.items()
                },
                "live": sorted(LIVE_SETTINGS),
                "secret": sorted(SECRET_SETTINGS),
                "read_only": sorted(BOOTSTRAP_SETTINGS),
                "ignored_overrides": list(resolved.ignored_overrides),
                "declared_but_unread": list(resolved.declared_but_unread),
            },
        )

    async def status(request: Request) -> Response:
        """Report what the robot is doing.

        Args:
            request: Unused.

        Returns:
            The behaviour layer's own view, as JSON.
        """
        del request
        if application is None:
            return JSONResponse({"running": False})
        return JSONResponse({"running": True, **application.status()})

    async def livez(request: Request) -> Response:
        """Answer that the interface is up.

        Args:
            request: Unused.

        Returns:
            A plain acknowledgement.
        """
        del request
        return PlainTextResponse("ok")

    return Starlette(
        middleware=[Middleware(_NoStore)],
        routes=[
            Route("/", index),
            Route("/settings", save, methods=["POST"]),
            Route("/reset", reset, methods=["POST"]),
            Route("/stop", stop, methods=["POST"]),
            Route("/config", configuration),
            Route("/status", status),
            Route("/livez", livez),
        ],
    )


class _NoStore:
    """Tell every cache to keep none of this.

    The settings page carries the resolved configuration of somebody's robot,
    and a browser that kept it would hand it to whoever opened that browser
    next. It carries no secret — those are rendered as set or unset — but a
    robot's address, its announced identity and what it is doing are not things
    to leave in a cache.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application.

        Args:
            app: What to serve behind this.
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add the header to every response that has one.

        Args:
            scope: The connection.
            receive: Where the request body comes from.
            send: Where the response goes.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def _sending(message: Message) -> None:
            """Add the header as the response starts.

            Args:
                message: One ASGI response event.
            """
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, _sending)


class _Current:
    """The resolution in effect, which a submission replaces."""

    def __init__(self, resolution: Resolution) -> None:
        """Hold the resolution the application started with.

        Args:
            resolution: What `load_settings` produced at startup.
        """
        self.resolution = resolution


def _overrides_from(
    fields: Mapping[str, str],
    *,
    base: Mapping[str, str],
    previous: Mapping[str, str],
) -> dict[str, str]:
    """Work out what to store, given what was submitted.

    Args:
        fields: The form submission.
        base: What each setting would be without any override.
        previous: The overrides currently stored.

    Returns:
        The overrides to write: only the settings whose wanted value differs
        from what the layers below supply, so nothing is pinned that did not
        need pinning.
    """
    wanted: dict[str, str] = {}
    for name in setting_names():
        if name in BOOTSTRAP_SETTINGS:
            # Never written from here, however the form was submitted: these
            # decide where the file being written lives and whether this page is
            # served at all. A browser submits every field it rendered, so
            # refusing them at the point of writing is what makes the
            # rendering's `disabled` a statement rather than a suggestion.
            continue
        if name in SECRET_SETTINGS:
            if fields.get(f"{CLEAR_PREFIX}{name}"):
                value = ""
            else:
                typed = fields.get(name, "")
                value = typed or previous.get(name, base[name])
        else:
            value = fields.get(name, base[name])
        if value != base[name]:
            wanted[name] = value
    return wanted


def _refuse_cross_site() -> Response:
    """Refuse a state-changing request a browser says came from somewhere else.

    Returns:
        The refusal, saying what it is rather than what the caller sent.
    """
    return PlainTextResponse(
        "this page's controls are used from this page. A request a browser "
        "reports as coming from another site is refused.",
        403,
    )


def _redirect_after(changed: tuple[str, ...]) -> Response:
    """Send the browser back to the page, saying what the submission did.

    A redirect rather than a rendered response, so that refreshing the page
    does not resubmit the form.

    Args:
        changed: Which settings the submission changed.

    Returns:
        The redirect.
    """
    parameters: list[tuple[str, str]] = []
    if changed:
        parameters.append(("saved", ",".join(changed)))
        restart = tuple(name for name in changed if name not in LIVE_SETTINGS)
        if restart:
            parameters.append(("restart", ",".join(restart)))
    target = "/" if not parameters else f"/?{urlencode(parameters)}"
    return Response(status_code=303, headers={"location": target})


def _page(
    resolution: Resolution,
    store: OverrideStore,
    application: SettingsHost | None,
    *,
    error: str | None = None,
    saved: tuple[str, ...] = (),
    restart_needed: tuple[str, ...] = (),
) -> str:
    """Render the page from whatever is currently true.

    Args:
        resolution: The settings in effect.
        store: Where an override is written, reported so an operator can find
            it.
        application: The running application, or `None`.
        error: What went wrong with the last submission.
        saved: Which settings the last submission changed.
        restart_needed: Which of those need a restart.

    Returns:
        The HTML.
    """
    running: dict[str, object] = (
        {"running": False} if application is None else application.status()
    )
    return render_settings_page(
        resolution,
        configuration_report(resolution),
        status=running,
        overrides_path=str(store.path),
        error=error,
        saved=saved,
        restart_needed=restart_needed,
    )
