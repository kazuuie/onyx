from collections.abc import Callable
from enum import Enum
from typing import Optional, TypeVar

from ee.onyx.db.external_perm import ExternalUserGroup
from gitlab import gitlab
from gitlab.v4.objects import Project
from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.utils.logger import setup_logger

logger = setup_logger()


class GitLabVisibility(Enum):
    """gitlab repository visibility options."""

    PUBLIC = "public"
    PRIVATE = "private"
    INTERNAL = "internal"


MAX_RETRY_COUNT = 3

T = TypeVar("T")


def _run_with_retry(
    operation: Callable[[], T],
    description: str,
    retry_count: int = 3,
) -> Optional[T]:
    """Simple retry logic for GitLab API operations."""
    for i in range(retry_count):
        try:
            return operation()
        except Exception as e:
            logger.error(f"Error during {description} (attempt {i + 1}): {e}")
            if i == retry_count - 1:
                return None
    return None


def get_project_visibility(project: Project) -> GitLabVisibility:
    visibility = project.visibility
    if visibility == "public":
        return GitLabVisibility.PUBLIC
    if visibility == "internal":
        return GitLabVisibility.INTERNAL
    return GitLabVisibility.PRIVATE


def get_external_access_permission(project: Project, add_prefix: bool = False) -> ExternalAccess:
    """
    Converts GitLab project permissions into Onyx's ExternalAccess format.
    """
    project_visibility = get_project_visibility(project)

    if project_visibility == GitLabVisibility.PUBLIC or project_visibility == GitLabVisibility.INTERNAL:
        return ExternalAccess(
            external_user_emails=set(),
            external_user_group_ids=set(),
            is_public=True,
        )

    project_group_id = f"project_{project.id}_members"
    if add_prefix:
        project_group_id = build_ext_group_name_for_onyx(source=DocumentSource.GITLAB, ext_group_name=project_group_id)

    return ExternalAccess(
        external_user_emails=set(),
        external_user_group_ids={project_group_id},
        is_public=False,
    )


def get_project_members_emails(project: Project, gitlab_client: gitlab.Gitlab, user_email_cache: dict[int, str]) -> list[str]:
    """
    Retrieves the email addresses of all users with access to the project.
    Uses members_all.list(get_all=True) to cover inherited permissions.
    """
    members = project.members_all.list(get_all=True)
    emails: set[str] = set()

    for m in members:
        try:
            # 1. Direct email attribute (visible to Admins/Users themselves)
            # 2. public_email (if the user has made it public)
            # Note: Fetching user details individually might cause an N+1 issue on large instances.
            # However, this is currently necessary to ensure we retrieve the primary email reliably
            # using the Admin token across self-hosted environments.

            if m.id in user_email_cache:
                email = user_email_cache[m.id]
                if email:
                    emails.add(email)
                continue

            email = getattr(m, "email", None)
            if not email:
                user = gitlab_client.users.get(m.id)
                email = getattr(user, "email", None) or getattr(user, "public_email", None)

            user_email_cache[m.id] = email or ""

            if email:
                emails.add(email)
            else:
                # Fallback warning if email is not visible.
                logger.warning(f"User {m.username} (ID: {m.id}) has no email visible. Sync might fail for this user.")
        except Exception as e:
            logger.error(f"Failed to fetch user {m.username} (ID: {m.id}): {e}")

    return list(emails)


def get_external_user_groups(
    project: Project, gitlab_client: gitlab.Gitlab, user_email_cache: dict[int, str]
) -> list[ExternalUserGroup]:
    """
    Used in Onyx's Permission Sync.
    Defines who belongs to the Onyx group named 'project_{id}_members'.
    """
    emails = _run_with_retry(
        lambda: get_project_members_emails(project, gitlab_client, user_email_cache), f"fetching members for project {project.id}"
    )
    if not emails:
        return []

    # Convert to the group ID format recognized by Onyx
    group_name = f"project_{project.id}_members"

    return [ExternalUserGroup(id=group_name, user_emails=emails)]
