"""
Count lines of code written across all owned GitHub repositories (public + private)
and update the Lines of Code badge in README.md.

Runs automatically via .github/workflows/update-loc.yml whenever commits are pushed.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

README_PATH = Path(__file__).resolve().parents[2] / "README.md"
MAX_SINGLE_COMMIT_LINES = 250_000  # skip raw dataset dumps (e.g. multi-million-line CSV imports)


def get_token() -> str:
    for key in ("LOC_TOKEN", "GH_PAT", "GITHUB_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return subprocess.check_output(["gh", "auth", "token"]).decode().strip()


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ansarbuilds-loc-updater",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if "errors" in payload and not payload.get("data"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload


REPOS_QUERY = """
query($cursor: String) {
  viewer {
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes { name owner { login } }
    }
  }
}
"""

BATCH_FRAGMENT = """
fragment Hist on Repository {
  name
  owner { login }
  defaultBranchRef {
    target {
      ... on Commit {
        history(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes {
            additions
            parents { totalCount }
            author { name }
          }
        }
      }
    }
  }
}
"""

PAGE_QUERY = """
query($owner: String!, $name: String!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              additions
              parents { totalCount }
              author { name }
            }
          }
        }
      }
    }
  }
}
"""


def fetch_all_repos(token: str) -> list[dict]:
    repos = []
    cursor = None
    while True:
        data = gql(token, REPOS_QUERY, {"cursor": cursor})["data"]["viewer"]["repositories"]
        repos.extend(data["nodes"])
        if data["pageInfo"]["hasNextPage"]:
            cursor = data["pageInfo"]["endCursor"]
        else:
            break
    return repos


def count_lines(token: str) -> tuple[int, int, int]:
    repos = fetch_all_repos(token)

    def fetch_batch(batch: list[dict]) -> list[dict]:
        aliases = []
        for idx, r in enumerate(batch):
            owner_json = json.dumps(r["owner"]["login"])
            name_json = json.dumps(r["name"])
            aliases.append(f"r{idx}: repository(owner: {owner_json}, name: {name_json}) {{ ...Hist }}")
        query = "query { " + " ".join(aliases) + " }\n" + BATCH_FRAGMENT
        return list(gql(token, query)["data"].values())

    batches = [repos[i : i + 8] for i in range(0, len(repos), 8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        batch_results = list(pool.map(fetch_batch, batches))

    total_lines = 0
    total_commits = 0
    for batch in batch_results:
        for repo in batch:
            if not repo or not repo.get("defaultBranchRef") or not repo["defaultBranchRef"].get("target"):
                continue
            hist = repo["defaultBranchRef"]["target"]["history"]
            commits = list(hist["nodes"])
            cursor = hist["pageInfo"]["endCursor"] if hist["pageInfo"]["hasNextPage"] else None
            while cursor:
                page = gql(
                    token,
                    PAGE_QUERY,
                    {"owner": repo["owner"]["login"], "name": repo["name"], "cursor": cursor},
                )
                hist2 = page["data"]["repository"]["defaultBranchRef"]["target"]["history"]
                commits.extend(hist2["nodes"])
                cursor = hist2["pageInfo"]["endCursor"] if hist2["pageInfo"]["hasNextPage"] else None

            for c in commits:
                author_name = ((c.get("author") or {}).get("name") or "").lower()
                if "github-actions" in author_name or "[bot]" in author_name:
                    continue
                if c["parents"]["totalCount"] > 1:
                    continue
                additions = c.get("additions") or 0
                if additions > MAX_SINGLE_COMMIT_LINES:
                    continue
                total_lines += additions
                total_commits += 1

    return total_lines, total_commits, len(repos)


def build_badge(formatted_lines: str) -> str:
    encoded = urllib.parse.quote(formatted_lines)
    return (
        f'<!-- loc-start --><img src="https://img.shields.io/badge/Lines%20of%20Code-{encoded}-e6d5bd'
        f'?style=for-the-badge&labelColor=101010" alt="Lines of Code: {formatted_lines}" /><!-- loc-end -->'
    )


def update_readme(total_lines: int) -> bool:
    formatted = f"{total_lines:,}"
    badge = build_badge(formatted)
    content = README_PATH.read_text(encoding="utf-8")

    pattern = re.compile(r"<!-- loc-start -->.*?<!-- loc-end -->", re.DOTALL)
    if pattern.search(content):
        updated = pattern.sub(badge, content)
    else:
        raise RuntimeError("Could not find <!-- loc-start -->...<!-- loc-end --> marker in README.md")

    if updated == content:
        print(f"README.md already up to date ({formatted} lines of code).")
        return False

    README_PATH.write_text(updated, encoding="utf-8")
    print(f"Updated README.md to {formatted} lines of code.")
    return True


if __name__ == "__main__":
    tok = get_token()
    lines, commits, repo_count = count_lines(tok)
    print(f"Counted {lines:,} lines written across {commits:,} commits in {repo_count} repositories.")
    update_readme(lines)
