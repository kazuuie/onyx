import os
from typing import Any

import pytest


@pytest.fixture
def gitlab_connector_config() -> dict[str, Any]:
    # GitLabのURL（デフォルトはSaaS版）
    gitlab_base_url = os.environ.get("GITLAB_BASE_URL", "https://gitlab.com")

    return {
        "gitlab_base_url": gitlab_base_url,
        "include_projects": "",  # 空の場合は全プロジェクト対象とする想定
    }


@pytest.fixture
def gitlab_credential_json() -> dict[str, Any]:
    # GitLabのPersonal Access Token
    access_token = os.environ.get("GITLAB_ACCESS_TOKEN")

    assert access_token, "GITLAB_ACCESS_TOKEN environment variable is required"

    return {
        "gitlab_access_token": access_token,
    }
