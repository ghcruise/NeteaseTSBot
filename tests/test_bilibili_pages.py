import copy
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend import main
from backend.db import Base
from backend.models import QueueItem


class BilibiliPagesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patches = ExitStack()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(main, "new_session", self.sessions))
        self.patches.enter_context(patch.object(main, "_schedule_ts_description_update"))
        self.patches.enter_context(patch.object(main, "_get_bilibili_duration_limit_ms", return_value=60000))
        self.view = {
            "bvid": "BVTEST", "title": "Album", "duration": 9999,
            "owner": {"name": "Artist"}, "tname": "Music", "pic": "https://example.com/cover.jpg",
            # Deliberately unsorted; the queue must follow P order.
            "pages": [
                {"page": 2, "cid": 202, "part": "Second", "duration": 20},
                {"page": 1, "cid": 101, "part": "First", "duration": 10},
                {"page": 3, "cid": 303, "part": "Third", "duration": 30},
            ],
        }
        self.fetch = self.patches.enter_context(patch.object(main, "_fetch_bilibili_view_sync", return_value=self.view))
        self.resolve = self.patches.enter_context(patch.object(main, "_resolve_bilibili_playback_payload", AsyncMock(
            return_value=("audio:p1", 9999000, "Artist", "Music", "cover")
        )))
        self.start = self.patches.enter_context(patch.object(main, "_start_resolved_queue_item", AsyncMock()))
        self.begin = self.patches.enter_context(patch.object(main, "_begin_play_request", AsyncMock(return_value=7)))
        self.current = self.patches.enter_context(patch.object(main, "_is_play_request_current", AsyncMock(return_value=True)))
        self.clear = self.patches.enter_context(patch.object(main, "_clear_pending_play_request", AsyncMock()))

    def request(self, **kwargs):
        return main.AddBilibiliQueueRequest(
            **{"video_id": "BVTEST", "title": "Album", "duration_ms": 9999000,
               "include_all_pages": True, **kwargs}
        )

    def rows(self):
        with self.sessions() as session:
            return list(session.scalars(select(QueueItem).order_by(QueueItem.position)))

    async def test_adds_all_pages_in_order_with_individual_durations(self):
        with self.sessions() as session:
            session.add(QueueItem(position=10, track_id="custom:existing", title="Existing", source_url=""))
            session.commit()
        result = await main.add_queue_bilibili(self.request())
        rows = self.rows()[1:]
        self.assertEqual([1, 2, 3], [row.source_page for row in rows])
        self.assertEqual([101, 202, 303], [row.source_cid for row in rows])
        self.assertEqual([10000, 20000, 30000], [row.duration for row in rows])
        self.assertEqual([11, 12, 13], [row.position for row in rows])
        self.assertEqual(["Album - P1 First", "Album - P2 Second", "Album - P3 Third"], [row.title for row in rows])
        self.assertEqual([row.id for row in rows], result["ids"])
        self.assertEqual(3, result["count"])
        self.assertEqual(rows[0].id, result["id"])
        self.fetch.assert_called_once_with("BVTEST")
        self.begin.assert_not_awaited()
        self.resolve.assert_not_awaited()

    async def test_immediate_play_starts_only_p1_after_adding_all_pages(self):
        async def start(session, item, **kwargs):
            self.assertEqual(3, len(self.rows()))
            self.assertEqual(1, item.source_page)
            self.assertEqual(10000, item.duration)
            self.assertEqual("audio:p1", kwargs["source_url"])

        self.start.side_effect = start
        await main.add_queue_bilibili(self.request(play_now=True))
        self.resolve.assert_awaited_once_with(
            video_id="BVTEST", page=1, cid=101, artist="Artist", album="Music",
            artwork_url="https://example.com/cover.jpg", duration_ms=10000,
        )
        self.start.assert_awaited_once()
        self.assertEqual(["audio:p1", "", ""], [row.source_url for row in self.rows()])
        self.clear.assert_awaited_once_with(request_generation=7)

    async def test_explicit_page_or_cid_adds_only_that_page(self):
        for selector in ({"page": 2}, {"cid": 202}, {"video_id": "https://www.bilibili.com/video/BVTEST?p=2"}):
            with self.subTest(selector=selector):
                result = await main.add_queue_bilibili(self.request(**selector))
                row = self.rows()[-1]
                self.assertEqual(1, result["count"])
                self.assertEqual((2, 202, 20000), (row.source_page, row.source_cid, row.duration))

    async def test_single_page_uses_page_duration_instead_of_search_total(self):
        self.view["pages"] = [{"page": 1, "cid": 101, "duration": 10}]
        await main.add_queue_bilibili(self.request())
        row, = self.rows()
        self.assertEqual("Album", row.title)
        self.assertEqual(10000, row.duration)

    async def test_each_queued_page_plays_its_own_cid_and_duration(self):
        result = await main.add_queue_bilibili(self.request())

        async def resolve(**kwargs):
            return (f"audio:p{kwargs['page']}", kwargs["duration_ms"],
                    kwargs["artist"], kwargs["album"], kwargs["artwork_url"])

        self.resolve.side_effect = resolve
        for item_id in result["ids"]:
            await main._play_queue_item_internal(item_id, requested_by="test")
        self.assertEqual(
            [(1, 101, 10000), (2, 202, 20000), (3, 303, 30000)],
            [(call.kwargs["page"], call.kwargs["cid"], call.kwargs["duration_ms"])
             for call in self.resolve.await_args_list],
        )
        self.assertEqual(["audio:p1", "audio:p2", "audio:p3"], [row.source_url for row in self.rows()])

    async def test_incomplete_or_invalid_page_data_adds_nothing(self):
        original = copy.deepcopy(self.view)
        variants = [
            [], [None],
            [{"page": 1, "cid": 101}],
            [{"page": 1, "cid": 101, "duration": 0}],
            [{"page": 1, "duration": 10}],
            [original["pages"][1], original["pages"][1]],
            [original["pages"][1], original["pages"][2]],
        ]
        for pages in variants:
            with self.subTest(pages=pages):
                self.view["pages"] = pages
                with self.assertRaises(HTTPException):
                    await main.add_queue_bilibili(self.request())
                self.assertEqual([], self.rows())

    async def test_late_page_over_limit_does_not_leave_partial_queue(self):
        self.view["pages"][2]["duration"] = 61
        with self.assertRaises(HTTPException) as raised:
            await main.add_queue_bilibili(self.request(play_now=True))
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual([], self.rows())
        self.resolve.assert_not_awaited()
        self.clear.assert_awaited_once_with(request_generation=7)

    async def test_fetch_or_download_failure_does_not_add_only_p1(self):
        for target in (self.fetch, self.resolve):
            with self.subTest(target=target):
                target.side_effect = HTTPException(status_code=502, detail="upstream unavailable")
                with self.assertRaises(HTTPException):
                    await main.add_queue_bilibili(self.request(play_now=True))
                self.assertEqual([], self.rows())
                target.side_effect = None

    async def test_superseded_playback_does_not_enqueue(self):
        self.current.return_value = False
        with self.assertRaises(HTTPException) as raised:
            await main.add_queue_bilibili(self.request(play_now=True))
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual([], self.rows())
        self.start.assert_not_awaited()

    async def test_invalid_or_mismatched_selection_does_not_enqueue(self):
        for selector in ({"page": 4}, {"page": 0}, {"cid": 999}, {"page": 1, "cid": 202}):
            with self.subTest(selector=selector):
                with self.assertRaises(HTTPException) as raised:
                    await main.add_queue_bilibili(self.request(**selector))
                self.assertEqual(400, raised.exception.status_code)
                self.assertEqual([], self.rows())

    def test_missing_page_duration_never_falls_back_to_multipart_total(self):
        self.view["pages"][0].pop("duration")
        metadata = main._normalize_bilibili_view_data(self.view, page=2, cid=202)
        self.assertIsNone(metadata["duration_ms"])

    def test_av_page_url_and_missing_explicit_page(self):
        self.assertEqual("https://www.bilibili.com/video/av123?p=2", main._build_bilibili_video_url("av123", page=2))
        self.assertIsNone(main._extract_bilibili_page("BVTEST"))
        self.assertEqual(("BVTEST", 1, None), main._normalize_bilibili_reference("BVTEST"))
