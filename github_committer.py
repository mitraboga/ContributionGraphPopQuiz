"""Serial, resumable GitHub Contents API writes with durable reward records."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import time
import uuid
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from leetcode_client import REWARDS


class GitHubError(RuntimeError):
    pass


class GitHubCommitter:
    def __init__(self, token, repo, author_name="", author_email="", session=None, write_delay=1.0):
        if not token or not repo:
            raise GitHubError("Set GITHUB_TOKEN and GITHUB_REPO in the bot's hosting environment.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise GitHubError("GITHUB_REPO must be owner/repository.")
        if bool(author_name) != bool(author_email):
            raise GitHubError("Set both GH_USER_NAME and GH_USER_EMAIL, or leave both unset to use the token owner.")
        self.repo = repo
        self.author = {"name": author_name, "email": author_email} if author_name else None
        self.sess = session or requests.Session()
        self.sess.headers.update({"Authorization": f"Bearer {token.strip()}",
                                  "Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28",
                                  "User-Agent": "LeetCode-Commit-Bot/2.0"})
        self.base = f"https://api.github.com/repos/{repo}"
        self.branch = None
        self.write_delay = write_delay

    @classmethod
    def from_env(cls):
        return cls(os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPO", ""),
                   os.getenv("GH_USER_NAME", ""), os.getenv("GH_USER_EMAIL", ""))

    def request(self, method, url, **kwargs):
        try:
            return self.sess.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise GitHubError("GitHub could not be reached. Any completed reward commits are preserved; retry /check.") from exc

    def raise_error(self, response):
        code = response.status_code
        try:
            payload = response.json()
            message = payload.get("message", "") if isinstance(payload, dict) else ""
        except ValueError:
            message = ""
        if not isinstance(message, str):
            message = ""
        # Keep GitHub's reason, but never echo authentication values if an
        # unexpected response contains them. Do not include raw response bodies.
        token = self.sess.headers.get("Authorization", "").removeprefix("Bearer ")
        if token:
            message = message.replace(token, "[redacted]")
        message = " ".join(message.split())[:180]
        reason = f" GitHub says: {message}." if message else ""
        headers = {key.lower(): value for key, value in (getattr(response, "headers", {}) or {}).items()}
        if code == 401:
            raise GitHubError("GitHub 401: the deployed GITHUB_TOKEN is invalid, expired, or revoked. Replace it in Render Environment (and COMMIT_GITHUB_TOKEN in Actions secrets), then redeploy. Never paste the token into Telegram.")
        if code in (403, 429):
            limited = (code == 429 or headers.get("x-ratelimit-remaining") == "0"
                       or "retry-after" in headers or "rate limit" in message.lower())
            if limited:
                wait = "Wait at least 60 seconds before retrying."
                if str(headers.get("retry-after", "")).isdigit():
                    wait = f"Wait {headers['retry-after']} seconds before retrying."
                elif str(headers.get("x-ratelimit-reset", "")).isdigit():
                    seconds = max(1, int(headers["x-ratelimit-reset"]) - int(time.time()))
                    wait = f"Wait {seconds} seconds for the rate limit to reset before retrying."
                raise GitHubError(f"GitHub {code}: rate limit reached for {self.repo}.{reason} {wait} Completed reward steps are preserved.")
            if "resource not accessible" in message.lower():
                raise GitHubError(f"GitHub {code}: the token cannot access the requested operation on {self.repo}.{reason} In GitHub token settings, select this exact destination repository and grant Contents: Read and write. It must match Render's GITHUB_REPO. Save the token settings, then retry. Completed reward steps are preserved.")
            raise GitHubError(f"GitHub {code}: access denied for {self.repo}.{reason} Check token repository access, Contents: Read and write, and repository rules. No rate-limit signal was returned. Completed reward steps are preserved.")
        if code == 404:
            raise GitHubError("GitHub 404: GITHUB_REPO or its default branch is unavailable to this token. Check the repository and token access.")
        raise GitHubError(f"GitHub HTTP {code}: the operation failed. Completed reward steps are preserved; retry /check.")

    def preflight(self):
        response = self.request("GET", self.base)
        if response.status_code != 200:
            self.raise_error(response)
        self.branch = response.json()["default_branch"]
        # Do not silently reward on a non-default branch, where contributions may not count.
        return f"GitHub authentication/read access OK: {self.repo}, default branch {self.branch}. This does not verify Contents write permission; the token must select this exact repository and grant Contents: Read and write."

    def read_json(self, path):
        response = self.request("GET", f"{self.base}/contents/{quote(path, safe='/')}", params={"ref": self.branch})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            self.raise_error(response)
        try:
            return json.loads(base64.b64decode(response.json()["content"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise GitHubError(f"Reward record at {path} is invalid; refusing to overwrite it.") from exc

    def create_json(self, path, payload, message):
        existing = self.read_json(path)
        if existing is not None:
            if existing != payload:
                raise GitHubError(f"Reward record at {path} differs; refusing to overwrite it.")
            return False
        body = {"message": message, "branch": self.branch,
                "content": base64.b64encode((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()).decode()}
        if self.author:
            body.update(author=self.author, committer=self.author)
        if self.write_delay:
            time.sleep(self.write_delay)
        response = self.request("PUT", f"{self.base}/contents/{quote(path, safe='/')}", json=body)
        if response.status_code == 201:
            return True
        # Another process may have created the same deterministic file. A request
        # may also have succeeded even if its response was lost. Never overwrite.
        if response.status_code in (409, 422):
            existing = self.read_json(path)
            if existing == payload:
                return False
        self.raise_error(response)

    def reward(self, record):
        """One reward per username/problem, with exactly N total commits, even on retry."""
        if self.branch is None:
            self.preflight()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", record["username"]) or not re.fullmatch(r"[a-z0-9-]+", record["slug"]):
            raise GitHubError("Invalid reward path.")
        prefix = f"leetcode/{record['username'].lower()}/{record['slug']}"
        first = self.read_json(f"{prefix}/001.json")
        if first is not None:
            record = first.get("reward")
            if not isinstance(record, dict) or f"leetcode/{record.get('username', '').lower()}/{record.get('slug')}" != prefix:
                raise GitHubError("Reward metadata differs; refusing to overwrite it.")
        if record["difficulty"] not in REWARDS or record["commits"] != REWARDS[record["difficulty"]]:
            raise GitHubError("Invalid reward difficulty/count; no new commits issued.")
        if first is not None:
            final = self.read_json(f"{prefix}/{record['commits']:03d}.json")
            if final == {"reward": record, "step": record["commits"]}:
                return 0, record
        created = 0
        for index in range(1, record["commits"] + 1):
            payload = {"reward": record, "step": index}
            created += self.create_json(f"{prefix}/{index:03d}.json", payload,
                                        f"leetcode: {record['title']} ({record['difficulty']}) reward {index}/{record['commits']}")
        return created, record

    def commit_n(self, n=1, tag=None):
        if not 1 <= n <= 50:
            raise GitHubError("/forcecommit count must be between 1 and 50.")
        self.preflight()
        today = dt.datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Kolkata"))).date().isoformat()
        run_id = uuid.uuid4().hex
        created = 0
        for index in range(1, n + 1):
            payload = {"kind": "manual_override", "day": today, "tag": tag or "manual", "run_id": run_id, "step": index}
            try:
                created += self.create_json(f"manual/{today}/{run_id}/{index:03d}.json", payload, f"manual: {today} override {index}/{n}")
            except GitHubError as exc:
                raise GitHubError(f"Manual request completed {created}/{n} commits. {exc} A new /forcecommit starts a separate request.") from exc
        return created


def diagnose_config():
    # No token fragments or personal email values in diagnostics.
    return "\n".join(f"{key}: {'SET' if os.getenv(key) else 'MISSING'}" for key in ("GITHUB_TOKEN", "GITHUB_REPO", "GH_USER_NAME", "GH_USER_EMAIL"))


def make_daily_commits_if_configured(n=1, tag=None):
    try:
        committer = GitHubCommitter.from_env()
        count = committer.commit_n(n, tag)
        return f"Committed {count} manual override records to {committer.repo}."
    except GitHubError as exc:
        return f"Commit failed: {exc}"


if __name__ == "__main__":
    print(diagnose_config())
    try:
        print(GitHubCommitter.from_env().preflight())
    except GitHubError as exc:
        print(exc)
        raise SystemExit(1)
