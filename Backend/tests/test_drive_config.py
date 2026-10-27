from __future__ import annotations

import json

import pytest

from science_adapter.upload_to_drive import DriveUploadError, load_drive_config


CLIENT = {
    "web": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}
BASE_ENV = {
    "GOOGLE_DRIVE_REFRESH_TOKEN": "test-refresh-token",
    "GOOGLE_DRIVE_FOLDER_ID": "folderId0123456789",
}


def test_load_drive_config_supports_inline_json() -> None:
    config = load_drive_config(BASE_ENV | {"GOOGLE_DRIVE_OAUTH_CLIENT_JSON": json.dumps(CLIENT)})

    assert config.client_id == CLIENT["web"]["client_id"]
    assert config.client_secret == CLIENT["web"]["client_secret"]
    assert config.token_uri == CLIENT["web"]["token_uri"]
    assert config.refresh_token == BASE_ENV["GOOGLE_DRIVE_REFRESH_TOKEN"]
    assert config.parent_folder_id == BASE_ENV["GOOGLE_DRIVE_FOLDER_ID"]


def test_load_drive_config_supports_secret_file(tmp_path) -> None:
    client_file = tmp_path / "google_drive_oauth_client.json"
    client_file.write_text(json.dumps(CLIENT), encoding="utf-8")

    config = load_drive_config(BASE_ENV | {"GOOGLE_DRIVE_OAUTH_CLIENT_JSON_FILE": str(client_file)})

    assert config.client_id == CLIENT["web"]["client_id"]
    assert config.client_secret == CLIENT["web"]["client_secret"]
    assert config.token_uri == CLIENT["web"]["token_uri"]


def test_secret_file_takes_precedence_over_inline_json(tmp_path) -> None:
    client_file = tmp_path / "google_drive_oauth_client.json"
    client_file.write_text(json.dumps(CLIENT), encoding="utf-8")
    env = BASE_ENV | {
        "GOOGLE_DRIVE_OAUTH_CLIENT_JSON": "not-json",
        "GOOGLE_DRIVE_OAUTH_CLIENT_JSON_FILE": str(client_file),
    }

    config = load_drive_config(env)

    assert config.client_id == CLIENT["web"]["client_id"]


def test_unreadable_secret_file_has_safe_error(tmp_path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(DriveUploadError, match="secret file is unavailable or unreadable"):
        load_drive_config(BASE_ENV | {"GOOGLE_DRIVE_OAUTH_CLIENT_JSON_FILE": str(missing)})


def test_oauth_client_must_use_web_credentials() -> None:
    installed = {"installed": CLIENT["web"]}
    with pytest.raises(DriveUploadError, match="must contain web credentials"):
        load_drive_config(BASE_ENV | {"GOOGLE_DRIVE_OAUTH_CLIENT_JSON": json.dumps(installed)})
