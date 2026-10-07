"""
Pro Sports Transactions contract-term scraper.

Pulls player-movement transaction rows from prosportstransactions.com,
filters to rows whose Notes field describes a contract signing/re-signing/
extension, and parses out structured terms: years, total value, AAV,
option flags, option year(s), guarantee status.

Scope: 2014-01-01 onwards, to align with existing Value Board contract data.

NOTE: Table parsing, options/AAV regex, and pagination detection have all
been validated against a real saved page (see git history / conversation
for the sample). Uses curl_cffi with impersonate="chrome124" instead of
plain requests, since the site's footer shows Cloudflare bot-management
JS (cf-beacon, __CF$cv$params) -- a static User-Agent header alone
wouldn't survive Cloudflare's TLS/JA3 fingerprint check.

STILL UNVERIFIED (couldn't test from my sandbox -- no network route to
this domain): whether impersonate="chrome124" is sufficient on its own,
or whether Cloudflare also wants a JS challenge solved / a clearance
cookie obtained via a real browser first. If you get blocked or see
challenge-page HTML instead of real content:
  - try bumping IMPERSONATE to a newer chrome version string curl_cffi
    supports (check `curl_cffi.requests.impersonate` options)
  - the session persisting cookies across requests should carry a
    clearance cookie once/if one is issued -- but if Cloudflare requires
    an interactive JS challenge, curl_cffi alone won't solve it and
    you'd need a headless browser (playwright) for at least the first
    request to mint a cookie, then hand that cookie to curl_cffi for the
    bulk scrape.
"""

from __future__ import annotations

import re
import time
import random
import csv
import sys
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Optional
from urllib.parse import urlencode

from curl_cffi import requests
from bs4 import BeautifulSoup

BASE = "https://www.prosportstransactions.com/basketball/Search/SearchResults.php"
PAGE_SIZE = 25  # inferred from the start= increments in the example URL
START_DATE = "2014-01-01"

# Site appears to be a small, long-running single-maintainer operation,
# but the page footer shows Cloudflare's bot-management/challenge-platform
# script (cf-beacon, __CF$cv$params), so a plain requests.get() with just a
# spoofed User-Agent header is likely to get JS-challenged or blocked --
# the fingerprinting operates at the TLS/JA3 level, not just headers.
# curl_cffi's impersonate= spoofs the actual TLS handshake of a real
# browser, which is what actually gets past Cloudflare here (same
# approach already in use for cdn.wnba.com).
#
# Be polite regardless: low concurrency (none -- sequential), randomized
# delay between requests.
MIN_DELAY = 1.5
MAX_DELAY = 3.0
IMPERSONATE = "chrome124"

# From the SessionNotCreatedException you hit: your installed Chrome is
# 137.x, but undetected_chromedriver's auto-detection grabbed a driver
# built for 151. Pinning this explicitly avoids that mismatch. Update if
# Chrome auto-updates on your machine later.
CHROME_MAJOR_VERSION = 137

# Confirmed via a captured browser request: this site's Cloudflare setup
# issues an actual interactive JS challenge (__cf_chl_tk token, cf_clearance
# cookie), not just passive TLS/JA3 fingerprinting. curl_cffi's impersonate=
# handles the latter but can't execute the challenge JS. Strategy: use
# Selenium (real browser automation) ONCE to solve the challenge and grab
# the resulting cf_clearance cookie + matching UA, then reuse that cookie in
# a curl_cffi session for the bulk of the scrape -- much faster than
# running a full browser per request. The cookie is short-lived and tied to
# IP + UA, so if you run this over a long session you may need to re-mint
# it partway through (bootstrap_clearance_cookie() is called again on a
# 403/challenge-page response).


def bootstrap_clearance_cookie(url: str) -> tuple[dict, str]:
    """Uses undetected_chromedriver to load `url`, click through Cloudflare's
    interactive Turnstile checkbox challenge, and return (cookies_dict,
    user_agent_string) to reuse in curl_cffi.

    Requires: pip install undetected-chromedriver

    Confirmed via screenshot: this site shows an explicit "Verify you are
    human" Turnstile checkbox (not an auto-resolving managed challenge),
    and vanilla selenium.webdriver.Chrome got stuck on it -- almost
    certainly because Cloudflare detected navigator.webdriver=true and
    similar automation tells before ever getting to the checkbox itself.
    undetected_chromedriver patches those tells at the binary/patch level
    (not just via Chrome options), which is the actual fix here.
    """
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By

    # undetected_chromedriver auto-detects your installed Chrome version to
    # pick a matching driver, but that detection can be wrong (e.g. it may
    # grab the latest driver release instead of matching your actual
    # browser). If you hit SessionNotCreatedException complaining about a
    # version mismatch, set version_main= to your Chrome's major version
    # number explicitly -- check yours with `google-chrome --version` or
    # chrome://version in the browser.
    driver = uc.Chrome(headless=False, version_main=CHROME_MAJOR_VERSION)
    try:
        driver.get(url)
        ua = driver.execute_script("return navigator.userAgent;")

        # Give the challenge script a moment to inject its iframe before
        # we go looking for it.
        time.sleep(3)

        clicked = False
        try:
            # The Turnstile checkbox lives inside a cross-origin iframe
            # (challenges.cloudflare.com), so we have to switch into it to
            # click the actual checkbox element rather than clicking a
            # coordinate on the parent page.
            iframe = driver.find_element(
                By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']"
            )
            driver.switch_to.frame(iframe)
            checkbox = driver.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
            checkbox.click()
            clicked = True
            driver.switch_to.default_content()
        except Exception:
            # No iframe/checkbox found -- either it already auto-resolved
            # (undetected_chromedriver alone is sometimes enough) or the
            # markup differs from what's assumed above. Either way, fall
            # through to the cookie-polling loop rather than failing here.
            driver.switch_to.default_content()

        clearance = None
        for _ in range(30):  # poll up to ~30s after the click
            cookies = driver.get_cookies()
            clearance = next(
                (c for c in cookies if c["name"] == "cf_clearance"), None
            )
            if clearance:
                break
            time.sleep(1)

        if not clearance:
            raise RuntimeError(
                "Still no cf_clearance cookie after "
                f"{'clicking the checkbox' if clicked else 'looking for a checkbox (none found)'}"
                ". Run with the browser visible and check by hand what's "
                "on screen at this point -- the iframe/checkbox selectors "
                "above are a best guess and may need adjusting to match "
                "this site's actual Turnstile markup."
            )

        cookie_dict = {c["name"]: c["value"] for c in driver.get_cookies()}
        return cookie_dict, ua
    finally:
        driver.quit()

# All 30 current franchises using the nickname format the site's search
# form expects (per the "e.g. Celtics" hint). Relocated/rebranded teams
# (e.g. SuperSonics -> Thunder) may need separate historical nicknames if
# you want pre-relocation coverage; not handled yet since scope is 2014+.
TEAM_NICKNAMES = [
    "Hawks", "Celtics", "Nets", "Hornets", "Bulls", "Cavaliers", "Mavericks",
    "Nuggets", "Pistons", "Warriors", "Rockets", "Pacers", "Clippers",
    "Lakers", "Grizzlies", "Heat", "Bucks", "Timberwolves", "Pelicans",
    "Knicks", "Thunder", "Magic", "76ers", "Suns", "Blazers", "Kings",
    "Spurs", "Raptors", "Jazz", "Wizards",
]
TEAM_NICKNAMES = [
    "Blazers"]
CONTRACT_KEYWORDS = re.compile(
    r"\b(signed|re-signed|resigned|extension|extended)\b", re.IGNORECASE
)

# ---- regex parsing of the Notes free-text field -------------------------

# e.g. "signed unrestricted free agent to a 3-year $47.4M contract; includes
#       player option for 2028-29"
# e.g. "re-signed unrestricted free agent to a 3-year / 4-year $13.7M
#       contract; includes team option for 2029-30"
YEARS_VALUE_RE = re.compile(
    r"(?P<years>\d+)-year(?:\s*/\s*(?P<years_alt>\d+)-year)?\s+"
    r"\$(?P<value>[\d.]+)M\s+contract",
    re.IGNORECASE,
)

OPTION_RE = re.compile(
    r"\b(?P<opt_type>player|team)\s+option\s+for\s+(?P<opt_year>\d{4}-\d{2})",
    re.IGNORECASE,
)

QUALIFYING_OFFER_RE = re.compile(r"\bqualifying offer\b", re.IGNORECASE)
NON_GUARANTEED_RE = re.compile(r"\bnon-guaranteed\b", re.IGNORECASE)
EXHIBIT10_RE = re.compile(r"\bExhibit 10\b", re.IGNORECASE)
TWO_WAY_RE = re.compile(r"\btwo-way\b", re.IGNORECASE)
EXTENSION_RE = re.compile(r"\bextension\b", re.IGNORECASE)
RE_SIGNED_RE = re.compile(r"\bre-?signed\b", re.IGNORECASE)


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
    years: Optional[int] = None
    years_alt: Optional[int] = None  # e.g. "3-year / 4-year" -> alt = 4
    total_value_musd: Optional[float] = None
    aav_musd: Optional[float] = None  # naive flat AAV; NOT year-by-year
    option_type: Optional[str] = None  # "player" / "team" / None
    option_year: Optional[str] = None  # e.g. "2028-29"
    parse_warnings: list = field(default_factory=list)


CHALLENGE_MARKERS = ("Just a moment", "cf-chl", "__cf_chl_tk")


def looks_like_challenge_page(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


def fetch_team_page(team: str, start: int, session: "requests.Session") -> str:
    params = {
        "Player": "",
        "Team": team,
        "BeginDate": START_DATE,
        "EndDate": "",
        "PlayerMovementChkBx": "yes",
        "Submit": "Search",
        "start": start,
    }
    url = f"{BASE}?{urlencode(params)}"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    if resp.status_code == 403 or looks_like_challenge_page(resp.text):
        raise NeedsFreshClearance(url)
    return resp.text


class NeedsFreshClearance(Exception):
    """Raised when a response looks like a Cloudflare challenge page --
    signals the caller to re-bootstrap the cf_clearance cookie."""
    def __init__(self, url: str):
        self.url = url
        super().__init__(f"Challenge page hit for {url}")


def parse_notes(notes: str) -> dict:
    """Extract structured fields from a Notes cell. Returns a dict of
    fields to merge into a ContractRow."""
    out = {
        "is_extension": bool(EXTENSION_RE.search(notes)),
        "is_resigning": bool(RE_SIGNED_RE.search(notes)),
        "is_qualifying_offer": bool(QUALIFYING_OFFER_RE.search(notes)),
        "is_non_guaranteed": bool(NON_GUARANTEED_RE.search(notes)),
        "is_exhibit10": bool(EXHIBIT10_RE.search(notes)),
        "is_two_way": bool(TWO_WAY_RE.search(notes)),
        "parse_warnings": [],
    }

    yv = YEARS_VALUE_RE.search(notes)
    if yv:
        years = int(yv.group("years"))
        years_alt = int(yv.group("years_alt")) if yv.group("years_alt") else None
        value = float(yv.group("value"))
        out["years"] = years
        out["years_alt"] = years_alt
        out["total_value_musd"] = value
        # naive flat AAV -- use the *longer* of years/years_alt if both given,
        # since the quoted total $ typically corresponds to the full term
        # including the option year. Flag this as an assumption.
        denom = years_alt or years
        if denom:
            out["aav_musd"] = round(value / denom, 3)
        out["parse_warnings"].append(
            "aav_musd is naive (total/years); does not reflect actual "
            "year-by-year raises. Cross-reference Spotrac for real schedule."
        )
    else:
        out["parse_warnings"].append("years/value pattern not matched")

    opt = OPTION_RE.search(notes)
    if opt:
        out["option_type"] = opt.group("opt_type").lower()
        out["option_year"] = opt.group("opt_year")

    return out


def parse_results_page(html: str, team: str) -> tuple[list[ContractRow], bool]:
    """Returns (rows, has_more_pages).

    has_more_pages is a guess based on whether a 'Next' link/text is
    present -- verify against real markup once you send back page source.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows_out: list[ContractRow] = []

    # confirmed via real page source: the data table is specifically
    # class="datatable center"; there's a second, separate pagination
    # table further down the page that we must not pick up here.
    table = soup.find("table", class_="datatable")
    if table is None:
        return rows_out, False

    # data rows carry no class of their own (just align="left"); the
    # header row is class="DraftTableLabel" -- skip it explicitly rather
    # than relying on position.
    trs = table.find_all("tr")
    for tr in trs:
        if "DraftTableLabel" in (tr.get("class") or []):
            continue
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        row_date = cells[0].get_text(strip=True)
        row_team = cells[1].get_text(strip=True)
        acquired = cells[2].get_text(" ", strip=True)
        relinquished = cells[3].get_text(" ", strip=True)
        notes = cells[4].get_text(" ", strip=True)

        if not CONTRACT_KEYWORDS.search(notes):
            continue  # trades / waivers / IL moves etc. -- not a signing

        # "Acquired" cell holds the player name (possibly with a leading
        # bullet char and, for trades, extra assets -- but we've already
        # filtered to signing-type notes above, so this should usually be
        # a single player name).
        player = acquired.lstrip("\u2022").strip()

        row = ContractRow(
            date=row_date, team=row_team, player=player, raw_notes=notes
        )
        parsed = parse_notes(notes)
        for k, v in parsed.items():
            setattr(row, k, v)
        rows_out.append(row)

    # Confirmed via real page source: "Next" renders as plain text
    # (<p class='bodyCopy'>Next</p>) on the last page, and only becomes an
    # <a href=...>Next</a> hyperlink when further pages exist. A naive
    # substring check on "Next" would wrongly think every page has more
    # pages, since the word appears either way.
    has_more = any(
        a.get_text(strip=True) == "Next" for a in soup.find_all("a")
    )

    return rows_out, has_more


def scrape_team(team: str, session: "requests.Session", max_pages: int = 200) -> list[ContractRow]:
    all_rows: list[ContractRow] = []
    start = 0
    for _ in range(max_pages):
        try:
            html = fetch_team_page(team, start, session)
        except NeedsFreshClearance as e:
            print(f"  Clearance expired mid-scrape, re-bootstrapping...", file=sys.stderr)
            cookies, ua = bootstrap_clearance_cookie(e.url)
            session.cookies.update(cookies)
            session.headers.update({"User-Agent": ua})
            html = fetch_team_page(team, start, session)  # retry once
        rows, has_more = parse_results_page(html, team)
        all_rows.extend(rows)
        print(f"  {team}: start={start} -> {len(rows)} contract rows "
              f"(has_more={has_more})", file=sys.stderr)
        if not has_more or not rows:
            break
        start += PAGE_SIZE
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return all_rows


def main():
    # impersonate= spoofs a real Chrome TLS/JA3 fingerprint for the whole
    # session (cookies persist across requests too).
    session = requests.Session(impersonate=IMPERSONATE)

    print("Bootstrapping Cloudflare clearance cookie via Selenium "
          "(a browser window will open briefly)...", file=sys.stderr)
    bootstrap_url = (
        f"{BASE}?{urlencode({'Player': '', 'Team': 'Celtics', 'BeginDate': START_DATE, 'EndDate': '', 'PlayerMovementChkBx': 'yes', 'Submit': 'Search', 'start': 0})}"
    )
    cookies, ua = bootstrap_clearance_cookie(bootstrap_url)
    session.cookies.update(cookies)
    session.headers.update({"User-Agent": ua})
    print("Clearance obtained.", file=sys.stderr)

    all_rows: list[ContractRow] = []

    for team in TEAM_NICKNAMES:
        print(f"Scraping {team}...", file=sys.stderr)
        try:
            team_rows = scrape_team(team, session)
            all_rows.extend(team_rows)
        except requests.exceptions.RequestException as e:
            print(f"  ERROR on {team}: {e}", file=sys.stderr)
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    out_path = "psx_contracts_2014_present.csv"
    fieldnames = list(asdict(all_rows[0]).keys()) if all_rows else []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in all_rows:
            d = asdict(row)
            d["parse_warnings"] = "; ".join(d["parse_warnings"])
            writer.writerow(d)

    print(f"Wrote {len(all_rows)} rows to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()