# VerseDB for ComicTagger

Search and tag comics, manga, manhwa, manhua, bande dessinée and magazines from [VerseDB](https://versedb.com) without leaving [ComicTagger](https://github.com/comictagger/comictagger). This plugin adds VerseDB as a metadata source next to Comic Vine, Metron and GCD, so you can match a series and write full metadata straight to your archives.

## What is VerseDB?

[VerseDB](https://versedb.com) is a community-driven comic book database. It catalogues over a million issues across 150,000+ series, with 168,000+ characters, 86,000+ creators and thousands of teams and story arcs. Unlike most comic databases, it isn't built around western single issues alone. Manga, manhwa, manhua, bande dessinée and magazines are all first-class.

A few things make it a good source to tag from:

- **Broad coverage.** Western comics sit next to manga and international formats, so one source can tag a mixed shelf. Right-to-left reading is detected and flagged for manga.
- **Curated accuracy.** Records are maintained and corrected by collectors through a moderation system and reconciled across several data sources, so credits, characters and arcs stay clean.
- **Key issues marked.** First appearances, origins, deaths and major events are tagged on the issues that carry them.
- **API-first.** This plugin talks to VerseDB's documented public REST API, the same one that powers the website and mobile apps.

A free account is enough to tag with. Pro raises the hourly request budget and unlocks extras such as variant covers (see [Settings](#settings) and [Rate limits and caching](#rate-limits-and-caching)).

## Requirements

- ComicTagger 1.6.0b7 or later (the release that added the rate-limit callback the plugin uses).
- A VerseDB account and a personal API token with the `read:public` scope. A free account can [create one](https://versedb.com/my/apps/custom).

## Installing

How you install depends on how you run ComicTagger.

### Standalone app (Windows, macOS, Linux builds)

The packaged builds don't have a Python environment to install into, so they load plugins from a folder:

1. Download `versedb_talker-1.0.0-py3-none-any.whl` from the [latest release](https://github.com/Reggio-Digital/versedb_talker/releases/latest).
2. Move the `.whl` into ComicTagger's plugin directory (leave it as a file, don't unzip it):
   - Windows: `%APPDATA%\ComicTagger\plugins`
   - macOS: `~/Library/Application Support/ComicTagger/plugins`
   - Linux: `~/.config/ComicTagger/plugins`

   ComicTagger also shows this path under **Settings** as a clickable "Plugin Dir" link, if you'd rather open it from the app.
3. Restart ComicTagger, then select VerseDB under **Settings → Comic Sources**.

When you upgrade, delete the old `.whl` first: ComicTagger loads every plugin file in the folder and doesn't compare versions, so two copies will clash.

Rather build it yourself? From a checkout:

```sh
pip install build
python -m build
# produces dist/versedb_talker-1.0.0-py3-none-any.whl
```

### Pip / from source

If you run ComicTagger out of a Python environment, install the talker into that same environment. ComicTagger picks it up through the `comictagger.talker` entry point, so there's nothing else to register:

```sh
pip install versedb_talker   # once it is published to PyPI
pip install .                # from a checkout
```

Restart ComicTagger either way.

## Settings

Open **Settings → Comic Sources** and select VerseDB, or pass the flags on the command line:

| Setting | CLI flag | Notes |
| --- | --- | --- |
| API Key | `--versedb-key` | Your personal API token. Required. |
| API URL | `--versedb-url` | Defaults to `https://versedb.com/api/`. Only change this for a self-hosted or staging instance. |
| Use series start as volume | `--versedb-use-series-start-as-volume` | Writes the series start year into the volume field instead of the volume number. |
| Fetch variant covers | `--versedb-fetch-variant-covers` | Fetches alternate variant covers to help cover matching. Requires a Pro subscription; adds one request per issue that has variants. Off by default. |

Use **Test Source** to check the token before you start tagging.

## What it maps

A single issue lookup returns almost everything ComicTagger keeps, so tagging an issue is usually one request:

| ComicTagger field | Comes from |
| --- | --- |
| series / issue / title | series name, `issue_number`, issue name |
| series aliases | series alias list |
| publisher / imprint | issue publisher and imprint |
| year / month / day | `cover_date`, falling back to `release_date` |
| volume | series volume number (or start year, with the option above) |
| issue count | series `cached_issues_count` |
| description | issue description |
| genres | series genres |
| credits | issue creators, with their role |
| characters / teams / story arcs | the matching issue fields |
| page count / price | issue `page_count` and `price` |
| gtin | first of `isbn`, `upc`, `ean` |
| language / format | series language and format |
| maturity rating | issue `age_rating` |
| critical rating | issue average rating |
| manga flag | series medium (manga reads right-to-left; manhwa and manhua are flagged as manga) |
| cover | `images.cover_lg` |
| alternate covers | issue variant covers, with the "Fetch variant covers" option above (Pro-gated) |
| web link | `https://versedb.com/issue/{id}/{slug}` |

## Rate limits and caching

The API allows 300 requests an hour on the free tier and 1000 on Pro, counted against your account. The talker leaves a short gap between requests and stores results in ComicTagger's cache, so repeat lookups don't go back to the server. If it does hit a 429 it waits out the `Retry-After` window and retries. A large tagging run can still use up the free-tier budget, so Pro is the better fit for bulk work.

## Cross-referencing

VerseDB's own IDs are used as the source identifiers. The public API doesn't expose Comic Vine, GCD or Metron IDs, so there's no match-by-foreign-id shortcut: matching works by series search and issue number, the same as the other talkers.

## Content preferences

Search and issue results are filtered server-side by the content preferences on **your VerseDB account** (NSFW visibility, and any hidden mediums, languages or genres). If a series or its issues don't show up when you expect them to, check those settings on the website, since the token inherits them.

## Development

```sh
pip install -r requirements-dev.txt
pip install -e .
```

## License

Apache-2.0. See [LICENSE](LICENSE).
