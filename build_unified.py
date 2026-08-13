"""
Builds a unified, season-level contract-terms dataset from:
  - psx_contracts_v2_2014_present.csv (PST signing events, re-parsed with
    the corrected regex from psx_contracts_v2.py)
  - crosswalk_matched.csv (player string -> modern.csv PLAYER_ID)
  - modern.csv (real per-season salary, fetched fresh from GitHub)

Output: two files.
  contracts_resolved.csv  -- one row per signing event, with player_id
                             attached. This is Layer 2 from our design
                             discussion.
  contract_seasons.csv    -- one row per (contract, season), expanded
                             from contracts_resolved. This is Layer 3.

Season numbering convention (confirmed against real SALARY_CAP figures):
modern.csv's `year` column is the season's END year (year=2014 means the
2013-14 season). A contract's first season is derived from its signing
date: month >= July -> upcoming season, ending next calendar year
(signing_year + 1). Month <= June -> the season already in progress,
ending this calendar year (signing_year).

Per the 2026-08 design discussion: we are NOT trying to reconstruct
whether an option was actually exercised/declined, or precisely link
extensions to prior contracts' true end dates. We only care about the
STRUCTURAL terms as signed (years, option existence/type). Season
expansion here is a simple date-anchored fan-out -- known to be
imprecise at the boundary for early-signed rookie extensions, accepted
as a limitation rather than solved.

Total season count per contract = years_alt if present, else years.
Option (if any) is assumed to apply to the FINAL season only. This is
based on the 3 real examples we validated by hand (Mitchell Robinson,
Ron Harper Jr., Jordan Walsh) -- not proven for every possible case. To
catch violations of this assumption, we cross-check: does the computed
final season's end-year match the independently-parsed option_year? If
not, `option_year_mismatch` is flagged True for manual review rather
than silently trusting either figure.
"""

from __future__ import annotations

import hashlib
import re
import sys
import pandas as pd

sys.path.insert(0, ".")
from psx_contracts_v2 import parse_notes  # re-use the validated parser

MODERN_CSV_URL = "https://raw.githubusercontent.com/gabriel1200/player_sheets/refs/heads/master/year_totals/modern.csv"


def derive_start_season(date_str: str) -> int:
    """Returns the END year of the season this signing's contract begins in."""
    d = pd.Timestamp(date_str)
    return d.year + 1 if d.month >= 7 else d.year


def make_contract_id(date: str, team: str, player: str, raw_notes: str) -> str:
    """Stable across re-scrapes, unlike a row index: same signing event
    (same date/team/player/notes text) always hashes to the same id, so
    contract_id can be used to diff or incrementally merge across runs.

    date/team/player alone are NOT sufficient -- confirmed against real
    data that a handful of players have two genuinely different signings
    on the same date with the same team (e.g. re-signed twice same day
    with different terms, or a two-way + a separate Exhibit 10 signing
    same day). raw_notes is required to tell these apart.

    Notes text is normalized (lowercased, whitespace-collapsed) before
    hashing so a trivial formatting change doesn't churn the id for what
    is otherwise the same real-world event, while still preserving full
    disambiguating power for genuinely different notes text.

    Uses hashlib rather than Python's built-in hash() since the latter is
    randomized per-process (PYTHONHASHSEED) for strings and would NOT be
    stable even within a single script's re-run, let alone across runs.
    Truncated to 12 hex chars -- negligible collision risk at this scale
    (thousands of rows, not billions) while staying short enough to be
    usable as a human-scannable key."""
    normalized_notes = re.sub(r"\s+", " ", raw_notes.strip().lower())
    key = f"{date}|{team}|{player}|{normalized_notes}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def build_resolved_contracts(contracts_path: str, crosswalk_path: str) -> pd.DataFrame:
    contracts = pd.read_csv(contracts_path)
    crosswalk = pd.read_csv(crosswalk_path)

    # Confirmed via real data: a handful of rows are exact duplicates
    # (same date/team/player/notes) -- likely pagination overlap during
    # scraping (a row landing on two consecutive page fetches if the
    # site's data shifted mid-scrape). Invisible under the old row-index
    # contract_id scheme; visible now that contract_id is a content hash.
    before = len(contracts)
    contracts = contracts.drop_duplicates(subset=["date", "team", "player", "raw_notes"])
    if before != len(contracts):
        print(f"NOTE: dropped {before - len(contracts)} duplicate contract "
              f"rows (identical date/team/player/notes).", file=sys.stderr)

    # Re-parse raw_notes with the CURRENT parse_notes -- the source CSV may
    # have been generated with an earlier version of the regex (e.g.
    # before the comma-variant and parenthetical-option fixes).
    reparsed = contracts["raw_notes"].apply(parse_notes).apply(pd.Series)
    reparsed["parse_warnings"] = reparsed["parse_warnings"].apply(lambda ws: "; ".join(ws))

    base = contracts[["date", "team", "player", "raw_notes"]].copy()
    resolved = pd.concat([base, reparsed], axis=1)

    resolved["contract_id"] = resolved.apply(
        lambda r: make_contract_id(r["date"], r["team"], r["player"], r["raw_notes"]), axis=1
    )

    xwalk = crosswalk[["player_string", "player_id", "matched_name", "method"]].rename(
        columns={"method": "crosswalk_method"}
    )
    resolved = resolved.merge(
        xwalk, left_on="player", right_on="player_string", how="left"
    ).drop(columns=["player_string"])

    resolved["start_season"] = resolved["date"].apply(derive_start_season)
    resolved["total_seasons"] = resolved["years_alt"].fillna(resolved["years"])

    return resolved


def expand_seasons(resolved: pd.DataFrame) -> pd.DataFrame:
    """One row per (contract, season). Only expands rows where we know
    both player_id (for the modern.csv join) and total_seasons (for the
    fan-out) -- rows missing either are simply absent from this output,
    not silently zero-filled. They're still present in contracts_resolved.csv."""
    rows = []
    expandable = resolved[resolved.player_id.notna() & resolved.total_seasons.notna()]

    for _, r in expandable.iterrows():
        n = int(r.total_seasons)
        for i in range(1, n + 1):
            season_end_year = int(r.start_season) + (i - 1)
            is_final = i == n
            row = {
                "contract_id": r.contract_id,
                "player": r.player,
                "player_id": r.player_id,
                "team": r.team,
                "season_index": i,
                "season_end_year": season_end_year,
                "is_option_season": bool(is_final and pd.notna(r.option_type)),
                "option_type": r.option_type if (is_final and pd.notna(r.option_type)) else None,
                "aav_naive_musd": r.aav_musd,
            }
            rows.append(row)

    seasons = pd.DataFrame(rows)

    # Cross-check: for option seasons, does the computed end-year match
    # the independently-parsed option_year text? E.g. option_year "2029-30"
    # should have season_end_year == 2030.
    def check_mismatch(row):
        if not row.is_option_season or not isinstance(row.option_type, str):
            return False
        opt_year_col = resolved.loc[resolved.contract_id == row.contract_id, "option_year"]
        if opt_year_col.empty or pd.isna(opt_year_col.iloc[0]):
            return False
        opt_year_str = str(opt_year_col.iloc[0])
        # "2029-30" -> end year 2030. Bare "2020" is treated the same way
        # as the FIRST number of a hyphenated pair (i.e. a season-start-
        # year shorthand), so end year = bare_year + 1 -- NOT bare_year
        # itself. (Confirmed by cross-checking against our independently
        # date-derived season_end_year on real data: treating bare years
        # as literal end-years produced systematic off-by-one mismatches
        # across every bare-year case, which is the actual signal that
        # this convention -- not the season math -- was the bug.)
        if "-" in opt_year_str:
            end_yr = int(opt_year_str.split("-")[0]) + 1
        else:
            end_yr = int(opt_year_str) + 1
        return end_yr != row.season_end_year

    seasons["option_year_mismatch"] = seasons.apply(check_mismatch, axis=1)
    return seasons


def attach_modern_salary(seasons: pd.DataFrame, modern: pd.DataFrame) -> pd.DataFrame:
    modern_slim = modern[["PLAYER_ID", "year", "salary"]].rename(
        columns={"PLAYER_ID": "player_id", "year": "season_end_year", "salary": "aav_realized_musd"}
    )

    # modern.csv itself contains exact-duplicate rows for a handful of
    # (PLAYER_ID, year) combos (confirmed: e.g. one player-season repeated
    # 64 times with identical values -- a data quality issue upstream in
    # modern.csv, not something introduced here). Left-joining against
    # duplicates silently explodes row count in the output. Since the
    # duplicates are identical in value, dropping to one row per
    # (player_id, year) loses no information.
    before = len(modern_slim)
    modern_slim = modern_slim.drop_duplicates(subset=["player_id", "season_end_year"])
    if before != len(modern_slim):
        print(f"NOTE: modern.csv had {before - len(modern_slim)} duplicate "
              f"(player_id, year) rows, deduplicated before merge.", file=sys.stderr)

    # modern.csv's salary is presumably in raw dollars -- normalize to $M
    # to match aav_naive_musd's units. Flagged as an assumption to verify.
    modern_slim["aav_realized_musd"] = modern_slim["aav_realized_musd"] / 1_000_000

    merged = seasons.merge(modern_slim, on=["player_id", "season_end_year"], how="left")
    assert len(merged) == len(seasons), (
        f"Merge changed row count ({len(seasons)} -> {len(merged)}) -- "
        "modern.csv likely has new duplicate keys not caught above."
    )

    merged["aav_final_musd"] = merged["aav_realized_musd"].combine_first(merged["aav_naive_musd"])
    merged["aav_source"] = "unknown"
    merged.loc[merged["aav_realized_musd"].notna(), "aav_source"] = "realized"
    merged.loc[merged["aav_realized_musd"].isna() & merged["aav_naive_musd"].notna(), "aav_source"] = "estimated_flat"

    return merged


def main():
    resolved = build_resolved_contracts(
        "psx_contracts_v2_2014_present.csv", "crosswalk_matched.csv"
    )
    resolved.to_csv("contracts_resolved.csv", index=False)
    print(f"contracts_resolved.csv: {len(resolved)} rows "
          f"({resolved.player_id.notna().sum()} with player_id matched)")

    seasons = expand_seasons(resolved)
    print(f"Expanded to {len(seasons)} season rows from "
          f"{resolved.total_seasons.notna().sum()} expandable contracts")

    modern = pd.read_csv(MODERN_CSV_URL, low_memory=False)
    final = attach_modern_salary(seasons, modern)
    final.to_csv("contract_seasons.csv", index=False)

    print()
    print("aav_source breakdown:")
    print(final.aav_source.value_counts())
    print()
    print(f"option_year_mismatch flagged: {final.option_year_mismatch.sum()} rows")


if __name__ == "__main__":
    main()