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
    """GitLab API操作の簡易的なリトライロジック"""
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
    GitLabプロジェクトの権限情報をOnyxのExternalAccess形式に変換します。
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


def get_project_members_emails(project: Project, gitlab_client: gitlab.Gitlab) -> list[str]:
    """
    プロジェクトにアクセス可能な全ユーザーのメールアドレスを取得。
    members_all.list(all=True) を使うことで、継承された権限もカバーする。
    """
    # read_api 権限が強ければ、ここで全メンバーのリストが取得可能
    members = project.members_all.list(get_all=True)
    emails: set[str] = set()

    for m in members:
        try:
            # 1. 直接的な email 属性（Admin/本人なら見える）
            # 2. public_email（ユーザーが公開設定にしている場合）
            user = gitlab_client.users.get(m.id)
            email = getattr(m, "email", None) or getattr(user, "public_email", None)

            if email:
                emails.add(email)
            else:
                # フォールバック: username が判明しているなら、
                # Onyx側のユーザー管理ルールに合わせて生成するか、警告を出す
                logger.warning(f"User {m.username} (ID: {m.id}) has no email visible. Sync might fail for this user.")
        except Exception as e:
            logger.error(f"Failed to fetch user {m.username} (ID: {m.id}): {e}")

    return list(emails)


def get_external_user_groups(project: Project, gitlab_client: gitlab.Gitlab) -> list[ExternalUserGroup]:
    """
    Onyxの Permission Sync で使用。
    'project_{id}_members' という Onyx 内のグループに誰が属するかを定義。
    """
    emails = _run_with_retry(
        lambda: get_project_members_emails(project, gitlab_client), f"fetching members for project {project.id}"
    )
    if not emails:
        return []

    # Onyxが認識できるグループIDの形式に変換
    group_name = f"project_{project.id}_members"

    return [ExternalUserGroup(id=group_name, user_emails=emails)]
