import json
from collections.abc import Generator

from ee.onyx.external_permissions.gitlab.utils import (
    get_external_access_permission,
)
from ee.onyx.external_permissions.perm_sync_types import FetchAllDocumentsFunction, FetchAllDocumentsIdsFunction
from onyx.access.models import DocExternalAccess
from onyx.connectors.gitlab.connector import DocMetadata, GitlabConnector
from onyx.db.models import ConnectorCredentialPair
from onyx.db.utils import DocumentRow, SortOrder
from onyx.indexing.indexing_heartbeat import IndexingHeartbeatInterface
from onyx.utils.logger import setup_logger

logger = setup_logger()

GITLAB_DOC_SYNC_LABEL = "gitlab_doc_sync"


def gitlab_doc_sync(
    cc_pair: ConnectorCredentialPair,
    fetch_all_existing_docs_fn: FetchAllDocumentsFunction,
    fetch_all_existing_docs_ids_fn: FetchAllDocumentsIdsFunction,  # noqa: ARG001
    callback: IndexingHeartbeatInterface | None = None,
) -> Generator[DocExternalAccess, None, None]:
    """
    Sync gitlab documents with external access permissions.

    This function checks each repository for visibility/team changes and updates
    document permissions accordingly without using checkpoints.
    """
    logger.info(f"Starting GitLab document sync for CC pair ID: {cc_pair.id}")

    # Initialize gitlab connector with credentials
    gitlab_connector: GitlabConnector = GitlabConnector(**cc_pair.connector.connector_specific_config)

    credential_json = cc_pair.credential.credential_json.get_value(apply_mask=False) if cc_pair.credential.credential_json else {}
    gitlab_connector.load_credentials(credential_json)
    logger.info("GitLab connector credentials loaded successfully")

    if not gitlab_connector.gitlab_client:
        logger.error("GitLab client initialization failed")
        raise ValueError("gitlab_client is required")

    # Get all repositories from gitlab API
    logger.info("Fetching all repositories from GitLab API")
    try:
        projects = gitlab_connector._fetch_configured_projects()
        logger.info(f"Found {len(projects)} projects to check")
    except Exception as e:
        logger.error(f"Failed to fetch repositories: {e}")
        raise

    project_to_doc_list_map: dict[str, list[DocumentRow]] = {}
    # sort order is ascending because we want to get the oldest documents first
    existing_docs: list[DocumentRow] = fetch_all_existing_docs_fn(sort_order=SortOrder.ASC)
    logger.info(f"Found {len(existing_docs)} documents to check")
    for doc in existing_docs:
        try:
            doc_metadata = DocMetadata.model_validate_json(json.dumps(doc.doc_metadata))
            if doc_metadata.repo not in project_to_doc_list_map:
                project_to_doc_list_map[doc_metadata.repo] = []
            project_to_doc_list_map[doc_metadata.repo].append(doc)
        except Exception as e:
            logger.error(f"Failed to parse doc metadata: {e} for doc {doc.id}")
            continue
    logger.info(f"Found {len(project_to_doc_list_map)} unique repositories in existing documents")
    # Process each repository individually
    for project in projects:
        try:
            logger.info(f"Processing repository: {project.id} (name: {project.path_with_namespace})")
            project_doc_list: list[DocumentRow] = project_to_doc_list_map.get(project.path_with_namespace, [])
            if not project_doc_list:
                logger.warning(f"No documents found for repository {project.id} ({project.path_with_namespace})")
                continue

            expected_external_access = get_external_access_permission(project, add_prefix=True)

            updates_yielded = 0

            for doc in project_doc_list:
                current_group_ids = set(doc.external_user_group_ids or [])
                expected_group_ids = expected_external_access.external_user_group_ids

                needs_update = current_group_ids != expected_group_ids or doc.is_public != expected_external_access.is_public

                if needs_update:
                    if callback:
                        callback.progress(GITLAB_DOC_SYNC_LABEL, 1)

                    yield DocExternalAccess(
                        doc_id=doc.id,
                        external_access=expected_external_access,
                    )
                    updates_yielded += 1
            if updates_yielded > 0:
                logger.info(f"Updated permissions for {updates_yielded} documents in {project.path_with_namespace}")

        except Exception as e:
            logger.error(f"Error processing repository {project.id} ({project.path_with_namespace}): {e}")

    logger.info(f"GitLab document sync completed for CC pair ID: {cc_pair.id}")
