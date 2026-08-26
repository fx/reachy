"""The settings interface the daemon links to.

Two modules. `render.py` turns the resolved configuration into a page and
touches nothing else; `app.py` holds the routes, the form handling and the one
narrow protocol the running application satisfies. The split is what lets "what
does the page say about a secret?" be answered by calling a function rather than
by driving a server.

Nothing here imports `main.py`. The interface is handed a `SettingsHost` — three
methods — which is both what breaks the import cycle and a complete statement of
what a settings page is allowed to do to a running robot.
"""

from reachy_mini_ha_satellite.web.app import (
    SettingsHost,
    base_form_values,
    create_app,
)
from reachy_mini_ha_satellite.web.render import (
    CLEAR_PREFIX,
    field_choices,
    form_value,
    render_settings_page,
)

__all__ = [
    "CLEAR_PREFIX",
    "SettingsHost",
    "base_form_values",
    "create_app",
    "field_choices",
    "form_value",
    "render_settings_page",
]
