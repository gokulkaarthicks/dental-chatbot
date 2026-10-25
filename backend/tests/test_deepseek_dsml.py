"""Regression coverage for DeepSeek native DSML tool-call responses."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.ai.deepseek import DeepSeekClient, _tool_calls_from_dsml
from app.config import Settings


def test_dsml_family_call_is_normalized_to_valid_json_arguments() -> None:
    content = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="search_family_availability">\n'
        '<｜｜DSML｜｜ parameter name="back_to_back" string="false">true\\</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="date_phrases" string="false">["next saturday"]\\</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="members" string="false">'
        '[{"appointment_type":"cleaning","patient_first_name":"Marcus"},'
        '{"appointment_type":"checkup","patient_first_name":"Ellie"},'
        '{"appointment_type":"checkup","patient_first_name":"Noah"}]'
        '\\</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="time_windows" string="false">["morning"]\\</｜｜DSML｜｜ parameter>\n'
        '\\</｜｜DSML｜｜ invoke>\n'
        '\\</｜｜DSML｜｜ calls>'
    )

    cleaned, calls = _tool_calls_from_dsml(content)

    assert cleaned == ""
    assert len(calls) == 1
    assert calls[0].name == "search_family_availability"
    assert calls[0].id == "dsml_1"
    assert calls[0].arguments == (
        '{"back_to_back": true, "date_phrases": ["next saturday"], '
        '"members": [{"appointment_type": "cleaning", "patient_first_name": "Marcus"}, '
        '{"appointment_type": "checkup", "patient_first_name": "Ellie"}, '
        '{"appointment_type": "checkup", "patient_first_name": "Noah"}], '
        '"time_windows": ["morning"]}'
    )


def test_plain_deepseek_content_is_unchanged() -> None:
    content = "Please tell me which child needs the checkup."

    cleaned, calls = _tool_calls_from_dsml(content)

    assert cleaned == content
    assert calls == []


def _dsml_search_content() -> str:
    return (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="search_family_availability">\n'
        '<｜｜DSML｜｜ parameter name="date_phrases" string="false">["any day"]\\</｜｜DSML｜｜ parameter>\n'
        '\\</｜｜DSML｜｜ invoke>\n'
        '\\</｜｜DSML｜｜ calls>'
    )


def test_dsml_is_promoted_only_when_tools_were_offered() -> None:
    settings = Settings(
        deepseek_api_key="test-key",
        deepseek_base_url="https://api.deepseek.test",
        deepseek_model="deepseek-flash",
        database_path=Path("/tmp/clinic-test.db"),
    )
    client = DeepSeekClient(settings=settings)
    payload_body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": _dsml_search_content(), "tool_calls": None},
            }
        ]
    }
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload_body

    with patch("app.ai.deepseek.httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.post.return_value = response
        with_tools = client._post(
            {"model": "deepseek-flash", "messages": []}, tools_enabled=True
        )
        without_tools = client._post(
            {"model": "deepseek-flash", "messages": []}, tools_enabled=False
        )

    assert len(with_tools.tool_calls) == 1
    assert with_tools.tool_calls[0].name == "search_family_availability"
    assert without_tools.tool_calls == []
    assert without_tools.content == ""
