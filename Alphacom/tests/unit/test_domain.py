from datetime import date

import pytest

from app.ids import canonical_account_id
from app.upstreams import normalize_case, parse_entitlements


@pytest.mark.parametrize("raw,expected", [
    ("ACC-007", "ACC-007"), ("ACC-7", "ACC-007"), ("acc-77", "ACC-077"), ("ACC-0077", "ACC-077"), ("ACC-1234", "ACC-1234"),
])
def test_account_ids_are_canonicalized(raw, expected):
    assert canonical_account_id(raw) == expected


def test_acc_77_is_not_acc_007():
    # The sample CSV and the sample case use these two ids. They are different accounts.
    assert canonical_account_id("ACC-77") != canonical_account_id("ACC-007")


@pytest.mark.parametrize("raw", ["ACC-", "ACC-12a", "CUST-7", "", "ACC-1234567890"])
def test_bad_account_ids_rejected(raw):
    with pytest.raises(ValueError):
        canonical_account_id(raw)


def test_normalize_case_folds_inconsistent_keys():
    raw = {"id": "CS-1", "accountId": "ACC-77", "priority": "P1", "Status": "open",
           "openedAt": "2026-10-24T08:12:00Z", "subject": "x",
           "Comments": [{"author": "jdoe", "body": "Investigating", "at": "2026-10-24T09:00:00Z"}]}
    c = normalize_case(raw)
    assert c["status"] == "open"
    assert c["accountId"] == "ACC-077"
    assert c["comments"] == [{"id": "CS-1-C1", "author": "jdoe", "body": "Investigating", "createdAt": "2026-10-24T09:00:00Z"}]

    raw_lower = {**raw, "status": "in_progress", "comments": []}
    del raw_lower["Status"], raw_lower["Comments"]
    assert normalize_case(raw_lower)["status"] == "in_progress"


CSV = """account_id,name,support_tier,contract_end,arr_usd
ACC-007,Northwind,Platinum,2027-01-15,1250000
ACC-23,Halden,Platinum,2026-12-20,640000
ACC-031,Ridgeway,Silver,2027-02-01,95000
ACC-040,Broken,Platinum,not-a-date,1
ACC-041,Weird,Diamond,2027-01-01,1
"""
XML = """<accounts>
<account id="ACC-023"><tier>Platinum</tier><renewal>2027-12-20</renewal></account>
<account id="ACC-31"><tier>Gold</tier></account>
</accounts>"""


def test_xml_feed_overrides_csv_for_tier_and_renewal():
    ents = parse_entitlements(CSV, XML)
    # Halden renewed early: feed (newer) wins over the nightly export.
    assert ents["ACC-023"].renewal_date == date(2027, 12, 20)
    assert ents["ACC-023"].contract_end == date(2026, 12, 20)
    # Tier upgrade from the feed; renewal falls back to the CSV contract end.
    assert ents["ACC-031"].tier == "Gold"
    assert ents["ACC-031"].renewal_date == date(2027, 2, 1)


def test_bad_entitlement_rows_are_skipped_not_fatal():
    ents = parse_entitlements(CSV, None)
    assert "ACC-040" not in ents  # bad date
    assert "ACC-041" not in ents  # unknown tier
    assert set(ents) == {"ACC-007", "ACC-023", "ACC-031"}
