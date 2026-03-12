from typing import Any

from ee.onyx.external_permissions.gitlab.group_sync import gitlab_group_sync
from onyx.configs.constants import DocumentSource
from onyx.connectors.models import InputType
from onyx.db.enums import AccessType, ConnectorCredentialPairStatus
from onyx.db.models import Connector, ConnectorCredentialPair, Credential
from shared_configs.contextvars import get_current_tenant_id
from sqlalchemy.orm import Session
from tests.daily.connectors.confluence.models import ExternalUserGroupSet


def test_gitlab_group_sync(
    db_session: Session,
    gitlab_connector_config: dict[str, Any],
    gitlab_credential_json: dict[str, Any],
) -> None:
    try:
        connector = Connector(
            name="Test GitLab Group Connector",
            source=DocumentSource.GITLAB,
            input_type=InputType.POLL,
            connector_specific_config=gitlab_connector_config,
        )
        db_session.add(connector)
        db_session.flush()

        credential = Credential(
            source=DocumentSource.GITLAB,
            credential_json=gitlab_credential_json,
        )
        db_session.add(credential)
        db_session.flush()

        cc_pair = ConnectorCredentialPair(
            connector_id=connector.id,
            credential_id=credential.id,
            status=ConnectorCredentialPairStatus.ACTIVE,
            access_type=AccessType.SYNC,
        )
        db_session.add(cc_pair)
        db_session.flush()

        tenant_id = get_current_tenant_id()
        group_sync_iter = gitlab_group_sync(
            tenant_id=tenant_id,
            cc_pair=cc_pair,
        )

        actual_groups = {group.id: ExternalUserGroupSet.from_model(external_user_group=group) for group in group_sync_iter}

        # アサーションの例
        assert len(actual_groups) > 0

        # 特定のプロジェクトグループが存在し、期待したメールアドレスが含まれているか確認
        # (テスト用GitLabインスタンスにある実際のデータに合わせる)
        expected_group_id = "project_123_members"
        if expected_group_id in actual_groups:
            group = actual_groups[expected_group_id]
            assert "expected_user@example.com" in group.user_emails

    finally:
        db_session.rollback()
