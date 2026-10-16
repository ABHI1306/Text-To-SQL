"""The editable host allowlist: link-local and metadata addresses stay refused."""

import pytest

from app.api.security import host_is_forbidden, validate_allowed_hosts


@pytest.mark.parametrize(
    "host", ["169.254.169.254", "169.254.1.1", "fe80::1", "metadata.google.internal", "Metadata."]
)
def test_link_local_and_metadata_hosts_are_forbidden(host: str) -> None:
    assert host_is_forbidden(host)


@pytest.mark.parametrize("host", ["db.internal", "10.0.0.5", "localhost", "demo-db"])
def test_ordinary_hosts_are_not_forbidden(host: str) -> None:
    assert not host_is_forbidden(host)


def test_a_valid_list_is_normalised_and_deduplicated() -> None:
    assert validate_allowed_hosts([" DB.Internal ", "db.internal", "10.0.0.0/24"]) == [
        "db.internal", "10.0.0.0/24",
    ]


@pytest.mark.parametrize(
    "entry", ["169.254.169.254", "169.254.0.0/16", "10.0.0.0/8, 169.254.0.0/15", "", "a b", "x@y"]
)
def test_forbidden_or_malformed_entries_are_refused(entry: str) -> None:
    with pytest.raises(ValueError):
        validate_allowed_hosts([entry])
