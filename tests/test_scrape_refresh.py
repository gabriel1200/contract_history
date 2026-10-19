import asyncio
from datetime import date

import pandas as pd
import pytest

import psx_contracts_v2 as scraper
from psx_contracts_v2 import merge_scraped_rows


def test_date_refresh_replaces_edited_and_deleted_rows_only_in_requested_slice(tmp_path):
    path = tmp_path / "contracts.csv"
    pd.DataFrame([
        {"date": "2026-09-23", "team": "Celtics", "player": "Older", "raw_notes": "old"},
        {"date": "2026-09-24", "team": "Celtics", "player": "Edited", "raw_notes": "old wording"},
        {"date": "2026-10-01", "team": "Celtics", "player": "Deleted", "raw_notes": "removed"},
        {"date": "2026-10-01", "team": "Hawks", "player": "Other team", "raw_notes": "keep"},
    ]).to_csv(path, index=False)

    fresh = pd.DataFrame([
        {"date": "2026-09-24", "team": "Celtics", "player": "Edited", "raw_notes": "corrected wording"},
    ])
    result = merge_scraped_rows(
        str(path), fresh, ["Celtics"], date(2026, 9, 24), date(2026, 10, 15),
        True, ["date", "team", "player", "raw_notes"],
    )

    assert set(zip(result.player, result.raw_notes)) == {
        ("Older", "old"), ("Edited", "corrected wording"), ("Other team", "keep"),
    }


def test_date_refresh_can_remove_all_rows_in_an_empty_category(tmp_path):
    path = tmp_path / "trades.csv"
    pd.DataFrame([
        {"date": "2026-09-23", "team": "Celtics", "raw_notes": "old trade"},
        {"date": "2026-09-24", "team": "Celtics", "raw_notes": "removed trade"},
    ]).to_csv(path, index=False)
    result = merge_scraped_rows(
        str(path), pd.DataFrame(), ["Celtics"], date(2026, 9, 24),
        date(2026, 10, 15), True, ["date", "team", "raw_notes"],
    )
    assert result.raw_notes.tolist() == ["old trade"]


def test_failed_team_does_not_change_saved_csvs(tmp_path, monkeypatch):
    path = tmp_path / "psx_contracts_v2_2014_present.csv"
    path.write_text("date,team,player,raw_notes\n2026-09-24,Celtics,Old,keep\n")
    original = path.read_bytes()
    monkeypatch.chdir(tmp_path)

    async def fail_team(*args, **kwargs):
        raise RuntimeError("request failed")

    monkeypatch.setattr(scraper, "scrape_team", fail_team)
    with pytest.raises(RuntimeError, match="existing CSVs were not changed"):
        asyncio.run(scraper._run(
            ["Celtics"], object(), date(2026, 9, 24), date(2026, 10, 15), True,
        ))
    assert path.read_bytes() == original


def test_failed_first_team_stops_before_trying_the_next(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    called = []

    async def fail_team(team, *args, **kwargs):
        called.append(team)
        raise RuntimeError("request failed")

    monkeypatch.setattr(scraper, "scrape_team", fail_team)
    with pytest.raises(RuntimeError, match="Scrape failed for Hawks"):
        asyncio.run(scraper._run(
            ["Hawks", "Celtics"], object(), date(2026, 9, 24), date(2026, 10, 15), True,
        ))
    assert called == ["Hawks"]


def test_browser_empty_results_finish_team_without_waiting(monkeypatch):
    class EmptyPage:
        async def goto(self, url, **kwargs):
            return None

        async def content(self):
            return ("<title>Basketball Transactions Search Results</title>"
                    "<p>There were no matching transactions found.</p>")

    handler = scraper.BrowserHandler(EmptyPage(), delay=0)

    class EmptySearch:
        async def get_dataframe(self):
            await handler.get("https://example.test/search", {})
            frame = pd.DataFrame(columns=["Date", "Team", "Acquired", "Relinquished", "Notes"])
            frame.attrs["errors"] = ("ValueError('No tables found')",)
            return frame

    monkeypatch.setattr(scraper.pst, "Search", lambda **kwargs: EmptySearch())
    assert asyncio.run(scraper.scrape_team(
        "Pacers", handler, start_date=date(2026, 9, 24), end_date=date(2026, 10, 15),
    )) == ([], [], [])
