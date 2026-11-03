"""Offline friendship pagination contracts. No account or HTTP requests.

Run with: python -m unittest discover -s tests -v
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from twikit import Client, InvalidResponse, User
from twikit.errors import TooManyRequests, Unauthorized

METHODS = ('get_user_following', 'get_user_followers', 'get_user_verified_followers',
           'get_user_followers_you_know', 'get_user_subscriptions')


def timeline(instructions, nested=False):
    value = {'instructions': instructions}
    if nested:
        value = {'timeline': value}
    return {'data': {'user': {'result': {'__typename': 'User', 'timeline': value}}}}


def page(entries, nested=False):
    return timeline([{'type': 'TimelineAddEntries', 'entries': entries}], nested)


def bottom(value='next-page'):
    return {'entryId': 'cursor-bottom-0', 'content': {'value': value}}


def user(result=None):
    if result is None:
        result = {'__typename': 'User', 'rest_id': '9999999999999999999', 'legacy': {
            'created_at': 'Wed Jan 01 00:00:00 +0000 2025', 'name': 'Fixture',
            'screen_name': 'renamed', 'profile_image_url_https': 'https://example.invalid/avatar'
        }}
    return {'entryId': 'user-123', 'content': {'itemContent': {
        'user_results': {'result': result}
    }}}


class FriendshipPaginationTests(unittest.IsolatedAsyncioTestCase):
    def client(self, response):
        endpoint = AsyncMock(return_value=(response, None))
        client = object.__new__(Client)
        client.gql = SimpleNamespace(**{name: endpoint for name in (
            'following', 'followers', 'blue_verified_followers',
            'followers_you_know', 'user_creator_subscriptions'
        )})
        return client, endpoint

    async def fetch(self, response, method='get_user_following', cursor=None, count=20):
        client, endpoint = self.client(response)
        result = await getattr(client, method)('target-user', count=count, cursor=cursor)
        endpoint.assert_awaited_once_with('target-user', count, cursor)
        return result, endpoint

    async def assert_terminal(self, response, expected_count=0, method='get_user_following'):
        result, endpoint = await self.fetch(response, method, cursor='last-page')
        self.assertEqual(len(result), expected_count)
        self.assertIsNone(result.next_cursor)
        for _ in range(2):
            following = await result.next()
            self.assertEqual(list(following), [])
            self.assertIsNone(following.next_cursor)
        endpoint.assert_awaited_once()
        return result

    async def test_explicit_empty_entries_are_terminal(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                await self.assert_terminal(page([], nested))

    async def test_users_without_bottom_cursor(self):
        result = await self.assert_terminal(page([user()], nested=True), 1)
        self.assertIsInstance(result[0], User)
        self.assertEqual(result[0].id, '9999999999999999999')
        self.assertEqual(result[0].screen_name, 'renamed')

    async def test_top_cursor_does_not_create_a_next_page(self):
        await self.assert_terminal(page([
            {'entryId': 'cursor-top-0', 'content': {'value': 'previous'}}
        ]))

    async def test_bottom_termination_is_explicit(self):
        await self.assert_terminal(timeline([
            {'type': 'TimelineTerminateTimeline', 'direction': 'Bottom'}
        ]))

    async def test_next_page_preserves_arguments_and_terminates(self):
        for entries in ([user(), bottom()], [bottom(), user()], [bottom()]):
            with self.subTest(entries=entries):
                result, endpoint = await self.fetch(page(entries), count=7)
                self.assertEqual(len(result), len(entries) - 1)
                self.assertEqual(result.next_cursor, 'next-page')
                endpoint.return_value = (page([]), None)
                next_result = await result.next()
                endpoint.assert_awaited_with('target-user', 7, 'next-page')
                self.assertEqual(list(next_result), [])
                self.assertIsNone(next_result.next_cursor)
                await next_result.next()
                self.assertEqual(endpoint.await_count, 2)

    async def test_all_shared_friendship_endpoints(self):
        for method in METHODS:
            with self.subTest(method=method):
                await self.assert_terminal(page([]), method=method)
                with self.assertRaises(InvalidResponse):
                    await self.fetch({'data': {}}, method=method)

    async def test_missing_or_malformed_envelopes_are_not_terminal(self):
        responses = [None, [], {}, {'data': {}}, {'data': None},
                     {'data': {'user': None}}, {'data': {'user': {'result': {}}}},
                     {'data': {'user': {'result': {'timeline': {}}}}},
                     {'data': {'user': {'result': {'timeline': None}}}},
                     {'data': {'entries': []}}, timeline([]), timeline(None)]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(InvalidResponse):
                await self.fetch(response, cursor='checkpoint')

    async def test_unrelated_entries_cannot_complete_a_page(self):
        response = {'data': {'unrelated': {'entries': []}, 'user': {'result': {}}}}
        with self.assertRaises(InvalidResponse):
            await self.fetch(response)

    async def test_graphql_partial_errors_are_not_terminal(self):
        response = page([])
        response['errors'] = [{'message': 'private-provider-body'}]
        with self.assertRaises(InvalidResponse) as raised:
            await self.fetch(response)
        self.assertNotIn('private-provider-body', str(raised.exception))

    async def test_malformed_instructions_are_not_terminal(self):
        for instructions in ([None], [{}], [{'type': 'TimelineClearCache'}],
                             [{'type': 'UnknownInstruction', 'entries': []}],
                             [{'type': 'TimelineAddEntries'}],
                             [{'type': 'TimelineTerminateTimeline', 'direction': 'Top'}],
                             [{'type': 'TimelineReplaceEntry', 'entry': user()}]):
            with self.subTest(instructions=instructions), self.assertRaises(InvalidResponse):
                await self.fetch(timeline(instructions))

    async def test_malformed_entry_batches_are_not_terminal(self):
        for entries in (None, {}, '', [None], [{}], [{'entryId': 123}],
                        [{'entryId': 'unknown'}]):
            with self.subTest(entries=entries), self.assertRaises(InvalidResponse):
                await self.fetch(page(entries))

    async def test_filtered_or_unavailable_users_are_incomplete(self):
        for entry in ({'entryId': 'user-filtered', 'content': {}},
                      user({'__typename': 'UserUnavailable'})):
            with self.subTest(entry=entry), self.assertRaises(InvalidResponse):
                await self.fetch(page([entry]))

    async def test_malformed_users_are_not_silently_dropped(self):
        for result in (None, [], {'__typename': 'User'},
                       {'__typename': 'User', 'rest_id': '123'},
                       {'__typename': 'User', 'rest_id': 123},
                       {'__typename': 'User', 'rest_id': 'private-id'}):
            entry = user() if result is None else user(result)
            if result is None:
                entry['content']['itemContent']['user_results']['result'] = None
            with self.subTest(result=result), self.assertRaises(InvalidResponse):
                await self.fetch(page([entry]))

    async def test_malformed_or_stalled_bottom_cursors_are_rejected(self):
        for value in ('', ' ', None, 123, {}, 'checkpoint'):
            with self.subTest(value=value), self.assertRaises(InvalidResponse):
                await self.fetch(page([bottom(value)]), cursor='checkpoint')
        with self.assertRaises(InvalidResponse):
            await self.fetch(page([{'entryId': 'cursor-bottom-0'}]))

    async def test_multiple_entry_instructions_preserve_continuation(self):
        result, _ = await self.fetch(timeline([
            {'type': 'TimelineClearCache'},
            {'type': 'TimelineAddEntries', 'entries': [user()]},
            {'type': 'TimelineAddEntries', 'entries': [bottom()]}
        ]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result.next_cursor, 'next-page')

    async def test_replacement_cursor_preserves_continuation(self):
        result, _ = await self.fetch(timeline([
            {'type': 'TimelineAddEntries', 'entries': [user()]},
            {'type': 'TimelineReplaceEntry', 'entry': bottom()}
        ]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result.next_cursor, 'next-page')

    async def test_conflicting_continuation_is_rejected(self):
        for instructions in (
            [{'type': 'TimelineAddEntries', 'entries': [bottom(), bottom('other')]}],
            [{'type': 'TimelineAddEntries', 'entries': [bottom()]},
             {'type': 'TimelineTerminateTimeline', 'direction': 'Bottom'}]
        ):
            with self.subTest(instructions=instructions), self.assertRaises(InvalidResponse):
                await self.fetch(timeline(instructions))

    async def test_interrupted_next_page_raises_without_restart(self):
        result, endpoint = await self.fetch(page([bottom()]))
        endpoint.return_value = ({'data': {}}, None)
        with self.assertRaises(InvalidResponse):
            await result.next()
        endpoint.assert_awaited_with('target-user', 20, 'next-page')
        self.assertEqual(endpoint.await_count, 2)
        self.assertEqual(result.next_cursor, 'next-page')

    async def test_provider_failures_propagate_without_retry(self):
        for exc in (Unauthorized('fixture'), TooManyRequests('fixture'), TimeoutError('fixture')):
            client, endpoint = self.client(page([]))
            endpoint.side_effect = exc
            with self.subTest(error=type(exc).__name__), self.assertRaises(type(exc)):
                await client.get_user_following('target-user', cursor='checkpoint')
            endpoint.assert_awaited_once()
