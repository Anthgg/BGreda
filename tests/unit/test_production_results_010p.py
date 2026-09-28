from decimal import Decimal

import pytest

from app.core.production_results import result_matches_started_quantity


@pytest.mark.parametrize(
    ("started", "good", "scrap", "reason", "expected"),
    [
        ("30", "27", "3", "Rotura en quema", True),
        ("30", "30", "0", None, True),
        ("30", "29", "0", None, False),
        ("30", "31", "0", None, False),
        ("30", "27", "2", "Rotura", False),
        ("30", "27", "3", "  ", False),
        ("30", "-1", "31", "Rotura", False),
    ],
)
def test_result_accounts_for_every_started_unit(
    started: str, good: str, scrap: str, reason: str | None, expected: bool
) -> None:
    assert (
        result_matches_started_quantity(
            started_quantity=Decimal(started),
            good_quantity=Decimal(good),
            scrap_quantity=Decimal(scrap),
            scrap_reason=reason,
        )
        is expected
    )
