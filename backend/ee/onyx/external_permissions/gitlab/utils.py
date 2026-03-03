from collections.abc import Callable
from enum import Enum
from typing import List
from typing import Optional
from typing import Tuple
from typing import TypeVar

from gitlab import gitlab
from gitlab import RateLimitExceededException
from gitlab.gitlabException import gitlabException
from gitlab.NamedUser import NamedUser
from gitlab.Organization import Organization
from gitlab.PaginatedList import PaginatedList
from gitlab.Repository import Repository
from gitlab.Team import Team
from pydantic import BaseModel

from ee.onyx.db.external_perm import ExternalUserGroup
from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.gitlab.rate_limit_utils import sleep_after_rate_limit_exception
from onyx.utils.logger import setup_logger

logger = setup_logger()


class gitlabVisibility(Enum):
    """gitlab repository visibility options."""

    PUBLIC = "public"
    PRIVATE = "private"
    INTERNAL = "internal"


MAX_RETRY_COUNT = 3

T = TypeVar("T")

# Higher-order function to wrap gitlab operations with retry and exception handling


def _run_with_retry(
    operation: Callable[[], T],
    description: str,
    gitlab_client: gitlab,
    retry_count: int = 0,
) -> Optional[T]:
    """Execute a gitlab operation with retry on rate limit and exception handling."""
    logger.debug(f"Starting operation '{description}', attempt {retry_count + 1}")
    try:
        result = operation()
        logger.debug(f"Operation '{description}' completed successfully")
        return result
    except RateLimitExceededException:
        if retry_count < MAX_RETRY_COUNT:
            sleep_after_rate_limit_exception(gitlab_client)
            logger.warning(
                f"Rate limit exceeded while {description}. Retrying... "
                f"(attempt {retry_count + 1}/{MAX_RETRY_COUNT})"
            )
            return _run_with_retry(
                operation, description, gitlab_client, retry_count + 1
            )
        else:
            error_msg = f"Max retries exceeded for {description}"
            logger.exception(error_msg)
            raise RuntimeError(error_msg)
    except gitlabException as e:
        logger.warning(f"gitlab API error during {description}: {e}")
        return None
    except Exception as e:
        logger.exception(f"Unexpected error during {description}: {e}")
        return None


class UserInfo(BaseModel):
    """Represents a gitlab user with their basic information."""

    login: str
    name: Optional[str] = None
    email: Optional[str] = None


class TeamInfo(BaseModel):
    """Represents a gitlab team with its members."""

    name: str
    slug: str
    members: List[UserInfo]


def _fetch_organization_members(
    gitlab_client: gitlab, org_name: str, retry_count: int = 0  # noqa: ARG001
) -> List[UserInfo]:
    """Fetch all organization members including owners and regular members."""
    org_members: List[UserInfo] = []
    logger.info(f"Fetching organization members for {org_name}")

    org = _run_with_retry(
        lambda: gitlab_client.get_organization(org_name),
        f"get organization {org_name}",
        gitlab_client,
    )
    if not org:
        logger.error(f"Failed to fetch organization {org_name}")
        raise RuntimeError(f"Failed to fetch organization {org_name}")

    member_objs: PaginatedList[NamedUser] | list[NamedUser] = (
        _run_with_retry(
            lambda: org.get_members(filter_="all"),
            f"get members for organization {org_name}",
            gitlab_client,
        )
        or []
    )

    for member in member_objs:
        user_info = UserInfo(login=member.login, name=member.name, email=member.email)
        org_members.append(user_info)

    logger.info(f"Fetched {len(org_members)} members for organization {org_name}")
    return org_members


def _fetch_repository_teams_detailed(
    repo: Repository, gitlab_client: gitlab, retry_count: int = 0  # noqa: ARG001
) -> List[TeamInfo]:
    """Fetch teams with access to the repository and their members."""
    teams_data: List[TeamInfo] = []
    logger.info(f"Fetching teams for repository {repo.full_name}")

    team_objs: PaginatedList[Team] | list[Team] = (
        _run_with_retry(
            lambda: repo.get_teams(),
            f"get teams for repository {repo.full_name}",
            gitlab_client,
        )
        or []
    )

    for team in team_objs:
        logger.info(
            f"Processing team {team.name} (slug: {team.slug}) for repository {repo.full_name}"
        )

        members: PaginatedList[NamedUser] | list[NamedUser] = (
            _run_with_retry(
                lambda: team.get_members(),
                f"get members for team {team.name}",
                gitlab_client,
            )
            or []
        )

        team_members = []
        for m in members:
            user_info = UserInfo(login=m.login, name=m.name, email=m.email)
            team_members.append(user_info)

        team_info = TeamInfo(name=team.name, slug=team.slug, members=team_members)
        teams_data.append(team_info)
        logger.info(f"Team {team.name} has {len(team_members)} members")

    logger.info(f"Fetched {len(teams_data)} teams for repository {repo.full_name}")
    return teams_data


def fetch_repository_team_slugs(
    repo: Repository, gitlab_client: gitlab, retry_count: int = 0  # noqa: ARG001
) -> List[str]:
    """Fetch team slugs with access to the repository."""
    logger.info(f"Fetching team slugs for repository {repo.full_name}")
    teams_data: List[str] = []

    team_objs: PaginatedList[Team] | list[Team] = (
        _run_with_retry(
            lambda: repo.get_teams(),
            f"get teams for repository {repo.full_name}",
            gitlab_client,
        )
        or []
    )

    for team in team_objs:
        teams_data.append(team.slug)

    logger.info(f"Fetched {len(teams_data)} team slugs for repository {repo.full_name}")
    return teams_data


def _get_collaborators_and_outside_collaborators(
    gitlab_client: gitlab,
    repo: Repository,
) -> Tuple[List[UserInfo], List[UserInfo]]:
    """Fetch and categorize collaborators into regular and outside collaborators."""
    collaborators: List[UserInfo] = []
    outside_collaborators: List[UserInfo] = []
    logger.info(f"Fetching collaborators for repository {repo.full_name}")

    repo_collaborators: PaginatedList[NamedUser] | list[NamedUser] = (
        _run_with_retry(
            lambda: repo.get_collaborators(),
            f"get collaborators for repository {repo.full_name}",
            gitlab_client,
        )
        or []
    )

    for collaborator in repo_collaborators:
        is_outside = False

        # Check if collaborator is outside the organization
        if repo.organization:
            org: Organization | None = _run_with_retry(
                lambda: gitlab_client.get_organization(repo.organization.login),
                f"get organization {repo.organization.login}",
                gitlab_client,
            )

            if org is not None:
                org_obj = org
                membership = _run_with_retry(
                    lambda: org_obj.has_in_members(collaborator),
                    f"check membership for {collaborator.login} in org {org_obj.login}",
                    gitlab_client,
                )
                is_outside = membership is not None and not membership

        info = UserInfo(
            login=collaborator.login, name=collaborator.name, email=collaborator.email
        )
        if repo.organization and is_outside:
            outside_collaborators.append(info)
        else:
            collaborators.append(info)

    logger.info(
        f"Categorized {len(collaborators)} regular and {len(outside_collaborators)} outside collaborators for {repo.full_name}"
    )
    return collaborators, outside_collaborators


def form_collaborators_group_id(repository_id: int) -> str:
    """Generate group ID for repository collaborators."""
    if not repository_id:
        logger.exception("Repository ID is required to generate collaborators group ID")
        raise ValueError("Repository ID must be set to generate group ID.")
    group_id = f"{repository_id}_collaborators"
    return group_id


def form_organization_group_id(organization_id: int) -> str:
    """Generate group ID for organization using organization ID."""
    if not organization_id:
        logger.exception(
            "Organization ID is required to generate organization group ID"
        )
        raise ValueError("Organization ID must be set to generate group ID.")
    group_id = f"{organization_id}_organization"
    return group_id


def form_outside_collaborators_group_id(repository_id: int) -> str:
    """Generate group ID for outside collaborators."""
    if not repository_id:
        logger.exception(
            "Repository ID is required to generate outside collaborators group ID"
        )
        raise ValueError("Repository ID must be set to generate group ID.")
    group_id = f"{repository_id}_outside_collaborators"
    return group_id


def get_repository_visibility(repo: Repository) -> gitlabVisibility:
    """
    Get the visibility of a repository.
    Returns gitlabVisibility enum member.
    """
    if hasattr(repo, "visibility"):
        visibility = repo.visibility
        logger.info(
            f"Repository {repo.full_name} visibility from attribute: {visibility}"
        )
        try:
            return gitlabVisibility(visibility)
        except ValueError:
            logger.warning(
                f"Unknown visibility '{visibility}' for repo {repo.full_name}, defaulting to private"
            )
            return gitlabVisibility.PRIVATE

    logger.info(f"Repository {repo.full_name} is private")
    return gitlabVisibility.PRIVATE


def get_external_access_permission(
    repo: Repository, gitlab_client: gitlab, add_prefix: bool = False
) -> ExternalAccess:
    """
    Get the external access permission for a repository.
    Uses group-based permissions for efficiency and scalability.

    add_prefix: When this method is called during the initial permission sync via the connector,
                the group ID isn't prefixed with the source while inserting the document record.
                So in that case, set add_prefix to True, allowing the method itself to handle
                prefixing. However, when the same method is invoked from doc_sync, our system
                already adds the prefix to the group ID while processing the ExternalAccess object.
    """
    # We maintain collaborators, and outside collaborators as two separate groups
    # instead of adding individual user emails to ExternalAccess.external_user_emails for two reasons:
    # 1. Changes in repo collaborators (additions/removals) would require updating all documents.
    # 2. Repo permissions can change without updating the repo's updated_at timestamp,
    #    forcing full permission syncs for all documents every time, which is inefficient.

    repo_visibility = get_repository_visibility(repo)
    logger.info(
        f"Generating ExternalAccess for {repo.full_name}: visibility={repo_visibility.value}"
    )

    if repo_visibility == gitlabVisibility.PUBLIC:
        logger.info(
            f"Repository {repo.full_name} is public - allowing access to all users"
        )
        return ExternalAccess(
            external_user_emails=set(),
            external_user_group_ids=set(),
            is_public=True,
        )
    elif repo_visibility == gitlabVisibility.PRIVATE:
        logger.info(
            f"Repository {repo.full_name} is private - setting up restricted access"
        )

        collaborators_group_id = form_collaborators_group_id(repo.id)
        outside_collaborators_group_id = form_outside_collaborators_group_id(repo.id)
        if add_prefix:
            collaborators_group_id = build_ext_group_name_for_onyx(
                source=DocumentSource.gitlab,
                ext_group_name=collaborators_group_id,
            )
            outside_collaborators_group_id = build_ext_group_name_for_onyx(
                source=DocumentSource.gitlab,
                ext_group_name=outside_collaborators_group_id,
            )
        group_ids = {collaborators_group_id, outside_collaborators_group_id}

        team_slugs = fetch_repository_team_slugs(repo, gitlab_client)
        if add_prefix:
            team_slugs = [
                build_ext_group_name_for_onyx(
                    source=DocumentSource.gitlab,
                    ext_group_name=slug,
                )
                for slug in team_slugs
            ]
        group_ids.update(team_slugs)

        logger.info(f"ExternalAccess groups for {repo.full_name}: {group_ids}")
        return ExternalAccess(
            external_user_emails=set(),
            external_user_group_ids=group_ids,
            is_public=False,
        )
    else:
        # Internal repositories - accessible to organization members
        logger.info(
            f"Repository {repo.full_name} is internal - accessible to org members"
        )
        org_group_id = form_organization_group_id(repo.organization.id)
        if add_prefix:
            org_group_id = build_ext_group_name_for_onyx(
                source=DocumentSource.gitlab,
                ext_group_name=org_group_id,
            )
        group_ids = {org_group_id}
        logger.info(f"ExternalAccess groups for {repo.full_name}: {group_ids}")
        return ExternalAccess(
            external_user_emails=set(),
            external_user_group_ids=group_ids,
            is_public=False,
        )


def get_external_user_group(
    repo: Repository, gitlab_client: gitlab
) -> list[ExternalUserGroup]:
    """
    Get the external user group for a repository.
    Creates ExternalUserGroup objects with actual user emails for each permission group.
    """
    repo_visibility = get_repository_visibility(repo)
    logger.info(
        f"Generating ExternalUserGroups for {repo.full_name}: visibility={repo_visibility.value}"
    )

    if repo_visibility == gitlabVisibility.PRIVATE:
        logger.info(f"Processing private repository {repo.full_name}")

        collaborators, outside_collaborators = (
            _get_collaborators_and_outside_collaborators(gitlab_client, repo)
        )
        teams = _fetch_repository_teams_detailed(repo, gitlab_client)
        external_user_groups = []

        user_emails = set()
        for collab in collaborators:
            if collab.email:
                user_emails.add(collab.email)
            else:
                logger.error(f"Collaborator {collab.login} has no email")

        if user_emails:
            collaborators_group = ExternalUserGroup(
                id=form_collaborators_group_id(repo.id),
                user_emails=list(user_emails),
            )
            external_user_groups.append(collaborators_group)
            logger.info(f"Created collaborators group with {len(user_emails)} emails")

        # Create group for outside collaborators
        user_emails = set()
        for collab in outside_collaborators:
            if collab.email:
                user_emails.add(collab.email)
            else:
                logger.error(f"Outside collaborator {collab.login} has no email")

        if user_emails:
            outside_collaborators_group = ExternalUserGroup(
                id=form_outside_collaborators_group_id(repo.id),
                user_emails=list(user_emails),
            )
            external_user_groups.append(outside_collaborators_group)
            logger.info(
                f"Created outside collaborators group with {len(user_emails)} emails"
            )

        # Create groups for teams
        for team in teams:
            user_emails = set()
            for member in team.members:
                if member.email:
                    user_emails.add(member.email)
                else:
                    logger.error(f"Team member {member.login} has no email")

            if user_emails:
                team_group = ExternalUserGroup(
                    id=team.slug,
                    user_emails=list(user_emails),
                )
                external_user_groups.append(team_group)
                logger.info(
                    f"Created team group {team.name} with {len(user_emails)} emails"
                )

        logger.info(
            f"Created {len(external_user_groups)} ExternalUserGroups for private repository {repo.full_name}"
        )
        return external_user_groups

    if repo_visibility == gitlabVisibility.INTERNAL:
        logger.info(f"Processing internal repository {repo.full_name}")

        org_group_id = form_organization_group_id(repo.organization.id)
        org_members = _fetch_organization_members(
            gitlab_client, repo.organization.login
        )

        user_emails = set()
        for member in org_members:
            if member.email:
                user_emails.add(member.email)
            else:
                logger.error(f"Org member {member.login} has no email")

        org_group = ExternalUserGroup(
            id=org_group_id,
            user_emails=list(user_emails),
        )
        logger.info(
            f"Created organization group with {len(user_emails)} emails for internal repository {repo.full_name}"
        )
        return [org_group]

    logger.info(f"Repository {repo.full_name} is public - no user groups needed")
    return []
