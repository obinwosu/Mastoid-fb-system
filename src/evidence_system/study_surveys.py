"""Versioned study questionnaire wording, validation and sequential review protocol.

These are participant-reported research responses, not validated OSATS scores,
clinical ground truth, or automatic assessments of the recorded performer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

SURVEY_VERSION = "mastoid_surveys_v3_resident"
FLOW_SCHEMA = "sequential_review_v2"
ROLE_UNITS = {
    "resident": "pgy",
    "fellow": "pgy",
    "attending": "independent_practice_years",
}
# PGY bubbles by role: residents PGY 1-5, fellows PGY 6-7. Attendings type years in practice.
ROLE_PGY = {"resident": [1, 2, 3, 4, 5], "fellow": [6, 7]}
# The resident study leaves out the three self-assessments for now (2026-11-07).
# Set True to bring them back; nothing else needs to change.
SELF_ASSESSMENTS = False
MASTOIDECTOMY_COUNTS = ["0", "1_5", "6_10", "11_20", "over_20"]

SELF_ASSESSMENT_ITEMS = [
    {
        "id": "protection",
        "title": "Protection of critical structures",
        "focus": "How consistently did you respect critical anatomical boundaries?",
        "anchors": {
            "1": "Critical boundaries were crossed, or unsafe approaches repeatedly continued.",
            "3": "Boundaries were usually respected, with occasional poorly controlled approaches.",
            "5": (
                "Boundaries were consistently respected, with drilling adjusted "
                "to the local anatomy."
            ),
        },
    },
    {
        "id": "visibility",
        "title": "Visibility during drilling",
        "focus": "How well did you maintain a clear view of the drill–bone contact area?",
        "anchors": {
            "1": "Drilling frequently continued while the contact area was obscured.",
            "3": (
                "The contact area was usually visible, with occasional drilling before "
                "visibility was restored."
            ),
            "5": (
                "The contact area remained visible during drilling; obscuration prompted "
                "a pause or adjustment."
            ),
        },
    },
    {
        "id": "control",
        "title": "Drill control",
        "focus": "How well did you control drill movements for the local task?",
        "anchors": {
            "1": (
                "Movements were frequently abrupt, unintended, or poorly matched "
                "to the working space."
            ),
            "3": "Movements were generally controlled, with occasional unnecessary corrections.",
            "5": (
                "Movements were consistently controlled, with stroke size adapted "
                "to the local task."
            ),
        },
    },
    {
        "id": "removal",
        "title": "Purposeful bone removal",
        "focus": "How consistently did your drilling advance the intended exposure?",
        "anchors": {
            "1": "Repeated drilling made little progress toward the intended exposure.",
            "3": "Most drilling advanced the intended exposure, with some unnecessary repetition.",
            "5": (
                "Bone removal consistently advanced the intended exposure "
                "with little unnecessary work."
            ),
        },
    },
    {
        "id": "orientation",
        "title": "Anatomical orientation",
        "focus": "How clearly did you understand your location and the relevant landmarks?",
        "anchors": {
            "1": (
                "I frequently lost track of my location, relevant landmarks, or intended direction."
            ),
            "3": (
                "I usually understood my location and relevant landmarks, "
                "but occasionally needed to reorient."
            ),
            "5": (
                "I consistently understood my location and used relevant landmarks "
                "to guide my next steps."
            ),
        },
    },
    {
        "id": "planning",
        "title": "Procedure flow and planning",
        "focus": "How coherently did you organize and adapt the sequence of steps?",
        "anchors": {
            "1": (
                "Steps frequently lacked a coherent plan, with repeated unnecessary "
                "returns to earlier areas."
            ),
            "3": (
                "Steps followed a recognizable sequence, with some avoidable "
                "interruptions or repetition."
            ),
            "5": (
                "Steps followed a coherent plan, with purposeful transitions and "
                "adjustments as the task progressed."
            ),
        },
    },
]

SURVEY_DEFINITION: dict[str, Any] = {
    "version": SURVEY_VERSION,
    "protocol": {
        "schema": FLOW_SCHEMA,
        "scope": "Self-assessment of the same recorded procedure at all three time points",
        "order_assignment": "Study entry selection; not automatic randomized assignment",
        "self_assessment_status": "Proposed OSATS-like research questionnaire; not validated",
        "user_review_performed": False,
        "clinician_review_performed": False,
        "total_score_defined": False,
        "pass_threshold_defined": False,
        "second_exposure": "Interpret as additional exposure after the first feedback format",
        "self_assessments_included": SELF_ASSESSMENTS,
    },
    "background": {
        "title": "Background Questionnaire",
        "instructions": (
            "A few questions about your training and experience before today's session."
        ),
        "fields": {
            "role": {
                "label": "Current role",
                "options": list(ROLE_UNITS),
            },
            "training_stage": {
                "label": "Current training year", "role_units": ROLE_UNITS, "role_pgy": ROLE_PGY,
                "description": "Residents choose PGY 1-5; fellows choose PGY 6 or 7; "
                               "attendings type their years in practice.",
            },
            "cortical_mastoidectomies": {
                "label": "Number of prior cortical mastoidectomies performed",
                "options": MASTOIDECTOMY_COUNTS,
                "option_labels": ["0", "1-5", "6-10", "11-20", ">20"],
            },
        },
    },
    "self_assessment": {
        "title": "Mastoidectomy self-assessment",
        "instructions": (
            "Rate your performance in this recorded procedure using the descriptions below. "
            "Your ratings may stay the same or change."
        ),
        "items": SELF_ASSESSMENT_ITEMS,
        "minimum": 1,
        "maximum": 5,
        "between_instruction": "Choose 2 or 4 for performance between the descriptions.",
        "missing_options": [
            {"value": "cannot_assess", "label": "Cannot assess",
             "description": "I do not have enough information to judge this."},
            {"value": "not_applicable", "label": "Not applicable",
             "description": "This aspect was outside the assigned task."},
        ],
    },
    "feedback": {
        "title": "Feedback questionnaire",
        "instructions": "Please rate the feedback format you just reviewed.",
        "items": [
            {"id": "clarity", "label": "Clarity", "text": "The feedback was clear to me."},
            {"id": "actionability", "label": "Actionability", "text": (
                "The feedback helped me identify specific actions I could take "
                "to improve my performance."
            )},
        ],
        "scale": [
            {"value": value, "label": label}
            for value, label in enumerate([
                "Strongly disagree", "Disagree", "Neutral",
                "Agree", "Strongly agree",
            ])
        ],
    },
    "preference": {
        "title": "Which feedback format did you prefer?",
        "question": "Which feedback format did you prefer?",
        "options": [
            {
                "value": "event-based", "label": "Event-based feedback",
                "description": (
                    "Highlighting the specific moments where your performance deviated "
                    "from expert performance."
                ),
            },
            {
                "value": "summary", "label": "Summary-based feedback",
                "description": (
                    "Contextualizing your performance metrics against expert performance metrics."
                ),
            },
            {"value": "combination", "label": "A combination of both"},
            {"value": "no_preference", "label": "No preference"},
        ],
    },
}


def definition_hash(definition: Mapping[str, Any]) -> str:
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(expected))}")
    return value


def _number(value: Any, label: str, *, minimum: int = 0, integer: bool = False) -> Any:
    try:
        finite = type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        finite = False
    if (
        not finite
        or value < minimum
        or (integer and type(value) is not int)
    ):
        kind = "integer" if integer else "number"
        raise ValueError(f"{label} must be a finite {kind} of at least {minimum}")
    return value


def _measurement(
    value: Any, label: str, *, integer: bool = False,
    allow_not_applicable: bool = False, minimum: int = 0,
) -> dict[str, Any]:
    item = _fields(value, {"status", "value"}, label)
    statuses = {"provided", "not_sure"}
    if allow_not_applicable:
        statuses.add("not_applicable")
    if not isinstance(item["status"], str) or item["status"] not in statuses:
        raise ValueError(f"Invalid response status for {label}")
    if item["status"] == "provided":
        _number(item["value"], label, minimum=minimum, integer=integer)
    elif item["value"] is not None:
        raise ValueError(f"{label} must have a null value when it is not provided")
    return copy.deepcopy(item)


def validate_background(answers: Any) -> dict[str, Any]:
    row = _fields(answers, set(SURVEY_DEFINITION["background"]["fields"]), "Background answers")
    role = row["role"]
    if not isinstance(role, str) or role not in ROLE_UNITS:
        raise ValueError("Choose a valid current role")
    stage = _fields(row["training_stage"], {"status", "value", "unit"}, "Training stage")
    if stage["unit"] != ROLE_UNITS[role]:
        raise ValueError("Training-stage unit does not match the selected role")
    if stage["status"] != "provided":
        raise ValueError("Give your training year or years in practice")
    _number(stage["value"], "Training stage", minimum=0, integer=True)
    if role in ROLE_PGY and stage["value"] not in ROLE_PGY[role]:
        years = ROLE_PGY[role]
        raise ValueError(f"{role.capitalize()}s choose PGY {years[0]}-{years[-1]}")
    count = row["cortical_mastoidectomies"]
    if not isinstance(count, str) or count not in MASTOIDECTOMY_COUNTS:
        raise ValueError("Choose the number of prior cortical mastoidectomies")
    return copy.deepcopy(row)


def validate_answers(stage: str, answers: Any) -> dict[str, Any] | None:
    if stage == "background":
        return validate_background(answers)
    if stage == "self_assessment":
        row = _fields(answers, {item["id"] for item in SELF_ASSESSMENT_ITEMS}, "Self-assessment")
        for key, value in row.items():
            if type(value) is int and 1 <= value <= 5:
                continue
            if isinstance(value, str) and value in {"cannot_assess", "not_applicable"}:
                continue
            raise ValueError(f"{key} must be 1–5, cannot_assess, or not_applicable")
        return copy.deepcopy(row)
    if stage == "questionnaire":
        row = _fields(answers, {"clarity", "actionability"}, "Feedback questionnaire")
        for key, value in row.items():
            if type(value) is not int or value not in range(5):
                raise ValueError(f"{key} must be an integer from 0 to 4")
        return copy.deepcopy(row)
    if stage == "preference":
        row = _fields(answers, {"preferred_view"}, "Preference questionnaire")
        if not isinstance(row["preferred_view"], str) or row["preferred_view"] not in {
            "event-based", "summary", "combination", "no_preference",
        }:
            raise ValueError("Choose event-based, summary, combination, or no_preference")
        return copy.deepcopy(row)
    if stage == "review":
        if answers is not None:
            raise ValueError("A review continuation does not accept questionnaire answers")
        return None
    raise ValueError("Invalid survey stage")


def protocol_steps(order: list[str], *, background: bool, final_case: bool) -> list[dict[str, Any]]:
    def step(
        step_id: str, stage: str, index: int, view: str | None = None,
        assessment_round: int | None = None,
    ) -> dict[str, Any]:
        return {
            "step_id": step_id, "stage": stage, "index": index,
            "current_view": view, "assessment_round": assessment_round,
        }

    steps = [step("background", "background", 0)] if background else []
    if SELF_ASSESSMENTS:
        steps.append(step("assessment_0", "self_assessment", 0, assessment_round=0))
    for index, view in enumerate(order):
        steps.append(step(f"review_{index}", "review", index, view))
        if SELF_ASSESSMENTS:
            steps.append(step(f"assessment_{index + 1}", "self_assessment", index, view, index + 1))
        steps.append(step(f"questionnaire_{index}", "questionnaire", index, view))
    if final_case:
        steps.append(step("preference", "preference", 2))
    steps.append(step("complete", "complete", 2))
    return steps


def new_review_flow(
    first: str, now: str, *, background: bool, final_case: bool,
) -> dict[str, Any]:
    order = [first, "summary" if first == "event-based" else "event-based"]
    steps = protocol_steps(order, background=background, final_case=final_case)
    return {
        "schema": FLOW_SCHEMA, "order": order, "cursor": 0, **steps[0],
        "steps": steps, "responses": {}, "transitions": [], "created_at": now,
        "completed_at": None, "final_case": final_case,
        "survey_definition": copy.deepcopy(SURVEY_DEFINITION),
        "survey_version": SURVEY_VERSION,
    }


def public_review_flow(flow: Mapping[str, Any]) -> dict[str, Any]:
    if flow.get("schema") != FLOW_SCHEMA:
        return copy.deepcopy(dict(flow))
    public = {
        key: copy.deepcopy(flow.get(key))
        for key in (
            "schema", "order", "stage", "step_id", "index", "current_view",
            "assessment_round", "created_at", "completed_at", "final_case", "survey_version",
        )
    }
    steps, cursor = flow.get("steps") or [], flow.get("cursor", 0)
    # Lets the page name the next step ("Continue to questionnaire").
    public["next_stage"] = steps[cursor + 1]["stage"] if cursor + 1 < len(steps) else None
    return public


def response_record(
    flow: Mapping[str, Any], answers: Mapping[str, Any], *, participant_id: str,
    run_id: str, now: str,
) -> dict[str, Any]:
    definition = flow["survey_definition"]
    section = "feedback" if flow["stage"] == "questionnaire" else flow["stage"]
    instrument = {
        "version": definition["version"], "protocol": definition["protocol"],
        section: definition[section],
    }
    return {
        "participant_id": participant_id, "run_id": run_id,
        "step_id": flow["step_id"], "stage": flow["stage"],
        "view": flow["current_view"], "assessment_round": flow["assessment_round"],
        "order": list(flow["order"]), "submitted_at": now,
        "instrument_version": definition["version"],
        "instrument_sha256": definition_hash(instrument),
        "instrument": copy.deepcopy(instrument), "answers": copy.deepcopy(dict(answers)),
    }
