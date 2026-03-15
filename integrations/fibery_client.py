"""Fibery API client."""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote

import requests
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

    def __init__(self, api_token: str, instance_url: Optional[str] = None):
        resolved_instance_url = (instance_url or FIBERY_INSTANCE_URL).strip()
        self._base_url = resolved_instance_url.rstrip("/")
        self._api_url = self._base_url + FIBERY_API_PATH
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
        })

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

            # Build select based on entity type
            select = [name_field]

            if entity.database in ("External Meeting", "Internal Meeting"):
                # Standard assignees field
                select.append({"assignments/assignees": {
                    "q/from": "assignments/assignees",
                    "q/select": ["user/name"],
                    "q/limit": 50,
                }})
            elif entity.database == "Market Interview":
                # Market Interview uses its own assignees field
                select.append({"Market/Assignees": {
                    "q/from": "Market/Assignees",
                    "q/select": ["user/name"],
                    "q/limit": 50,
                }})

            if entity.database == "External Meeting":
                select.append({"Network/People": {
                    "q/from": "Network/People",
                    "q/select": [
                        "Network/name",
                        {"Network/Organizations": ["Network/Name"]},
                    ],
                    "q/limit": 50,
                }})
                select.append({"Network/Organizations": {
                    "q/from": "Network/Organization",
                    "q/select": ["Network/Name"],
                    "q/limit": 50,
                }})
                select.append({"Network/Operators": {
                    "q/from": "Market/Operator",
                    "q/select": ["Market/Name"],
                    "q/limit": 50,
                }})

            elif entity.database == "Market Interview":
                select.append({"Market/People": {
                    "q/from": "Market/People",
                    "q/select": [
                        "Network/name",
                        {"Network/Organizations": ["Network/Name"]},
                    ],
                    "q/limit": 50,
                }})
                # Market/Organization is a single relation, not collection
                select.append({"Market/Organization": ["Network/Name"]})

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

            resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
            resp.raise_for_status()
            result = resp.json()

            row = result[0]["result"][0]
            ctx.entity_name = row.get(name_field, entity.entity_name) or entity.entity_name

            # Parse assignees
            assignees_key = "Market/Assignees" if entity.database == "Market Interview" else "assignments/assignees"
            for a in row.get(assignees_key, []) or []:
                name = a.get("user/name", "")
                if name:
                    ctx.assignee_names.append(name)

            # Parse people (External Meeting + Market Interview)
            people_key = "Network/People" if entity.database == "External Meeting" else "Market/People"
            for p in row.get(people_key, []) or []:
                name = p.get("Network/name", "")
                if not name:
                    continue
                ctx.people_names.append(name)
                org_data = p.get("Network/Organizations") or {}
                org_name = org_data.get("Network/Name", "") if isinstance(org_data, dict) else ""
                ctx.people_with_orgs.append({"name": name, "org": org_name})

            # Parse organizations
            if entity.database == "External Meeting":
                for o in row.get("Network/Organizations", []) or []:
                    name = o.get("Network/Name", "")
                    if name:
                        ctx.organization_names.append(name)
            elif entity.database == "Market Interview":
                org_data = row.get("Market/Organization") or {}
                org_name = org_data.get("Network/Name", "") if isinstance(org_data, dict) else ""
                if org_name:
                    ctx.organization_names.append(org_name)

            # Parse operators (External Meeting only)
            if entity.database == "External Meeting":
                for o in row.get("Network/Operators", []) or []:
                    name = o.get("Market/Name", "")
                    if name:
                        ctx.operator_names.append(name)

            logger.info(
                "Entity context: %d assignees, %d people, %d orgs, %d operators",
                len(ctx.assignee_names), len(ctx.people_names),
                len(ctx.organization_names), len(ctx.operator_names),
            )

        except Exception as e:
            logger.warning("Failed to fetch entity context: %s", e)

        return ctx

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

    def update_transcript_only(self, entity: FiberyEntity, transcript: str) -> None:
        """Write only the Transcript field to a Fibery entity."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        transcript_field = f"{entity.space}/Transcript"
        secrets = self._get_document_secrets(entity, [transcript_field])
        secret = secrets.get(transcript_field)
        if not secret:
            logger.warning("No document secret for Transcript field, skipping")
            return
        self._update_document(secret, self._text_to_html(transcript))
        logger.info("Updated Transcript field (secret=%s…)", secret[:8])

    def update_summary_only(self, entity: FiberyEntity, ai_summary: str) -> None:
        """Write only the AI Summary field to a Fibery entity."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        summary_field = f"{entity.space}/AI Summary"
        secrets = self._get_document_secrets(entity, [summary_field])
        secret = secrets.get(summary_field)
        if not secret:
            logger.warning("No document secret for AI Summary field, skipping")
            return
        self._update_document(secret, self._text_to_html(ai_summary))
        logger.info("Updated AI Summary field (secret=%s…)", secret[:8])

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

    _DATABASES_WITH_FILES = {"External Meeting", "Internal Meeting"}
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

    def attach_file_to_entity(self, entity: FiberyEntity, file_id: str) -> None:
        """Attach an uploaded file to a Fibery entity's Files collection."""
        if not entity.uuid:
            self.get_entity_uuid(entity)

        payload = [{
            "command": "fibery.entity/add-collection-items",
            "args": {
                "type": f"{entity.space}/{entity.database}",
                "field": "Files/Files",
                "entity": {"fibery/id": entity.uuid},
                "items": [{"fibery/id": file_id}],
            },
        }]
        resp = self._session.post(self._api_url, json=payload, timeout=self._TIMEOUT)
        resp.raise_for_status()
        logger.info("Attached file %s to entity %s", file_id, entity.uuid)

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
