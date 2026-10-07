"""LeetCode reward discovery and a SQLite cache backed by GitHub records."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

from github_committer import GitHubCommitter, GitHubError
from leetcode_client import LeetCodeClient, LeetCodeError, REWARDS
from storage import _db, init_db


@dataclass
class SyncResult:
    completed: list[tuple[dict, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def message(self):
        lines = [f"✅ {r['title']} ({r['difficulty']}): {n} new commits; {r['commits']}/{r['commits']} reward complete." for r, n in self.completed if n]
        if not lines and not self.errors:
            lines.append("No new eligible Accepted problems found. Already rewarded problems are skipped.")
        lines.extend(f"⚠️ {error}" for error in self.errors)
        return "\n".join(lines)


class RewardService:
    def __init__(self, username=None, tzname=None, start_date=None, client=None, committer=None):
        self.username = (username or os.getenv("LEETCODE_USERNAME", "_mitraboga")).lower()
        self.tz = ZoneInfo(tzname or os.getenv("TZ", "Asia/Kolkata"))
        start = start_date or os.getenv("LEETCODE_START_DATE") or dt.datetime.now(self.tz).date().isoformat()
        self.start = dt.datetime.combine(dt.date.fromisoformat(start), dt.time.min, self.tz)
        self.client = client or LeetCodeClient()
        self.committer = committer

    def sync(self, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        result = SyncResult()
        init_db()
        with _db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS leetcode_rewards (
                username TEXT NOT NULL, slug TEXT NOT NULL, record TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(username, slug)
            )''')
        try:
            accepted = self.client.recent_accepted(self.username)
            for submission in accepted:
                if not self.start.timestamp() <= submission.timestamp <= now.timestamp():
                    continue
                with _db() as conn:
                    exists = conn.execute("SELECT 1 FROM leetcode_rewards WHERE username=? AND slug=?", (self.username, submission.slug)).fetchone()
                if exists:
                    continue
                difficulty = self.client.difficulty(submission.slug)
                record = {**asdict(submission), "username": self.username, "difficulty": difficulty,
                          "commits": REWARDS[difficulty],
                          "problem_url": f"https://leetcode.com/problems/{submission.slug}/",
                          "submission_url": f"https://leetcode.com/submissions/detail/{submission.id}/",
                          "day": dt.datetime.fromtimestamp(submission.timestamp, self.tz).date().isoformat()}
                with _db() as conn:
                    conn.execute("INSERT OR IGNORE INTO leetcode_rewards (username,slug,record) VALUES (?,?,?)", (self.username, submission.slug, json.dumps(record)))
        except LeetCodeError as exc:
            # Previously verified pending rewards can still be retried.
            result.errors.append(str(exc))
        with _db() as conn:
            pending = conn.execute("SELECT record FROM leetcode_rewards WHERE username=? AND completed=0 ORDER BY rowid", (self.username,)).fetchall()
        if not pending:
            return result
        try:
            committer = self.committer or GitHubCommitter.from_env()
            committer.preflight()
            for row in pending:
                record = json.loads(row["record"])
                created, canonical = committer.reward(record)
                with _db() as conn:
                    conn.execute("UPDATE leetcode_rewards SET completed=1, record=? WHERE username=? AND slug=?", (json.dumps(canonical), self.username, record["slug"]))
                result.completed.append((canonical, created))
        except GitHubError as exc:
            result.errors.append(str(exc))
        return result
