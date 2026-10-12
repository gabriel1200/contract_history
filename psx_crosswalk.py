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

  5. normalized-variant match -- the same normalization applied to each
     slash variant and parenthetical-stripped form separately, so
     "Ogugua Anunoby / O.G. Anunoby" finds modern's "OG Anunoby".
  6. index_master fallback -- a unique normalized-name match in
     index_master.csv (players with a game appearance since 2013), for
     players modern.csv does not carry. These get an ID (so franchise
     tenure joins) but no modern.csv salary/age/position.

Anything left after all tiers is written to a separate "unmatched"
CSV rather than silently dropped or guessed at. In practice, based on a
manual look at real unmatched names, a large fraction of these are
players/coaches who are legitimately absent from modern.csv (no on-court
stats captured there -- 10-day/two-way players who never logged
meaningful minutes, or front-office/coaching hires) -- NOT crosswalk
failures. The unmatched file is for you to eyeball and confirm that
split, not an assumption baked into this script.
"""

from __future__ import annotations

import os
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
NBA_API_MIN_ID = 200000
NBA_API_MIN_TO_YEAR = 2012  # start year of the last season played; PST begins Jan 2014, so a 2012-13 player can still be waived then
API_DISAMBIGUATOR_RE = re.compile(r"\((?:b\.?|b\.? [^)]*|[A-Z][a-z]+|I{1,3}|IV|V)\)")
PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
GENERATION_MARKER_RE = re.compile(r"\((?:I{1,3}|IV|V|Jr\.?|Sr\.?)\)", re.IGNORECASE)
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


INDEX_MASTER_MIN_YEAR = 2013  # PST signings start in 2014; older namesakes would be false matches


def name_variants(raw: str) -> list[str]:
    """The raw string plus each slash part, with and without parentheticals."""
    parts = [raw] + [v.strip() for v in raw.split("/") if v.strip()]
    out = []
    for part in parts:
        out.append(part)
        # "(I)", "(II)", "(Jr.)" tell two relatives apart; stripping it would merge them
        # (e.g. "Larry Drew (I)" is not Larry Drew II), so only strip other parentheticals.
        if not GENERATION_MARKER_RE.search(part):
            out.append(re.sub(r"\s+", " ", PAREN_RE.sub(" ", part)).strip())
    return list(dict.fromkeys(v for v in out if v))


def build_crosswalk(player_strings: list[str], modern_df: pd.DataFrame,
                    index_df: Optional[pd.DataFrame] = None,
                    nba_api_df: Optional[pd.DataFrame] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (matched_df, unmatched_df). ``index_df`` (index_master.csv) enables tier 6; ``nba_api_df``
    (nba_api_players.csv: id, full_name, is_active) enables tier 7."""

    # name -> id, but keep track of names that map to MULTIPLE distinct
    # ids in modern.csv (can happen with true name collisions between two
    # different real players) so we don't silently pick one.
    name_to_ids: dict[str, set[int]] = {}
    for _, r in modern_df[["PLAYER_NAME", "PLAYER_ID"]].drop_duplicates().iterrows():
        name_to_ids.setdefault(r["PLAYER_NAME"], set()).add(int(r["PLAYER_ID"]))

    normalized_to_names: dict[str, set[str]] = {}
    for name in name_to_ids:
        normalized_to_names.setdefault(normalize_name(name), set()).add(name)

    index_norm_to_ids: dict[str, dict[int, str]] = {}
    if index_df is not None:
        recent = index_df[pd.to_numeric(index_df.year, errors="coerce") >= INDEX_MASTER_MIN_YEAR]
        for _, r in recent[["player", "nba_id"]].dropna().drop_duplicates().iterrows():
            index_norm_to_ids.setdefault(normalize_name(str(r["player"])), {})[int(r["nba_id"])] = str(r["player"])

    api_norm_to_ids: dict[str, dict[int, str]] = {}
    if nba_api_df is not None:
        # Only recent careers: a name that last played before the PST window (2014+) is a namesake, a coach or an
        # executive. Rows from the live endpoint carry career years; rows only the bundled list had do not, so those
        # fall back to "active, or an id of 200000+" (ids under that are 1940s-2000s careers).
        years = pd.to_numeric(nba_api_df["to_year"], errors="coerce") if "to_year" in nba_api_df else pd.Series(float("nan"), index=nba_api_df.index)
        recent = years.ge(NBA_API_MIN_TO_YEAR) | (years.isna() & (nba_api_df["is_active"].astype(bool) | (nba_api_df["id"] >= NBA_API_MIN_ID)))
        for _, r in nba_api_df.loc[recent, ["id", "full_name"]].dropna().drop_duplicates().iterrows():
            api_norm_to_ids.setdefault(normalize_name(str(r["full_name"])), {})[int(r["id"])] = str(r["full_name"])

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

        # tier 5: normalized match on each slash / parenthetical variant
        variant_ids: dict[int, str] = {}
        for variant in name_variants(raw.strip()):
            for cname in normalized_to_names.get(normalize_name(variant), set()):
                for pid in name_to_ids[cname]:
                    variant_ids[pid] = cname
        if len(variant_ids) == 1:
            (pid, cname), = variant_ids.items()
            res.matched_name, res.player_id, res.method = cname, pid, "normalized_variant"
            results.append(res)
            continue
        if len(variant_ids) > 1:
            res.candidates_considered = max(res.candidates_considered, len(variant_ids))

        # tier 6: index_master fallback (unique id only; ambiguity is never guessed)
        index_ids: dict[int, str] = {}
        for variant in name_variants(raw.strip()):
            index_ids.update(index_norm_to_ids.get(normalize_name(variant), {}))
        if not variant_ids and len(index_ids) == 1:
            (pid, iname), = index_ids.items()
            res.matched_name, res.player_id, res.method = iname, pid, "index_master"
            results.append(res)
            continue
        if not variant_ids and len(index_ids) > 1:
            res.candidates_considered = max(res.candidates_considered, len(index_ids))

        # tier 7: nba_api all-time player list snapshot. It has no career years, so a namesake from an earlier
        # era could match a PST name; that is why it is the last tier and its rows are labelled nba_api_static.
        if not variant_ids and not index_ids:
            api_ids: dict[int, str] = {}
            for variant in name_variants(raw.strip()):
                # a suffix ("Jr.") or a "(b. 1991-01-22)" / "(Aaron)" tag means PST is separating namesakes or father
                # and son; normalisation would erase exactly that, so such strings are left for a human
                if SUFFIX_RE.search(variant) or API_DISAMBIGUATOR_RE.search(raw):
                    continue
                api_ids.update(api_norm_to_ids.get(normalize_name(variant), {}))
            if len(api_ids) == 1:
                (pid, aname), = api_ids.items()
                res.matched_name, res.player_id, res.method = aname, pid, "nba_api_static"
                results.append(res)
                continue
            if len(api_ids) > 1:
                res.candidates_considered = max(res.candidates_considered, len(api_ids))

        results.append(res)  # unresolved

    all_df = pd.DataFrame([r.__dict__ for r in results])
    matched_df = all_df[all_df.player_id.notna()].copy()
    unmatched_df = all_df[all_df.player_id.isna()].copy()
    return matched_df, unmatched_df


MODERN_CSV_URL = "https://raw.githubusercontent.com/gabriel1200/player_sheets/refs/heads/master/year_totals/modern.csv"


MOVEMENTS_CSV = "psx_movements_v2_2014_present.csv"
MOVEMENTS_RESOLVED_CSV = "movements_resolved.csv"
# Notes that are about staff, ownership or an organisation rather than a player. Their names are not looked up.
NON_PLAYER_NOTES_RE = re.compile(
    r"\b(?:hired|fired|promoted|demoted|resigned as|named|interim|ownership|coach|general manager|president|trainer|"
    r"director|executive|scout|assistant|coordinator|owner)\b", re.IGNORECASE)


def _movement_player_column(movements: pd.DataFrame) -> pd.Series:
    """The single player named on a movement row (acquired side if present, else relinquished); '' for staff rows."""
    acq = movements["acquired"].fillna("").astype(str).str.strip()
    rel = movements["relinquished"].fillna("").astype(str).str.strip()
    name = acq.where(acq != "", rel)
    staff = movements["notes"].fillna("").astype(str).str.contains(NON_PLAYER_NOTES_RE)
    labeled = movements.get("move_type", pd.Series("", index=movements.index)).fillna("").astype(str) != ""
    return name.mask(staff & ~labeled, "")


def movement_player_strings(movements: pd.DataFrame) -> list[str]:
    col = _movement_player_column(movements)
    return sorted(col[col != ""].unique())


def resolve_movements(movements: pd.DataFrame, matched: pd.DataFrame) -> pd.DataFrame:
    """movements + player_string / player_id / matched_name / match_method (blank when unresolved)."""
    out = movements.copy()
    out["player_string"] = _movement_player_column(movements)
    lookup = matched.drop_duplicates("player_string").set_index("player_string")
    out["player_id"] = out["player_string"].map(lookup["player_id"])
    out["matched_name"] = out["player_string"].map(lookup["matched_name"])
    out["match_method"] = out["player_string"].map(lookup["method"])
    out["player_id"] = pd.to_numeric(out["player_id"], errors="coerce").astype("Int64")
    return out


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
    index_master = pd.read_csv("index_master.csv", low_memory=False) if os.path.exists("index_master.csv") else None

    movements = pd.read_csv(MOVEMENTS_CSV, keep_default_na=False) if os.path.exists(MOVEMENTS_CSV) else None
    movement_names = movement_player_strings(movements) if movements is not None else []
    player_strings = sorted(set(contracts.player.dropna().unique()) | set(movement_names))
    nba_api_df = pd.read_csv("nba_api_players.csv") if os.path.exists("nba_api_players.csv") else None
    matched, unmatched = build_crosswalk(player_strings, modern, index_master, nba_api_df)

    matched.to_csv("crosswalk_matched.csv", index=False)
    unmatched.to_csv("crosswalk_unmatched.csv", index=False)

    if movements is not None:
        resolved = resolve_movements(movements, matched)
        resolved.to_csv(MOVEMENTS_RESOLVED_CSV, index=False)
        is_player = resolved.player_string != ""
        print(f"Wrote {MOVEMENTS_RESOLVED_CSV}: {len(resolved)} rows, {int(is_player.sum())} with a player name, "
              f"{int((resolved.player_id.notna()).sum())} resolved to a player_id "
              f"({resolved[resolved.move_type != ''].player_id.notna().mean():.1%} of waiver-type rows)")
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