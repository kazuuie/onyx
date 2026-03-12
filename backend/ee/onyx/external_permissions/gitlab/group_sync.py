from collections.abc import Generator

from ee.onyx.db.external_perm import ExternalUserGroup
from ee.onyx.external_permissions.gitlab.utils import get_external_user_groups  # 後述のutil関数
from gitlab.v4.objects import Project
from onyx.connectors.gitlab.connector import GitlabConnector
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()


def gitlab_group_sync(
    tenant_id: str,  # noqa: ARG001
    cc_pair: ConnectorCredentialPair,
) -> Generator[ExternalUserGroup, None, None]:
    # 1. Connectorの初期化
    gitlab_connector: GitlabConnector = GitlabConnector(**cc_pair.connector.connector_specific_config)

    # 2. 認証情報のロード
    credential_json = cc_pair.credential.credential_json.get_value(apply_mask=False) if cc_pair.credential.credential_json else {}
    gitlab_connector.load_credentials(credential_json)

    if not gitlab_connector.gitlab_client:
        raise ValueError("gitlab_client is required")

    logger.info("Starting GitLab group sync...")

    # 3. 設定されたオーナー/リポジトリに基づきプロジェクト一覧を取得
    # GitlabConnector._fetch_configured_projects() を利用
    try:
        projects: list[Project] = gitlab_connector._fetch_configured_projects()
    except Exception as e:
        logger.error(f"Failed to fetch configured projects: {e}")
        return

    # 4. 各プロジェクトに関連するグループ（権限セット）を抽出
    for project in projects:
        try:
            # プロジェクト単位、および所属グループ単位のメンバー情報を生成してyield
            yield from get_external_user_groups(project, gitlab_connector.gitlab_client)
        except Exception as e:
            logger.error(f"Error processing project {project.id} ({project.path_with_namespace}): {e}")
