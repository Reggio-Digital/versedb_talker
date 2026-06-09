"""
VerseDB information source for ComicTagger

VerseDB (https://versedb.com) is a comic book database with a REST API. This
talker maps its public `/api` surface onto ComicTagger's metadata types.
"""

# Copyright 2026 Reggio Digital
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import time
from typing import Any, Callable, cast

import settngs
from comicapi import utils
from comicapi.genericmetadata import ComicSeries, GenericMetadata, ImageHash, MetadataOrigin
from comicapi.issuestring import IssueString
from comicapi.utils import LocationParseError, parse_url
from comictalker import talker_utils
from comictalker.comiccacher import ComicCacher
from comictalker.comiccacher import Issue as CCIssue
from comictalker.comiccacher import Series as CCSeries
from comictalker.comictalker import ComicTalker, RLCallBack, TalkerDataError, TalkerNetworkError

try:
    import niquests as requests
except ImportError:
    import requests

logger = logging.getLogger(f"comictalker.{__name__}")

# Media that read right-to-left, used to set the CIX "Manga" flag
RTL_MEDIUMS = {"manga"}
# Media that are still manga-family (left-to-right)
MANGA_FAMILY_MEDIUMS = {"manhwa", "manhua"}

# Search/issue list paging: stay well under the free hourly request cap
MAX_SEARCH_PAGES = 5
SERIES_PAGE_LIMIT = 50
ISSUE_PAGE_LIMIT = 100


class VerseDBTalker(ComicTalker):
    name: str = "VerseDB"
    id: str = "versedb"
    comictagger_min_ver: str = "1.6.0b7"
    website: str = "https://versedb.com"
    logo_url: str = f"{website}/images/logos/VerseDB-logo-square-1000x1000.png"
    token_url: str = f"{website}/my/apps/custom"
    attribution: str = f"Metadata provided by <a href='{website}'>{name}</a>"
    about: str = (
        f"<a href='{website}'>{name}</a> is a comic book database covering comics, manga, manhwa, "
        f"bande dessinée and magazines, with a REST API for cataloguing and discovery."
        f"<p>NOTE: A free <a href='{website}'>{name}</a> account and a personal API token "
        f"(scope <code>read:public</code>) are required. Generate one on your "
        f"<a href='{token_url}'>VerseDB apps page</a> and paste it "
        f"into the API Key field below. The free tier allows 300 requests/hour; a Pro subscription raises this "
        f"to 1000/hour.</p>"
    )

    def __init__(self, version: str, cache_folder: pathlib.Path) -> None:
        super().__init__(version, cache_folder)
        # Default settings
        self.default_api_url = self.api_url = f"{self.website}/api/"
        self.default_api_key = self.api_key = ""
        self.use_series_start_as_volume: bool = False
        self.fetch_variant_covers: bool = False
        # Polite spacing between requests; the server enforces the real hourly cap
        self.min_request_interval: float = 0.2
        self._last_request_ts: float = 0.0

    def register_settings(self, parser: settngs.Manager) -> None:
        parser.add_setting(
            "--versedb-use-series-start-as-volume",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Use series start as volume",
            help="Use the series start year as the volume number",
        )
        parser.add_setting(
            "--versedb-fetch-variant-covers",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Fetch variant covers",
            help="Fetch alternate variant covers to aid cover matching "
            "(requires a Pro subscription; one extra request per issue that has variants)",
        )
        parser.add_setting(
            f"--{self.id}-key",
            default="",
            display_name="API Key",
            help="Use the given VerseDB API token (scope read:public)",
        )
        parser.add_setting(
            f"--{self.id}-url",
            display_name="API URL",
            help=f"Use the given VerseDB API URL (default: {self.default_api_url})",
        )

    def parse_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        settings = super().parse_settings(settings)
        self.use_series_start_as_volume = settings["versedb_use_series_start_as_volume"]
        self.fetch_variant_covers = settings["versedb_fetch_variant_covers"]
        return settings

    def check_status(self, settings: dict[str, Any]) -> tuple[str, bool]:
        url = talker_utils.fix_url(settings[f"{self.id}_url"]) or self.default_api_url
        key = settings[f"{self.id}_key"] or self.default_api_key
        if not key:
            return f"An API token is required. Create one at {self.token_url}.", False
        try:
            resp = requests.get(
                url.rstrip("/") + "/series",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                    "user-agent": "comictagger/" + self.version,
                },
                params={"limit": 1},
                timeout=15,
            )
            if resp.status_code in (401, 403):
                return "Access denied. Invalid API token or missing read:public scope.", False
            if resp.status_code == 200:
                return "The API token is valid.", True
            return f"Unexpected response from VerseDB: HTTP {resp.status_code}", False
        except Exception as e:
            return f"Failed to connect to the API! {e}", False

    # ------------------------------------------------------------------ #
    # ComicTalker interface
    # ------------------------------------------------------------------ #

    def search_for_series(
        self,
        series_name: str,
        callback: Callable[[int, int], None] | None = None,
        refresh_cache: bool = False,
        literal: bool = False,
        series_match_thresh: int = 90,
        *,
        on_rate_limit: RLCallBack | None = None,
    ) -> list[ComicSeries]:
        # Meilisearch tokenizes punctuation itself (Spider-Man -> spider + man), so send the
        # query near-verbatim. ComicTagger's sanitize_title drops the hyphen with no space
        # ("spiderman"), which mashes two tokens into one and degrades relevance on this index.
        search_series_name = series_name.strip()
        logger.info("%s searching: %s", self.name, search_series_name)

        cvc = ComicCacher(self.cache_folder, self.version)
        if not refresh_cache and not literal:
            cached = cvc.get_search_results(self.id, search_series_name)
            if len(cached) > 0:
                return [self._format_series(json.loads(x[0].data)) for x in cached]

        params = {"q": search_series_name, "limit": SERIES_PAGE_LIMIT, "page": 1}
        response = self._get_content(self._url("series"), params, on_rate_limit=on_rate_limit)
        results: list[dict[str, Any]] = list(response.get("data", []))
        meta = response.get("meta", {})
        last_page = int(meta.get("last_page", 1) or 1)
        total = int(meta.get("total", len(results)) or len(results))

        if callback is not None:
            callback(len(results), total)

        page = 1
        while page < min(last_page, MAX_SEARCH_PAGES):
            if not literal and results:
                stop = any(
                    not utils.titles_match(search_series_name, r.get("name", ""), series_match_thresh)
                    for r in response.get("data", [])
                )
                if stop:
                    break
            page += 1
            params["page"] = page
            response = self._get_content(self._url("series"), params, on_rate_limit=on_rate_limit)
            results.extend(response.get("data", []))
            if callback is not None:
                callback(len(results), total)

        cvc.add_search_results(
            self.id,
            search_series_name,
            [CCSeries(id=str(r["id"]), data=json.dumps(r).encode("utf-8")) for r in results],
            False,
        )
        return [self._format_series(r) for r in results]

    def fetch_comic_data(
        self,
        issue_id: str | None = None,
        series_id: str | None = None,
        issue_number: str = "",
        *,
        on_rate_limit: RLCallBack | None = None,
    ) -> GenericMetadata:
        if issue_id:
            return self._fetch_issue_by_id(issue_id, on_rate_limit=on_rate_limit)
        if series_id and issue_number:
            return self._fetch_issue_by_series_and_number(series_id, issue_number, on_rate_limit=on_rate_limit)
        return GenericMetadata()

    def fetch_series(self, series_id: str, *, on_rate_limit: RLCallBack | None = None) -> ComicSeries:
        return self._format_series(self._fetch_series_record(series_id, on_rate_limit=on_rate_limit))

    def fetch_issues_in_series(
        self, series_id: str, *, on_rate_limit: RLCallBack | None = None
    ) -> list[GenericMetadata]:
        logger.debug("Fetching all issues in series: %s", series_id)
        cvc = ComicCacher(self.cache_folder, self.version)
        cached_issues = cvc.get_series_issues_info(str(series_id), self.id)
        cached_series = cvc.get_series_info(str(series_id), self.id)

        if cached_series is not None and cached_issues:
            series_data = json.loads(cached_series[0].data)
            if len(cached_issues) == series_data.get("cached_issues_count", -1):
                return [self._map_issue_list(json.loads(x[0].data)) for x in cached_issues]

        # include=series is required: the issue list omits the nested series block
        # unless asked, leaving series name and volume empty. (format/issue_count/
        # publisher aren't in the list's series column select; the detail endpoint fills those.)
        params = {
            "series_id": series_id,
            "include": "series",
            "limit": ISSUE_PAGE_LIMIT,
            "page": 1,
            "sort": "issue_number",
        }
        response = self._get_content(self._url("issues"), params, on_rate_limit=on_rate_limit)
        results: list[dict[str, Any]] = list(response.get("data", []))
        last_page = int(response.get("meta", {}).get("last_page", 1) or 1)

        page = 1
        while page < last_page:
            page += 1
            params["page"] = page
            response = self._get_content(self._url("issues"), params, on_rate_limit=on_rate_limit)
            results.extend(response.get("data", []))

        cvc.add_issues_info(
            self.id,
            [CCIssue(id=str(r["id"]), series_id=str(series_id), data=json.dumps(r).encode("utf-8")) for r in results],
            False,
        )
        return [self._map_issue_list(r) for r in results]

    def fetch_issues_by_series_issue_num_and_year(
        self,
        series_id_list: list[str],
        issue_number: str,
        year: str | int | None,
        *,
        on_rate_limit: RLCallBack | None = None,
    ) -> list[GenericMetadata]:
        int_year = utils.xlate_int(year)
        wanted = IssueString(issue_number).as_string().casefold()
        results: list[GenericMetadata] = []
        for series_id in series_id_list:
            for md in self.fetch_issues_in_series(series_id, on_rate_limit=on_rate_limit):
                if IssueString(md.issue).as_string().casefold() != wanted:
                    continue
                if int_year is not None and md.year is not None and md.year != int_year:
                    continue
                results.append(md)
                break
        return results

    # ------------------------------------------------------------------ #
    # Series helpers
    # ------------------------------------------------------------------ #

    def _fetch_series_record(self, series_id: str, *, on_rate_limit: RLCallBack | None = None) -> dict[str, Any]:
        cvc = ComicCacher(self.cache_folder, self.version)
        cached = cvc.get_series_info(str(series_id), self.id)
        if cached is not None and cached[1]:
            return cast("dict[str, Any]", json.loads(cached[0].data))

        record = self._get_content(self._url(f"series/{series_id}"), on_rate_limit=on_rate_limit).get("data") or {}
        if record:
            cvc.add_series_info(
                self.id,
                CCSeries(id=str(record["id"]), data=json.dumps(record).encode("utf-8")),
                True,
            )
        return record

    def _format_series(self, record: dict[str, Any]) -> ComicSeries:
        series = ComicSeries(
            id=str(record.get("id") or ""),
            name=record.get("name") or record.get("display_name") or "",
            aliases=set(record.get("aliases") or []),
            count_of_issues=utils.xlate_int(record.get("cached_issues_count")),
            count_of_volumes=None,
            description=record.get("description") or "",
            image_url=self._cover_url(record),
            publisher=self._publisher_name(record),
            start_year=utils.xlate_int(record.get("start_year")),
            format=record.get("format"),
        )
        slug = record.get("slug")
        if slug:
            try:
                series.web_links = [parse_url(f"{self._site_url()}/series/{record.get('id')}/{slug}")]
            except LocationParseError:
                ...
        return series

    # ------------------------------------------------------------------ #
    # Issue helpers
    # ------------------------------------------------------------------ #

    def _fetch_issue_by_id(self, issue_id: str, *, on_rate_limit: RLCallBack | None = None) -> GenericMetadata:
        cvc = ComicCacher(self.cache_folder, self.version)
        cached = cvc.get_issue_info(issue_id, self.id)
        if cached is not None and cached[1]:
            return self._map_issue_detail(json.loads(cached[0].data))

        record = self._get_content(self._url(f"issues/{issue_id}"), on_rate_limit=on_rate_limit).get("data", {})
        if record:
            if self.fetch_variant_covers and utils.xlate_int(record.get("variant_count")):
                record["variants"] = self._fetch_variants(record["id"], on_rate_limit=on_rate_limit)
            cvc.add_issues_info(
                self.id,
                [CCIssue(id=str(record["id"]), series_id=str(record.get("series_id", "")), data=json.dumps(record).encode("utf-8"))],
                True,
            )
        return self._map_issue_detail(record)

    def _fetch_variants(self, issue_id: Any, *, on_rate_limit: RLCallBack | None = None) -> list[dict[str, Any]]:
        # Variant covers sit behind a Pro-gated endpoint (HTTP 402 on the free tier); treat any
        # failure as "no variants" so this optional enrichment never breaks a tagging run.
        try:
            response = self._get_content(self._url(f"issues/{issue_id}/variants"), on_rate_limit=on_rate_limit)
        except TalkerNetworkError as e:
            logger.debug("%s could not fetch variants for issue %s: %s", self.name, issue_id, e)
            return []
        return list(response.get("data", []))

    def _fetch_issue_by_series_and_number(
        self, series_id: str, issue_number: str, *, on_rate_limit: RLCallBack | None = None
    ) -> GenericMetadata:
        wanted = IssueString(issue_number or "1").as_string().casefold()
        for md in self.fetch_issues_in_series(series_id, on_rate_limit=on_rate_limit):
            if IssueString(md.issue).as_string().casefold() == wanted and md.issue_id is not None:
                return self._fetch_issue_by_id(md.issue_id, on_rate_limit=on_rate_limit)
        return GenericMetadata()

    def _map_issue_list(self, issue: dict[str, Any]) -> GenericMetadata:
        """Partial metadata from the issue list resource (no credits/characters)."""
        series = issue.get("series") or {}
        md = GenericMetadata(
            data_origin=MetadataOrigin(self.id, self.name),
            issue_id=utils.xlate(issue.get("id")),
            series_id=utils.xlate(issue.get("series_id")),
            issue=utils.xlate(IssueString(str(issue.get("issue_number") or "")).as_string()),
            title=utils.xlate(issue.get("name")),
            series=utils.xlate(series.get("name")),
        )
        md.volume = utils.xlate_int(series.get("volume_number"))
        md.issue_count = utils.xlate_int(series.get("cached_issues_count"))
        md.format = series.get("format")
        if series.get("publisher_name"):
            md.publisher = series.get("publisher_name")
        self._apply_date(md, issue.get("cover_date"), issue.get("release_date"))
        self._apply_cover(md, issue)
        return md

    def _map_issue_detail(self, issue: dict[str, Any]) -> GenericMetadata:
        if not issue:
            return GenericMetadata()
        series = issue.get("series") or {}
        md = GenericMetadata(
            data_origin=MetadataOrigin(self.id, self.name),
            issue_id=utils.xlate(issue.get("id")),
            series_id=utils.xlate(issue.get("series_id")),
            issue=utils.xlate(IssueString(str(issue.get("issue_number") or "")).as_string()),
            title=utils.xlate(issue.get("name")),
            series=utils.xlate(series.get("name")),
            series_aliases=set(series.get("aliases") or []),
            description=issue.get("description"),
            issue_count=utils.xlate_int(series.get("cached_issues_count")),
            maturity_rating=issue.get("age_rating"),
            page_count=utils.xlate_int(issue.get("page_count")),
            language=series.get("language"),
            format=series.get("format"),
        )

        publisher = issue.get("publisher") or {}
        if publisher.get("name"):
            md.publisher = publisher["name"]
        imprint = issue.get("imprint") or {}
        if imprint.get("name"):
            md.imprint = imprint["name"]

        md.volume = utils.xlate_int(series.get("volume_number"))
        if self.use_series_start_as_volume:
            md.volume = utils.xlate_int(series.get("start_year"))

        md.genres = {g["name"] for g in series.get("genres", []) if g.get("name")}
        md.price = utils.xlate_float(issue.get("price"))
        md.gtin = issue.get("isbn") or issue.get("upc") or issue.get("ean")
        md.critical_rating = utils.xlate_float(issue.get("average_rating"))
        md.manga = self._manga_flag(series.get("medium"))

        self._apply_date(md, issue.get("cover_date"), issue.get("release_date"))
        self._apply_cover(md, issue)

        for variant in issue.get("variants", []):
            url = self._cover_url(variant)
            if url:
                md._alternate_images.append(ImageHash(URL=url, Hash=0, Kind=""))

        for character in issue.get("characters", []):
            if character.get("name"):
                md.characters.add(character["name"])
        for team in issue.get("teams", []):
            if team.get("name"):
                md.teams.add(team["name"])
        for arc in issue.get("story_arcs", []):
            if arc.get("name"):
                md.story_arcs.append(arc["name"])
        for creator in issue.get("creators", []):
            if creator.get("name"):
                md.add_credit(creator["name"], creator.get("role") or "", False)

        slug = issue.get("slug")
        if slug:
            try:
                md.web_links = [parse_url(f"{self._site_url()}/issue/{issue['id']}/{slug}")]
            except LocationParseError:
                ...
        return md

    # ------------------------------------------------------------------ #
    # Shared mapping utilities
    # ------------------------------------------------------------------ #

    @staticmethod
    def _apply_date(md: GenericMetadata, cover_date: str | None, release_date: str | None) -> None:
        date_str = cover_date or release_date
        if date_str:
            md.day, md.month, md.year = utils.parse_date_str(date_str)

    @staticmethod
    def _apply_cover(md: GenericMetadata, record: dict[str, Any]) -> None:
        url = (record.get("images") or {}).get("cover_lg") or record.get("cover_url")
        if url:
            md._cover_image = ImageHash(URL=url, Hash=0, Kind="")

    @staticmethod
    def _cover_url(record: dict[str, Any]) -> str:
        return (record.get("images") or {}).get("cover_lg") or record.get("cover_url") or ""

    @staticmethod
    def _publisher_name(record: dict[str, Any]) -> str:
        if record.get("publisher_name"):
            return record["publisher_name"]
        publisher = record.get("publisher") or {}
        return publisher.get("name") or ""

    @staticmethod
    def _manga_flag(medium: str | None) -> str | None:
        if medium in RTL_MEDIUMS:
            return "YesAndRightToLeft"
        if medium in MANGA_FAMILY_MEDIUMS:
            return "Yes"
        return None

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def _site_url(self) -> str:
        return self.api_url.rstrip("/").removesuffix("/api")

    def _url(self, path: str) -> str:
        return self.api_url.rstrip("/") + "/" + path.lstrip("/")

    def _get_content(
        self, url: str, params: dict[str, Any] | None = None, *, on_rate_limit: RLCallBack | None = None
    ) -> dict[str, Any]:
        response = self._get_url_content(url, params or {}, on_rate_limit=on_rate_limit)
        if isinstance(response, dict) and response.get("message") and "data" not in response:
            logger.debug("%s query failed: %s", self.name, response["message"])
            raise TalkerNetworkError(self.name, 0, str(response["message"]))
        return cast("dict[str, Any]", response)

    def _get_url_content(self, url: str, params: dict[str, Any], *, on_rate_limit: RLCallBack | None = None) -> Any:
        self._respect_interval()
        for tries in range(3):
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Accept": "application/json",
                        "user-agent": "comictagger/" + self.version,
                    },
                    timeout=30,
                )
                self._last_request_ts = time.time()

                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    raise TalkerNetworkError(
                        self.name, 1, "Access denied. Check your VerseDB API token and that it has the read:public scope."
                    )
                if resp.status_code == 404:
                    raise TalkerNetworkError(self.name, 0, "VerseDB returned 404 (not found).")
                if resp.status_code == 429:
                    wait = min(int(resp.headers.get("Retry-After", 5) or 5), 60)
                    self._notify_rate_limit(on_rate_limit, wait)
                    logger.warning("%s rate limit hit, waiting %s seconds", self.name, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    logger.debug("Try #%d: HTTP %d", tries + 1, resp.status_code)
                    time.sleep(1)
                    continue
                # Unhandled status (e.g. 400/422): surface it instead of failing opaquely.
                raise TalkerNetworkError(self.name, 0, f"Unexpected response from VerseDB: HTTP {resp.status_code}")
            except requests.exceptions.Timeout:
                logger.debug("Connection to %s timed out.", self.name)
                raise TalkerNetworkError(self.name, 4)
            except requests.exceptions.RequestException as e:
                logger.debug("Request exception: %s", e)
                raise TalkerNetworkError(self.name, 0, str(e)) from e
            except json.JSONDecodeError:
                logger.debug("JSON decode error", exc_info=True)
                raise TalkerDataError(self.name, 2, "VerseDB did not provide json")

        raise TalkerNetworkError(self.name, 5, "Retries exhausted (rate limit or server error).")

    @staticmethod
    def _notify_rate_limit(on_rate_limit: RLCallBack | None, wait: float) -> None:
        """Tell the host (e.g. GUI progress) we're about to block on a rate limit.

        on_rate_limit is ComicTagger's RLCallBack namedtuple: (callback, interval),
        where callback(full_delay, delay) and delay is capped to interval.
        """
        if on_rate_limit is None:
            return
        try:
            interval = getattr(on_rate_limit, "interval", 0) or 0
            delay = min(wait, interval) if interval > 0 else wait
            on_rate_limit.callback(float(wait), float(delay))
        except Exception:  # never let a host callback break the request flow
            logger.debug("on_rate_limit callback failed", exc_info=True)

    def _respect_interval(self) -> None:
        if self.min_request_interval <= 0:
            return
        elapsed = time.time() - self._last_request_ts
        if 0 < elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
