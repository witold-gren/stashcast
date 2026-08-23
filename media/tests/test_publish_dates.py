"""
Tests for publication dates.

Feeds must date an episode by when it was published on the source platform, not by
when it happened to be downloaded. `publish_date` exists on the model and already
drives feed ordering, but nothing used to populate it - so every item was undated and
feeds silently fell back to `downloaded_at`.
"""

import datetime
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from media.models import MediaGroup, MediaItem
from media.service.media_info import parse_publish_date
from media.service.resolve import PrefetchResult
from media.tasks import backfill_publish_dates, fetch_publish_date_for_item

UTC = datetime.timezone.utc


class ParsePublishDateTest(TestCase):
    """Tests for turning yt-dlp metadata into a publication date"""

    def test_timestamp_preferred_over_date_string(self):
        """The epoch timestamp carries a time of day, so it wins"""
        result = parse_publish_date({'timestamp': 1415628355, 'upload_date': '20200101'})
        self.assertEqual(result, datetime.datetime(2014, 11, 10, 14, 5, 55, tzinfo=UTC))

    def test_upload_date_string(self):
        result = parse_publish_date({'upload_date': '20141110'})
        self.assertEqual(result, datetime.datetime(2014, 11, 10, tzinfo=UTC))

    def test_release_timestamp_fallback(self):
        result = parse_publish_date({'release_timestamp': 1415628355})
        self.assertEqual(result, datetime.datetime(2014, 11, 10, 14, 5, 55, tzinfo=UTC))

    def test_release_date_fallback(self):
        result = parse_publish_date({'release_date': '20141110'})
        self.assertEqual(result, datetime.datetime(2014, 11, 10, tzinfo=UTC))

    def test_result_is_timezone_aware(self):
        """USE_TZ is on, so a naive datetime would raise warnings and compare wrongly"""
        self.assertIsNotNone(parse_publish_date({'upload_date': '20141110'}).tzinfo)

    def test_no_date_available(self):
        self.assertIsNone(parse_publish_date({}))
        self.assertIsNone(parse_publish_date(None))

    def test_unparseable_values_are_ignored(self):
        self.assertIsNone(parse_publish_date({'upload_date': 'nonsense'}))
        self.assertIsNone(parse_publish_date({'timestamp': 'nonsense'}))

    def test_falls_through_bad_timestamp_to_good_string(self):
        """A broken timestamp must not shadow a usable date string"""
        result = parse_publish_date({'timestamp': 'nonsense', 'upload_date': '20141110'})
        self.assertEqual(result, datetime.datetime(2014, 11, 10, tzinfo=UTC))


class ApplyPrefetchDateTest(TestCase):
    """Tests that a prefetched date reaches the model"""

    def _item(self):
        return MediaItem.objects.create(
            source_url='https://youtu.be/v1',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
        )

    def test_publish_date_is_stored(self):
        from media.processing import _apply_prefetch_result

        item = self._item()
        result = PrefetchResult(
            title='Odcinek',
            has_audio_streams=True,
            publish_date=datetime.datetime(2014, 11, 10, tzinfo=UTC),
        )

        _apply_prefetch_result(item, result, log_path=None)

        item.refresh_from_db()
        self.assertEqual(item.publish_date, datetime.datetime(2014, 11, 10, tzinfo=UTC))

    def test_missing_date_does_not_clear_an_existing_one(self):
        """A re-fetch that yields no date must not wipe a date we already have"""
        from media.processing import _apply_prefetch_result

        item = self._item()
        item.publish_date = datetime.datetime(2014, 11, 10, tzinfo=UTC)
        item.save()

        _apply_prefetch_result(
            item, PrefetchResult(title='Odcinek', has_audio_streams=True), log_path=None
        )

        item.refresh_from_db()
        self.assertEqual(item.publish_date, datetime.datetime(2014, 11, 10, tzinfo=UTC))


class BackfillPublishDatesTest(TestCase):
    """Tests for filling in dates on items downloaded before dates were captured"""

    def _ready_item(self, title, publish_date=None):
        return MediaItem.objects.create(
            source_url=f'https://youtu.be/{title}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug=title,
            title=title,
            status=MediaItem.STATUS_READY,
            downloaded_at=timezone.now(),
            publish_date=publish_date,
        )

    def test_fetches_and_stores_the_date(self):
        item = self._ready_item('a')
        fetched = PrefetchResult(publish_date=datetime.datetime(2014, 11, 10, tzinfo=UTC))

        with patch('media.service.resolve.prefetch', return_value=fetched):
            result = fetch_publish_date_for_item(item)

        self.assertEqual(result, datetime.datetime(2014, 11, 10, tzinfo=UTC))
        item.refresh_from_db()
        self.assertEqual(item.publish_date, datetime.datetime(2014, 11, 10, tzinfo=UTC))

    def test_source_without_a_date_returns_none(self):
        item = self._ready_item('a')

        with patch('media.service.resolve.prefetch', return_value=PrefetchResult()):
            self.assertIsNone(fetch_publish_date_for_item(item))

        item.refresh_from_db()
        self.assertIsNone(item.publish_date)

    def test_backfill_only_touches_undated_items(self):
        dated = self._ready_item('dated', publish_date=datetime.datetime(2020, 1, 1, tzinfo=UTC))
        self._ready_item('undated')
        fetched = PrefetchResult(publish_date=datetime.datetime(2014, 11, 10, tzinfo=UTC))

        with patch('media.service.resolve.prefetch', return_value=fetched) as mock_prefetch:
            updated, skipped = backfill_publish_dates()

        self.assertEqual(updated, 1)
        self.assertEqual(mock_prefetch.call_count, 1)
        dated.refresh_from_db()
        self.assertEqual(dated.publish_date, datetime.datetime(2020, 1, 1, tzinfo=UTC))

    def test_refresh_all_touches_dated_items_too(self):
        self._ready_item('dated', publish_date=datetime.datetime(2020, 1, 1, tzinfo=UTC))
        self._ready_item('undated')
        fetched = PrefetchResult(publish_date=datetime.datetime(2014, 11, 10, tzinfo=UTC))

        with patch('media.service.resolve.prefetch', return_value=fetched):
            updated, _ = backfill_publish_dates(only_missing=False)

        self.assertEqual(updated, 2)

    def test_limit_is_respected(self):
        for name in ('a', 'b', 'c'):
            self._ready_item(name)
        fetched = PrefetchResult(publish_date=datetime.datetime(2014, 11, 10, tzinfo=UTC))

        with patch('media.service.resolve.prefetch', return_value=fetched):
            updated, _ = backfill_publish_dates(limit=2)

        self.assertEqual(updated, 2)

    def test_a_failing_item_does_not_abort_the_run(self):
        """A deleted or private video must not stop the rest of the backfill"""
        self._ready_item('bad')
        self._ready_item('good')
        good = PrefetchResult(publish_date=datetime.datetime(2014, 11, 10, tzinfo=UTC))

        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise Exception('Video unavailable')
            return good

        with patch('media.service.resolve.prefetch', side_effect=flaky):
            updated, skipped = backfill_publish_dates()

        self.assertEqual(updated, 1)
        self.assertEqual(skipped, 1)

    def test_queued_items_are_skipped(self):
        """Nothing to fetch for something that has not been downloaded yet"""
        MediaItem.objects.create(
            source_url='https://youtu.be/q',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
            status=MediaItem.STATUS_QUEUED,
        )

        with patch('media.service.resolve.prefetch') as mock_prefetch:
            updated, skipped = backfill_publish_dates()

        self.assertEqual((updated, skipped), (0, 0))
        mock_prefetch.assert_not_called()


class FeedOrderingTest(TestCase):
    """Tests that feeds are ordered by publication date, undated items last"""

    def setUp(self):
        self.group = MediaGroup.objects.create(name='Lekcje')

    def _item(self, slug, publish_date, downloaded_days_ago):
        return MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug=slug,
            title=slug,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
            group=self.group,
            publish_date=publish_date,
            downloaded_at=timezone.now() - datetime.timedelta(days=downloaded_days_ago),
        )

    def test_newest_published_comes_first_regardless_of_download_order(self):
        """The old upload downloaded most recently must not jump to the top"""
        # Downloaded yesterday but published in 2014
        self._item('old-publish', datetime.datetime(2014, 1, 1, tzinfo=UTC), 1)
        # Downloaded a month ago but published in 2024
        self._item('new-publish', datetime.datetime(2024, 1, 1, tzinfo=UTC), 30)

        from media.feeds import _PUBLISH_ORDER

        ordered = list(
            MediaItem.objects.filter(status=MediaItem.STATUS_READY).order_by(*_PUBLISH_ORDER)
        )
        self.assertEqual([i.slug for i in ordered], ['new-publish', 'old-publish'])

    def test_undated_items_sort_last(self):
        """Items whose date is not filled in yet must not lead the feed"""
        self._item('dated', datetime.datetime(2014, 1, 1, tzinfo=UTC), 30)
        self._item('undated', None, 1)

        from media.feeds import _PUBLISH_ORDER

        ordered = list(
            MediaItem.objects.filter(status=MediaItem.STATUS_READY).order_by(*_PUBLISH_ORDER)
        )
        self.assertEqual([i.slug for i in ordered], ['dated', 'undated'])

    def test_item_pubdate_prefers_publish_date(self):
        from media.feeds import AudioFeed

        item = self._item('a', datetime.datetime(2014, 1, 1, tzinfo=UTC), 1)
        self.assertEqual(AudioFeed().item_pubdate(item), item.publish_date)

    def test_item_pubdate_falls_back_to_download_date(self):
        from media.feeds import AudioFeed

        item = self._item('a', None, 1)
        self.assertEqual(AudioFeed().item_pubdate(item), item.downloaded_at)
