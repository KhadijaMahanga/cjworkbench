from collections import namedtuple
from enum import Enum
import re
from typing import Any, Dict, List, Optional, Tuple
import aiohttp
from aiohttp.client_exceptions import ClientResponseError
from django.conf import settings
import numpy as np
from oauthlib import oauth1
from oauthlib.common import urlencode
import pandas as pd
import yarl  # expose aiohttp's innards -- ick.
from server import oauth


class QueryType(Enum):
    # Named after Twitter API endpoints
    USER_TIMELINE = 'user_timeline'
    """
    List a user's tweets.

    https://developer.twitter.com/en/docs/tweets/timelines/api-reference/get-statuses-user_timeline.html
    """

    SEARCH = 'search'
    """
    Search all tweets.

    https://developer.twitter.com/en/docs/tweets/search/overview/standard
    """

    LISTS_STATUSES = 'lists_statuses'
    """
    List the tweets of all members of a list.

    https://developer.twitter.com/en/docs/accounts-and-users/create-manage-lists/api-reference/get-lists-statuses
    """

    @property
    def query_param_name(self):
        """
        Name of the single param we use as argument for this query.

        For instance: 'search' has param_name 'query'
        """
        return {
            self.USER_TIMELINE: 'username',
            self.SEARCH: 'query',
            self.LISTS_STATUSES: 'listurl',
        }[self]


def _migrate_params_v0_to_v1(params):
    """
    v0: 'querytype' is 0, 1 or 2

    v1: 'querytype' is 'user_timeline', 'search', 'lists_statuses'
    """
    params = dict(params)  # copy
    params['querytype'] = [
        'user_timeline',
        'search',
        'lists_statuses',
    ][params['querytype']]

    return params


def migrate_params(params):
    if isinstance(params['querytype'], int):
        params = _migrate_params_v0_to_v1(params)

    return params


Column = namedtuple('Column', ['name', 'path', 'dtype', 'parse'])


HTML_TAG_RE = re.compile('<[^>]*>')


def parse_source(source: str) -> str:
    """Parse a Twitter Status 'source', to remove HTML tag."""
    return HTML_TAG_RE.sub('', source)


def parse_lang(lang: str) -> Optional[str]:
    """Parse a Twitter Status 'lang', to set 'und' to None."""
    return None if lang == 'und' else lang


def parse_tweet_text(tweet_json: Dict[str, Any]) -> str:
    if 'retweeted_status' in tweet_json:
        status = tweet_json['retweeted_status']
        screen_name = status['user']['screen_name']
        try:
            text = status['full_text']
        except KeyError:
            # Some retweeted statuses don't have full_text.
            text = status['text']
        return 'RT @%s: %s' % (screen_name, text)
    else:
        return tweet_json['full_text']


Columns = [
    Column('screen_name', ['user', 'screen_name'], np.object, None),
    Column('created_at', ['created_at'], 'datetime64[ns]', None),
    Column('text', [], np.object, parse_tweet_text),
    Column('retweet_count', ['retweet_count'], np.int64, None),
    Column('favorite_count', ['favorite_count'], np.int64, None),
    Column('in_reply_to_screen_name', ['in_reply_to_screen_name'], np.object,
           None),
    Column('retweeted_status_screen_name',
           ['retweeted_status', 'user', 'screen_name'], np.object, None),
    Column('user_description', ['user', 'description'], np.object, None),
    Column('source', ['source'], np.object, parse_source),
    Column('lang', ['lang'], np.object, parse_lang),
    Column('id', ['id'], np.int64, None),
]


def create_empty_table():
    data = {}
    for column in Columns:
        data[column.name] = pd.Series([], dtype=column.dtype)
    return pd.DataFrame(data)


def read_raw_value(status: Dict[str, Any], column: Column) -> Any:
    parts = column.path
    raw = status

    try:
        for part in parts:
            raw = raw[part]
    except KeyError:
        raw = None

    return raw


def read_column(statuses: List[Dict[str, Any]], column: Column) -> pd.Series:
    values = [read_raw_value(status, column) for status in statuses]
    series = pd.Series(values, name=column.name)

    if column.parse:
        series = series.map(column.parse)

    series = series.astype(column.dtype)
    return series


def _recover_from_160258591(table):
    """Reset types of columns, in-place."""
    # https://www.pivotaltracker.com/story/show/160258591
    for column in Columns:
        try:
            table[column.name] = table[column.name].astype(column.dtype)
        except KeyError:
            table[column.name] = None
            table[column.name] = table[column.name].astype(column.dtype)


# Get dataframe of last tweets fron our storage,
async def get_stored_tweets(get_stored_dataframe):
    table = await get_stored_dataframe()
    if table is None or table.empty:
        table = create_empty_table()
    else:
        _recover_from_160258591(table)
    # [2019-05-28] We used to support 1M rows; now we only support 100k.
    # Truncate as early as possible, to release RAM as early as possible.
    table = table.truncate(after=settings.TWITTER_MAX_ROWS_PER_TABLE - 1)
    return table


def statuses_to_dataframe(statuses: List[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(dict(
        [(column.name, read_column(statuses, column)) for column in Columns]
    ))


async def fetch_from_twitter(access_token, path, params: List[Tuple[str, str]],
                             since_id: Optional[int], per_page: int,
                             n_pages: int) -> List[Dict[str, Any]]:
    service = oauth.OAuthService.lookup_or_none('twitter_credentials')
    if not service:
        raise Exception('Twitter connection misconfigured')

    oauth_client = oauth1.Client(
        client_key=service.consumer_key,
        client_secret=service.consumer_secret,
        resource_owner_key=access_token['oauth_token'],
        resource_owner_secret=access_token['oauth_token_secret']
    )

    page_dataframes = [create_empty_table()]

    max_id = None
    async with aiohttp.ClientSession() as session:  # aiohttp timeout of 5min
        for page in range(n_pages):
            # Assume {path} contains '?' already
            page_params = [
                *params,
                ('tweet_mode', 'extended'),
                ('count', str(per_page)),
            ]
            if since_id:
                page_params.append(('since_id', str(since_id)))
            if max_id:
                page_params.append(('max_id', str(max_id)))

            page_url = (
                f'https://api.twitter.com/1.1/{path}?{urlencode(page_params)}'
            )

            page_url, headers, body = oauth_client.sign(
                page_url,
                headers={'Accept': 'application/json'}
            )

            # aiohttp internally performs URL canonization before sending
            # request. DISABLE THIS: it rewrites the signed URL!
            #
            # https://github.com/aio-libs/aiohttp/issues/3424
            page_url = yarl.URL(page_url, encoded=True)  # disable magic

            response = await session.get(page_url, headers=headers)
            response.raise_for_status()
            page_statuses = await response.json()

            if isinstance(page_statuses, dict) and 'statuses' in page_statuses:
                # /search wraps result in {}
                page_statuses = page_statuses['statuses']

            if not page_statuses:
                break

            # Parse one page at a time, instead of parsing all at the end.
            # Should save a bit of memory and make a smaller CPU-blip in our
            # event loop.
            page_dataframes.append(statuses_to_dataframe(page_statuses))
            max_id = page_statuses[-1]['id'] - 1

    return pd.concat(page_dataframes, ignore_index=True, sort=False)


async def twitter_user_timeline(access_token, screen_name,
                                since_id: Optional[int]
                                ) -> List[Dict[str, Any]]:
    # 3200 tweets, aribitrarily
    return await fetch_from_twitter(access_token,
                                    'statuses/user_timeline.json',
                                    [('screen_name', screen_name)], since_id,
                                    200, 16)


async def twitter_search(access_token, q,
                         since_id: Optional[int]) -> List[Dict[str, Any]]:
    # 1000 tweets, aribitrarily, to try to go easy on rate limits
    # (this is still 10 calls)
    return await fetch_from_twitter(access_token, 'search/tweets.json',
                                    [('q', q), ('result_type', 'recent')],
                                    since_id, 100, 10)


async def twitter_list_timeline(access_token, owner_screen_name, slug,
                                since_id: Optional[int]
                                ) -> List[Dict[str, Any]]:
    # 2000 tweets, aribitrarily, to try to go easy on rate limits
    # (this is still 10 calls)
    return await fetch_from_twitter(
        access_token,
        'lists/statuses.json',
        [
            ('owner_screen_name', owner_screen_name),
            ('slug', slug),
        ],
        since_id,
        200,
        5
    )


# Inspired by https://github.com/twitter/twitter-text
USERNAME_REGEX_PART = r'@?([a-zA-Z0-9_]{1,15})'
LIST_REGEX_PART = r'([a-z][-_a-z0-9]{0,24})'

USERNAME_REGEX = re.compile(f'^{USERNAME_REGEX_PART}$')
LIST_URL_REGEX = re.compile(
    f'^(?:https?://)twitter.com/{USERNAME_REGEX_PART}'
    f'/lists/{LIST_REGEX_PART}$'
)
LIST_REGEX = re.compile(f'^{USERNAME_REGEX_PART}/{LIST_REGEX_PART}$')


# Get from Twitter, return as dataframe
async def get_new_tweets(access_token, querytype, query, old_tweets):
    if old_tweets is not None and not old_tweets.empty:
        last_id = old_tweets['id'].max()
    else:
        last_id = None

    if querytype == QueryType.USER_TIMELINE:
        match = USERNAME_REGEX.match(query)
        if not match:
            raise ValueError('Not a valid Twitter username')
        username = match.group(1)

        # 16 pages of 200 each is Twitter's current maximum archived
        return await twitter_user_timeline(access_token, username, last_id)

    elif querytype == QueryType.SEARCH:
        return await twitter_search(access_token, query, last_id)

    else:  # querytype == QueryType.LISTS_STATUSES
        match = LIST_URL_REGEX.match(query)
        if not match:
            match = LIST_REGEX.match(query)
        if not match:
            raise ValueError('Not a valid Twitter list URL')

        return await twitter_list_timeline(access_token, match.group(1),
                                           match.group(2), last_id)


# Combine this set of tweets with previous set of tweets
def merge_tweets(old_table, new_table):
    if old_table is None or old_table.empty:
        return new_table
    elif new_table is None or new_table.empty:
        return old_table
    else:
        # The new tweets all go before the old tweets.
        #
        # We tend to add columns to our output in successive versions of the
        # Twitter module. `new_table` has the correct list of columns;
        # `old_table` may be incomplete. `new_table` has the correct list of
        # columns; `old_table` may be incomplete.
        #
        # sort=False: use the ordering in new_table. (pandas 0.23 corrects the
        # previous unintuitive behavior in DataFrame.append().)
        free = settings.TWITTER_MAX_ROWS_PER_TABLE - len(new_table)
        old_table = old_table.iloc[:free]
        return new_table.append(old_table, ignore_index=True, sort=False)


# Render just returns previously retrieved tweets
def render(table, params, *, fetch_result):
    if fetch_result is None:
        return table

    if fetch_result.status == 'error':
        return fetch_result

    if fetch_result.dataframe.empty:
        # Previously, we saved empty tables improperly
        dataframe = create_empty_table()
    else:
        _recover_from_160258591(fetch_result.dataframe)
        dataframe = fetch_result.dataframe

    # [2019-05-29] Even after fetch, let's save RAM. (This probably won't
    # aftect much because render result are cached.)
    dataframe = dataframe.truncate(after=settings.TWITTER_MAX_ROWS_PER_TABLE - 1)

    return {
        'dataframe': dataframe,
        'column_formats': {'id': '{:d}'},  # don't add commas to user IDs
    }


async def fetch(params, *, get_stored_dataframe):
    querytype = QueryType(params['querytype'])
    query: str = params[querytype.query_param_name]
    access_token = (params['twitter_credentials'] or {}).get('secret')

    if not query.strip() and not access_token:
        return None  # Don't create a version

    if not query.strip():
        return 'Please enter a query'

    if not access_token:
        return 'Please sign in to Twitter'

    try:
        if params['accumulate']:
            old_tweets = await get_stored_tweets(get_stored_dataframe)
            tweets = await get_new_tweets(access_token, querytype, query,
                                          old_tweets)
            tweets = merge_tweets(old_tweets, tweets)
        else:
            tweets = await get_new_tweets(access_token, querytype,
                                          query, None)
        return tweets

    except ValueError as err:
        return str(err)

    except ClientResponseError as err:
        if err.status:
            if querytype == QueryType.USER_TIMELINE and err.status == 401:
                return "User %s's tweets are private" % query
            elif querytype == QueryType.USER_TIMELINE and err.status == 404:
                return 'User %s does not exist' % query
            elif err.status == 429:
                return (
                    'Twitter API rate limit exceeded. '
                    'Please wait a few minutes and try again.'
                )
            else:
                return 'Error from Twitter: %d %s' % (err.status, err.message)
        else:
            return 'Error fetching tweets: %s' % str(err)
