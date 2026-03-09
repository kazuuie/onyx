import json
from collections.abc import Generator

from ee.onyx.external_permissions.gitlab.utils import (
    fetch_repository_team_slugs,
    form_collaborators_group_id,
    form_organization_group_id,
    form_outside_collaborators_group_id,
    get_external_access_permission,
    get_repository_visibility,
    gitlabVisibility,
)
from ee.onyx.external_permissions.perm_sync_types import FetchAllDocumentsFunction, FetchAllDocumentsIdsFunction
from gitlab import gitlab
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
    """
    プロジェクトの可視性やメンバーシップに変更があるか確認します。
    """
    # 1. 可視性(Public/Internal/Private)のチェック
    current_visibility = get_project_visibility(project)

    # DB内の現在のグループIDから推測される可視性と比較するロジックをここに実装
    # (GitHub版の _is_repo_visibility_changed_from_groups を参考)

    # 2. メンバー/グループの変更チェック
    # GitLabのProject MembersやGroup MembersをAPIで取得して比較

    # 簡略化のため、常にTrue（または何らかのロジック）を返します
    return True


def _check_repository_for_changes(
    repo: Repository,
    gitlab_client: gitlab,
    current_external_group_ids: list[str],
) -> bool:
    """
    Check if repository has any permission changes (visibility or team updates).
    """
    logger.info(f"Checking repository {repo.id} ({repo.name}) for changes")

    # Check for repository visibility changes using the sample document data
    if _is_repo_visibility_changed_from_groups(
        repo=repo,
        current_external_group_ids=current_external_group_ids,
    ):
        logger.info(f"Repository {repo.id} ({repo.name}) has visibility changes")
        return True

    # Check for team membership changes if repository is private
    if get_repository_visibility(repo) == gitlabVisibility.PRIVATE and _teams_updated_from_groups(
        repo=repo,
        gitlab_client=gitlab_client,
        current_external_group_ids=current_external_group_ids,
    ):
        logger.info(f"Repository {repo.id} ({repo.name}) has team changes")
        return True

    logger.info(f"Repository {repo.id} ({repo.name}) has no changes")
    return False


def _is_repo_visibility_changed_from_groups(
    repo: Repository,
    current_external_group_ids: list[str],
) -> bool:
    """
    Check if repository visibility has changed by analyzing existing external group IDs.

    Args:
        repo: gitlab repository object
        current_external_group_ids: List of external group IDs from existing document

    Returns:
        True if visibility has changed
    """
    current_repo_visibility = get_repository_visibility(repo)
    logger.info(f"Current repository visibility: {current_repo_visibility.value}")

    # Build expected group IDs for current visibility
    collaborators_group_id = build_ext_group_name_for_onyx(
        source=DocumentSource.GITLAB,
        ext_group_name=form_collaborators_group_id(repo.id),
    )

    org_group_id = None
    if repo.organization:
        org_group_id = build_ext_group_name_for_onyx(
            source=DocumentSource.GITLAB,
            ext_group_name=form_organization_group_id(repo.organization.id),
        )

    # Determine existing visibility from group IDs
    has_collaborators_group = collaborators_group_id in current_external_group_ids
    has_org_group = org_group_id and org_group_id in current_external_group_ids

    if has_collaborators_group:
        existing_repo_visibility = gitlabVisibility.PRIVATE
    elif has_org_group:
        existing_repo_visibility = gitlabVisibility.INTERNAL
    else:
        existing_repo_visibility = gitlabVisibility.PUBLIC

    logger.info(f"Inferred existing visibility: {existing_repo_visibility.value}")

    visibility_changed = existing_repo_visibility != current_repo_visibility
    if visibility_changed:
        logger.info(
            f"Visibility changed for repo {repo.id} ({repo.name}): "
            f"{existing_repo_visibility.value} -> {current_repo_visibility.value}"
        )

    return visibility_changed


def _teams_updated_from_groups(
    repo: Repository,
    gitlab_client: gitlab,
    current_external_group_ids: list[str],
) -> bool:
    """
    Check if repository team memberships have changed using existing group IDs.
    """
    # Fetch current team slugs for the repository
    current_teams = fetch_repository_team_slugs(repo=repo, gitlab_client=gitlab_client)
    logger.info(f"Current teams for repository {repo.id} (name: {repo.name}): {current_teams}")

    # Build group IDs to exclude from team comparison (non-team groups)
    collaborators_group_id = build_ext_group_name_for_onyx(
        source=DocumentSource.GITLAB,
        ext_group_name=form_collaborators_group_id(repo.id),
    )
    outside_collaborators_group_id = build_ext_group_name_for_onyx(
        source=DocumentSource.GITLAB,
        ext_group_name=form_outside_collaborators_group_id(repo.id),
    )
    non_team_group_ids = {collaborators_group_id, outside_collaborators_group_id}

    # Extract existing team IDs from current external group IDs
    existing_team_ids = set()
    for group_id in current_external_group_ids:
        # Skip all non-team groups, keep only team groups
        if group_id not in non_team_group_ids:
            existing_team_ids.add(group_id)

    # Note: existing_team_ids from DB are already prefixed (e.g., "gitlab__team-slug")
    # but current_teams from API are raw team slugs, so we need to add the prefix
    current_team_ids = set()
    for team_slug in current_teams:
        team_group_id = build_ext_group_name_for_onyx(
            source=DocumentSource.GITLAB,
            ext_group_name=team_slug,
        )
        current_team_ids.add(team_group_id)

    logger.info(f"Existing team IDs: {existing_team_ids}, Current team IDs: {current_team_ids}")

    # Compare actual team IDs to detect changes
    teams_changed = current_team_ids != existing_team_ids
    if teams_changed:
        logger.info(
            f"Team changes detected for repo {repo.id} (name: {repo.name}): "
            f"existing={existing_team_ids}, current={current_team_ids}"
        )

    return teams_changed
