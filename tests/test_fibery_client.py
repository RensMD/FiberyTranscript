import logging
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


class _FakeResponse:
    def __init__(self, payload, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TestFiberyClientContext:
    def test_get_entity_context_uses_nested_query_objects_for_internal_meeting(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="General",
            database="Internal Meeting",
            entity_name="Weekly sync",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._session.post = MagicMock(return_value=_FakeResponse([
            {
                "success": True,
                "result": [{
                    "General/Name": "Weekly sync",
                    "assignments/assignees": [{"user/name": "Rens"}],
                }],
            }
        ]))

        try:
            context = client.get_entity_context(entity)
            select = client._session.post.call_args.kwargs["json"][0]["args"]["query"]["q/select"]
            assert select["General/Name"] == ["General/Name"]
            assert select["assignments/assignees"] == {
                "q/from": ["assignments/assignees"],
                "q/select": {
                    "user/name": ["user/name"],
                },
                "q/limit": "q/no-limit",
            }
            assert context.assignee_names == ["Rens"]
        finally:
            client.close()

    def test_get_entity_context_builds_external_meeting_nested_queries_and_parses_results(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="Network",
            database="External Meeting",
            entity_name="Customer sync",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._session.post = MagicMock(return_value=_FakeResponse([
            {
                "success": True,
                "result": [{
                    "Network/name": "Customer sync",
                    "assignments/assignees": [{"user/name": "Rens"}],
                    "Network/People": [
                        {
                            "Network/name": "Alice",
                        },
                        {
                            "Network/name": "Bob",
                        },
                    ],
                    "Network/Organizations": [{"Network/Name": "Acme"}],
                    "Network/Operators": [{"Market/Name": "Operator One"}],
                }],
            }
        ]))

        try:
            context = client.get_entity_context(entity)
            select = client._session.post.call_args.kwargs["json"][0]["args"]["query"]["q/select"]
            assert select["Network/People"] == {
                "q/from": ["Network/People"],
                "q/select": {
                    "fibery/id": ["fibery/id"],
                    "Network/name": ["Network/name"],
                },
                "q/limit": "q/no-limit",
            }
            assert select["Network/Organizations"] == {
                "q/from": ["Network/Organizations"],
                "q/select": {
                    "Network/Name": ["Network/Name"],
                },
                "q/limit": "q/no-limit",
            }
            assert select["Network/Operators"] == {
                "q/from": ["Network/Operators"],
                "q/select": {
                    "Market/Name": ["Market/Name"],
                },
                "q/limit": "q/no-limit",
            }
            assert context.assignee_names == ["Rens"]
            assert context.people_names == ["Alice", "Bob"]
            assert context.people_with_orgs == [
                {"name": "Alice", "org": ""},
                {"name": "Bob", "org": ""},
            ]
            assert context.organization_names == ["Acme"]
            assert context.operator_names == ["Operator One"]
        finally:
            client.close()

    def test_get_entity_context_builds_market_interview_queries_and_parses_results(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="Market",
            database="Market Interview",
            entity_name="Interview",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._session.post = MagicMock(return_value=_FakeResponse([
            {
                "success": True,
                "result": [{
                    "Market/Name": "Interview",
                    "Market/Assignees": [{"user/name": "Rens"}],
                    "Market/People": [{
                        "Network/name": "Andrej",
                    }],
                    "Market/Organizations": [{"Network/Name": "OpenAI"}],
                }],
            }
        ]))

        try:
            context = client.get_entity_context(entity)
            select = client._session.post.call_args.kwargs["json"][0]["args"]["query"]["q/select"]
            assert select["Market/Assignees"] == {
                "q/from": ["Market/Assignees"],
                "q/select": {
                    "user/name": ["user/name"],
                },
                "q/limit": "q/no-limit",
            }
            assert select["Market/People"] == {
                "q/from": ["Market/People"],
                "q/select": {
                    "fibery/id": ["fibery/id"],
                    "Network/name": ["Network/name"],
                },
                "q/limit": "q/no-limit",
            }
            assert select["Market/Organizations"] == {
                "q/from": ["Market/Organizations"],
                "q/select": {
                    "Network/Name": ["Network/Name"],
                },
                "q/limit": "q/no-limit",
            }
            assert context.assignee_names == ["Rens"]
            assert context.people_names == ["Andrej"]
            assert context.people_with_orgs == [{"name": "Andrej", "org": ""}]
            assert context.organization_names == ["OpenAI"]
        finally:
            client.close()

    def test_get_entity_context_retries_market_interview_with_legacy_singular_org_field(self, caplog):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="Market",
            database="Market Interview",
            entity_name="Interview",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._session.post = MagicMock(side_effect=[
            _FakeResponse(
                [{
                    "success": False,
                    "result": {
                        "message": '"Market/Organizations" field was not found in "Market/Market Interview" database.'
                    },
                }],
                text='[{"success":false,"result":{"message":"\\"Market/Organizations\\" field was not found"}}]',
            ),
            _FakeResponse([{
                "success": True,
                "result": [{
                    "Market/Name": "Interview",
                    "Market/Assignees": [{"user/name": "Rens"}],
                    "Market/People": [{"Network/name": "Andrej"}],
                    "Market/Organization": {"Network/Name": "OpenAI"},
                }],
            }]),
        ])

        try:
            with caplog.at_level(logging.INFO):
                context = client.get_entity_context(entity)

            assert client._session.post.call_count == 2
            first_select = client._session.post.call_args_list[0].kwargs["json"][0]["args"]["query"]["q/select"]
            second_select = client._session.post.call_args_list[1].kwargs["json"][0]["args"]["query"]["q/select"]
            assert "Market/Organizations" in first_select
            assert "Market/Organization" in second_select
            assert context.assignee_names == ["Rens"]
            assert context.people_names == ["Andrej"]
            assert context.organization_names == ["OpenAI"]
            assert "retrying with reduced select" in caplog.text
        finally:
            client.close()

    def test_get_entity_context_retries_market_interview_without_optional_org_fields(self, caplog):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="Market",
            database="Market Interview",
            entity_name="Interview",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._session.post = MagicMock(side_effect=[
            _FakeResponse(
                [{
                    "success": False,
                    "result": {
                        "message": '"Market/Organizations" field was not found in "Market/Market Interview" database.'
                    },
                }],
                text='[{"success":false,"result":{"message":"\\"Market/Organizations\\" field was not found"}}]',
            ),
            _FakeResponse(
                [{
                    "success": False,
                    "result": {
                        "message": '"Market/Organization" field was not found in "Market/Market Interview" database.'
                    },
                }],
                text='[{"success":false,"result":{"message":"\\"Market/Organization\\" field was not found"}}]',
            ),
            _FakeResponse([{
                "success": True,
                "result": [{
                    "Market/Name": "Interview",
                    "Market/Assignees": [{"user/name": "Rens"}],
                    "Market/People": [{"Network/name": "Andrej"}],
                }],
            }]),
        ])

        try:
            with caplog.at_level(logging.INFO):
                context = client.get_entity_context(entity)

            assert client._session.post.call_count == 3
            first_select = client._session.post.call_args_list[0].kwargs["json"][0]["args"]["query"]["q/select"]
            second_select = client._session.post.call_args_list[1].kwargs["json"][0]["args"]["query"]["q/select"]
            third_select = client._session.post.call_args_list[2].kwargs["json"][0]["args"]["query"]["q/select"]
            assert "Market/Organizations" in first_select
            assert "Market/Organization" in second_select
            assert "Market/Organizations" not in third_select
            assert "Market/Organization" not in third_select
            assert context.assignee_names == ["Rens"]
            assert context.people_names == ["Andrej"]
            assert context.organization_names == []
            assert caplog.text.count("retrying with reduced select") == 2
        finally:
            client.close()

    def test_get_entity_context_logs_response_details_on_query_failure(self, caplog):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="General",
            database="Internal Meeting",
            entity_name="Weekly sync",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._session.post = MagicMock(return_value=_FakeResponse(
            [{"success": False, "result": {"message": "Unknown field assignments/assignees"}}],
            text='[{"success":false,"result":{"message":"Unknown field assignments/assignees"}}]',
        ))

        try:
            with caplog.at_level(logging.WARNING):
                context = client.get_entity_context(entity)
            assert context.assignee_names == []
            assert "Failed to fetch entity context" in caplog.text
            assert "Unknown field assignments/assignees" in caplog.text
            assert "response=" in caplog.text
            assert "select=" in caplog.text
        finally:
            client.close()


class TestFiberyClientFileAttachment:
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
                "Market/Files",
                [{"fibery/id": "file-uuid"}],
            )
        finally:
            client.close()

    def test_attach_file_to_market_interview_falls_back_to_legacy_files_collection(self):
        client = FiberyClient(api_token="token", instance_url="https://example.fibery.io")
        entity = FiberyEntity(
            space="Market",
            database="Market Interview",
            entity_name="Interview",
            internal_id="123",
            uuid="entity-uuid",
        )
        client._add_collection_items = MagicMock(side_effect=[
            RuntimeError("Unknown field Market/Files"),
            None,
        ])

        try:
            client.attach_file_to_entity(entity, "file-uuid")
            assert client._add_collection_items.call_count == 2
            assert client._add_collection_items.call_args_list[0].args == (
                entity,
                "Market/Files",
                [{"fibery/id": "file-uuid"}],
            )
            assert client._add_collection_items.call_args_list[1].args == (
                entity,
                "Files/Files",
                [{"fibery/id": "file-uuid"}],
            )
        finally:
            client.close()
