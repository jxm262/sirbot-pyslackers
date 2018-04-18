import re
import json
import pprint
import logging
import datetime

from slack import methods
from slack.events import Message
from slack.exceptions import SlackAPIError
from asyncpg.exceptions import UniqueViolationError

from .utils import ADMIN_CHANNEL

LOG = logging.getLogger(__name__)
TELL_REGEX = re.compile('tell (<(#|@)(?P<to_id>[A-Z0-9]*)(|.*)?>) (?P<msg>.*)')


def create_endpoints(plugin):
    plugin.on_message('hello', hello, flags=re.IGNORECASE, mention=True)
    plugin.on_message('^tell', tell, flags=re.IGNORECASE, mention=True, admin=True)
    plugin.on_message('.*', mention, flags=re.IGNORECASE, mention=True)
    plugin.on_message('.*', save_in_database, wait=False)
    plugin.on_message('.*', channel_topic, subtype='channel_topic')
    plugin.on_message('g#', github_repo_link)
    plugin.on_message('^inspect', inspect, flags=re.IGNORECASE, mention=True, admin=True)
    plugin.on_message('^help', help_message, flags=re.IGNORECASE)


async def hello(message, app):
    response = message.response()
    response['text'] = 'Hello <@{user}>'.format(user=message['user'])
    await app['plugins']['slack'].api.query(url=methods.CHAT_POST_MESSAGE, data=response)


async def help_message(message, app):
    response = message.response()
    response['text'] = 'Sir Bot-a-lot help'
    response['attachments'] = [
        {
            'color': 'good',
            'fields': [
                {
                    'title': '@sir_botalot hello',
                    'value': f'Say hello to sir_botalot.',
                    'short': True
                },
                {
                    'title': '/report',
                    'value': 'Report an offending user to the admin team.',
                    'short': True
                },
                {
                    'title': '/gif search terms',
                    'value': 'Search for a gif on giphy.com .',
                    'short': True
                },
                {
                    'title': '/pypi search terms',
                    'value': 'Search for packages on pypi.org .',
                    'short': True
                },
                {
                    'title': '/sponsors',
                    'value': 'Referal links from our sponsors.',
                    'short': True
                },
                {
                    'title': '/snippet',
                    'value': 'Instruction on creating a slack code snippet.',
                    'short': True
                },
                {
                    'title': 'g#user/repo',
                    'value': 'Share the link to that github repo. User default to `pyslackers`.',
                }
            ]
        }
    ]

    await app['plugins']['slack'].api.query(url=methods.CHAT_POST_MESSAGE, data=response)


async def tell(message, app):
    match = TELL_REGEX.match(message['text'])
    response = message.response()

    if match:
        to_id = match.group('to_id')
        msg = match.group('msg')

        if to_id.startswith(('C', 'U')):
            response['text'] = msg
            response['channel'] = to_id
        else:
            response['text'] = 'Sorry I can not understand the destination.'
    else:
        response['text'] = 'Sorry I can not understand'

    await app['plugins']['slack'].api.query(url=methods.CHAT_POST_MESSAGE, data=response)


async def mention(message, app):

    try:
        if message['user'] != app['plugins']['slack'].bot_user_id:
            await app['plugins']['slack'].api.query(url=methods.REACTIONS_ADD, data={
                'name': 'sirbot',
                'channel': message['channel'],
                'timestamp': message['ts']
            })
    except SlackAPIError as e:
        if e.error != 'already_reacted':
            raise


async def save_in_database(message, app):
    if 'pg' in app['plugins']:
        LOG.debug('Saving message "%s" to database.', message['ts'])

        if message['ts']:  # We sometimes receive message without a timestamp. See #45
            try:
                async with app['plugins']['pg'].connection() as pg_con:
                    await pg_con.execute(
                        '''INSERT INTO slack.messages (id, text, "user", channel, raw, time)
                        VALUES ($1, $2, $3, $4, $5, $6)''',
                        message['ts'], message.get('text'), message.get('user'), message.get('channel'), dict(message),
                        datetime.datetime.fromtimestamp(int(message['ts'].split('.')[0]))
                    )
            except UniqueViolationError:
                LOG.debug('Message "%s" already in database.', message['ts'])


async def channel_topic(message, app):

    if message['user'] not in app['plugins']['slack'].admins and message['user'] != app['plugins']['slack'].bot_user_id:

        async with app['plugins']['pg'].connection() as pg_con:
            channel = await pg_con.fetchrow('''SELECT raw FROM slack.channels WHERE id = $1''', message['channel'])
            LOG.debug(channel)
            if channel:
                old_topic = channel['raw']['topic']['value']
            else:
                old_topic = 'Original topic not found'

        response = Message()
        response['channel'] = ADMIN_CHANNEL
        response['attachments'] = [
            {
                'fallback': 'Channel topic changed notice: old topic',
                'title': f'<@{message["user"]}> changed <#{message["channel"]}> topic.',
                'fields': [
                    {
                        'title': 'Previous topic',
                        'value': old_topic
                    },
                    {
                        'title': 'New topic',
                        'value': message['topic']
                    }
                ],
            },
        ]

        if channel:
            response['attachments'][0]['callback_id'] = 'topic_change'
            response['attachments'][0]['actions'] = [
                {
                    'name': 'validate',
                    'text': 'Validate',
                    'style': 'primary',
                    'type': 'button',
                },
                {
                    'name': 'revert',
                    'text': 'Revert',
                    'style': 'danger',
                    'value': json.dumps({'channel': message['channel'], 'old_topic': old_topic}),
                    'type': 'button',
                }
            ]

        await app['plugins']['slack'].api.query(url=methods.CHAT_POST_MESSAGE, data=response)


async def github_repo_link(message, app):
    if 'text' in message and message['text']:
        response = message.response()

        start = message['text'].find('g#')
        repo = message['text'][start + 2:].split()[0]

        if '/' not in repo:
            repo = 'pyslackers/' + repo

        url = f'https://github.com/{repo}'
        r = await app['http_session'].request('GET', url)

        if r.status == 200:
            response['text'] = url
            await app['plugins']['slack'].api.query(url=methods.CHAT_POST_MESSAGE, data=response)


async def inspect(message, app):
    if message['channel'] == ADMIN_CHANNEL and 'text' in message and message['text']:
        response = message.response()
        match = re.search('<@(.*)>', message['text'])

        if match:
            user_id = match.group(1)

            async with app['plugins']['pg'].connection() as pg_con:
                data = await pg_con.fetchrow('''SELECT raw, join_date FROM slack.users WHERE id = $1''', user_id)

            if data:
                user = data['raw']
                user['join_date'] = data['join_date'].isoformat()
            else:
                data = await app['plugins']['slack'].api.query(url=methods.USERS_INFO, data={'user': user_id})
                user = data['user']

            response['text'] = f"<@{user_id}> profile information \n```{pprint.pformat(user)}```"
        else:
            response['text'] = f"Sorry I couldn't figure out which user to inspect"

        await app['plugins']['slack'].api.query(url=methods.CHAT_POST_MESSAGE, data=response)
