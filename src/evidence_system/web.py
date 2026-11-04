"""Local evidence-feedback web service with explicit read/write app modes."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

from .coaching_plan import build_session_coaching_plan, load_cohort_benchmark
from .ray_v3_view import build_ray_v3_view
from .retrieval import EvidenceRetriever
from .study_surveys import (
    FLOW_SCHEMA,
    SURVEY_DEFINITION,
    new_review_flow,
    public_review_flow,
    response_record,
    validate_answers,
)

APP_MODES = {"recommendations", "expert-study"}
EXPERT_STUDY_SCHEMA = "expert_feedback_study_v1"
DEFAULT_INTERFACE_VERSION = "expert-feedback-v1"
EXPERT_AUDIO_MAX_BYTES = 8 * 1024 * 1024
EXPERT_AUDIO_MIME_TYPES = {
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
EXPERT_STUDY_EVENT_TYPES = {
    "study_started",
    "study_resumed",
    "stimulus_opened",
    "video_play",
    "video_pause",
    "video_seek",
    "blind_submitted",
    "feedback_revealed",
    "revealed_submitted",
    "case_completed",
    "next_case_opened",
    "analysis_started",
    "analysis_completed",
    "review_view_opened",
    "review_step_advanced",
    "feedback_item_opened",
    "feedback_clip_played",
    "feedback_signal_frame_opened",
    "support_details_opened",
    "experience_completed",
    "expert_moment_saved",
    "expert_moment_deleted",
    "audio_recording_started",
}
PARTICIPANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _official_ui_directory(mode: str) -> Path:
    if mode not in APP_MODES:
        raise ValueError(f"Unsupported app mode: {mode}")
    directory = {
        "recommendations": "recommendations",
        "expert-study": "expert_study",
    }[mode]
    return (Path(__file__).resolve().parents[2] / "apps" / directory).resolve()


def _validate_ui_directory(mode: str, ui_dir: Path) -> Path:
    expected = _official_ui_directory(mode)
    actual = ui_dir.expanduser().resolve()
    if actual != expected:
        raise ValueError(f"{mode} mode must use {expected}; got {actual}")
    return actual


def normalize_participant_id(value: str) -> str:
    participant_id = value.strip()
    if not PARTICIPANT_ID_PATTERN.fullmatch(participant_id):
        raise ValueError(
            "Study ID must be 1 to 64 letters, numbers, periods, dashes, or underscores"
        )
    return participant_id


class ExpertStudyStore:
    """Atomic local study storage, separate from the read-only evidence database."""

    _path_locks: dict[str, Any] = {}
    _path_locks_guard = threading.Lock()

    def __init__(self, path: Path, interface_version: str) -> None:
        self.path = path
        self.interface_version = interface_version
        key = os.path.normcase(str(path.resolve()))
        with self._path_locks_guard:
            self._lock = self._path_locks.setdefault(key, threading.RLock())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize transactions across case servers sharing this state file."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            with lock_path.open("a+b") as handle:
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

    def _empty(self) -> dict[str, Any]:
        return {
            "schema": "local_expert_study_state_v1",
            "expert_study": {
                "schema": EXPERT_STUDY_SCHEMA,
                "sessions": {},
                "participants": {},
                "events": [],
                "expert_proposed_moments": {},
            },
        }

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        study = dict(payload.get("expert_study") or {})
        return {
            **payload,
            "schema": "local_expert_study_state_v1",
            "expert_study": {
                **study,
                "schema": EXPERT_STUDY_SCHEMA,
                "sessions": dict(study.get("sessions") or {}),
                "participants": dict(study.get("participants") or {}),
                "events": list(study.get("events") or []),
                "expert_proposed_moments": dict(study.get("expert_proposed_moments") or {}),
            },
        }

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=self.path.parent,
            prefix="study-state-",
            suffix=".tmp",
        ) as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(self.path)

    @staticmethod
    def _session_key(participant_id: str, run_id: str) -> str:
        return f"{participant_id}::{run_id}"

    def progress(self, participant_id: str, run_id: str) -> dict[str, Any]:
        participant_id = normalize_participant_id(participant_id)
        with self._locked():
            study = self._read_unlocked()["expert_study"]
            return dict(study["sessions"].get(self._session_key(participant_id, run_id)) or {})

    def start_session(
        self,
        *,
        participant_id: str,
        run_id: str,
        stimulus_ids: Sequence[str],
        first_review_view: str = "event-based",
        final_case: bool = True,
    ) -> dict[str, Any]:
        if first_review_view not in {"event-based", "summary"}:
            raise ValueError("First review view must be event-based or summary")
        participant_id = normalize_participant_id(participant_id)
        key = self._session_key(participant_id, run_id)
        now = datetime.now(UTC).isoformat()
        with self._locked():
            payload = self._read_unlocked()
            sessions = payload["expert_study"]["sessions"]
            existing = dict(sessions.get(key) or {})
            row = {
                **existing,
                "participant_id": participant_id,
                "run_id": run_id,
                "stimulus_ids": list(stimulus_ids),
                "started_at": existing.get("started_at") or now,
                "last_opened_at": now,
                "first_review_view": existing.get("first_review_view") or first_review_view,
                "case_completed_at": existing.get("case_completed_at"),
                "interface_version_at_start": (
                    existing.get("interface_version_at_start") or self.interface_version
                ),
                "interface_version_last_opened": self.interface_version,
            }
            if "review_flow" not in row:
                first = row["first_review_view"]
                if existing:
                    # A participant may already have seen feedback under an older protocol.
                    # Never manufacture a fresh, uncontaminated baseline for that session.
                    row["review_flow"] = {
                        "schema": "sequential_review_v1",
                        "order": [first, "summary" if first == "event-based" else "event-based"],
                        "stage": "review", "index": 0, "current_view": first,
                        "questionnaires": {}, "transitions": [], "created_at": now,
                        "completed_at": None,
                    }
                else:
                    profile = payload["expert_study"]["participants"].get(participant_id) or {}
                    row["review_flow"] = new_review_flow(
                        first, now, background=not bool(profile.get("background")),
                        final_case=final_case,
                    )
            flow = row["review_flow"]
            profile = payload["expert_study"]["participants"].get(participant_id) or {}
            if (
                flow.get("schema") == FLOW_SCHEMA
                and flow.get("stage") == "background"
                and profile.get("background")
            ):
                # Another case may have saved the shared background after this tab opened.
                # Resume at the still-unanswered baseline without duplicating that response.
                flow["cursor"] += 1
                flow.update(flow["steps"][flow["cursor"]])
                flow["transitions"].append({
                    "from_stage": "background", "to_stage": "self_assessment",
                    "from_step_id": "background", "to_step_id": flow["step_id"],
                    "reason": "background_saved_in_another_case", "at": now,
                })
            sessions[key] = row
            self._write_unlocked(payload)
        return row

    def advance_review(
        self, *, participant_id: str, run_id: str, view: str | None, stage: str,
        step_id: str | None = None, answers: Any = None,
    ) -> dict[str, Any]:
        """Validate and record one current step in one atomic transaction."""
        participant_id = normalize_participant_id(participant_id)
        with self._locked():
            payload = self._read_unlocked()
            session = payload["expert_study"]["sessions"].get(
                self._session_key(participant_id, run_id)
            )
            if not session:
                raise ValueError("Start the expert-study experience before continuing")
            flow = session.get("review_flow")
            if not flow or flow.get("current_view") != view or flow.get("stage") != stage:
                raise ValueError("The review step has changed; reload to resume the current step")
            modern = flow.get("schema") == FLOW_SCHEMA
            if modern and (not isinstance(step_id, str) or flow.get("step_id") != step_id):
                raise ValueError("The review step has changed; reload to resume the current step")
            if stage != "background" and not session.get("analysis_completed_at"):
                raise ValueError("Complete automatic analysis before continuing the review")
            now = datetime.now(UTC).isoformat()
            if modern:
                if stage == "complete":
                    raise ValueError("This review is already complete")
                validated = validate_answers(stage, answers)
                if validated is not None:
                    if step_id in flow["responses"]:
                        raise ValueError("This questionnaire has already been submitted")
                    record = response_record(
                        flow, validated, participant_id=participant_id, run_id=run_id, now=now,
                    )
                    profile = payload["expert_study"]["participants"].setdefault(
                        participant_id, {"participant_id": participant_id}
                    )
                    if stage in {"background", "preference"}:
                        if stage in profile:
                            raise ValueError(
                                f"The {stage} questionnaire has already been submitted"
                            )
                        profile[stage] = record
                    flow["responses"][step_id] = record
                flow["cursor"] += 1
                flow.update(flow["steps"][flow["cursor"]])
                if flow["stage"] == "complete":
                    flow["completed_at"] = now
                    session["case_completed_at"] = session.get("case_completed_at") or now
            elif view not in {"event-based", "summary"} or stage not in {"review", "questionnaire"}:
                raise ValueError("Invalid review step")
            elif answers is not None:
                raise ValueError("This legacy sequence does not collect questionnaire answers")
            elif stage == "review":
                flow["stage"] = "questionnaire"
            else:
                flow["questionnaires"][view] = {
                    "status": "placeholder_continued",
                    "continued_at": now,
                    "answers": None,
                }
                flow["index"] += 1
                if flow["index"] < len(flow["order"]):
                    flow["stage"] = "review"
                    flow["current_view"] = flow["order"][flow["index"]]
                else:
                    flow.update(stage="complete", current_view=None, completed_at=now)
                    # Keep legacy completion/response history when introducing this flow.
                    session["case_completed_at"] = session.get("case_completed_at") or now
            flow["transitions"].append(
                {
                    "view": view, "from_stage": stage, "to_stage": flow["stage"], "at": now,
                    **({"from_step_id": step_id, "to_step_id": flow["step_id"]} if modern else {}),
                }
            )
            self._write_unlocked(payload)
        return flow

    def add_event(
        self,
        *,
        participant_id: str,
        run_id: str,
        event_type: str,
        stimulus_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        participant_id = normalize_participant_id(participant_id)
        if event_type not in EXPERT_STUDY_EVENT_TYPES:
            raise ValueError(f"Unsupported expert-study event: {event_type}")
        encoded = json.dumps(dict(details or {}), ensure_ascii=False)
        if len(encoded) > 4000:
            raise ValueError("Expert-study event details are too large")
        row = {
            "event_id": str(uuid4()),
            "participant_id": participant_id,
            "run_id": run_id,
            "stimulus_id": stimulus_id,
            "event_type": event_type,
            "interface_version": self.interface_version,
            "details": dict(details or {}),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        with self._locked():
            payload = self._read_unlocked()
            payload["expert_study"]["events"].append(row)
            if event_type == "analysis_completed":
                session = payload["expert_study"]["sessions"].get(
                    self._session_key(participant_id, run_id)
                )
                if session is not None:
                    session["analysis_completed_at"] = row["recorded_at"]
            self._write_unlocked(payload)
        return row

    def moments(self, participant_id: str, run_id: str) -> list[dict[str, Any]]:
        participant_id = normalize_participant_id(participant_id)
        with self._locked():
            values = self._read_unlocked()["expert_study"]["expert_proposed_moments"].values()
            return sorted(
                [
                    dict(row)
                    for row in values
                    if row.get("participant_id") == participant_id and row.get("run_id") == run_id
                ],
                key=lambda row: (
                    float(row.get("t_video_s") or 0.0),
                    str(row.get("created_at") or ""),
                ),
            )

    def add_moment(
        self,
        *,
        participant_id: str,
        run_id: str,
        t_video_s: float,
        category: str,
        observation: str,
        coaching_message: str,
        system_coverage: str,
        importance: str,
        audio_bytes: bytes | None = None,
        audio_mime_type: str | None = None,
        audio_duration_s: float | None = None,
    ) -> dict[str, Any]:
        participant_id = normalize_participant_id(participant_id)
        if t_video_s < 0:
            raise ValueError("Coaching-moment time must be non-negative")
        if category not in {"exposure", "safety", "visualization", "efficiency", "other"}:
            raise ValueError("Select a supported coaching-moment category")
        if system_coverage not in {"missed", "partial", "addressed", "unsure"}:
            raise ValueError("Select how the automated feedback covered this moment")
        if importance not in {"low", "medium", "high"}:
            raise ValueError("Select the importance of this coaching moment")
        observation = observation.strip()[:4000]
        coaching_message = coaching_message.strip()[:4000]
        if not observation and not coaching_message and not audio_bytes:
            raise ValueError("Type an observation, coaching message, or record audio")
        if audio_duration_s is not None and not 0 <= audio_duration_s <= 300:
            raise ValueError("Audio recording must be five minutes or shorter")
        if audio_bytes is not None and len(audio_bytes) > EXPERT_AUDIO_MAX_BYTES:
            raise ValueError("Audio recording is larger than 8 MiB")

        normalized_mime = (audio_mime_type or "").split(";", 1)[0].strip().lower()
        if audio_bytes is not None and normalized_mime not in EXPERT_AUDIO_MIME_TYPES:
            raise ValueError("Unsupported audio recording format")

        moment_id = str(uuid4())
        audio: dict[str, Any] | None = None
        final_audio: Path | None = None
        if audio_bytes is not None:
            recordings = self.path.parent / "recordings"
            recordings.mkdir(parents=True, exist_ok=True)
            extension = EXPERT_AUDIO_MIME_TYPES[normalized_mime]
            final_audio = recordings / f"{moment_id}{extension}"
            with tempfile.NamedTemporaryFile(
                "wb",
                delete=False,
                dir=recordings,
                prefix="recording-",
                suffix=".tmp",
            ) as handle:
                handle.write(audio_bytes)
                temporary_audio = Path(handle.name)
            temporary_audio.replace(final_audio)
            audio = {
                "file_name": final_audio.name,
                "mime_type": normalized_mime,
                "duration_s": round(float(audio_duration_s or 0.0), 3),
                "size_bytes": len(audio_bytes),
            }

        row = {
            "moment_id": moment_id,
            "participant_id": participant_id,
            "run_id": run_id,
            "t_video_s": round(float(t_video_s), 3),
            "category": category,
            "observation": observation,
            "coaching_message": coaching_message,
            "system_coverage": system_coverage,
            "importance": importance,
            "audio": audio,
            "interface_version": self.interface_version,
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            with self._locked():
                payload = self._read_unlocked()
                payload["expert_study"]["expert_proposed_moments"][moment_id] = row
                self._write_unlocked(payload)
        except Exception:
            if final_audio is not None:
                final_audio.unlink(missing_ok=True)
            raise
        return row

    def delete_moment(self, participant_id: str, run_id: str, moment_id: str) -> dict[str, Any]:
        participant_id = normalize_participant_id(participant_id)
        with self._locked():
            payload = self._read_unlocked()
            moments = payload["expert_study"]["expert_proposed_moments"]
            row = dict(moments.get(moment_id) or {})
            if (
                not row
                or row.get("participant_id") != participant_id
                or row.get("run_id") != run_id
            ):
                raise KeyError(moment_id)
            del moments[moment_id]
            self._write_unlocked(payload)
        filename = Path(str((row.get("audio") or {}).get("file_name") or "")).name
        if filename:
            (self.path.parent / "recordings" / filename).unlink(missing_ok=True)
        return row

    def audio(self, participant_id: str, run_id: str, moment_id: str) -> tuple[Path, str]:
        participant_id = normalize_participant_id(participant_id)
        with self._locked():
            row = dict(
                self._read_unlocked()["expert_study"]["expert_proposed_moments"].get(moment_id)
                or {}
            )
        if not row or row.get("participant_id") != participant_id or row.get("run_id") != run_id:
            raise KeyError(moment_id)
        audio = row.get("audio") or {}
        filename = Path(str(audio.get("file_name") or "")).name
        path = self.path.parent / "recordings" / filename
        if not filename or not path.is_file():
            raise KeyError(moment_id)
        return path, str(audio.get("mime_type") or "application/octet-stream")

    def complete(
        self,
        *,
        participant_id: str,
        run_id: str,
        answers: Mapping[str, Any],
    ) -> dict[str, Any]:
        participant_id = normalize_participant_id(participant_id)
        key = self._session_key(participant_id, run_id)
        now = datetime.now(UTC).isoformat()
        with self._locked():
            payload = self._read_unlocked()
            sessions = payload["expert_study"]["sessions"]
            session = dict(sessions.get(key) or {})
            if not session:
                raise ValueError("Start the expert-study experience before completing it")
            session["experience_response"] = {
                "answers": dict(answers),
                "submitted_at": now,
            }
            session["case_completed_at"] = now
            session["interface_version_at_completion"] = self.interface_version
            sessions[key] = session
            self._write_unlocked(payload)
        return session


class FeedbackService:
    def __init__(
        self,
        *,
        mode: str,
        db_path: Path,
        run_id: str,
        video_path: Path | None,
        video_manifest_path: Path | None,
        ray_v3_dir: Path | None,
        cohort_benchmark_path: Path | None,
        available_runs: Sequence[Mapping[str, str]],
        display_name: str,
        interface_version: str,
        study_state_path: Path | None = None,
        phase_reference_path: Path | None = None,
    ) -> None:
        if mode not in APP_MODES:
            raise ValueError(f"Unsupported app mode: {mode}")
        if mode == "recommendations" and study_state_path is not None:
            raise ValueError("Recommendations mode cannot be given a study-state path")
        if mode == "expert-study" and study_state_path is None:
            raise ValueError("Expert-study mode requires a study-state path")
        self.mode = mode
        self.retriever = EvidenceRetriever(db_path)
        self.run_id = run_id
        self.video_path = video_path
        self.video_manifest_path = video_manifest_path
        self.ray_v3_dir = ray_v3_dir
        self.artifact_root = db_path.expanduser().resolve().parent
        self._binding_hash_cache: dict[tuple, str] = {}
        self._review_reader_cache: tuple | None = None
        self._review_png_cache: dict[tuple, bytes] = {}
        self._review_images: dict[str, bytes] = {}
        self._review_lock = threading.RLock()
        self._motion_cache: tuple | None = None
        self.cohort_benchmark_path = cohort_benchmark_path
        self.phase_reference_path = phase_reference_path
        self.available_runs = [dict(row) for row in available_runs]
        self.display_name = display_name
        self.interface_version = interface_version
        self.study_store = (
            ExpertStudyStore(study_state_path, interface_version)
            if study_state_path is not None
            else None
        )

    def media_summary(self) -> dict[str, Any]:
        available = bool(self.video_path and self.video_path.is_file())
        manifest: dict[str, Any] = {}
        if available and self.video_manifest_path and self.video_manifest_path.is_file():
            manifest = json.loads(self.video_manifest_path.read_text(encoding="utf-8"))
        return {
            "available": available,
            "url": "/api/media/video" if available else None,
            "mime_type": manifest.get("browser_mime_type", "video/webm"),
            "duration_s": manifest.get("duration_s"),
            "fps": manifest.get("fps"),
            "color_order_validated": (manifest.get("validation") or {}).get(
                "color_order_check_passed"
            ),
        }

    def bootstrap(self) -> dict[str, Any]:
        summary = self.retriever.get_run_summary(self.run_id)
        events = self.retriever.get_events(self.run_id, limit=500)["events"]
        phases = summary["phases"]
        media = self.media_summary()
        duration = max(
            max((float(row["t_end_s"]) for row in phases), default=0.0),
            max((float(row["t_end_s"]) for row in events), default=0.0),
            float(media.get("duration_s") or 0.0),
        )
        ray_v3, ray_source = self._current_visibility(summary["run"])
        benchmark = load_cohort_benchmark(self.cohort_benchmark_path)
        motion = None
        if benchmark and benchmark.get("schema") == "motion_expert_reference_v1":
            motion = self._current_motion_comparison(summary["run"], benchmark)
            benchmark = None
        coaching_plan = build_session_coaching_plan(
            events,
            phases,
            ray_v3,
            benchmark,
            **({"motion_comparison": motion} if motion is not None else {}),
        )
        self._enrich_review_keyframes(coaching_plan, summary["run"])
        return {
            "schema": "automatic_recommendations_v1",
            "run": {
                "run_id": self.run_id,
                "display_name": self.display_name,
                "duration_s": duration,
                "counts": summary["counts"],
                "event_counts": summary["event_counts"],
                "evidence_status_counts": summary["evidence_status_counts"],
            },
            "available_runs": self.available_runs,
            "phases": phases,
            "events": events,
            "coaching_plan": coaching_plan,
            "motion_comparison": motion,
            "detection_status": {
                "under_ledge": {**ray_v3["detection_status"], "source": ray_source},
            },
            "under_ledge_screening": ray_v3,
            "review_candidates": ray_v3["review_candidates"],
            "media": media,
        }

    def _current_visibility(self, run: Mapping[str, Any]) -> tuple[dict, str]:
        """Share the existing verified detector across both read-only review pages."""
        ray_dir = self.ray_v3_dir
        ray_source = "explicit" if ray_dir is not None else "not_configured"
        if ray_dir is None:
            candidate = (self.artifact_root / "stages" / "08_under_ledge_ray").resolve()
            if candidate.is_relative_to(self.artifact_root) and candidate.is_dir():
                ray_dir = candidate
                ray_source = "generated_sibling"
        ray_v3 = build_ray_v3_view(self.run_id, ray_dir)
        if ray_v3.get("available"):
            try:
                strict_binding = self._validate_ray_binding(ray_v3, run)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                ray_v3 = build_ray_v3_view(self.run_id, None)
                ray_v3["reason"] = "strict_ray_binding_mismatch"
                ray_v3["binding_validation"] = {"status": "rejected", "reason": str(exc)}
                ray_v3["detection_status"].update(
                    status="unavailable",
                    reason="strict_ray_binding_mismatch",
                    message=(
                        "Exposure screening is unavailable because its evidence "
                        "does not match this recording."
                    ),
                )
            else:
                if strict_binding:
                    ray_v3["binding_validation"] = {
                        "status": "verified",
                        "authority": "database_source_manifest_hash",
                    }
        return ray_v3, ray_source

    def _summary_safety_available(self, run: Mapping, context: Mapping) -> bool:
        """Require the completed, source-bound geometry/event build before reporting zero."""
        authority = context["audit"].get("geometry_provenance") or {}
        historical = (
            authority.get("status") == "verified_R4_v3_sources"
            and authority.get("real_R4_validated") is True
        )
        current_model = (
            authority.get("status") == "verified_anatomy_model_sources"
            and authority.get("model_parameter_sources_verified") is True
            and authority.get("real_R4_validated") is False
            and authority.get("recording_geometry_verified") is False
            and authority.get("geometry_quality") == "unverified_new_recording"
        )
        if not (historical or current_model):
            return False
        stages = context["stages"]
        edt = json.loads((stages / "02_edt_enrichment/edt_manifest.json").read_text("utf-8"))
        core = json.loads(context["core_path"].read_text("utf-8"))
        if (
            edt.get("geometry_provenance") != authority
            or core.get("geometry_provenance") != authority
            or edt.get("global_run_id") != run["global_run_id"]
            or edt.get("ready_for_visibility_enrichment") is not True
        ):
            return False
        with self.retriever._connect() as connection:
            source = connection.execute(
                "SELECT path,sha256 FROM generated_artifacts "
                "WHERE global_run_id=? AND artifact_type='event_inventory'",
                (run["global_run_id"],),
            ).fetchone()
        expected = (stages / "06_events/events.jsonl").resolve()
        return bool(
            source
            and Path(source["path"]).resolve() == expected
            and self._binding_sha256(expected) == source["sha256"]
        )

    def phase_summary(self, participant_id: str | None = None) -> dict[str, Any]:
        """A whole-phase read-only view; no expert-source reads or new coaching rules."""
        from .phase_summary import (
            METRICS,
            build_phase_motion,
            compare_phase_motion,
            load_phase_reference,
        )
        from .phase_summary_safety import build_phase_safety

        study = None
        if self.mode == "expert-study":
            participant_id = normalize_participant_id(participant_id or "")
            session = self._study().progress(participant_id, self.run_id)
            if not session.get("analysis_completed_at"):
                raise ValueError("Start the study and complete automatic analysis before review")
            study = {
                "participant_id": participant_id,
                "first_review_view": session.get("first_review_view", "event-based"),
            }
        summary = self.retriever.get_run_summary(self.run_id)
        run = summary["run"]
        labels = {"early_surface": "Early drilling", "antrum": "Antrum", "incus": "Incus"}
        aliases = {"speed_mm_s": "tool_speed_mm_s", "mean_stroke_length_mm": "stroke_length_mm"}
        phases = []
        for name, label in labels.items():
            intervals = [
                {key: row[key] for key in ("phase_id", "t_start_s", "t_end_s")}
                for row in summary["phases"]
                if row["phase_name"] == name
            ]
            phases.append(
                {
                    "phase_name": name,
                    "label": label,
                    "intervals": intervals,
                    "duration_s": sum(row["t_end_s"] - row["t_start_s"] for row in intervals),
                    "metrics": [
                        {
                            "key": aliases.get(key, key), "label": title, "unit": unit,
                            "value": None, "available": False, "reason": "source_unavailable",
                            "reference": {"available": False, "points": []},
                        }
                        for key, (title, unit) in METRICS.items()
                    ],
                }
            )
        diagnostics: dict[str, Any] = {}
        reference = None
        if self.phase_reference_path is not None:
            try:
                reference = load_phase_reference(self.phase_reference_path)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                diagnostics["reference_error"] = str(exc)
        frames, context = [], None
        comparison = None
        try:
            context = self._bound_review_context(run)
            frames, provenance = self._motion_source_frames(run, context)
            provenance["recording_id"] = self.run_id
            current = build_phase_motion(
                frames,
                global_run_id=run["global_run_id"],
                participant_id=run.get("participant_id") or None,
                provenance=provenance,
            )
            comparison = compare_phase_motion(current, reference)
            indexed = {row["phase_name"]: row for row in comparison["phases"]}
            for phase in phases:
                result = indexed.get(phase["phase_name"])
                if result is not None:
                    phase.update(
                        intervals=result["intervals"],
                        duration_s=result["duration_s"],
                        coverage_fraction=result["coverage_fraction"],
                        metrics=[
                            {**item, "key": aliases.get(key, key)}
                            for key, item in result["metrics"].items()
                        ],
                    )
            diagnostics["motion_input_sha256"] = current["input_rows_sha256"]
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            diagnostics["motion_error"] = str(exc)
        safety_available = False
        if context is not None:
            try:
                safety_available = self._summary_safety_available(run, context)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                diagnostics["safety_error"] = str(exc)
        # Summary totals must include all canonical events, without the coaching API's limit.
        with self.retriever._connect() as connection:
            records = connection.execute(
                "SELECT e.*,p.phase_name FROM events e "
                "LEFT JOIN phases p ON p.phase_id=e.phase_id WHERE e.global_run_id=? "
                "ORDER BY e.t_start_s,e.event_id",
                (run["global_run_id"],),
            ).fetchall()
            events = [self.retriever._event_row(connection, row) for row in records]
        visibility, _ = self._current_visibility(run)
        if (visibility.get("binding_validation") or {}).get("status") != "verified":
            visibility = {"available": False}
        safety = build_phase_safety(
            phases, events, visibility, frames=frames, safety_available=safety_available
        )
        for phase in phases:
            phase["safety"] = safety[phase["phase_name"]]
        media = self.media_summary()
        return {
            "schema": "phase_summary_v1",
            "study": study,
            "run": {
                "run_id": self.run_id, "display_name": self.display_name,
                "duration_s": max(
                    float(media.get("duration_s") or 0),
                    max((row["t_end_s"] for row in summary["phases"]), default=0),
                ),
            },
            "available_runs": self.available_runs,
            "media": media,
            "phases": phases,
            "reference": {
                "available": reference is not None,
                "sha256": reference.get("artifact_sha256") if reference else None,
                **(comparison["reference_summary"] if comparison is not None else {}),
                "safety_available": False,
            },
            "diagnostics": diagnostics,
        }

    def _binding_sha256(self, path: Path) -> str:
        """Hash known artifacts once per stable file identity during serving."""
        path = path.resolve()
        stat = path.stat()
        key = self._file_identity(path, stat)
        if key not in self._binding_hash_cache:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1 << 20):
                    digest.update(block)
            after = path.stat()
            if key != self._file_identity(path, after):
                raise ValueError("A bound artifact changed while it was being checked")
            self._binding_hash_cache[key] = digest.hexdigest()
        return self._binding_hash_cache[key]

    @staticmethod
    def _file_identity(path: Path, stat=None) -> tuple:
        stat = stat or path.stat()
        return (
            str(path.resolve()),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    def _bound_review_context(self, run: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve authority from the current database, never a request path."""
        with self.retriever._connect() as connection:
            artifact = connection.execute(
                "SELECT path, sha256 FROM generated_artifacts "
                "WHERE global_run_id=? AND artifact_type='stroke_build'",
                (run["global_run_id"],),
            ).fetchone()
            phase_source = connection.execute(
                "SELECT path, sha256 FROM source_files "
                "WHERE global_run_id=? AND source_type='model_phase_prediction'",
                (run["global_run_id"],),
            ).fetchone()
        if artifact is None or phase_source is None or not run.get("source_manifest_hash"):
            raise ValueError("The archive has no bound model-phase source/clock receipt")
        artifact_path = Path(artifact["path"]).resolve()
        if not artifact_path.is_relative_to(self.artifact_root):
            raise ValueError("Frame artifacts are outside the current database root")
        if self._binding_sha256(artifact_path) != artifact["sha256"]:
            raise ValueError("The database-bound stroke artifact changed")
        stages = artifact_path.parent.parent
        audit_path = stages / "00_source_audit/source_manifest.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        canonical = json.dumps(
            {k: v for k, v in audit.items() if k != "generated_at"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if (
            hashlib.sha256(canonical).hexdigest() != run["source_manifest_hash"]
            or audit.get("global_run_id") != run["global_run_id"]
        ):
            raise ValueError("Source audit differs from the database authority")
        phase_path = Path(phase_source["path"]).resolve()
        if not phase_path.is_relative_to(self.artifact_root):
            raise ValueError("Phase authority is outside the current database root")
        if self._binding_sha256(phase_path) != phase_source["sha256"]:
            raise ValueError("The database-bound phase manifest changed")
        phase = json.loads(phase_path.read_text(encoding="utf-8"))
        clock = phase.get("clock") or {}
        if (
            phase.get("global_run_id") != run["global_run_id"]
            or clock.get("configured_global_run_id") != run["global_run_id"]
        ):
            raise ValueError("Phase/clock authority belongs to another recording")
        core_path = stages / "01_core_frames/extraction_manifest.json"
        core = json.loads(core_path.read_text(encoding="utf-8"))
        if (
            core.get("global_run_id") != run["global_run_id"]
            or core.get("clock_alignment") != clock
        ):
            raise ValueError("Core clock differs from the database-bound phase clock")
        raw_value = (audit.get("source_classification") or {}).get("primary_raw")
        if not raw_value:
            raise ValueError("The original recording directory is not bound")
        raw = Path(raw_value).resolve()
        inventory = {
            row["file_name"]: dict(row) for row in (audit.get("hdf5") or {}).get("chunks", [])
        }
        alignment_rows = None
        if clock.get("source") in {"explicit_frame_alignment", "generated_hdf5_native_alignment"}:
            alignment = Path(clock["path"]).resolve()
            if self._binding_sha256(alignment) != clock.get("sha256"):
                raise ValueError("The bound frame alignment changed")
            with alignment.open(encoding="utf-8-sig", newline="") as stream:
                alignment_rows = list(csv.DictReader(stream))
            if len(alignment_rows) != clock.get("frame_count"):
                raise ValueError("Frame alignment inventory differs")
            previous = -math.inf
            for index, row in enumerate(alignment_rows):
                value = float(row["video_time_s"])
                if (
                    int(row["frame_idx_global"]) != index
                    or not math.isfinite(value)
                    or value <= previous
                ):
                    raise ValueError("Frame alignment order is invalid")
                previous = value
        elif clock.get("source") != "native_relative_time":
            raise ValueError("Unsupported original-frame clock")
        if clock.get("source") == "generated_hdf5_native_alignment":
            from .hdf5_clock import MANIFEST_NAME, SCHEMA, SOURCE

            manifest_path = Path(clock["generated_manifest_path"]).resolve()
            if manifest_path != Path(
                clock["path"]
            ).resolve().parent / MANIFEST_NAME or self._binding_sha256(manifest_path) != clock.get(
                "generated_manifest_sha256"
            ):
                raise ValueError("Generated-clock receipt changed")
            generated = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = generated.get("source_inventory")
            inventory_hash = hashlib.sha256(
                json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                generated.get("schema") != SCHEMA
                or generated.get("source") != SOURCE
                or generated.get("alignment_sha256") != clock.get("sha256")
                or generated.get("frame_count") != clock.get("frame_count")
                or generated.get("alignment_file") != Path(clock["path"]).name
                or inventory_hash != generated.get("source_inventory_sha256")
                or inventory_hash != clock.get("source_inventory_sha256")
                or Path(generated["raw_run_dir"]).resolve() != raw
                or Path(clock["source_raw_run_dir"]).resolve() != raw
                or clock.get("native_correspondence_verified") is not True
            ):
                raise ValueError("Generated-clock source inventory is not bound")
            inventory = {row["file_name"]: dict(row) for row in rows}
        return {
            "run": dict(run),
            "stages": stages,
            "audit": audit,
            "phase": phase,
            "phase_path": phase_path,
            "phase_sha256": phase_source["sha256"],
            "core_path": core_path,
            "clock": clock,
            "raw": raw,
            "inventory": inventory,
            "alignment_rows": alignment_rows,
        }

    def _decode_review_frame(
        self, context: Mapping[str, Any], index: int, anchor: float
    ) -> tuple[bytes, float]:
        import cv2
        import numpy as np

        from .hdf5_reader import RecordingReader, list_hdf5_paths

        with self.retriever._connect() as connection:
            row = connection.execute(
                "SELECT f.*, s.path AS source_path, s.sha256 AS source_sha256, s.source_type "
                "FROM frames f JOIN source_files s ON f.source_file_id=s.source_file_id "
                "WHERE f.global_run_id=? AND f.frame_idx=?",
                (context["run"]["global_run_id"], index),
            ).fetchone()
        if row is None or row["source_type"] != "raw_hdf5":
            raise ValueError("The anchored source frame is unavailable")
        observation = json.loads(row["observation_json"])
        timestamp = float(row["t_video_s"])
        if (
            not math.isfinite(timestamp)
            or not math.isclose(timestamp, anchor, abs_tol=1e-6, rel_tol=0)
            or int(observation["frame_idx_global"]) != index
            or float(observation["t_video_s"]) != timestamp
        ):
            raise ValueError("The anchor time and database frame do not match")
        path = Path(row["source_path"]).resolve()
        if path.parent != context["raw"] or path.name != observation.get("source_hdf5"):
            raise ValueError("The anchored source path differs from the bound recording")
        declaration = context["inventory"].get(path.name)
        if not declaration:
            raise ValueError("The source chunk is absent from the bound inventory")
        expected_hash = row["source_sha256"] or declaration.get("sha256")
        if not expected_hash or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("Original source content has no bound checksum")
        if (
            row["source_sha256"]
            and declaration.get("sha256")
            and row["source_sha256"] != declaration["sha256"]
        ):
            raise ValueError("Source checksums disagree")
        if self._binding_sha256(path) != expected_hash:
            raise ValueError("The original source chunk changed")
        local_index = int(observation["source_frame_idx"])
        if local_index < 0 or row["source_row"] != local_index:
            raise ValueError("The original frame index is not bound")
        aligned = context["alignment_rows"]
        if aligned is not None:
            mapped = aligned[index]
            if float(mapped["video_time_s"]) != timestamp:
                raise ValueError("The anchor differs from the bound video clock")
            for field in ("source_hdf5", "source_frame_idx", "t_abs"):
                if field in mapped:
                    expected = observation[field]
                    if field == "source_hdf5":
                        matched = mapped[field] == expected
                    else:
                        matched = float(mapped[field]) == float(expected)
                    if not matched:
                        raise ValueError("Generated clock and native source identity disagree")
        elif not math.isclose(
            float(observation["t_abs"]) - float(context["clock"]["raw_time_origin_abs"]),
            timestamp,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("The native-relative source clock differs")
        if {p.name for p in list_hdf5_paths(context["raw"])} != set(context["inventory"]):
            raise ValueError("The native recording inventory changed")
        identities = tuple(
            self._file_identity(context["raw"] / name) for name in sorted(context["inventory"])
        )
        reader_key = (str(context["raw"]), identities)
        if self._review_reader_cache is None or self._review_reader_cache[0] != reader_key:
            reader = RecordingReader(context["raw"])
            if {p.name for p in reader.paths} != set(context["inventory"]):
                raise ValueError("The native recording inventory changed")
            self._review_reader_cache = (reader_key, reader)
        reader = self._review_reader_cache[1]
        cache_key = (expected_hash, self._file_identity(path), local_index)
        if cache_key not in self._review_png_cache:
            with reader.open_chunk(path) as handle:
                times, images = handle.get("data/time"), handle.get("data/l_img")
                if (
                    times is None
                    or images is None
                    or local_index >= len(times)
                    or local_index >= len(images)
                ):
                    raise ValueError("The requested native image does not exist")
                if float(times[local_index]) != float(observation["t_abs"]):
                    raise ValueError("Native image timestamp differs from the bound observation")
                bgr = np.asarray(images[local_index])
                if bgr.dtype != np.uint8 or bgr.ndim != 3 or bgr.shape[2] != 3:
                    raise ValueError("Unsupported original-image shape or type")
                # OpenCV encodes BGR into a browser-standard RGB PNG correctly.
                try:
                    success, encoded = cv2.imencode(".png", bgr)
                except cv2.error as exc:
                    raise ValueError("Original image encoding failed") from exc
                if not success:
                    raise ValueError("Original image encoding failed")
            if cache_key[1] != self._file_identity(path):
                raise ValueError("The original source changed while decoding")
            if len(self._review_png_cache) >= 32:
                self._review_png_cache.clear()
            self._review_png_cache[cache_key] = encoded.tobytes()
        return self._review_png_cache[cache_key], timestamp

    def _enrich_review_keyframes(self, plan: Mapping[str, Any], run: Mapping[str, Any]) -> None:
        with self._review_lock:
            authorized = {}
            context, context_error = None, None
            for item in plan.get("items", []):
                for ordinal, clip in enumerate(item.get("review_clips") or []):
                    clip["keyframe_available"] = False
                    clip.pop("keyframe_url", None)
                    anchor = clip.get("anchor_t_s")
                    index = clip.get("anchor_frame_idx_global")
                    if index is None or anchor is None:
                        clip["keyframe_reason"] = "no_bound_native_anchor"
                        continue
                    try:
                        if (
                            isinstance(index, bool)
                            or int(index) != float(index)
                            or not math.isfinite(float(anchor))
                        ):
                            raise ValueError("Invalid native anchor")
                        if not float(clip["t_start_s"]) <= float(anchor) <= float(clip["t_end_s"]):
                            raise ValueError("The anchor is outside its review interval")
                        if context_error:
                            raise ValueError(context_error)
                        if context is None:
                            try:
                                context = self._bound_review_context(run)
                            except (OSError, ValueError, KeyError, TypeError) as exc:
                                context_error = str(exc)
                                raise
                        png, timestamp = self._decode_review_frame(
                            context, int(index), float(anchor)
                        )
                        token = hashlib.sha256(
                            json.dumps(
                                [
                                    run["global_run_id"],
                                    item.get("feedback_id"),
                                    clip.get("review_clip_id"),
                                    ordinal,
                                    int(index),
                                    timestamp,
                                ],
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        authorized[token] = png
                        clip.update(
                            keyframe_available=True,
                            keyframe_time_s=timestamp,
                            keyframe_frame_idx_global=int(index),
                            keyframe_url="/api/media/keyframe?" + urlencode({"clip_id": token}),
                        )
                        clip.pop("keyframe_reason", None)
                    except (ImportError, OSError, ValueError, KeyError, TypeError, IndexError):
                        clip["keyframe_reason"] = "original_source_unavailable_or_unbound"
            self._review_images = authorized

    def original_review_frame(self, clip_id: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", clip_id):
            raise ValueError("Unknown review frame")
        # Revalidate current coaching membership and changed-file identities.
        with self._review_lock:
            self.bootstrap()
            if clip_id not in self._review_images:
                raise ValueError("Original review frame is unavailable")
            return self._review_images[clip_id]

    def _motion_source_frames(
        self, run: Mapping[str, Any], context: Mapping[str, Any]
    ) -> tuple[list[dict], dict]:
        """Read canonical recorded signals with the existing DB/clock identity checks."""
        with self.retriever._connect() as connection:
            records = connection.execute(
                "SELECT frame_idx,t_video_s,phase_id,observation_json FROM frames "
                "WHERE global_run_id=? ORDER BY frame_idx",
                (run["global_run_id"],),
            ).fetchall()
        frames = []
        for index, record in enumerate(records):
            row = json.loads(record["observation_json"])
            if (
                record["frame_idx"] != index
                or row.get("frame_idx_global") != index
                or row.get("t_video_s") != record["t_video_s"]
                or row.get("phase_id") != record["phase_id"]
            ):
                raise ValueError("Current database observation identity differs")
            frames.append(row)
        if len(frames) != context["clock"].get("frame_count"):
            raise ValueError("Motion frame count differs from the bound recording clock")
        provenance = {
            "pose_source": "canonical_vision_pose",
            "position_unit": "m",
            "phase_source_kind": "model_phase_prediction",
            "phase_model_sha256": context["phase"].get("checkpoint_sha256"),
            "clock_source": context["clock"]["source"],
            "source_artifacts": [
                {
                    "kind": "frames",
                    "path": str(self.retriever.db_path.resolve()),
                    "sha256": self._binding_sha256(self.retriever.db_path),
                },
                {
                    "kind": "phases",
                    "path": str(context["phase_path"]),
                    "sha256": context["phase_sha256"],
                },
                {
                    "kind": "clock",
                    "path": str(context["core_path"]),
                    "sha256": self._binding_sha256(context["core_path"]),
                },
            ],
        }
        return frames, provenance

    def _current_motion_comparison(
        self, run: Mapping[str, Any], reference: Mapping[str, Any]
    ) -> dict:
        """Compare current DB observations; an archive never borrows observed reference values."""
        from .motion_comparison import (
            add_stroke_length,
            artifact_sha256,
            build_motion_windows,
            compare_motion_windows,
            validate_window_build,
        )

        unavailable = {
            "schema": "motion_comparison_v1",
            "available": False,
            "status": "unavailable",
            "reason": "current_motion_source_unbound",
            "global_run_id": run["global_run_id"],
            "windows": [],
            "episodes": [],
            "per_phase": {},
        }
        unavailable["artifact_sha256"] = artifact_sha256(unavailable)
        try:
            context = self._bound_review_context(run)
            stages = context["stages"]
            sidecar = stages / "09_motion_comparison/motion_windows.json"
            expected_sources = {
                "frames": stages / "05_strokes/frame_observations_complete.csv",
                "phases": stages / "01_core_frames/phase_intervals.csv",
                "clock": context["core_path"],
            }
            identities = [
                self._file_identity(self.retriever.db_path),
                self._file_identity(context["phase_path"]),
                self._file_identity(context["core_path"]),
            ]
            if self.cohort_benchmark_path:
                identities.append(self._file_identity(self.cohort_benchmark_path))
            if sidecar.is_file():
                identities.append(self._file_identity(sidecar))
                identities.extend(self._file_identity(path) for path in expected_sources.values())
            cache_key = tuple(identities)
            if self._motion_cache is not None and self._motion_cache[0] == cache_key:
                return self._motion_cache[1]
            frames, provenance = self._motion_source_frames(run, context)
            current = build_motion_windows(
                frames,
                global_run_id=run["global_run_id"],
                participant_id=run.get("participant_id") or None,
                provenance=provenance,
                protocol=reference["protocol"],
            )
            origin = "current_database_observations_archive_fallback"
            length_origin = None
            if sidecar.is_file():
                saved = json.loads(sidecar.read_text(encoding="utf-8"))
                validate_window_build(saved)
                # Verify the archive using its own grid before adapting current
                # observations to the selected reference. Never rewrite it.
                current = build_motion_windows(
                    frames,
                    global_run_id=run["global_run_id"],
                    participant_id=run.get("participant_id") or None,
                    provenance=provenance,
                    protocol=saved["protocol"],
                )
                saved_provenance = saved["provenance"]
                for key in (
                    "pose_source",
                    "position_unit",
                    "phase_source_kind",
                    "phase_model_sha256",
                    "clock_source",
                ):
                    if saved_provenance.get(key) != provenance[key]:
                        raise ValueError(
                            "Saved motion source contract differs from current authority"
                        )
                declarations = saved_provenance["source_artifacts"]
                if len(declarations) != 3 or {s["kind"] for s in declarations} != set(
                    expected_sources
                ):
                    raise ValueError("Saved motion source inventory differs")
                for declaration in declarations:
                    path = expected_sources[declaration["kind"]].resolve()
                    if (
                        Path(declaration["path"]).resolve() != path
                        or self._binding_sha256(path) != declaration["sha256"]
                    ):
                        raise ValueError("Saved motion artifact differs from the current pipeline")
                if saved.get("stroke_length_metadata") is not None:
                    current = add_stroke_length(current, frames)
                    for key in ("stroke_length_metadata", "stroke_lengths"):
                        if saved.get(key) != current.get(key):
                            raise ValueError(
                                "Saved stroke lengths differ from current database observations"
                            )
                    length_origin = "bound_pipeline_sidecar_verified_against_current_database"
                for key in (
                    "global_run_id",
                    "participant_id",
                    "protocol_sha256",
                    "input_rows_sha256",
                    "source_frame_count",
                    "native_time_origin_abs",
                    "native_duration_s",
                    "windows",
                ):
                    if saved.get(key) != current.get(key):
                        raise ValueError(
                            "Saved motion values differ from current database observations"
                        )
                current = saved
                origin = "bound_pipeline_sidecar_verified_against_current_database"
                if saved["protocol"] != reference["protocol"]:
                    current = build_motion_windows(
                        frames,
                        global_run_id=run["global_run_id"],
                        participant_id=run.get("participant_id") or None,
                        provenance=provenance,
                        protocol=reference["protocol"],
                    )
                    origin = "current_database_reference_grid_after_archive_validation"
                    length_origin = None
            length_unavailable = None
            if (
                any(
                    item.get("stroke_length_metadata") is not None
                    for item in reference["recordings"]
                )
                and current.get("stroke_length_metadata") is None
            ):
                try:
                    current = add_stroke_length(current, frames)
                    length_origin = "current_database_complete_strokes"
                except ValueError as error:
                    length_unavailable = str(error)
            result = compare_motion_windows(
                current, reference, participant_id=run.get("participant_id") or None
            )
            for episode in result.get("length_episodes", []):
                anchors = [
                    s
                    for s in current.get("stroke_lengths", [])
                    if s["stroke_id"] in episode["source_stroke_ids"]
                    and episode["native_start_s"] < s["native_end_s"] <= episode["native_end_s"]
                ]
                if anchors:
                    anchor = min(anchors, key=lambda s: s["native_end_s"])
                    index = anchor["source_end_row"]
                    episode["anchor_frame_idx_global"] = index
                    episode["anchor_t_s"] = float(frames[index]["t_video_s"])
                    episode["anchor_role"] = "first_eligible_completed_stroke_in_review_interval"
            result["current_source"] = origin
            if length_unavailable is not None:
                result["stroke_length_unavailable_reason"] = length_unavailable
            if length_origin is not None:
                result["stroke_length_source"] = length_origin
            result["artifact_sha256"] = artifact_sha256(result)
            self._motion_cache = (cache_key, result)
            return result
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return unavailable

    def _validate_ray_binding(self, ray: Mapping[str, Any], run: Mapping[str, Any]) -> bool:
        """Bind strict-build sidecars to DB-anchored evidence, not their run claim."""
        with self.retriever._connect() as connection:
            artifact = connection.execute(
                "SELECT path, sha256 FROM generated_artifacts WHERE global_run_id = ? "
                "AND artifact_type = 'stroke_build'",
                (run["global_run_id"],),
            ).fetchone()
            phase_source = connection.execute(
                "SELECT path, sha256 FROM source_files WHERE global_run_id = ? "
                "AND source_type = 'model_phase_prediction'",
                (run["global_run_id"],),
            ).fetchone()
        stages = self.artifact_root / "stages"
        artifact_path = None
        if artifact is not None and not str(artifact["path"]).startswith("in_memory://"):
            artifact_path = Path(artifact["path"]).resolve()
            if artifact_path.is_relative_to(self.artifact_root):
                stages = artifact_path.parent.parent
        audit_path = stages / "00_source_audit" / "source_manifest.json"
        if not audit_path.is_file():
            if run.get("source_manifest_hash"):
                raise ValueError("The database-bound source audit is unavailable")
            return False  # Small historical/synthetic stores can have no source audit.
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        canonical = json.dumps(
            {key: value for key, value in audit.items() if key != "generated_at"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != run.get("source_manifest_hash"):
            raise ValueError("Source audit does not match the database manifest hash")
        if audit.get("global_run_id") != run["global_run_id"]:
            raise ValueError("Source audit belongs to a different recording")
        authority = audit.get("geometry_provenance") or {}
        strict_historical = (
            authority.get("status") == "verified_R4_v3_sources"
            and authority.get("real_R4_validated") is True
        )
        strict_model = (
            authority.get("status") == "verified_anatomy_model_sources"
            and authority.get("real_R4_validated") is False
            and authority.get("model_parameter_sources_verified") is True
            and authority.get("recording_geometry_verified") is False
            and authority.get("geometry_quality") == "unverified_new_recording"
        )
        if authority.get("status") == "verified_anatomy_model_sources" and not strict_model:
            raise ValueError("New model-geometry authority has inconsistent verification flags")
        if not (strict_historical or strict_model):
            return False  # Preserve explicit synthetic and historical low-level contracts.
        if artifact_path is None or not artifact_path.is_relative_to(self.artifact_root):
            raise ValueError(
                "Strict source artifacts are not bound inside the database artifact root"
            )
        if self._binding_sha256(artifact_path) != artifact["sha256"]:
            raise ValueError("The database-bound stroke artifact changed")
        provenance = ray.get("provenance") or {}
        if provenance.get("geometry_provenance") != authority:
            raise ValueError("Ray geometry provenance differs from the database-bound R4 receipt")
        core_path = stages / "01_core_frames" / "extraction_manifest.json"
        core = json.loads(core_path.read_text(encoding="utf-8"))
        if (
            core.get("geometry_provenance") != authority
            or core.get("global_run_id") != run["global_run_id"]
        ):
            raise ValueError("Current frame provenance differs from the database-bound R4 receipt")
        clock = core.get("clock_alignment")
        if not isinstance(clock, dict) or provenance.get("clock_alignment") != clock:
            raise ValueError("Ray clock differs from the current frame clock")
        if phase_source is not None:
            phase_path = Path(phase_source["path"]).resolve()
            if not phase_path.is_relative_to(self.artifact_root):
                raise ValueError("The phase clock authority is outside the database artifact root")
            if self._binding_sha256(phase_path) != phase_source["sha256"]:
                raise ValueError("The database-bound phase manifest changed")
            phase = json.loads(phase_path.read_text(encoding="utf-8"))
            if phase.get("clock") != clock:
                raise ValueError("Frame clock differs from the database-bound phase clock")
        if clock.get("source") in {"explicit_frame_alignment", "generated_hdf5_native_alignment"}:
            if not clock.get("path") or self._binding_sha256(Path(clock["path"])) != clock.get(
                "sha256"
            ):
                raise ValueError("The bound frame-alignment source changed")
        if clock.get("source") == "generated_hdf5_native_alignment":
            from .hdf5_clock import MANIFEST_NAME, SCHEMA, SOURCE

            alignment_path = Path(clock["path"]).resolve()
            manifest_value = clock.get("generated_manifest_path")
            if not manifest_value:
                raise ValueError("The generated HDF5 clock manifest is unavailable")
            manifest_path = Path(manifest_value).resolve()
            if manifest_path != alignment_path.parent / MANIFEST_NAME:
                raise ValueError("The generated HDF5 clock manifest has a different location")
            if self._binding_sha256(manifest_path) != clock.get("generated_manifest_sha256"):
                raise ValueError("The bound generated HDF5 clock manifest changed")
            generated = json.loads(manifest_path.read_text(encoding="utf-8"))
            inventory = generated.get("source_inventory")
            inventory_digest = hashlib.sha256(
                json.dumps(
                    inventory,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                generated.get("schema") != SCHEMA
                or generated.get("source") != SOURCE
                or generated.get("alignment_file") != alignment_path.name
                or generated.get("alignment_sha256") != clock.get("sha256")
                or generated.get("frame_count") != clock.get("frame_count")
                or not isinstance(inventory, list)
                or not inventory
                or inventory_digest != generated.get("source_inventory_sha256")
                or inventory_digest != clock.get("source_inventory_sha256")
                or not clock.get("source_raw_run_dir")
                or not generated.get("raw_run_dir")
                or Path(generated["raw_run_dir"]).resolve()
                != Path(clock["source_raw_run_dir"]).resolve()
                or generated.get("training_reference_clock") is not False
                or clock.get("training_reference_clock") is not False
                or clock.get("native_correspondence_verified") is not True
            ):
                raise ValueError("Generated HDF5 clock source binding is inconsistent")
        inputs = provenance.get("input_artifacts") or {}
        expected_paths = {
            "frames": stages / "05_strokes" / "frame_observations_complete.csv",
            "removed_voxels": stages / "02_edt_enrichment" / "removed_voxel_edt.csv",
        }
        for name, expected_path in expected_paths.items():
            declared = inputs.get(name) or {}
            if (
                not declared.get("path")
                or Path(declared["path"]).resolve() != expected_path.resolve()
            ):
                raise ValueError(f"Ray {name} are not the current recording's stage artifact")
            if self._binding_sha256(expected_path) != declared.get("sha256"):
                raise ValueError(f"Ray {name} hash differs from the current stage artifact")
        return True

    def _study(self) -> ExpertStudyStore:
        if self.mode != "expert-study" or self.study_store is None:
            raise RuntimeError("Study-write operations are disabled for this app")
        return self.study_store

    def _study_items(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.bootstrap()["coaching_plan"]["items"]]

    def _case_context(self) -> dict[str, Any]:
        index = next(
            (i for i, row in enumerate(self.available_runs) if row.get("run_id") == self.run_id),
            0,
        )
        next_url = (
            self.available_runs[index + 1].get("url")
            if index + 1 < len(self.available_runs)
            else None
        )
        return {
            "run_id": self.run_id,
            "alias": self.display_name or f"Case {index + 1:02d}",
            "index": index + 1,
            "total": len(self.available_runs),
            "next_case_url": next_url or None,
        }

    @staticmethod
    def _study_clips(item: Mapping[str, Any]) -> list[dict[str, Any]]:
        source = list(item.get("review_clips") or item.get("evidence_segments") or [])
        if not source:
            source = [
                {
                    "t_start_s": item.get("t_start_s"),
                    "t_end_s": item.get("t_end_s"),
                }
            ]
        padding = 3.0 if str(item.get("category") or "") == "exposure" else 5.0
        clips: list[dict[str, Any]] = []
        for row in source:
            start = max(0.0, float(row.get("t_start_s") or 0.0))
            end = max(start, float(row.get("t_end_s") or start))
            anchor = row.get("anchor_t_s")
            clips.append(
                {
                    "t_start_s": start,
                    "t_end_s": end,
                    "playback_start_s": max(0.0, start - padding),
                    "playback_end_s": end + padding,
                    "signal_time_s": (float(anchor) if anchor is not None else (start + end) / 2.0),
                    "exact_signal_frame": anchor is not None,
                    "signal_frame_idx": row.get("anchor_frame_idx_global"),
                    "anatomy": row.get("anatomy") or item.get("anatomy"),
                    "phase": row.get("phase") or item.get("phase"),
                    **{
                        key: row[key]
                        for key in (
                            "phases",
                            "keyframe_available",
                            "keyframe_url",
                            "keyframe_time_s",
                            "keyframe_frame_idx_global",
                            "keyframe_reason",
                        )
                        if key in row
                    },
                }
            )
        return clips

    @staticmethod
    def _method_details(item: Mapping[str, Any]) -> dict[str, str]:
        category = str(item.get("category") or "")
        limitation = str(item.get("claim_boundary") or "")
        if category == "safety":
            return {
                "data_used": "Time-aligned simulator anatomy and drilling activity.",
                "trigger": (
                    "The simulator indicated a possible protected-structure "
                    "boundary signal that warrants visual review."
                ),
                "selection": ("Moments are grouped by anatomy and linked to recorded video."),
                "limitation": limitation,
            }
        if category == "technique":
            return {
                "data_used": (
                    "Phase-level stroke pattern and procedural progress from the "
                    "recorded simulator session."
                ),
                "trigger": (
                    "The observed phase pattern is compared descriptively with "
                    "the configured reference range."
                ),
                "selection": (
                    "Only phase patterns meeting the configured comparison rule "
                    "are promoted to coaching."
                ),
                "limitation": limitation,
            }
        return {
            "data_used": (
                "Time-linked camera view, burr location, and surrounding-bone "
                "geometry from the recorded simulator session."
            ),
            "trigger": (
                "The system looked for moments when surrounding bone may have "
                "limited a direct view of the working point."
            ),
            "selection": ("Nearby signals are combined into short context clips."),
            "limitation": limitation,
        }

    @staticmethod
    def _scale(value: Any, label: str) -> int:
        try:
            rating = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer from 1 to 5") from exc
        if rating not in {1, 2, 3, 4, 5}:
            raise ValueError(f"{label} must be an integer from 1 to 5")
        return rating

    def start_study(
        self, participant_id: str, first_review_view: str = "event-based"
    ) -> dict[str, Any]:
        store = self._study()
        if first_review_view not in {"event-based", "summary"}:
            raise ValueError("First review view must be event-based or summary")
        participant_id = normalize_participant_id(participant_id)
        items = self._study_items()
        case = self._case_context()
        previous = store.progress(participant_id, self.run_id)
        session = store.start_session(
            participant_id=participant_id,
            run_id=self.run_id,
            stimulus_ids=[str(row["feedback_id"]) for row in items],
            first_review_view=first_review_view,
            final_case=case["index"] >= case["total"],
        )
        store.add_event(
            participant_id=participant_id,
            run_id=self.run_id,
            event_type="study_resumed" if previous else "study_started",
            details={"feedback_count": len(items)},
        )
        flow = session["review_flow"]
        legacy = flow.get("schema") != FLOW_SCHEMA
        final_case = case["next_case_url"] is None if legacy else flow["final_case"]
        seed = f"{EXPERT_STUDY_SCHEMA}|{participant_id}"
        return {
            "schema": "expert_workflow_study_start_v1",
            "interface_version": self.interface_version,
            "participant_id": participant_id,
            "run_id": self.run_id,
            "first_review_view": session["first_review_view"],
            "review_flow": public_review_flow(flow),
            "surveys": flow.get("survey_definition", SURVEY_DEFINITION),
            "legacy_survey_flow": legacy,
            "survey_warning": (
                "This saved session uses the previous review sequence. Start with a new "
                "Study ID to complete the new questionnaires and baseline self-assessment."
                if legacy else None
            ),
            "analysis_completed": bool(session.get("analysis_completed_at")),
            "case": case,
            "media": self.media_summary(),
            "case_completed": session["review_flow"]["stage"] == "complete",
            "workflow": {
                "mode": "recorded_session",
                "disclosure": (
                    "This experience uses a previously recorded simulator "
                    "session. Evidence retrieval and feedback assembly run locally."
                ),
                "stages": [
                    "Load recorded simulator session",
                    "Retrieve time-aligned evidence",
                    "Assemble the coaching plan",
                    "Prepare linked video review",
                ],
            },
            "completion_code": (
                "EXP-" + hashlib.sha256(seed.encode()).hexdigest()[:8].upper()
                if final_case and (legacy or flow["stage"] == "complete")
                else None
            ),
            "expert_proposed_moments": self.public_moments(participant_id),
        }

    def analyze_study(self, participant_id: str) -> dict[str, Any]:
        store = self._study()
        participant_id = normalize_participant_id(participant_id)
        if not store.progress(participant_id, self.run_id):
            raise ValueError("Start the expert-study experience before analysis")
        store.add_event(
            participant_id=participant_id,
            run_id=self.run_id,
            event_type="analysis_started",
        )
        bootstrap = self.bootstrap()
        feedback = []
        for item in self._study_items():
            feedback.append(
                {
                    **{
                        key: item.get(key)
                        for key in (
                            "feedback_id",
                            "title",
                            "category",
                            "priority",
                            "message",
                            "observed_issue",
                            "why_it_matters",
                            "recommended_action",
                            "evidence_summary",
                            "evidence_status",
                            "claim_boundary",
                            "phase",
                            "t_start_s",
                            "t_end_s",
                        )
                    },
                    "clips": self._study_clips(item),
                    "method_details": self._method_details(item),
                }
            )
        store.add_event(
            participant_id=participant_id,
            run_id=self.run_id,
            event_type="analysis_completed",
            details={"feedback_count": len(feedback)},
        )
        flow = store.progress(participant_id, self.run_id)["review_flow"]
        return {
            "schema": "expert_workflow_experience_v1",
            "review_flow": public_review_flow(flow),
            "surveys": flow.get("survey_definition", SURVEY_DEFINITION),
            "legacy_survey_flow": flow.get("schema") != FLOW_SCHEMA,
            "interface_version": self.interface_version,
            "case": self._case_context(),
            "session": {
                "duration_s": bootstrap["run"]["duration_s"],
                "phase_count": len(bootstrap["phases"]),
                "feedback_count": len(feedback),
            },
            "phases": [
                {
                    "phase_id": row.get("phase_id"),
                    "phase_name": row.get("phase_name"),
                    "phase_source_kind": row.get("phase_source_kind"),
                    "phase_source_sha256": row.get("phase_source_sha256"),
                    "segment_role": row.get("segment_role"),
                    "t_start_s": row.get("t_start_s"),
                    "t_end_s": row.get("t_end_s"),
                }
                for row in bootstrap["phases"]
            ],
            "feedback": feedback,
            "detection_status": bootstrap["detection_status"],
            "review_candidates": bootstrap["review_candidates"],
            "media": bootstrap["media"],
            "expert_proposed_moments": self.public_moments(participant_id),
        }

    def advance_review(
        self, participant_id: str, view: str | None, stage: str,
        step_id: str | None = None, answers: Any = None,
    ) -> dict[str, Any]:
        participant_id = normalize_participant_id(participant_id)
        store = self._study()
        flow = store.advance_review(
            participant_id=participant_id, run_id=self.run_id, view=view, stage=stage,
            step_id=step_id, answers=answers,
        )
        store.add_event(
            participant_id=participant_id, run_id=self.run_id,
            event_type="review_step_advanced",
            details={"view": view, "from_stage": stage, "to_stage": flow["stage"]},
        )
        complete = flow["stage"] == "complete"
        case = self._case_context()
        final_case = (
            flow["final_case"] if flow.get("schema") == FLOW_SCHEMA
            else case["next_case_url"] is None
        )
        seed = f"{EXPERT_STUDY_SCHEMA}|{participant_id}"
        return {
            "review_flow": public_review_flow(flow),
            "case_completed": complete,
            "next_case_url": case["next_case_url"] if complete else None,
            "completion_code": (
                "EXP-" + hashlib.sha256(seed.encode()).hexdigest()[:8].upper()
                if complete and final_case else None
            ),
        }

    def _public_moment(self, participant_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
        public = {
            key: row.get(key)
            for key in (
                "moment_id",
                "t_video_s",
                "category",
                "observation",
                "coaching_message",
                "system_coverage",
                "importance",
                "interface_version",
                "created_at",
            )
        }
        audio = row.get("audio") or {}
        public["audio"] = (
            {
                "mime_type": audio.get("mime_type"),
                "duration_s": audio.get("duration_s"),
                "size_bytes": audio.get("size_bytes"),
                "url": (
                    "/api/expert-study/coaching-moment-audio"
                    f"?participant_id={participant_id}&moment_id={row.get('moment_id')}"
                ),
            }
            if audio
            else None
        )
        return public

    def public_moments(self, participant_id: str) -> list[dict[str, Any]]:
        return [
            self._public_moment(participant_id, row)
            for row in self._study().moments(participant_id, self.run_id)
        ]

    def add_moment(self, participant_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        store = self._study()
        participant_id = normalize_participant_id(participant_id)
        session = store.progress(participant_id, self.run_id)
        if not session:
            raise ValueError("Start the expert-study experience before adding a moment")
        if session.get("case_completed_at"):
            raise ValueError("This case has already been completed")
        encoded_audio = str(value.get("audio_base64") or "")
        audio_bytes = None
        if encoded_audio:
            try:
                audio_bytes = base64.b64decode(encoded_audio, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("Audio recording could not be decoded") from exc
        row = store.add_moment(
            participant_id=participant_id,
            run_id=self.run_id,
            t_video_s=float(value.get("t_video_s") or 0.0),
            category=str(value.get("category") or ""),
            observation=str(value.get("observation") or ""),
            coaching_message=str(value.get("coaching_message") or ""),
            system_coverage=str(value.get("system_coverage") or ""),
            importance=str(value.get("importance") or ""),
            audio_bytes=audio_bytes,
            audio_mime_type=str(value.get("audio_mime_type") or ""),
            audio_duration_s=(
                float(value["audio_duration_s"])
                if value.get("audio_duration_s") is not None
                else None
            ),
        )
        store.add_event(
            participant_id=participant_id,
            run_id=self.run_id,
            event_type="expert_moment_saved",
            details={
                "moment_id": row["moment_id"],
                "t_video_s": row["t_video_s"],
                "category": row["category"],
                "system_coverage": row["system_coverage"],
                "importance": row["importance"],
                "has_audio": bool(row.get("audio")),
            },
        )
        return {"ok": True, "moment": self._public_moment(participant_id, row)}

    def delete_moment(self, participant_id: str, moment_id: str) -> dict[str, Any]:
        store = self._study()
        participant_id = normalize_participant_id(participant_id)
        if store.progress(participant_id, self.run_id).get("case_completed_at"):
            raise ValueError("This case has already been completed")
        row = store.delete_moment(participant_id, self.run_id, moment_id)
        store.add_event(
            participant_id=participant_id,
            run_id=self.run_id,
            event_type="expert_moment_deleted",
            details={"moment_id": moment_id, "t_video_s": row.get("t_video_s")},
        )
        return {"ok": True, "moment_id": moment_id}

    def complete_study(self, participant_id: str, answers: Mapping[str, Any]) -> dict[str, Any]:
        store = self._study()
        participant_id = normalize_participant_id(participant_id)
        if store.progress(participant_id, self.run_id).get("review_flow") is not None:
            raise ValueError("Complete both review views and their questionnaire placeholders")
        use_intent = str(answers.get("would_use") or "")
        if use_intent not in {"yes", "with_changes", "no", "unsure"}:
            raise ValueError("Select whether you would use this workflow")
        validated = {
            "workflow_clarity": self._scale(answers.get("workflow_clarity"), "Workflow clarity"),
            "feedback_usefulness": self._scale(
                answers.get("feedback_usefulness"), "Feedback usefulness"
            ),
            "debrief_fit": self._scale(answers.get("debrief_fit"), "Debrief fit"),
            "would_use": use_intent,
            "most_useful": str(answers.get("most_useful") or "").strip()[:4000],
            "missing_or_concerning": str(answers.get("missing_or_concerning") or "").strip()[:4000],
        }
        session = store.complete(
            participant_id=participant_id,
            run_id=self.run_id,
            answers=validated,
        )
        store.add_event(
            participant_id=participant_id,
            run_id=self.run_id,
            event_type="experience_completed",
            details={"would_use": use_intent},
        )
        case = self._case_context()
        seed = f"{EXPERT_STUDY_SCHEMA}|{participant_id}"
        return {
            "ok": True,
            "case_completed_at": session["case_completed_at"],
            "next_case_url": case["next_case_url"],
            "completion_code": (
                None
                if case["next_case_url"]
                else "EXP-" + hashlib.sha256(seed.encode()).hexdigest()[:8].upper()
            ),
        }

    def log_event(
        self,
        participant_id: str,
        stimulus_id: str | None,
        event_type: str,
        details: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        participant_id = normalize_participant_id(participant_id)
        if event_type in {"analysis_started", "analysis_completed"}:
            raise ValueError("Analysis status is recorded by the analysis service")
        if stimulus_id and stimulus_id not in {
            str(row["feedback_id"]) for row in self._study_items()
        }:
            raise KeyError(stimulus_id)
        row = self._study().add_event(
            participant_id=participant_id,
            run_id=self.run_id,
            stimulus_id=stimulus_id,
            event_type=event_type,
            details=details,
        )
        return {"ok": True, "recorded_at": row["recorded_at"]}


def parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        raise ValueError("Unsupported byte range")
    first, last = match.groups()
    if not first:
        length = int(last)
        if length < 1:
            raise ValueError("Invalid suffix byte range")
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("Byte range is outside the file")
    return start, min(end, size - 1)


def _handler(service: FeedbackService, ui_dir: Path) -> type[BaseHTTPRequestHandler]:
    assets = {
        "/": (ui_dir / "index.html", "text/html; charset=utf-8"),
        "/index.html": (ui_dir / "index.html", "text/html; charset=utf-8"),
        "/styles.css": (ui_dir / "styles.css", "text/css; charset=utf-8"),
        "/app.js": (ui_dir / "app.js", "text/javascript; charset=utf-8"),
    }
    if service.mode == "expert-study":
        assets.update({
            "/surveys.js": (ui_dir / "surveys.js", "text/javascript; charset=utf-8"),
            "/surveys.css": (ui_dir / "surveys.css", "text/css; charset=utf-8"),
        })
    review_ui = _official_ui_directory("recommendations")
    summary_ui = review_ui.parent / "summary"
    assets.update(
        {
            "/summary": (summary_ui / "index.html", "text/html; charset=utf-8"),
            "/summary/": (summary_ui / "index.html", "text/html; charset=utf-8"),
            "/summary/styles.css": (summary_ui / "styles.css", "text/css; charset=utf-8"),
            "/summary/app.js": (summary_ui / "app.js", "text/javascript; charset=utf-8"),
            "/review-styles.css": (review_ui / "styles.css", "text/css; charset=utf-8"),
            "/event-review.js": (review_ui / "event-review.js", "text/javascript; charset=utf-8"),
        }
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "MastoidFeedback/1.0"

        def _headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def _json(self, status: int, value: Mapping[str, Any]) -> None:
            body = json.dumps(dict(value), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._headers()
            self.end_headers()
            self.wfile.write(body)

        def _body(self, maximum: int = 65536) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 1 or length > maximum:
                raise ValueError(f"Request body must be between 1 and {maximum} bytes")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Request body must be a JSON object")
            return value

        def _file(self, path: Path | None, mime_type: str, *, send_body: bool = True) -> None:
            if path is None or not path.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": "Media not available"})
                return
            size = path.stat().st_size
            try:
                requested = parse_byte_range(self.headers.get("Range"), size)
            except (TypeError, ValueError):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self._headers()
                self.end_headers()
                return
            start, end = requested or (0, size - 1)
            self.send_response(HTTPStatus.PARTIAL_CONTENT if requested else HTTPStatus.OK)
            self.send_header("Content-Type", mime_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header(
                "Cache-Control", "no-store" if mime_type.startswith("text/")
                else "private, max-age=3600",
            )
            self._headers()
            self.end_headers()
            if not send_body:
                return
            remaining = end - start + 1
            with path.open("rb") as handle:
                handle.seek(start)
                try:
                    while remaining:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

        def _audio(self, parsed, *, send_body: bool = True) -> None:
            if service.mode != "expert-study":
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            query = parse_qs(parsed.query)
            participant_id = (query.get("participant_id") or [""])[0]
            moment_id = (query.get("moment_id") or [""])[0]
            path, mime_type = service._study().audio(participant_id, service.run_id, moment_id)
            self._file(path, mime_type, send_body=send_body)

        def _keyframe(self, parsed, *, send_body: bool = True) -> None:
            query = parse_qs(parsed.query, keep_blank_values=True)
            if set(query) != {"clip_id"} or len(query["clip_id"]) != 1:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Original review frame unavailable"})
                return
            try:
                body = service.original_review_frame(query["clip_id"][0])
            except ValueError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Original review frame unavailable"})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._headers()
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/health":
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "mode": service.mode,
                            "run_id": service.run_id,
                            "interface_version": service.interface_version,
                            "available_runs": service.available_runs,
                        },
                    )
                    return
                if parsed.path == "/api/bootstrap" and service.mode == "recommendations":
                    self._json(HTTPStatus.OK, service.bootstrap())
                    return
                if parsed.path == "/api/phase-summary":
                    participant = (parse_qs(parsed.query).get("participant_id") or [None])[0]
                    if service.mode == "expert-study" and participant is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "Study session required"})
                        return
                    self._json(HTTPStatus.OK, service.phase_summary(participant))
                    return
                if parsed.path == "/api/media/video":
                    self._file(service.video_path, "video/webm")
                    return
                if parsed.path == "/api/media/keyframe":
                    self._keyframe(parsed)
                    return
                if parsed.path == "/api/expert-study/coaching-moment-audio":
                    self._audio(parsed)
                    return
                asset = assets.get(parsed.path)
                if asset is None or not asset[0].is_file():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                body = asset[0].read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", asset[1])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._headers()
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_HEAD(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/media/video":
                    self._file(service.video_path, "video/webm", send_body=False)
                    return
                if parsed.path == "/api/media/keyframe":
                    self._keyframe(parsed, send_body=False)
                    return
                if parsed.path == "/api/expert-study/coaching-moment-audio":
                    self._audio(parsed, send_body=False)
                    return
            except (ValueError, KeyError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self._headers()
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if service.mode != "expert-study":
                self._json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "Recommendations mode is read-only"},
                )
                return
            parsed = urlparse(self.path)
            try:
                body = self._body(
                    12 * 1024 * 1024
                    if parsed.path == "/api/expert-study/coaching-moment"
                    else 65536
                )
                participant_id = str(body.get("participant_id") or "")
                if parsed.path == "/api/expert-study/start":
                    self._json(
                        HTTPStatus.OK,
                        service.start_study(
                            participant_id, str(body.get("first_review_view", "event-based"))
                        ),
                    )
                    return
                if parsed.path == "/api/expert-study/analyze":
                    self._json(HTTPStatus.OK, service.analyze_study(participant_id))
                    return
                if parsed.path == "/api/expert-study/review-next":
                    self._json(
                        HTTPStatus.OK,
                        service.advance_review(
                            participant_id, body.get("view"), body.get("stage"),
                            body.get("step_id"), body.get("answers"),
                        ),
                    )
                    return
                if parsed.path == "/api/expert-study/complete-experience":
                    answers = body.get("answers")
                    if not isinstance(answers, dict):
                        raise ValueError("answers must be an object")
                    self._json(
                        HTTPStatus.OK,
                        service.complete_study(participant_id, answers),
                    )
                    return
                if parsed.path == "/api/expert-study/event":
                    details = body.get("details")
                    if details is not None and not isinstance(details, dict):
                        raise ValueError("details must be an object")
                    self._json(
                        HTTPStatus.OK,
                        service.log_event(
                            participant_id,
                            str(body["stimulus_id"]) if body.get("stimulus_id") else None,
                            str(body.get("event_type") or ""),
                            details,
                        ),
                    )
                    return
                if parsed.path == "/api/expert-study/coaching-moment":
                    self._json(HTTPStatus.OK, service.add_moment(participant_id, body))
                    return
                if parsed.path == "/api/expert-study/delete-coaching-moment":
                    self._json(
                        HTTPStatus.OK,
                        service.delete_moment(participant_id, str(body.get("moment_id") or "")),
                    )
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve_feedback_app(
    *,
    mode: str,
    db_path: Path,
    run_id: str,
    ui_dir: Path,
    host: str,
    port: int,
    video_path: Path | None = None,
    video_manifest_path: Path | None = None,
    ray_v3_dir: Path | None = None,
    cohort_benchmark_path: Path | None = None,
    phase_reference_path: Path | None = None,
    study_state_path: Path | None = None,
    available_runs: Sequence[Mapping[str, str]] = (),
    display_name: str = "",
    interface_version: str = DEFAULT_INTERFACE_VERSION,
) -> None:
    ui_dir = _validate_ui_directory(mode, ui_dir)
    required_assets = [ui_dir / name for name in ("index.html", "styles.css", "app.js")]
    missing = [str(path) for path in required_assets if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing UI assets: " + ", ".join(missing))
    service = FeedbackService(
        mode=mode,
        db_path=db_path,
        run_id=run_id,
        video_path=video_path,
        video_manifest_path=video_manifest_path,
        ray_v3_dir=ray_v3_dir,
        cohort_benchmark_path=cohort_benchmark_path,
        phase_reference_path=phase_reference_path,
        available_runs=available_runs or [{"run_id": run_id, "url": ""}],
        display_name=display_name,
        interface_version=interface_version,
        study_state_path=study_state_path,
    )
    server = ThreadingHTTPServer((host, int(port)), _handler(service, ui_dir))
    print(
        f"{mode} app ready: http://{host}:{server.server_port}/",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
