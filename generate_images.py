#!/usr/bin/env python3

import asyncio
import os
from html import escape
from pathlib import Path

import aiohttp

from github_stats import Stats

TEMPLATES = Path("templates")
OUTPUT = Path("generated")
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


################################################################################
# Helper Functions
################################################################################


def render(template: str, values: dict[str, str]) -> None:
    """
    Fill in a template's {{ placeholders }} and write it to the output folder
    :param template: file name of the template
    :param values: replacement text for each placeholder name
    """
    output = (TEMPLATES / template).read_text(encoding="utf-8")
    for key, value in values.items():
        output = output.replace(f"{{{{ {key} }}}}", value)

    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / template).write_text(output, encoding="utf-8")


def env_set(name: str) -> set[str] | None:
    """
    :param name: environment variable holding a comma-separated list
    :return: the list's stripped, non-empty items, or None if unset
    """
    raw = os.getenv(name)
    if not raw:
        return None
    return {x.strip() for x in raw.split(",") if x.strip()}


################################################################################
# Individual Image Generation Functions
################################################################################


async def generate_overview(s: Stats) -> None:
    """
    Generate an SVG badge with summary statistics
    :param s: Represents user's GitHub statistics
    """
    (
        name,
        stars,
        forks,
        contributions,
        (added, deleted),
        views,
        repos,
    ) = await asyncio.gather(
        s.name,
        s.stargazers,
        s.forks,
        s.total_contributions,
        s.lines_changed,
        s.views,
        s.repos,
    )
    render(
        "overview.svg",
        {
            "name": escape(name),
            "stars": f"{stars:,}",
            "forks": f"{forks:,}",
            "contributions": f"{contributions:,}",
            "lines_changed": f"{added + deleted:,}",
            "views": f"{views:,}",
            "repos": f"{len(repos):,}",
        },
    )


async def generate_languages(s: Stats) -> None:
    """
    Generate an SVG badge with summary languages used
    :param s: Represents user's GitHub statistics
    """
    progress = ""
    lang_list = ""
    sorted_languages = sorted(
        (await s.languages).items(), reverse=True, key=lambda t: t[1].get("size")
    )
    delay_between = 150
    for i, (lang, data) in enumerate(sorted_languages):
        color = data.get("color") or "#000000"
        progress += (
            f'<span style="background-color: {color};'
            f'width: {data.get("prop", 0):0.3f}%;" '
            f'class="progress-item"></span>'
        )
        lang_list += f"""
<li style="animation-delay: {i * delay_between}ms;">
<svg xmlns="http://www.w3.org/2000/svg" class="octicon" style="fill:{color};"
viewBox="0 0 16 16" version="1.1" width="16" height="16"><path
fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z"></path></svg>
<span class="lang">{escape(lang)}</span>
<span class="percent">{data.get("prop", 0):0.2f}%</span>
</li>

"""

    render("languages.svg", {"progress": progress, "lang_list": lang_list})


################################################################################
# Main Function
################################################################################


async def main() -> None:
    """
    Generate all badges
    """
    access_token = os.getenv("ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("A personal access token is required to proceed!")
    user = os.getenv("GITHUB_ACTOR")
    if user is None:
        raise RuntimeError("Environment variable GITHUB_ACTOR must be set.")
    # Convert a truthy value to a Boolean
    raw_ignore_forked_repos = os.getenv("EXCLUDE_FORKED_REPOS", "")
    ignore_forked_repos = raw_ignore_forked_repos.strip().lower() not in {"", "false"}

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        s = Stats(
            user,
            access_token,
            session,
            exclude_repos=env_set("EXCLUDED"),
            exclude_langs=env_set("EXCLUDED_LANGS"),
            ignore_forked_repos=ignore_forked_repos,
        )
        await asyncio.gather(generate_languages(s), generate_overview(s))


if __name__ == "__main__":
    asyncio.run(main())
