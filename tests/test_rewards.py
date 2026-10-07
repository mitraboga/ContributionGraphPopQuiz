import base64
import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

import main
import storage
from github_committer import GitHubCommitter, GitHubError
from leetcode_client import LeetCodeClient, LeetCodeError, REWARDS, Submission
from rewards import RewardService


class Response:
    def __init__(self, status, data=None):
        self.status_code = status
        self.data = data
    def json(self):
        return self.data


class MemoryGitHub:
    def __init__(self):
        self.headers = {}
        self.files = {}
        self.writes = []
        self.fail_step = None
        self.lose_response = None
    def request(self, method, url, **kwargs):
        if '/contents/' not in url:
            return Response(200, {'default_branch': 'main'})
        path = url.split('/contents/')[1]
        if method == 'GET':
            if path not in self.files:
                return Response(404)
            return Response(200, {'content': base64.b64encode(json.dumps(self.files[path]).encode()).decode()})
        payload = json.loads(base64.b64decode(kwargs['json']['content']))
        if self.fail_step == payload['step']:
            return Response(403)
        if path in self.files:
            return Response(422)
        self.files[path] = payload
        self.writes.append(kwargs['json'])
        if payload['step'] == self.lose_response:
            self.lose_response = None
            raise requests.Timeout('response lost')
        return Response(201)


def record(difficulty='Easy', slug='two-sum', id='123'):
    return {'username': '_mitraboga', 'id': id, 'title': 'Two Sum', 'slug': slug,
            'timestamp': 1791336600, 'difficulty': difficulty, 'commits': REWARDS[difficulty], 'day': '2026-10-07'}


@pytest.mark.parametrize('difficulty,count', [('Easy',10), ('Medium',20), ('Hard',50)])
def test_exact_rewards_and_replays(difficulty, count):
    remote = MemoryGitHub()
    committer = GitHubCommitter('test-token', 'me/repo', session=remote, write_delay=0)
    created, canonical = committer.reward(record(difficulty))
    assert created == count == len(remote.writes)
    assert all(w['branch'] == 'main' for w in remote.writes)
    # Another Accepted submission of the same problem must not add commits.
    created, canonical2 = committer.reward(record(difficulty, id='456'))
    assert created == 0 and canonical2 == canonical
    assert len(remote.writes) == count


@pytest.mark.parametrize('lost_response', [False, True])
def test_partial_reward_resumes_without_extra_commits(lost_response):
    remote = MemoryGitHub()
    remote.lose_response = 7 if lost_response else None
    remote.fail_step = None if lost_response else 7
    committer = GitHubCommitter('test-token', 'me/repo', session=remote, write_delay=0)
    with pytest.raises(GitHubError):
        committer.reward(record('Hard'))
    assert len(remote.writes) == (7 if lost_response else 6)
    remote.fail_step = None
    committer.reward(record('Hard'))
    assert len(remote.writes) == 50
    assert len(remote.files) == 50


def test_author_is_actually_sent():
    remote = MemoryGitHub()
    committer = GitHubCommitter('test-token', 'me/repo', 'Mitra', 'verified@example.com', session=remote, write_delay=0)
    committer.reward(record())
    assert remote.writes[0]['author'] == {'name': 'Mitra', 'email': 'verified@example.com'}
    assert remote.writes[0]['committer'] == remote.writes[0]['author']


def test_401_reports_deployment_fix_without_token():
    remote = MemoryGitHub()
    remote.request = lambda *a, **kw: Response(401)
    committer = GitHubCommitter('secret-should-not-appear', 'me/repo', session=remote)
    with pytest.raises(GitHubError, match='401') as error:
        committer.preflight()
    assert 'secret-should-not-appear' not in str(error.value)
    assert 'Replace' in str(error.value)


def test_forcecommit_default_and_cap():
    remote = MemoryGitHub()
    committer = GitHubCommitter('test-token', 'me/repo', session=remote, write_delay=0)
    assert committer.commit_n() == 1
    assert list(remote.files.values())[0]['kind'] == 'manual_override'
    with pytest.raises(GitHubError):
        committer.commit_n(51)
    assert len(remote.writes) == 1


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'DB_PATH', str(tmp_path / 'quiz.db'))
    storage.init_db()


class FakeLeetCode:
    def __init__(self, submissions, difficulty='Easy'):
        self.submissions = submissions
        self.d = difficulty
    def recent_accepted(self, username):
        return self.submissions
    def difficulty(self, slug):
        return self.d


def test_date_filter_duplicates_cache_loss_and_timezone(db):
    cutoff = int(dt.datetime(2026,10,6,18,30,tzinfo=dt.timezone.utc).timestamp())
    client = FakeLeetCode([Submission('1','Old','old',cutoff-1), Submission('2','New','new',cutoff),
                          Submission('3','Again','new',cutoff+10), Submission('4','Future','future',cutoff+10000)])
    remote = MemoryGitHub()
    committer = GitHubCommitter('test-token','me/repo',session=remote,write_delay=0)
    service = RewardService('_mitraboga','Asia/Kolkata','2026-10-07',client,committer)
    now = dt.datetime.fromtimestamp(cutoff+60,dt.timezone.utc)
    first = service.sync(now)
    assert len(remote.writes) == 10
    assert first.completed[0][0]['day'] == '2026-10-07'
    assert not service.sync(now).completed
    # Actions has a fresh SQLite DB on each run; GitHub still deduplicates.
    with storage._db() as conn:
        conn.execute('DELETE FROM leetcode_rewards')
    second = service.sync(now)
    assert second.completed[0][1] == 0
    assert len(remote.writes) == 10


def test_pending_retries_next_day_even_without_recent_submission(db):
    now = dt.datetime(2026,10,7,12,tzinfo=dt.timezone.utc)
    client = FakeLeetCode([Submission('1','Two Sum','two-sum',int(now.timestamp()))])
    remote = MemoryGitHub(); remote.fail_step = 4
    service = RewardService('_mitraboga','Asia/Kolkata','2026-10-07',client,
        GitHubCommitter('test-token','me/repo',session=remote,write_delay=0))
    assert service.sync(now).errors
    assert len(remote.writes) == 3
    remote.fail_step = None
    client.submissions = []
    result = service.sync(now+dt.timedelta(days=1))
    assert result.completed[0][1] == 7
    assert len(remote.writes) == 10


@pytest.mark.parametrize('data', [{'errors':[{'message':'bad'}]}, {'data':None}, {'data':{'matchedUser':None,'recentAcSubmissionList':[]}}])
def test_leetcode_malformed_or_missing_user_is_not_success(data):
    session=MagicMock();session.post.return_value=Response(200,data)
    with pytest.raises(LeetCodeError):
        LeetCodeClient(session).recent_accepted('_mitraboga')


def test_unavailable_difficulty_is_not_guessed():
    session=MagicMock();session.post.return_value=Response(200,{'data':{'question':None}})
    with pytest.raises(LeetCodeError):
        LeetCodeClient(session).difficulty('two-sum')


def test_unnotify_is_persisted(db):
    storage.set_notify_time(1,1,9,0,'Asia/Kolkata')
    storage.clear_notify_time(1,1)
    assert list(storage.iter_all_notify_prefs()) == []


def make_update(uid=1):
    update=MagicMock()
    update.effective_user.id=uid
    update.effective_chat.id=1
    update.effective_chat.type='private'
    update.effective_message.reply_text=AsyncMock()
    update.callback_query=None
    return update


def test_other_users_cannot_force_commits(monkeypatch):
    import asyncio
    monkeypatch.setenv('TELEGRAM_USER_ID','1')
    update=make_update(2)
    context=MagicMock()
    context.args=['50']
    asyncio.run(main.forcecommit(update,context))
    assert 'Owner access' in update.effective_message.reply_text.call_args.args[0]
    assert not context.application.bot_data.__getitem__.called


def test_cs_answer_replay_and_stale_buttons(db,monkeypatch):
    import asyncio
    monkeypatch.setenv('TELEGRAM_USER_ID','1')
    question=main.BANK[0]
    today=dt.datetime.now(main.ZoneInfo(main.DEFAULT_TZ)).date().isoformat()
    context=MagicMock()
    context.user_data={'cs_q':{'nonce':'abc','message_id':8,'chat_id':1,'question':question,'day':today,'answered':False}}
    update=make_update()
    query=MagicMock()
    query.data=f'cs:abc:{question.correct_index}'
    query.answer=AsyncMock(); query.edit_message_text=AsyncMock(); query.edit_message_reply_markup=AsyncMock()
    query.message.message_id=8;query.message.chat_id=1
    update.callback_query=query
    asyncio.run(main.cs_callback(update,context))
    asyncio.run(main.cs_callback(update,context))
    assert storage.get_daily_count(1,1,today) == 1
    assert storage.get_score(1,1) == (1,1)
    markup=query.edit_message_text.call_args.kwargs['reply_markup']
    assert len(markup.inline_keyboard)==1
    assert markup.inline_keyboard[0][0].callback_data=='cs:abc:next'
    context.user_data['cs_q']['answered']=False
    query.data='cs:old:0'
    asyncio.run(main.cs_callback(update,context))
    assert storage.get_daily_count(1,1,today)==1


def test_cs_bank_rotates_without_repeats(db,monkeypatch):
    import asyncio
    monkeypatch.setenv('TELEGRAM_USER_ID','1')
    update=make_update()
    context=MagicMock();context.user_data={}
    context.bot.send_message=AsyncMock(return_value=MagicMock(message_id=7))
    asyncio.run(main.csquiz(update,context)); q1=context.user_data['cs_q']['question']
    asyncio.run(main.csquiz(update,context)); q2=context.user_data['cs_q']['question']
    assert q1 != q2
    assert len(context.user_data['cs_seen'])==2


def test_application_registers_leetcode_and_optional_cs_commands():
    app=main.build_application('123456:TEST_ONLY')
    commands=set()
    for handler in app.handlers[0]:
        commands.update(getattr(handler,'commands',[]))
    assert {'daily','check','forcecommit','quiz','csquiz','diagnose','whoami'} <= commands
    assert app.post_init is main.post_init
