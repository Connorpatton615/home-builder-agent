# jurisdiction.py — maps a Baldwin County zip code to the governing municipality
# Used by agents to pull the right compliance folder/doc from the knowledge base.

ZIP_TO_MUNICIPALITY = {
    "36507": "Bay Minette",
    "36511": "Baldwin County (Unincorporated)",  # Bon Secour
    "36526": "Daphne",
    "36527": "Spanish Fort",
    "36528": "Baldwin County (Unincorporated)",  # Fish River
    "36530": "Elberta",
    "36532": "Fairhope",
    "36533": "Fairhope",
    "36535": "Foley",
    "36542": "Gulf Shores",
    "36547": "Gulf Shores",
    "36549": "Lillian",
    "36551": "Loxley",
    "36555": "Magnolia Springs",
    "36561": "Orange Beach",
    "36562": "Perdido Beach",
    "36564": "Baldwin County (Unincorporated)",  # Point Clear
    "36567": "Robertsdale",
    "36574": "Baldwin County (Unincorporated)",  # Seminole
    "36576": "Silverhill",
    "36578": "Stapleton",
    "36579": "Stockton",
    "36580": "Summerdale",
    "36587": "Baldwin County (Unincorporated)",
}

MUNICIPALITY_KNOWLEDGE_PATH = ["Home Building Agent V.1", "KNOWLEDGE BASE", "Baldwin County, AL"]


def get_municipality(zip_code: str) -> str:
    """Return the governing municipality name for a Baldwin County zip code.
    Falls back to 'Baldwin County (Unincorporated)' for unknown zips."""
    return ZIP_TO_MUNICIPALITY.get(str(zip_code).strip(), "Baldwin County (Unincorporated)")


def get_knowledge_folder_path(zip_code: str) -> list[str]:
    """Return the full Drive folder path list for the compliance docs for this zip."""
    municipality = get_municipality(zip_code)
    return MUNICIPALITY_KNOWLEDGE_PATH + [municipality]
