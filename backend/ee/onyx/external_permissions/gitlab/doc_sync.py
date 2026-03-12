import json
from collections.abc import Generator

from ee.onyx.external_permissions.gitlab.utils import (
    GitLabVisibility,
    get_external_access_permission,
    get_project_visibility,
)
from ee.onyx.external_permissions.perm_sync_types import FetchAllDocumentsFunction, FetchAllDocumentsIdsFunction
from gitlab.v4.objects import Project
from onyx.access.models import DocExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.gitlab.connector import DocMetadata, GitlabConnector
from onyx.db.models import ConnectorCredentialPair
from onyx.db.utils import DocumentRow, SortOrder
from onyx.indexing.indexing_heartbeat import IndexingHeartbeatInterface
from onyx.utils.logger import setup_logger

logger = setup_logger()

gitlab_DOC_SYNC_LABEL = "gitlab_doc_sync"


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
    logger.info(f"Starting gitlab document sync for CC pair ID: {cc_pair.id}")

    # Initialize gitlab connector with credentials
    gitlab_connector: GitlabConnector = GitlabConnector(**cc_pair.connector.connector_specific_config)

    credential_json = cc_pair.credential.credential_json.get_value(apply_mask=False) if cc_pair.credential.credential_json else {}
    gitlab_connector.load_credentials(credential_json)
    logger.info("gitlab connector credentials loaded successfully")

    if not gitlab_connector.gitlab_client:
        logger.error("gitlab client initialization failed")
        raise ValueError("gitlab_client is required")

    # Get all repositories from gitlab API
    logger.info("Fetching all repositories from gitlab API")
    try:
        projects = gitlab_connector.fetch_configured_projects()
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
    logger.info(f"Found {len(project_to_doc_list_map)} documents to check")
    # Process each repository individually
    for project in projects:
        try:
            logger.info(f"Processing repository: {project.id} (name: {project.path_with_namespace})")
            project_doc_list: list[DocumentRow] = project_to_doc_list_map.get(project.path_with_namespace, [])
            if not project_doc_list:
                logger.warning(f"No documents found for repository {project.id} ({project.path_with_namespace})")
                continue

            current_external_group_ids = project_doc_list[0].external_user_group_ids or []
            # Check if repository has any permission changes
            has_changes = _check_project_for_changes(
                project=project,
                gitlab_client=gitlab_connector.gitlab_client,
                current_external_group_ids=current_external_group_ids,
            )

            if has_changes:
                logger.info(f"Repository {project.id} ({project.path_with_namespace}) has changes, updating documents")

                # Get new external access permissions for this repository
                new_external_access = get_external_access_permission(project, gitlab_connector.gitlab_client)

                logger.info(f"Found {len(project_doc_list)} documents for repository {project.path_with_namespace}")

                # Yield updated external access for each document
                for doc in project_doc_list:
                    if callback:
                        callback.progress(gitlab_DOC_SYNC_LABEL, 1)

                    yield DocExternalAccess(
                        doc_id=doc.id,
                        external_access=new_external_access,
                    )
            else:
                logger.info(f"Repository {project.id} ({project.path_with_namespace}) has no changes, skipping")
        except Exception as e:
            logger.error(f"Error processing repository {project.id} ({project.path_with_namespace}): {e}")

    logger.info(f"gitlab document sync completed for CC pair ID: {cc_pair.id}")


def _check_project_for_changes(
    project: Project,
    current_external_group_ids: list[str],
) -> bool:
    current_visibility = get_project_visibility(project)

    # 1. 可視性による推測
    is_public_currently = current_visibility == GitLabVisibility.PUBLIC
    was_public_previously = len(current_external_group_ids) == 0  # 簡略化した判定

    if is_public_currently != was_public_previously:
        return True

    if is_public_currently:
        return False  # 以前も今もPublicなら変更なし

    # 2. グループ構成の変更チェック
    # 現在設定されるべきグループIDをシミュレート
    expected_group_ids = set()
    proj_group_id = build_ext_group_name_for_onyx(DocumentSource.GITLAB, f"project_{project.id}_members")
    expected_group_ids.add(proj_group_id)

    if project.namespace["kind"] == "group":
        ns_group_id = build_ext_group_name_for_onyx(DocumentSource.GITLAB, f"group_{project.namespace['id']}")
        expected_group_ids.add(ns_group_id)

    # 既存のDBのグループIDセットと比較
    current_group_ids_set = set(current_external_group_ids)

    # 差分があれば更新が必要
    return expected_group_ids != current_group_ids_set
