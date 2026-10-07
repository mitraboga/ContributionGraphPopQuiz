"""Read public LeetCode metadata; never ask for a password or session cookie."""
from __future__ import annotations

from dataclasses import dataclass
import re
import requests

REWARDS = {"Easy": 10, "Medium": 20, "Hard": 50}


class LeetCodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Submission:
    id: str
    title: str
    slug: str
    timestamp: int


class LeetCodeClient:
    ENDPOINT = "https://leetcode.com/graphql/"

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "LeetCode-Commit-Bot/2.0",
                                     "Referer": "https://leetcode.com/"})

    def query(self, query: str, variables: dict) -> dict:
        try:
            response = self.session.post(self.ENDPOINT, json={"query": query, "variables": variables}, timeout=20)
        except requests.RequestException as exc:
            raise LeetCodeError("LeetCode could not be reached. No new rewards were issued; retry /check later.") from exc
        if response.status_code != 200:
            raise LeetCodeError(f"LeetCode returned HTTP {response.status_code}. No new rewards were issued; retry /check later.")
        try:
            payload = response.json()
            if payload.get("errors") or not isinstance(payload.get("data"), dict):
                raise ValueError("Missing GraphQL data")
            return payload["data"]
        except (ValueError, AttributeError, TypeError) as exc:
            raise LeetCodeError("LeetCode returned an unexpected response. No new rewards were issued.") from exc

    def recent_accepted(self, username: str) -> list[Submission]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
            raise LeetCodeError("Invalid LeetCode username in LEETCODE_USERNAME.")
        data = self.query('''query($username: String!, $limit: Int!) {
            matchedUser(username: $username) { username }
            recentAcSubmissionList(username: $username, limit: $limit) {
                id title titleSlug timestamp
            }
        }''', {"username": username, "limit": 20})
        if not data.get("matchedUser"):
            raise LeetCodeError(f"LeetCode profile {username} was not found.")
        try:
            rows = data["recentAcSubmissionList"]
            if not isinstance(rows, list):
                raise ValueError("Missing submissions")
            submissions = [Submission(str(r["id"]), str(r["title"]), str(r["titleSlug"]), int(r["timestamp"])) for r in rows]
            if any(s.timestamp <= 0 or not s.id.isdigit() or not re.fullmatch(r"[a-z0-9-]+", s.slug) for s in submissions):
                raise ValueError("Invalid submission")
            return sorted(submissions, key=lambda s: (s.timestamp, s.id))
        except (KeyError, ValueError, TypeError) as exc:
            raise LeetCodeError("LeetCode submission data changed. No new rewards were issued.") from exc

    def difficulty(self, slug: str) -> str:
        data = self.query('''query($slug: String!) {
            question(titleSlug: $slug) { difficulty }
        }''', {"slug": slug})
        difficulty = (data.get("question") or {}).get("difficulty")
        if difficulty not in REWARDS:
            raise LeetCodeError(f"Could not verify difficulty for {slug}. No reward was issued.")
        return difficulty

    def daily_problem(self) -> dict:
        data = self.query('''query {
            activeDailyCodingChallengeQuestion {
                date link question { title titleSlug difficulty }
            }
        }''', {})
        daily = data.get("activeDailyCodingChallengeQuestion") or {}
        question = daily.get("question") or {}
        if question.get("difficulty") not in REWARDS or not question.get("titleSlug"):
            raise LeetCodeError("LeetCode's daily problem is temporarily unavailable. You can solve any problem and use /check.")
        return question
