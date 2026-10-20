"""
Tests for the review page: going through uploads awaiting approval one at a time.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from media.models import MediaGroup, MediaItem

User = get_user_model()


class ReviewPageTest(TestCase):
    def setUp(self):
        User.objects.create_superuser('admin', 'a@example.com', 'password')
        self.client.login(username='admin', password='password')
        self.process_media = patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

        self.zero = MediaGroup.objects.create(name='Kanał Zero', require_approval=True)
        self.history = MediaGroup.objects.create(name='Historia', require_approval=True)
        now = timezone.now()
        self.first = self._awaiting('First', self.zero, now - timedelta(hours=3))
        self.second = self._awaiting('Second', self.history, now - timedelta(hours=2))
        self.third = self._awaiting('Third', self.zero, now - timedelta(hours=1))

    def _awaiting(self, title, group, created_at):
        item = MediaItem.objects.create(
            source_url=f'https://www.youtube.com/watch?v={title}',
            requested_type='video',
            slug='pending',
            title=title,
            description='Long description\nwith https://example.com/link',
            group=group,
            status=MediaItem.STATUS_AWAITING_APPROVAL,
            thumbnail_url='https://i.ytimg.com/vi/x/hq.jpg',
        )
        MediaItem.objects.filter(pk=item.pk).update(created_at=created_at)
        return item

    def _current(self, response):
        return response.context['item']

    def _decide(self, item, action, next_url=None):
        return self.client.post(
            reverse('item_decision', args=[item.guid, action]),
            {'next': next_url or reverse('review')},
        )

    def test_shows_the_oldest_item_first(self):
        response = self.client.get(reverse('review'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._current(response), self.first)
        self.assertEqual(response.context['remaining'], 3)
        self.assertContains(response, 'https://i.ytimg.com/vi/x/hq.jpg')
        self.assertContains(response, '<a href="https://example.com/link"')

    def test_download_goes_on_to_the_next_item(self):
        response = self._decide(self.first, 'approve')

        self.assertRedirects(response, reverse('review'), fetch_redirect_response=False)
        self.process_media.assert_called_once_with(self.first.guid)
        self.assertEqual(self._current(self.client.get(reverse('review'))), self.second)

    def test_reject_goes_on_and_can_be_undone(self):
        self._decide(self.first, 'reject')

        response = self.client.get(reverse('review'))
        self.assertEqual(self._current(response), self.second)
        self.assertEqual(response.context['last_rejected'], self.first)

        self._decide(self.first, 'restore')

        self.first.refresh_from_db()
        self.assertEqual(self.first.status, MediaItem.STATUS_AWAITING_APPROVAL)
        response = self.client.get(reverse('review'))
        self.assertEqual(self._current(response), self.first)
        self.assertIsNone(response.context['last_rejected'])

    def test_restore_only_applies_to_rejected_items(self):
        self._decide(self.first, 'restore')

        self.first.refresh_from_db()
        self.assertEqual(self.first.status, MediaItem.STATUS_AWAITING_APPROVAL)

    def test_skip_and_start_over(self):
        self.client.post(reverse('review'), {'skip': self.first.guid})
        self.client.post(reverse('review'), {'skip': self.second.guid})

        response = self.client.get(reverse('review'))
        self.assertEqual(self._current(response), self.third)
        self.assertEqual(response.context['skipped_count'], 2)

        self.client.post(reverse('review'), {'skip': self.third.guid})
        response = self.client.get(reverse('review'))
        self.assertIsNone(self._current(response))
        self.assertContains(response, 'reset=1')

        self.client.get(reverse('review'), {'reset': '1'})
        self.assertEqual(self._current(self.client.get(reverse('review'))), self.first)

    def test_group_filter(self):
        url = f'{reverse("review")}?group={self.history.slug}'

        response = self.client.get(url)

        self.assertEqual(self._current(response), self.second)
        self.assertEqual(response.context['remaining'], 1)
        # Deciding keeps the filter
        redirect = self._decide(self.second, 'approve', next_url=url)
        self.assertEqual(redirect['Location'], url)

    def test_item_parameter_opens_that_item(self):
        """Push notifications link to their own item"""
        response = self.client.get(reverse('review'), {'item': self.third.guid})

        self.assertEqual(self._current(response), self.third)

    def test_item_parameter_ignores_items_no_longer_awaiting(self):
        self._decide(self.third, 'reject')

        response = self.client.get(reverse('review'), {'item': self.third.guid})

        self.assertEqual(self._current(response), self.first)

    def test_nothing_left(self):
        MediaItem.objects.update(status=MediaItem.STATUS_READY)

        response = self.client.get(reverse('review'))

        self.assertIsNone(self._current(response))
        self.assertNotContains(response, 'reset=1')

    def test_next_must_stay_on_this_site(self):
        response = self._decide(self.first, 'reject', next_url='https://evil.example/')

        self.assertEqual(response['Location'], reverse('item_detail', args=[self.first.guid]))

    def test_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse('review'))

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])
