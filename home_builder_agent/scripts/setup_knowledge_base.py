"""setup_knowledge_base.py — Create Baldwin County municipality folder structure in Drive.

Finds the existing KNOWLEDGE BASE folder inside Home Building Agent V.1, then
ensures a "Baldwin County, AL" subfolder exists with one sub-subfolder per
municipality.  Safe to re-run: prints "Already exists" for folders already present.

Usage:
    python home_builder_agent/scripts/setup_knowledge_base.py
"""

import json

from home_builder_agent.core.auth import get_credentials
from home_builder_agent.integrations.drive import find_folder_by_path, drive_service

MUNICIPALITIES = [
    "Baldwin County (Unincorporated)",
    "Bay Minette",
    "Daphne",
    "Fairhope",
    "Foley",
    "Gulf Shores",
    "Orange Beach",
    "Spanish Fort",
    "Robertsdale",
    "Loxley",
    "Elberta",
    "Silverhill",
    "Summerdale",
    "Stapleton",
    "Stockton",
    "Perdido Beach",
    "Lillian",
    "Magnolia Springs",
]


def _find_or_create_folder(service, folder_name: str, parent_id: str) -> tuple[str, bool]:
    """Return (folder_id, created).  created=False means it already existed."""
    escaped = folder_name.replace("'", r"\'")
    query = (
        f"name='{escaped}' "
        "and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        "and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id,name)").execute()
    folders = results.get("files", [])
    if folders:
        return folders[0]["id"], False
    new_folder = service.files().create(
        body={
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
    ).execute()
    return new_folder["id"], True


def main():
    creds = get_credentials()
    svc = drive_service(creds)

    # Step 1: find the existing KNOWLEDGE BASE folder
    kb_id = find_folder_by_path(svc, ["Home Building Agent V.1", "Home Builder Agent V.1", "KNOWLEDGE BASE"])
    print(f"Found KNOWLEDGE BASE: {kb_id}")

    # Step 2: ensure "Baldwin County, AL" subfolder
    bc_id, created = _find_or_create_folder(svc, "Baldwin County, AL", kb_id)
    label = "Created" if created else "Already exists"
    print(f"{label}: Baldwin County, AL  (id={bc_id})")

    # Step 3: create one subfolder per municipality
    municipality_ids: dict[str, str] = {}
    for name in MUNICIPALITIES:
        folder_id, created = _find_or_create_folder(svc, name, bc_id)
        label = "Created" if created else "Already exists"
        print(f"  {label}: {name}  (id={folder_id})")
        municipality_ids[name] = folder_id

    print("\nMunicipality → folder ID map:")
    print(json.dumps(municipality_ids, indent=2))
    return municipality_ids


if __name__ == "__main__":
    main()
