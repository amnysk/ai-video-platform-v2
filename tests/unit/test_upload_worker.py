"""upload worker の queue 分割・起動時検査（ADR-0020）。secret を表示しない（INV-20）。"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from contracts.states import UPLOAD_MEDIA_TASK_QUEUE, UPLOAD_TASK_QUEUE
from contracts.upload import DEFAULT_UPLOAD_CHUNK_BYTES, DEFAULT_UPLOAD_CONCURRENCY
from contracts.upload_activities import UPLOAD_ACTIVITY_NAMES, UPLOAD_FINAL_VIDEO
from infrastructure.config import Settings
from workers.upload.activities import UploadActivities


def _names(fns) -> set[str]:
    return {fn.__temporal_activity_definition.name for fn in fns}


def test_upload_activity_is_alone_on_the_media_queue(monkeypatch) -> None:
    import workers.upload.run_worker as rw

    acts = UploadActivities.__new__(UploadActivities)
    assert _names(UploadActivities.media_activities(acts)) == {UPLOAD_FINAL_VIDEO}
    assert _names(UploadActivities.state_activities(acts)) | {UPLOAD_FINAL_VIDEO} == set(
        UPLOAD_ACTIVITY_NAMES
    )
    created: list[dict] = []

    class _Worker:
        def __init__(self, client, **kwargs) -> None:
            created.append(kwargs)

    monkeypatch.setattr(rw, "Worker", _Worker)
    rw.build_workers(object(), acts)  # type: ignore[arg-type]
    state, media = created
    assert state["task_queue"] == UPLOAD_TASK_QUEUE and state["workflows"]
    assert media["task_queue"] == UPLOAD_MEDIA_TASK_QUEUE
    assert media["max_concurrent_activities"] == DEFAULT_UPLOAD_CONCURRENCY == 1


def test_config_chunk_default_is_the_contract_default() -> None:
    assert Settings.model_fields["youtube_chunk_bytes"].default == DEFAULT_UPLOAD_CHUNK_BYTES


async def test_missing_youtube_config_fails_fast_without_printing_secrets(tmp_path) -> None:
    from workers.upload.run_worker import build_uploader, require_channel_id

    async with httpx.AsyncClient() as client:
        with pytest.raises(SystemExit, match="YOUTUBE_CLIENT_ID"):
            build_uploader(Settings(youtube_client_id=None), client)
        with pytest.raises(SystemExit, match="YOUTUBE_CHANNEL_ID"):
            require_channel_id(Settings(youtube_channel_id="not-a-channel"))

        token = tmp_path / "token"
        token.write_text("super-secret-refresh-token")
        token.chmod(0o644)  # 0600 でない
        with pytest.raises(SystemExit) as info:
            build_uploader(
                Settings(
                    youtube_client_id="cid",
                    youtube_client_secret=SecretStr("very-secret-client"),
                    youtube_refresh_token_path=str(token),
                ),
                client,
            )
        message = str(info.value)
        assert "0600" in message
        assert "super-secret" not in message and "very-secret" not in message

        token.chmod(0o600)
        uploader = build_uploader(
            Settings(
                youtube_client_id="cid",
                youtube_client_secret=SecretStr("very-secret-client"),
                youtube_refresh_token_path=str(token),
            ),
            client,
        )
        assert "secret" not in repr(uploader)
