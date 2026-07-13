import json
from datetime import datetime

from tiktok_mcp_client.cli import (
    extract_rows,
    flatten_dict,
    parse_json_argument,
    render_dynamic_values,
)


def test_parse_json_argument(tmp_path):
    path = tmp_path / "arguments.json"
    path.write_text('{"advertiser_id": "123"}', encoding="utf-8")

    assert parse_json_argument(f"@{path}") == {"advertiser_id": "123"}


def test_render_dynamic_values():
    now = datetime(2026, 7, 13, 21, 0, 0)
    value = {
        "start_date": "${yesterday}",
        "end_date": "${today}",
        "week_start": "${week_ago}",
        "nested": ["fetched-${now}"],
    }

    assert render_dynamic_values(value, now) == {
        "start_date": "2026-07-12",
        "end_date": "2026-07-13",
        "week_start": "2026-07-06",
        "nested": ["fetched-2026-07-13T21:00:00"],
    }


def test_extract_and_flatten_rows():
    payload = {
        "parsed": {
            "data": {
                "list": [
                    {
                        "dimensions": {"campaign_id": "1"},
                        "metrics": {"spend": "12.5"},
                        "tags": ["a", "b"],
                    }
                ]
            }
        }
    }

    rows = extract_rows(payload)
    assert len(rows) == 1
    assert flatten_dict(rows[0]) == {
        "dimensions.campaign_id": "1",
        "metrics.spend": "12.5",
        "tags": json.dumps(["a", "b"], ensure_ascii=False, separators=(",", ":")),
    }

    assert extract_rows({"structuredContent": {"rows": [{"id": "2"}]}}) == [
        {"id": "2"}
    ]
