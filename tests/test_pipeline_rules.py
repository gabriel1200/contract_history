"""Pipeline rule tests (synthetic frames; no network or snapshot needed).

    ../web_app/venv/bin/python -m pytest tests/test_pipeline_rules.py -q
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_unified as B  # noqa: E402
import psx_crosswalk as X  # noqa: E402


def _resolved(rows):
    base = {"team": "Knicks", "player": "A", "player_id": np.nan, "total_value_musd": np.nan, "total_seasons": np.nan,
            "has_contract_word": True, "is_coach_or_exec": False, "aav_musd": np.nan, "start_season": 2025}
    frame = pd.DataFrame([{**base, **row} for row in rows])
    frame["contract_id"] = [f"c{i}" for i in range(len(frame))]
    return frame


def test_later_report_of_the_same_priced_signing_supersedes_the_earlier_one():
    frame = _resolved([
        {"date": "2024-06-26", "player": "Anunoby", "total_value_musd": 212.5, "total_seasons": 5},
        {"date": "2024-07-06", "player": "Anunoby", "total_value_musd": 212.5, "total_seasons": 5},
        {"date": "2024-10-20", "player": "Anunoby", "total_value_musd": 212.5, "total_seasons": 5},  # >60 days later: separate
        {"date": "2024-07-01", "player": "Other", "total_value_musd": 212.5, "total_seasons": 5},   # different player
    ])
    out = B.flag_superseded_reports(frame)
    assert out.is_superseded.tolist() == [True, False, False, False]
    assert out.superseded_by.iloc[0] == "c1"


def test_unpriced_or_staff_rows_are_never_merged():
    frame = _resolved([
        {"date": "2024-12-01", "player": "A"}, {"date": "2024-12-05", "player": "A"},  # two 10-day deals, no value
        {"date": "2024-12-01", "player": "Coach", "total_value_musd": 16.0, "total_seasons": 5, "is_coach_or_exec": True},
        {"date": "2024-12-03", "player": "Coach", "total_value_musd": 16.0, "total_seasons": 5, "is_coach_or_exec": True},
    ])
    assert not B.flag_superseded_reports(frame).is_superseded.any()


def test_priced_deals_without_a_player_id_still_get_a_flat_schedule():
    frame = _resolved([
        {"date": "2025-07-01", "player": "Rookie", "total_value_musd": 20.0, "total_seasons": 4, "aav_musd": 5.0},
        {"date": "2025-07-01", "player": "Unpriced", "total_seasons": 4},                       # no price: stays out
        {"date": "2025-07-01", "player": "Coach", "total_value_musd": 16.0, "total_seasons": 5, "aav_musd": 3.2, "is_coach_or_exec": True},
        {"date": "2025-07-01", "player": "Known", "player_id": 7.0, "total_seasons": 2},        # ID but no price: unchanged behavior
    ])
    frame["option_type"] = None
    seasons = B.expand_seasons(frame)
    assert set(seasons.contract_id) == {"c0", "c3"}
    assert len(seasons[seasons.contract_id == "c0"]) == 4 and seasons.loc[seasons.contract_id == "c0", "aav_naive_musd"].eq(5.0).all()


def test_unmatched_player_id_never_borrows_another_players_salary():
    seasons = pd.DataFrame({"contract_id": ["a"], "player_id": [np.nan], "season_end_year": [2026], "aav_naive_musd": [5.0]})
    modern = pd.DataFrame({"PLAYER_ID": [np.nan], "year": [2026], "salary": [30_000_000.0], "AGE": [25], "Pos": ["PG"], "DRAFT_YEAR": [2020]})
    out = B.attach_modern_salary(seasons, modern)
    assert out.aav_source.iloc[0] == "estimated_flat" and out.aav_final_musd.iloc[0] == 5.0


MODERN = pd.DataFrame({"PLAYER_NAME": ["OG Anunoby", "Larry Drew II", "Jalen Johnson"], "PLAYER_ID": [1628384, 203580, 1630552]})
INDEX = pd.DataFrame({
    "player": ["Nathan Mensah", "Phil Jackson", "Phil Jackson", "Kevin Jones", "Kevin Jones"],
    "nba_id": [1641877, 111, 222, 333, 444], "year": [2024, 1975, 2020, 2019, 2021],
})


def _match(name):
    matched, unmatched = X.build_crosswalk([name], MODERN, INDEX)
    row = (matched if len(matched) else unmatched).iloc[0]
    return row.method, row.player_id


def test_slash_variants_are_normalized_individually():
    assert _match("Ogugua Anunoby / O.G. Anunoby") == ("normalized_variant", 1628384)


def test_generational_marker_is_not_stripped():
    method, pid = _match("Larry Drew (I)")  # the father; Larry Drew II is a different person
    assert pd.isna(pid)


def test_index_master_fallback_is_unique_and_recent_only():
    assert _match("Nathan Mensah") == ("index_master", 1641877)
    assert _match("Phil Jackson") == ("index_master", 222)  # the 1975 namesake is out of range, never picked
    assert pd.isna(_match("Kevin Jones")[1])  # two recent candidates: ambiguity is not guessed


def test_appearance_status_distinguishes_unused_unmatched_and_not_yet_played_cohorts():
    index = pd.DataFrame({"player": ["Played Guy", "Other Guy"], "nba_id": [1, 2], "year": [2025, 2025]})
    frame = _resolved([
        {"date": "2024-09-20", "player": "Played Guy", "player_id": 1.0},   # cohort 2025, has a game row
        {"date": "2024-09-20", "player": "Other Guy", "player_id": 3.0},    # matched id, no game row
        {"date": "2024-09-20", "player": "Camp Nobody"},                    # unmatched name, never in index
        {"date": "2024-09-20", "player": "Other Guy"},                      # unmatched id but the name is a known player
        {"date": "2025-09-20", "player": "Camp Nobody"},                    # cohort 2026 is beyond index_master: unknown
    ])
    assert B.attach_nba_appearance(frame, index).nba_appearance.tolist() == [
        "appeared", "no_game_that_season", "never_appeared", "unknown", "unknown"]


# ---- Spotrac forward schedule ----------------------------------------------------------------

def _spotrac_files(tmp_path):
    salaries = pd.DataFrame({
        "Player": ["Star One", "Star One", "Gary Trent", "Ghost WAIVED"], "spotrac_id": [1, 1, 2, 3], "nba_id": [10, 10, np.nan, np.nan],
        "Team": ["AAA", "BBB", "CCC", "DDD"],
        "2026-27": [40e6, 6e6, 15e6, 21e6], "2027-28": [42e6, 0, 16e6, 21e6], "2025-26": [np.nan] * 4,
    })
    # options file in a DIFFERENT row order, with dead money for the waived player
    options = pd.DataFrame({"Player": ["Gary Trent", "Ghost WAIVED", "Star One", "Star One"], "Team": ["CCC", "DDD", "BBB", "AAA"],
                            "2026-27": ["0", "D", "0", "0"], "2027-28": ["T", "D", "0", "P"]})
    sal_path, opt_path = tmp_path / "s.csv", tmp_path / "o.csv"
    salaries.to_csv(sal_path, index=False)
    options.to_csv(opt_path, index=False)
    return str(sal_path), str(opt_path)


def test_spotrac_options_join_on_player_and_team_and_dead_money_is_dropped(tmp_path):
    sal, opt = _spotrac_files(tmp_path)
    long = B.load_spotrac_schedule(sal, opt, min_rows_per_season=1).set_index(["Player", "Team", "season_end_year"])
    assert ("Ghost WAIVED", "DDD", 2027) not in long.index                      # dead money never prices a signing
    assert ("Star One", "BBB", 2028) not in long.index                          # zero salary dropped
    assert long.loc[("Star One", "AAA", 2028), "spot_option"] == "P"            # options file is in a different row order
    assert long.loc[("Gary Trent", "CCC", 2028), "spot_option"] == "T"
    assert pd.isna(long.loc[("Star One", "AAA", 2027), "spot_option"])
    assert long.loc[("Gary Trent", "CCC", 2027), "spot_salary_musd"] == 15.0
    assert set(long.index.get_level_values("season_end_year")) == {2027, 2028}  # the stray empty 2025-26 column is ignored


def _long(rows):
    return pd.DataFrame(rows, columns=["spot_uid", "Player", "nba_id", "Team", "season_end_year", "spot_salary_musd", "spot_option", "name_key"])


def _seasons(rows):
    base = {"player": "A", "player_id": np.nan, "team_abbr": "AAA", "aav_source": "estimated_flat", "aav_final_musd": 10.0,
            "aav_realized_musd": np.nan, "season_end_year": 2027, "season_index": 1}
    frame = pd.DataFrame([{**base, **row} for row in rows]).reset_index(drop=True)
    frame["season_index"] = frame.groupby("contract_id").cumcount() + 1
    return frame


def _events(rows):
    return pd.DataFrame(rows, columns=["contract_id", "date", "is_superseded"])


def test_latest_event_claims_the_spotrac_season_and_older_ones_keep_their_estimate():
    spot = _long([("S|AAA", "Star", 10, "AAA", 2027, 42.0, "P", "star"), ("S|AAA", "Star", 10, "AAA", 2028, 44.0, None, "star")])
    final = _seasons([
        {"contract_id": "old", "player_id": 10.0, "season_end_year": 2027, "aav_final_musd": 30.0},
        {"contract_id": "old", "player_id": 10.0, "season_end_year": 2028, "aav_final_musd": 30.0},
        {"contract_id": "new", "player_id": 10.0, "season_end_year": 2028, "aav_final_musd": 40.0},
        {"contract_id": "real", "player_id": 10.0, "season_end_year": 2027, "aav_source": "realized", "aav_final_musd": 5.0},
    ])
    out = B.attach_spotrac_schedule(final, _events([("old", "2024-07-01", False), ("new", "2026-07-01", False), ("real", "2025-07-01", False)]), spot)
    by = {(r.contract_id, r.season_end_year): r for r in out.itertuples()}
    assert by[("old", 2027)].aav_source == "spotrac_schedule" and by[("old", 2027)].aav_final_musd == 42.0
    assert by[("old", 2027)].spotrac_option == "P"
    assert by[("new", 2028)].aav_final_musd == 44.0                                  # latest event claims 2028
    assert by[("old", 2028)].aav_final_musd == 30.0 and by[("old", 2028)].season_claimed_by_later_deal
    assert by[("real", 2027)].aav_source == "realized" and by[("real", 2027)].aav_final_musd == 5.0  # realized is never overwritten


def test_superseded_reports_cannot_claim_and_name_only_mismatches_are_rejected():
    spot = _long([("T|CCC", "Gary Trent", np.nan, "CCC", 2027, 15.0, None, "gary trent")])
    final = _seasons([
        {"contract_id": "dup", "player": "Gary Trent Jr.", "team_abbr": "CCC", "aav_final_musd": 14.0},
        {"contract_id": "real", "player": "Gary Trent Jr.", "team_abbr": "CCC", "aav_final_musd": 14.5},
        {"contract_id": "other_team", "player": "Gary Trent Jr.", "team_abbr": "ZZZ", "aav_final_musd": 3.0},   # other team, 5x off
        {"contract_id": "far_unknown", "player": "Gary Trent Jr.", "team_abbr": "ZZZ", "aav_source": "unknown", "aav_final_musd": np.nan},
    ])
    out = B.attach_spotrac_schedule(final, _events([("dup", "2026-07-01", True), ("real", "2026-07-05", False), ("other_team", "2026-07-06", False), ("far_unknown", "2026-07-07", False)]), spot).set_index("contract_id")
    assert out.loc["real", "aav_source"] == "spotrac_schedule"
    assert out.loc["dup", "aav_source"] == "estimated_flat" and out.loc["dup", "season_claimed_by_later_deal"]
    assert out.loc["other_team", "aav_source"] == "estimated_flat"                                          # name-only, other team, not close
    assert out.loc["far_unknown", "aav_source"] == "unknown"                                                # no estimate to sanity-check against


def test_missing_spotrac_input_leaves_flat_estimates_untouched():
    final = _seasons([{"contract_id": "c", "player_id": 10.0}])
    out = B.attach_spotrac_schedule(final, _events([("c", "2026-07-01", False)]), None)
    assert out.aav_source.tolist() == ["estimated_flat"] and out.spotrac_option.isna().all()


def test_nba_id_shared_by_differently_named_spotrac_rows_is_not_trusted(tmp_path):
    salaries = pd.DataFrame({
        "Player": ["Damian Lillard", "Cole Anthony WAIVED", "Solo Guy"], "spotrac_id": [5, 0, 6], "nba_id": [203081, 203081, 77],
        "Team": ["POR", "MEM", "AAA"], "2026-27": [13e6, 3.7e6, 4e6], "2027-28": [14e6, 3.7e6, 4e6],
    })
    options = pd.DataFrame({"Player": salaries.Player, "Team": salaries.Team, "2026-27": ["0", "0", "0"], "2027-28": ["0", "0", "0"]})
    salaries.to_csv(tmp_path / "s.csv", index=False)
    options.to_csv(tmp_path / "o.csv", index=False)
    long = B.load_spotrac_schedule(str(tmp_path / "s.csv"), str(tmp_path / "o.csv"), min_rows_per_season=1)
    ids = long.groupby("Player").nba_id.first()
    assert pd.isna(ids["Damian Lillard"]) and pd.isna(ids["Cole Anthony WAIVED"])   # the shared id is dropped for both
    assert ids["Solo Guy"] == 77                                                      # unshared ids still match by ID


def test_through_year_anchors_guaranteed_years_only_when_options_follow():
    rookie = B.parse_notes_advanced("signed first round pick to a 2-year, $8.7M contract through 2020-21 with 1-year team options for 2021-22, 2022-23")
    assert rookie["calendar_end_basis"] == "guaranteed_years" and rookie["option_year"] == "2022-23"   # last option season, not the through-year
    plain = B.parse_notes_advanced("signed first round pick to a 4-year $17.1M contract through 2025-26")
    assert plain["calendar_end_basis"] == "full_term"

    def start(parsed, date):
        row = pd.Series({**parsed, "total_seasons": parsed["years_alt"] if parsed["years_alt"] else parsed["years"], "date": date})
        return B.derive_start_season(row)

    assert start(rookie, "2019-07-01") == 2020        # was 2018: counted 4 years back from the guaranteed-through year
    assert start(plain, "2022-07-03") == 2023         # a full-term "through" still counts the whole term back


def test_option_year_rule_does_not_leak_to_non_first_round_notes():
    note = B.parse_notes_advanced("re-signed for the remainder of the season through 2024-25 with 1-year team options for 2025-26, 2026-27")
    assert note["option_year"] == "2024-25"           # unchanged: their modeled term excludes the option seasons


def test_extension_seasons_are_priced_from_their_own_terms_not_the_old_contracts_pay():
    final = pd.DataFrame({
        "contract_id": ["ext", "ext", "ext_no_terms", "plain"], "season_index": [1, 2, 1, 1],
        "aav_naive_musd": [38.6, 38.6, np.nan, 5.0], "aav_realized_musd": [13.5, 34.0, 9.0, 5.5],
        "aav_final_musd": [13.5, 34.0, 9.0, 5.5], "aav_source": ["realized"] * 4,
    })
    resolved = pd.DataFrame({"contract_id": ["ext", "ext_no_terms", "plain"], "is_extension": [True, True, False], "aav_musd": [38.6, np.nan, 5.0]})
    out = B.price_extensions_from_terms(final, resolved)
    assert out.aav_final_musd.tolist() == [38.6, 38.6, 9.0, 5.5]                 # only the priced extension changes
    assert out.aav_source.tolist() == ["contract_terms", "contract_terms", "realized", "realized"]   # by design, not a fallback
    assert out.aav_realized_musd.tolist() == [13.5, 34.0, 9.0, 5.5]              # realized stays for reference
    assert out.realized_overridden_by_terms.tolist() == [True, True, False, False]
