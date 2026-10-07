# NBA Contract History Pipeline -- Data Dictionary

Last updated against pipeline fingerprint: **7,843 contracts / 8,522 season
rows (4,075 contracts) / 4,634 realized AAV / 954 Spotrac-priced future seasons / 25
option_year_mismatch flags / 9 superseded reports.** If a fresh
run doesn't match these numbers, something upstream changed (a script
edit, a `modern.csv` update, or a new scrape) -- worth diffing rather
than assuming it's fine.

## Pipeline overview

```
1. psx_contracts_v2.py    scrapes prosportstransactions.com (Celtics-style
                           team pages, 2014-present) via the
                           pro_sports_transactions package + Unflare for
                           Cloudflare bypass. Splits every "player
                           movement" row into two buckets by keyword
                           match on the Notes text:
                             -> psx_contracts_v2_2014_present.csv  (signings)
                             -> psx_trades_v2_2014_present.csv     (trades)
                           Rows that are neither (waivers, IL moves,
                           G-League assignments) are fetched but discarded.

2. psx_crosswalk.py       resolves each contract's free-text player name
                           to a modern.csv PLAYER_ID, in 4 confidence
                           tiers (exact / slash-variant / parenthetical-
                           stripped / normalized). Ambiguous matches are
                           never guessed -- they fall through to unmatched.
                             -> crosswalk_matched.csv
                             -> crosswalk_unmatched.csv

3. build_unified.py       joins contracts to the crosswalk, expands each
                           contract into one row per season, and attaches
                           real per-season salary from modern.csv where
                           available (falling back to a naive estimate
                           otherwise). It also uses index_master.csv to
                           attach prior franchise service at signing.
                             -> contracts_resolved.csv   (Layer 2: one row
                                per signing event, structural terms)
                             -> contract_seasons.csv     (Layer 3: one row
                                per contract-season, with resolved AAV)
```

`index_master.csv` is a player-team-season history input (NBA player ID,
season end year, and team ID); it must be available before the unified
build to calculate franchise-tenure fields. `MODERN_CSV_PATH` can point to
a local modern.csv instead of the default remote source. The current
snapshot has 6,425 / 7,843 signing events (81.9%) linked to player-team
history; unresolved IDs remain `Unknown tenure` rather than being labeled
as first-time players.

**Spotrac forward schedule (optional input, added 2026-10-07):** `build_unified.py`
reads `SPOTRAC_SALARIES_PATH` / `SPOTRAC_OPTIONS_PATH` (default
`../web_app/data/nba_salaries.csv` and `nba_options.csv`, exported by the web app's
`contract_data2.ipynb`). The two files are not row-aligned (join on player + team); zero
salaries and dead-money rows (`D`) are dropped. Seasons from the first dense Spotrac season
(2026-27, season_end_year 2027) with no realized salary are priced from it, matched by NBA ID,
else name (a name-only match must be the same team or within 35% of our own estimate). Only
the latest unsuperseded event per player-season claims a price. If the files are missing the
build keeps the flat estimates. Refresh the export before each build; it is a dated snapshot.

Run order: `psx_contracts_v2.py` (needs Unflare running locally on
`localhost:5002`) -> `psx_crosswalk.py` -> `build_unified.py`. The last
two pull `modern.csv` live from GitHub, so no local copy is needed for
those.

**Design decision (explicit, from working through this together):** we
deliberately do NOT try to reconstruct whether an option was actually
exercised/declined, or precisely link contract extensions to the true
end of a player's prior deal. We only capture the *structural* terms as
signed -- years, dollar value, whether an option exists and its type.
Season-boundary precision is a known, accepted limitation as a result
(see "Known limitations" below).

---

## File: `psx_contracts_v2_2014_present.csv` (raw scrape output, Layer 1)

One row per PST "player movement" transaction whose Notes text matched
`signed|re-signed|resigned|extension|extended`. This is the untouched
scrape -- nothing here is inferred, only what PST's Notes field literally
says, mechanically parsed.

| column | meaning |
|---|---|
| `date` | signing date, as reported by PST |
| `team` | team nickname (PST's format, e.g. "Celtics") |
| `player` | free-text player name from PST's "Acquired" cell |
| `raw_notes` | the literal Notes text -- source of truth for everything else in this row |
| `is_extension` / `is_resigning` / `is_qualifying_offer` / `is_non_guaranteed` / `is_exhibit10` / `is_two_way` | keyword flags on `raw_notes` |
| `is_coach_or_exec` | notes mention head coach / GM / president of basketball ops / etc. -- these aren't player contracts, filter out if you want a pure player-contract view |
| `has_contract_word` | False usually means an overseas signing (e.g. "signed with Olimpia Milano (Italy)") -- no dollar terms possible to parse, this is expected, not a bug |
| `years` / `years_alt` | contract length in years. `years_alt` is set when PST's phrasing implies a longer total including an option (either "X-year / Y-year" slash format, or "X-year ... with a N-year option" phrasing) |
| `total_value_musd` | total contract value in $ millions, from the FIRST dollar figure when the notes give a range (e.g. "$118M / $125.9M") -- see `parse_warnings` |
| `aav_musd` | naive flat AAV = `total_value_musd / (years_alt or years)`. Does NOT reflect real year-by-year raises -- this is a placeholder, superseded by `aav_final_musd` in `contract_seasons.csv` wherever real salary data exists |
| `option_type` / `option_year` | "player" or "team", and the season it applies to (e.g. "2028-29" or a bare year like "2022") |
| `parse_warnings` | free-text notes on parsing caveats for this specific row (e.g. "years/value pattern not matched", "dual value in source") |

**Coverage reality check** (as of the last full audit): only ~31% of
rows have a parseable `years`/`total_value_musd` at all. Half of all rows
genuinely have no dollar figure in PST's source text (10-day contracts,
bare minimum-salary deals) -- this is a hard ceiling of the data source,
not a parsing gap.

---

## File: `psx_trades_v2_2014_present.csv`

One row per team-side of a trade. **Every trade appears once per team
involved** -- a 3-team trade shows up as 3 separate rows (one per team's
page on PST), not deduplicated. Left this way deliberately (see
conversation on 2026-08-13): nothing currently consumes this file, and
the per-team-side shape is actually the natural one for team-centric
questions ("what did team X give up"). If you ever need one-row-per-
trade-event, that's a harder multi-team dedup problem -- solve it against
a real use case when one exists, not speculatively.

| column | meaning |
|---|---|
| `date`, `team` | as in contracts |
| `counterparty` | parsed from "trade with X" phrasing; blank for multi-team trades where PST's phrasing didn't match cleanly (4/1523 rows) |
| `acquired_assets` / `relinquished_assets` | semicolon-joined lists -- players and picks both, PST doesn't distinguish them structurally |
| `num_acquired` / `num_relinquished` | asset counts, handy for filtering to simple 1-for-1 trades vs. complex ones |
| `raw_notes` | kept for anything the parser missed |

---

## File: `crosswalk_matched.csv` / `crosswalk_unmatched.csv`

Maps PST's free-text `player` strings to `modern.csv`'s numeric
`PLAYER_ID`, via `psx_crosswalk.py`. 6 tiers (plus hand-confirmed `manual_alias`
entries), tried in order, first unambiguous match wins:

1. **exact** -- string match against `modern.csv` PLAYER_NAME
2. **slash_variant** -- PST sometimes lists multiple name forms
   separated by "/" (e.g. "Henry Walker / Bill Walker"); each variant is
   tried
3. **paren_stripped** -- handles "Justin Hamilton (Anthony)" by removing
   the parenthetical
4. **normalized** -- strips suffixes (Jr./Sr./II/III), periods,
   diacritics, and case for a last-resort match
5. **normalized_variant** -- the same normalization applied to each slash
   variant and parenthetical-stripped form separately ("Ogugua Anunoby /
   O.G. Anunoby" -> modern's "OG Anunoby"). Generational markers such as
   "(I)" or "(Jr.)" are never stripped: "Larry Drew (I)" is not Larry
   Drew II.
6. **index_master** -- a unique normalized-name match in `index_master.csv`
   restricted to players with a season since 2013, for players `modern.csv`
   does not carry. They get an ID (franchise tenure joins) but no
   `modern.csv` salary/age/position/draft.

Ambiguous matches (multiple different real players could match) are
**never auto-resolved** -- they land in `crosswalk_unmatched.csv` with
`candidates_considered > 1` so they're distinguishable from simple
not-found cases.

**Current match rate: ~82%.** Spot-checked the unmatched ~18% and most
are players genuinely absent from `modern.csv` (fringe/two-way/undrafted
players with no real on-court stats, or coaches) -- not confirmed
exhaustively, this is the open item under "Known limitations."

---

## File: `contracts_resolved.csv` (Layer 2)

`psx_contracts_v2_2014_present.csv`, re-parsed with the current regex
(may be newer than whatever generated the raw CSV -- this script always
re-parses `raw_notes` fresh rather than trusting stored derived columns)
and joined to the crosswalk. One row per signing event. All columns from
the raw file, plus:

| column | meaning |
|---|---|
| `contract_id` | `sha256(date\|team\|player\|normalized_notes)[:12]`. Stable across re-scrapes of the same underlying event -- use this to diff runs or track a specific contract. Normalization (lowercase, whitespace-collapse) means trivial text formatting changes won't churn the id, but a real edit to the notes text will. **Not** derived from row position -- do not assume ordering is meaningful. |
| `player_id` | from the crosswalk; NaN if unmatched (~17% of rows) |
| `matched_name` | the `modern.csv` PLAYER_NAME the crosswalk resolved to (may differ from `player` -- e.g. nickname vs. full name) |
| `calendar_end_basis` | what the parsed "through YYYY-YY" / option year anchors: `guaranteed_years` ("through" in a note that also lists options: it ends the guaranteed years, e.g. a first-round pick's 2 guaranteed seasons, with the option seasons following), or `full_term` (it ends the whole term). Used to derive `start_season`; before 2026-10-07 every "through" was treated as the end of the option-inclusive term, putting 61 first-round contracts two seasons early. |
| `crosswalk_method` | which tier resolved this match (exact / slash_variant / paren_stripped / normalized / normalized_variant / index_master / manual_alias) |
| `signing_season_salary_musd` | **context only, not this event's price.** What `modern.csv` shows the player was paid in the signing cohort's season (month >= July -> year + 1), null if unmatched, absent from `modern.csv`, or zero. It sizes events whose notes carry no dollars (e.g. a bare "re-signed" whose season salary is 35% of the cap). It can belong to a different or earlier contract and one player-season may carry several events, so never sum it or treat it as a schedule. |
| `nba_appearance` | Whether the signed player took the floor, from `index_master.csv` (one row per player-team-season with a game): `appeared`; `no_game_that_season` (matched player, no game in the signing cohort's season); `never_appeared` (unmatched name found nowhere in index_master since 2013); `unknown` (unmatched name that does appear in index_master, or a cohort newer than index_master's latest season, where absence proves nothing -- all 2027-cohort rows). Evidence of use, not of contract terms: a player who never played did not carry a meaningful used commitment, but a guaranteed deal for an injured or stashed player is possible, so wording-flagged events are not treated as immaterial downstream. |
| `is_superseded` / `superseded_by` | True when the same player (ID, else name), team, stated total value and term were reported again within 60 days; the earlier report is flagged and `superseded_by` is the later report's `contract_id`. Typically an agreement report followed by the formal signing (9 cases as of 2026-10-07, largest Anunoby's $212.5M). Nothing is dropped; the web app excludes superseded rows. Priced, termed player deals only, so 10-day and bare camp signings are never merged. |
| `start_season` | derived from `date`: month >= July -> `date.year + 1` (upcoming season); month <= June -> `date.year` (season already in progress). This is the season's END year, matching `modern.csv`'s convention (confirmed against real historical NBA salary cap figures -- e.g. `modern.csv` year=2014 has cap $58.679M, which is the actual 2013-14 cap). |
| `total_seasons` | `years_alt` if present, else `years` -- total length used for season expansion in Layer 3 |
| `prior_team_seasons` | Distinct seasons the player appeared for the signing franchise before the signing cohort; trade seasons count for each team represented. Null means history could not be linked. |
| `current_team_stint_seasons` | Consecutive prior completed seasons with the signing franchise, ending in the season immediately before the signing cohort; 0 if not with that team in that season. |
| `prior_team_tenure_band` / `current_team_stint_band` | Display groupings derived from the counts; unmatched history is `Unknown tenure`. |

Tenure uses the signing cohort's season-end year (signings in July or later
map to the following end year; earlier dates map to the calendar year).
Only history seasons with an end year strictly before that cohort count,
so a team-season the player may join later in the same calendar season is
not leaked into the signing-time measure. `prior_team_seasons` counts all
distinct prior seasons with the franchise. `current_team_stint_seasons`
counts the contiguous run ending in the immediately preceding completed
season, and is zero if that season was not with the signing franchise.
When traded midseason, a season is credited to each team recorded in the
player-team-season index; this is a season count, not a months-on-team
estimate.

**Known duplicate-row fix applied:** 3 exact-duplicate rows (identical
date/team/player/notes -- likely pagination overlap during scraping)
are dropped before `contract_id` generation.

---

## File: `contract_seasons.csv` (Layer 3)

One row per `(contract, season)`, expanded from `contracts_resolved.csv`.
**Only contracts with `total_seasons` populated get expanded, and then only
if they have a `player_id` or a stated price (`aav_musd`).** No-ID events
with a stated value (61 contracts as of 2026-10-07: new rookies and
slash-style names the crosswalk missed, e.g. Anunoby's $212.5M extension)
are expanded at the flat estimate (`aav_source = estimated_flat`, no
realized salary, no age/position/draft). Coach/executive deals and unpriced
no-ID events are still absent (they remain in `contracts_resolved.csv`).

| column | meaning |
|---|---|
| `contract_id`, `player`, `player_id`, `team` | joined from Layer 2 |
| `prior_team_seasons`, `current_team_stint_seasons`, tenure bands | Franchise service at signing, repeated on each structural contract-season row |
| `season_index` | 1-based position within this contract (1 = first season) |
| `season_end_year` | the season this row represents, in `modern.csv`'s end-year convention |
| `is_option_season` | True only for the FINAL season of a contract that has a parsed `option_type`. **Assumption, not proven for every case**: based on 3 hand-validated real examples, not exhaustively checked. If a contract has an option on a non-final season, this would mis-flag it -- not currently checked for. |
| `option_type` | carried over onto the option season only |
| `aav_naive_musd` | the flat total/years estimate from Layer 2 |
| `option_year_mismatch` | **self-check column.** True when the option season's computed `season_end_year` doesn't match the independently-parsed `option_year` text. Currently 29 rows flagged: 24 are the accepted extension-timing limitation (see below), 5 are rare/ambiguous phrasings not worth chasing for single-digit row counts. Treat new mismatches appearing after a re-run as worth investigating. |
| `aav_realized_musd` | real per-season salary from `modern.csv`, when available (deduplicated first -- see below) |
| `aav_final_musd` | `aav_realized_musd` if present; else, for seasons the Spotrac forward schedule covers, that schedule's salary (see `spotrac_schedule`); else `aav_naive_musd` |
| `aav_source` | `"realized"` (modern.csv salary actually paid), `"spotrac_schedule"` (future season priced from Spotrac's contract schedule), `"contract_terms"` (extension season priced from the contract's own stated total / term on purpose, even though a realized figure exists), `"estimated_flat"` (stated total / years, used only as a fallback when nothing better exists) or `"unknown"` -- **always check this before trusting `aav_final_musd`**; reliability differs a lot. `spotrac_schedule` is a contractual schedule, not salary already paid, and it reflects the snapshot date of the Spotrac export. |
| `spotrac_option` | Spotrac's option code for that season on `spotrac_schedule` rows: `T` team option, `P` player option, `NG` non-guaranteed, `UFA`/`RFA` the free-agency marker; empty otherwise. A listed option is still unexercised as of the snapshot. |
| `realized_overridden_by_terms` | True on extension seasons whose `aav_final_musd` was set from the contract's stated total / term although a realized salary existed (819 rows, 309 extensions as of 2026-10-07). The realized value stays in `aav_realized_musd` for reference. See "Extension timing" under Known limitations. |
| `season_claimed_by_later_deal` | True when Spotrac prices this player-season but a later unsuperseded contract event claimed it (Spotrac lists only the current deal), so this older event keeps its as-signed estimate. Informational; nothing is removed. |

**modern.csv duplicate-row fix applied:** `modern.csv` itself (as
generated upstream, outside this pipeline) had exact-duplicate rows for
4 player-seasons (one repeated 64 times) -- a data quality issue in
whatever builds `modern.csv`, not introduced here. Deduplicated before
the merge, with an `assert` in `attach_modern_salary()` that will fail
loudly if new duplicates appear in a future `modern.csv` update rather
than silently re-inflating row counts.

---

## Known limitations (accepted, not bugs)

- **Extension timing**: a contract extension signed while time remains
  on a player's prior deal (common for rookie-scale extensions, signed
  up to a year before taking effect) gets its `start_season` anchored to
  the signing date, not the prior contract's true end. This can put the
  season boundary ~1 year early. Explicitly accepted per design
  discussion -- fixing it would require linking each contract to a
  player's actual prior contract's outcome, which is more precision than
  the current use case needs. **Consequence handled 2026-10-07:** the
  early seasons of an extension coincide with the OLD contract's seasons,
  so a realized-salary join attached that other contract's pay to the
  extension (year 1 averaged 68% of the extension's own AAV; 44% of
  extensions were below 60%). Extensions with a stated value are now priced
  from their own terms (stated total / term, `contract_terms`; Spotrac may
  still price future seasons) and the realized figure is kept only in
  `aav_realized_musd`, flagged by `realized_overridden_by_terms`. The
  structural start is still as signed. Contract first, AAV secondary.
- **`is_option_season` assumption**: option applies to the final season
  only. Validated against 3 real examples, not proven universally.
- **Crosswalk 18% miss rate**: spot-checked as "probably genuine
  absences from modern.csv," not exhaustively verified. `nba_salaries.csv`
  has `spotrac_id`/`bref_id` columns that haven't been used yet and could
  resolve some of this gap -- open item.
- **`contract_id` hash sensitivity**: changes if PST edits the notes text
  or the scraper's text extraction changes, even for the same real-world
  event. Normalization (case/whitespace) mitigates trivial drift but
  doesn't eliminate this.
- **Coach/exec and overseas rows are flagged, not removed**, from
  `contracts_resolved.csv` (`is_coach_or_exec`, `has_contract_word`).
  Filter downstream if you want a pure NBA-player-contract view.
- **Trades file is per-team-side**, not deduped to one row per trade
  event (see trades section above).
