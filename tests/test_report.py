from tiktok_mcp_client.report import (
    build_summary,
    filter_rows_with_activity,
    resolve_date_preset,
)


def test_resolve_date_preset_lifetime():
    start, end, lifetime = resolve_date_preset("lifetime")
    assert start is None and end is None and lifetime is True


def test_resolve_date_preset_custom():
    start, end, lifetime = resolve_date_preset(
        "custom", start_date="2026-07-01", end_date="2026-07-13"
    )
    assert start == "2026-07-01"
    assert end == "2026-07-13"
    assert lifetime is False


def test_build_summary_and_filter():
    rows = [
        {
            "advertiser_id": "1",
            "advertiser_name": "A",
            "metrics": {"spend": "10", "impressions": "100", "clicks": "5", "conversion": "0"},
        },
        {
            "advertiser_id": "1",
            "advertiser_name": "A",
            "metrics": {"spend": "0", "impressions": "0", "clicks": "0", "conversion": "0"},
        },
        {
            "advertiser_id": "2",
            "advertiser_name": "B",
            "metrics.spend": "5",
            "metrics.impressions": "50",
            "metrics.clicks": "2",
            "metrics.conversion": "1",
        },
    ]
    active = filter_rows_with_activity(rows)
    assert len(active) == 2
    spend_only = filter_rows_with_activity(rows, only_spend=True)
    assert len(spend_only) == 2
    summary = build_summary(active)
    assert summary["totals"]["spend"] == 15.0
    assert summary["totals"]["conversion"] == 1.0
    assert len(summary["by_advertiser"]) == 2
