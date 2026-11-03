"""Unit tests for the Pydantic input models (no server process needed)."""

import math

import pytest
from pydantic import ValidationError

from server import GetCustomerRecordInput, TriggerRefundInput

VALID_REASON = "Item arrived damaged"


# ----------------------------- customer_id ---------------------------------

@pytest.mark.parametrize("cid", ["CUST-00000", "CUST-00042", "CUST-99999"])
def test_customer_id_valid(cid):
    assert GetCustomerRecordInput(customer_id=cid).customer_id == cid


@pytest.mark.parametrize("cid", [
    "cust-00042",      # lowercase prefix
    "CUST-0042",       # 4 digits
    "CUST-000420",     # 6 digits
    "CUST-ABCDE",      # letters instead of digits
    "CUST00042",       # missing hyphen
    "CUST_00042",      # wrong separator
    " CUST-00042",     # leading whitespace
    "CUST-00042\n",    # trailing newline (classic regex '$' trap)
    "CUST-٠٠٠٤٢",      # Arabic-Indic digits (\d would match Unicode digits)
    "",
])
def test_customer_id_bad_format(cid):
    with pytest.raises(ValidationError):
        GetCustomerRecordInput(customer_id=cid)


@pytest.mark.parametrize("cid", [12345, None, ["CUST-00042"], {"id": "CUST-00042"}, True])
def test_customer_id_wrong_type(cid):
    with pytest.raises(ValidationError):
        GetCustomerRecordInput(customer_id=cid)


def test_customer_id_missing():
    with pytest.raises(ValidationError):
        GetCustomerRecordInput.model_validate({})


def test_unknown_field_rejected():
    with pytest.raises(ValidationError) as exc:
        GetCustomerRecordInput.model_validate({"customer_id": "CUST-00042", "customerId": "x"})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


# ----------------------------- amount --------------------------------------

@pytest.mark.parametrize("amount, expected", [(0.01, 0.01), (49.99, 49.99), (100, 100.0), (10_000, 10_000.0)])
def test_amount_valid(amount, expected):
    m = TriggerRefundInput(customer_id="CUST-00042", amount=amount, reason=VALID_REASON)
    assert m.amount == expected


@pytest.mark.parametrize("amount", [
    0, 0.0, -0.0, -1, -0.01,       # not positive
    10_000.01, 1e9,                # above the cap
    10.999, 0.001,                 # more than 2 decimals
    math.nan, math.inf, -math.inf, # non-finite
])
def test_amount_bad_value(amount):
    with pytest.raises(ValidationError):
        TriggerRefundInput(customer_id="CUST-00042", amount=amount, reason=VALID_REASON)


@pytest.mark.parametrize("amount", ["50", "50.00", None, True, [50], {"value": 50}])
def test_amount_wrong_type(amount):
    # Strict mode: no string->float coercion, and booleans are not numbers.
    with pytest.raises(ValidationError):
        TriggerRefundInput(customer_id="CUST-00042", amount=amount, reason=VALID_REASON)


def test_amount_float_noise_tolerated():
    # 0.1 + 0.2 == 0.30000000000000004 in binary floating point; still 2 decimals.
    m = TriggerRefundInput(customer_id="CUST-00042", amount=0.1 + 0.2, reason=VALID_REASON)
    assert m.amount == 0.3


# ----------------------------- reason --------------------------------------

def test_reason_exactly_10_chars_ok():
    m = TriggerRefundInput(customer_id="CUST-00042", amount=5, reason="0123456789")
    assert m.reason == "0123456789"


def test_reason_is_stripped():
    m = TriggerRefundInput(customer_id="CUST-00042", amount=5, reason="   damaged item   ")
    assert m.reason == "damaged item"


@pytest.mark.parametrize("reason", [
    "",
    "short",
    "123456789",          # 9 chars
    " " * 20,             # whitespace only
    "   short    ",       # 5 chars after stripping
    "x" * 501,            # over max length
])
def test_reason_bad_length(reason):
    with pytest.raises(ValidationError):
        TriggerRefundInput(customer_id="CUST-00042", amount=5, reason=reason)


@pytest.mark.parametrize("reason", [None, 1234567890, ["a long enough reason"]])
def test_reason_wrong_type(reason):
    with pytest.raises(ValidationError):
        TriggerRefundInput(customer_id="CUST-00042", amount=5, reason=reason)


def test_all_errors_reported_at_once():
    with pytest.raises(ValidationError) as exc:
        TriggerRefundInput.model_validate({"customer_id": "bad", "amount": -1, "reason": "x"})
    assert {e["loc"][0] for e in exc.value.errors()} == {"customer_id", "amount", "reason"}


# ----------------------------- generated schema ----------------------------

def test_schema_advertises_constraints():
    schema = TriggerRefundInput.model_json_schema()
    props = schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"customer_id", "amount", "reason"}
    assert props["customer_id"]["pattern"] == r"^CUST-[0-9]{5}$"
    assert props["amount"]["exclusiveMinimum"] == 0
    assert props["reason"]["minLength"] == 10
