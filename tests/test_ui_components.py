from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from invoiceops.schemas import StageEvent, StageName, StageStatus
from invoiceops.ui_components import update_live_stage


class FakeStatus:
    def __init__(self):
        self.messages = []

    def write(self, message):
        self.messages.append(message)

    def update(self, **_kwargs):
        pass


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ({}, []),
        ({"warnings": None}, []),
        ({"warnings": ""}, []),
        ({"warnings": "One warning."}, ["One warning."]),
        ({"warnings": []}, []),
        ({"warnings": ["First warning.", "Second warning."]},
         ["First warning.", "Second warning."]),
        ({"warnings": 2}, []),
    ],
)
def test_stage_event_normalizes_warning_contract(detail, expected):
    event = StageEvent(
        stage=StageName.VALIDATION,
        status=StageStatus.COMPLETED,
        detail=detail,
    )

    assert event.detail["warnings"] == expected


def test_every_workflow_stage_defaults_warnings_to_a_list():
    for stage in StageName:
        event = StageEvent(stage=stage, status=StageStatus.RUNNING)
        assert event.detail["warnings"] == []


@pytest.mark.parametrize(
    ("detail", "expected_message"),
    [
        ({}, None),
        ({"warnings": None}, None),
        ({"warnings": []}, None),
        ({"warnings": "A legacy single warning."}, "Warnings: A legacy single warning."),
        ({"warnings": ["First warning.", "Second warning."]},
         "Warnings: First warning.; Second warning."),
        ({"warnings": 2}, None),
    ],
)
def test_live_stage_renderer_is_defensive_for_optional_warnings(
    monkeypatch,
    detail,
    expected_message,
):
    handle = FakeStatus()
    monkeypatch.setattr("invoiceops.ui_components.st.status", lambda *_args, **_kwargs: handle)
    event = SimpleNamespace(
        stage=StageName.VALIDATION,
        status=StageStatus.COMPLETED,
        timestamp=datetime.now(timezone.utc),
        message="",
        detail=detail,
    )

    update_live_stage(event, {}, {})

    warning_messages = [message for message in handle.messages if message.startswith("Warnings:")]
    assert warning_messages == ([expected_message] if expected_message else [])
