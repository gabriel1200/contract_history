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
import numpy as np
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
    "Blazers": {"abbr": "POR", "team_id": 1610612757, "name": "Portland Trail Blazers"},
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
        # "through 2020-21 with 1-year team options for 2021-22, 2022-23": "through" marks the last
        # GUARANTEED season, so the option seasons are the years listed after the option phrase and
        # the final one is the last season of the term.
        after_option = text_lower[m3.end():]
        # Only first-round picks: their modeled term (years_alt) already includes the option seasons.
        listed = (re.findall(r"20\d{2}-\d{2,4}", after_option)
                  if "first round pick" in text_lower and re.match(r"\s*(?:for|,|;|\()", after_option) else [])
        if listed:
            return opt_type, listed[-1]
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

    # "through YYYY-YY" names the last GUARANTEED season (it anchors `years`, not the option-inclusive
    # `years_alt`); an option year anchors the end of the whole term.
    calendar_end_basis: Optional[str] = None
    m_through = re.search(r"through\s+(20\d{2}(?:-\d{2,4})?)", nl)
    if m_through:
        yr_str = m_through.group(1)
        calendar_end_year = int(yr_str.split("-")[0]) + 1 if "-" in yr_str else int(yr_str)
        # With options in the same note, "through" ends the guaranteed years (the options follow);
        # with none, it ends the whole term.
        calendar_end_basis = "guaranteed_years" if "option" in nl else "full_term"
    elif option_year and re.match(r"^20\d{2}", option_year):
        opt_str = option_year
        calendar_end_year = int(opt_str.split("-")[0]) + 1 if "-" in opt_str else int(opt_str) + 1
        calendar_end_basis = "full_term"

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
        "calendar_end_basis": calendar_end_basis,
        "parse_warnings": "; ".join(warnings),
    }


def make_contract_id(date: str, team: str, player: str, raw_notes: str) -> str:
    normalized_notes = re.sub(r"\s+", " ", str(raw_notes).strip().lower())
    key = f"{date}|{team}|{player}|{normalized_notes}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def derive_start_season(row: pd.Series) -> int:
    total = row.get("total_seasons")
    cal_end = row.get("calendar_end_year")
    if row.get("calendar_end_basis") == "guaranteed_years" and pd.notna(row.get("years")) and pd.notna(cal_end) and float(row["years"]) > 0:
        return int(cal_end - float(row["years"]) + 1)
    if pd.notna(total) and pd.notna(cal_end) and float(total) > 0:
        return int(cal_end - float(total) + 1)
    d = pd.Timestamp(row["date"])
    return d.year + 1 if d.month >= 7 else d.year


DUPLICATE_REPORT_WINDOW_DAYS = 60


def flag_superseded_reports(resolved: pd.DataFrame) -> pd.DataFrame:
    """Mark earlier copies of the same signing reported more than once.

    PST often logs an agreement and then the formal signing (or a later re-report), each
    with identical terms. When the same player (ID, else name), team, stated total value
    and term recur within DUPLICATE_REPORT_WINDOW_DAYS, every report except the latest is
    flagged ``is_superseded`` and points at the kept report via ``superseded_by``. Nothing
    is dropped here; consumers decide. Only priced, termed player deals are considered, so
    10-day and bare camp signings (no value) are never merged.
    """
    flag = lambda column: resolved[column].fillna(False).astype(bool)
    eligible = resolved.total_value_musd.notna() & resolved.total_seasons.notna() & flag("has_contract_word") & ~flag("is_coach_or_exec")
    who = resolved.player_id.astype("string").where(resolved.player_id.notna(), "name:" + resolved.player.astype(str))
    frame = pd.DataFrame({
        "who": who, "team": resolved.team, "value": resolved.total_value_musd, "years": resolved.total_seasons,
        "date": pd.to_datetime(resolved.date), "contract_id": resolved.contract_id,
    })[eligible].sort_values("date", kind="stable")
    grouped = frame.groupby(["who", "team", "value", "years"], sort=False)
    superseded = (grouped.date.shift(-1) - frame.date).dt.days.le(DUPLICATE_REPORT_WINDOW_DAYS)
    resolved["is_superseded"] = False
    resolved.loc[superseded[superseded].index, "is_superseded"] = True
    resolved["superseded_by"] = None
    resolved.loc[superseded[superseded].index, "superseded_by"] = grouped.contract_id.shift(-1)[superseded]
    return resolved


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
    resolved = flag_superseded_reports(resolved)

    # Attach standardized team identity
    resolved["team_abbr"] = resolved["team"].map(lambda t: NBA_TEAMS.get(t, {}).get("abbr"))
    resolved["team_id"] = resolved["team"].map(lambda t: NBA_TEAMS.get(t, {}).get("team_id"))

    return resolved


def expand_seasons(resolved: pd.DataFrame) -> pd.DataFrame:
    rows = []
    # A schedule needs a term and a price, not a player ID: the ID is only used later to join
    # realized salary. Events whose crosswalk failed (new rookies, slash-style names) but that
    # state a value still get their reported-term fan-out at the flat estimate
    # (aav_source = "estimated_flat"). Coach/executive deals and unpriced no-ID events stay out.
    is_staff = resolved["is_coach_or_exec"].fillna(False).astype(bool)
    priced_without_id = resolved.player_id.isna() & resolved.aav_musd.notna() & ~is_staff
    expandable = resolved[resolved.total_seasons.notna() & (resolved.player_id.notna() | priced_without_id)]

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
                "prior_team_seasons": r.get("prior_team_seasons"),
                "current_team_stint_seasons": r.get("current_team_stint_seasons"),
                "prior_team_tenure_band": r.get("prior_team_tenure_band"),
                "current_team_stint_band": r.get("current_team_stint_band"),
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
    modern_slim = modern[["PLAYER_ID", "year", "salary", "AGE", "Pos", "DRAFT_YEAR"]].rename(
        columns={
            "PLAYER_ID": "player_id", "year": "season_end_year", "salary": "aav_realized_musd",
            "AGE": "age", "Pos": "position", "DRAFT_YEAR": "draft_class",
        }
    )
    modern_slim = modern_slim.drop_duplicates(subset=["player_id", "season_end_year"])
    modern_slim["aav_realized_musd"] = modern_slim["aav_realized_musd"] / 1_000_000.0

    merged = seasons.merge(modern_slim, on=["player_id", "season_end_year"], how="left")
    # Never let an unmatched (NaN) player_id pick up another row's salary or metadata.
    merged.loc[merged["player_id"].isna(), ["aav_realized_musd", "age", "position", "draft_class"]] = None
    merged["aav_final_musd"] = merged["aav_realized_musd"].combine_first(merged["aav_naive_musd"])
    merged["aav_source"] = "unknown"
    merged.loc[merged["aav_realized_musd"].notna(), "aav_source"] = "realized"
    merged.loc[merged["aav_realized_musd"].isna() & merged["aav_naive_musd"].notna(), "aav_source"] = "estimated_flat"

    return merged


SPOTRAC_YEAR_COLUMN = re.compile(r"^\d{4}-\d{2}$")
SPOTRAC_NAME_VALUE_TOLERANCE = 0.35  # a name-only match on another team must price within 35% of our own estimate


def load_spotrac_schedule(salaries_path: str, options_path: str, min_rows_per_season: int = 50) -> pd.DataFrame:
    """Long-format Spotrac forward schedule: one row per player-team-season with a salary.

    The two exports are NOT row-aligned, so options are joined on player+team. Zero salaries
    and dead money (option code ``D``: waived or stretched players) are dropped because they
    cannot price a new signing. ``spot_uid`` identifies a Spotrac player-team row.
    """
    from psx_crosswalk import normalize_name

    sal = pd.read_csv(salaries_path, low_memory=False)
    opt = pd.read_csv(options_path, low_memory=False)
    sal["spot_uid"] = sal.Player.astype(str) + "|" + sal.Team.astype(str)
    opt["spot_uid"] = opt.Player.astype(str) + "|" + opt.Team.astype(str)
    salary_cols = [c for c in sal.columns if SPOTRAC_YEAR_COLUMN.match(str(c))]
    option_cols = [c for c in opt.columns if SPOTRAC_YEAR_COLUMN.match(str(c))]
    long = sal.melt(id_vars=["spot_uid", "Player", "spotrac_id", "nba_id", "Team"], value_vars=salary_cols, var_name="label", value_name="salary")
    codes = opt.melt(id_vars=["spot_uid"], value_vars=option_cols, var_name="label", value_name="code").drop_duplicates(["spot_uid", "label"])
    long = long.merge(codes, on=["spot_uid", "label"], how="left")
    long["code"] = long.code.astype("string").where(~long.code.astype("string").isin(["0", "<NA>"]))
    long["season_end_year"] = long.label.str[:4].astype(int) + 1
    # The exporter gives every WAIVED row placeholder spotrac_id 0, which its crosswalk maps to one
    # real player's nba_id. An id shared by several differently named rows (or on a WAIVED row)
    # is untrustworthy, so match those rows by name only.
    clean = long.Player.str.replace(r"\s+WAIVED$", "", regex=True)
    shared = long.assign(clean=clean).groupby("nba_id").clean.transform("nunique").gt(1)
    long.loc[shared | long.Player.str.contains(r"\bWAIVED$", regex=True), "nba_id"] = np.nan
    long = long[long.salary.gt(0) & ~long.code.eq("D").fillna(False).astype(bool)].copy()
    # the export carries a stray historical column; the real forward schedule starts where the data is dense
    density = long.groupby("season_end_year").size()
    long = long[long.season_end_year >= density[density >= min_rows_per_season].index.min()]
    long["spot_salary_musd"] = long.salary / 1_000_000.0
    long["name_key"] = long.Player.str.replace(r"\s+WAIVED$", "", regex=True).map(normalize_name)
    return long[["spot_uid", "Player", "nba_id", "Team", "season_end_year", "spot_salary_musd", "code", "name_key"]].rename(columns={"code": "spot_option"})


def attach_spotrac_schedule(final: pd.DataFrame, resolved: pd.DataFrame, spotrac: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Price future contract-seasons from Spotrac's schedule instead of the flat estimate.

    Only seasons with no realized salary and inside Spotrac's horizon are eligible. Matching is
    by NBA ID, else by normalized name; a name-only match must be the same team or price within
    SPOTRAC_NAME_VALUE_TOLERANCE of our own estimate. Spotrac describes only a player's CURRENT
    contract, so when several events cover the same player-season the latest unsuperseded event
    claims it; older ones keep their as-signed estimate and are marked
    ``season_claimed_by_later_deal``. Adds ``spotrac_option`` (T/P/NG/UFA/RFA at that season).
    """
    from psx_crosswalk import name_variants, normalize_name

    final = final.copy()
    final["spotrac_option"] = None
    final["season_claimed_by_later_deal"] = False
    if spotrac is None or spotrac.empty:
        return final

    meta = resolved.drop_duplicates("contract_id").set_index("contract_id")
    rows = final[final.season_end_year.ge(spotrac.season_end_year.min()) & final.aav_source.ne("realized")].copy()
    rows["row_id"] = rows.index
    rows["event_date"] = pd.to_datetime(rows.contract_id.map(meta["date"]))
    rows["event_superseded"] = rows.contract_id.map(meta["is_superseded"]).fillna(False).astype(bool) if "is_superseded" in meta else False

    spot_cols = ["spot_uid", "nba_id", "Team", "season_end_year", "spot_salary_musd", "spot_option", "name_key"]
    by_id = rows[rows.player_id.notna()].merge(
        spotrac[spotrac.nba_id.notna()][spot_cols].rename(columns={"nba_id": "spot_nba_id", "Team": "spot_team", "name_key": "spot_name"}),
        left_on=["player_id", "season_end_year"], right_on=["spot_nba_id", "season_end_year"], how="inner")
    by_id["via"] = "id"

    unmatched = rows[~rows.row_id.isin(by_id.row_id)]
    keys = pd.DataFrame({"player": unmatched.player.dropna().unique()})
    keys["name_key"] = keys.player.map(lambda p: sorted({normalize_name(v) for v in name_variants(str(p))}))
    keys = keys.explode("name_key")
    by_name = unmatched.merge(keys, on="player", how="inner").merge(
        spotrac[spot_cols].rename(columns={"nba_id": "spot_nba_id", "Team": "spot_team", "name_key": "spot_name"}),
        left_on=["name_key", "season_end_year"], right_on=["spot_name", "season_end_year"], how="inner")
    by_name["via"] = "name"

    cand = pd.concat([by_id, by_name], ignore_index=True)
    if cand.empty:
        return final
    cand["team_match"] = cand.team_abbr.eq(cand.spot_team)
    near = ((cand.spot_salary_musd - cand.aav_final_musd).abs() / cand.spot_salary_musd).le(SPOTRAC_NAME_VALUE_TOLERANCE)
    cand = cand[cand.via.eq("id") | cand.team_match | near].copy()
    # one Spotrac row per contract-season: prefer a unique candidate, else the unique same-team one
    size = cand.groupby("row_id").row_id.transform("size")
    cand = cand[size.eq(1) | cand.team_match]
    cand = cand[cand.groupby("row_id").row_id.transform("size").eq(1)]

    cand = cand.sort_values(["event_date", "contract_id"], kind="stable")
    eligible = cand[~cand.event_superseded]
    winners = eligible.drop_duplicates(["spot_uid", "season_end_year"], keep="last")
    losers = cand[~cand.row_id.isin(winners.row_id)]
    final.loc[winners.row_id, "aav_final_musd"] = winners.spot_salary_musd.values
    final.loc[winners.row_id, "aav_source"] = "spotrac_schedule"
    final.loc[winners.row_id, "spotrac_option"] = winners.spot_option.values
    final.loc[losers.row_id, "season_claimed_by_later_deal"] = True
    return final


def price_extensions_from_terms(final: pd.DataFrame, resolved: pd.DataFrame) -> pd.DataFrame:
    """Price extension seasons from the contract's own terms, not from realized salary.

    The model keeps an extension's structural schedule as signed (the pipeline deliberately does
    not link it to the end of the player's prior deal), so its early seasons often coincide with
    the OLD contract's seasons and a realized join would attach that other contract's pay to the
    extension (year 1 averaged 68% of the extension's own AAV). Extensions with a stated value are
    therefore priced at stated total / term (``aav_source = "contract_terms"``: a deliberate
    contract-first price, distinct from the ``estimated_flat`` fallback used when no realized
    salary exists); the realized figure stays in
    ``aav_realized_musd`` and ``realized_overridden_by_terms`` marks the rows. Spotrac can still
    price future seasons afterwards.
    """
    final = final.copy()
    extension_ids = resolved.loc[resolved.is_extension.fillna(False).astype(bool) & resolved.aav_musd.notna(), "contract_id"]
    mask = final.contract_id.isin(extension_ids) & final.aav_source.eq("realized") & final.aav_naive_musd.notna()
    final["realized_overridden_by_terms"] = mask
    final.loc[mask, "aav_final_musd"] = final.loc[mask, "aav_naive_musd"]
    final.loc[mask, "aav_source"] = "contract_terms"
    return final


def attach_modern_event_metadata(resolved: pd.DataFrame, modern: pd.DataFrame) -> pd.DataFrame:
    """Attach identity/context fields from the player's actual start season.

    Age is deliberately season-specific rather than a latest-career value;
    position and draft class are carried from the same modern.csv row.
    """
    metadata = modern[["PLAYER_ID", "year", "AGE", "Pos", "DRAFT_YEAR"]].rename(
        columns={
            "PLAYER_ID": "player_id", "year": "start_season", "AGE": "age",
            "Pos": "position", "DRAFT_YEAR": "draft_class",
        }
    )
    metadata = metadata.drop_duplicates(subset=["player_id", "start_season"])
    resolved = resolved.merge(metadata, on=["player_id", "start_season"], how="left")

    # Context only: what the player was actually paid in the signing cohort's season. It is
    # NOT the price of this event (the event may be one of several, or a different contract),
    # but it sizes events whose notes carry no dollars, e.g. a bare "re-signed".
    signed = pd.to_datetime(resolved.date)
    resolved["signing_cohort_end_year"] = np.where(signed.dt.month.ge(7), signed.dt.year + 1, signed.dt.year)
    paid = modern[["PLAYER_ID", "year", "salary"]].rename(columns={"PLAYER_ID": "player_id", "year": "signing_cohort_end_year", "salary": "signing_season_salary_musd"})
    paid = paid.drop_duplicates(subset=["player_id", "signing_cohort_end_year"])
    paid["signing_season_salary_musd"] = (paid.signing_season_salary_musd / 1_000_000.0).where(paid.signing_season_salary_musd.gt(0))
    resolved = resolved.merge(paid, on=["player_id", "signing_cohort_end_year"], how="left")
    resolved.loc[resolved.player_id.isna(), "signing_season_salary_musd"] = np.nan
    return resolved.drop(columns=["signing_cohort_end_year"])


def attach_nba_appearance(resolved: pd.DataFrame, index_master: pd.DataFrame) -> pd.DataFrame:
    """Record whether the signed player appeared in any NBA game that season.

    ``index_master.csv`` has one row per player-team-season with a game appearance, so it is
    evidence of use: a signing whose player never took the floor is a camp or non-guaranteed
    deal in practice. Values: ``appeared``; ``no_game_that_season`` (matched player, no row in
    the signing cohort's season); ``never_appeared`` (unmatched name that appears nowhere in
    index_master since 2013); ``unknown`` (unmatched name that does appear, or a cohort whose
    season index_master has not reached yet, where absence proves nothing).
    """
    from psx_crosswalk import INDEX_MASTER_MIN_YEAR, name_variants, normalize_name

    seasons = pd.to_numeric(index_master.year, errors="coerce")
    ids = pd.to_numeric(index_master.nba_id, errors="coerce")
    played = set(zip(ids[ids.notna() & seasons.notna()].astype("int64"), seasons[ids.notna() & seasons.notna()].astype("int64")))
    recent_names = {normalize_name(str(name)) for name in index_master.loc[seasons >= INDEX_MASTER_MIN_YEAR, "player"].dropna().unique()}
    latest_season = int(seasons.max())

    signed = pd.to_datetime(resolved.date)
    cohort = np.where(signed.dt.month.ge(7), signed.dt.year + 1, signed.dt.year)

    def status(player: Any, player_id: Any, cohort_year: int) -> str:
        if cohort_year > latest_season:
            return "unknown"
        if pd.notna(player_id):
            return "appeared" if (int(player_id), int(cohort_year)) in played else "no_game_that_season"
        known = any(normalize_name(variant) in recent_names for variant in name_variants(str(player)))
        return "unknown" if known else "never_appeared"

    resolved["nba_appearance"] = [status(p, i, int(c)) for p, i, c in zip(resolved.player, resolved.player_id, cohort)]
    return resolved


def attach_team_tenure(resolved: pd.DataFrame, index_master: pd.DataFrame) -> pd.DataFrame:
    """Attach prior and continuous franchise service as of each signing cohort.

    A player-season is credited to every team represented that season, which
    deliberately counts both teams when the player was traded midseason.
    """
    history = index_master[["nba_id", "year", "team_id"]].rename(
        columns={"nba_id": "player_id", "year": "history_season_end_year"}
    ).copy()
    history["player_id"] = pd.to_numeric(history.player_id, errors="coerce")
    history["team_id"] = pd.to_numeric(history.team_id, errors="coerce")
    history["history_season_end_year"] = pd.to_numeric(history.history_season_end_year, errors="coerce")
    if "GP" in index_master:
        games = pd.to_numeric(index_master.GP, errors="coerce")
        history = history.loc[games.gt(0)]
    history = history.dropna(subset=["player_id", "team_id", "history_season_end_year"])
    history[["player_id", "team_id", "history_season_end_year"]] = history[["player_id", "team_id", "history_season_end_year"]].astype("int64")
    history = history.drop_duplicates(["player_id", "team_id", "history_season_end_year"])

    events = resolved[["contract_id", "player_id", "team_id", "date"]].copy()
    events["player_id"] = pd.to_numeric(events.player_id, errors="coerce")
    events["team_id"] = pd.to_numeric(events.team_id, errors="coerce")
    dates = pd.to_datetime(events.date, errors="coerce")
    events["signing_cohort_end_year"] = dates.dt.year + dates.dt.month.ge(7).astype("float64")
    valid_events = events.dropna(subset=["player_id", "team_id", "signing_cohort_end_year"])
    matches = valid_events.merge(history, on=["player_id", "team_id"], how="inner", validate="many_to_many")
    prior = matches.loc[matches.history_season_end_year.lt(matches.signing_cohort_end_year)].copy()
    if prior.empty:
        tenure = pd.DataFrame(columns=["contract_id", "prior_team_seasons", "current_team_stint_seasons"])
    else:
        prior = prior.sort_values(["contract_id", "history_season_end_year"], ascending=[True, False])
        prior["next_prior_season"] = prior.groupby("contract_id", sort=False).history_season_end_year.shift(-1)
        prior["starts_stint"] = prior.next_prior_season.isna() | prior.history_season_end_year.sub(prior.next_prior_season).ne(1)
        prior["stint_segment"] = prior.groupby("contract_id", sort=False).starts_stint.cumsum()
        prior["stint_segment_seasons"] = prior.groupby(["contract_id", "stint_segment"]).history_season_end_year.transform("size")
        totals = prior.groupby("contract_id", sort=False).history_season_end_year.nunique().rename("prior_team_seasons")
        latest = prior.drop_duplicates("contract_id").set_index("contract_id")
        current = latest.stint_segment_seasons.where(
            latest.history_season_end_year.eq(latest.signing_cohort_end_year - 1), 0
        ).rename("current_team_stint_seasons")
        tenure = pd.concat([totals, current], axis=1).reset_index()

    output = resolved.merge(tenure, on="contract_id", how="left", validate="one_to_one")
    history_ids = set(history.player_id.unique())
    known_history = output.player_id.isin(history_ids) & pd.to_numeric(output.team_id, errors="coerce").notna()
    output.loc[known_history, "prior_team_seasons"] = output.loc[known_history, "prior_team_seasons"].fillna(0)
    output.loc[known_history, "current_team_stint_seasons"] = output.loc[known_history, "current_team_stint_seasons"].fillna(0)
    bins = [-1, 0, 1, 3, 6, float("inf")]
    output["prior_team_tenure_band"] = pd.cut(
        pd.to_numeric(output.prior_team_seasons, errors="coerce"), bins,
        labels=["First season", "1 prior season", "2–3 prior seasons", "4–6 prior seasons", "7+ prior seasons"], include_lowest=True,
    ).astype("object").fillna("Unknown tenure")
    output["current_team_stint_band"] = pd.cut(
        pd.to_numeric(output.current_team_stint_seasons, errors="coerce"), bins,
        labels=["No immediately prior season", "1-season run", "2–3-season run", "4–6-season run", "7+ season run"], include_lowest=True,
    ).astype("object").fillna("Unknown tenure")
    return output


def main():
    contracts_file = (
        "psx_contracts_v2_2014_present.csv"
        if os.path.exists("psx_contracts_v2_2014_present.csv")
        else "psx_contracts_2014_present.csv"
    )
    crosswalk_file = "crosswalk_matched.csv"

    print(f"Loading raw inputs from {contracts_file}...")
    print("Loading player-team season history from index_master.csv...")
    index_master = pd.read_csv("index_master.csv", low_memory=False)
    resolved = attach_team_tenure(build_resolved_contracts(contracts_file, crosswalk_file), index_master)
    resolved = attach_nba_appearance(resolved, index_master)
    modern_source = os.environ.get("MODERN_CSV_PATH", MODERN_CSV_URL)
    print(f"Fetching modern per-season salaries from {modern_source}...")
    try:
        modern = pd.read_csv(modern_source, low_memory=False)
        resolved = attach_modern_event_metadata(resolved, modern)
        resolved.to_csv("contracts_resolved.csv", index=False)
        print(f"Saved contracts_resolved.csv: {len(resolved)} rows")
        seasons = expand_seasons(resolved)
        print(f"Expanded to {len(seasons)} contract-seasons")
        final = price_extensions_from_terms(attach_modern_salary(seasons, modern), resolved)
    except Exception as e:
        print("Could not fetch remote modern.csv, falling back to local naive values:", e)
        resolved.to_csv("contracts_resolved.csv", index=False)
        print(f"Saved contracts_resolved.csv: {len(resolved)} rows")
        seasons = expand_seasons(resolved)
        print(f"Expanded to {len(seasons)} contract-seasons")
        final = seasons
        final["aav_realized_musd"] = None
        final["aav_final_musd"] = final["aav_naive_musd"]
        final["aav_source"] = "estimated_flat"

    spotrac = None
    salaries_path = os.environ.get("SPOTRAC_SALARIES_PATH", "../web_app/data/nba_salaries.csv")
    options_path = os.environ.get("SPOTRAC_OPTIONS_PATH", "../web_app/data/nba_options.csv")
    if os.path.exists(salaries_path) and os.path.exists(options_path):
        spotrac = load_spotrac_schedule(salaries_path, options_path)
        print(f"Loaded Spotrac forward schedule: {len(spotrac)} player-seasons from {salaries_path}")
    else:
        print("Spotrac forward schedule not found; future seasons keep the flat estimate.")
    final = attach_spotrac_schedule(final, resolved, spotrac)
    print(f"Spotrac-priced seasons: {int(final.aav_source.eq('spotrac_schedule').sum())}")

    final.to_csv("contract_seasons.csv", index=False)
    print(f"Saved contract_seasons.csv: {len(final)} rows")


if __name__ == "__main__":
    main()
