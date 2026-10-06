"""Contact lookup extension point.

Contacts are intentionally local until a contacts provider is selected.
"""


def lookup_contact(email: str) -> dict[str, str] | None:
    return {"email": email} if email.strip() else None
