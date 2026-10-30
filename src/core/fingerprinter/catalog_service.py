"""Catalog Service managing .pklz library reconciliation, inspection, and track introspection."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
import threading
from typing import Callable, Sequence

from src.core.config.constants import PKLZ_EXTENSION
from src.core.config.models import PklzCatalogEntry
from src.core.database.catalog_repository import CatalogRepository
from src.core.fingerprinter.builder import (
    FingerprintBuilderCancelledError,
    FingerprintBuilderError,
    FingerprintBuilderService,
    PklzInspectionStats,
    ensure_audfprint_importable,
)
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import SilentProcessRunner

logger = logging.getLogger(__name__)

_BATCH_INDEX_REGEX = re.compile(r"\.part(\d+)\.pklz$", re.IGNORECASE)


class CatalogServiceError(Exception):
    """Base exception for CatalogService failures."""


class CatalogServiceCancelledError(CatalogServiceError):
    """Raised when reconciliation is cancelled by user request."""


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Summary metrics of a directory reconciliation run."""

    discovered_files: int
    new_or_updated: int
    pruned_entries: int
    failed_files: tuple[Path, ...]
    total_tracks: int
    total_hashes: int
    total_size_bytes: int


def extract_prefix_and_batch(file_path: Path, root_dir: Path | None = None) -> tuple[str, int]:
    """Derive prefix label and batch index from file path and optional root directory."""
    name = file_path.name
    match = _BATCH_INDEX_REGEX.search(name)
    if match:
        batch_idx = int(match.group(1))
        prefix = name[: match.start()]
    else:
        batch_idx = 1
        prefix = file_path.stem

    if root_dir is not None:
        try:
            rel_parent = file_path.parent.relative_to(root_dir)
            if str(rel_parent) != ".":
                prefix = f"{rel_parent.as_posix()}/{prefix}"
        except ValueError:
            pass

    return prefix, batch_idx


class CatalogService:
    """Domain service orchestrating .pklz reference database discovery, inspection, and catalog synchronization."""

    def __init__(
        self,
        catalog_repo: CatalogRepository,
        builder: FingerprintBuilderService | None = None,
        runner: SilentProcessRunner | None = None,
        locator: BinaryLocator | None = None,
    ) -> None:
        self._repo = catalog_repo
        if builder is not None:
            self._builder = builder
        else:
            effective_runner = runner or SilentProcessRunner()
            effective_locator = locator or BinaryLocator()
            self._builder = FingerprintBuilderService(
                runner=effective_runner,
                locator=effective_locator,
                catalog_repo=catalog_repo,
            )

    def reconcile_directory(
        self,
        root_dir: Path,
        prune_missing: bool = True,
        force_reindex: bool = False,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> ReconciliationResult:
        """Scan root_dir recursively for .pklz databases, extract metadata, and synchronize the catalog."""
        if not root_dir.is_dir():
            raise CatalogServiceError(f"Directory not found: {root_dir}")

        resolved_root = root_dir.resolve()
        discovered = sorted(
            [p for p in resolved_root.rglob(
                f"*{PKLZ_EXTENSION}") if p.is_file() and not p.name.endswith(".tmp")]
        )
        total_files = len(discovered)

        new_or_updated = 0
        failed_files: list[Path] = []

        for idx, file_path in enumerate(discovered):
            if cancel_event is not None and cancel_event.is_set():
                raise CatalogServiceCancelledError(
                    "Catalog reconciliation cancelled by user.")

            if progress_callback is not None:
                progress_callback(idx + 1, total_files, file_path.name)

            try:
                resolved_file = file_path.resolve()
                curr_size = resolved_file.stat().st_size
                existing = self._repo.get_by_path(resolved_file)

                # Fast path: Skip re-reading pickle if already indexed and size is identical
                if not force_reindex and existing is not None and existing.file_size_bytes == curr_size:
                    continue

                stats = self._builder.inspect_pklz(
                    resolved_file, compute_sha256=True)
                prefix, batch_idx = extract_prefix_and_batch(
                    resolved_file, resolved_root)

                self._repo.upsert_entry(
                    file_path=stats.file_path,
                    prefix_label=prefix,
                    batch_index=batch_idx,
                    track_count=stats.track_count,
                    total_hashes=stats.total_hashes,
                    file_size_bytes=stats.file_size_bytes,
                    sha256_hash=stats.sha256_hash,
                )
                new_or_updated += 1
            except (FingerprintBuilderError, OSError) as exc:
                logger.warning(
                    "Failed to inspect %s during reconciliation: %s", file_path.name, exc)
                failed_files.append(file_path)

        pruned_count = 0
        if prune_missing:
            # Prune records residing inside root_dir whose files no longer exist on disk
            all_entries = self._repo.list_entries()
            for entry in all_entries:
                try:
                    entry.file_path.relative_to(resolved_root)
                    if not entry.file_path.is_file():
                        self._repo.delete_by_path(entry.file_path)
                        pruned_count += 1
                except ValueError:
                    # Entry is not under resolved_root, leave intact
                    continue

        summary = self.get_catalog_summary()
        return ReconciliationResult(
            discovered_files=total_files,
            new_or_updated=new_or_updated,
            pruned_entries=pruned_count,
            failed_files=tuple(failed_files),
            total_tracks=summary["total_tracks"],
            total_hashes=summary["total_hashes"],
            total_size_bytes=summary["total_size_bytes"],
        )

    def inspect_database(self, file_path: Path, compute_sha256: bool = True) -> PklzInspectionStats:
        """Inspect a single .pklz database directly from disk."""
        try:
            return self._builder.inspect_pklz(file_path, compute_sha256=compute_sha256)
        except FingerprintBuilderError as exc:
            raise CatalogServiceError(
                f"Inspection failed for {file_path.name}: {exc}") from exc

    def get_database_track_names(self, file_path: Path) -> list[str]:
        """Load and extract track title strings from a target .pklz hash table."""
        ensure_audfprint_importable()
        import hash_table  # type: ignore[import-not-found]

        resolved_path = file_path.resolve()
        if not resolved_path.is_file():
            raise CatalogServiceError(
                f"Database file not found: {resolved_path}")

        try:
            ht = hash_table.HashTable(str(resolved_path))
            return [str(n) for n in getattr(ht, "names", []) if n is not None]
        except Exception as exc:
            raise CatalogServiceError(
                f"Failed to read track names from {file_path.name}: {exc}") from exc

    def list_catalog(self, prefix_label: str | None = None) -> list[PklzCatalogEntry]:
        """Fetch indexed catalog entries from SQLite."""
        return self._repo.list_entries(prefix_label=prefix_label)

    def get_catalog_summary(self) -> dict[str, int]:
        """Return aggregate track, hash, and size counts across all indexed databases."""
        entries = self._repo.list_entries()
        return {
            "total_databases": len(entries),
            "total_tracks": sum(e.track_count for e in entries),
            "total_hashes": sum(e.total_hashes for e in entries),
            "total_size_bytes": sum(e.file_size_bytes for e in entries),
        }
