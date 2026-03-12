from typing import Any

from ee.onyx.external_permissions.gitlab.doc_sync import gitlab_doc_sync
from onyx.access.models import DocExternalAccess
from onyx.configs.constants import DocumentSource
from onyx.connectors.models import InputType
from onyx.db.enums import AccessType, ConnectorCredentialPairStatus
from onyx.db.models import Connector, ConnectorCredentialPair, Credential
from onyx.db.utils import DocumentRow, SortOrder
from onyx.utils.variable_functionality import global_version
from sqlalchemy.orm import Session


def test_gitlab_doc_sync(
    db_session: Session,
    gitlab_connector_config: dict[str, Any],
    gitlab_credential_json: dict[str, Any],
) -> None:
    # EE機能を有効化（権限同期をスキップさせないため）
    global_version.set_ee()

    try:
        # テスト用のプロジェクト設定（IDは環境に合わせて調整）
        connector = Connector(
            name="Test GitLab Doc Sync Connector",
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

        # Mock functions
        def fetch_all_existing_docs_fn(sort_order: SortOrder | None = None) -> list[DocumentRow]:
            return []

        def fetch_all_existing_docs_ids_fn() -> list[str]:
            return []

        doc_sync_iter = gitlab_doc_sync(
            cc_pair=cc_pair,
            fetch_all_existing_docs_fn=fetch_all_existing_docs_fn,
            fetch_all_existing_docs_ids_fn=fetch_all_existing_docs_ids_fn,
        )

        results = list(doc_sync_iter)
        assert len(results) > 0

        for doc in results:
            if not isinstance(doc, DocExternalAccess):
                continue

            # TODO: テスト環境のプロジェクトのVisibilityに応じてアサーションを調整
            # 例: プロジェクトID 123 が Private の場合
            if "project/123" in doc.doc_id:
                assert not doc.external_access.is_public
                assert "gitlab_project_123_members" in doc.external_access.external_user_group_ids

            # 例: Internalプロジェクトの場合（前回の決定通り、is_public=True）
            # if "project/456" in doc.doc_id:
            #     assert doc.external_access.is_public

    finally:
        db_session.rollback()
