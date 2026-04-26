"""integrations — Google API wrappers.

One file per Google service. Each file exposes a small, well-named function
surface that hides Google API request/response shape. Agents import these
helpers instead of building their own request bodies.

Modules:
    drive   — folder walking, archival, file uploads
    docs    — Docs formatting (margins, paragraph spacing, checkboxes)
    sheets  — tracker creation, dashboard tab, visual formatting
    gmail   — thread listing, message metadata extraction
"""
