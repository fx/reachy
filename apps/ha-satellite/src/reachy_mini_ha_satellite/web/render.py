"""The settings page, rendered as a string by functions that touch nothing.

Separate from the routes so that "what does this page say?" is answerable by a
test that calls a function, rather than by one that drives a server. The page is
one file with its styles inline: it is served off a robot to an operator's
laptop, and a page that fetched a stylesheet would have a second way to fail on a
network that is the reason somebody opened it.

**A secret never reaches this module as a value.** `resolved_configuration`
renders it as set or unset before anything here sees it, and a secret's input
field is rendered empty with the current value carried nowhere. That ordering is
the point: escaping happens after redaction, never before, so there is no
transformed spelling of a credential for the redactor to have missed.
"""

from __future__ import annotations

import html

# Imported at run time rather than under `TYPE_CHECKING`, because
# `_daemon_link_note` asks `isinstance` of it: the status mapping arrives as
# `object` and the nested report has to be proved to be a mapping before it is
# read.
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal, get_args, get_origin

from reachy_mini_ha_satellite.config import (
    GROUNDSTATION_CREDENTIAL_SETTING,
    GROUNDSTATION_URL_MAX_LENGTH,
    GROUNDSTATION_URL_SETTING,
    IDENTITY_SETTING,
    LIVE_SETTINGS,
    SECRET_SETTINGS,
    Settings,
    as_configured_string,
    groundstation_is_resolved,
    identity_is_resolved,
    local_detection_clause,
)
from reachy_mini_ha_satellite.daemon_link import DaemonLinkState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachy_mini_ha_satellite.config import Resolution, SettingReport

__all__ = [
    "CLEARED_IDENTITY_HEADING",
    "CLEAR_PREFIX",
    "UNCONFIGURED_HEADING",
    "field_choices",
    "form_value",
    "render_settings_page",
]

#: What the page calls a robot that has not been given an identity yet. A robot
#: with no announced name has no name to head the page with either, and heading
#: it with an empty string would leave an operator on a page that looks broken
#: at exactly the moment they most need it to look deliberate.
UNCONFIGURED_HEADING: Final = "This robot is not configured yet"

#: And what it calls one whose identity has been cleared while it is still
#: announcing under the one it started with. A separate heading rather than the
#: one above, because "not configured yet" is false of a robot Home Assistant is
#: connected to — see `_identity_note` for the state and why it is reachable.
CLEARED_IDENTITY_HEADING: Final = "The announced identity has been cleared"

#: How a form asks for a secret to be unset rather than left alone. An empty
#: password field means "leave it as it is", because that is what a browser
#: submits for a field nobody touched — so clearing one needs a second control
#: that says so.
CLEAR_PREFIX: Final = "clear."

_STYLE: Final = """
:root { color-scheme: light dark; }
body {
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0 auto; max-width: 54rem; padding: 2rem 1rem 4rem;
}
h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
p.lede { margin-top: 0; opacity: 0.75; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.4rem 0.5rem; vertical-align: top; }
tbody tr:nth-child(odd) { background: rgba(127, 127, 127, 0.08); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
input[type=text], input[type=password], select { width: 100%; box-sizing: border-box; }
.note { border-left: 3px solid currentColor; padding: 0.5rem 0.75rem; margin: 1rem 0; }
.error { border-left-color: #c0392b; }
.saved { border-left-color: #27ae60; }
.hazard { border-left-color: #d35400; }
.tag {
  border: 1px solid currentColor; border-radius: 0.6rem;
  font-size: 0.75rem; padding: 0 0.4rem; opacity: 0.8;
}
.actions { display: flex; gap: 0.5rem; margin: 1rem 0; }
pre { overflow-x: auto; }
"""


def _escape(value: object) -> str:
    """Render anything as text safe to put in a page.

    Args:
        value: What to render.

    Returns:
        The HTML-escaped text, quotes included, so the same helper is safe in
        an attribute as in a body.
    """
    return html.escape(str(value), quote=True)


def form_value(settings: Settings, name: str) -> str:
    """Render one setting as the string its form field should carry.

    Args:
        settings: The settings in effect.
        name: Which setting.

    Returns:
        The string, or the empty string for a secret — whose value is never
        put into a page, so its field is always blank and submitting it blank
        means "leave it alone".
    """
    if name in SECRET_SETTINGS:
        return ""
    return as_configured_string(getattr(settings, name))


def field_choices(name: str) -> tuple[str, ...] | None:
    """List the values a setting is allowed to take, when there are few enough.

    Args:
        name: Which setting.

    Returns:
        The choices, or `None` when the setting is free text. Booleans are
        choices too, and deliberately: a checkbox submits nothing when it is
        unchecked, so a form of checkboxes cannot tell "switched off" from
        "not on this page".
    """
    # Deliberately `object`: the declared type is `type[Any] | None`, and
    # narrowing it by `is bool` would leave the type checker believing the two
    # checks below can never run.
    annotation: object = Settings.model_fields[name].annotation
    if annotation is bool:
        return ("true", "false")
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return tuple(str(member.value) for member in annotation)
    if get_origin(annotation) is Literal:
        return tuple(str(argument) for argument in get_args(annotation))
    return None


def _field(report: SettingReport, settings: Settings) -> str:
    """Render one row of the form.

    Args:
        report: The setting, its provenance and whether it is secret.
        settings: The settings in effect, which the field's value comes from.

    Returns:
        The table row.
    """
    name = _escape(report.name)
    # Every control carries an id and the row header is a `label` pointing at
    # it. A table header does not label a control for assistive technology, so
    # without this a screen reader announces "text field" and nothing about
    # which setting it changes.
    field = f"setting-{name}"
    choices = field_choices(report.name)
    if not report.writable:
        # Readable, which is what REQ-049 asks of a setting that is not secret,
        # and deliberately not writable: bootstrap values decide whether this
        # form is reachable, while compatibility values are accepted only so an
        # existing environment keeps starting. The tags below distinguish them.
        control = (
            f'<input type="text" id="{field}" disabled '
            f'value="{_escape(form_value(settings, report.name))}">'
        )
    elif report.secret:
        control = (
            f'<input type="password" id="{field}" name="{name}" value="" '
            f'autocomplete="new-password" placeholder="leave blank to keep">'
            f'<label><input type="checkbox" id="clear-{field}" '
            f'name="{CLEAR_PREFIX}{name}" value="1"> unset it</label>'
        )
    elif choices is not None:
        current = form_value(settings, report.name)
        options = "".join(
            f'<option value="{_escape(choice)}"'
            f"{' selected' if choice == current else ''}>{_escape(choice)}</option>"
            for choice in choices
        )
        control = f'<select id="{field}" name="{name}">{options}</select>'
    else:
        # The one field with a declared maximum on the page, and it is the
        # shared one rather than a number written here: an over-long address is
        # refused by the settings model and by the Home Assistant control alike,
        # so the browser stopping it first is a courtesy and not the check.
        limit = (
            f' maxlength="{GROUNDSTATION_URL_MAX_LENGTH}"'
            if report.name == GROUNDSTATION_URL_SETTING
            else ""
        )
        control = (
            f'<input type="text" id="{field}" name="{name}"{limit} '
            f'value="{_escape(form_value(settings, report.name))}">'
        )

    tags = [f'<span class="tag">{_escape(report.source.value)}</span>']
    if report.compatibility:
        tags.append('<span class="tag">legacy compatibility; ignored</span>')
    elif not report.writable:
        tags.append(
            '<span class="tag">set in the environment: this page depends on it</span>',
        )
    elif report.name in LIVE_SETTINGS:
        tags.append('<span class="tag">applies at once</span>')
    else:
        tags.append('<span class="tag">needs a restart</span>')
    if report.secret:
        tags.append(f'<span class="tag">{_escape(report.value)}</span>')

    return (
        f'<tr><th scope="row"><label for="{field}"><code>{name}</code></label>'
        f'<br><code style="opacity:0.6">{_escape(report.variable)}</code></th>'
        f"<td>{control}</td><td>{''.join(tags)}</td></tr>"
    )


def _lede(
    settings: Settings,
    *,
    resolved_identity: bool,
    announced_identity: str | None,
) -> str:
    """Say what this robot announces itself as, or that it announces nothing.

    **The authoritative sentence on the page, so it states what is announced
    rather than what is configured.** The two are different facts — the identity
    is restart-bound — and rendering the configured one here was wrong in the
    direction that matters: a robot saved with a new `device_name` would be
    described as announced under it while Home Assistant was still keyed on the
    preceding one, on the page whose standing hazard is that very key.

    Args:
        settings: The settings in effect.
        resolved_identity: Whether an identity has been supplied.
        announced_identity: What this process announces, or `None` when it
            announces nothing — including when there is no application behind
            this page at all.

    Returns:
        The first sentence of the page.
    """
    if announced_identity is None:
        if resolved_identity:
            # A page with an application that built no announcing surface, or
            # with no application at all. `_identity_note` tells the two apart;
            # neither is announcing, which is all this sentence claims.
            return "Nothing is announced to Home Assistant yet."
        return (
            "Nothing is announced to Home Assistant: this robot has no "
            "announced identity yet."
        )
    announced = (
        f"Announced to Home Assistant as <code>{_escape(announced_identity)}</code>."
    )
    if not resolved_identity:
        return (
            f"{announced} The configured identity has been cleared, so the next "
            f"start announces nothing."
        )
    if settings.device_name != announced_identity:
        return (
            f"{announced} The configured identity is now <code>"
            f"{_escape(settings.device_name)}</code>, which takes effect at the "
            f"next start."
        )
    return announced


#:= docs/specs/stock-robot-installation/index.md#req-102-nothing-is-announced-while-the-identity-is-unresolved
#:% The satellite MUST NOT announce itself to Home Assistant, or serve a Home
#:% Assistant connection, while its announced identity is unresolved.
def _identity_note(
    *,
    resolved_identity: bool,
    announcing: bool | None,
    renamed: bool,
) -> str:
    """Render the standing note about the announced identity.

    Five states rather than one, and they are five because the identity is
    **restart-bound**: what a process announces is fixed when it is built, so
    the configured identity and the announced one are two facts, and either can
    move without the other in either direction.

    | Configured | Announcing | What the operator is told |
    |---|---|---|
    | unresolved | no | the embargo, and how to leave it |
    | unresolved | **yes** | the identity was *cleared* and this process is still announcing under the one it started with |
    | resolved | no | an identity is set and this process started without one |
    | resolved, **changed** | yes | the change has not happened yet, and what it will do when it does |
    | resolved, unchanged | yes | the standing hazard: do not change it |

    **The second row is the one worth reading, because getting it wrong is a
    lie in the direction that matters.** Clearing `device_name` on a robot that
    is announcing resolves — an unresolved identity is a state now — so it is
    persisted and badged "needs a restart" like any other restart-bound change,
    and the process goes on announcing under the identity it was built with. A
    page that showed the embargo there would tell an operator no device was
    registered while Home Assistant was still connected to one.

    It is not refused, and that is deliberate rather than an omission: the
    identity can come from an override alone, so refusing would make *Reset*
    impossible on such a robot — and stopping it to get round that starts it
    again with the same override, which is a dead end of exactly the kind this
    change exists to remove. REQ-102 still holds, because "its announced
    identity" is what a process announces and that is resolved for as long as it
    announces anything; the cleared value takes effect at the next start, and
    that start builds no announcing surface at all.

    `announcing` is `None` for a page with no application behind it — the
    rendering-only composition the composition root never produces. There is
    nothing running to be announcing or not, so neither middle row is one that
    page can be in, and it renders the first or the last.

    Args:
        resolved_identity: Whether an identity has been supplied.
        announcing: Whether an announcing surface was built, or `None` when
            nothing is running behind this page.
        renamed: Whether the configured identity differs from the one this
            process announces. Only meaningful while announcing.

    Returns:
        One note.
    """
    if not resolved_identity and announcing:
        return (
            '<div class="note hazard"><strong>The announced identity has been '
            "cleared, and this application is still announcing under the one it "
            "started with.</strong> What a satellite announces is fixed when it "
            "starts, so clearing the value takes effect at the next start — and "
            "that start will announce nothing at all, leaving the Home Assistant "
            "device this robot registered with no satellite behind it. If that "
            "was not what you meant, set <code>"
            f"{_escape(IDENTITY_SETTING)}</code> back below before stopping the "
            "application.</div>"
        )
    if not resolved_identity:
        return (
            '<div class="note hazard"><strong>Nothing is announced to Home '
            "Assistant until <code>"
            f"{_escape(IDENTITY_SETTING)}</code> is set.</strong> No device is "
            "registered, no entity exists and no Home Assistant connection is "
            "served, so there is nothing here for a later identity to collide "
            "with. Set it below and save.<br><br>Choose the name now and never "
            "change it: Home Assistant keys the device on it, so a later change "
            "registers a second device, every entity identifier gains a suffix, "
            "history detaches and automations referencing the old identifiers "
            "stop matching. Upgrading an existing installation means setting it "
            "to whatever the previous application announced.<br><br>The "
            "announcing surface is built when the application starts, so after "
            "saving press <em>Stop</em> below and start it again from the robot "
            "dashboard — no shell, and no reinstall.</div>"
        )
    if announcing is False:
        return (
            '<div class="note hazard"><strong>An identity is set, and this '
            "application started without one, so it is still announcing "
            "nothing.</strong> Press <em>Stop</em> below and start it again "
            "from the robot dashboard — the daemon leaves a cleanly-stopped "
            "application stopped.</div>"
        )
    if renamed:
        return (
            '<div class="note hazard"><strong>The announced identity has been '
            "changed, and Home Assistant is still keyed on the one this "
            "application started with.</strong> When it next starts it will "
            "announce the new one, and Home Assistant will register a "
            "<em>second</em> device: every entity identifier gains a suffix, "
            "history stays with the old device and automations referencing the "
            "old identifiers stop matching. Nothing has happened yet — set "
            f"<code>{_escape(IDENTITY_SETTING)}</code> back below if that was "
            "not what you meant.</div>"
        )
    return (
        '<div class="note hazard"><strong>Do not change '
        "<code>device_name</code> on a robot Home Assistant already knows.</strong> "
        "Home Assistant keys the device on it: a new name registers a new "
        "device, every entity identifier gains a suffix, history detaches, and "
        "automations referencing the old identifiers stop matching.</div>"
    )


#:= docs/specs/stock-robot-installation/index.md#req-103-remote-perception-is-optional-at-first-start
#:% The satellite MUST start with an unresolved groundstation address or credential
#:% and run on local detection until both are supplied through a configuration
#:% surface.
def _groundstation_note(settings: Settings) -> str:
    """Say that an unsupplied groundstation is unconfigured rather than broken.

    Args:
        settings: The settings in effect.

    Returns:
        The note, or nothing at all when a groundstation is configured.
    """
    if groundstation_is_resolved(settings):
        return ""
    return (
        '<div class="note">No groundstation is configured, so the remote '
        "detector is <strong>unconfigured</strong> rather than failed: no "
        "session is opened, nothing is connecting and nothing is being retried, "
        # The one definition of what detects a face meanwhile, shared with the
        # boot log rather than written again here. A robot with no local
        # weights has nothing to fall back to, and saying otherwise would
        # describe somebody else's robot to the operator of this one.
        f"and {_escape(local_detection_clause(settings))} "
        f"Set both <code>{_escape(GROUNDSTATION_URL_SETTING)}</code> and "
        f"<code>{_escape(GROUNDSTATION_CREDENTIAL_SETTING)}</code> below — one "
        "without the other opens nothing — and a running application adopts "
        "them without a restart.</div>"
    )


def _daemon_link_note(status: Mapping[str, object]) -> str:
    """Say the robot cannot reach its own daemon, when that is what is wrong.

    The page renders every status key in the summary line already, and that is
    not enough for this one: an operator opens this page because the robot has
    stopped moving, and `daemon_link` sorted between `controller` and `gaze` in
    a run-on list is not being told. It leads the notes because every other
    thing this page could say is true and irrelevant while it holds — the
    identity is fine, the groundstation is fine, and nothing is moving.

    Args:
        status: What the application reports about itself, or a page with no
            application behind it, which reports no link and gets no note.

    Returns:
        The note, or nothing at all while the daemon is answering.
    """
    link = status.get("daemon_link")
    if not isinstance(link, Mapping):
        return ""
    if link.get("state") != DaemonLinkState.DOWN.value:
        return ""
    return (
        '<div class="note hazard">The link to the robot daemon is '
        "<strong>down</strong>: the last command this application sent was "
        "refused, so nothing it asks for is reaching the motors and the robot "
        "will not move, however this page is configured. <strong>The "
        "application is still running</strong> and needs no restart — it "
        "commands the daemon again on its own and this goes back to "
        "<code>up</code> as soon as one lands. How soon depends on what the "
        "robot is doing: with face tracking on it re-checks every tick, and "
        "with tracking off it finds out at the next thing that moves the robot, "
        "so this can stay showing an outage that has already ended. If it does "
        "not come back, restart the daemon's own service on the robot.</div>"
    )


def _resolved_table(report: Sequence[SettingReport]) -> str:
    """Render the resolved configuration, defaults included.

    Args:
        report: Every setting, already redacted.

    Returns:
        The table.
    """
    rows = "".join(
        f'<tr><th scope="row"><code>{_escape(row.name)}</code></th>'
        f"<td><code>{_escape(row.value)}</code></td>"
        f"<td>{_escape(row.source.value)}</td></tr>"
        for row in report
    )
    return (
        "<table><thead><tr><th>Setting</th><th>In effect</th><th>From</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


#:= docs/specs/stock-robot-installation/index.md#req-101-an-unresolved-identity-starts-the-application-rather-than-stopping-it
#:% The satellite MUST start and serve its settings interface when no announced
#:% identity has been configured, reporting the identity as unresolved rather than
#:% refusing to start.
def render_settings_page(
    resolution: Resolution,
    report: Sequence[SettingReport],
    *,
    status: Mapping[str, object],
    overrides_path: str,
    announcing: bool | None = None,
    announced_identity: str | None = None,
    error: str | None = None,
    saved: Sequence[str] = (),
    restart_needed: Sequence[str] = (),
) -> str:
    """Render the whole page.

    Args:
        resolution: The settings in effect and where each came from.
        report: The same, rendered with secrets redacted.
        status: What the robot is doing right now.
        overrides_path: Where a change written here is kept.
        announcing: Whether the application behind this page built an announcing
            surface, or `None` when there is no application behind it. Taken
            from the application rather than inferred from the settings, because
            the identity is restart-bound: one supplied a moment ago is resolved
            configuration and still nothing announced, and that gap is precisely
            what an operator on this page needs told.
        announced_identity: *Which* identity it announces, or `None` when it
            announces none. The gap runs the other way too — an identity changed
            on a running robot leaves Home Assistant keyed on the preceding one
            — so the sentence that says what Home Assistant sees is rendered
            from this rather than from the settings.
        error: What went wrong with the last submission, if anything.
        saved: Which settings the last submission changed.
        restart_needed: Which of those need the application restarted.

    Returns:
        One self-contained HTML document.
    """
    settings = resolution.settings
    resolved_identity = identity_is_resolved(settings)
    # A robot with no announced identity has no name to head the page with —
    # not even the display name, since heading it with one would imply a
    # configured robot. The title is the heading on its own in those states,
    # because "This robot is not configured yet settings" is nobody's tab.
    renamed = announced_identity is not None and (
        settings.device_name != announced_identity
    )
    if renamed:
        # A fact rather than a label: the display name is configuration too and
        # would be as stale as the identity beside it, so while the two disagree
        # the page is headed by what Home Assistant is actually keyed on. The
        # friendly name is a display value and its own one-restart staleness is
        # not worth a second carried string.
        heading = str(announced_identity)
    elif resolved_identity:
        heading = settings.announced_friendly_name
    elif announcing:
        heading = CLEARED_IDENTITY_HEADING
    else:
        heading = UNCONFIGURED_HEADING
    title = f"{heading} settings" if resolved_identity else heading
    notes: list[str] = []
    if error is not None:
        notes.append(
            f'<div class="note error"><strong>Nothing was saved.</strong> '
            f"<pre>{_escape(error)}</pre></div>",
        )
    if saved:
        changed = ", ".join(f"<code>{_escape(name)}</code>" for name in saved)
        notes.append(f'<div class="note saved">Saved: {changed}.</div>')
    if restart_needed:
        pending = ", ".join(f"<code>{_escape(name)}</code>" for name in restart_needed)
        notes.append(
            f'<div class="note hazard">{pending} take effect when the '
            f"application next starts. <em>Stop</em> it below, then start it "
            f"again from the robot dashboard — the daemon leaves a "
            f"cleanly-stopped application stopped.</div>",
        )

    state = ", ".join(
        f"<code>{_escape(key)}</code>: {_escape(value)}"
        for key, value in sorted(status.items())
    )
    ignored = ""
    if resolution.ignored_overrides:
        stale = ", ".join(
            f"<code>{_escape(name)}</code>" for name in resolution.ignored_overrides
        )
        ignored = (
            f'<div class="note hazard">{stale} are stored in '
            f"<code>{_escape(overrides_path)}</code> but are not settings any "
            f"more, and are being ignored. <em>Reset</em> clears them.</div>"
        )

    unread = ""
    if resolution.declared_but_unread:
        inert = ", ".join(
            f"<code>{_escape(name)}</code>" for name in resolution.declared_but_unread
        )
        unread = (
            f'<div class="note hazard">{inert} are declared for this robot but '
            f"this application does not read them, so setting them has no "
            f"effect here. They are valid names — another component reads them "
            f"— so the daemon environment is not wrong; this page is telling "
            f"you they change nothing about <em>this</em> application."
        ) + "</div>"

    fields = "".join(_field(row, settings) for row in report)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_escape(title)}</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<h1>{_escape(heading)}</h1>"
        f'<p class="lede">'
        f"{_lede(settings, resolved_identity=resolved_identity, announced_identity=announced_identity)}"
        f" {state}</p>"
        f"{_daemon_link_note(status)}"
        f"{_identity_note(resolved_identity=resolved_identity, announcing=announcing, renamed=renamed)}"
        f"{_groundstation_note(settings)}"
        f"{''.join(notes)}{ignored}{unread}"
        '<form method="post" action="settings">'
        "<table><thead><tr><th>Setting</th><th>Value</th><th></th></tr></thead>"
        f"<tbody>{fields}</tbody></table>"
        '<p><button type="submit">Save</button></p></form>'
        # A `div`, not a `p`. A paragraph's content model forbids a block-level
        # element, so a browser meeting `<p><form>` silently closes the
        # paragraph first — producing a DOM that is not the one written here,
        # which is the one thing a page nobody can open in a debugger must not
        # do. `test_satellite_web_settings.py` parses the rendered page and
        # fails on any such nesting, so this is a gate rather than a habit.
        '<div class="actions"><form method="post" action="reset">'
        '<button type="submit">Reset every override</button></form>'
        '<form method="post" action="stop">'
        '<button type="submit">Stop</button></form></div>'
        "<h2>Resolved configuration</h2>"
        f"<p>Overrides written here are kept in <code>{_escape(overrides_path)}</code>, "
        "outside the wheel, so reinstalling the application keeps them. Values "
        "marked <code>environment</code> come from the daemon's environment; "
        "saving one back to its environment value removes the override.</p>"
        f"{_resolved_table(report)}"
        "</body></html>"
    )
