from unittest.mock import MagicMock

from integrations.fibery_client import FiberyClient, FiberyEntity


class TestFiberyClientTranscript:
    def test_get_entity_transcript_returns_empty_when_document_missing(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="General",
            database="Internal Meeting",
            entity_name="Meeting",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._get_document_secrets = MagicMock(return_value={})

        try:
            assert client.get_entity_transcript(entity) == ""
        finally:
            client.close()

    def test_get_entity_transcript_returns_document_content(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="General",
            database="Internal Meeting",
            entity_name="Meeting",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._get_document_secrets = MagicMock(
            return_value={"General/Transcript": "secret-123"}
        )
        client._get_document_content = MagicMock(return_value="Transcript text")

        try:
            assert client.get_entity_transcript(entity) == "Transcript text"
        finally:
            client.close()


class TestFiberyClientFiles:
    def test_market_interview_supports_files(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="Market",
            database="Market Interview",
            entity_name="Interview",
            internal_id="123",
            uuid="entity-uuid",
        )

        try:
            assert client.entity_supports_files(entity) is True
        finally:
            client.close()

    def test_attach_file_to_market_interview_uses_files_collection(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="Market",
            database="Market Interview",
            entity_name="Interview",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._add_collection_items = MagicMock()

        try:
            client.attach_file_to_entity(entity, "file-uuid")
            client._add_collection_items.assert_called_once_with(
                entity,
                "Files/Files",
                [{"fibery/id": "file-uuid"}],
            )
        finally:
            client.close()
