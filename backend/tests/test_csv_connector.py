"""CSV connector functional tests.

These cover the behaviours the brief requires: streaming, bounded memory, stable source
identifiers, pagination, incremental checkpointing, and clear handling of empty,
malformed, inaccessible, and missing files.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from humisense.connectors.csv_connector import CsvConnector, CsvCursor
from humisense.connectors.types import (
    ConnectorContext,
    Cursor,
    ResourceLimits,
)
from humisense.errors import (
    ConnectorConfigError,
    ConnectorDataError,
    ConnectorResourceLimitError,
)
from humisense.security.paths import PathPolicy
from tests.conftest import synthetic_rows, write_csv

pytestmark = pytest.mark.asyncio


def make_context(
    csv_root: Path,
    *,
    config: dict[str, object],
    page_size: int = 500,
    max_processing_seconds: float = 10.0,
    max_records: int = 100_000,
    max_file_bytes: int = 1_000_000,
) -> ConnectorContext:
    return ConnectorContext(
        organization_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        connector_type="csv",
        correlation_id="test-correlation",
        config=config,
        limits=ResourceLimits(
            max_records=max_records,
            max_file_bytes=max_file_bytes,
            max_processing_seconds=max_processing_seconds,
            page_size=page_size,
        ),
        path_policy=PathPolicy((str(csv_root),), max_file_bytes=max_file_bytes),
    )


async def test_captures_all_rows(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(10))
    context = make_context(csv_root, config={"path": "data.csv", "timestamp_column": "timestamp"})
    connector = CsvConnector(context)
    page = await connector.fetch_records(None, 100)

    assert len(page.records) == 10
    assert page.has_more is False
    first = page.records[0]
    assert first.record_type == "csv_row"
    assert first.payload["machine_id"] == "LINE-01"
    assert first.payload["tag"] == "temperature"
    assert first.payload["unit"] == "degC"
    assert first.source_timestamp is not None
    assert first.source_timestamp.year == 2026
    # Payload values are preserved verbatim: no unit conversion, no rounding.
    assert first.payload["value"] == "60.000"
    assert first.metadata["untrusted"] is True


async def test_source_record_ids_are_stable_across_runs(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(5))
    context = make_context(csv_root, config={"path": "data.csv"})
    first = await CsvConnector(context).fetch_records(None, 100)
    second = await CsvConnector(context).fetch_records(None, 100)

    assert [r.source_record_id for r in first.records] == [
        r.source_record_id for r in second.records
    ]


async def test_rewriting_identical_content_keeps_record_ids_stable(csv_root: Path) -> None:
    """A regenerated but identical file must re-sync as a no-op.

    The identifier is derived from the row's position, not from the file's modification
    time, so an export that rewrites the same rows does not create a duplicate set.
    """
    path = write_csv(csv_root / "data.csv", synthetic_rows(5))
    context = make_context(csv_root, config={"path": "data.csv"})
    before = await CsvConnector(context).fetch_records(None, 100)
    # Rewrite byte-identical content; only the modification time changes.
    write_csv(path, synthetic_rows(5))
    after = await CsvConnector(context).fetch_records(None, 100)
    assert [r.source_record_id for r in before.records] == [
        r.source_record_id for r in after.records
    ]


async def test_changed_content_is_distinguished_by_the_content_hash(csv_root: Path) -> None:
    """A real content change must still be captured as new data.

    The row identifier stays the same, so the envelope's content hash is what tells
    Layer 2 that the row's value changed.
    """
    from humisense.services.records import build_envelope

    path = write_csv(csv_root / "data.csv", synthetic_rows(5))
    context = make_context(csv_root, config={"path": "data.csv"})
    before = await CsvConnector(context).fetch_records(None, 100)
    write_csv(
        path,
        [
            *synthetic_rows(5)[:-1],
            ["2026-01-01T09:00:00Z", "LINE-09", "temperature", "99.5", "degC"],
        ],
    )
    after = await CsvConnector(context).fetch_records(None, 100)

    def envelope(draft: object) -> object:
        return build_envelope(
            draft=draft,  # type: ignore[arg-type]
            organization_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            connector_type="csv",
            correlation_id="test",
        )

    assert (
        envelope(before.records[4]).idempotency_key()
        != envelope(after.records[4]).idempotency_key()
    )


async def test_pagination_is_stable_and_complete(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(25))
    context = make_context(csv_root, config={"path": "data.csv"}, page_size=7)
    connector = CsvConnector(context)

    collected: list[str] = []
    cursor: Cursor | None = None
    pages = 0
    while True:
        page = await connector.fetch_records(cursor, 7)
        collected.extend(record.source_record_id for record in page.records)
        pages += 1
        if not page.has_more or page.next_cursor is None:
            break
        cursor = Cursor.model_validate(page.next_cursor)
        assert pages < 10, "pagination did not terminate"

    assert len(collected) == 25
    assert len(set(collected)) == 25
    assert pages == 4


async def test_checkpoint_resumes_without_duplicates(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(20))
    context = make_context(csv_root, config={"path": "data.csv"}, page_size=5)

    first_page = await CsvConnector(context).fetch_records(None, 5)
    cursor = CsvCursor.model_validate(first_page.next_cursor or {})

    second_page = await CsvConnector(context).fetch_records(cursor, 5)
    first_ids = {r.source_record_id for r in first_page.records}
    second_ids = {r.source_record_id for r in second_page.records}
    assert not (first_ids & second_ids)


async def test_skip_leading_notice_line(csv_root: Path) -> None:
    """Historian exports often start with a banner; it is skipped, never interpreted."""
    write_csv(
        csv_root / "data.csv",
        synthetic_rows(3),
        notice="# SYNTHETIC DATA - NOT REAL HISTORIAN TELEMETRY",
    )
    context = make_context(
        csv_root,
        config={"path": "data.csv", "skip_leading_lines": 1, "timestamp_column": "timestamp"},
    )
    page = await CsvConnector(context).fetch_records(None, 100)
    assert len(page.records) == 3
    assert page.records[0].payload["machine_id"] == "LINE-01"


async def test_row_number_accounts_for_skipped_lines(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(2), notice="# banner")
    context = make_context(csv_root, config={"path": "data.csv", "skip_leading_lines": 1})
    page = await CsvConnector(context).fetch_records(None, 100)
    # Line 1 is the banner, line 2 is the header, line 3 is the first data row.
    assert page.records[0].metadata["row_number"] == 3


async def test_missing_file_is_reported_clearly(csv_root: Path) -> None:
    context = make_context(csv_root, config={"path": "does-not-exist.csv"})
    connector = CsvConnector(context)
    result = await connector.test_connection()
    assert result.ok is False
    assert result.error_code == "connector_source_error"
    # The message must not disclose a host filesystem path.
    assert str(csv_root) not in result.message


async def test_out_of_root_paths_do_not_disclose_existence(csv_root: Path) -> None:
    """T7: a path outside the allowed root must look the same whether or not it exists.

    Otherwise the connector becomes an existence oracle for arbitrary host paths. A path
    *inside* a root is reported differently, which is intentional: the allowed roots are
    operator configuration, not secrets, and an operator needs to distinguish "wrong
    directory" from "file missing".
    """
    existing_outside = csv_root.parent / "outside.txt"
    existing_outside.write_text("data", encoding="utf-8")

    present = await CsvConnector(
        make_context(csv_root, config={"path": str(existing_outside)})
    ).test_connection()
    absent = await CsvConnector(
        make_context(csv_root, config={"path": "/etc/definitely-not-present-xyz"})
    ).test_connection()

    assert present.ok is False and absent.ok is False
    assert present.error_code == absent.error_code == "path_not_allowed"
    assert present.message == absent.message


async def test_empty_file_is_reported(csv_root: Path) -> None:
    (csv_root / "empty.csv").write_text("", encoding="utf-8")
    context = make_context(csv_root, config={"path": "empty.csv"})
    result = await CsvConnector(context).test_connection()
    assert result.ok is False


async def test_header_only_file_tests_ok_without_rows(csv_root: Path) -> None:
    write_csv(csv_root / "header_only.csv", [])
    context = make_context(csv_root, config={"path": "header_only.csv"})
    result = await CsvConnector(context).test_connection()
    assert result.ok is True
    assert result.details["has_data_rows"] is False


async def test_malformed_row_width_is_a_partial_failure(csv_root: Path) -> None:
    (csv_root / "bad.csv").write_text(
        "timestamp,machine_id,tag,value,unit\n"
        "2026-01-01T00:00:00Z,LINE-01,temperature,60.0,degC\n"
        "2026-01-01T00:01:00Z,LINE-01,pressure\n"
        "2026-01-01T00:02:00Z,LINE-02,vibration,1.1,mm_s\n",
        encoding="utf-8",
    )
    context = make_context(csv_root, config={"path": "bad.csv"})
    page = await CsvConnector(context).fetch_records(None, 100)

    assert len(page.records) == 2
    assert len(page.partial_failures) == 1
    failure = page.partial_failures[0]
    assert failure.reason_code == "column_count_mismatch"
    assert failure.location == "row 3"
    # The message must not include the row content.
    assert "pressure" not in failure.safe_message


async def test_unparseable_timestamp_is_not_guessed(csv_root: Path) -> None:
    (csv_root / "odd.csv").write_text(
        "timestamp,machine_id,value\nnot-a-timestamp,LINE-01,1.0\n",
        encoding="utf-8",
    )
    context = make_context(csv_root, config={"path": "odd.csv", "timestamp_column": "timestamp"})
    page = await CsvConnector(context).fetch_records(None, 100)
    assert len(page.records) == 1
    assert page.records[0].source_timestamp is None
    assert page.partial_failures[0].reason_code == "unparseable_timestamp"


async def test_configured_column_missing_from_header_is_rejected(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(2))
    context = make_context(
        csv_root, config={"path": "data.csv", "machine_id_column": "nonexistent"}
    )
    result = await CsvConnector(context).test_connection()
    assert result.ok is False
    assert result.error_code == "connector_data_invalid"


async def test_duplicate_header_columns_are_rejected(csv_root: Path) -> None:
    (csv_root / "dup.csv").write_text(
        "timestamp,tag,tag,value\n2026-01-01T00:00:00Z,a,b,1.0\n", encoding="utf-8"
    )
    context = make_context(csv_root, config={"path": "dup.csv"})
    result = await CsvConnector(context).test_connection()
    assert result.ok is False
    assert result.error_code == "connector_data_invalid"


async def test_row_limit_produces_partial_failure(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(50))
    context = make_context(csv_root, config={"path": "data.csv", "max_rows": 5})
    page = await CsvConnector(context).fetch_records(None, 1000)
    assert len(page.records) <= 5
    assert any(f.reason_code == "row_limit_reached" for f in page.partial_failures)


async def test_processing_time_budget_is_enforced(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(200))
    context = make_context(csv_root, config={"path": "data.csv"}, max_processing_seconds=0.0)
    with pytest.raises(ConnectorResourceLimitError):
        await CsvConnector(context).fetch_records(None, 200)


async def test_unknown_configuration_field_is_rejected(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(2))
    context = make_context(csv_root, config={"path": "data.csv", "injected": "value"})
    with pytest.raises(ConnectorConfigError):
        CsvConnector(context).validate_config({"path": "data.csv", "injected": "value"})


async def test_alternative_delimiter_and_no_header(csv_root: Path) -> None:
    (csv_root / "semi.csv").write_text("2026-01-01T00:00:00Z;LINE-01;60.0\n", encoding="utf-8")
    context = make_context(
        csv_root, config={"path": "semi.csv", "delimiter": ";", "has_header": False}
    )
    page = await CsvConnector(context).fetch_records(None, 10)
    assert page.records[0].payload == {
        "column_1": "2026-01-01T00:00:00Z",
        "column_2": "LINE-01",
        "column_3": "60.0",
    }


async def test_latin1_encoded_file_is_read(csv_root: Path) -> None:
    path = csv_root / "latin.csv"
    path.write_bytes(
        "timestamp,machine_id,tag,value,unit\n2026-01-01T00:00:00Z,LINE-01,temp°,1.0,degC\n".encode(
            "latin-1"
        )
    )
    context = make_context(csv_root, config={"path": "latin.csv"})
    page = await CsvConnector(context).fetch_records(None, 10)
    assert len(page.records) == 1


async def test_undecodable_binary_file_is_rejected(csv_root: Path) -> None:
    (csv_root / "binary.csv").write_bytes(bytes(range(256)) * 8)
    context = make_context(csv_root, config={"path": "binary.csv", "encoding": "utf-8"})
    with pytest.raises((ConnectorDataError, ConnectorConfigError)):
        await CsvConnector(context).fetch_records(None, 10)


async def test_oversized_file_is_rejected_by_policy(csv_root: Path) -> None:
    write_csv(csv_root / "big.csv", synthetic_rows(2000))
    context = make_context(csv_root, config={"path": "big.csv"}, max_file_bytes=512)
    result = await CsvConnector(context).test_connection()
    assert result.ok is False
    assert result.error_code == "connector_resource_limit"


async def test_long_field_is_truncated_with_partial_failure(csv_root: Path) -> None:
    long_value = "x" * 9000
    (csv_root / "long.csv").write_text(
        f"timestamp,machine_id,tag,value,unit\n2026-01-01T00:00:00Z,LINE-01,{long_value},1.0,degC\n",
        encoding="utf-8",
    )
    context = make_context(csv_root, config={"path": "long.csv"})
    page = await CsvConnector(context).fetch_records(None, 10)
    assert any(f.reason_code == "field_too_long" for f in page.partial_failures)
    assert len(page.records[0].payload["tag"]) == 8192


async def test_csv_formula_content_is_preserved_not_executed(csv_root: Path) -> None:
    """T12/T8: a formula-looking cell is inert data, stored verbatim."""
    (csv_root / "formula.csv").write_text(
        "timestamp,machine_id,tag,value,unit\n"
        "2026-01-01T00:00:00Z,LINE-01,=cmd|' /C calc'!A0,1.0,degC\n"
        "@SUM(1+1),LINE-02,normal,2.0,bar\n",
        encoding="utf-8",
    )
    context = make_context(csv_root, config={"path": "formula.csv"})
    page = await CsvConnector(context).fetch_records(None, 10)
    assert page.records[0].payload["tag"] == "=cmd|' /C calc'!A0"
    assert page.records[1].payload["timestamp"] == "@SUM(1+1)"


async def test_fetch_respects_page_size_limit(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(100))
    context = make_context(csv_root, config={"path": "data.csv"}, page_size=3)
    page = await CsvConnector(context).fetch_records(None, 1000)
    assert len(page.records) == 3


async def test_scanned_count_reported(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(6))
    context = make_context(csv_root, config={"path": "data.csv"})
    page = await CsvConnector(context).fetch_records(None, 100)
    assert page.scanned_count == 6


async def _read_all(connector: CsvConnector, page_size: int) -> list[str]:
    """Read every page in order and return the source ids, verifying linear progress."""
    collected: list[str] = []
    cursor: Cursor | None = None
    while True:
        page = await connector.fetch_records(cursor, page_size)
        collected.extend(record.source_record_id for record in page.records)
        if not page.has_more or page.next_cursor is None:
            return collected
        cursor = Cursor.model_validate(page.next_cursor)


async def test_pagination_resumes_by_seek_not_rescan(csv_root: Path) -> None:
    """Regression: a resume must not re-read the file from the beginning.

    The checkpoint carries the byte offset of the next row, so a later page starts where
    the previous one stopped. Without it the connector re-scanned every preceding row on
    each page, which made a full sync quadratic in the row count.
    """
    write_csv(csv_root / "data.csv", synthetic_rows(50))
    connector = CsvConnector(make_context(csv_root, config={"path": "data.csv"}, page_size=10))

    first = await connector.fetch_records(None, 10)
    assert first.has_more is True
    cursor = CsvCursor.model_validate(first.next_cursor or {})
    # The offset must point past the header, at the start of the next data row.
    assert cursor.byte_offset > 0

    second = await connector.fetch_records(cursor, 10)
    # A seek-based resume still yields the same rows as a full scan of the whole file.
    full = await _read_all(
        CsvConnector(make_context(csv_root, config={"path": "data.csv"}, page_size=10)), 10
    )
    assert [r.source_record_id for r in second.records] == full[10:20]


async def test_offset_less_checkpoint_still_resumes_correctly(csv_root: Path) -> None:
    """A checkpoint written by an older build (no byte offset) must keep working.

    Backwards compatibility matters because the platform stores the opaque cursor and may
    hand back a value produced before an upgrade.
    """
    write_csv(csv_root / "data.csv", synthetic_rows(30))
    connector = CsvConnector(make_context(csv_root, config={"path": "data.csv"}, page_size=10))

    first = await connector.fetch_records(None, 10)
    current = CsvCursor.model_validate(first.next_cursor or {})
    # A pre-upgrade checkpoint carried a fingerprint but no byte offset.
    legacy = CsvCursor(
        row_index=current.row_index,
        byte_offset=0,
        file_fingerprint=current.file_fingerprint,
    )
    legacy_page = await connector.fetch_records(legacy, 10)
    full = await _read_all(
        CsvConnector(make_context(csv_root, config={"path": "data.csv"}, page_size=10)), 10
    )
    assert [r.source_record_id for r in legacy_page.records] == full[10:20]


async def test_full_scan_reads_each_row_once(csv_root: Path) -> None:
    """A complete scan must be linear, not quadratic, in the row count.

    The connector counts rows it actually examines. Reading the whole file in pages must
    therefore examine each row about once; a re-scanning implementation would examine far
    more.
    """
    rows = 400
    write_csv(csv_root / "data.csv", synthetic_rows(rows))
    connector = CsvConnector(make_context(csv_root, config={"path": "data.csv"}, page_size=50))

    examined = 0
    cursor: Cursor | None = None
    pages = 0
    while True:
        page = await connector.fetch_records(cursor, 50)
        examined += page.scanned_count
        pages += 1
        if not page.has_more or page.next_cursor is None:
            break
        cursor = Cursor.model_validate(page.next_cursor)

    # 400 rows at 50 per page is 8 full pages plus one trailing empty page.
    assert pages == rows // 50 + 1
    assert examined == rows, f"expected {rows} row reads, examined {examined}"


async def test_close_is_idempotent(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(1))
    context = make_context(csv_root, config={"path": "data.csv"})
    connector = CsvConnector(context)
    await connector.close()
    await connector.close()


async def test_health_check_delegates_to_test(csv_root: Path) -> None:
    write_csv(csv_root / "data.csv", synthetic_rows(3))
    context = make_context(csv_root, config={"path": "data.csv"})
    health = await CsvConnector(context).health_check()
    assert health.healthy is True
