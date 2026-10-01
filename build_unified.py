"""
build_unified.py

Builds a unified, season-level contract-terms dataset from:
  - psx_contracts_v2_2014_present.csv (or psx_contracts_2014_present.csv)
  - crosswalk_matched.csv (player string -> modern.csv PLAYER_ID)
  - modern.csv (real per-season salary from GitHub)

Standardizes team identities upstream (team_abbr, team_id).
Output:
  - contracts_resolved.csv (one row per signing event, with player_id & team_abbr)
  - contract_seasons.csv   (one row per contract-season, expanded)
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from typing import Any, Dict, Optional, Tuple
import pandas as pd

MODERN_CSV_URL = (
    "https://raw.githubusercontent.com/gabriel1200/player_sheets/refs/heads/master/year_totals/modern.csv"
)

NBA_TEAMS = {
    "Hawks": {"abbr": "ATL", "team_id": 1610612737, "name": "Atlanta Hawks"},
    "Celtics": {"abbr": "BOS", "team_id": 1610612738, "name": "Boston Celtics"},
    "Nets": {"abbr": "BKN", "team_id": 1610612751, "name": "Brooklyn Nets"},
    "Hornets": {"abbr": "CHA", "team_id": 1610612766, "name": "Charlotte Hornets"},
    "Bulls": {"abbr": "CHI", "team_id": 1610612741, "name": "Chicago Bulls"},
    "Cavaliers": {"abbr": "CLE", "team_id": 1610612739, "name": "Cleveland Cavaliers"},
    "Mavericks": {"abbr": "DAL", "team_id": 1610612742, "name": "Dallas Mavericks"},
    "Nuggets": {"abbr": "DEN", "team_id": 1610612743, "name": "Denver Nuggets"},
    "Pistons": {"abbr": "DET", "team_id": 1610612765, "name": "Detroit Pistons"},
    "Warriors": {"abbr": "GSW", "team_id": 1610612744, "name": "Golden State Warriors"},
    "Rockets": {"abbr": "HOU", "team_id": 1610612745, "name": "Houston Rockets"},
    "Pacers": {"abbr": "IND", "team_id": 1610612754, "name": "Indiana Pacers"},
    "Clippers": {"abbr": "LAC", "team_id": 1610612746, "name": "LA Clippers"},
    "Lakers": {"abbr": "LAL", "team_id": 1610612747, "name": "Los Angeles Lakers"},
    "Grizzlies": {"abbr": "MEM", "team_id": 1610612763, "name": "Memphis Grizzlies"},
    "Heat": {"abbr": "MIA", "team_id": 1610612748, "name": "Miami Heat"},
    "Bucks": {"abbr": "MIL", "team_id": 1610612749, "name": "Milwaukee Bucks"},
    "Timberwolves": {"abbr": "MIN", "team_id": 1610612750, "name": "Minnesota Timberwolves"},
    "Pelicans": {"abbr": "NOP", "team_id": 1610612740, "name": "New Orleans Pelicans"},
    "Knicks": {"abbr": "NYK", "team_id": 1610612752, "name": "New York Knicks"},
    "Thunder": {"abbr": "OKC", "team_id": 1610612760, "name": "Oklahoma City Thunder"},
    "Magic": {"abbr": "ORL", "team_id": 1610612753, "name": "Orlando Magic"},
    "76ers": {"abbr": "PHI", "team_id": 1610612755, "name": "Philadelphia 76ers"},
    "Suns": {"abbr": "PHX", "team_id": 1610612756, "name": "Phoenix Suns"},
    "Trail Blazers": {"abbr": "POR", "team_id": 1610612757, "name": "Portland Trail Blazers"},
    "Kings": {"abbr": "SAC", "team_id": 1610612758, "name": "Sacramento Kings"},
    "Spurs": {"abbr": "SAS", "team_id": 1610612759, "name": "San Antonio Spurs"},
    "Raptors": {"abbr": "TOR", "team_id": 1610612761, "name": "Toronto Raptors"},
    "Jazz": {"abbr": "UTA", "team_id": 1610612762, "name": "Utah Jazz"},
    "Wizards": {"abbr": "WAS", "team_id": 1610612764, "name": "Washington Wizards"},
}


def parse_option_clause(text_lower: str) -> Tuple[Optional[str], Optional[str]]:
    if "early termination" in text_lower or "early termintation" in text_lower or re.search(r"\beto\b", text_lower):
        m_yr = re.search(r"(?:for\s+)?(20\d{2}(?:-\d{2,4})?)", text_lower)
        return "early_termination", (m_yr.group(1) if m_yr else None)

    m1 = re.search(
        r"(?:[;\(,]|\b)\s*(\d{4}(?:-\d{2,4})?|first|second|third|fourth|fifth|final|\d+(?:st|nd|rd|th)?)\s+(?:year\s+)?is\s+(player|team|mutual|club)\s+option",
        text_lower,
    )
    if m1:
        target, opt = m1.group(1), ("team" if m1.group(2) == "club" else m1.group(2))
        opt_year = target if re.match(r"^20\d{2}", target) else None
        return opt, opt_year

    m2 = re.search(
        r"(?:includes\s+)?(player|team|mutual|club)\s+option\s+(?:for\s+)?(20\d{2}(?:-\d{2,4})?)",
        text_lower,
    )
    if m2:
        return ("team" if m2.group(1) == "club" else m2.group(1)), m2.group(2)

    m3 = re.search(
        r"with\s+(?:a\s+|two\s+)?(?:\d+[ -]year(?:s)?\s+)?(?:[\$\d\.,\w]+\s+)?(player|team|mutual|club)\s+options?",
        text_lower,
    )
    if m3:
        opt_type = "team" if m3.group(1) == "club" else m3.group(1)
        m_yr = re.search(r"(?:for|through)\s+(20\d{2}(?:-\d{2,4})?)", text_lower)
        return opt_type, (m_yr.group(1) if m_yr else None)

    if "team opion" in text_lower or "team's option" in text_lower or "club option" in text_lower:
        return "team", None
    m4 = re.search(r"\b(player|team|mutual)\s+options?\b", text_lower)
    if m4:
        return m4.group(1), None

    return None, None


def parse_notes_advanced(raw_note: str) -> Dict[str, Any]:
    note = str(raw_note).strip()
    nl = note.lower()
    warnings = []

    is_extension = bool(re.search(r"\bextension\b", nl))
    is_resigning = bool(re.search(r"\bre-signed\b", nl))
    is_qualifying_offer = bool(re.search(r"qualifying offer", nl))
    is_non_guaranteed = bool(re.search(r"non-guaranteed|partially guaranteed", nl))
    is_exhibit10 = bool(re.search(r"exhibit\s*10", nl))
    is_two_way = bool(re.search(r"two\s*-?\s*way", nl))
    is_coach_or_exec = bool(re.search(r"coach|general manager|president of basketball|executive", nl))
    has_contract_word = bool(re.search(r"contract|deal|extension|offer sheet|agreement|signed|10-day|two-way", nl))

    years: Optional[float] = None
    years_alt: Optional[float] = None
    total_value_musd: Optional[float] = None
    aav_musd: Optional[float] = None
    calendar_end_year: Optional[int] = None

    option_type, option_year = parse_option_clause(nl)

    if "first round pick" in nl:
        years = 2.0
        years_alt = 4.0
        if not option_type:
            option_type = "team"
    else:
        cleaned_nl = re.sub(r'with\s+(?:a\s+|two\s+)?(?:\d+[ -]year(?:s)?\s+)?(?:[\$\d\.,\w]+\s+)?(?:player|team|mutual|club)\s+options?', '', nl)
        cleaned_nl = re.sub(r'includes\s+(?:\d+[ -]year(?:s)?\s+)?(?:player|team|mutual|club)\s+option', '', cleaned_nl)

        slash_match = re.search(r'(\d+)[ -]year\s*(?:contract|deal)?\s*/\s*(\d+)[ -]year', cleaned_nl)
        if slash_match:
            years = float(slash_match.group(1))
            years_alt = float(slash_match.group(2))
        else:
            single_year = re.search(r'(\d+)[ -]year', cleaned_nl)
            if single_year:
                years = float(single_year.group(1))
            elif is_exhibit10:
                years = 1.0
            elif re.search(r"remainder of (?:the )?season|rest of (?:the )?season|rest-of-season", cleaned_nl):
                years = 1.0

    m_through = re.search(r"through\s+(20\d{2}(?:-\d{2,4})?)", nl)
    if m_through:
        yr_str = m_through.group(1)
        calendar_end_year = int(yr_str.split("-")[0]) + 1 if "-" in yr_str else int(yr_str)
    elif option_year and re.match(r"^20\d{2}", option_year):
        opt_str = option_year
        calendar_end_year = int(opt_str.split("-")[0]) + 1 if "-" in opt_str else int(opt_str) + 1

    def to_m(val: str, unit: Optional[str]) -> float:
        v = float(val)
        u = (unit or "m").lower()
        return v * 1000.0 if u == "b" else v if u == "m" else v / 1000.0

    slash_money = re.search(r"\$([0-9.]+)\s*([MKBmbk])?\s*/\s*\$([0-9.]+)\s*([MKBmbk])?", note)
    if slash_money:
        total_value_musd = to_m(slash_money.group(1), slash_money.group(2))
    else:
        m_dollars = re.findall(r"\$([0-9.]+)\s*([MKBmbk])?", note)
        if m_dollars:
            total_value_musd = to_m(m_dollars[0][0], m_dollars[0][1])

    total_seasons = years_alt if years_alt is not None else years
    if total_value_musd is not None and total_seasons:
        aav_musd = round(total_value_musd / total_seasons, 3)
        warnings.append(
            "aav_musd is naive (total/years); does not reflect actual year-by-year raises. "
            "Cross-reference Spotrac for real schedule."
        )
    elif years is None and total_value_musd is None:
        warnings.append("years/value pattern not matched")

    return {
        "is_extension": is_extension,
        "is_resigning": is_resigning,
        "is_qualifying_offer": is_qualifying_offer,
        "is_non_guaranteed": is_non_guaranteed,
        "is_exhibit10": is_exhibit10,
        "is_two_way": is_two_way,
        "is_coach_or_exec": is_coach_or_exec,
        "has_contract_word": has_contract_word,
        "years": years,
        "years_alt": years_alt,
        "total_value_musd": total_value_musd,
        "aav_musd": aav_musd,
        "option_type": option_type,
        "option_year": option_year,
        "calendar_end_year": calendar_end_year,
        "parse_warnings": "; ".join(warnings),
    }


def make_contract_id(date: str, team: str, player: str, raw_notes: str) -> str:
    normalized_notes = re.sub(r"\s+", " ", str(raw_notes).strip().lower())
    key = f"{date}|{team}|{player}|{normalized_notes}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def derive_start_season(row: pd.Series) -> int:
    total = row.get("total_seasons")
    cal_end = row.get("calendar_end_year")
    if pd.notna(total) and pd.notna(cal_end) and float(total) > 0:
        return int(cal_end - float(total) + 1)
    d = pd.Timestamp(row["date"])
    return d.year + 1 if d.month >= 7 else d.year


def build_resolved_contracts(contracts_path: str, crosswalk_path: str) -> pd.DataFrame:
    contracts = pd.read_csv(contracts_path)
    crosswalk = pd.read_csv(crosswalk_path)

    before = len(contracts)
    contracts = contracts.drop_duplicates(subset=["date", "team", "player", "raw_notes"])
    if before != len(contracts):
        print(f"NOTE: dropped {before - len(contracts)} duplicate rows.", file=sys.stderr)

    reparsed = contracts["raw_notes"].apply(parse_notes_advanced).apply(pd.Series)
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

    resolved["total_seasons"] = resolved["years_alt"].fillna(resolved["years"])
    resolved["start_season"] = resolved.apply(derive_start_season, axis=1)

    # Attach standardized team identity
    resolved["team_abbr"] = resolved["team"].map(lambda t: NBA_TEAMS.get(t, {}).get("abbr"))
    resolved["team_id"] = resolved["team"].map(lambda t: NBA_TEAMS.get(t, {}).get("team_id"))

    return resolved


def expand_seasons(resolved: pd.DataFrame) -> pd.DataFrame:
    rows = []
    expandable = resolved[resolved.player_id.notna() & resolved.total_seasons.notna()]

    for _, r in expandable.iterrows():
        n = int(r.total_seasons)
        for i in range(1, n + 1):
            season_end_year = int(r.start_season) + (i - 1)
            is_final = (i == n)
            rows.append({
                "contract_id": r.contract_id,
                "player": r.player,
                "player_id": r.player_id,
                "team": r.team,
                "team_abbr": r.get("team_abbr"),
                "team_id": r.get("team_id"),
                "season_index": i,
                "season_end_year": season_end_year,
                "is_option_season": bool(is_final and pd.notna(r.option_type)),
                "option_type": r.option_type if (is_final and pd.notna(r.option_type)) else None,
                "aav_naive_musd": r.aav_musd,
            })

    seasons = pd.DataFrame(rows)

    def check_mismatch(row: pd.Series) -> bool:
        if not row.is_option_season or not isinstance(row.option_type, str):
            return False
        opt_col = resolved.loc[resolved.contract_id == row.contract_id, "option_year"]
        if opt_col.empty or pd.isna(opt_col.iloc[0]):
            return False
        opt_str = str(opt_col.iloc[0])
        end_yr = int(opt_str.split("-")[0]) + 1 if "-" in opt_str else int(opt_str) + 1
        return end_yr != row.season_end_year

    seasons["option_year_mismatch"] = seasons.apply(check_mismatch, axis=1)
    return seasons


def attach_modern_salary(seasons: pd.DataFrame, modern: pd.DataFrame) -> pd.DataFrame:
    modern_slim = modern[["PLAYER_ID", "year", "salary"]].rename(
        columns={"PLAYER_ID": "player_id", "year": "season_end_year", "salary": "aav_realized_musd"}
    )
    modern_slim = modern_slim.drop_duplicates(subset=["player_id", "season_end_year"])
    modern_slim["aav_realized_musd"] = modern_slim["aav_realized_musd"] / 1_000_000.0

    merged = seasons.merge(modern_slim, on=["player_id", "season_end_year"], how="left")
    merged["aav_final_musd"] = merged["aav_realized_musd"].combine_first(merged["aav_naive_musd"])
    merged["aav_source"] = "unknown"
    merged.loc[merged["aav_realized_musd"].notna(), "aav_source"] = "realized"
    merged.loc[merged["aav_realized_musd"].isna() & merged["aav_naive_musd"].notna(), "aav_source"] = "estimated_flat"

    return merged


def main():
    contracts_file = (
        "psx_contracts_v2_2014_present.csv"
        if os.path.exists("psx_contracts_v2_2014_present.csv")
        else "psx_contracts_2014_present.csv"
    )
    crosswalk_file = "crosswalk_matched.csv"

    print(f"Loading raw inputs from {contracts_file}...")
    resolved = build_resolved_contracts(contracts_file, crosswalk_file)
    resolved.to_csv("contracts_resolved.csv", index=False)
    print(f"Saved contracts_resolved.csv: {len(resolved)} rows")

    seasons = expand_seasons(resolved)
    print(f"Expanded to {len(seasons)} contract-seasons")

    print(f"Fetching modern per-season salaries from {MODERN_CSV_URL}...")
    try:
        modern = pd.read_csv(MODERN_CSV_URL, low_memory=False)
        final = attach_modern_salary(seasons, modern)
    except Exception as e:
        print("Could not fetch remote modern.csv, falling back to local naive values:", e)
        final = seasons
        final["aav_realized_musd"] = None
        final["aav_final_musd"] = final["aav_naive_musd"]
        final["aav_source"] = "estimated_flat"

    final.to_csv("contract_seasons.csv", index=False)
    print(f"Saved contract_seasons.csv: {len(final)} rows")


if __name__ == "__main__":
    main()