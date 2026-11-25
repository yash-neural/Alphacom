import re

_ACCOUNT = re.compile(r"^ACC-(\d{1,9})$", re.IGNORECASE)


def canonical_account_id(raw: str) -> str:
    """The upstreams disagree on padding (CSV has ACC-77, cases have ACC-077).

    Canonical form is the integer padded to 3 digits, so ACC-77, ACC-077 and
    ACC-0077 are the same account. ACC-77 is *not* ACC-007; we don't guess.
    """
    m = _ACCOUNT.match(raw.strip())
    if not m:
        raise ValueError(f"not an account id: {raw!r}")
    return f"ACC-{int(m.group(1)):03d}"
