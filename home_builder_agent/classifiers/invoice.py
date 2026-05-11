"""classifiers/invoice.py — Invoice/receipt signal detection and data extraction.

Three entry points:

  is_invoice_email()    — pure rule-based pre-filter (no API call, free)
  extract_invoice_data() — Sonnet-based structured extraction from email text
  extract_receipt_data() — Sonnet-based vision extraction from a receipt photo

Used by the Finance Office agent and any future watcher that processes
supplier emails or receipt uploads for Palmetto Custom Homes.
"""

import json
import re

from home_builder_agent.config import WRITER_MODEL

# ---------------------------------------------------------------------------
# Keywords that suggest an invoice/billing email
# ---------------------------------------------------------------------------
_INVOICE_KEYWORDS = [
    "invoice",
    "bill ",
    "billing",
    "statement",
    "payment due",
    "balance due",
    "amount due",
    "please pay",
    "remittance",
    "estimate",
    "proposal",
    "quote",
    "receipt",
]

# ---------------------------------------------------------------------------
# Construction cost categories (mirrors Finance Office sheet sections)
# ---------------------------------------------------------------------------
_COST_CATEGORIES = [
    "Permits & Fees",
    "Site Work",
    "Footings & Foundation",
    "Concrete Slabs",
    "Framing",
    "Structural Steel",
    "Windows & Exterior Doors",
    "Roofing & Gutters",
    "Mechanical Systems",
    "Exterior Veneer",
    "Insulation & Drywall",
    "Cabinets & Countertops",
    "Fireplace / Hearth / Mantle",
    "Interior Trim & Stairs",
    "Flooring",
    "Wall Coverings & Paint",
    "Appliance Package",
    "Landscaping & Site Improvements",
    "Clean-Up",
    "General Conditions",
]


def is_invoice_email(subject: str, snippet: str, attachment_names: list | None = None) -> bool:
    """Return True if the email looks like an invoice, bill, or statement.

    Pure rule-based — no API call. Intended as a fast pre-filter before
    calling extract_invoice_data().

    Args:
        subject: Email subject line.
        snippet: Short preview text from the email body.
        attachment_names: Optional list of attachment filenames.

    Returns:
        True if any invoice signal is detected.
    """
    haystack = (subject + " " + snippet).lower()

    for keyword in _INVOICE_KEYWORDS:
        if keyword in haystack:
            return True

    if attachment_names:
        for name in attachment_names:
            if name.lower().endswith(".pdf"):
                return True

    return False


def extract_invoice_data(
    client,
    from_name: str,
    from_email: str,
    subject: str,
    body_text: str,
) -> tuple[dict, object]:
    """Extract structured invoice data from an email using Sonnet.

    Uses WRITER_MODEL (Sonnet) for accuracy — financial figures must be right.

    Args:
        client: Anthropic client (from core.claude_client.make_client).
        from_name: Sender display name.
        from_email: Sender email address.
        subject: Email subject line.
        body_text: Full or truncated email body text.

    Returns:
        (data_dict, response.usage) where data_dict contains:
            vendor, invoice_number, amount, invoice_date, due_date,
            description, job_hint
        Never returns None values — uses "" or 0.0 for missing fields.
    """
    _safe_default = {
        "vendor": from_name or from_email,
        "invoice_number": "",
        "amount": 0.0,
        "invoice_date": "",
        "due_date": "",
        "description": "",
        "job_hint": "",
    }

    prompt = f"""You are parsing a construction invoice email for Palmetto Custom Homes, a luxury home builder in Baldwin County, AL.

EMAIL DETAILS:
- From: {from_name} <{from_email}>
- Subject: {subject}
- Body:
{body_text}

Extract the invoice data and return ONLY a JSON object (no fence, no preamble):
{{
  "vendor": "<company or person who sent the invoice>",
  "invoice_number": "<invoice # or empty string>",
  "amount": <total amount due as a number, 0.0 if unknown>,
  "invoice_date": "<YYYY-MM-DD or empty string>",
  "due_date": "<YYYY-MM-DD or empty string — if payment terms say net 30, calculate from invoice_date>",
  "description": "<brief description of what the invoice covers>",
  "job_hint": "<any project name, lot number, address, or job reference found in the email, or empty string>"
}}

Rules:
- amount must be a number (float), never a string.
- Dates must be YYYY-MM-DD strings or empty string — never null.
- All string fields must be strings — never null.
- If the vendor name is not in the body, use the From name."""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    try:
        data = json.loads(text)
        # Enforce safe types — no None values
        data.setdefault("vendor", _safe_default["vendor"])
        data.setdefault("invoice_number", "")
        data.setdefault("description", "")
        data.setdefault("invoice_date", "")
        data.setdefault("due_date", "")
        data.setdefault("job_hint", "")
        if not isinstance(data.get("amount"), (int, float)):
            data["amount"] = 0.0
        # Coerce None to safe defaults
        for key in ("vendor", "invoice_number", "invoice_date", "due_date", "description", "job_hint"):
            if data.get(key) is None:
                data[key] = ""
        if data.get("amount") is None:
            data["amount"] = 0.0
    except json.JSONDecodeError:
        data = _safe_default.copy()

    return data, response.usage


def extract_receipt_data(
    client,
    image_base64: str,
    media_type: str = "image/jpeg",
) -> tuple[dict, object]:
    """Extract structured data from a receipt photo using Sonnet vision.

    Args:
        client: Anthropic client (from core.claude_client.make_client).
        image_base64: Base64-encoded image bytes (no data URI prefix).
        media_type: MIME type of the image (default "image/jpeg").

    Returns:
        (data_dict, response.usage) where data_dict contains:
            vendor, date, total, line_items, category_guess
        Never returns None values — uses "" / 0.0 / [] for missing fields.
    """
    _safe_default = {
        "vendor": "",
        "date": "",
        "total": 0.0,
        "line_items": [],
        "category_guess": "General Conditions",
    }

    categories_str = "\n".join(f"  - {c}" for c in _COST_CATEGORIES)

    prompt = f"""You are reading a receipt photo for a custom home construction project in Baldwin County, AL. Extract the data as JSON.

Return ONLY a JSON object (no fence, no preamble):
{{
  "vendor": "<store or company name>",
  "date": "<YYYY-MM-DD or empty string>",
  "total": <total amount as a number>,
  "line_items": [
    {{"description": "<item description>", "amount": <amount as a number>}}
  ],
  "category_guess": "<best matching construction cost category from the list below>"
}}

Construction cost categories (pick exactly one):
{categories_str}

Rules:
- total and all amount values must be numbers (float), never strings.
- date must be YYYY-MM-DD or empty string.
- line_items may be an empty list if individual items are not legible.
- category_guess must be one of the listed categories."""

    response = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_base64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    try:
        data = json.loads(text)
        data.setdefault("vendor", "")
        data.setdefault("date", "")
        data.setdefault("line_items", [])
        data.setdefault("category_guess", "General Conditions")
        # Coerce None / wrong types
        if data.get("vendor") is None:
            data["vendor"] = ""
        if data.get("date") is None:
            data["date"] = ""
        if not isinstance(data.get("total"), (int, float)):
            data["total"] = 0.0
        if data.get("total") is None:
            data["total"] = 0.0
        if not isinstance(data.get("line_items"), list):
            data["line_items"] = []
        if data.get("category_guess") not in _COST_CATEGORIES:
            data["category_guess"] = "General Conditions"
        # Validate line items
        clean_items = []
        for item in data["line_items"]:
            if isinstance(item, dict):
                clean_items.append({
                    "description": item.get("description") or "",
                    "amount": float(item["amount"]) if isinstance(item.get("amount"), (int, float)) else 0.0,
                })
        data["line_items"] = clean_items
    except json.JSONDecodeError:
        data = _safe_default.copy()

    return data, response.usage
