"""Regression guard for supports_temperature() endpoint coverage.

Bug found 2026-08-20: databricks-gpt-5-mini rejects temperature=0 with a 400
("Only the default (1) value is supported"), exactly like the gpt-5-6
family, but _NO_TEMPERATURE_RE only excluded "gpt-5-6" — every
/compare/summarize call on an image was failing in production, since
COMPARE_SUMMARY_IMAGE_ENDPOINT defaults to databricks-gpt-5-mini. Confirmed
live against the endpoint before fixing.
"""

from server.services.streaming import supports_temperature


def test_bare_gpt_5_mini_does_not_support_temperature():
    assert supports_temperature('databricks-gpt-5-mini') is False


def test_gpt_5_4_mini_still_supports_temperature():
    # Distinct model, confirmed live to accept temperature=0 — the regex
    # must not over-match and exclude it too.
    assert supports_temperature('databricks-gpt-5-4-mini') is True


def test_gpt_5_6_family_does_not_support_temperature():
    assert supports_temperature('databricks-gpt-5-6-luna') is False
    assert supports_temperature('databricks-gpt-5-6-terra') is False


def test_other_endpoints_support_temperature():
    assert supports_temperature('databricks-claude-sonnet-4-6') is True
    assert supports_temperature('databricks-gemini-3-1-flash-lite') is True
