"""Movement rows -> player ids (synthetic frames; no network).

    ../web_app/venv/bin/python -m pytest tests/test_movements_resolve.py -q
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psx_crosswalk as X  # noqa: E402

MATCHED = pd.DataFrame({
    "player_string": ["Mo Williams", "Maurice Williams / Mo Williams", "Jon Doe"],
    "matched_name": ["Mo Williams"] * 2 + ["Jon Doe"],
    "player_id": [1.0, 1.0, 2.0],
    "method": ["exact", "slash_variant", "exact"],
})


def _rows(rows):
    base = {"date": "2024-01-01", "team": "Heat", "acquired": "", "relinquished": "", "notes": "waived", "move_type": "waived"}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_player_taken_from_whichever_side_is_filled_and_ids_attach():
    out = X.resolve_movements(_rows([
        {"relinquished": "Maurice Williams / Mo Williams"},
        {"acquired": "Jon Doe", "notes": "claimed off waivers (from Nets)", "move_type": "claimed"},
        {"relinquished": "Nobody Known"},
    ]), MATCHED)
    assert list(out.player_id.astype("float")) [:2] == [1.0, 2.0]
    assert pd.isna(out.player_id.iloc[2]) and out.player_string.iloc[2] == "Nobody Known"


def test_staff_rows_are_not_treated_as_players_but_labeled_rows_always_are():
    out = X.resolve_movements(_rows([
        {"acquired": "Some Coach", "notes": "hired as assistant coach", "move_type": ""},
        {"relinquished": "Jon Doe", "notes": "waived", "move_type": "waived"},
    ]), MATCHED)
    assert out.player_string.iloc[0] == "" and out.player_string.iloc[1] == "Jon Doe"
    assert X.movement_player_strings(_rows([{"relinquished": "Jon Doe"}])) == ["Jon Doe"]


def test_nba_api_tier_matches_recent_players_but_not_namesakes_fathers_or_old_careers():
    api = pd.DataFrame({
        "id": [1630654, 452, 2668, 77106],
        "full_name": ["Armando Bacot", "Vin Baker", "Josh Davis", "Phil Jackson"],
        "is_active": [True, False, False, False],
    })
    modern = pd.DataFrame({"PLAYER_NAME": ["Someone Else"], "PLAYER_ID": [1]})
    names = ["Armando Bacot", "Vin Baker Jr.", "Josh Davis (b. 1991-01-22)", "Phil Jackson"]
    matched, unmatched = X.build_crosswalk(names, modern, None, api)
    assert list(matched.player_string) == ["Armando Bacot"] and matched.method.iloc[0] == "nba_api_static"
    assert set(unmatched.player_string) == set(names[1:])


def test_trade_assets_split_outside_parentheses_and_keep_only_players():
    trades = pd.DataFrame([{
        "date": "2024-02-08", "team": "Heat", "counterparty": "Pacers",
        "acquired_assets": "Jon Doe; 2026 first round pick (protected top 4; else second round) (?-?); cash; rights to Overseas Guy; $1.1M cash",
        "relinquished_assets": "Mo Williams",
    }])
    rows = X.trade_player_rows(trades)
    assert sorted(zip(rows.side, rows.player_string)) == [("acquired", "Jon Doe"), ("relinquished", "Mo Williams")]
    out = X.resolve_trade_players(rows, MATCHED)
    assert sorted(out.player_id.astype(float)) == [1.0, 2.0]
