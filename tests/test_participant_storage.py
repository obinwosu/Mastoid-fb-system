"""Participant isolation, portable IDs and lossless migration of existing study data."""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.study_storage import migrate_study_state
from evidence_system.web import ExpertStudyStore


def legacy_payload():
    return {
        "schema": "local_expert_study_state_v1",
        "custom_metadata": {"version": 17},
        "expert_study": {
            "schema": "expert_feedback_study_v1",
            "custom_study_metadata": ["preserve"],
            "participants": {
                "A": {"participant_id": "A", "background": {"answers": {"role": "resident"}}},
                "B": {"participant_id": "B", "preference": {"answers": {"view": "summary"}}},
            },
            "sessions": {
                "A::one": {
                    "participant_id": "A",
                    "run_id": "one",
                    "first_review_view": "summary",
                    "review_flow": {
                        "stage": "self_assessment",
                        "responses": {
                            "baseline": {
                                "answers": {"control": 4},
                                "instrument": {"exact": "snapshot"},
                            }
                        },
                    },
                },
                "A::two": {"participant_id": "A", "run_id": "two", "custom": "retain"},
                "B::one": {
                    "participant_id": "B",
                    "run_id": "one",
                    "experience_response": {"answers": {"legacy": "answer"}},
                },
            },
            "events": [
                {"participant_id": "B", "event_id": "first", "recorded_at": "one"},
                {"participant_id": "A", "event_id": "second", "recorded_at": "two"},
                {"participant_id": "B", "event_id": "third", "recorded_at": "three"},
            ],
            "expert_proposed_moments": {
                "moment": {
                    "participant_id": "A",
                    "run_id": "one",
                    "observation": "keep",
                    "audio": {"file_name": "old.wav", "size_bytes": 4},
                }
            },
        },
    }


def source_file(tmp_path, payload=None):
    source = tmp_path / "old" / "study.json"
    source.parent.mkdir()
    (source.parent / "recordings").mkdir()
    (source.parent / "recordings/old.wav").write_bytes(b"RIFF")
    source.write_text(json.dumps(payload or legacy_payload(), indent=2), encoding="utf-8")
    return source


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_migration_preserves_complete_rows_metadata_audio_and_source_bytes(tmp_path):
    source = source_file(tmp_path)
    original = source.read_bytes()
    destination = tmp_path / "participants"
    receipt = migrate_study_state(source, destination)
    assert receipt["participant_count"] == 2
    assert receipt["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original
    assert Path(receipt["source_backup"]).read_bytes() == original
    assert (destination / "recordings/old.wav").read_bytes() == b"RIFF"
    assert (source.parent / "recordings/old.wav").read_bytes() == b"RIFF"
    original_study = legacy_payload()["expert_study"]
    for owner in ["A", "B"]:
        payload = read(destination / f"{owner}.json")
        study = payload["expert_study"]
        assert payload["participant_id"] == owner
        assert payload["custom_metadata"] == {"version": 17}
        assert study["custom_study_metadata"] == ["preserve"]
        assert study["participants"] == {owner: original_study["participants"][owner]}
        for key in ["sessions", "expert_proposed_moments"]:
            assert study[key] == {
                k: v for k, v in original_study[key].items() if v["participant_id"] == owner
            }
        assert study["events"] == [
            row for row in original_study["events"] if row["participant_id"] == owner
        ]


def test_new_submissions_change_only_their_participant_file(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    for owner in ["A", "B"]:
        store.start_session(participant_id=owner, run_id="one", stimulus_ids=[])
    before_b = store.participant_path("B").read_bytes()
    store.add_event(participant_id="A", run_id="one", event_type="video_play", details={"t": 1})
    store.start_session(participant_id="A", run_id="two", stimulus_ids=[])
    assert store.participant_path("B").read_bytes() == before_b
    saved = read(store.participant_path("A"))["expert_study"]
    assert set(saved["sessions"]) == {"A::one", "A::two"}
    assert [row["participant_id"] for row in saved["events"]] == ["A"]
    assert not (tmp_path / "participants.json").exists()


def test_same_source_migration_and_legacy_constructor_do_not_reimport_stale_answers(tmp_path):
    source = source_file(tmp_path)
    store = ExpertStudyStore(source, "test")
    assert store.directory == source.with_name("study_participants")
    store.add_event(participant_id="A", run_id="one", event_type="video_seek", details={"t": 10})
    advanced = store.participant_path("A").read_bytes()
    first_receipt = migrate_study_state(source, store.directory)
    reopened = ExpertStudyStore(source, "later")
    assert reopened.participant_path("A").read_bytes() == advanced
    assert migrate_study_state(source, store.directory) == first_receipt
    assert source.read_bytes() == Path(first_receipt["source_backup"]).read_bytes()


def test_changed_legacy_source_is_rejected_without_overwriting_new_data(tmp_path):
    source = source_file(tmp_path)
    store = ExpertStudyStore(source, "test")
    before = {p.name: p.read_bytes() for p in store.directory.glob("*.json")}
    payload = read(source)
    payload["expert_study"]["participants"]["A"]["new"] = "legacy edit"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after migration"):
        ExpertStudyStore(source, "test")
    assert {p.name: p.read_bytes() for p in store.directory.glob("*.json")} == before


def test_conflicting_target_is_rejected_before_any_participant_is_migrated(tmp_path):
    source = source_file(tmp_path)
    destination = tmp_path / "participants"
    store = ExpertStudyStore(destination, "test")
    store.start_session(participant_id="B", run_id="new", stimulus_ids=[])
    before = store.participant_path("B").read_bytes()
    with pytest.raises(ValueError, match="overwrite existing participant"):
        migrate_study_state(source, destination)
    assert store.participant_path("B").read_bytes() == before
    assert not store.participant_path("A").exists()
    assert not (destination / ".migrations").exists()


@pytest.mark.parametrize("owner", ["CON", "NUL", "PRN.log", "COM1", "LPT9"])
def test_windows_reserved_ids_have_portable_filenames(tmp_path, owner):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    store.start_session(participant_id=owner, run_id="one", stimulus_ids=[])
    path = store.participant_path(owner)
    assert path.name == "~" + hashlib.sha256(owner.encode()).hexdigest() + ".json"
    assert read(path)["participant_id"] == owner
    assert store.progress(owner, "one")["participant_id"] == owner


def test_case_distinct_ids_do_not_overwrite_on_case_insensitive_filesystems(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    store.start_session(participant_id="User", run_id="one", stimulus_ids=[])
    original = store.participant_path("User").read_bytes()
    store.start_session(participant_id="user", run_id="two", stimulus_ids=[])
    assert (
        store.participant_path("User").name.casefold()
        != store.participant_path("user").name.casefold()
    )
    assert store.participant_path("User").read_bytes() == original
    assert store.progress("user", "two")["participant_id"] == "user"
    assert store.progress("User", "two") == {}


def test_migrating_case_distinct_ids_preserves_both(tmp_path):
    payload = legacy_payload()
    payload["expert_study"]["participants"]["a"] = {"participant_id": "a", "value": "lower"}
    source = source_file(tmp_path, payload)
    destination = tmp_path / "participants"
    receipt = migrate_study_state(source, destination)
    assert len({value.casefold() for value in receipt["participant_files"].values()}) == 3
    store = ExpertStudyStore(destination, "test")
    assert (
        read(store.participant_path("a"))["expert_study"]["participants"]["a"]["value"] == "lower"
    )
    assert store.progress("A", "one")["participant_id"] == "A"


@pytest.mark.parametrize("section", ["sessions", "participants", "events"])
def test_unknown_or_mismatched_ownership_is_rejected_without_partial_migration(tmp_path, section):
    payload = copy.deepcopy(legacy_payload())
    if section == "sessions":
        payload["expert_study"][section]["A::one"]["participant_id"] = "B"
    elif section == "participants":
        payload["expert_study"][section]["A"]["participant_id"] = "B"
    else:
        payload["expert_study"][section][0].pop("participant_id")
    source = source_file(tmp_path, payload)
    original = source.read_bytes()
    destination = tmp_path / "participants"
    with pytest.raises(ValueError):
        migrate_study_state(source, destination)
    assert source.read_bytes() == original
    assert not list(destination.glob("*.json"))


def test_corrupt_filename_identity_does_not_silently_start_a_new_session(tmp_path):
    store = ExpertStudyStore(tmp_path / "participants", "test")
    store.start_session(participant_id="A", run_id="one", stimulus_ids=[])
    path = store.participant_path("A")
    payload = read(path)
    payload["participant_id"] = "Unrelated"
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="filename and stored identity disagree"):
        store.progress("A", "one")
    assert path.read_bytes() == original


def test_missing_legacy_audio_prevents_partial_migration(tmp_path):
    source = source_file(tmp_path)
    (source.parent / "recordings/old.wav").unlink()
    destination = tmp_path / "participants"
    with pytest.raises(FileNotFoundError):
        migrate_study_state(source, destination)
    assert not list(destination.glob("*.json"))
