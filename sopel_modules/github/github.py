# coding=utf8
"""
github.py - Sopel GitHub Module
Copyright 2015 Max Gurela
Copyright 2019 dgw

 _______ __ __   __           __
|     __|__|  |_|  |--.--.--.|  |--.
|    |  |  |   _|     |  |  ||  _  |
|_______|__|____|__|__|_____||_____|

"""

from __future__ import unicode_literals
from sopel import tools
from sopel.module import OP, NOLIMIT, commands, example, require_chanmsg, rule, url
from sopel.formatting import bold, color, monospace
from sopel.tools.time import get_timezone, format_time
from sopel.config.types import StaticSection, ValidatedAttribute

from . import formatting
from .formatting import shorten_url, emojize
from .webhook import setup_webhook, shutdown_webhook

import operator
from collections import deque

import sys
if sys.version_info.major < 3:
    from urllib import urlencode
    from urllib2 import HTTPError
else:
    from urllib.parse import urlencode
    from urllib.error import HTTPError
import json
import requests
import re
import datetime
import base64

'''
 _______           __         __
|   |   |.-----.--|  |.--.--.|  |.-----.
|       ||  _  |  _  ||  |  ||  ||  -__|
|__|_|__||_____|_____||_____||__||_____|

'''

# GitHub enforces alphanumeric usernames, and allows only one punctuation character: hyphen ('-')
# Regex copied and slightly modified to meet our needs from CC0 source:
# https://github.com/shinnn/github-username-regex/blob/0794566cc10e8c5a0e562823f8f8e99fa044e5f4/module.js#L1
githubUsername = r'[A-Za-z\d](?:[A-Za-z\d]|-(?=[A-Za-z\d])){0,38}'
# GitHub additionally allows dots ('.') in repo names, as well as hyphens
# not copied from anywhere, but handy to simply reuse
githubRepoSlug = r'[A-Za-z0-9\.\-]+'
# lots of regex and other globals to make this stuff work
baseURL = r'https?://(?:www\.)?github\.com/({username}/{repo})'.format(username=githubUsername, repo=githubRepoSlug)
repoURL = baseURL + r'/?(?!\S)'
issueURL = baseURL + r'/(?:issues|pull)/([\d]+)(?:#issuecomment-([\d]+))?'
commitURL = baseURL + r'/(?:commit)/([A-z0-9\-]+)'
contentURL = baseURL + r'/(?:blob|raw)/([^/\s]+)/([^#\s]+)(?:#L(\d+)(?:-L(\d+))?)?'


class GitHubSection(StaticSection):
    client_id = ValidatedAttribute('client_id', default=None)
    client_secret = ValidatedAttribute('client_secret', default=None)
    secret = ValidatedAttribute('secret', default=None)  # TODO remove in 0.3.0 or thereabouts
    webhook = ValidatedAttribute('webhook', bool, default=False)
    webhook_host = ValidatedAttribute('webhook_host', default='0.0.0.0')
    webhook_port = ValidatedAttribute('webhook_port', default='3333')
    external_url = ValidatedAttribute('external_url', default='http://your_ip_or_domain_here:3333')


def configure(config):
    config.define_section('github', GitHubSection, validate=False)
    config.github.configure_setting('client_id', 'GitHub API Client ID')
    config.github.configure_setting('client_secret', 'GitHub API Client Secret')
    config.github.configure_setting('webhook', 'Enable webhook listener functionality')
    if config.github.webhook:
        config.github.configure_setting('webhook_host', 'Listen IP for incoming webhooks (0.0.0.0 for all IPs)')
        config.github.configure_setting('webhook_port', 'Listen port for incoming webhooks')
        config.github.configure_setting('external_url', 'Callback URL for webhook activation, should be your externally facing domain or IP. You must include the port unless you are reverse proxying.')


def setup(sopel):
    sopel.config.define_section('github', GitHubSection)

    if sopel.config.github.webhook:
        setup_webhook(sopel)

    if not sopel.config.github.client_secret:
        if sopel.config.github.secret:
            sopel.config.github.client_secret = sopel.config.github.secret
            del sopel.config.github.secret
            sopel.config.save()
            tools.stderr("[GitHub] Migrated `secret` to `client_secret` in config.")


def shutdown(sopel):
    shutdown_webhook(sopel)

'''
 _______ ______ _____        ______                    __
|   |   |   __ |     |_     |   __ |.---.-.----.-----.|__|.-----.-----.
|   |   |      <       |    |    __||  _  |   _|__ --||  ||     |  _  |
|_______|___|__|_______|    |___|   |___._|__| |_____||__||__|__|___  |
                                                                |_____|
'''


def fetch_api_endpoint(bot, url):
    # GitHub deprecated passing authentication via query parameters in November
    # 2019. Passing OAuth client credentials as user/password instead is the
    # supported replacement:
    # https://developer.github.com/changes/2020-02-10-deprecating-auth-through-query-param/
    auth = None
    if bot.config.github.client_id and bot.config.github.client_secret:
        auth = (bot.config.github.client_id, bot.config.github.client_secret)
    return requests.get(url, auth=auth).text


@rule(r'.*(?<!\S)/?#(\d+)\b.*')
@require_chanmsg
def issue_reference(bot, trigger):
    """
    Separate function to work around Sopel not loading rules/commands for @url callables.
    """
    issue_info(bot, trigger)


@url(issueURL)
def issue_info(bot, trigger, match=None):
    if match:  # Link triggered
        repo = match.group(1)
        num = match.group(2)
        URL = 'https://api.github.com/repos/%s/issues/%s' % (repo, num)
        if (match.group(3)):
            URL = 'https://api.github.com/repos/%s/issues/comments/%s' % (repo, match.group(3))
    else:  # Issue/PR number triggered
        repo = bot.db.get_channel_value('github_issue_repo', trigger.sender)
        num = trigger.group(1)
        if not repo:
            return NOLIMIT
        URL = 'https://api.github.com/repos/%s/issues/%s' % (repo, num)

    try:
        raw = fetch_api_endpoint(bot, URL)
    except HTTPError:
        bot.say('[GitHub] API returned an error.')
        return NOLIMIT
    data = json.loads(raw)
    try:
        body = data['body']
    except (KeyError):
        bot.say('[GitHub] API says this is an invalid issue. Please report this if you know it should work!')
        return NOLIMIT

    if body.strip() == '':
        body = 'No description provided.'
    else:
        body = formatting.fmt_short_comment_body(body)

    type_ = 'issue'
    state = data['state']
    if 'pull_request' in data:
        type_ = 'PR'

    if type_ == 'PR' and state == 'closed':
        # annoying consequence of "all PRs are issues, but not all issues are PRs"
        # merge status is only included if the object is fetched via `pulls` endpoint
        try:
            pr_raw = fetch_api_endpoint(bot, data['pull_request']['url'])
        except HTTPError:
            # just use the "issue" state, fine
            pass

        pr_data = json.loads(pr_raw)
        if pr_data.get("merged"):
            state = "merged"

    response = [
        bold('[GitHub]'),
        ' [',
        repo,
        ' #',
        num,
        '] ',
        state,
        ' ',
        type_,
        ' by ',
        data['user']['login'],
        ': '
    ]

    if ('title' in data):
        response.append(emojize(data['title']))
        response.append(bold(' | '))
    response.append(emojize(body))

    # append link, if not triggered by a link
    if not match:
        link = shorten_url(data['html_url'])
        response.append(bold(' | '))
        response.append(link)

    bot.say(''.join(response))


@commands('gh-repo')
@example('.gh-repo sopel-irc/sopel-github')
@require_chanmsg('[GitHub] You can only link a repository to a channel.')
def manage_channel_repo(bot, trigger):
    """
    Set the repository to use for looking up standalone issue/PR references.
    """
    allowed = bot.channels[trigger.sender].privileges.get(trigger.nick, 0) >= OP
    if not allowed and not trigger.admin:
        return bot.say('You must be a channel operator to use this command!')

    if not trigger.group(2):
        return bot.say(bot.db.get_channel_value('github_issue_repo', trigger.sender))

    bot.db.set_channel_value('github_issue_repo', trigger.sender, trigger.group(3))
    bot.reply('Set linked repo for %s to %s.' % (trigger.sender, trigger.group(3)))


@url(commitURL)
def commit_info(bot, trigger, match=None):
    match = match or trigger
    URL = 'https://api.github.com/repos/%s/commits/%s' % (match.group(1), match.group(2))

    try:
        raw = fetch_api_endpoint(bot, URL)
    except HTTPError:
        bot.say('[GitHub] API returned an error.')
        return NOLIMIT
    data = json.loads(raw)
    try:
        lines = data['commit']['message'].splitlines()
        if len(lines) > 1:
            body = lines[0] + '…'
        elif len(lines) > 0:
            body = lines[0]
        else:
            body = ''
    except (KeyError):
        bot.say('[GitHub] API says this is an invalid commit. Please report this if you know it\'s a correct link!')
        return NOLIMIT

    if body.strip() == '':
        body = 'No commit message provided.'

    file_count = len(data['files'])
    response = [
        bold('[GitHub]'),
        ' [',
        match.group(1),
        '] ',
        data['author']['login'] if data['author'] else data['commit']['author']['name'],
        ': ',
        body,
        bold(' | '),
        str(data['stats']['total']),
        ' changes in ',
        str(file_count),
        ' file' if file_count == 1 else ' files'
    ]
    bot.say(''.join(response))


@url(contentURL)
def file_info(bot, trigger, match=None):
    match = match or trigger
    repo = match.group(1)
    path = match.group(3)
    ref = match.group(2)
    start_line = match.group(4)
    end_line = match.group(5)
    URL = 'https://api.github.com/repos/%s/contents/%s?ref=%s' % (repo, path, ref)

    try:
        raw = fetch_api_endpoint(bot, URL)
    except HTTPError:
        bot.say('[GitHub] API returned an error.')
        return NOLIMIT
    data = json.loads(raw)

    if data.get('type', 'dir') != 'file':
        # silently ignore directory contents (and malformed responses) for now
        return NOLIMIT

    response = [
        bold('[GitHub]'),
        ' [',
        repo,
        '] ',
        data['path'],
        ' @ ',
        ref,
    ]

    if start_line:
        lines = base64.b64decode(data['content']).splitlines()

        try:
            snippet = lines[int(start_line) - 1].decode('utf-8')
        except (IndexError, UnicodeDecodeError):
            # Line doesn't exist, or not a text file
            snippet = None

        if snippet:
            response.extend([
                ' | L',
                start_line,
                ': ',
                monospace(snippet),
                ' […] (to L%s)' % end_line if end_line else '',
            ])

    bot.say(''.join(response))


def get_data(bot, trigger, URL):
    URL = URL.split('#')[0]
    try:
        raw = fetch_api_endpoint(bot, URL)
        rawLang = fetch_api_endpoint(bot, URL + '/languages')
    except HTTPError:
        bot.say('[GitHub] API returned an error.')
        return NOLIMIT
    data = json.loads(raw)
    langData = list(json.loads(rawLang).items())
    langData = sorted(langData, key=operator.itemgetter(1), reverse=True)

    if 'message' in data:
        return bot.say('[GitHub] %s' % data['message'])

    langColors = deque(['12', '08', '09', '13'])

    max = sum([pair[1] for pair in langData])

    data['language'] = ''
    for (key, val) in langData[:3]:
        data['language'] = data['language'] + color(str("{0:.1f}".format(float(val) / max * 100)) + '% ' + key, langColors[0]) + ' '
        langColors.rotate()

    if len(langData) > 3:
        remainder = sum([pair[1] for pair in langData[3:]])
        data['language'] = data['language'] + color(str("{0:.1f}".format(float(remainder) / max * 100)) + '% Other', langColors[0]) + ' '

    timezone = get_timezone(bot.db, bot.config, None, trigger.nick)
    if not timezone:
        timezone = 'UTC'
    data['pushed_at'] = format_time(bot.db, bot.config, timezone, trigger.nick, trigger.sender, from_utc(data['pushed_at']))

    return data


@url(repoURL)
def repo_info(bot, trigger, match=None):
    user, repo = [s.strip() for s in match.group(1).split('/', 1)]
    URL = 'https://api.github.com/repos/%s/%s' % (user, repo)
    fmt_response(bot, trigger, URL, True)


@commands('github', 'gh')
@example('.gh sopel-irc/sopel-github')
def github_repo(bot, trigger):
    repo = trigger.group(3) or None

    if repo is None:
        return bot.reply('I need a repository name, or `user/reponame`.')

    if repo.lower() == 'version':
        return bot.say('[GitHub] Version {} by {}, report issues at {}'.format(
            github.__version__, github.__author__, github.__repo__))

    if repo.lower() == 'status':
        current = json.loads(requests.get('https://status.github.com/api/status.json').text)
        lastcomm = json.loads(requests.get('https://status.github.com/api/last-message.json').text)

        status = current['status']
        if status == 'major':
            status = "\x02\x034Broken\x03\x02"
        elif status == 'minor':
            status = "\x02\x037Shakey\x03\x02"
        elif status == 'good':
            status = "\x02\x033Online\x03\x02"

        lstatus = lastcomm['status']
        if lstatus == 'major':
            lstatus = "\x02\x034Broken\x03\x02"
        elif lstatus == 'minor':
            lstatus = "\x02\x037Shakey\x03\x02"
        elif lstatus == 'good':
            lstatus = "\x02\x033Online\x03\x02"

        timezone = get_timezone(bot.db, bot.config, None, trigger.nick)
        if not timezone:
            timezone = 'UTC'
        lastcomm['created_on'] = format_time(bot.db, bot.config, timezone, trigger.nick, trigger.sender, from_utc(lastcomm['created_on']))

        return bot.say('[GitHub] Current Status: ' + status + ' | Last Message: ' + lstatus + ': ' + lastcomm['body'] + ' (' + lastcomm['created_on'] + ')')
    elif repo.lower() == 'rate-limit':
        return bot.say(fetch_api_endpoint(bot, 'https://api.github.com/rate_limit'))

    if '/' not in repo:
        repo = trigger.nick.strip() + '/' + repo
    URL = 'https://api.github.com/repos/%s' % (repo.strip())

    fmt_response(bot, trigger, URL)


def from_utc(utcTime, fmt="%Y-%m-%dT%H:%M:%SZ"):
    """
    Convert UTC time string to time.struct_time
    """
    return datetime.datetime.strptime(utcTime, fmt)


def fmt_response(bot, trigger, URL, from_regex=False):
    data = get_data(bot, trigger, URL)

    if not data:
        return

    response = [
        bold('[GitHub]'),
        ' ',
        str(data['full_name'])
    ]

    if data['description'] != None:
        response.append(' - ' + str(emojize(data['description'])))

    if not data['language'].strip() == '':
        response.extend([' | ', data['language'].strip()])

    response.extend([
        ' | Last Push: ',
        str(data['pushed_at']),
        ' | Stargazers: ',
        str(data['stargazers_count']),
        ' | Watchers: ',
        str(data['subscribers_count']),
        ' | Forks: ',
        str(data['forks_count']),
        ' | Network: ',
        str(data['network_count']),
        ' | Open Issues: ',
        str(data['open_issues'])
    ])

    if not from_regex:
        response.extend([' | ', data['html_url']])

    bot.say(''.join(response))


@commands('gh-hook')
@require_chanmsg('[GitHub] GitHub hooks can only be configured in a channel')
@example('.gh-hook maxpowa/Inumuta enable')
def configure_repo_messages(bot, trigger):
    '''
    .gh-hook <repo> [enable|disable] - Enable/disable displaying webhooks from repo in current channel (You must be a channel OP)
    Repo notation is just <user/org>/<repo>, not the whole URL.
    '''
    allowed = bot.channels[trigger.sender].privileges.get(trigger.nick, 0) >= OP
    if not allowed and not trigger.admin:
        return bot.say('You must be a channel operator to use this command!')

    if not trigger.group(2):
        return bot.say(configure_repo_messages.__doc__.strip())

    channel = trigger.sender.lower()
    repo_name = trigger.group(3).lower()

    if not '/' in repo_name or 'http://' in repo_name or 'https://' in repo_name:
        return bot.say('Invalid repo formatting, see "{}help gh-hook" for an example'.format(bot.config.core.help_prefix))

    enabled = True if not trigger.group(4) or trigger.group(4).lower() == 'enable' else False

    auth_data = {
        'client_id': bot.config.github.client_id,
        'scope': 'write:repo_hook',
        'state': '{}:{}'.format(repo_name, channel)}
    auth_url = 'https://github.com/login/oauth/authorize?{}'.format(urlencode(auth_data))

    conn = bot.db.connect()
    c = conn.cursor()

    c.execute('SELECT * FROM gh_hooks WHERE channel = ? AND repo_name = ?', (channel, repo_name))
    result = c.fetchone()
    if not result:
        c.execute('''INSERT INTO gh_hooks (channel, repo_name, enabled) VALUES (?, ?, ?)''', (channel, repo_name, enabled))
        bot.say("Successfully enabled listening for {repo}'s events in {chan}.".format(chan=channel, repo=repo_name))
        bot.say('Great! Please allow me to create my webhook by authorizing via this link: ' + shorten_url(auth_url))
        bot.say('Once that webhook is successfully created, I\'ll post a message in here. Give me about a minute or so to set it up after you authorize. You can configure the colors that I use to display webhooks with {}gh-hook-color'.format(bot.config.core.help_prefix))
    else:
        c.execute('''UPDATE gh_hooks SET enabled = ? WHERE channel = ? AND repo_name = ?''', (enabled, channel, repo_name))
        bot.say("Successfully {state} the subscription to {repo}'s events".format(state='enabled' if enabled else 'disabled', repo=repo_name))
        if enabled:
            bot.say('Great! Please allow me to create my webhook by authorizing via this link: ' + shorten_url(auth_url))
            bot.say('Once that webhook is successfully created, I\'ll post a message in here. Give me about a minute or so to set it up after you authorize. You can configure the colors that I use to display webhooks with {}gh-hook-color'.format(bot.config.core.help_prefix))
    conn.commit()
    conn.close()


@commands('gh-hook-color')
@require_chanmsg('[GitHub] GitHub hooks can only be configured in a channel')
@example('.gh-hook-color maxpowa/Inumuta 13 15 6 6 14 2')
def configure_repo_colors(bot, trigger):
    '''
    .gh-hook-color <repo> <repo color> <name color> <branch color> <tag color> <hash color> <url color> - Set custom colors for the webhook messages (Uses mIRC color indicies)
    '''
    allowed = bot.channels[trigger.sender].privileges.get(trigger.nick, 0) >= OP
    if not allowed and not trigger.admin:
        return bot.say('You must be a channel operator to use this command!')

    if not trigger.group(2):
        return bot.say(configure_repo_colors.__doc__.strip())

    channel = trigger.sender.lower()
    repo_name = trigger.group(3).lower()
    colors = []
    try:
        colors = [int(c) % 16 for c in trigger.group(2).replace(trigger.group(3), '', 1).split()]
    except:
        return bot.say('You must provide exactly 6 colors that are integers and are space separated. See "{}help gh-hook-color" for more information.'.format(bot.config.core.help_prefix))

    if len(colors) != 6:
        return bot.say('You must provide exactly 6 colors! See "{}help gh-hook-color" for more information.'.format(bot.config.core.help_prefix))

    conn = bot.db.connect()
    c = conn.cursor()

    c.execute('SELECT * FROM gh_hooks WHERE channel = ? AND repo_name = ?', (channel, repo_name))
    result = c.fetchone()
    if not result:
        return bot.say('Please use "{}gh-hook {} enable" before attempting to configure colors!'.format(bot.config.core.help_prefix, repo_name))
    else:
        combined = colors
        combined.append(channel)
        combined.append(repo_name)
        c.execute('''UPDATE gh_hooks SET repo_color = ?, name_color = ?, branch_color = ?, tag_color = ?,
                     hash_color = ?, url_color = ? WHERE channel = ? AND repo_name = ?''', combined)
        conn.commit()
        c.execute('SELECT * FROM gh_hooks WHERE channel = ? AND repo_name = ?', (channel, repo_name))
        row = c.fetchone()
        bot.say("[{}] Example name: {} tag: {} commit: {} branch: {} url: {}".format(
                formatting.fmt_repo(repo_name, row),
                formatting.fmt_name(trigger.nick, row),
                formatting.fmt_tag('tag', row),
                formatting.fmt_hash('c0mm17', row),
                formatting.fmt_branch('master', row),
                formatting.fmt_url('http://git.io/', row)))
