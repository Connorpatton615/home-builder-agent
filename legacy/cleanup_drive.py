"""
cleanup_drive.py — One-shot cleanup of duplicate Timeline docs and Tracker sheets.

For each project found in GENERATED TIMELINES:
  - Keep the MOST RECENT Timeline doc and Tracker sheet
  - Move all older versions to ARCHIVE/ subfolder (created if missing)
  - Rename the kept files to drop the timestamp suffix
    e.g. "Tracker – Pelican Point – 2026-04-25 16:57" → "Tracker – Pelican Point"

Safe to run multiple times — idempotent. Files moved to ARCHIVE/ are not
deleted; you can recover any if needed by moving them back.

Run once to clean up the current state, then leave it. Future Agent 2 runs
will use the new naming pattern and auto-archive on re-runs.
"""

import os
import re
from collections import defaultdict
from datetime import datetime

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from googleapiclient.discovery import build

from agent_2_5_dashboard import get_credentials, DRIVE_FOLDER_PATH


# Names start with one of these prefixes
DOC_PREFIX = "Timeline – "
SHEET_PREFIX = "Tracker – "

# Timestamp pattern in legacy names: " – YYYY-MM-DD HH:MM"
TIMESTAMP_SUFFIX_RE = re.compile(r" – \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def walk_to_folder(drive_service, folder_path):
    parent_id = "root"
    walked = []
    for name in folder_path:
        walked.append(name)
        query = (
            f"name='{name}' "
            "and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            "and trashed=false"
        )
        folders = drive_service.files().list(
            q=query, fields="files(id,name)"
        ).execute().get("files", [])
        if not folders:
            raise FileNotFoundError(f"Folder not found: {' / '.join(walked)}")
        parent_id = folders[0]["id"]
    return parent_id


def ensure_archive_folder(drive_service, parent_id):
    """Find or create an ARCHIVE subfolder under parent. Returns its ID."""
    query = (
        "name='ARCHIVE' "
        "and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        "and trashed=false"
    )
    folders = drive_service.files().list(
        q=query, fields="files(id)"
    ).execute().get("files", [])
    if folders:
        return folders[0]["id"]
    folder = drive_service.files().create(
        body={
            "name": "ARCHIVE",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
    ).execute()
    print(f"  Created ARCHIVE folder: {folder['id']}")
    return folder["id"]


def project_name_from_filename(name):
    """Strip the Timeline/Tracker prefix and timestamp suffix to get the project."""
    if name.startswith(DOC_PREFIX):
        rest = name[len(DOC_PREFIX):]
    elif name.startswith(SHEET_PREFIX):
        rest = name[len(SHEET_PREFIX):]
    else:
        return None
    # Strip a legacy " – timestamp" suffix if present
    rest = TIMESTAMP_SUFFIX_RE.sub("", rest)
    return rest.strip()


def list_artifacts(drive_service, parent_id):
    """List all Timeline docs and Tracker sheets in parent. Returns list of dicts."""
    query = (
        "(name contains 'Timeline – ' or name contains 'Tracker – ') "
        f"and '{parent_id}' in parents "
        "and trashed=false "
        "and ("
        "  mimeType='application/vnd.google-apps.document' "
        "  or mimeType='application/vnd.google-apps.spreadsheet'"
        ")"
    )
    return drive_service.files().list(
        q=query,
        orderBy="modifiedTime desc",
        fields="files(id,name,modifiedTime,mimeType)",
        pageSize=200,
    ).execute().get("files", [])


def archive_file(drive_service, file_id, current_parent_id, archive_parent_id):
    """Move file from current parent to archive parent."""
    drive_service.files().update(
        fileId=file_id,
        addParents=archive_parent_id,
        removeParents=current_parent_id,
        fields="id,parents",
    ).execute()


def rename_file(drive_service, file_id, new_name):
    drive_service.files().update(
        fileId=file_id, body={"name": new_name}, fields="id,name"
    ).execute()


def main():
    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)

    print(f"Walking to: {' / '.join(DRIVE_FOLDER_PATH)}")
    folder_id = walk_to_folder(drive_service, DRIVE_FOLDER_PATH)

    print("Listing existing Timeline + Tracker artifacts...")
    artifacts = list_artifacts(drive_service, folder_id)
    print(f"  Found {len(artifacts)} files in GENERATED TIMELINES")

    # Group: (project_name, kind) → list of files (most-recent first since we
    # already orderBy modifiedTime desc)
    grouped = defaultdict(list)
    skipped = []
    for f in artifacts:
        proj = project_name_from_filename(f["name"])
        if proj is None:
            skipped.append(f["name"])
            continue
        kind = "Timeline" if f["name"].startswith(DOC_PREFIX) else "Tracker"
        grouped[(proj, kind)].append(f)

    if skipped:
        print(f"  Skipped {len(skipped)} files not matching pattern: {skipped}")

    # Plan the actions
    archive_actions = []  # list of file dicts to move to archive
    rename_actions = []   # list of (file dict, new_name) to rename

    for (proj, kind), files in grouped.items():
        keeper = files[0]  # most recent
        target_name = f"{kind} – {proj}"
        if keeper["name"] != target_name:
            rename_actions.append((keeper, target_name))
        for older in files[1:]:
            archive_actions.append(older)

    # Confirm before acting
    print()
    print(f"Plan:")
    print(f"  KEEP + rename:   {len(rename_actions)} file(s)")
    for f, new_name in rename_actions:
        print(f"    {f['name']!r} → {new_name!r}")
    print(f"  Archive (move):  {len(archive_actions)} file(s)")
    for f in archive_actions:
        print(f"    {f['name']!r}  (modified {f['modifiedTime']})")

    if not rename_actions and not archive_actions:
        print()
        print("Nothing to do — already clean!")
        return

    print()
    confirm = input("Proceed with cleanup? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted — no changes made.")
        return

    # Ensure archive folder
    archive_folder_id = ensure_archive_folder(drive_service, folder_id)

    # Execute archives
    for f in archive_actions:
        archive_file(drive_service, f["id"], folder_id, archive_folder_id)
        print(f"  Archived: {f['name']}")

    # Execute renames
    for f, new_name in rename_actions:
        rename_file(drive_service, f["id"], new_name)
        print(f"  Renamed:  {f['name']} → {new_name}")

    print()
    print(f"Done. Active in folder: {len(grouped)} project artifact(s).")
    print(f"Archived: {len(archive_actions)} old version(s).")


if __name__ == "__main__":
    main()
