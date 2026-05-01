from __future__ import annotations

from typing import Any

from services.eep_worker.app import presigned_inputs


class _FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_presigned_url(
        self,
        operation: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str:
        self.calls.append(
            {
                "operation": operation,
                "Params": Params,
                "ExpiresIn": ExpiresIn,
            }
        )
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?sig=1"


def test_runpod_endpoint_presigns_s3_uri(monkeypatch) -> None:
    fake = _FakeS3Client()
    presigned_inputs._s3_client.cache_clear()
    monkeypatch.setenv("RUNPOD_INPUT_URL_TTL_SECONDS", "900")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: fake)

    result = presigned_inputs.maybe_presign_input_uri(
        "s3://libraryai/jobs/j1/proxy/1.png",
        "https://abc-8001.proxy.runpod.net/v1/geometry",
    )

    assert result == "https://s3.example/libraryai/jobs/j1/proxy/1.png?sig=1"
    assert fake.calls == [
        {
            "operation": "get_object",
            "Params": {"Bucket": "libraryai", "Key": "jobs/j1/proxy/1.png"},
            "ExpiresIn": 900,
        }
    ]


def test_non_runpod_endpoint_keeps_s3_uri(monkeypatch) -> None:
    presigned_inputs._s3_client.cache_clear()
    monkeypatch.delenv("RUNPOD_PRESIGN_INPUTS", raising=False)

    result = presigned_inputs.maybe_presign_input_uri(
        "s3://libraryai/jobs/j1/proxy/1.png",
        "http://iep1a:8001/v1/geometry",
    )

    assert result == "s3://libraryai/jobs/j1/proxy/1.png"


def test_always_mode_presigns_non_runpod_endpoint(monkeypatch) -> None:
    fake = _FakeS3Client()
    presigned_inputs._s3_client.cache_clear()
    monkeypatch.setenv("RUNPOD_PRESIGN_INPUTS", "always")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: fake)

    result = presigned_inputs.maybe_presign_input_uri(
        "s3://libraryai/jobs/j1/proxy/1.png",
        "http://iep1a:8001/v1/geometry",
    )

    assert result.startswith("https://s3.example/")
