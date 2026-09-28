"""The error vocabulary Phase 2's honest-degradation logic reads."""

import pytest

from src.connectors.errors import (
    DEFAULT_ERROR_MAPPING,
    Action,
    Classification,
    FailureMode,
    FailureType,
    classify,
)
from src.models.errors import ErrorCode


def test_every_failure_mode_is_mapped():
    """A mode with no entry would hit ``classify``'s raise at runtime."""
    assert set(DEFAULT_ERROR_MAPPING) == set(FailureMode)


@pytest.mark.parametrize("mode", list(FailureMode))
def test_each_mode_maps_to_exactly_one_failure_type(mode):
    classification = classify(mode)
    assert isinstance(classification.failure_type, FailureType)
    assert isinstance(classification.action, Action)
    assert isinstance(classification.error_code, ErrorCode)


def test_timeout_is_transient_and_therefore_degradable():
    """The Phase 2 contract: a timed-out source becomes a partial result."""
    classification = classify(FailureMode.TIMEOUT)
    assert classification.failure_type is FailureType.TRANSIENT_ERROR
    assert classification.error_code is ErrorCode.SOURCE_TIMEOUT
    assert classification.degradable is True


def test_auth_failure_is_config_error_and_not_degradable():
    """A bad credential must fail the query, not be dressed up as partial.

    Returning the other source's rows and marking the result merely ``partial``
    would present a permanently broken connector as a temporary gap.
    """
    classification = classify(FailureMode.AUTH)
    assert classification.failure_type is FailureType.CONFIG_ERROR
    assert classification.action is Action.FAIL
    assert classification.error_code is ErrorCode.CONNECTOR_AUTH_ERROR
    assert classification.degradable is False


def test_throttle_maps_to_the_rate_limit_code():
    classification = classify(FailureMode.THROTTLED)
    assert classification.action is Action.RATE_LIMITED
    assert classification.error_code is ErrorCode.RATE_LIMIT_EXHAUSTED


def test_not_enabled_is_a_config_error():
    classification = classify(FailureMode.NOT_ENABLED)
    assert classification.error_code is ErrorCode.CONNECTOR_NOT_ENABLED
    assert classification.degradable is False


def test_only_transient_failures_are_degradable():
    """The whole point of carrying failure_type, asserted across the table."""
    for mode, classification in DEFAULT_ERROR_MAPPING.items():
        expected = classification.failure_type is FailureType.TRANSIENT_ERROR
        assert classification.degradable is expected, mode


def test_unmapped_mode_raises_rather_than_guessing():
    """LAW 4: no silent default.

    A default branch returning something plausible would let a newly-added
    failure mode be classified as retryable by accident.
    """
    with pytest.raises(ValueError, match="unmapped connector failure mode"):
        classify("meteor_strike")


def test_classification_is_immutable():
    """The mapping is module-level shared state; a mutable value would let one
    caller's edit change how every later failure is classified."""
    classification = classify(FailureMode.TIMEOUT)
    with pytest.raises(AttributeError):
        classification.failure_type = FailureType.CONFIG_ERROR


def test_action_enum_matches_the_design_doc_vocabulary():
    """Card 2's enum, kept complete so §5 of the design doc reads the same.

    The mapping below only uses four of these; the enum is the shared
    vocabulary, not a list of what the mock happens to produce.
    """
    assert {a.value for a in Action} == {
        "SUCCESS",
        "RETRY",
        "RATE_LIMITED",
        "REFRESH_TOKEN_THEN_RETRY",
        "FAIL",
        "IGNORE",
    }


def test_classification_can_be_built_directly_for_phase_two():
    """Phase 2 constructs these when it maps its own failures; keep it public."""
    built = Classification(Action.FAIL, FailureType.SYSTEM_ERROR, ErrorCode.SOURCE_TIMEOUT)
    assert built.degradable is False
