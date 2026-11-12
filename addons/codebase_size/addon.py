from scripts.addon_registry import Addon

ADDON = Addon(
    name="codebase_size",
    summary="Code and doc lines per repo, scored against the first stored run (pt size)",
    public=True,
    commands=("addons.codebase_size.cli:size_command",),
    router="addons.codebase_size.routes:router",
    pages=("/codebase",),
    nav=(
        {
            "group": "group-agents",
            "id": "codebase",
            "label": "Codebase size",
            "href": "/codebase",
            "match_prefixes": ["/codebase"],
            "navigation_type": "spa",
        },
    ),
)
