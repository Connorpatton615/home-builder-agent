"""drive.py — Google Drive API helpers.

Functions in this module hide the request/response shape of the Drive API.
Three concerns:
  - Folder navigation (find a folder by name path, create if missing)
  - Idempotent artifact placement (archive existing → create new)
  - File upload (HTML → Google Doc, etc.)

Phase 1 had three copies of `find_folder_by_path()` / `walk_to_folder()` /
`find_drive_folder()` — same logic, different names. Now there's one.
"""

import io
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


def drive_service(creds):
    """Build a Drive v3 service. Thin wrapper for symmetry with sheets/docs."""
    return build("drive", "v3", credentials=creds)


def find_folder_by_path(service, path):
    """Walk a folder name path and return the deepest folder's ID.

    Raises FileNotFoundError if any segment doesn't exist.

    Example:
        find_folder_by_path(svc, ["Home Building Agent V.1", "GENERATED TIMELINES"])
    """
    parent_id = "root"
    walked = []
    for name in path:
        walked.append(name)
        # Drive query strings require apostrophes escaped as \'
        escaped = name.replace("'", r"\'")
        query = (
            f"name='{escaped}' "
            "and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            "and trashed=false"
        )
        results = service.files().list(
            q=query, fields="files(id, name)"
        ).execute()
        folders = results.get("files", [])
        if not folders:
            raise FileNotFoundError(
                f"Folder not found in Drive: {' / '.join(walked)}"
            )
        parent_id = folders[0]["id"]
    return parent_id


def ensure_archive_folder(service, parent_folder_id):
    """Find or create an ARCHIVE/ subfolder under parent. Returns its ID.

    Used by agents that re-run on the same project — prior versions are
    archived rather than deleted, so Chad can recover anything.
    """
    query = (
        "name='ARCHIVE' "
        "and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_folder_id}' in parents "
        "and trashed=false"
    )
    folders = service.files().list(
        q=query, fields="files(id)"
    ).execute().get("files", [])
    if folders:
        return folders[0]["id"]
    folder = service.files().create(
        body={
            "name": "ARCHIVE",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        },
        fields="id",
    ).execute()
    return folder["id"]


def archive_existing_artifact(service, name, parent_folder_id, archive_folder_id):
    """If a file `name` exists in parent, move it to ARCHIVE with a timestamp.

    Returns the count of files moved (0 = nothing existed).

    Idempotent re-run pattern: re-running the timeline generator on the same
    project doesn't create 'Timeline – Pelican Point (1)', it archives the
    existing one with name suffix " (archived 2026-04-26_1530)" and creates
    a fresh artifact with the canonical name.
    """
    query = (
        f"name='{name}' "
        f"and '{parent_folder_id}' in parents "
        "and trashed=false"
    )
    files = service.files().list(
        q=query, fields="files(id,name)"
    ).execute().get("files", [])
    if not files:
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    for f in files:
        new_name = f"{f['name']} (archived {stamp})"
        service.files().update(
            fileId=f["id"],
            body={"name": new_name},
            addParents=archive_folder_id,
            removeParents=parent_folder_id,
            fields="id",
        ).execute()
    return len(files)


def upload_as_google_doc(service, html, doc_name, parent_folder_id):
    """Upload HTML to Drive as a Google Doc (Drive auto-converts HTML)."""
    metadata = {
        "name": doc_name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [parent_folder_id],
    }
    media = MediaIoBaseUpload(
        io.BytesIO(html.encode("utf-8")), mimetype="text/html"
    )
    return service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()


def find_files_by_name_pattern(service, name_substring, parent_folder_id,
                               mime_type=None, order_by="modifiedTime desc"):
    """List files in a folder matching a name substring, newest first.

    Used by the dashboard refresher and watcher to find tracker sheets:
        find_files_by_name_pattern(svc, "Tracker", folder_id,
                                   mime_type="application/vnd.google-apps.spreadsheet")
    """
    parts = [
        f"name contains '{name_substring}'",
        f"'{parent_folder_id}' in parents",
        "trashed=false",
    ]
    if mime_type:
        parts.append(f"mimeType='{mime_type}'")
    query = " and ".join(parts)

    fields = "files(id,name,modifiedTime,webViewLink)"
    results = service.files().list(
        q=query, orderBy=order_by, fields=fields, pageSize=100
    ).execute()
    return results.get("files", [])


def get_file_modified_time(service, file_id):
    """Return the modifiedTime ISO string for one file. Used by watchers."""
    fresh = service.files().get(
        fileId=file_id, fields="modifiedTime"
    ).execute()
    return fresh["modifiedTime"]


def find_latest_tracker(service, folder_path):
    """Walk to the GENERATED TIMELINES folder and return the most recent
    Tracker spreadsheet. Convenience for the dashboard refresher and
    status updater which always operate on "the current project."

    Returns: dict with id, name, modifiedTime, webViewLink.
    """
    parent_id = find_folder_by_path(service, folder_path)
    sheets = find_files_by_name_pattern(
        service, "Tracker", parent_id,
        mime_type="application/vnd.google-apps.spreadsheet",
    )
    if not sheets:
        raise FileNotFoundError(
            f"No Tracker sheets found in {' / '.join(folder_path)}"
        )
    return sheets[0]


def find_all_trackers(service, folder_path):
    """Like find_latest_tracker but returns ALL Tracker sheets, newest first.

    Used by the watcher which polls every tracker for changes, not just the
    most recent one.
    """
    parent_id = find_folder_by_path(service, folder_path)
    return find_files_by_name_pattern(
        service, "Tracker", parent_id,
        mime_type="application/vnd.google-apps.spreadsheet",
    )


def find_tracker_by_project(service, folder_path, project_name: str):
    """Find the Tracker sheet for a specific project by name.

    Walks all trackers in the GENERATED TIMELINES folder and matches on
    extract_project_name(). Returns the most recent match, or None if no
    Tracker for that project exists.

    Use this when you need a per-project Tracker lookup — find_latest_tracker
    returns the globally newest one, which breaks once Chad has multiple
    active projects.
    """
    candidates = find_all_trackers(service, folder_path)
    project_lower = project_name.strip().lower()
    for t in candidates:
        if extract_project_name(t["name"]).strip().lower() == project_lower:
            return t
    return None


def extract_project_name(tracker_name):
    """Pull project name from a Tracker sheet's name.

    Handles current and legacy naming conventions:
      'Tracker – Pelican Point'                      (current)
      'Tracker – Pelican Point – 2026-04-25 11:10'   (legacy, pre-cleanup)
    """
    parts = tracker_name.split(" – ")
    if len(parts) >= 2:
        return parts[1]
    return tracker_name
