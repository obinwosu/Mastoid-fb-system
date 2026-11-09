"""Atomic participant files and lossless import of the earlier shared study JSON."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PARTICIPANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LOCKS: dict[str, Any] = {}
_LOCKS_GUARD = threading.Lock()
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def normalize_participant_id(value: str) -> str:
    participant_id = value.strip()
    if not PARTICIPANT_ID_PATTERN.fullmatch(participant_id):
        raise ValueError(
            "Study ID must be 1 to 64 letters, numbers, periods, dashes, or underscores"
        )
    return participant_id


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=path.parent, prefix="study-state-", suffix=".tmp"
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _empty(participant_id: str) -> dict[str, Any]:
    return {
        "schema": "local_expert_study_state_v1",
        "participant_id": participant_id,
        "expert_study": {
            "schema": "expert_feedback_study_v1",
            "sessions": {},
            "participants": {},
            "events": [],
            "expert_proposed_moments": {},
        },
    }


def _partition(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate ownership before writing anything; preserve row order and unknown metadata."""
    study = dict(payload.get("expert_study") or {})
    partitions: dict[str, dict[str, Any]] = {}

    def target(owner: str) -> dict[str, Any]:
        participant = normalize_participant_id(owner)
        if participant != owner:
            raise ValueError("Stored participant IDs must not contain surrounding whitespace")
        if participant not in partitions:
            result = copy.deepcopy(dict(payload))
            result["participant_id"] = participant
            result["expert_study"] = {
                **copy.deepcopy(study),
                "sessions": {},
                "participants": {},
                "events": [],
                "expert_proposed_moments": {},
            }
            partitions[participant] = result
        return partitions[participant]["expert_study"]

    for participant, row in (study.get("participants") or {}).items():
        if row.get("participant_id", participant) != participant:
            raise ValueError("Participant profile ownership does not match its key")
        target(participant)["participants"][participant] = copy.deepcopy(row)
    for key, row in (study.get("sessions") or {}).items():
        participant, separator, _ = key.partition("::")
        if not separator or row.get("participant_id", participant) != participant:
            raise ValueError("Session ownership does not match its key")
        target(participant)["sessions"][key] = copy.deepcopy(row)
    for row in study.get("events") or []:
        target(row.get("participant_id", ""))["events"].append(copy.deepcopy(row))
    for key, row in (study.get("expert_proposed_moments") or {}).items():
        target(row.get("participant_id", ""))["expert_proposed_moments"][key] = copy.deepcopy(row)
    return partitions


def _checked_payload(path: Path, participant_id: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("participant_id", participant_id) != participant_id:
        raise ValueError(f"Participant file belongs to a different Study ID: {path.name}")
    owners = _partition(payload)
    if set(owners) - {participant_id}:
        raise ValueError(f"Participant file contains other Study IDs: {path.name}")
    return payload


def _participant_path(directory: Path, participant_id: str) -> Path:
    """Keep ordinary filenames readable and avoid Windows aliases on every platform."""
    digest_name = "~" + hashlib.sha256(participant_id.encode()).hexdigest() + ".json"
    hashed = directory / digest_name
    if hashed.exists():
        _checked_payload(hashed, participant_id)
        return hashed
    if participant_id.split(".", 1)[0].upper() in _RESERVED:
        return hashed
    name = participant_id + ".json"
    matches = [p for p in directory.glob("*.json") if p.name.casefold() == name.casefold()]
    for candidate in matches:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        owner = payload.get("participant_id")
        if owner is None:
            owners = _partition(payload)
            owner = next(iter(owners), None) if len(owners) == 1 else None
        if owner == participant_id:
            return candidate
        if not isinstance(owner, str) or owner.casefold() != participant_id.casefold():
            raise ValueError(f"Participant filename and stored identity disagree: {candidate.name}")
    return hashed if matches else directory / name


class ParticipantStateFiles:
    """Store each subject independently while retaining the legacy JSON structure."""

    def __init__(self, path: Path, interface_version: str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.interface_version = interface_version
        self.directory = (
            self.path.with_name(self.path.stem + "_participants")
            if self.path.suffix.lower() == ".json"
            else self.path
        )
        self.recordings_dir = self.directory / "recordings"
        if self.path.suffix.lower() == ".json" and self.path.is_file():
            migrate_study_state(self.path, self.directory)

    def participant_path(self, participant_id: str) -> Path:
        participant_id = normalize_participant_id(participant_id)
        with _file_lock(self.directory / ".participants.lock"):
            return _participant_path(self.directory, participant_id)

    @contextmanager
    def _locked(self, participant_id: str) -> Iterator[None]:
        # The short directory lock also serializes case-insensitive filename allocation.
        # Per-file locks retain atomicity when several case servers write the same subject.
        with _file_lock(self.directory / ".participants.lock"):
            path = _participant_path(self.directory, participant_id)
            with _file_lock(path.with_name(path.name + ".lock")):
                yield

    def _read_unlocked(self, participant_id: str) -> dict[str, Any]:
        path = _participant_path(self.directory, participant_id)
        payload = (
            _checked_payload(path, participant_id) if path.is_file() else _empty(participant_id)
        )
        study = dict(payload.get("expert_study") or {})
        return {
            **payload,
            "schema": "local_expert_study_state_v1",
            "participant_id": participant_id,
            "expert_study": {
                **study,
                "schema": "expert_feedback_study_v1",
                "sessions": dict(study.get("sessions") or {}),
                "participants": dict(study.get("participants") or {}),
                "events": list(study.get("events") or []),
                "expert_proposed_moments": dict(study.get("expert_proposed_moments") or {}),
            },
        }

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        participant_id = normalize_participant_id(payload["participant_id"])
        if set(_partition(payload)) - {participant_id}:
            raise ValueError("Cannot write another Study ID into a participant file")
        _write_json(_participant_path(self.directory, participant_id), payload)


def migrate_study_state(source: Path, destination: Path) -> dict[str, Any]:
    """Split a legacy file once, preserving its bytes and all participant-owned records."""
    source, destination = (
        Path(source).expanduser().resolve(),
        Path(destination).expanduser().resolve(),
    )
    if source == destination or source.is_relative_to(destination):
        raise ValueError("Legacy source must be outside the participant destination directory")
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_key = hashlib.sha256(os.path.normcase(str(source)).encode()).hexdigest()
    marker = destination / ".migrations" / f"{source_key}.json"
    backup = marker.with_name(source_key + ".source.json")
    with _file_lock(destination / ".participants.lock"):
        if marker.is_file():
            receipt = json.loads(marker.read_text(encoding="utf-8"))
            if receipt.get("source_sha256") != source_hash:
                raise ValueError(
                    "Legacy study file changed after migration; reconcile it explicitly"
                )
            if (
                not backup.is_file()
                or hashlib.sha256(backup.read_bytes()).hexdigest() != source_hash
            ):
                raise ValueError("Migration backup is missing or changed")
            for participant, filename in receipt["participant_files"].items():
                _checked_payload(destination / filename, participant)
            return receipt
        payload = json.loads(source_bytes.decode("utf-8"))
        partitions = _partition(payload)
        paths: dict[str, Path] = {}
        folded: set[str] = set()
        for participant, value in partitions.items():
            path = _participant_path(destination, participant)
            if path.name.casefold() in folded:
                path = destination / (
                    "~" + hashlib.sha256(participant.encode()).hexdigest() + ".json"
                )
            folded.add(path.name.casefold())
            paths[participant] = path
            if path.is_file() and _checked_payload(path, participant) != value:
                raise ValueError(
                    f"Migration would overwrite existing participant data: {participant}"
                )
        attachments = {}
        for row in (payload.get("expert_study", {}).get("expert_proposed_moments") or {}).values():
            filename = (row.get("audio") or {}).get("file_name")
            if not filename:
                continue
            if Path(filename).name != filename or "/" in filename or "\\" in filename:
                raise ValueError("Legacy audio filename must be a basename")
            data = (source.parent / "recordings" / filename).read_bytes()
            target = destination / "recordings" / filename
            if target.exists() and target.read_bytes() != data:
                raise ValueError(f"Migration audio destination already differs: {filename}")
            attachments[target] = data
        if backup.exists() and backup.read_bytes() != source_bytes:
            raise ValueError("Migration backup already contains different source data")
        _atomic_bytes(backup, source_bytes)
        for participant, value in partitions.items():
            if not paths[participant].exists():
                _write_json(paths[participant], value)
        for target, data in attachments.items():
            if not target.exists():
                _atomic_bytes(target, data)
        receipt = {
            "schema": "participant_study_migration_v1",
            "status": "completed",
            "source": str(source),
            "source_sha256": source_hash,
            "source_backup": str(backup),
            "destination": str(destination),
            "migrated_at": datetime.now(UTC).isoformat(),
            "participant_count": len(partitions),
            "participant_files": {key: path.name for key, path in paths.items()},
            "audio_file_count": len(attachments),
            "source_modified": False,
        }
        _write_json(marker, receipt)
        return receipt
