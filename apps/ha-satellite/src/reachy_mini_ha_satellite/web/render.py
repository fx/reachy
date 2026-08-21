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
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal, get_args, get_origin

from reachy_mini_ha_satellite.config import (
    LIVE_SETTINGS,
    SECRET_SETTINGS,
    Settings,
    as_configured_string,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from reachy_mini_ha_satellite.config import Resolution, SettingReport

__all__ = [
    "CLEAR_PREFIX",
    "field_choices",
    "form_value",
    "render_settings_page",
]

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
        # and deliberately not writable: these decide where this form's own file
        # lives and whether this page is served at all, so the page cannot be
        # what changes them. See `config.BOOTSTRAP_SETTINGS`.
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
        control = (
            f'<input type="text" id="{field}" name="{name}" '
            f'value="{_escape(form_value(settings, report.name))}">'
        )

    tags = [f'<span class="tag">{_escape(report.source.value)}</span>']
    if not report.writable:
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


def render_settings_page(
    resolution: Resolution,
    report: Sequence[SettingReport],
    *,
    status: Mapping[str, object],
    overrides_path: str,
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
        error: What went wrong with the last submission, if anything.
        saved: Which settings the last submission changed.
        restart_needed: Which of those need the application restarted.

    Returns:
        One self-contained HTML document.
    """
    settings = resolution.settings
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
        f"<title>{_escape(settings.announced_friendly_name)} settings</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<h1>{_escape(settings.announced_friendly_name)}</h1>"
        f'<p class="lede">Announced to Home Assistant as '
        f"<code>{_escape(settings.device_name)}</code>. {state}</p>"
        '<div class="note hazard"><strong>Do not change '
        "<code>device_name</code> on a robot Home Assistant already knows.</strong> "
        "Home Assistant keys the device on it: a new name registers a new "
        "device, every entity identifier gains a suffix, history detaches, and "
        "automations referencing the old identifiers stop matching.</div>"
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
