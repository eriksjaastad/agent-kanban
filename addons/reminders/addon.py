"""Reminders: things you want to remember you have (#8282).

Its table still comes from the core's migration 022 until add-ons own their
migrations.
"""

from scripts.addon_registry import Addon

ADDON = Addon(
    name="reminders",
    summary="Things you want to remember you have, until they are built into a process, skill or tool",
    public=True,
    commands=("addons.reminders.cli:reminders_group",),
    router="addons.reminders.routes:router",
    pages=("/reminders",),
    # A top-level leaf, not inside a group: its label is the reminder (#8282).
    nav=(
        {
            "id": "reminders",
            "label": "Reminders",
            "href": "/reminders",
            "match_prefixes": ["/reminders"],
            "navigation_type": "spa",
        },
    ),
)
