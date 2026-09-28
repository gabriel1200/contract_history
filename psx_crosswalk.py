"""
Crosswalk: PST contract 'player' name strings -> modern.csv PLAYER_ID.

Strategy, tried in order of confidence (a name is resolved by the first
strategy that produces exactly one match -- ambiguous matches are NOT
guessed at, they fall through to unmatched output for manual review):

  1. exact string match against modern.csv PLAYER_NAME
  2. slash-variant match -- PST sometimes lists multiple name forms for
     the same person separated by "/", e.g. "Henry Walker / Bill Walker"
     or "Milos Teodosic / Milos Tedosic". Try each variant exactly.
  3. parenthetical-stripped match -- handles "Justin Hamilton (Anthony)"
     style entries by removing the parenthetical and retrying.
  4. normalized match -- strip suffixes (Jr./Sr./II/III/IV), periods,
     diacritics, and case, then match against a similarly-normalized
     index of modern.csv names. This is the fuzziest tier and is where
     ambiguity (two different real players normalizing to the same
     string) is most likely, so ambiguous normalized matches are
     deliberately NOT resolved -- they go to unmatched.

Anything left after all four tiers is written to a separate "unmatched"
CSV rather than silently dropped or guessed at. In practice, based on a
manual look at real unmatched names, a large fraction of these are
players/coaches who are legitimately absent from modern.csv (no on-court
stats captured there -- 10-day/two-way players who never logged
meaningful minutes, or front-office/coaching hires) -- NOT crosswalk
failures. The unmatched file is for you to eyeball and confirm that
split, not an assumption baked into this script.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

import pandas as pd

# Hand-confirmed via the fuzzy-matching audit (2026-08) -- NOT
# auto-generated. Each of these was manually verified as the same real
# person, not just a high string-similarity score (most high-similarity
# candidates turned out to be different real players with similar names,
# e.g. "Jaylen Johnson" vs "Jalen Johnson" -- see conversation history).
# Add to this list only after manual confirmation, never from an
# automated fuzzy-match pass.
MANUAL_ALIASES: dict[str, str] = {
    "D.J. Augustine": "D.J. Augustin",
    "Jazian Gorman": "Jazian Gortman",
    "Charles Cook": "Charles Cooke",
    "Devin Canady": "Devin Cannady",
    "Justyn Hamilton": "Justin Hamilton",
    "Ron Holland II": "Ronald Holland II",
}
PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
SUFFIX_RE = re.compile(r"\s+(Jr\.?|Sr\.?|II|III|IV|V)$", re.IGNORECASE)


def normalize_name(name: str) -> str:
    """Strip diacritics, suffixes, periods, and case for fuzzy matching.
    NOT used for exact-tier matching -- only for the last-resort tier."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = SUFFIX_RE.sub("", n)
    n = n.replace(".", "").replace("'", "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


@dataclass
class CrosswalkResult:
    player_string: str  # original string as it appears in PST contracts data
    matched_name: Optional[str] = None  # the modern.csv PLAYER_NAME it resolved to
    player_id: Optional[int] = None
    method: Optional[str] = None  # exact / slash_variant / paren_stripped / normalized
    candidates_considered: int = 0  # for ambiguous cases, how many candidates existed


def build_crosswalk(player_strings: list[str], modern_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (matched_df, unmatched_df)."""

    # name -> id, but keep track of names that map to MULTIPLE distinct
    # ids in modern.csv (can happen with true name collisions between two
    # different real players) so we don't silently pick one.
    name_to_ids: dict[str, set[int]] = {}
    for _, r in modern_df[["PLAYER_NAME", "PLAYER_ID"]].drop_duplicates().iterrows():
        name_to_ids.setdefault(r["PLAYER_NAME"], set()).add(int(r["PLAYER_ID"]))

    normalized_to_names: dict[str, set[str]] = {}
    for name in name_to_ids:
        normalized_to_names.setdefault(normalize_name(name), set()).add(name)

    def resolve_exact(name: str) -> Optional[tuple[str, int]]:
        ids = name_to_ids.get(name)
        if ids and len(ids) == 1:
            return name, next(iter(ids))
        return None

    results: list[CrosswalkResult] = []

    for raw in player_strings:
        if not isinstance(raw, str) or not raw.strip():
            continue
        res = CrosswalkResult(player_string=raw)

        # tier 0: manual, hand-confirmed aliases (see MANUAL_ALIASES above)
        alias_target = MANUAL_ALIASES.get(raw.strip())
        if alias_target:
            hit = resolve_exact(alias_target)
            if hit:
                res.matched_name, res.player_id, res.method = hit[0], hit[1], "manual_alias"
                results.append(res)
                continue
            # alias target not found in modern.csv this run (e.g. name
            # changed upstream) -- fall through to automated tiers rather
            # than silently failing

        # tier 1: exact
        hit = resolve_exact(raw.strip())
        if hit:
            res.matched_name, res.player_id, res.method = hit[0], hit[1], "exact"
            results.append(res)
            continue

        # tier 2: slash variants
        if "/" in raw:
            variants = [v.strip() for v in raw.split("/") if v.strip()]
            matches = {resolve_exact(v) for v in variants} - {None}
            if len(matches) == 1:
                m = next(iter(matches))
                res.matched_name, res.player_id, res.method = m[0], m[1], "slash_variant"
                results.append(res)
                continue
            elif len(matches) > 1:
                res.candidates_considered = len(matches)
                results.append(res)  # ambiguous -- unmatched, don't guess
                continue

        # tier 3: strip parenthetical
        stripped = PAREN_RE.sub(" ", raw).strip()
        stripped = re.sub(r"\s+", " ", stripped)
        if stripped != raw.strip():
            hit = resolve_exact(stripped)
            if hit:
                res.matched_name, res.player_id, res.method = hit[0], hit[1], "paren_stripped"
                results.append(res)
                continue

        # tier 4: normalized (suffix/diacritic/case-insensitive)
        norm = normalize_name(raw)
        candidate_names = normalized_to_names.get(norm, set())
        candidate_ids = set()
        for cname in candidate_names:
            candidate_ids |= name_to_ids[cname]
        if len(candidate_ids) == 1:
            res.matched_name = next(iter(candidate_names))
            res.player_id = next(iter(candidate_ids))
            res.method = "normalized"
            results.append(res)
            continue
        elif len(candidate_ids) > 1:
            res.candidates_considered = len(candidate_ids)

        results.append(res)  # unresolved

    all_df = pd.DataFrame([r.__dict__ for r in results])
    matched_df = all_df[all_df.player_id.notna()].copy()
    unmatched_df = all_df[all_df.player_id.isna()].copy()
    return matched_df, unmatched_df


MODERN_CSV_URL = "https://raw.githubusercontent.com/gabriel1200/player_sheets/refs/heads/master/year_totals/modern.csv"


def main():
    # Pulls modern.csv straight from your GitHub repo by default -- no
    # need to keep a local copy in sync. Pass a local path instead if
    # you're working offline or testing against a modified version:
    #   python psx_crosswalk.py /path/to/local/modern.csv
    import sys
    modern_source = sys.argv[1] if len(sys.argv) > 1 else MODERN_CSV_URL
    modern = pd.read_csv(modern_source, low_memory=False)

    # This one stays local -- it's the output of your own scraper run,
    # not something hosted elsewhere. Put it in the same directory you
    # run this script from, or edit the path below.
    contracts = pd.read_csv("psx_contracts_v2_2014_present.csv")

    player_strings = sorted(contracts.player.dropna().unique())
    matched, unmatched = build_crosswalk(player_strings, modern)

    matched.to_csv("crosswalk_matched.csv", index=False)
    unmatched.to_csv("crosswalk_unmatched.csv", index=False)

    print(f"Total unique player strings: {len(player_strings)}")
    print(f"Matched: {len(matched)} ({len(matched) / len(player_strings):.1%})")
    print(f"Unmatched: {len(unmatched)} ({len(unmatched) / len(player_strings):.1%})")
    print()
    print("Match method breakdown:")
    print(matched.method.value_counts())
    print()
    ambiguous = unmatched[unmatched.candidates_considered > 1]
    print(f"Of the unmatched, {len(ambiguous)} were AMBIGUOUS (multiple "
          f"candidates found, not resolved automatically) -- these are "
          f"worth reviewing first since they're not simple 'not in "
          f"modern.csv' cases.")


if __name__ == "__main__":
    main()