from collections.abc import Generator

from gitlab import Repository

from ee.onyx.db.external_perm import ExternalUserGroup
from ee.onyx.external_permissions.gitlab.utils import get_external_user_group
from onyx.connectors.gitlab.connector import GitlabConnector
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()


def gitlab_group_sync(
    tenant_id: str,  # noqa: ARG001
    cc_pair: ConnectorCredentialPair,
) -> Generator[ExternalUserGroup, None, None]:
    gitlab_connector: GitlabConnector = GitlabConnector(
        **cc_pair.connector.connector_specific_config
    )
    credential_json = (
        cc_pair.credential.credential_json.get_value(apply_mask=False)
        if cc_pair.credential.credential_json
        else {}
    )
    gitlab_connector.load_credentials(credential_json)
    if not gitlab_connector.gitlab_client:
        raise ValueError("gitlab_client is required")

    logger.info("Starting gitlab group sync...")
    repos: list[Repository.Repository] = []
    if gitlab_connector.repositories:
        if "," in gitlab_connector.repositories:
            # Multiple repositories specified
            repos = gitlab_connector.get_gitlab_repos(gitlab_connector.gitlab_client)
        else:
            # Single repository (backward compatibility)
            repos = [gitlab_connector.get_gitlab_repo(gitlab_connector.gitlab_client)]
    else:
        # All repositories
        repos = gitlab_connector.get_all_repos(gitlab_connector.gitlab_client)

    for repo in repos:
        try:
            for external_group in get_external_user_group(
                repo, gitlab_connector.gitlab_client
            ):
                logger.info(f"External group: {external_group}")
                yield external_group
        except Exception as e:
            logger.error(f"Error processing repository {repo.id} ({repo.name}): {e}")
