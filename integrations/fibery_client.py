"""Fibery API client."""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from config.constants import FIBERY_API_PATH, FIBERY_INSTANCE_URL

logger = logging.getLogger(__name__)


@dataclass
class FiberyEntity:
    """Parsed Fibery entity reference."""
    space: str
    database: str
    entity_name: str
    internal_id: str
    uuid: Optional[str] = None


@dataclass
class EntityContext:
    """Structured context extracted from a linked Fibery meeting entity."""
    entity_name: str = ""
    entity_type: str = ""  # "External Meeting", "Market Interview", "Internal Meeting"
    assignee_names: list[str] = field(default_factory=list)
    people_names: list[str] = field(default_factory=list)
    people_with_orgs: list[dict] = field(default_factory=list)  # [{"name": "John", "org": "Acme"}]
    organization_names: list[str] = field(default_factory=list)
    operator_names: list[str] = field(default_factory=list)


class FiberyClient:
    """Client for Fibery API operations."""

    _TIMEOUT = 30  # seconds for all HTTP requests

    # UUIDs for Market/Urgency single-select options
    _URGENCY_MAP = {
        "Hair on fire": "cf93c940-e006-11f0-af4c-1b6308c0e0d9",
        "High": "a80301b0-e020-11f0-be74-55115a5d9ece",
        "Medium": "d55f3ee0-e006-11f0-af4c-1b6308c0e0d9",
        "Low": "e3d57c51-e006-11f0-af4c-1b6308c0e0d9",
    }
    # UUIDs for Market/Frequency single-select options
    _FREQUENCY_MAP = {
        "Daily": "a12afce0-e006-11f0-af4c-1b6308c0e0d9",
        "Weekly": "a55dd120-e006-11f0-af4c-1b6308c0e0d9",
        "Monthly": "a6fba2a1-e006-11f0-af4c-1b6308c0e0d9",
        "Quarterly": "a9be6c21-e006-11f0-af4c-1b6308c0e0d9",
        "Yearly": "abc4c371-e006-11f0-af4c-1b6308c0e0d9",
        "Once": "ad14c311-e006-11f0-af4c-1b6308c0e0d9",
    }
    # Workflow state UUIDs for Market/Problem and Market Interview
    _AI_SUGGESTION_STATE_ID = "cacb3460-2dc8-11f1-8119-a147c72e932c"
    _EXTRACT_PROBLEMS_STATE_ID = "91abf940-2dea-11f1-a4bf-bb661e9f5363"

    def __init__(self, api_token: str, instance_url: Optional[str] = None):
        resolved_instance_url = (instance_url or FIBERY_INSTANCE_URL).strip()
        self._base_url = resolved_instance_url.rstrip("/")
        self._api_url = self._base_url + FIBERY_API_PATH
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
        })
        # Retry on 429/503 with exponential backoff (respects Retry-After header).
        # backoff_factor=2 → sleeps: 2s, 4s, 8s between attempts.
        _retry = Retry(
            total=4,
            backoff_factor=2,
            status_forcelist=[429, 503],
            allowed_methods=["POST", "GET"],
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        _adapter = HTTPAdapter(max_retries=_retry)
        self._session.mount("https://", _adapter)
        self._session.mount("http://", _adapter)

    def extract_url_candidates(self, url: str) -> list:
        """Extract candidate entity URLs from a potentially compound Fibery URL.

        When viewing an entity alongside a list in split-view, Fibery generates
        compound URLs like:
          https://instance.fibery.io/Space/View-123#Space/Type/entity-slug-456

        Returns a list of 1 or 2 simple entity URL strings. If the URL has no
        fragment, returns [url] unchanged.
        """
        url = url.strip()
        if "#" not in url:
            return [url]

        main_part, fragment = url.split("#", 1)
        main_part = main_part.rstrip("/")
        fragment = fragment.strip("/")

        candidates = []

        # Main path is an entity if it has 3 segments beyond the domain
        # e.g. https://x.fibery.io/Space/Database/slug-NNN → 6 split parts
        main_segments = main_part.split("/")
        if len(main_segments) >= 6 and re.search(r"-\d+$", main_segments[-1]):
            candidates.append(main_part)

        # Fragment is an entity if it looks like Space/Type/slug-NNN
        frag_parts = fragment.split("/")
        if len(frag_parts) >= 3 and re.search(r"-\d+$", frag_parts[-1]):
            candidates.append(f"{self._base_url}/{fragment}")

        return candidates if candidates else [url]

    def parse_url(self, url: str) -> FiberyEntity:
        """Parse a Fibery entity URL into its components.

        Replicates the N8N workflow's 'Fibery Database and Entity' code node.
        URL format: https://instance.fibery.io/{Space}/{Database}/{slug-{InternalID}}
        """
        parts = url.strip().rstrip("/").split("/")
        if len(parts) < 6:
            raise ValueError(
                f"Invalid Fibery URL format. Expected at least 6 segments, got {len(parts)}. "
                f"URL should look like: https://instance.fibery.io/Space/Database/entity-name-123"
            )

        # Validate domain matches the configured instance
        url_base = "/".join(parts[:3])  # "https://instance.fibery.io"
        if url_base.rstrip("/") != self._base_url.rstrip("/"):
            raise ValueError(
                f"URL domain '{url_base}' does not match the configured Fibery instance '{self._base_url}'"
            )

        raw_space = parts[3]
        raw_db = parts[4]
        raw_slug = parts[-1]

        # Database: underscores to spaces
        database = raw_db.replace("_", " ")

        # Extract InternalId: digits at the end after last hyphen
        id_match = re.search(r"-(\d+)$", raw_slug)
        if not id_match:
            raise ValueError(
                f"Could not extract entity ID from URL slug '{raw_slug}'. "
                f"Expected a numeric ID at the end (e.g., entity-name-123)"
            )
        internal_id = None
        if id_match:
            internal_id = id_match.group(1)
            raw_slug = raw_slug[: id_match.start()]

        # Clean entity name: triple dashes → separator, single dashes → spaces
        placeholder = "___TRIPLE_DASH___"
        raw_slug = raw_slug.replace("---", placeholder)
        raw_slug = raw_slug.replace("-", " ")

        if re.match(r"^\d+___TRIPLE_DASH___", raw_slug):
            raw_slug = raw_slug.replace(placeholder, " ")
        else:
            raw_slug = raw_slug.replace(placeholder, " - ")

        entity_name = unquote(raw_slug).strip()

        return FiberyEntity(
            space=raw_space,
            database=database,
            entity_name=entity_name,
            internal_id=internal_id,
        )

    def get_entity_uuid(self, entity: FiberyEntity) -> str:
        """Query Fibery to resolve an entity's UUID from its public ID."""
        payload = [
            {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": f"{entity.space}/{entity.database}",
                        "q/select": ["fibery/id", "fibery/public-id"],
                        "q/where": ["=", ["fibery/public-id"], "$id_to_find"],
                        "q/limit": 1,
                    },
                    "params": {
                        "$id_to_find": entity.internal_id,
                    },
                },
            }
        ]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        result = resp.json()

        try:
            uuid = result[0]["result"][0]["fibery/id"]
        except (IndexError, KeyError) as e:
            raise ValueError(
                f"Entity not found in Fibery: {entity.space}/{entity.database} "
                f"with public-id '{entity.internal_id}'"
            ) from e

        entity.uuid = uuid
        logger.info("Resolved entity UUID: %s", uuid)
        return uuid

    def get_entity_notes(self, entity: FiberyEntity) -> str:
        """Fetch the Notes rich-text content from a Fibery entity."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        notes_field = f"{entity.space}/Notes"

        # Notes is a Collaboration (rich-text) field, so we need to:
        # 1. Get the document secret
        # 2. Fetch the document content via the documents API
        secrets = self._get_document_secrets(entity, [notes_field])
        secret = secrets.get(notes_field)
        if not secret:
            logger.info("No document secret for Notes field, returning empty")
            return ""

        return self._get_document_content(secret)

    def get_entity_transcript(self, entity: FiberyEntity) -> str:
        """Fetch the Transcript rich-text content from a Fibery entity."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        transcript_field = f"{entity.space}/Transcript"
        secrets = self._get_document_secrets(entity, [transcript_field])
        secret = secrets.get(transcript_field)
        if not secret:
            logger.info("No document secret for Transcript field, returning empty")
            return ""

        return self._get_document_content(secret)

    def get_entity_name(self, entity: FiberyEntity) -> str:
        """Fetch the display name of a Fibery entity.

        Queries for the entity's name field. Falls back to URL-parsed name.
        """
        if not entity.uuid:
            self.get_entity_uuid(entity)

        name_field = f"{entity.space}/name"
        payload = [
            {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": f"{entity.space}/{entity.database}",
                        "q/select": [name_field],
                        "q/where": ["=", ["fibery/id"], "$uuid"],
                        "q/limit": 1,
                    },
                    "params": {
                        "$uuid": entity.uuid,
                    },
                },
            }
        ]
        try:
            resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            name = result[0]["result"][0].get(name_field, "")
            if name:
                return name
        except Exception:
            pass

        return entity.entity_name

    def get_entity_context(self, entity: FiberyEntity) -> EntityContext:
        """Fetch meeting context (participants, organizations) from a Fibery entity.

        Returns an EntityContext with all available names for use in transcription
        word boost and summarization context. Returns empty EntityContext on failure.
        """
        if not entity.uuid:
            self.get_entity_uuid(entity)

        ctx = EntityContext(
            entity_name=entity.entity_name,
            entity_type=entity.database,
        )

        try:
            db_type = f"{entity.space}/{entity.database}"
            name_field = f"{entity.space}/{'name' if entity.space == 'Network' else 'Name'}"
            rows = None
            select = None
            resp = None
            last_exc = None
            select_variants = self._build_entity_context_select_variants(entity, name_field)

            for index, candidate_select in enumerate(select_variants, start=1):
                select = candidate_select
                payload = [{
                    "command": "fibery.entity/query",
                    "args": {
                        "query": {
                            "q/from": db_type,
                            "q/select": select,
                            "q/where": ["=", ["fibery/id"], "$uuid"],
                            "q/limit": 1,
                        },
                        "params": {"$uuid": entity.uuid},
                    },
                }]

                try:
                    resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
                    resp.raise_for_status()
                    result = resp.json()
                    rows = self._extract_query_rows(result, context="entity context")
                    break
                except Exception as exc:
                    last_exc = exc
                    if index < len(select_variants):
                        logger.info(
                            "Entity context query variant %d/%d failed for %s/%s, retrying with reduced select: %s",
                            index,
                            len(select_variants),
                            entity.space,
                            entity.database,
                            exc,
                        )
                        continue
                    raise

            if not rows:
                raise last_exc or LookupError("entity context query returned no rows")
            row = rows[0]
            ctx.entity_name = row.get(name_field, entity.entity_name) or entity.entity_name

            # Parse assignees
            assignees_key = "Market/Assignees" if entity.database == "Market Interview" else "assignments/assignees"
            for a in self._as_relation_rows(row.get(assignees_key)):
                name = a.get("user/name", "")
                if name:
                    ctx.assignee_names.append(name)

            # Parse people (External Meeting + Market Interview)
            people_key = None
            if entity.database == "External Meeting":
                people_key = "Network/People"
            elif entity.database == "Market Interview":
                people_key = "Market/People"

            people_rows = self._as_relation_rows(row.get(people_key)) if people_key else []
            for p in people_rows:
                name = p.get("Network/name", "")
                if not name:
                    continue
                ctx.people_names.append(name)
                org_data = p.get("Network/Organizations") or {}
                org_name = ""
                if isinstance(org_data, list):
                    org_names = [
                        item.get("Network/Name", "")
                        for item in org_data
                        if isinstance(item, dict) and item.get("Network/Name", "")
                    ]
                    org_name = ", ".join(org_names)
                elif isinstance(org_data, dict):
                    org_name = org_data.get("Network/Name", "")
                ctx.people_with_orgs.append({"name": name, "org": org_name})

            # Parse organizations
            if entity.database == "External Meeting":
                for o in self._as_relation_rows(row.get("Network/Organizations")):
                    name = o.get("Network/Name", "")
                    if name:
                        ctx.organization_names.append(name)
            elif entity.database == "Market Interview":
                for o in self._as_relation_rows(row.get("Market/Organizations")):
                    name = o.get("Network/Name", "")
                    if name:
                        ctx.organization_names.append(name)
                if not ctx.organization_names:
                    org_data = row.get("Market/Organization") or {}
                    org_name = org_data.get("Network/Name", "") if isinstance(org_data, dict) else ""
                    if org_name:
                        ctx.organization_names.append(org_name)

            # Parse operators (External Meeting only)
            if entity.database == "External Meeting":
                for o in self._as_relation_rows(row.get("Network/Operators")):
                    name = o.get("Market/Name", "")
                    if name:
                        ctx.operator_names.append(name)

            logger.info(
                "Entity context: %d assignees, %d people, %d orgs, %d operators",
                len(ctx.assignee_names), len(ctx.people_names),
                len(ctx.organization_names), len(ctx.operator_names),
            )

        except Exception as e:
            response_excerpt = ""
            try:
                response_excerpt = resp.text[:500]
            except Exception:
                response_excerpt = ""
            logger.warning(
                "Failed to fetch entity context for %s/%s (%s): %s | response=%s | select=%s",
                entity.space,
                entity.database,
                entity.uuid or entity.internal_id,
                e,
                response_excerpt or "<unavailable>",
                select if "select" in locals() else "<unavailable>",
            )

        return ctx

    @classmethod
    def _build_entity_context_select_variants(cls, entity: FiberyEntity, name_field: str) -> list[dict]:
        """Build q/select variants for entity-context enrichment.

        Fibery collection relations require an object form with nested q/select
        and q/from blocks; bare list syntax triggers
        query-select-collection-field-vector-shape-invalid.
        """
        base_select: dict = {
            name_field: [name_field],
        }

        if entity.database in ("External Meeting", "Internal Meeting"):
            base_select["assignments/assignees"] = cls._nested_context_query(
                "assignments/assignees",
                {
                    "user/name": ["user/name"],
                },
            )
        elif entity.database == "Market Interview":
            base_select["Market/Assignees"] = cls._nested_context_query(
                "Market/Assignees",
                {
                    "user/name": ["user/name"],
                },
            )

        if entity.database == "External Meeting":
            base_select["Network/People"] = cls._nested_context_query(
                "Network/People",
                {
                    "fibery/id": ["fibery/id"],
                    "Network/name": ["Network/name"],
                },
            )
            base_select["Network/Organizations"] = cls._nested_context_query(
                "Network/Organizations",
                {
                    "Network/Name": ["Network/Name"],
                },
            )
            base_select["Network/Operators"] = cls._nested_context_query(
                "Network/Operators",
                {
                    "Market/Name": ["Market/Name"],
                },
            )
            return [base_select]
        elif entity.database == "Market Interview":
            base_select["Market/People"] = cls._nested_context_query(
                "Market/People",
                {
                    "fibery/id": ["fibery/id"],
                    "Network/name": ["Network/name"],
                },
            )
            full_select = dict(base_select)
            full_select["Market/Organizations"] = cls._nested_context_query(
                "Market/Organizations",
                {
                    "Network/Name": ["Network/Name"],
                },
            )
            legacy_select = dict(base_select)
            legacy_select["Market/Organization"] = {
                "Network/Name": ["Market/Organization", "Network/Name"],
            }
            return [full_select, legacy_select, base_select]

        return [base_select]

    @staticmethod
    def _nested_context_query(path: str | list[str], select: dict) -> dict:
        """Return a nested Fibery relation query for q/select."""
        path_parts = [path] if isinstance(path, str) else list(path)
        return {
            "q/from": path_parts,
            "q/select": select,
            "q/limit": "q/no-limit",
        }

    @staticmethod
    def _as_relation_rows(value) -> list[dict]:
        """Normalize Fibery relation query results to a list of row dicts."""
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _extract_query_rows(result, *, context: str) -> list:
        """Return Fibery query rows or raise a helpful response-shape error."""
        if not isinstance(result, list) or not result:
            raise ValueError(f"{context} returned unexpected top-level response: {type(result).__name__}")

        first = result[0]
        if not isinstance(first, dict):
            raise ValueError(f"{context} returned a non-dict first command result")
        if first.get("success") is False:
            raise RuntimeError(first.get("result") or f"{context} query failed")

        rows = first.get("result")
        if isinstance(rows, list):
            return rows
        raise ValueError(f"{context} returned non-list rows: {type(rows).__name__}")

    def update_entity(
        self,
        entity: FiberyEntity,
        ai_summary: str,
        transcript: str,
    ) -> None:
        """Write AI Summary and Transcript back to a Fibery entity.

        Fibery rich-text (Collaboration) fields require a two-step update:
        1. Query the entity to get each field's document secret.
        2. POST the new HTML content to the documents API.
        """
        if not entity.uuid:
            self.get_entity_uuid(entity)

        summary_field = f"{entity.space}/AI Summary"
        transcript_field = f"{entity.space}/Transcript"

        # Step 1: retrieve document secrets for both collaboration fields
        secrets = self._get_document_secrets(entity, [summary_field, transcript_field])

        # Step 2: update each document
        updates = {
            summary_field: ai_summary,
            transcript_field: transcript,
        }
        for field_name, text in updates.items():
            secret = secrets.get(field_name)
            if not secret:
                logger.warning("No document secret found for field '%s', skipping", field_name)
                continue
            self._update_document(secret, self._text_to_html(text))
            logger.info("Updated field '%s' (secret=%s…)", field_name, secret[:8])

        logger.info("Fibery entity %s updated", entity.uuid)

    def update_transcript_only(self, entity: FiberyEntity, transcript: str, append: bool = False) -> None:
        """Write only the Transcript field to a Fibery entity.

        Args:
            append: If True, read existing content and append with separator.
        """
        if not entity.uuid:
            self.get_entity_uuid(entity)

        transcript_field = f"{entity.space}/Transcript"
        secrets = self._get_document_secrets(entity, [transcript_field])
        secret = secrets.get(transcript_field)
        if not secret:
            logger.warning("No document secret for Transcript field, skipping")
            return

        if append:
            transcript = self._append_content(secret, transcript)

        self._update_document(secret, self._text_to_html(transcript))
        logger.info("Updated Transcript field (secret=%s…, append=%s)", secret[:8], append)

    def update_summary_only(self, entity: FiberyEntity, ai_summary: str, append: bool = False) -> None:
        """Write only the AI Summary field to a Fibery entity.

        Args:
            append: If True, read existing content and append with separator.
        """
        if not entity.uuid:
            self.get_entity_uuid(entity)

        summary_field = f"{entity.space}/AI Summary"
        secrets = self._get_document_secrets(entity, [summary_field])
        secret = secrets.get(summary_field)
        if not secret:
            logger.warning("No document secret for AI Summary field, skipping")
            return

        if append:
            ai_summary = self._append_content(secret, ai_summary)

        self._update_document(secret, self._text_to_html(ai_summary))
        logger.info("Updated AI Summary field (secret=%s…, append=%s)", secret[:8], append)

    def _append_content(self, secret: str, new_text: str) -> str:
        """Read existing document content and append new text with a separator.

        Returns the combined text, or just new_text if the document is empty.
        """
        try:
            existing = self._get_document_content(secret)
            if existing and existing.strip():
                logger.info("Appending to existing content (%d chars)", len(existing))
                return f"{existing.strip()}\n\n---\n\n{new_text}"
        except Exception as e:
            logger.warning("Could not read existing content for append, replacing: %s", e)
        return new_text

    # --- Document helpers ---

    def _get_document_secrets(
        self, entity: FiberyEntity, fields: list
    ) -> dict:
        """Query Fibery for the Collaboration document secrets of the given fields.

        Returns a dict mapping field_name → secret string.
        """
        select = [
            {field: ["Collaboration~Documents/secret"]}
            for field in fields
        ]
        payload = [
            {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": f"{entity.space}/{entity.database}",
                        "q/select": select,
                        "q/where": ["=", ["fibery/id"], "$uuid"],
                        "q/limit": 1,
                    },
                    "params": {"$uuid": entity.uuid},
                },
            }
        ]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        result = resp.json()

        secrets = {}
        try:
            row = result[0]["result"][0]
            for field in fields:
                doc = row.get(field, {})
                secret = doc.get("Collaboration~Documents/secret") if isinstance(doc, dict) else None
                if secret:
                    secrets[field] = secret
        except (IndexError, KeyError) as exc:
            logger.warning("Could not extract document secrets: %s", exc)

        return secrets

    def _get_document_content(self, secret: str) -> str:
        """Read a Fibery collaboration document's content as markdown."""
        doc_url = self._base_url + f"/api/documents/{secret}"
        resp = self._session.get(doc_url, params={"format": "md"}, timeout=self._TIMEOUT)
        resp.raise_for_status()
        content = resp.json().get("content", "")
        logger.debug("Document content (%d chars): %s…", len(content), content[:100])
        return content

    def _update_document(self, secret: str, html_content: str) -> None:
        """Update a Fibery collaboration document via the documents API."""
        doc_url = self._base_url + "/api/documents/commands"
        payload = {
            "command": "create-or-update-documents",
            "args": [{"secret": secret, "content": html_content}],
        }
        resp = self._session.post(
            doc_url,
            json=payload,
            params={"format": "html"},
            timeout=self._TIMEOUT,
        )
        resp.raise_for_status()
        logger.debug("Document update response: %s", resp.text[:200])


    def create_entity(self, space: str, database: str, name: str, date: str) -> FiberyEntity:
        """Create a new entity in Fibery and return it with UUID and public-id populated.

        Args:
            space: Fibery space (e.g. "General").
            database: Full database type (e.g. "General/Internal Meeting").
            name: Entity display name.
            date: ISO date string (e.g. "2025-03-05").

        Returns:
            FiberyEntity with uuid and internal_id set.
        """
        import uuid as uuid_mod

        entity_uuid = str(uuid_mod.uuid4())
        name_field = f"{space}/{'Name' if space != 'Network' else 'name'}"
        date_field = f"{space}/Date"

        entity_data = {
            "fibery/id": entity_uuid,
            name_field: name,
            date_field: date,
        }

        # External Meeting requires a Label single-select field.
        # WARNING: This UUID is the Fibery ID for the "Other" label in the
        # Network/Label single-select. If this label is deleted or recreated
        # in Fibery, entity creation for External Meetings will fail.
        # To find the current UUID: query fibery.entity/query on
        # "enum/Network/Label" and look for the "Other" entry's fibery/id.
        if database == "Network/External Meeting":
            entity_data["Network/Label"] = {"fibery/id": "5f979430-f03f-11ef-a8e4-5b389a8d8232"}

        payload = [
            {
                "command": "fibery.entity/create",
                "args": {
                    "type": database,
                    "entity": entity_data,
                },
            }
        ]

        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        result = resp.json()

        if not result or not result[0].get("success"):
            error_msg = result[0].get("result", "Unknown error") if result else "Empty response"
            raise RuntimeError(f"Failed to create entity: {error_msg}")

        logger.info("Created entity %s in %s", entity_uuid, database)

        # Fetch public-id for URL construction
        query_payload = [
            {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": database,
                        "q/select": ["fibery/public-id", name_field],
                        "q/where": ["=", ["fibery/id"], "$uuid"],
                        "q/limit": 1,
                    },
                    "params": {"$uuid": entity_uuid},
                },
            }
        ]
        resp = self._session.post(self._api_url, json=query_payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        query_result = resp.json()

        public_id = query_result[0]["result"][0]["fibery/public-id"]
        display_name = query_result[0]["result"][0].get(name_field, name)

        db_name = database.split("/", 1)[1]
        entity = FiberyEntity(
            space=space,
            database=db_name,
            entity_name=display_name,
            internal_id=str(public_id),
            uuid=entity_uuid,
        )

        logger.info("Created entity: %s (public-id=%s)", display_name, public_id)
        return entity

    def get_entity_url(self, entity: FiberyEntity) -> str:
        """Construct a browser URL for a Fibery entity."""
        slug = entity.entity_name.replace(" ", "-")
        db_slug = entity.database.replace(" ", "_")
        return f"{self._base_url}/{entity.space}/{db_slug}/{slug}-{entity.internal_id}"

    # --- File upload ---

    _DATABASES_WITH_FILES = {"External Meeting", "Internal Meeting", "Market Interview"}
    _FILE_UPLOAD_TIMEOUT = 300  # seconds (large audio files)

    def entity_supports_files(self, entity: FiberyEntity) -> bool:
        """Check if an entity type has a Files/Files collection field."""
        return entity.database in self._DATABASES_WITH_FILES

    def upload_file(self, file_path) -> dict:
        """Upload a local file to Fibery storage.

        Returns dict with fibery/id, fibery/name, fibery/secret, etc.
        """
        from pathlib import Path
        import mimetypes
        import time

        file_path = Path(file_path)
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        upload_url = self._base_url + "/api/files"

        last_exc = None
        for attempt in range(2):
            if attempt > 0:
                time.sleep(2 ** attempt)
                logger.info("Retrying file upload (attempt %d)", attempt + 1)
            try:
                with open(file_path, "rb") as f:
                    resp = self._session.post(
                        upload_url,
                        files={"file": (file_path.name, f, content_type)},
                        headers={"Content-Type": None},
                        timeout=self._FILE_UPLOAD_TIMEOUT,
                    )
                resp.raise_for_status()
                result = resp.json()
                logger.info(
                    "Uploaded file %s (%.1f MB) -> id=%s",
                    file_path.name,
                    file_path.stat().st_size / 1e6,
                    result.get("fibery/id"),
                )
                return result
            except Exception as e:
                last_exc = e
                logger.warning("File upload attempt %d failed: %s", attempt + 1, e)

        raise last_exc

    def _get_file_fields(self, entity: FiberyEntity) -> list[str]:
        """Return candidate file collection fields for an entity type."""
        if entity.database == "Market Interview":
            # Market Interview files live in the Market namespace in some
            # workspaces. Keep the legacy Files/Files field as a fallback so
            # older schemas continue to work.
            return ["Market/Files", "Files/Files"]
        return ["Files/Files"]

    def attach_file_to_entity(self, entity: FiberyEntity, file_id: str) -> None:
        """Attach an uploaded file to a Fibery entity's Files collection."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        items = [{"fibery/id": file_id}]
        last_exc = None
        for field in self._get_file_fields(entity):
            try:
                self._add_collection_items(entity, field, items)
                logger.info("Attached file %s to entity %s via %s", file_id, entity.uuid, field)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Failed to attach file %s to entity %s via %s: %s",
                    file_id,
                    entity.uuid,
                    field,
                    exc,
                )

        raise last_exc

    # --- Recording lock ---

    def get_recording_lock(self, entity: FiberyEntity) -> Optional[str]:
        """Read the 'Recording By' text field from a Fibery entity.

        Returns the raw field value (e.g. 'John|2026-03-05T14:30:00') or empty string.
        """
        if not entity.uuid:
            self.get_entity_uuid(entity)

        lock_field = f"{entity.space}/Recording By"
        payload = [
            {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": f"{entity.space}/{entity.database}",
                        "q/select": [lock_field],
                        "q/where": ["=", ["fibery/id"], "$uuid"],
                        "q/limit": 1,
                    },
                    "params": {"$uuid": entity.uuid},
                },
            }
        ]
        try:
            resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            return result[0]["result"][0].get(lock_field, "") or ""
        except Exception:
            return ""

    def set_recording_lock(self, entity: FiberyEntity, lock_value: str) -> None:
        """Set the 'Recording By' text field on a Fibery entity."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        lock_field = f"{entity.space}/Recording By"
        payload = [
            {
                "command": "fibery.entity/update",
                "args": {
                    "type": f"{entity.space}/{entity.database}",
                    "entity": {
                        "fibery/id": entity.uuid,
                        lock_field: lock_value,
                    },
                },
            }
        ]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        logger.info("Recording lock set: %s", lock_value or "(cleared)")

    def clear_recording_lock(self, entity: FiberyEntity) -> None:
        """Clear the 'Recording By' text field on a Fibery entity."""
        self.set_recording_lock(entity, "")

    def get_entity_segments(self, entity: FiberyEntity) -> list[str]:
        """Query linked Market/Segments from a Market Interview entity.

        Returns list of segment name strings (empty list on failure or no segments).
        """
        if not entity.uuid:
            self.get_entity_uuid(entity)

        payload = [
            {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": f"{entity.space}/{entity.database}",
                        "q/select": {
                            "Market/Segments": {
                                "q/from": ["Market/Segments"],
                                "q/select": {"Market/Name": ["Market/Name"]},
                                "q/limit": "q/no-limit",
                            }
                        },
                        "q/where": ["=", ["fibery/id"], "$uuid"],
                        "q/limit": 1,
                    },
                    "params": {"$uuid": entity.uuid},
                },
            }
        ]
        try:
            resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            rows = result[0].get("result", [])
            if not rows:
                return []
            segments_raw = rows[0].get("Market/Segments", [])
            if isinstance(segments_raw, list):
                return [
                    s.get("Market/Name", "")
                    for s in segments_raw
                    if isinstance(s, dict) and s.get("Market/Name")
                ]
        except Exception as e:
            logger.warning("Failed to fetch segments for entity %s: %s", entity.uuid, e)
        return []

    def create_problem_entity(self, interview_entity: FiberyEntity, problem_data: dict) -> FiberyEntity:
        """Create a Market/Problem entity linked to a Market Interview.

        Sets all structured text fields, single-select fields by UUID, AI Confidence,
        workflow state, and interview relation. Writes evidence to Market/Other document.
        Returns FiberyEntity with uuid and internal_id populated.
        """
        import uuid as uuid_mod

        if not interview_entity.uuid:
            self.get_entity_uuid(interview_entity)

        entity_uuid = str(uuid_mod.uuid4())
        entity_data: dict = {
            "fibery/id": entity_uuid,
            "workflow/state": {"fibery/id": self._AI_SUGGESTION_STATE_ID},
            "Market/Interview": {"fibery/id": interview_entity.uuid},
        }

        # Text field mapping from problem_data keys to Fibery field names
        text_field_map = {
            "struggle_with": "Market/Struggle with",
            "when_they": "Market/When they",
            "in_order_to_achieve": "Market/In order to achieve",
            "based_on": "Market/Based on",
            "they_solve_this_now_by": "Market/They solve this now by",
            "the_downside_is": "Market/The downside is",
            "they_are_searching_by": "Market/They are searching by",
        }
        for key, field in text_field_map.items():
            value = (problem_data.get(key) or "").strip()
            if value:
                entity_data[field] = value

        # AI Confidence: stored as 0.0–1.0 ratio (Fibery Percent format)
        confidence = problem_data.get("confidence")
        if isinstance(confidence, (int, float)):
            entity_data["Market/AI Confidence"] = confidence / 100.0

        # Single-select fields require UUID references
        urgency = (problem_data.get("urgency") or "").strip()
        urgency_id = self._URGENCY_MAP.get(urgency)
        if urgency_id:
            entity_data["Market/Urgency"] = {"fibery/id": urgency_id}

        frequency = (problem_data.get("frequency") or "").strip()
        frequency_id = self._FREQUENCY_MAP.get(frequency)
        if frequency_id:
            entity_data["Market/Frequency"] = {"fibery/id": frequency_id}

        payload = [
            {
                "command": "fibery.entity/create",
                "args": {
                    "type": "Market/Problem",
                    "entity": entity_data,
                },
            }
        ]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        result = resp.json()

        if not result or not result[0].get("success"):
            error_msg = result[0].get("result", "Unknown error") if result else "Empty response"
            raise RuntimeError(f"Failed to create problem entity: {error_msg}")

        logger.info("Created Market/Problem entity %s", entity_uuid)

        # Fetch public-id for FiberyEntity construction
        query_payload = [
            {
                "command": "fibery.entity/query",
                "args": {
                    "query": {
                        "q/from": "Market/Problem",
                        "q/select": ["fibery/public-id"],
                        "q/where": ["=", ["fibery/id"], "$uuid"],
                        "q/limit": 1,
                    },
                    "params": {"$uuid": entity_uuid},
                },
            }
        ]
        resp = self._session.post(self._api_url, json=query_payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        public_id = str(resp.json()[0]["result"][0]["fibery/public-id"])

        problem_entity = FiberyEntity(
            space="Market",
            database="Problem",
            entity_name=(problem_data.get("struggle_with") or "")[:80],
            internal_id=public_id,
            uuid=entity_uuid,
        )

        # Write evidence to Market/Other (rich-text document field)
        evidence = (problem_data.get("evidence") or "").strip()
        if evidence:
            other_field = "Market/Other"
            secrets = self._get_document_secrets(problem_entity, [other_field])
            secret = secrets.get(other_field)
            if secret:
                self._update_document(secret, self._text_to_html(evidence))
                logger.info("Updated Market/Other with evidence for entity %s", entity_uuid)
            else:
                logger.warning("No document secret for Market/Other on problem entity %s", entity_uuid)

        return problem_entity

    def set_interview_state(self, entity: FiberyEntity, state_id: str) -> None:
        """Set workflow state on a Market Interview (or any) entity."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        payload = [
            {
                "command": "fibery.entity/update",
                "args": {
                    "type": f"{entity.space}/{entity.database}",
                    "entity": {
                        "fibery/id": entity.uuid,
                        "workflow/state": {"fibery/id": state_id},
                    },
                },
            }
        ]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        logger.info("Set state %s on entity %s", state_id, entity.uuid)

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    @staticmethod
    def _text_to_html(text: str) -> str:
        """Convert markdown-formatted text to HTML for Fibery rich-text fields.

        Handles bold (**text**), italic (*text*), headings (# / ##),
        bullet lists (* / -), numbered lists (1. 2. …), and plain paragraphs.
        Processes line-by-line so mixed content (e.g. bold heading followed by
        bullets in the same paragraph block) renders correctly.
        """
        import html as html_lib
        import re

        def _inline(s: str) -> str:
            """Apply inline markdown to already-HTML-escaped text."""
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
            return s

        lines = text.strip().splitlines()
        parts = []
        ul_items: list[str] = []
        ol_items: list[str] = []

        def _flush_ul():
            if ul_items:
                parts.append("<ul>" + "".join(f"<li>{item}</li>" for item in ul_items) + "</ul>")
                ul_items.clear()

        def _flush_ol():
            if ol_items:
                parts.append("<ol>" + "".join(f"<li>{item}</li>" for item in ol_items) + "</ol>")
                ol_items.clear()

        for line in lines:
            stripped = line.rstrip()

            # Blank line — flush any open list and emit paragraph break (ignored in HTML)
            if not stripped:
                _flush_ul()
                _flush_ol()
                continue

            # Headings
            if stripped.startswith("### "):
                _flush_ul(); _flush_ol()
                parts.append(f"<h3>{_inline(html_lib.escape(stripped[4:]))}</h3>")
                continue
            if stripped.startswith("## "):
                _flush_ul(); _flush_ol()
                parts.append(f"<h2>{_inline(html_lib.escape(stripped[3:]))}</h2>")
                continue
            if stripped.startswith("# "):
                _flush_ul(); _flush_ol()
                parts.append(f"<h1>{_inline(html_lib.escape(stripped[2:]))}</h1>")
                continue

            # Unordered bullet (- or *)
            bullet_m = re.match(r"^[*\-]\s+(.*)", stripped)
            if bullet_m:
                _flush_ol()
                ul_items.append(_inline(html_lib.escape(bullet_m.group(1))))
                continue

            # Numbered list
            num_m = re.match(r"^\d+\.\s+(.*)", stripped)
            if num_m:
                _flush_ul()
                ol_items.append(_inline(html_lib.escape(num_m.group(1))))
                continue

            # Plain line — flush open lists and emit as paragraph
            _flush_ul()
            _flush_ol()
            parts.append(f"<p>{_inline(html_lib.escape(stripped))}</p>")

        _flush_ul()
        _flush_ol()

        return "".join(parts) or "<p></p>"

    def _add_collection_items(self, entity: FiberyEntity, field: str, items: list[dict]) -> None:
        """Add collection relation items to an entity."""
        if not items:
            return
        if not entity.uuid:
            self.get_entity_uuid(entity)

        payload = [{
            "command": "fibery.entity/add-collection-items",
            "args": {
                "type": f"{entity.space}/{entity.database}",
                "field": field,
                "entity": {"fibery/id": entity.uuid},
                "items": items,
            },
        }]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()

    def _remove_collection_items(self, entity: FiberyEntity, field: str, items: list[dict]) -> None:
        """Remove collection relation items from an entity."""
        if not items:
            return
        if not entity.uuid:
            self.get_entity_uuid(entity)

        payload = [{
            "command": "fibery.entity/remove-collection-items",
            "args": {
                "type": f"{entity.space}/{entity.database}",
                "field": field,
                "entity": {"fibery/id": entity.uuid},
                "items": items,
            },
        }]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
