from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
import libsql  # type: ignore[import-untyped]

from agentsenate.models import InitiativeAnalysis, SourceItem


class Storage(Protocol):
    def has_successful_run(self) -> bool: ...

    def start_run(self, started_at: datetime) -> str: ...

    def finish_run(self, run_id: str, status: str, details: dict[str, Any]) -> None: ...

    def store_items(self, items: list[SourceItem]) -> None: ...

    def pending_items(self) -> list[SourceItem]: ...

    def mark_baselined(self, external_ids: list[str], at: datetime) -> None: ...

    def mark_screened(self, analyses: list[InitiativeAnalysis], at: datetime) -> None: ...

    def mark_emailed(self, external_ids: list[str], at: datetime) -> None: ...

    def record_delivery(
        self, idempotency_key: str, external_ids: list[str], provider_id: str, sent_at: datetime
    ) -> None: ...


class TursoStorage:
    def __init__(self, database_url: str, auth_token: str) -> None:
        is_local = database_url == ":memory:" or database_url.startswith("file:")
        if not database_url or (not is_local and not auth_token):
            raise RuntimeError("Turso database URL and auth token are required")
        self.connection = (
            libsql.connect(database=database_url)
            if is_local
            else libsql.connect(database=database_url, auth_token=auth_token)
        )

    def has_successful_run(self) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM monitor_runs WHERE status = ? LIMIT 1", ("success",)
        ).fetchone()
        return row is not None

    def start_run(self, started_at: datetime) -> str:
        run_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO monitor_runs (id, started_at, status, details) VALUES (?, ?, ?, ?)",
            (run_id, started_at.isoformat(), "running", "{}"),
        )
        self.connection.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, details: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE monitor_runs SET status = ?, details = ?, finished_at = ? WHERE id = ?",
            (
                status,
                json.dumps(details, default=str),
                datetime.now(UTC).isoformat(),
                run_id,
            ),
        )
        self.connection.commit()

    def store_items(self, items: list[SourceItem]) -> None:
        if not items:
            return
        rows = []
        for item in items:
            payload = item.model_dump(mode="json")
            rows.append(
                (
                    item.source,
                    item.source_category.value,
                    item.external_id,
                    str(item.source_url),
                    item.observed_at.isoformat(),
                    item.published_at.isoformat() if item.published_at else None,
                    item.university_name,
                    item.title,
                    item.author,
                    item.raw_text,
                    item.document_id,
                    item.policy_status,
                    item.financial_cost,
                    item.funding_source,
                    json.dumps(payload["metadata"]),
                    item.content_hash,
                )
            )
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO source_items (
              source, source_category, external_id, source_url, observed_at, published_at,
              university_name, title, author, raw_text, document_id, policy_status,
              financial_cost, funding_source, metadata, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()

    def pending_items(self) -> list[SourceItem]:
        cursor = self.connection.execute(
            """
            SELECT source, source_category, external_id, source_url, observed_at, published_at,
                   university_name, title, author, raw_text, document_id, policy_status,
                   financial_cost, funding_source, metadata, content_hash
            FROM source_items
            WHERE screened_at IS NULL
            ORDER BY observed_at
            """
        )
        columns = [description[0] for description in cursor.description]
        items = []
        for row in cursor.fetchall():
            payload = dict(zip(columns, row, strict=True))
            payload["metadata"] = json.loads(payload["metadata"])
            items.append(SourceItem.model_validate(payload))
        return items

    def mark_baselined(self, external_ids: list[str], at: datetime) -> None:
        self._update_ids("screened_at = ?", at.isoformat(), external_ids)

    def mark_screened(self, analyses: list[InitiativeAnalysis], at: datetime) -> None:
        self.connection.executemany(
            "UPDATE source_items SET screened_at = ?, analysis = ? WHERE external_id = ?",
            [
                (at.isoformat(), analysis.model_dump_json(), analysis.external_id)
                for analysis in analyses
            ],
        )
        self.connection.commit()

    def mark_emailed(self, external_ids: list[str], at: datetime) -> None:
        self._update_ids("emailed_at = ?", at.isoformat(), external_ids)

    def record_delivery(
        self, idempotency_key: str, external_ids: list[str], provider_id: str, sent_at: datetime
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO digest_deliveries
              (idempotency_key, external_ids, provider_id, sent_at)
            VALUES (?, ?, ?, ?)
            """,
            (idempotency_key, json.dumps(external_ids), provider_id, sent_at.isoformat()),
        )
        self.connection.commit()

    def claim_inbound(
        self,
        email_id: str,
        sender: str,
        subject: str,
        message_id: str,
        received_at: datetime,
        request_text: str,
        attachments: list[dict[str, Any]],
    ) -> bool:
        row = self.connection.execute(
            """
            INSERT INTO inbound_messages (
              email_id, sender, subject, message_id, received_at, status,
              request_text, attachments
            ) VALUES (?, ?, ?, ?, ?, 'processing', ?, ?)
            ON CONFLICT(email_id) DO UPDATE SET
              status = 'processing',
              error = NULL,
              attempts = inbound_messages.attempts + 1,
              updated_at = CURRENT_TIMESTAMP
            WHERE inbound_messages.status = 'failed'
            RETURNING email_id
            """,
            (
                email_id,
                sender,
                subject,
                message_id,
                received_at.isoformat(),
                request_text,
                json.dumps(attachments),
            ),
        ).fetchone()
        self.connection.commit()
        return row is not None

    def complete_inbound(self, email_id: str, result: dict[str, Any], delivery_id: str) -> None:
        self.connection.execute(
            """
            UPDATE inbound_messages
            SET status = 'completed', result = ?, delivery_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE email_id = ?
            """,
            (json.dumps(result), delivery_id, email_id),
        )
        self.connection.commit()

    def fail_inbound(self, email_id: str, error: str) -> None:
        self.connection.execute(
            """
            UPDATE inbound_messages
            SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE email_id = ?
            """,
            (error[:2_000], email_id),
        )
        self.connection.commit()

    def recent_context(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            """
            SELECT university_name, title, source_url, analysis, observed_at
            FROM source_items
            WHERE analysis IS NOT NULL
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "university_name": row[0],
                "title": row[1],
                "source_url": row[2],
                "analysis": json.loads(row[3]),
                "observed_at": row[4],
            }
            for row in cursor.fetchall()
        ]

    def _update_ids(self, assignment: str, value: str, external_ids: list[str]) -> None:
        if not external_ids:
            return
        placeholders = ", ".join("?" for _ in external_ids)
        self.connection.execute(
            f"UPDATE source_items SET {assignment} WHERE external_id IN ({placeholders})",
            (value, *external_ids),
        )
        self.connection.commit()


class SupabaseRestStorage:
    def __init__(
        self,
        supabase_url: str,
        service_role_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not supabase_url or not service_role_key:
            raise RuntimeError("Supabase URL and service role key are required")
        self.base_url = supabase_url.rstrip("/") + "/rest/v1"
        self.client = client or httpx.Client(timeout=30)
        self.headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
        }

    def has_successful_run(self) -> bool:
        rows = self._request(
            "GET",
            "monitor_runs",
            params={"select": "id", "status": "eq.success", "limit": "1"},
        ).json()
        return bool(rows)

    def start_run(self, started_at: datetime) -> str:
        run_id = str(uuid4())
        self._request(
            "POST",
            "monitor_runs",
            json={
                "id": run_id,
                "started_at": started_at.isoformat(),
                "status": "running",
                "details": {},
            },
            prefer="return=minimal",
        )
        return run_id

    def finish_run(self, run_id: str, status: str, details: dict[str, Any]) -> None:
        self._request(
            "PATCH",
            "monitor_runs",
            params={"id": f"eq.{run_id}"},
            json={
                "status": status,
                "details": details,
                "finished_at": datetime.now(UTC).isoformat(),
            },
            prefer="return=minimal",
        )

    def store_items(self, items: list[SourceItem]) -> None:
        if not items:
            return
        rows = []
        for item in items:
            rows.append(
                {
                    "source": item.source,
                    "source_category": item.source_category.value,
                    "external_id": item.external_id,
                    "source_url": str(item.source_url),
                    "observed_at": item.observed_at.isoformat(),
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                    "university_name": item.university_name,
                    "title": item.title,
                    "author": item.author,
                    "raw_text": item.raw_text,
                    "document_id": item.document_id,
                    "policy_status": item.policy_status,
                    "financial_cost": item.financial_cost,
                    "funding_source": item.funding_source,
                    "metadata": item.metadata,
                    "content_hash": item.content_hash,
                }
            )
        self._request(
            "POST",
            "source_items",
            params={"on_conflict": "source,external_id"},
            json=rows,
            prefer="resolution=ignore-duplicates,return=minimal",
        )

    def pending_items(self) -> list[SourceItem]:
        rows = self._request(
            "GET",
            "source_items",
            params={
                "select": (
                    "source,source_category,external_id,source_url,observed_at,published_at,"
                    "university_name,title,author,raw_text,document_id,policy_status,"
                    "financial_cost,funding_source,metadata,content_hash"
                ),
                "screened_at": "is.null",
                "order": "observed_at.asc",
            },
        ).json()
        return [SourceItem.model_validate(row) for row in rows]

    def mark_baselined(self, external_ids: list[str], at: datetime) -> None:
        self._update_ids({"screened_at": at.isoformat()}, external_ids)

    def mark_screened(self, analyses: list[InitiativeAnalysis], at: datetime) -> None:
        for analysis in analyses:
            self._request(
                "PATCH",
                "source_items",
                params={"external_id": f"eq.{analysis.external_id}"},
                json={
                    "screened_at": at.isoformat(),
                    "analysis": analysis.model_dump(mode="json"),
                },
                prefer="return=minimal",
            )

    def mark_emailed(self, external_ids: list[str], at: datetime) -> None:
        self._update_ids({"emailed_at": at.isoformat()}, external_ids)

    def record_delivery(
        self, idempotency_key: str, external_ids: list[str], provider_id: str, sent_at: datetime
    ) -> None:
        self._request(
            "POST",
            "digest_deliveries",
            params={"on_conflict": "idempotency_key"},
            json={
                "idempotency_key": idempotency_key,
                "external_ids": external_ids,
                "provider_id": provider_id,
                "sent_at": sent_at.isoformat(),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def claim_inbound(
        self,
        email_id: str,
        sender: str,
        subject: str,
        message_id: str,
        received_at: datetime,
        request_text: str,
        attachments: list[dict[str, Any]],
    ) -> bool:
        response = self._request(
            "POST",
            "inbound_messages",
            params={"on_conflict": "email_id"},
            json={
                "email_id": email_id,
                "sender": sender,
                "subject": subject,
                "message_id": message_id,
                "received_at": received_at.isoformat(),
                "status": "processing",
                "request_text": request_text,
                "attachments": attachments,
            },
            prefer="resolution=ignore-duplicates,return=representation",
        )
        return bool(response.json())

    def complete_inbound(self, email_id: str, result: dict[str, Any], delivery_id: str) -> None:
        self._request(
            "PATCH",
            "inbound_messages",
            params={"email_id": f"eq.{email_id}"},
            json={
                "status": "completed",
                "result": result,
                "delivery_id": delivery_id,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            prefer="return=minimal",
        )

    def fail_inbound(self, email_id: str, error: str) -> None:
        self._request(
            "PATCH",
            "inbound_messages",
            params={"email_id": f"eq.{email_id}"},
            json={
                "status": "failed",
                "error": error[:2_000],
                "updated_at": datetime.now(UTC).isoformat(),
            },
            prefer="return=minimal",
        )

    def recent_context(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._request(
            "GET",
            "source_items",
            params={
                "select": "university_name,title,source_url,analysis,observed_at",
                "analysis": "not.is.null",
                "order": "observed_at.desc",
                "limit": str(limit),
            },
        ).json()
        if not isinstance(rows, list):
            raise RuntimeError("Supabase returned an unexpected recent context payload")
        return [row for row in rows if isinstance(row, dict)]

    def _update_ids(self, values: dict[str, Any], external_ids: list[str]) -> None:
        if not external_ids:
            return
        ids = ",".join(external_ids)
        self._request(
            "PATCH",
            "source_items",
            params={"external_id": f"in.({ids})"},
            json=values,
            prefer="return=minimal",
        )

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, str] | None = None,
        json: object | None = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        response = self.client.request(
            method,
            f"{self.base_url}/{table}",
            params=params,
            json=json,
            headers=headers,
        )
        response.raise_for_status()
        return response


class MemoryStorage:
    """Non-persistent storage used for dry runs and tests."""

    def __init__(self, previously_ran: bool = True) -> None:
        self.previously_ran = previously_ran
        self.items: dict[str, SourceItem] = {}
        self.screened: dict[str, InitiativeAnalysis] = {}
        self.emailed: set[str] = set()
        self.deliveries: dict[str, str] = {}

    def has_successful_run(self) -> bool:
        return self.previously_ran

    def start_run(self, started_at: datetime) -> str:
        del started_at
        return str(UUID(int=0))

    def finish_run(self, run_id: str, status: str, details: dict[str, Any]) -> None:
        del run_id, details
        if status == "success":
            self.previously_ran = True

    def store_items(self, items: list[SourceItem]) -> None:
        for item in items:
            self.items.setdefault(item.external_id, item)

    def pending_items(self) -> list[SourceItem]:
        return [item for key, item in self.items.items() if key not in self.screened]

    def mark_baselined(self, external_ids: list[str], at: datetime) -> None:
        del at
        for external_id in external_ids:
            self.screened[external_id] = InitiativeAnalysis(
                external_id=external_id,
                is_useful=False,
                impact_classification="Baseline",
                target_stakeholder="N/A",
                executive_summary="Initial baseline item; not sent.",
                actionability_score=1,
                evidence="N/A",
                ranking_rationale="Excluded from initial baseline.",
            )

    def mark_screened(self, analyses: list[InitiativeAnalysis], at: datetime) -> None:
        del at
        self.screened.update({analysis.external_id: analysis for analysis in analyses})

    def mark_emailed(self, external_ids: list[str], at: datetime) -> None:
        del at
        self.emailed.update(external_ids)

    def record_delivery(
        self, idempotency_key: str, external_ids: list[str], provider_id: str, sent_at: datetime
    ) -> None:
        del external_ids, sent_at
        self.deliveries[idempotency_key] = provider_id
