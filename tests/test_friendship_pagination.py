"""Offline regression tests for GraphQL friendship pagination.

Run with: python -m unittest discover -s tests -v
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from twikit.client.client import Client


def page(entries):
    return {'data': {'user': {'result': {'timeline': {'instructions': [
        {'entries': entries}
    ]}}}}}


def bottom(value='next-page'):
    return {'entryId': 'cursor-bottom-0', 'content': {'value': value}}


def user(result=None):
    return {'entryId': 'user-123', 'content': {'result': (
        {'__typename': 'User', 'rest_id': '123'} if result is None else result
    )}}


class FriendshipPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, response, method='get_user_following', cursor=None):
        endpoint = AsyncMock(return_value=(response, None))
        client = object.__new__(Client)
        client.gql = SimpleNamespace(**{name: endpoint for name in (
            'following', 'followers', 'blue_verified_followers',
            'followers_you_know', 'user_creator_subscriptions'
        )})
        result = await getattr(client, method)('target-user', count=20, cursor=cursor)
        endpoint.assert_awaited_once_with('target-user', 20, cursor)
        return result, endpoint

    async def assert_terminal(self, response, expected_count=0, method='get_user_following'):
        with patch('twikit.client.client.User', side_effect=lambda client, data: data):
            result, endpoint = await self.fetch(response, method, cursor='last-page')
        self.assertEqual(len(result), expected_count)
        self.assertIsNone(result.next_cursor)
        for _ in range(2):
            following = await result.next()
            self.assertEqual(list(following), [])
            self.assertIsNone(following.next_cursor)
        endpoint.assert_awaited_once()
        return result

    async def test_empty_entries(self):
        await self.assert_terminal(page([]))

    async def test_missing_entries(self):
        await self.assert_terminal({'data': {}})

    async def test_users_without_bottom_cursor(self):
        result = await self.assert_terminal(page([user()]), 1)
        self.assertEqual(result[0]['rest_id'], '123')

    async def test_top_cursor_and_unrelated_entries(self):
        await self.assert_terminal(page([
            {'entryId': 'cursor-top-0', 'content': {'value': 'previous'}},
            {'entryId': 'other-entry'}
        ]))

    async def test_filtered_and_unavailable_users(self):
        with self.assertWarnsRegex(UserWarning, 'Quality Filter'):
            await self.assert_terminal(page([
                {'entryId': 'user-filtered', 'content': {}},
                user({'__typename': 'UserUnavailable'})
            ]))

    async def test_empty_bottom_cursor_is_terminal(self):
        for value in ('', None):
            with self.subTest(value=value):
                result, endpoint = await self.fetch(page([bottom(value)]))
                self.assertEqual(len(await result.next()), 0)
                endpoint.assert_awaited_once()

    async def test_next_page_preserves_arguments_and_terminates(self):
        for entries in ([user(), bottom()], [bottom(), user()], [bottom()]):
            with self.subTest(entries=entries):
                with patch('twikit.client.client.User', side_effect=lambda client, data: data):
                    result, endpoint = await self.fetch(page(entries))
                self.assertEqual(len(result), len(entries) - 1)
                self.assertEqual(result.next_cursor, 'next-page')
                endpoint.return_value = (page([]), None)
                next_result = await result.next()
                endpoint.assert_awaited_with('target-user', 20, 'next-page')
                self.assertEqual(list(next_result), [])
                self.assertIsNone(next_result.next_cursor)
                await next_result.next()
                self.assertEqual(endpoint.await_count, 2)

    async def test_all_shared_friendship_endpoints(self):
        for method in ('get_user_following', 'get_user_followers',
                       'get_user_verified_followers', 'get_user_followers_you_know',
                       'get_user_subscriptions'):
            with self.subTest(method=method):
                await self.assert_terminal(page([]), method=method)
