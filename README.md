<h1 align="center">🤖 Contribution Graph Pop Quiz → LeetCode Commit Bot 🧠</h1>
<h3 align="center">Telegram reminders, accepted LeetCode solves, and GitHub commit rewards</h3>
<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?logo=telegram" alt="Telegram" />
  <img src="https://img.shields.io/badge/GitHub-Actions%20%26%20REST-181717?logo=github" alt="GitHub" />
  <img src="https://img.shields.io/badge/SQLite-Cache-003B57?logo=sqlite" alt="SQLite" />
  <img src="https://img.shields.io/badge/Render-Webhooks-46E3B7?logo=render" alt="Render" />
</p>

This personal Telegram bot rewards Accepted LeetCode submissions from **[_mitraboga](https://leetcode.com/u/_mitraboga/)**. It verifies the problem's difficulty and creates a fixed number of commits in the configured GitHub repository.

| Problem difficulty | Commits per problem |
| --- | ---: |
| Easy | 10 |
| Medium | 20 |
| Hard | 50 |

The CS question bank remains available as optional practice. `/forcecommit` remains a manual fallback.

## How rewards work

1. Solve any LeetCode problem and receive **Accepted**. The daily challenge is a suggestion; any problem qualifies.
2. Send `/check` in Telegram, or wait for an automatic check.
3. The bot retrieves the profile's recent Accepted submissions and verifies difficulty.
4. Each problem gets one reward after activation, identified by username and problem slug. Repeated submissions of that problem do not earn more commits.
5. The bot creates 10, 20, or 50 JSON records, one per commit, on the destination repository's **default branch**.

Each record contains the problem title/link, submission ID/link, acceptance time, local date, difficulty, reward size, and reward step. These are **reward log commits**, not automatically uploaded solution code.

Rewards live at `leetcode/_mitraboga/<problem-slug>/001.json` through `010.json`, `020.json`, or `050.json`. The first record establishes the canonical reward. The last record confirms completion. If a request fails after 7 of 50 commits, `/check` resumes the unfinished reward and creates the remaining 43. Deterministic file paths prevent duplicate rewards across `/check`, restarts, GitHub Actions, and lost API responses.

The default activation date is **2026-10-07 in Asia/Kolkata**. Earlier submissions do not earn rewards. Set `LEETCODE_START_DATE` once and keep it fixed in both hosting and Actions. Solving an older problem again after activation can earn its first recorded reward; the public recent-submission list cannot establish your full lifetime solve history.

## Telegram commands

| Command | Behavior |
| --- | --- |
| `/start`, `/help` | Help and reward rules |
| `/daily` | Today's LeetCode challenge |
| `/check` | Discover accepted problems and retry pending rewards |
| `/status` | Linked profile, activation date, and reward settings |
| `/forcecommit [n] [tag]` | Manual override; default 1, maximum 50 commits |
| `/diagnose` | Read-only GitHub authentication check; no token values displayed |
| `/notify HH:MM [Area/City]` | Schedule a daily LeetCode reminder |
| `/when`, `/unnotify` | Show or disable the saved reminder |
| `/csquiz`, `/quiz` | Optional five-question CS practice; no reward commits |
| `/score` | Practice accuracy, including existing historical scores |
| `/streak` | Streak and solve totals from locally synced reward records |
| `/whoami` | Show Telegram user/chat IDs for setup |

Examples:

```text
/daily
/check
/forcecommit 10 busy-day
/notify 09:00 Asia/Kolkata
/csquiz
```

The obsolete contribution-count `/quiz` flow has been replaced by CS practice. Answers are consumed once, answered buttons are removed, old message callbacks cannot affect new questions, and questions rotate without repeats until the bank is exhausted within a running session. Only the configured owner can use reward commands, and only in a private chat.

## Local setup

Python 3.11 or newer is required. Get a bot token from [BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/mitraboga/ContributionGraphPopQuiz.git
cd ContributionGraphPopQuiz
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
pip install -r requirements.txt
```

macOS/Linux:

```bash
source .venv/bin/activate
cp .env.example .env
pip install -r requirements.txt
```

Fill in `.env`. Real secrets are excluded from Git and Docker builds:

```env
BOT_TOKEN=YOUR_BOTFATHER_TOKEN
TELEGRAM_USER_ID=YOUR_NUMERIC_USER_ID
TELEGRAM_CHAT_ID=YOUR_PRIVATE_CHAT_ID
LEETCODE_USERNAME=_mitraboga
LEETCODE_START_DATE=2026-10-07
TZ=Asia/Kolkata
GITHUB_TOKEN=YOUR_PERSONAL_ACCESS_TOKEN
GITHUB_REPO=mitraboga/YOUR_EXISTING_COMMIT_REPOSITORY
GH_USER_NAME=Mitra Boga
GH_USER_EMAIL=YOUR_VERIFIED_GITHUB_EMAIL
DB_PATH=quiz_scores.db
```

Run:

```bash
python main.py
```

Use `/whoami` to obtain your numeric IDs if needed, then update the environment and restart. In a private chat the user and chat IDs normally match. `TELEGRAM_CHAT_ID` can also supply owner identity when `TELEGRAM_USER_ID` is unset.

Create a **fine-grained personal access token** for the destination repository with **Contents: Read and write**. Use a verified GitHub email (or your GitHub-provided noreply address). `GH_USER_NAME` and `GH_USER_EMAIL` are optional as a pair; leave both empty to let GitHub attribute the commits to the token owner. A repository with no default branch needs an initial commit before rewards can be written.

## Update the existing Render bot

1. Merge/deploy the updated code on your Render service.
2. In **Render → your service → Environment**, replace the invalid `GITHUB_TOKEN` with a valid token. Keep `BOT_TOKEN` and the existing destination `GITHUB_REPO`.
3. Add `TELEGRAM_USER_ID`, `TELEGRAM_CHAT_ID`, `LEETCODE_USERNAME=_mitraboga`, `LEETCODE_START_DATE=2026-10-07`, and `TZ=Asia/Kolkata`. Keep the same activation date in Actions.
4. Set a custom `WEBHOOK_SECRET` using letters, digits, `_`, and `-`. Existing non-default secrets can be retained. Render's `RENDER_EXTERNAL_URL` automatically selects webhook mode.
5. Start command: `python main.py`. Build command: `pip install -r requirements.txt`.
6. Redeploy, then send `/start`, `/status`, and `/diagnose`. Once authentication succeeds, solve a problem and send `/check`.

The webhook uses `/telegram` and verifies Telegram's secret header. The secret is not embedded in URLs. Hosting environment values take precedence over `.env`, preventing stale local values from overriding the updated token.

The app checks submissions every **15 minutes while running**. [Render free services can sleep after inactivity and their local files are ephemeral](https://render.com/docs/free). SQLite reminder preferences, practice scores, pending queues, and locally synced streak history therefore need a persistent disk for durability. Reward deduplication itself survives a lost SQLite database because the records are stored in GitHub. `/streak` reports the synced cache; after cache loss, `/check` can repopulate only submissions still visible in LeetCode's recent list.

## GitHub Actions: checks while Render sleeps

The included `LeetCode reward sync` workflow checks at minutes **07 and 37 each hour**, even when Render is asleep. Its schedule is opt-in and runs from the default branch. Configure this repository under **Settings → Secrets and variables → Actions**:

**Secrets**

| Name | Value |
| --- | --- |
| `COMMIT_GITHUB_TOKEN` | Valid personal token with write access to the destination repository |
| `BOT_TOKEN` | Existing Telegram bot token, for completion/error notifications |
| `TELEGRAM_CHAT_ID` | Your private chat ID |

**Variables**

| Name | Value |
| --- | --- |
| `GITHUB_REPO` | Your existing destination repository, `owner/name` |
| `LEETCODE_USERNAME` | `_mitraboga` |
| `LEETCODE_START_DATE` | `2026-10-07` |
| `GH_USER_NAME`, `GH_USER_EMAIL` | Both set, or both unset, matching Render |
| `LEETCODE_SYNC_ENABLED` | `true` to enable scheduled rewards |
| `LEETCODE_REMINDER_ENABLED` | Optional `true` for a 09:00 IST reminder from Actions |

The Actions token secret is named `COMMIT_GITHUB_TOKEN` to distinguish it from GitHub's built-in workflow token. Use your personal token so commits are attributed to your account. The same destination and author settings must be used by Render and Actions.

After merging, run **Actions → LeetCode reward sync → Run workflow** to verify configuration. Leave `LEETCODE_REMINDER_ENABLED` unset if using `/notify` to avoid two daily reminders. Scheduled Actions can be delayed under load; `/check` remains available. See [GitHub's schedule guidance](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Troubleshooting

**GitHub 401:** The deployed token is invalid, expired, revoked, or incorrectly copied. Update **Render Environment → GITHUB_TOKEN** and **Actions secrets → COMMIT_GITHUB_TOKEN**, then redeploy. Updating code or `/setuser` cannot repair an invalid token. Never paste credentials into Telegram or commit them.

**GitHub 403/429:** Check Contents permission and repository access. If rate limited, wait before `/check`. Commits are serialized with a delay between writes. Pending reward steps remain retryable.

**GitHub 404:** Check `GITHUB_REPO`, token repository access, and the default branch. `/diagnose` checks authentication/read access without making a test commit; write permission is exercised by an actual reward or manual override.

**No new reward:** Confirm the profile, Accepted status, activation date, and whether that problem was already rewarded. LeetCode exposes only recent Accepted submissions (requested limit: 20), so long outages or more than 20 submissions between checks can hide unsynced solves. The website GraphQL interface is unofficial and can change or become unavailable; the bot reports errors instead of guessing completion or difficulty.

**Commits missing from the graph:** Commits must satisfy [GitHub's contribution requirements](https://docs.github.com/en/account-and-profile/how-tos/contribution-settings/troubleshooting-missing-contributions), including account-linked author email and an eligible repository/default branch. Private contributions also depend on profile visibility settings. The graph can take time to update. Reward records preserve the acceptance day, but commits appear on the day they are actually created; late retries do not backdate Git history.

**Manual override interrupted:** The response states how many commits completed. `/forcecommit` creates a new manual request every time; choose only the remaining count if you intend to complete the same total. It does not mark LeetCode problems solved or extend their streak.

## Validation and files

```bash
pip install pytest
python -m pytest -q
python -m compileall -q main.py rewards.py github_committer.py leetcode_client.py
# Read-only GitHub configuration check; creates no commits:
python github_committer.py
```

`main.py` handles Telegram commands and schedules; `leetcode_client.py` reads public metadata; `rewards.py` queues and syncs rewards; `github_committer.py` creates resumable GitHub records; `storage.py` retains existing SQLite tables; `questions.py` holds the optional CS bank. `sync_leetcode.py` and `send_daily.py` provide standalone Actions entry points. Tests use in-memory GitHub fixtures, never live reward writes.

## 👤 Author

<p align="center">
  <b style="font-size:18px;">Mitra Boga</b><br/><br/>

  <!-- LinkedIn: true blue label + lighter-blue username block -->
  <a href="https://www.linkedin.com/in/bogamitra/" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/LinkedIn-bogamitra-4DA3FF?style=for-the-badge&logo=linkedin&logoColor=white&labelColor=0A66C2" />
  </a>

  <!-- X: near-black label + darker-gray username block (dark-mode friendly) -->
  <a href="https://x.com/techtraboga" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/X-@techtraboga-3A3F45?style=for-the-badge&logo=x&logoColor=white&labelColor=111418" />
  </a>
</p>
