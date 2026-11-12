"""Codebase size: how much code and docs each repo carries, scored against a baseline (#8083).

Its table, codebase_size_snapshots, still comes from the core schema until
add-ons own their migrations. The table is append-only, so a deleted project
leaves nothing for this add-on to clean up.
"""
