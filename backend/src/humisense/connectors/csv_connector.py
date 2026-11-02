"""CSV connector.

This is the first fully implemented connector and the reference for the SDK. It reads a
delimiter-separated file from a location the operator has explicitly allowlisted and
produces one :class:`SourceRecordDraft` per data row.

Safety properties, each covered by a test:

* The path must resolve, after following symlinks, inside a configured allowed root.
* Files are opened read-only and streamed; memory use is bounded by the batch size, not
  the file size.
* File size, row count, and processing time are capped.
* The file is never executed, and cell values are never evaluated as formulas or macros.
* Errors surfaced to an operator never include file contents or absolute host paths.
* ``source_record_id`` is stable across runs, so re-syncing the same file is idempotent.
* No write, search, webhook, or live capability is advertised, because none is
  implemented.
"""

from __future__ import annotations

import csv
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from humisense.connectors.base import BaseConnector
from humisense.connectors.types import (
    AuthMode,
    Availability,
    ConfigField,
    ConfigFieldType,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorManifest,
    Cursor,
    FetchPage,
    PartialFailure,
    SourceRecordDraft,
    ValidatedConfig,
)
from humisense.errors import (
    ConnectorConfigError,
    ConnectorDataError,
    ConnectorResourceLimitError,
    ConnectorSourceError,
    HumisenseError,
)
from humisense.security.paths import PathPolicy

RECORD_TYPE = "csv_row"
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")

_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


class CsvConfig(BaseModel):
    """Non-secret configuration for a CSV connection."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=1024,
        description="File path relative to an allowed root, or an absolute path inside one.",
    )
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    quotechar: str = Field(default='"', min_length=1, max_length=1)
    encoding: str | None = Field(default=None, max_length=32)
    has_header: bool = True
    skip_leading_lines: int = Field(
        default=0,
        ge=0,
        le=1000,
        description=(
            "Number of leading lines to ignore before the header. Historian exports "
            "commonly start with a comment or banner line. Ignoring lines is bounded "
            "and never evaluated."
        ),
    )
    timestamp_column: str | None = Field(default=None, max_length=120)
    machine_id_column: str | None = Field(default=None, max_length=120)
    tag_column: str | None = Field(default=None, max_length=120)
    value_column: str | None = Field(default=None, max_length=120)
    unit_column: str | None = Field(default=None, max_length=120)
    max_rows: int = Field(default=100_000, ge=1, le=1_000_000)
    timestamp_format: str | None = Field(default=None, max_length=64)
    """An explicit ``strptime`` format. When omitted, ISO-8601 is attempted. Layer 1 does
    not guess a format: an unparseable timestamp yields a null ``source_timestamp`` and a
    partial-failure entry, never an inferred value."""

    @field_validator("path")
    @classmethod
    def _reject_control_characters(cls, value: str) -> str:
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("path must not contain control characters")
        return value

    @field_validator("delimiter", "quotechar")
    @classmethod
    def _single_character(cls, value: str) -> str:
        if len(value) != 1:
            raise ValueError("must be exactly one character")
        return value

    @field_validator("delimiter")
    @classmethod
    def _delimiter_sane(cls, value: str) -> str:
        if value in {"\n", "\r", '"', "\x00"}:
            raise ValueError("delimiter is not a usable separator")
        return value


class CsvCursor(Cursor):
    """Pagination checkpoint for the CSV connector."""

    row_index: int = 0
    """Zero-based index of the next data row to read, excluding the header."""

    byte_offset: int = 0
    """Byte offset of the next data row, used to resume without re-scanning.

    Without it, resuming a page re-opened the file and re-read every preceding row, so a
    full scan was quadratic in the row count. The offset is captured from ``tell()`` at a
    row boundary, so a resume seeks straight to the checkpoint. Zero means "no usable
    offset", in which case the reader falls back to skipping ``row_index`` rows, which
    keeps a checkpoint written by an older build working."""

    file_fingerprint: str | None = None
    """SHA-256 of the file's size and modification time. A mismatch restarts the scan so
    a file replaced between pages is not silently skipped."""


class CsvConnector(BaseConnector):
    """Reads records from a delimiter-separated file."""

    manifest: ClassVar[ConnectorManifest] = ConnectorManifest(
        connector_type="csv",
        display_name="CSV / Delimited File",
        description=(
            "Captures records from a delimited text file in a location the administrator "
            "has allowlisted. Read-only."
        ),
        vendor="Humisense",
        version="1.0.0",
        auth_modes=(AuthMode.NONE,),
        capabilities=ConnectorCapabilities(
            supports_incremental=True,
            supports_pagination=True,
            supports_timestamps=True,
            supports_metadata=True,
            supports_health_check=True,
            supports_search=False,
            supports_webhook=False,
            supports_write=False,
            supports_live=False,
        ),
        availability=Availability.AVAILABLE,
        record_types=(RECORD_TYPE,),
        config_fields=(
            ConfigField(
                name="path",
                label="File path",
                field_type=ConfigFieldType.PATH,
                help_text=(
                    "Relative to a server-configured allowed root. Absolute paths are "
                    "accepted only when they resolve inside an allowed root."
                ),
                placeholder="historian_export.csv",
                max_length=1024,
            ),
            ConfigField(
                name="delimiter",
                label="Delimiter",
                field_type=ConfigFieldType.STRING,
                required=False,
                default=",",
                max_length=1,
            ),
            ConfigField(
                name="has_header",
                label="First row is a header",
                field_type=ConfigFieldType.BOOLEAN,
                required=False,
                default=True,
            ),
            ConfigField(
                name="skip_leading_lines",
                label="Leading lines to ignore",
                field_type=ConfigFieldType.INTEGER,
                required=False,
                default=0,
                help_text=(
                    "Lines before the header, such as a banner or comment. Bounded to 1000."
                ),
            ),
            ConfigField(
                name="timestamp_column",
                label="Timestamp column",
                field_type=ConfigFieldType.STRING,
                required=False,
                placeholder="timestamp",
                max_length=120,
            ),
            ConfigField(
                name="machine_id_column",
                label="Machine ID column",
                field_type=ConfigFieldType.STRING,
                required=False,
                placeholder="machine_id",
                max_length=120,
            ),
            ConfigField(
                name="tag_column",
                label="Tag column",
                field_type=ConfigFieldType.STRING,
                required=False,
                placeholder="tag",
                max_length=120,
            ),
            ConfigField(
                name="value_column",
                label="Value column",
                field_type=ConfigFieldType.STRING,
                required=False,
                placeholder="value",
                max_length=120,
            ),
            ConfigField(
                name="unit_column",
                label="Unit column",
                field_type=ConfigFieldType.STRING,
                required=False,
                placeholder="unit",
                max_length=120,
            ),
            ConfigField(
                name="timestamp_format",
                label="Timestamp format",
                field_type=ConfigFieldType.STRING,
                required=False,
                help_text="strptime format. Leave blank to attempt ISO-8601 only.",
                placeholder="%Y-%m-%d %H:%M:%S",
                max_length=64,
            ),
            ConfigField(
                name="max_rows",
                label="Maximum rows",
                field_type=ConfigFieldType.INTEGER,
                required=False,
                default=100_000,
                help_text="Hard cap on rows captured per synchronization.",
            ),
        ),
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._path_policy: PathPolicy | None = None
        self._resolved: Path | None = None

    # --- Configuration ----------------------------------------------------

    def _validated(self) -> CsvConfig:
        try:
            return CsvConfig.model_validate(self.context.config)
        except Exception as exc:  # pydantic ValidationError
            raise ConnectorConfigError(
                "The CSV configuration is invalid.",
                detail=f"csv config validation failed: {type(exc).__name__}",
            ) from exc

    def _resolve(self, config: CsvConfig) -> Path:
        """Resolve the configured path, enforcing containment and the size cap.

        Containment is checked first, on the fully resolved path, so a symlink or a
        traversal sequence cannot escape the allowed root. Existence is then reported as
        an access failure: a missing file and a disallowed path produce the same
        client-visible code, so an operator cannot use the connector to probe which host
        paths exist.
        """
        policy = self._policy()
        resolved = policy.resolve_file(config.path, must_exist=False)
        try:
            size = resolved.path.stat().st_size
        except FileNotFoundError:
            raise ConnectorSourceError(
                "The file could not be found in the permitted locations.",
                detail="configured csv path does not exist",
            ) from None
        except OSError as exc:
            raise ConnectorSourceError(
                "The file could not be read.",
                detail="stat failed while resolving the configured csv path",
            ) from exc
        if size > self.context.limits.max_file_bytes:
            raise ConnectorResourceLimitError(
                detail="File exceeds the configured maximum size",
                context={"size_bytes": size, "max_bytes": self.context.limits.max_file_bytes},
            )
        return resolved.path

    def _policy(self) -> PathPolicy:
        if self._path_policy is None:
            policy = self.context.path_policy
            if policy is None:
                raise ConnectorConfigError(
                    "No file locations are configured for this environment.",
                    detail="CsvConnector requires a PathPolicy in the execution context",
                )
            if not isinstance(policy, PathPolicy):
                raise ConnectorConfigError(
                    "The file location policy is not usable.",
                    detail="context.path_policy is not a PathPolicy",
                )
            self._path_policy = policy
        return self._path_policy

    def validate_config(self, config: dict[str, Any]) -> ValidatedConfig:
        """Validate configuration without touching the filesystem."""
        merged = {**self.context.config, **config}
        try:
            parsed = CsvConfig.model_validate(merged)
        except Exception as exc:
            raise ConnectorConfigError(
                "The CSV configuration is invalid.",
                detail=f"csv config validation failed: {type(exc).__name__}",
            ) from exc

        warnings: list[str] = []
        policy = self.context.path_policy
        if policy is not None:
            try:
                # must_exist=False: validation runs before the file necessarily exists.
                policy.resolve_file(parsed.path, must_exist=False)
            except Exception as exc:
                raise ConnectorConfigError(
                    "The configured path is outside the permitted locations.",
                    detail="path containment check failed",
                ) from exc
        else:
            warnings.append(
                "File location policy was not available during validation; the path is "
                "re-checked when the connection is tested."
            )

        return ValidatedConfig(values=parsed.model_dump(), warnings=tuple(warnings))

    # --- Connection test --------------------------------------------------

    async def test_connection(self) -> ConnectionTestResult:
        """Open the file, validate the header, and read one data row."""
        started = time.monotonic()
        try:
            config = self._validated()
            path = self._resolve(config)
            encoding = self._pick_encoding(config, path)
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=config.delimiter, quotechar=config.quotechar)
                self._skip_leading_lines(reader, config.skip_leading_lines)
                header = next(reader, None)
                if config.has_header and header is None:
                    return ConnectionTestResult(
                        ok=False,
                        message="The file is empty.",
                        error_code="connector_data_invalid",
                        latency_ms=(time.monotonic() - started) * 1000,
                    )
                if config.has_header and header is not None:
                    self._validate_header(config, header)
                first_row = next(reader, None)
                rows_present = first_row is not None
        except HumisenseError as exc:
            # Every typed connector failure becomes a result. A test must report, not raise:
            # an operator needs the reason, and the API must not turn a bad path or a
            # malformed header into an unhandled 500.
            return ConnectionTestResult(
                ok=False,
                message=exc.public_message,
                error_code=exc.code,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        except OSError as exc:
            self._log.warning("csv_open_failed", error_type=type(exc).__name__)
            return ConnectionTestResult(
                ok=False,
                message="The file could not be opened. Check that it exists and is readable.",
                error_code="connector_source_error",
                latency_ms=(time.monotonic() - started) * 1000,
            )

        return ConnectionTestResult(
            ok=True,
            message=(
                "File opened and parsed successfully."
                if rows_present
                else "File opened successfully but contains no data rows."
            ),
            latency_ms=(time.monotonic() - started) * 1000,
            details={
                "size_bytes": path.stat().st_size,
                "has_data_rows": rows_present,
                "read_only": True,
            },
        )

    async def health_check(self) -> Any:
        from humisense.connectors.types import HealthResult

        result = await self.test_connection()
        return HealthResult(
            healthy=result.ok,
            message=result.message,
            details={"latency_ms": result.latency_ms},
        )

    # --- Capture ----------------------------------------------------------

    async def fetch_records(self, cursor: Cursor | None, limit: int) -> FetchPage:
        """Read one page of rows, starting after the checkpoint."""
        started = time.monotonic()
        config = self._validated()
        path = self._resolve(config)

        csv_cursor = self._coerce_cursor(cursor)
        fingerprint = self._fingerprint(path)

        start_index = csv_cursor.row_index
        if csv_cursor.file_fingerprint and csv_cursor.file_fingerprint != fingerprint:
            # The file changed since the last page. Restart rather than skip rows.
            self._log.info(
                "csv_file_changed_restarting",
                previous_fingerprint=csv_cursor.file_fingerprint,
            )
            start_index = 0
        if not csv_cursor.file_fingerprint:
            start_index = 0

        # ``max_rows`` bounds the whole scan by absolute row index; it must not also
        # cap the page, or the bound could never be observed and reported.
        page_limit = min(limit, self.context.limits.page_size)
        if page_limit < 1:
            raise ConnectorConfigError(detail="page limit resolved to zero")

        records: list[SourceRecordDraft] = []
        failures: list[PartialFailure] = []
        row_index = 0
        scanned = 0
        next_cursor: dict[str, Any] | None = None
        has_more = False

        try:
            encoding = self._pick_encoding(config, path)
            with path.open("r", encoding=encoding, newline="") as handle:
                # Wrap the file in a line generator so ``tell()`` stays usable: csv.reader
                # disables it when it iterates a file object directly, but a generator
                # calling ``readline()`` keeps byte offsets available.
                reader = csv.reader(
                    self._line_iterator(handle),
                    delimiter=config.delimiter,
                    quotechar=config.quotechar,
                )
                self._skip_leading_lines(reader, config.skip_leading_lines)
                header: list[str] | None = None
                if config.has_header:
                    header = next(reader, None)
                    if header is None:
                        return FetchPage(records=[], has_more=False, scanned_count=0)
                    self._validate_header(config, header)

                data_start_offset = handle.tell()

                # Fast path: a checkpoint written by this build carries the byte offset of
                # the next row, so the page resumes with a single seek instead of
                # re-reading every preceding row. A checkpoint without an offset (or one
                # whose fingerprint no longer matches) falls back to skipping rows, which
                # keeps an older checkpoint working and restarts cleanly after a change.
                seek_offset = csv_cursor.byte_offset
                if seek_offset < data_start_offset or (
                    csv_cursor.file_fingerprint and csv_cursor.file_fingerprint != fingerprint
                ):
                    seek_offset = 0
                if seek_offset:
                    handle.seek(seek_offset)
                    row_index = csv_cursor.row_index
                    start_index = 0

                stopped_early = False
                while True:
                    # ``tell()`` before pulling the row is the start of that row: csv.reader
                    # consumes lines lazily, so nothing past this row has been read yet.
                    row_offset = handle.tell()
                    row = next(reader, None)
                    if row is None:
                        break
                    data_index = row_index
                    row_index += 1
                    if data_index < start_index:
                        continue
                    if len(records) >= page_limit:
                        has_more = True
                        next_cursor = CsvCursor(
                            row_index=data_index,
                            byte_offset=row_offset,
                            file_fingerprint=fingerprint,
                        ).as_dict()
                        stopped_early = True
                        break

                    self.check_cancellation()
                    self.enforce_time_budget(started)

                    scanned += 1
                    if data_index >= config.max_rows:
                        failures.append(
                            PartialFailure(
                                source_record_id=None,
                                reason_code="row_limit_reached",
                                safe_message=(
                                    "Row limit reached; remaining rows were not captured."
                                ),
                                location=f"row {self._line_number(data_index, config)}",
                            )
                        )
                        has_more = False
                        next_cursor = None
                        stopped_early = True
                        break

                    draft = self._row_to_record(
                        row=row,
                        header=header,
                        data_index=data_index,
                        fingerprint=fingerprint,
                        file_name=path.name,
                        config=config,
                        failures=failures,
                    )
                    if draft is not None:
                        records.append(draft)

                    if len(records) >= page_limit:
                        has_more = True
                        next_cursor = CsvCursor(
                            row_index=data_index + 1,
                            byte_offset=handle.tell(),
                            file_fingerprint=fingerprint,
                        ).as_dict()
                        break
        except ConnectorConfigError:
            raise
        except OSError as exc:
            self._log.warning("csv_read_failed", error_type=type(exc).__name__)
            raise ConnectorSourceError(
                "The file could not be read.",
                detail="csv read raised OSError",
            ) from exc
        except UnicodeDecodeError as exc:
            raise ConnectorDataError(
                "The file could not be decoded with the configured encoding.",
                detail="unicode decode error during csv read",
            ) from exc

        if not stopped_early and next_cursor is None:
            next_cursor = CsvCursor(row_index=row_index, file_fingerprint=fingerprint).as_dict()

        return FetchPage(
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
            partial_failures=failures,
            scanned_count=scanned,
        )

    # --- Internals --------------------------------------------------------

    def _coerce_cursor(self, cursor: Cursor | None) -> CsvCursor:
        if cursor is None:
            return CsvCursor()
        if isinstance(cursor, CsvCursor):
            return cursor
        try:
            return CsvCursor.model_validate(cursor.model_dump())
        except ValidationError:
            # An unreadable checkpoint restarts the scan rather than failing the job.
            return CsvCursor()

    def _row_to_record(
        self,
        *,
        row: list[str],
        header: list[str] | None,
        data_index: int,
        fingerprint: str,
        file_name: str,
        config: CsvConfig,
        failures: list[PartialFailure],
    ) -> SourceRecordDraft | None:
        """Convert one CSV row into a record draft, or record a partial failure."""
        if not row:
            return None
        if header is not None and len(row) != len(header):
            failures.append(
                PartialFailure(
                    source_record_id=None,
                    reason_code="column_count_mismatch",
                    safe_message=(
                        f"Row has {len(row)} columns but the header declares {len(header)}."
                    ),
                    location=f"row {self._line_number(data_index, config)}",
                )
            )
            return None

        if header is not None:
            payload: dict[str, Any] = {}
            for position, column in enumerate(header):
                key = column.strip() or f"column_{position + 1}"
                payload[key] = row[position] if position < len(row) else None
        else:
            payload = {f"column_{i + 1}": value for i, value in enumerate(row)}

        source_timestamp: datetime | None = None
        if config.timestamp_column:
            raw_timestamp = payload.get(config.timestamp_column)
            if raw_timestamp in (None, ""):
                failures.append(
                    PartialFailure(
                        source_record_id=None,
                        reason_code="missing_timestamp",
                        safe_message="Row has no value in the configured timestamp column.",
                        location=f"row {self._line_number(data_index, config)}",
                    )
                )
            else:
                source_timestamp = self._parse_timestamp(str(raw_timestamp), config)
                if source_timestamp is None:
                    failures.append(
                        PartialFailure(
                            source_record_id=None,
                            reason_code="unparseable_timestamp",
                            safe_message=(
                                "Timestamp did not match the configured format; it was "
                                "left unset rather than guessed."
                            ),
                            location=f"row {self._line_number(data_index, config)}",
                        )
                    )

        # Physical line number in the file, so an operator can find the row directly.
        header_lines = 1 if config.has_header else 0
        metadata: dict[str, Any] = {
            "row_number": data_index + 1 + header_lines + config.skip_leading_lines,
            "source_format": "csv",
            "file_fingerprint": fingerprint,
            "untrusted": True,
        }
        for label, column_name in (
            ("machine_id", config.machine_id_column),
            ("tag", config.tag_column),
            ("unit", config.unit_column),
        ):
            if column_name and payload.get(column_name) not in (None, ""):
                metadata[label] = str(payload[column_name])

        if config.value_column and payload.get(config.value_column) not in (None, ""):
            metadata["raw_value"] = str(payload[config.value_column])

        for key in payload:
            if isinstance(payload[key], str) and len(payload[key]) > 8192:
                failures.append(
                    PartialFailure(
                        source_record_id=None,
                        reason_code="field_too_long",
                        safe_message="A field exceeded the maximum length and was truncated.",
                        location=f"row {self._line_number(data_index, config)}",
                    )
                )
                payload[key] = payload[key][:8192]

        # The identifier is derived from the file's name and the row's position, never
        # from the modification time. A file that is regenerated with identical content
        # (a nightly export, or the same data re-exported) keeps the same identifiers, so
        # re-syncing it is a no-op instead of inserting a duplicate set of rows. A genuine
        # content change is still detected, because the envelope's content hash differs
        # and the uniqueness constraint covers ``source_record_id`` and ``content_hash``
        # together.
        return SourceRecordDraft(
            source_record_id=f"{self._stable_file_digest(file_name)}:{data_index}",
            record_type=RECORD_TYPE,
            payload=payload,
            metadata=metadata,
            source_timestamp=source_timestamp,
        )

    @staticmethod
    def _line_number(data_index: int, config: CsvConfig) -> int:
        """Return the physical 1-based line number for a zero-based data row index."""
        header_lines = 1 if config.has_header else 0
        return data_index + 1 + header_lines + config.skip_leading_lines

    @staticmethod
    def _line_iterator(handle: Any) -> Any:
        """Yield physical lines, keeping the file's byte offset observable.

        ``csv.reader`` disables ``tell()`` on a file object it iterates directly, because
        it may read ahead. Feeding it a generator that calls ``readline()`` one line at a
        time keeps ``tell()`` usable and means the offset observed before a row is pulled
        is exactly the start of that row.
        """
        while True:
            line = handle.readline()
            if not line:
                return
            yield line

    @staticmethod
    def _skip_leading_lines(reader: Any, count: int) -> None:
        """Consume ``count`` leading lines without interpreting them."""
        for _ in range(count):
            try:
                next(reader)
            except StopIteration:
                return

    def _validate_header(self, config: CsvConfig, header: list[str]) -> None:
        stripped = [column.strip() for column in header]
        if not any(stripped):
            raise ConnectorDataError("The file header is empty.")
        duplicates = {name for name in stripped if stripped.count(name) > 1 and name}
        if duplicates:
            raise ConnectorDataError(
                "The file header contains duplicate column names.",
                detail=f"duplicate column count={len(duplicates)}",
            )
        required_columns = [
            column
            for column in (
                config.timestamp_column,
                config.machine_id_column,
                config.tag_column,
                config.value_column,
                config.unit_column,
            )
            if column
        ]
        missing = [column for column in required_columns if column not in stripped]
        if missing:
            raise ConnectorDataError(
                "A configured column is not present in the file header.",
                detail=f"missing column count={len(missing)}",
            )

    def _parse_timestamp(self, raw: str, config: CsvConfig) -> datetime | None:
        """Parse a timestamp using only the configured or ISO-8601 format.

        A failed parse returns ``None``; Layer 1 does not invent a timestamp. The caller
        records a partial failure so the gap is visible rather than hidden.
        """
        value = raw.strip()
        if not value:
            return None
        if config.timestamp_format:
            try:
                return datetime.strptime(value, config.timestamp_format)
            except ValueError:
                return None
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def _pick_encoding(self, config: CsvConfig, path: Path) -> str:
        """Choose a text encoding.

        When the operator did not pin one, the file is probed with a bounded read. This
        is a bounded, read-only heuristic, not a content guess that affects semantics.
        """
        if config.encoding:
            return config.encoding
        try:
            sample = path.open("rb").read(64 * 1024)
        except OSError as exc:
            raise ConnectorSourceError("The file could not be read.") from exc
        for candidate in _ENCODING_CANDIDATES:
            try:
                sample.decode(candidate)
            except UnicodeDecodeError:
                continue
            return candidate
        raise ConnectorDataError("The file could not be decoded with any supported text encoding.")

    @staticmethod
    def _stable_file_digest(file_name: str) -> str:
        """Return a digest of the file's name, used to namespace row identifiers.

        Only the name is used. The absolute path is deliberately excluded so that moving
        the allowlisted root does not silently re-identify every row.
        """
        return hashlib.sha256(file_name.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _fingerprint(path: Path) -> str:
        """Return a stable identity for the file's current content version.

        Size and nanosecond modification time are sufficient to detect a replacement
        between pages, and far cheaper than hashing a large file.
        """
        stat = path.stat()
        material = f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def close(self) -> None:
        self._path_policy = None
        self._resolved = None
