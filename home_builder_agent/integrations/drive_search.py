"""drive_search.py — keyword search + file content extraction across Drive.

Used by hb-ask (RAG agent) to retrieve relevant Drive content for questions
Chad asks. Designed to be lean — no vector embeddings yet, just keyword
search via Drive's `fullText contains` query plus typed file readers for
the three formats Chad's project folders contain (Sheets, Docs, plain text).

Why no embeddings in v0:
  - Vector indexing adds 1-2 days of build + maintenance burden
    (re-index when files change, choose embedding model, choose vector
    store, query-time vector lookup)
  - Chad's domain is narrow (~10-30 files per project). Keyword retrieval
    + Claude's reasoning over the matched content is good enough
  - V1+ can layer embeddings if keyword hits a ceiling (e.g., "what did
    I tell the framer last month" doesn't keyword well)
"""

from __future__ import annotations

import csv
import io
from typing import Optional

from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_drive_files(
    drive_svc,
    query: str,
    *,
    parent_folder_id: str | None = None,
    max_results: int = 20,
) -> list[dict]:
    """Keyword search across Drive. Returns file metadata dicts.

    Drive supports `fullText contains 'phrase'` which searches both file
    name and (for indexed types) content. Works well for Google Docs,
    Sheets, and plain markdown. Less reliable for PDFs.

    Args:
        drive_svc:        authenticated Drive service
        query:            keyword string (will be quoted for Drive query)
        parent_folder_id: scope search to this folder + subfolders. Drive's
                          `in parents` is single-level only — for recursive
                          we'd need to enumerate child folder IDs first.
                          v0 accepts the single-level limitation.
        max_results:      cap on returned files

    Returns:
        list of {id, name, mimeType, modifiedTime, webViewLink, parents}
    """
    q_parts = []

    # Escape single quotes in user query
    safe_query = query.replace("'", "\\'")
    q_parts.append(f"fullText contains '{safe_query}'")
    q_parts.append("trashed = false")

    if parent_folder_id:
        q_parts.append(f"'{parent_folder_id}' in parents")

    drive_query = " and ".join(q_parts)

    try:
        result = drive_svc.files().list(
            q=drive_query,
            fields="files(id,name,mimeType,modifiedTime,webViewLink,parents)",
            pageSize=max_results,
            orderBy="modifiedTime desc",
        ).execute()
    except HttpError as e:
        # Bad query → empty results rather than crash
        return []

    return result.get("files", [])


# ---------------------------------------------------------------------------
# File content extraction
# ---------------------------------------------------------------------------

def read_drive_file(
    drive_svc,
    file_id: str,
    *,
    sheets_svc=None,
    max_chars: int = 50_000,
) -> dict:
    """Read a Drive file and return its text content + metadata.

    Routes by mimeType:
      Google Sheets → CSV via export, optionally per-sheet via Sheets API
      Google Docs   → plain text via export
      Markdown / text → raw bytes decoded as UTF-8
      Other (PDF, images, etc.) → returns metadata + content_unavailable=True

    Args:
        drive_svc:    authenticated Drive service
        file_id:      Drive file ID
        sheets_svc:   optional Sheets service. If provided AND the file is a
                      Google Sheet, returns structured per-tab content
                      instead of CSV. Useful when retrieval needs to know
                      "Master Schedule has these phases" vs flat CSV blob.
        max_chars:    truncate content to this many chars (Claude context guard)

    Returns:
        {
            "id": str,
            "name": str,
            "mimeType": str,
            "modifiedTime": str,
            "webViewLink": str,
            "content": str,
            "truncated": bool,
            "content_unavailable": bool,
        }
    """
    meta = drive_svc.files().get(
        fileId=file_id,
        fields="id,name,mimeType,modifiedTime,webViewLink",
    ).execute()

    mime = meta.get("mimeType", "")
    name = meta.get("name", "")

    out = {
        "id": meta["id"],
        "name": name,
        "mimeType": mime,
        "modifiedTime": meta.get("modifiedTime", ""),
        "webViewLink": meta.get("webViewLink", ""),
        "content": "",
        "truncated": False,
        "content_unavailable": False,
    }

    try:
        if mime == "application/vnd.google-apps.spreadsheet":
            if sheets_svc is not None:
                content = _read_sheets_structured(sheets_svc, file_id)
            else:
                content = _export_drive_file(drive_svc, file_id, "text/csv")
        elif mime == "application/vnd.google-apps.document":
            content = _export_drive_file(drive_svc, file_id, "text/plain")
        elif mime in ("text/markdown", "text/plain", "text/x-markdown"):
            content = _download_drive_bytes(drive_svc, file_id).decode("utf-8", errors="replace")
        else:
            out["content_unavailable"] = True
            out["content"] = f"(File format {mime!r} is not extractable in v0)"
            return out
    except HttpError as e:
        out["content_unavailable"] = True
        out["content"] = f"(Failed to read: {type(e).__name__}: {e})"
        return out

    if len(content) > max_chars:
        out["content"] = content[:max_chars]
        out["truncated"] = True
    else:
        out["content"] = content

    return out


# ---------------------------------------------------------------------------
# Format-specific readers
# ---------------------------------------------------------------------------

def _export_drive_file(drive_svc, file_id: str, target_mime: str) -> str:
    """Export a Google file (Sheet/Doc) to a target plain format."""
    response = drive_svc.files().export(
        fileId=file_id, mimeType=target_mime
    ).execute()
    if isinstance(response, bytes):
        return response.decode("utf-8", errors="replace")
    return str(response)


def _download_drive_bytes(drive_svc, file_id: str) -> bytes:
    """Download raw file bytes (for non-Google files like markdown)."""
    return drive_svc.files().get_media(fileId=file_id).execute()


def _read_sheets_structured(sheets_svc, sheet_id: str, max_tabs: int = 10) -> str:
    """Read a Google Sheet tab-by-tab, returning a structured text dump.

    Better than CSV export when retrieval needs to know "this is the
    Master Schedule tab, here's what's in it" — preserves tab boundaries
    and lets Claude reason about which tab each row came from.
    """
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tabs = meta.get("sheets", [])[:max_tabs]

    out_lines: list[str] = []
    for t in tabs:
        tab_name = t["properties"]["title"]
        # Read up to 200 rows, 26 cols per tab
        try:
            res = sheets_svc.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"'{tab_name}'!A1:Z200",
            ).execute()
            rows = res.get("values", [])
        except HttpError:
            rows = []

        out_lines.append(f"=== TAB: {tab_name} ===")
        if not rows:
            out_lines.append("  (empty)")
        else:
            for row in rows:
                # Pad short rows so columns align
                out_lines.append(" | ".join(str(c) for c in row))
        out_lines.append("")

    return "\n".join(out_lines)
