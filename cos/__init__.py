"""Metroplex CoS: intake, delegation and routing over Teletraan.

Metroplex is a thin, replaceable judge. Durable work state lives in Teletraan;
this package only reads it, decides, and issues fenced, idempotent commands as
the `cos` principal. Design: docs/2026-09-24-metroplex-cos-spec.md and
docs/2026-09-24-metroplex-mvp-scope.md.
"""
