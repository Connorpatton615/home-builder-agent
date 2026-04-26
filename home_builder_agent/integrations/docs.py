"""docs.py — Google Docs API formatting.

Three concerns isolated here:
  1. Document-wide margins
  2. Paragraph spacing across the body
  3. Converting markdown checkbox markers ([ ], [x]) to native checkbox bullets

These are the formatting passes the timeline generator runs after Drive
auto-converts the markdown→HTML upload.
"""

from googleapiclient.discovery import build

from home_builder_agent.config import (
    DOC_MARGIN_PT,
    PARA_LINE_SPACING_PCT,
    PARA_SPACE_AFTER_PT,
    PARA_SPACE_BEFORE_PT,
)


def docs_service(creds):
    """Build a Docs v1 service."""
    return build("docs", "v1", credentials=creds)


def apply_doc_formatting(creds, doc_id):
    """Apply margins + paragraph spacing + checkbox conversion to a Doc.

    Three layers, each with its own batchUpdate call (each layer mutates
    document indices, so we re-read the doc between layers).
    """
    service = docs_service(creds)

    # --- 1. Document-wide margins ---
    margin_request = {
        "updateDocumentStyle": {
            "documentStyle": {
                "marginTop":    {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
                "marginBottom": {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
                "marginLeft":   {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
                "marginRight":  {"magnitude": DOC_MARGIN_PT, "unit": "PT"},
            },
            "fields": "marginTop,marginBottom,marginLeft,marginRight",
        }
    }
    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": [margin_request]}
    ).execute()

    # --- 2. Paragraph spacing across whole body ---
    doc = service.documents().get(documentId=doc_id).execute()
    end_index = doc.get("body", {}).get("content", [])[-1].get("endIndex", 1)

    spacing_request = {
        "updateParagraphStyle": {
            "range": {"startIndex": 1, "endIndex": end_index - 1},
            "paragraphStyle": {
                "spaceAbove": {"magnitude": PARA_SPACE_BEFORE_PT, "unit": "PT"},
                "spaceBelow": {"magnitude": PARA_SPACE_AFTER_PT, "unit": "PT"},
                "lineSpacing": PARA_LINE_SPACING_PCT,
            },
            "fields": "spaceAbove,spaceBelow,lineSpacing",
        }
    }
    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": [spacing_request]}
    ).execute()

    # --- 3. Checkbox conversion ([ ]/[x] markdown → native Docs checkboxes) ---
    convert_checkboxes(service, doc_id)


def convert_checkboxes(service, doc_id):
    """Find paragraphs starting with [ ] or [x] and convert to checkbox bullets.

    Two-pass algorithm:
      Pass A — delete the literal '[ ] ' / '[x] ' marker from the paragraph
               text (4 chars each). Process in REVERSE document order so
               earlier deletions don't shift later indices.
      Pass B — re-read doc, locate the now-marker-free paragraphs by text
               match, apply BULLET_CHECKBOX preset.

    Limitation: Docs API today doesn't reliably expose 'check' state via
    createParagraphBullets, so [x] marked items render as unchecked checkboxes
    and Chad clicks once to confirm. Acceptable for MVP.
    """
    doc = service.documents().get(documentId=doc_id).execute()

    targets = []  # (startIndex, endIndex, was_checked, full_text)
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        text = ""
        for el in para.get("elements", []):
            tr = el.get("textRun")
            if tr:
                text += tr.get("content", "")
        text_stripped = text.lstrip()
        if text_stripped.startswith("[ ] ") or text_stripped.startswith("[x] "):
            was_checked = text_stripped.startswith("[x] ")
            targets.append((
                element.get("startIndex"),
                element.get("endIndex"),
                was_checked,
                text,
            ))

    if not targets:
        return

    # --- Pass A: delete markers (reverse order so indices stay stable) ---
    delete_requests = []
    for start_idx, _, _, full_text in reversed(targets):
        marker_offset = full_text.find("[")
        marker_abs_start = start_idx + marker_offset
        marker_abs_end = marker_abs_start + 4  # "[ ] " or "[x] " is 4 chars
        delete_requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": marker_abs_start,
                    "endIndex": marker_abs_end,
                }
            }
        })

    if delete_requests:
        service.documents().batchUpdate(
            documentId=doc_id, body={"requests": delete_requests}
        ).execute()

    # --- Pass B: locate marker-free paragraphs, apply checkbox bullets ---
    doc = service.documents().get(documentId=doc_id).execute()

    expected_task_texts = []
    for _, _, _, full_text in targets:
        stripped = full_text.replace("[ ] ", "", 1).replace("[x] ", "", 1)
        expected_task_texts.append(stripped.strip())

    bullet_requests = []
    matched = set()
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        text = ""
        for el in para.get("elements", []):
            tr = el.get("textRun")
            if tr:
                text += tr.get("content", "")
        text_clean = text.strip()
        for i, expected in enumerate(expected_task_texts):
            if i in matched or not expected:
                continue
            if text_clean == expected:
                bullet_requests.append({
                    "createParagraphBullets": {
                        "range": {
                            "startIndex": element.get("startIndex"),
                            "endIndex": element.get("endIndex") - 1,
                        },
                        "bulletPreset": "BULLET_CHECKBOX",
                    }
                })
                matched.add(i)
                break

    if bullet_requests:
        service.documents().batchUpdate(
            documentId=doc_id, body={"requests": bullet_requests}
        ).execute()
        print(f"  Converted {len(bullet_requests)} task lines to checkboxes.")
