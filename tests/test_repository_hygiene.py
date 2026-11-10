"""Repository-level safeguards against publishing local or study data."""

from __future__ import annotations

import hashlib
import re
import subprocess
import unittest
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
# Explicit user authorization (2026-11-04): carry these frozen runtime resources
# with the project for migration. No other data or changed bytes are exempt.
AUTHORIZED_RESOURCE_HASHES = {
    # Follow-up user decision: retain each whole observed phase shorter than 10s.
    "references/r4_motion_windows_10s_v2.json": (
        "d5913b47a7bf3e8722cfb42b76d22e3a4379945973245507050abdc93a66e54e"
    ),
    # User-requested additional offline ten-second distribution (2026-11-09).
    "references/r4_motion_windows_10s_v1.json": (
        "f5b197bd02df375d69d5497a3e4440a721a432ee096062666e20c80f7e8058ff"
    ),
    "references/r4_phase_summary_v1.json": (
        "ba091b3489acf7432c9c335d435f6ceae2574f9865c55806e0f99023fc2f1c47"
    ),
    "references/r4_motion_windows_v2.json": (
        "5291f70328806f5899c21bc6bf61e9c509c70d98069c99502df405c94c3664b9"
    ),
    "references/r4_motion_windows_v3.json": (
        "16dea829688b5eecdfe66698cf3c60e41386bc463070a3587952dd2132a7f071"
    ),
    "references/r4_motion_windows_v4.json": (
        "14f68ad459bad32d56fd48ad1a8da6cb05d4d72e47db1855bd16b5a0158e880b"
    ),
    "references/r4_motion_windows_v6.json": (
        "1377c2badee4a11bce18319cb0c2c5a01d0c35b6d2ff2d24c39255e7e1e589f2"
    ),
    "references/geometry_corrections_v3.csv": (
        "eb635245a9c1b2f7dc4df007a59ba3f8958a75ba08a0826765444947e8c2e080"
    ),
    "models/e71_full_recording_holdout/model.pt": (
        "501f8d264bd74952e637e29ba9a86d18eff9561bfc14ac84059c3f618972e6cf"
    ),
    "models/e71_full_recording_holdout/manifest.json": (
        "9c25d07d7f862e7975ac296d66b1b2bf83577453aaf59b70867dd3256c0b661e"
    ),
    "models/e71_full_recording_holdout/neural_preprocessing.json": (
        "812bbc0d55eb2b75365df20e9f13d3b306daf3b37e1c496e513bf7d8f5aeee0b"
    ),
}

FORBIDDEN_FILE_SUFFIXES = (
    ".avi",
    ".db",
    ".dcm",
    ".dll",
    ".dylib",
    ".exe",
    ".h5",
    ".hdf5",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".nii",
    ".nii.gz",
    ".npy",
    ".npz",
    ".ogg",
    ".pickle",
    ".pkl",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".wav",
    ".webm",
    ".zip",
)

WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+)"
)
HTTP_URL = re.compile(r"https?://[^\s<>\"'`\[\]()\\]+", re.IGNORECASE)
POSIX_USER_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:Users|home|mnt|Volumes)/[^\s\"']+")
CODED_PARTICIPANT_TRIAL_ID = re.compile(
    r"(?<![A-Za-z0-9.])P[0-9]{2}(?:_T[0-9]{2})?\b", re.IGNORECASE
)


def repository_candidates() -> list[Path]:
    """Return tracked and non-ignored untracked files as Git would see them."""

    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            "Unable to enumerate repository candidates with Git: "
            + result.stderr.decode("utf-8", errors="replace")
        )

    relative_paths = result.stdout.decode("utf-8").split("\0")
    return [
        REPOSITORY_ROOT.joinpath(*PurePosixPath(value).parts) for value in relative_paths if value
    ]


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def windows_absolute_path_match(text: str) -> re.Match[str] | None:
    # SharePoint navigation routes can resemble drives. Keep source offsets intact
    # and leave the POSIX and participant-ID checks on the original text.
    masked = HTTP_URL.sub(lambda match: " " * len(match.group()), text)
    return WINDOWS_ABSOLUTE_PATH.search(masked)


class RepositoryHygieneTests(unittest.TestCase):
    def test_http_url_routes_do_not_mask_real_local_paths(self) -> None:
        urls = (
            "https://example.sharepoint.com/:f:/r/sites/study/Shared%20Documents",
            "HTTP://example.test/:f:/r/resources",
        )
        drive = "C" + ":" + "\\" + "private" + "\\" + "survey.json"
        unc = "\\" * 2 + "host" + "\\" + "share" + "\\" + "survey.json"
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(windows_absolute_path_match(f"[Source]({url})"))
                for local_path in (drive, unc):
                    text = f"[Source]({url})\nLocal file: {local_path}"
                    match = windows_absolute_path_match(text)
                    self.assertIsNotNone(match)
                    assert match is not None
                    self.assertEqual(match.start(), text.index(local_path))
                    self.assertEqual(line_number(text, match.start()), 2)

    def test_local_data_and_result_directories_are_ignored(self) -> None:
        probes = (
            "config/local/.repository-hygiene-probe",
            "data/.repository-hygiene-probe",
            "results/.repository-hygiene-probe",
            "study_data/participants/.repository-hygiene-probe",
        )
        missing = []
        for probe in probes:
            result = subprocess.run(
                ["git", "check-ignore", "--quiet", "--", probe],
                cwd=REPOSITORY_ROOT,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                missing.append(probe)
        self.assertFalse(
            missing,
            "The following local/generated paths are not protected by "
            f".gitignore: {', '.join(missing)}",
        )

    def test_candidate_tree_contains_only_publishable_source_files(self) -> None:
        problems: list[str] = []
        for path in repository_candidates():
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            lower_name = path.name.lower()

            if path.is_symlink():
                problems.append(f"{relative}: symbolic links are not allowed")
                continue
            if not path.is_file():
                continue
            if relative in AUTHORIZED_RESOURCE_HASHES:
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != AUTHORIZED_RESOURCE_HASHES[relative]
                ):
                    problems.append(
                        f"{relative}: frozen resource differs from its authorized digest"
                    )
                continue
            if any(lower_name.endswith(suffix) for suffix in FORBIDDEN_FILE_SUFFIXES):
                problems.append(f"{relative}: forbidden binary/data extension")
                continue
            if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                problems.append(
                    f"{relative}: exceeds the {MAX_SOURCE_FILE_BYTES}-byte source limit"
                )
                continue
            if CODED_PARTICIPANT_TRIAL_ID.search(relative):
                problems.append(f"{relative}: coded participant/trial ID in path")

            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                problems.append(f"{relative}: file is not UTF-8 text")
                continue

            for label, pattern in (
                ("local Windows/UNC absolute path", WINDOWS_ABSOLUTE_PATH),
                ("local POSIX user absolute path", POSIX_USER_ABSOLUTE_PATH),
                ("coded participant/trial ID", CODED_PARTICIPANT_TRIAL_ID),
            ):
                match = (
                    windows_absolute_path_match(text)
                    if pattern is WINDOWS_ABSOLUTE_PATH else pattern.search(text)
                )
                if match:
                    problems.append(f"{relative}:{line_number(text, match.start())}: {label}")

        self.assertFalse(
            problems,
            "Repository hygiene violations:\n" + "\n".join(sorted(problems)),
        )


if __name__ == "__main__":
    unittest.main()
