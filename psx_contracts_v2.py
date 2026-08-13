"""
Pro Sports Transactions contract-term scraper, v2 -- built on the
`pro_sports_transactions` PyPI package instead of hand-rolled requests.

https://pypi.org/project/pro-sports-transactions/
https://github.com/rsforbes/pro_sports_transactions

Why this replaces the curl_cffi + undetected_chromedriver approach in
psx_contracts.py: that file is still fine and can stay as-is (per your
note it's currently running), but this package already solves the exact
Cloudflare problem we were hand-rolling, via a companion service called
Unflare (https://github.com/iamyegor/Unflare) that handles the browser
challenge + cookie caching for you. Less code we have to maintain
ourselves for a problem that isn't really our core problem.

PREREQUISITE -- you must have Unflare running locally before this script
will work:
  1. Set up Unflare per https://github.com/iamyegor/Unflare (it's a
     separate service, typically run via Docker)
  2. Start it -- it defaults to http://localhost:5002
  3. Then run this script

Scope: 2014-01-01 onwards, to align with existing Value Board contract
data. Notes-field regex parsing is carried over unchanged from
psx_contracts.py, since it's already validated against real site output.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Optional

import pandas as pd
import pro_sports_transactions as pst
from pro_sports_transactions.handlers import UnflareConfig, UnflareRequestHandler

START_DATE = date.fromisoformat("2014-01-01")
END_DATE = date.today()

UNFLARE_URL = "http://localhost:5002/scrape"

TEAM_NICKNAMES = [
    "Hawks", "Celtics", "Nets", "Hornets", "Bulls", "Cavaliers", "Mavericks",
    "Nuggets", "Pistons", "Warriors", "Rockets", "Pacers", "Clippers",
    "Lakers", "Grizzlies", "Heat", "Bucks", "Timberwolves", "Pelicans",
    "Knicks", "Thunder", "Magic", "76ers", "Suns", "Trail Blazers", "Kings",
    "Spurs", "Raptors", "Jazz", "Wizards",
]

CONTRACT_KEYWORDS = re.compile(
    r"\b(signed|re-signed|resigned|extension|extended)\b", re.IGNORECASE
)
TRADE_KEYWORDS = re.compile(r"\btrade\b", re.IGNORECASE)
TRADE_COUNTERPARTY_RE = re.compile(r"trade with (.+)", re.IGNORECASE)

# ---- regex parsing of the Notes free-text field (unchanged from v1,
# validated against real prosportstransactions.com output) -------------

YEARS_VALUE_RE = re.compile(
    r"(?P<years>\d+)-year(?:\s*/\s*(?P<years_alt>\d+)-year)?,?\s+"
    r"~?\$(?P<value>[\d.]+)(?P<unit1>[MK])?"
    r"(?:\s*[-/]\s*\$?(?P<value_alt>[\d.]+))?(?P<unit2>[MK])"
    r"(?:\s+\w[\w-]*){0,4}?\s+contract",
    re.IGNORECASE,
)
# Option year: originally required "2028-29" format, but real data also
# has bare-year options like "player option for 2022".
OPTION_RE = re.compile(
    r"\b(?P<opt_type>player|team)\s+option\s+for\s+(?P<opt_year>\d{4}(?:-\d{2})?)",
    re.IGNORECASE,
)
# Second phrasing PST uses: "(2026-27 is team option)" instead of
# "includes team option for 2026-27" -- confirmed present in real data
# (31 rows), not caught by OPTION_RE above.
OPTION_PAREN_RE = re.compile(
    r"\(\s*(?P<opt_year>\d{4}(?:-\d{2})?)\s+is\s+(?:a\s+)?(?P<opt_type>player|team)\s+option\s*\)",
    re.IGNORECASE,
)
# Not every "signed"/"re-signed" row is a player contract -- coaching and
# front-office hires use the same verbs. Flag rather than silently drop,
# since whether to exclude these is a downstream decision.
COACH_EXEC_RE = re.compile(
    r"\b(head coach|assistant coach|general manager|"
    r"president of basketball operations|vice president|front office)\b",
    re.IGNORECASE,
)
# Separate from the "X-year / Y-year" slash format: "3-year $6.6M contract
# through 2022-23 with a 1-year team option for 2023-24" -- the extra
# option year here was never being added to total contract length.
EXTRA_OPTION_YEARS_RE = re.compile(
    r"with an?\s+(?P<extra_years>\d+)-year\s+(?:player|team)\s+option",
    re.IGNORECASE,
)
QUALIFYING_OFFER_RE = re.compile(r"\bqualifying offer\b", re.IGNORECASE)
NON_GUARANTEED_RE = re.compile(r"\bnon-guaranteed\b", re.IGNORECASE)
EXHIBIT10_RE = re.compile(r"\bExhibit 10\b", re.IGNORECASE)
TWO_WAY_RE = re.compile(r"\btwo-way\b", re.IGNORECASE)
EXTENSION_RE = re.compile(r"\bextension\b", re.IGNORECASE)
RE_SIGNED_RE = re.compile(r"\bre-?signed\b", re.IGNORECASE)


@dataclass
class TradeRow:
    date: str
    team: str
    counterparty: Optional[str]  # parsed from "trade with X"; None if unparseable
    acquired_assets: list = field(default_factory=list)
    relinquished_assets: list = field(default_factory=list)
    num_acquired: int = 0
    num_relinquished: int = 0
    raw_notes: str = ""


def extract_assets(cell_text: str) -> list[str]:
    """Splits a table cell on the bullet character ('\u2022') rather than
    on <br/> tags, since pandas.read_html has already flattened the HTML
    to plain text by the time we see it -- bullet-splitting is robust
    regardless of whether read_html turned <br/> into spaces, newlines,
    or dropped it. Validated against the real Paul George/Jaylen Brown
    trade row from your saved sample."""
    parts = [p.strip() for p in cell_text.split("\u2022")]
    return [p for p in parts if p]


def parse_trade_row(date: str, team: str, acquired: str, relinquished: str, notes: str) -> TradeRow:
    m = TRADE_COUNTERPARTY_RE.search(notes)
    counterparty = m.group(1).strip() if m else None
    acquired_assets = extract_assets(acquired)
    relinquished_assets = extract_assets(relinquished)
    return TradeRow(
        date=date,
        team=team,
        counterparty=counterparty,
        acquired_assets=acquired_assets,
        relinquished_assets=relinquished_assets,
        num_acquired=len(acquired_assets),
        num_relinquished=len(relinquished_assets),
        raw_notes=notes,
    )



@dataclass
class ContractRow:
    date: str
    team: str
    player: str
    raw_notes: str
    is_extension: bool = False
    is_resigning: bool = False
    is_qualifying_offer: bool = False
    is_non_guaranteed: bool = False
    is_exhibit10: bool = False
    is_two_way: bool = False
    is_coach_or_exec: bool = False
    has_contract_word: bool = True  # False is a signal for overseas/other non-standard signings
    years: Optional[int] = None
    years_alt: Optional[int] = None
    total_value_musd: Optional[float] = None
    aav_musd: Optional[float] = None  # naive flat AAV -- see note in parse_notes
    option_type: Optional[str] = None
    option_year: Optional[str] = None
    parse_warnings: list = field(default_factory=list)


def parse_notes(notes: str) -> dict:
    out = {
        "is_extension": bool(EXTENSION_RE.search(notes)),
        "is_resigning": bool(RE_SIGNED_RE.search(notes)),
        "is_qualifying_offer": bool(QUALIFYING_OFFER_RE.search(notes)),
        "is_non_guaranteed": bool(NON_GUARANTEED_RE.search(notes)),
        "is_exhibit10": bool(EXHIBIT10_RE.search(notes)),
        "is_two_way": bool(TWO_WAY_RE.search(notes)),
        "is_coach_or_exec": bool(COACH_EXEC_RE.search(notes)),
        "has_contract_word": "contract" in notes.lower(),
        "parse_warnings": [],
    }

    yv = YEARS_VALUE_RE.search(notes)
    if yv:
        years = int(yv.group("years"))
        years_alt = int(yv.group("years_alt")) if yv.group("years_alt") else None
        out["years"] = years
        out["years_alt"] = years_alt

        # unit1 (on the first number) is often absent when it shares the
        # second number's unit, e.g. "$7.4-8M" or "$27 / $37M" -- fall
        # back to unit2 in that case.
        unit1 = yv.group("unit1") or yv.group("unit2")
        unit2 = yv.group("unit2")
        value = float(yv.group("value"))
        value = value / 1000 if unit1 == "K" else value  # normalize to $M
        out["total_value_musd"] = value

        if yv.group("value_alt"):
            value_alt = float(yv.group("value_alt"))
            value_alt = value_alt / 1000 if unit2 == "K" else value_alt
            out["parse_warnings"].append(
                f"dual value in source (~${value}M / ~${value_alt}M range) -- "
                "total_value_musd uses the FIRST figure only; check raw_notes "
                "for the second (often a with-options ceiling or floor)."
            )

        denom = years_alt or years
        if denom:
            out["aav_musd"] = round(value / denom, 3)
        out["parse_warnings"].append(
            "aav_musd is naive (total/years); does not reflect actual "
            "year-by-year raises. Cross-reference Spotrac for real schedule."
        )

        # 'with a N-year team/player option' extends total length beyond
        # the base X-year figure, distinct from the 'X-year / Y-year'
        # slash format. Only apply if years_alt wasn't already set by that
        # format, to avoid double-extending.
        if years_alt is None:
            extra = EXTRA_OPTION_YEARS_RE.search(notes)
            if extra:
                out["years_alt"] = years + int(extra.group("extra_years"))
    else:
        out["parse_warnings"].append("years/value pattern not matched")

    opt = OPTION_RE.search(notes) or OPTION_PAREN_RE.search(notes)
    if opt:
        out["option_type"] = opt.group("opt_type").lower()
        out["option_year"] = opt.group("opt_year")

    return out


def rows_from_dataframe(df: pd.DataFrame) -> tuple[list[ContractRow], list[TradeRow]]:
    contract_rows: list[ContractRow] = []
    trade_rows: list[TradeRow] = []

    for _, r in df.iterrows():
        notes = str(r["Notes"])
        date_, team = str(r["Date"]), str(r["Team"])
        acquired_raw, relinquished_raw = str(r["Acquired"]), str(r["Relinquished"])

        if CONTRACT_KEYWORDS.search(notes):
            player = acquired_raw.lstrip("\u2022").strip()
            row = ContractRow(date=date_, team=team, player=player, raw_notes=notes)
            for k, v in parse_notes(notes).items():
                setattr(row, k, v)
            contract_rows.append(row)
        elif TRADE_KEYWORDS.search(notes):
            trade_rows.append(
                parse_trade_row(date_, team, acquired_raw, relinquished_raw, notes)
            )
        # else: waivers, IL activations, G-league assignments, etc. --
        # not captured in either output for now (cheap to add a third
        # "other movement" bucket later if it turns out to matter; we're
        # already fetching these rows regardless of what we keep).

    return contract_rows, trade_rows


async def scrape_team(
    team: str, handler: UnflareRequestHandler, max_pages: int = 300
) -> tuple[list[ContractRow], list[TradeRow]]:
    all_contract_rows: list[ContractRow] = []
    all_trade_rows: list[TradeRow] = []
    starting_row = 0
    for page_num in range(max_pages):
        search = pst.Search(
            league=pst.League.NBA,
            transaction_types=(pst.TransactionType.Movement,),
            start_date=START_DATE,
            end_date=END_DATE,
            team=team,
            starting_row=starting_row,
            request_handler=handler,
        )
        df = await search.get_dataframe()

        if df.attrs.get("errors"):
            print(f"  {team} page {page_num}: ERROR {df.attrs['errors']}",
                  file=sys.stderr)
            break

        contract_rows, trade_rows = rows_from_dataframe(df)
        all_contract_rows.extend(contract_rows)
        all_trade_rows.extend(trade_rows)

        total_pages = df.attrs.get("pages", 0)
        print(f"  {team}: row {starting_row} -> {len(contract_rows)} contract, "
              f"{len(trade_rows)} trade rows (page {page_num + 1} of {total_pages})",
              file=sys.stderr)

        if len(df) == 0 or (page_num + 1) >= total_pages:
            break
        starting_row += 25

    return all_contract_rows, all_trade_rows


async def main():
    config = UnflareConfig(url=UNFLARE_URL, timeout=60000)
    handler = UnflareRequestHandler(config)

    all_contract_rows: list[ContractRow] = []
    all_trade_rows: list[TradeRow] = []
    for team in TEAM_NICKNAMES:
        print(f"Scraping {team}...", file=sys.stderr)
        try:
            contract_rows, trade_rows = await scrape_team(team, handler)
            all_contract_rows.extend(contract_rows)
            all_trade_rows.extend(trade_rows)
        except Exception as e:
            print(f"  ERROR on {team}: {e!r}", file=sys.stderr)

    contracts_path = "psx_contracts_v2_2014_present.csv"
    if all_contract_rows:
        out_df = pd.DataFrame([asdict(r) for r in all_contract_rows])
        out_df["parse_warnings"] = out_df["parse_warnings"].apply(
            lambda ws: "; ".join(ws)
        )
        out_df.to_csv(contracts_path, index=False)
    print(f"Wrote {len(all_contract_rows)} rows to {contracts_path}", file=sys.stderr)

    # Trades get their own file since the schema doesn't overlap with
    # contracts (multi-asset acquired/relinquished lists vs. single-player
    # contract terms). Note: a trade between Team A and Team B appears
    # TWICE in the combined output -- once from A's team-page (A's
    # acquired = B's relinquished) and once from B's. That's a property of
    # how the site organizes data per-team, not a bug in this script --
    # dedupe downstream (e.g. on date + sorted asset sets) if you want
    # one row per trade rather than one row per team-side-of-trade.
    trades_path = "psx_trades_v2_2014_present.csv"
    if all_trade_rows:
        trade_df = pd.DataFrame([asdict(r) for r in all_trade_rows])
        trade_df["acquired_assets"] = trade_df["acquired_assets"].apply(
            lambda a: "; ".join(a)
        )
        trade_df["relinquished_assets"] = trade_df["relinquished_assets"].apply(
            lambda a: "; ".join(a)
        )
        trade_df.to_csv(trades_path, index=False)
    print(f"Wrote {len(all_trade_rows)} rows to {trades_path}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())