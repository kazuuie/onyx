from collections.abc import Generator

from ee.onyx.db.external_perm import ExternalUserGroup
from ee.onyx.external_permissions.gitlab.utils import get_external_user_groups
from gitlab.v4.objects import Project
from onyx.connectors.gitlab.connector import GitlabConnector
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()


def build_user_email_cache(gitlab_client) -> dict[int, str]:
    cache = {}
    all_users = gitlab_client.users.list(get_all=True)
    for user in all_users:
        cache[user.id] = getattr(user, "email", None) or getattr(user, "public_email", "")
    return cache


def gitlab_group_sync(
    tenant_id: str,  # noqa: ARG001
    cc_pair: ConnectorCredentialPair,
) -> Generator[ExternalUserGroup, None, None]:
    # 1. Initialize the connector
    gitlab_connector: GitlabConnector = GitlabConnector(**cc_pair.connector.connector_specific_config)

    # 2. Load credentials
    credential_json = cc_pair.credential.credential_json.get_value(apply_mask=False) if cc_pair.credential.credential_json else {}
    gitlab_connector.load_credentials(credential_json)

    if not gitlab_connector.gitlab_client:
        raise ValueError("gitlab_client is required")

    logger.info("Starting GitLab group sync...")

    try:
        user_email_cache = build_user_email_cache(gitlab_connector.gitlab_client)
        logger.info(f"Successfully built user email cache for {len(user_email_cache)} users.")
    except Exception as e:
        logger.error(f"Failed to build user email cache: {e}")
        user_email_cache = {}

    # 3. Fetch configured projects based on the provided owner/repository settings
    try:
        projects: list[Project] = gitlab_connector._fetch_configured_projects()
    except Exception as e:
        logger.error(f"Failed to fetch configured projects: {e}")
        return

    user_email_cache = {}

    # 4. Extract groups (permission sets) related to each project
    for project in projects:
        try:
            # Generate and yield member information per project and its associated groups
            yield from get_external_user_groups(project, gitlab_connector.gitlab_client, user_email_cache)
        except Exception as e:
            logger.error(f"Error processing project {project.id} ({project.path_with_namespace}): {e}")
