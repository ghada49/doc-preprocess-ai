from __future__ import annotations

from services.eep.app.gates.artifact_validation import (
    ArtifactHardCheckResult,
    ArtifactValidationResult,
)
from services.eep_worker.app.worker_loop import _newspaper_iep1d_unavailable_fallback_allowed


def _validation(passed: bool) -> ArtifactValidationResult:
    return ArtifactValidationResult(
        hard_result=ArtifactHardCheckResult(
            passed=passed,
            failed_checks=[] if passed else ["file_exists"],
        ),
        soft_score=0.8 if passed else None,
        signal_scores={} if passed else None,
        soft_passed=passed if passed else None,
        passed=passed,
    )


def test_iep1d_unavailable_can_fallback_for_valid_newspaper_artifact() -> None:
    assert _newspaper_iep1d_unavailable_fallback_allowed("newspaper", _validation(True)) is True


def test_iep1d_unavailable_does_not_fallback_for_book() -> None:
    assert _newspaper_iep1d_unavailable_fallback_allowed("book", _validation(True)) is False


def test_iep1d_unavailable_does_not_fallback_for_invalid_newspaper_artifact() -> None:
    assert _newspaper_iep1d_unavailable_fallback_allowed("newspaper", _validation(False)) is False
