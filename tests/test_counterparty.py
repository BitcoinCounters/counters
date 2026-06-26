"""Unit tests for CounterpartyClient helpers that don't need a live node."""

from counters.counterparty import CounterpartyClient


def test_is_creation_plain():
    assert CounterpartyClient.is_creation({"asset_events": "creation"})


def test_is_creation_space_separated_with_lock():
    # Regression: a --locked named issuance reports "creation lock_quantity"
    # (space-separated). is_creation must still recognise the creation event.
    assert CounterpartyClient.is_creation({"asset_events": "creation lock_quantity"})


def test_is_creation_comma_separated():
    assert CounterpartyClient.is_creation({"asset_events": "creation,lock_quantity"})


def test_is_creation_reissuance_is_false():
    assert not CounterpartyClient.is_creation({"asset_events": "change_description"})
    assert not CounterpartyClient.is_creation({"asset_events": "lock_quantity"})


def test_is_creation_missing_or_empty():
    assert not CounterpartyClient.is_creation({})
    assert not CounterpartyClient.is_creation({"asset_events": ""})
    assert not CounterpartyClient.is_creation({"asset_events": None})


def test_is_valid():
    assert CounterpartyClient.is_valid({"status": "valid"})
    assert not CounterpartyClient.is_valid({"status": "invalid"})
    assert not CounterpartyClient.is_valid({})
