#!/usr/bin/env python3

import asyncio
import os
import time
from collections.abc import Mapping
from typing import Any

import aiohttp

API_URL = "https://api.github.com"
MAX_ATTEMPTS = 5
MAX_RETRY_DELAY = 60
RETRY_STATUSES = {500, 502, 503, 504}


###############################################################################
# Main Classes
###############################################################################


class Queries:
    """
    Class with functions to query the GitHub GraphQL (v4) API and the REST (v3)
    API. Also includes functions to dynamically generate GraphQL queries.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        max_connections: int = 10,
    ):
        self.username = username
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
        """
        :param headers: response headers of a rate-limited request
        :param attempt: zero-based number of the attempt that failed
        :return: seconds to wait before retrying, as advised by GitHub
        """
        if "retry-after" in headers:
            delay = float(headers["retry-after"])
        elif headers.get("x-ratelimit-remaining") == "0":
            delay = float(headers.get("x-ratelimit-reset", 0)) - time.time()
        else:
            delay = 2**attempt
        return min(max(delay, 1), MAX_RETRY_DELAY)

    async def _request(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        """
        Make an authenticated request, retrying on network errors, rate limits
        and server errors. URLs are never logged, so that the names of private
        repositories do not leak into public Actions logs.
        :param method: HTTP method
        :param path: API path to request
        :return: HTTP status and decoded JSON body (None when empty)
        """
        url = f"{API_URL}/{path.lstrip('/')}"
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with (
                    self.semaphore,
                    self.session.request(
                        method, url, headers=self.headers, **kwargs
                    ) as r,
                ):
                    rate_limited = r.status == 429 or (
                        r.status == 403
                        and (
                            "retry-after" in r.headers
                            or r.headers.get("x-ratelimit-remaining") == "0"
                        )
                    )
                    if not rate_limited and r.status not in RETRY_STATUSES:
                        return r.status, await r.json(content_type=None)
                    print(f"Request returned {r.status}. Retrying...")
                    delay = self._retry_delay(r.headers, attempt)
            except (TimeoutError, aiohttp.ClientError) as e:
                print(f"Request failed with {type(e).__name__}. Retrying...")
                delay = 2**attempt
            await asyncio.sleep(delay)
        raise RuntimeError(f"Request failed after {MAX_ATTEMPTS} attempts")

    async def query(self, generated_query: str) -> dict:
        """
        Make a request to the GraphQL API using the authentication token from
        the environment
        :param generated_query: string query to be sent to the API
        :return: decoded GraphQL JSON output
        """
        status, result = await self._request(
            "POST", "graphql", json={"query": generated_query}
        )
        if status != 200 or not isinstance(result, dict) or not result.get("data"):
            # Fail loudly rather than publishing images full of zeros
            raise RuntimeError(
                f"GraphQL query failed with status {status}. "
                "Check that the ACCESS_TOKEN secret is valid and not expired."
            )
        for error in result.get("errors", []):
            print(f"GraphQL query returned a {error.get('type', 'UNKNOWN')} error")
        return result

    async def query_rest(self, path: str, params: dict | None = None) -> Any:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :return: deserialized REST JSON output, or an empty dict on failure
        """
        for _ in range(60):
            status, result = await self._request("GET", path, params=params or {})
            if status == 202:
                # GitHub is still computing the statistics for this repository
                print("A path returned 202. Retrying...")
                await asyncio.sleep(2)
                continue
            # Other statuses are expected, e.g. 403 for the traffic of
            # repositories the user cannot push to, or 204 for empty ones
            return result if status == 200 and result is not None else {}
        print("There were too many 202s. Data for this repository will be incomplete.")
        return {}

    @staticmethod
    def repos_overview(
        contrib_cursor: str | None = None, owned_cursor: str | None = None
    ) -> str:
        """
        :return: GraphQL query with overview of user repositories
        """
        return f"""{{
  viewer {{
    login,
    name,
    repositories(
        first: 100,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        isFork: false,
        after: {"null" if owned_cursor is None else '"' + owned_cursor + '"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
    repositoriesContributedTo(
        first: 100,
        includeUserRepositories: false,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        contributionTypes: [
            COMMIT,
            PULL_REQUEST,
            REPOSITORY,
            PULL_REQUEST_REVIEW
        ]
        after: {"null" if contrib_cursor is None else '"' + contrib_cursor + '"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    @staticmethod
    def contrib_years() -> str:
        """
        :return: GraphQL query to get all years the user has been a contributor
        """
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        """
        :param year: year to query for
        :return: portion of a GraphQL query with desired info for a given year
        """
        return f"""
    year{year}: contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{int(year) + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: list[str]) -> str:
        """
        :param years: list of years to get contributions for
        :return: query to retrieve contribution information for all user years
        """
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Stats:
    """
    Retrieve and store statistics about GitHub usage.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        exclude_repos: set[str] | None = None,
        exclude_langs: set[str] | None = None,
        ignore_forked_repos: bool = False,
    ):
        self.username = username
        self._ignore_forked_repos = ignore_forked_repos
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self.queries = Queries(username, access_token, session)
        # Images are generated concurrently, so make sure the expensive
        # repository overview query only runs once
        self._stats_lock = asyncio.Lock()

        self._name: str | None = None
        self._stargazers: int | None = None
        self._forks: int | None = None
        self._total_contributions: int | None = None
        self._languages: dict[str, Any] | None = None
        self._repos: set[str] | None = None
        self._lines_changed: tuple[int, int] | None = None
        self._views: int | None = None

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        lines_changed = await self.lines_changed
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.repos)}
Lines of code added: {lines_changed[0]:,}
Lines of code deleted: {lines_changed[1]:,}
Lines of code changed: {lines_changed[0] + lines_changed[1]:,}
Project page views: {await self.views:,}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes
        """
        async with self._stats_lock:
            if self._repos is not None:
                return
            await self._get_stats()

    async def _get_stats(self) -> None:
        name = None
        stargazers = 0
        forks = 0
        languages: dict[str, Any] = {}
        seen_repos: set[str] = set()

        exclude_langs_lower = {x.lower() for x in self._exclude_langs}

        next_owned = None
        next_contrib = None
        while True:
            raw_results = await self.queries.query(
                Queries.repos_overview(
                    owned_cursor=next_owned, contrib_cursor=next_contrib
                )
            )
            viewer = raw_results.get("data", {}).get("viewer", {})
            name = viewer.get("name") or viewer.get("login") or "No Name"

            contrib_repos = viewer.get("repositoriesContributedTo") or {}
            owned_repos = viewer.get("repositories") or {}

            repos = list(owned_repos.get("nodes", []))
            if not self._ignore_forked_repos:
                repos += contrib_repos.get("nodes", [])

            for repo in repos:
                if repo is None:
                    continue
                repo_name = repo.get("nameWithOwner")
                if repo_name in seen_repos or repo_name in self._exclude_repos:
                    continue
                seen_repos.add(repo_name)
                stargazers += repo.get("stargazers", {}).get("totalCount", 0)
                forks += repo.get("forkCount", 0)

                for lang in repo.get("languages", {}).get("edges", []):
                    lang_name = lang.get("node", {}).get("name", "Other")
                    if lang_name.lower() in exclude_langs_lower:
                        continue
                    if lang_name in languages:
                        languages[lang_name]["size"] += lang.get("size", 0)
                        languages[lang_name]["occurrences"] += 1
                    else:
                        languages[lang_name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color"),
                        }

            owned_page = owned_repos.get("pageInfo", {})
            contrib_page = contrib_repos.get("pageInfo", {})
            if owned_page.get("hasNextPage") or contrib_page.get("hasNextPage"):
                next_owned = owned_page.get("endCursor", next_owned)
                next_contrib = contrib_page.get("endCursor", next_contrib)
            else:
                break

        # TODO: Improve languages to scale by number of contributions to
        #       specific filetypes
        langs_total = sum(v.get("size", 0) for v in languages.values())
        for v in languages.values():
            v["prop"] = 100 * v.get("size", 0) / langs_total if langs_total else 0

        self._name = name
        self._stargazers = stargazers
        self._forks = forks
        self._languages = languages
        self._repos = seen_repos

    @property
    async def name(self) -> str:
        """
        :return: GitHub user's name (e.g., Jacob Strieb)
        """
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert self._name is not None
        return self._name

    @property
    async def stargazers(self) -> int:
        """
        :return: total number of stargazers on user's repos
        """
        if self._stargazers is not None:
            return self._stargazers
        await self.get_stats()
        assert self._stargazers is not None
        return self._stargazers

    @property
    async def forks(self) -> int:
        """
        :return: total number of forks on user's repos
        """
        if self._forks is not None:
            return self._forks
        await self.get_stats()
        assert self._forks is not None
        return self._forks

    @property
    async def languages(self) -> dict:
        """
        :return: summary of languages used by the user
        """
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert self._languages is not None
        return self._languages

    @property
    async def languages_proportional(self) -> dict:
        """
        :return: summary of languages used by the user, with proportional usage
        """
        languages = await self.languages
        return {k: v.get("prop", 0) for (k, v) in languages.items()}

    @property
    async def repos(self) -> set[str]:
        """
        :return: list of names of user's repos
        """
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert self._repos is not None
        return self._repos

    @property
    async def total_contributions(self) -> int:
        """
        :return: count of user's total contributions as defined by GitHub
        """
        if self._total_contributions is not None:
            return self._total_contributions

        years = (
            (await self.queries.query(Queries.contrib_years()))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionYears", [])
        )
        total = 0
        if years:
            by_year = (
                (await self.queries.query(Queries.all_contribs(years)))
                .get("data", {})
                .get("viewer", {})
                .values()
            )
            for year in by_year:
                total += year.get("contributionCalendar", {}).get(
                    "totalContributions", 0
                )
        self._total_contributions = total
        return total

    async def _repo_lines_changed(self, repo: str) -> tuple[int, int]:
        """
        :param repo: repository in owner/name format
        :return: lines added and deleted by the user in the repository
        """
        additions = 0
        deletions = 0
        r = await self.queries.query_rest(f"/repos/{repo}/stats/contributors")
        for author_obj in r:
            # Handle malformed response from the API by skipping this repo
            if not isinstance(author_obj, dict) or not isinstance(
                author_obj.get("author", {}), dict
            ):
                continue
            author = author_obj.get("author", {}).get("login", "")
            if author != self.username:
                continue

            for week in author_obj.get("weeks", []):
                additions += week.get("a", 0)
                deletions += week.get("d", 0)
        return additions, deletions

    @property
    async def lines_changed(self) -> tuple[int, int]:
        """
        :return: count of total lines added, removed, or modified by the user
        """
        if self._lines_changed is not None:
            return self._lines_changed
        per_repo = await asyncio.gather(
            *(self._repo_lines_changed(repo) for repo in await self.repos)
        )
        self._lines_changed = (
            sum(a for a, _ in per_repo),
            sum(d for _, d in per_repo),
        )
        return self._lines_changed

    async def _repo_views(self, repo: str) -> int:
        """
        :param repo: repository in owner/name format
        :return: page views the repository received in the last 14 days
        """
        r = await self.queries.query_rest(f"/repos/{repo}/traffic/views")
        if not isinstance(r, dict):
            return 0
        return sum(view.get("count", 0) for view in r.get("views", []))

    @property
    async def views(self) -> int:
        """
        Note: only returns views for the last 14 days (as-per GitHub API)
        :return: total number of page views the user's projects have received
        """
        if self._views is not None:
            return self._views
        per_repo = await asyncio.gather(
            *(self._repo_views(repo) for repo in await self.repos)
        )
        self._views = sum(per_repo)
        return self._views


###############################################################################
# Main Function
###############################################################################


async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    if access_token is None or user is None:
        raise RuntimeError(
            "ACCESS_TOKEN and GITHUB_ACTOR environment variables cannot be None!"
        )
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())


if __name__ == "__main__":
    asyncio.run(main())
