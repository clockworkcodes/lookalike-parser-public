"""
Офлайн-тесты: Telegram и Instagram заменены фейками, сеть не используется.

Запуск:
    python -m unittest discover -s tests -v
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ALLOWED_USER_ID"] = "42"
os.environ.pop("IG_SESSION_JSON", None)
os.environ.pop("RENDER_EXTERNAL_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from instagrapi.exceptions import LoginRequired, PleaseWaitFewMinutes  # noqa: E402
from instagrapi.types import User, UserShort  # noqa: E402

import bot as app  # noqa: E402

import tempfile  # noqa: E402

TMP_DIR = Path(tempfile.mkdtemp(prefix="lkp-tests-"))
app.SESSION_PATH = TMP_DIR / "session.json"


# --------------------------------------------------------------------------- #
# Фейки
# --------------------------------------------------------------------------- #


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.documents = []
        self._next_id = 100

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self._next_id += 1
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, reply_markup=reply_markup))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)

    async def send_document(self, chat_id, document, caption=None, **kwargs):
        self.documents.append(SimpleNamespace(chat_id=chat_id, document=document, caption=caption))


def short(pk, username, full_name=""):
    return UserShort(pk=str(pk), username=username, full_name=full_name)


class FakeInstagram:
    """Граф рекомендаций: pk -> список UserShort."""

    def __init__(self, graph, usernames, infos=None, gql_error=None, chaining_graph=None, fbsearch_graph=None):
        self.graph = graph
        self.usernames = usernames
        self.infos = infos or {}
        self.gql_error = gql_error
        self.chaining_graph = chaining_graph or {}
        self.fbsearch_graph = fbsearch_graph or {}
        self.calls = []
        self.dumps = 0

    following_graph = {}

    def user_following_v1(self, user_id, amount=0):
        self.calls.append(("following", user_id))
        users = self.following_graph.get(user_id, [])
        return users[:amount] if amount else users

    def private_request(self, endpoint, params=None, **kwargs):
        assert endpoint == "fbsearch/accounts_recs/", endpoint
        user_id = params["target_user_id"]
        self.calls.append(("fbsearch", user_id))
        return {"users": self.fbsearch_graph.get(user_id, []), "status": "ok"}

    def dump_settings(self, path):
        self.dumps += 1
        Path(path).write_text(json.dumps({"authorization_data": {"sessionid": "fake"}}), encoding="utf-8")
        return True

    def user_id_from_username(self, username):
        self.calls.append(("id", username))
        return self.usernames[username]

    def user_related_profiles_gql(self, user_id):
        self.calls.append(("gql", user_id))
        if self.gql_error:
            raise self.gql_error
        return self.graph.get(user_id, [])

    def chaining(self, user_id):
        self.calls.append(("chaining", user_id))
        return {"users": self.chaining_graph.get(user_id, [])}

    def user_info(self, user_id):
        self.calls.append(("info", user_id))
        return self.infos[user_id]


def make_info(pk, username, followers, bio="", category=""):
    return User(
        pk=str(pk),
        username=username,
        full_name=username.title(),
        is_private=False,
        profile_pic_url="https://example.com/p.jpg",
        is_verified=False,
        media_count=10,
        follower_count=followers,
        following_count=5,
        biography=bio,
        external_url="https://link.example",
        category_name=category,
        is_business=False,
    )


def sign_init_data(user_id, token=app.BOT_TOKEN, auth_date=None):
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAH-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def no_sleep(*args, **kwargs):
    await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Парсинг параметров
# --------------------------------------------------------------------------- #


class ParamsTests(unittest.TestCase):
    def test_seeds_normalization(self):
        seeds = app.parse_seeds("@Prod.One, https://www.instagram.com/beat_maker/ ,prod.one bad!name")
        self.assertEqual(seeds, ["prod.one", "beat_maker"])

    def test_limit_is_clamped(self):
        self.assertEqual(app.build_params("a", 5, "", False).max_accounts, 10)
        self.assertEqual(app.build_params("a", 999, "", False).max_accounts, 200)
        self.assertEqual(app.build_params("a", "oops", "", False).max_accounts, 50)

    def test_followers_only_in_deep_mode(self):
        p = app.build_params("a", 50, "", False, 100, 200)
        self.assertIsNone(p.min_followers)
        p = app.build_params("a", 50, "", True, "1 000", None)
        self.assertEqual(p.min_followers, 1000)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            app.build_params("", 50, "", False)
        with self.assertRaises(ValueError):
            app.build_params("a", 50, "", True, 500, 100)
        with self.assertRaises(ValueError):
            app.build_params("a", 50, "", True, "abc", None)

    def test_command_args(self):
        p = app.parse_command_args("seeds=user1, user2 limit=80 kw=prod, beats deep=1 min=1000 max=50000")
        self.assertEqual(p.seeds, ["user1", "user2"])
        self.assertEqual(p.max_accounts, 80)
        self.assertEqual(p.keywords, ["prod", "beats"])
        self.assertTrue(p.deep_check)
        self.assertEqual((p.min_followers, p.max_followers), (1000, 50000))

        p = app.parse_command_args("user1, user2")
        self.assertEqual(p.seeds, ["user1", "user2"])
        self.assertFalse(p.deep_check)

    def test_extract_users_from_any_payload_shape(self):
        payload = {
            "status": "ok",
            "suggestions": [
                {"user": {"pk": "11", "username": "nested_one", "full_name": "N1", "is_verified": True}},
                {"node": {"id": "12", "username": "graph_node"}},
            ],
            "users": [{"pk_id": "13", "username": "pk_id_user"}, {"pk": "99", "username": "target_self"}],
            "junk": {"id": "abc", "username": "not_numeric"},
        }
        users = app.extract_users_from_payload(payload, exclude_pk="99")
        self.assertEqual(sorted(u.username for u in users), ["graph_node", "nested_one", "pk_id_user"])

    def test_keyword_matching(self):
        self.assertTrue(app.matches_keywords([], "x"))
        self.assertTrue(app.matches_keywords(["beats"], "lofi", "Best BEATS here"))
        self.assertFalse(app.matches_keywords(["beats"], "lofi", None))


# --------------------------------------------------------------------------- #
# Сессия Instagram
# --------------------------------------------------------------------------- #


def settings_with(sessionid, marker):
    from instagrapi import Client

    settings = Client().get_settings()
    settings["authorization_data"] = {"ds_user_id": "1", "sessionid": sessionid}
    settings["mid"] = marker
    return settings


class SessionTests(unittest.TestCase):
    def setUp(self):
        app.SESSION_PATH.unlink(missing_ok=True)
        self._env = app.IG_SESSION_JSON

    def tearDown(self):
        app.IG_SESSION_JSON = self._env
        app.ig_client = None
        app.SESSION_PATH.unlink(missing_ok=True)

    def test_env_session_loaded_with_request_delays(self):
        app.IG_SESSION_JSON = json.dumps(settings_with("abc", "ENV"))
        app.init_instagram()
        self.assertIsNotNone(app.ig_client, app.ig_init_error)
        self.assertEqual(app.ig_client.delay_range, [5.0, 15.0])
        self.assertEqual(app.ig_client.mid, "ENV")
        self.assertTrue(app.SESSION_PATH.exists())

    def test_saved_session_preferred_when_same_sessionid(self):
        app.SESSION_PATH.write_text(json.dumps(settings_with("abc", "SAVED")), encoding="utf-8")
        app.IG_SESSION_JSON = json.dumps(settings_with("abc", "ENV"))
        app.init_instagram()
        self.assertEqual(app.ig_client.mid, "SAVED")

    def test_env_wins_when_session_regenerated(self):
        app.SESSION_PATH.write_text(json.dumps(settings_with("old", "SAVED")), encoding="utf-8")
        app.IG_SESSION_JSON = json.dumps(settings_with("new", "ENV"))
        app.init_instagram()
        self.assertEqual(app.ig_client.mid, "ENV")
        saved = json.loads(app.SESSION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["authorization_data"]["sessionid"], "new")

    def test_corrupted_saved_session_falls_back_to_env(self):
        app.SESSION_PATH.write_text("{broken", encoding="utf-8")
        app.IG_SESSION_JSON = json.dumps(settings_with("abc", "ENV"))
        app.init_instagram()
        self.assertEqual(app.ig_client.mid, "ENV")

    def test_session_without_sessionid_rejected(self):
        app.IG_SESSION_JSON = json.dumps({"uuids": {}})
        app.init_instagram()
        self.assertIsNone(app.ig_client)
        self.assertIn("sessionid", app.ig_init_error)

    def test_save_session_is_atomic_dump(self):
        app.IG_SESSION_JSON = json.dumps(settings_with("abc", "ENV"))
        app.init_instagram()
        app.ig_client.mid = "REFRESHED"
        app.save_session(app.ig_client)
        saved = json.loads(app.SESSION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["mid"], "REFRESHED")
        self.assertFalse(app.SESSION_PATH.with_suffix(".json.tmp").exists())


# --------------------------------------------------------------------------- #
# Выгрузка
# --------------------------------------------------------------------------- #


class ExportTests(unittest.TestCase):
    def setUp(self):
        app.STATE.reset(app.build_params("seed", 50, "", False), 42)
        app.STATE.is_running = False
        app.STATE.finish_reason = "limit"

    def test_csv_has_bom_and_escapes_formulas(self):
        row = app.base_row(short(1, "user"), "seed", 1)
        row["biography"] = '=HYPERLINK("http://evil")'
        data = app.build_csv([row])
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        text = data.decode("utf-8-sig")
        self.assertIn("username,full_name,profile_url", text.splitlines()[0])
        self.assertIn("'=HYPERLINK", text)

    def test_status_links_point_to_instagram(self):
        app.STATE.params = app.build_params("prod.zzy", 50, "", False)
        app.STATE.current = "рекомендации @prod.zzy (уровень 0)"
        text = app.render_status()
        self.assertEqual(text.count('<a href="https://instagram.com/prod.zzy">@prod.zzy</a>'), 2)
        self.assertNotRegex(text, r"(?<![>/])@prod\.zzy(?!<)")

    def test_caption_lists_first_15(self):
        rows = [app.base_row(short(i, f"user{i}"), "seed", 1) for i in range(20)]
        caption = app.build_caption(rows, app.STATE.params)
        self.assertEqual(caption.count('<a href="https://instagram.com/'), 15)
        self.assertIn('<a href="https://instagram.com/user0">@user0</a>', caption)
        self.assertIn("ещё 5", caption)
        self.assertLess(len(caption), 1024)


# --------------------------------------------------------------------------- #
# BFS-обход
# --------------------------------------------------------------------------- #


class CrawlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._sleep = app.interruptible_sleep
        app.interruptible_sleep = no_sleep
        for name in app.related_source_hits:
            app.related_source_hits[name] = 0
        self.bot = FakeBot()

    def tearDown(self):
        app.interruptible_sleep = self._sleep
        app.ig_client = None
        app.STATE.is_running = False

    async def run_job(self, cl, params):
        app.ig_client = cl
        ok, status, _ = await app.start_job(self.bot, params, chat_id=42)
        self.assertTrue(ok, status)
        await asyncio.wait_for(app.STATE.task, timeout=5)

    async def test_fast_mode_bfs_with_keywords(self):
        graph = {
            "1": [short(2, "beats_a"), short(3, "random_guy"), short(4, "x", "Prod Studio")],
            "3": [short(5, "beats_b"), short(2, "beats_a")],
            "2": [short(6, "music_c")],
        }
        cl = FakeInstagram(graph, {"seed": "1"})
        await self.run_job(cl, app.build_params("seed", 10, "beats, prod", False))

        found = [r["username"] for r in app.STATE.results]
        self.assertEqual(found, ["beats_a", "x", "beats_b"])
        self.assertEqual(app.STATE.finish_reason, "exhausted")
        self.assertFalse(app.STATE.is_running)
        self.assertEqual(len(self.bot.documents), 1)
        self.assertNotIn(("info", "2"), cl.calls)
        self.assertIn("@beats_a", self.bot.documents[0].caption)
        self.assertGreaterEqual(cl.dumps, 1, "сессия должна сохраняться после задачи")

    async def test_limit_stops_crawl(self):
        graph = {"1": [short(i, f"user{i}") for i in range(2, 40)]}
        cl = FakeInstagram(graph, {"seed": "1"})
        await self.run_job(cl, app.build_params("seed", 10, "", False))
        self.assertEqual(len(app.STATE.results), 10)
        self.assertEqual(app.STATE.finish_reason, "limit")

    async def test_deep_mode_bio_and_followers(self):
        graph = {"1": [short(2, "a"), short(3, "b"), short(4, "c")]}
        infos = {
            "2": make_info(2, "a", 5000, bio="I make beats"),
            "3": make_info(3, "b", 50, bio="beats too"),
            "4": make_info(4, "c", 9000, category="Musician/Band"),
        }
        cl = FakeInstagram(graph, {"seed": "1"}, infos=infos)
        await self.run_job(cl, app.build_params("seed", 10, "beats, musician", True, 1000, 10000))
        found = {r["username"]: r for r in app.STATE.results}
        self.assertEqual(set(found), {"a", "c"})
        self.assertEqual(found["a"]["followers"], 5000)
        self.assertEqual(found["c"]["category"], "Musician/Band")

    async def test_falls_back_to_private_chaining(self):
        chaining = {"1": [{"pk": "7", "username": "from_chaining", "full_name": ""}]}
        cl = FakeInstagram({}, {"seed": "1"}, gql_error=RuntimeError("gated"), chaining_graph=chaining)
        await self.run_job(cl, app.build_params("seed", 10, "", False))
        self.assertEqual([r["username"] for r in app.STATE.results], ["from_chaining"])

    async def test_falls_back_to_fbsearch(self):
        fb = {"1": [{"pk": 8, "username": "from_fbsearch", "full_name": "FB"}]}
        cl = FakeInstagram({}, {"seed": "1"}, gql_error=RuntimeError("gated"), fbsearch_graph=fb)
        await self.run_job(cl, app.build_params("seed", 10, "", False))
        self.assertEqual([r["username"] for r in app.STATE.results], ["from_fbsearch"])
        self.assertEqual(app.related_source_hits["fbsearch"], 1)

    async def test_successful_source_is_tried_first(self):
        graph = {"1": [short(2, "a")], "2": [short(3, "b")]}
        cl = FakeInstagram(graph, {"seed": "1"})
        await self.run_job(cl, app.build_params("seed", 10, "", False))
        related_calls = [c for c in cl.calls if c[0] in ("gql", "chaining", "fbsearch")]
        # seed: chaining → fbsearch → gql (успех); дальше gql идёт первым
        self.assertEqual(related_calls[:4], [("chaining", "1"), ("fbsearch", "1"), ("gql", "1"), ("gql", "2")])

    async def test_seed_with_disabled_recommendations_uses_following(self):
        from instagrapi.exceptions import InvalidTargetUser

        class NotEligibleSeed(FakeInstagram):
            def chaining(self, user_id):
                if user_id == "1":
                    self.calls.append(("chaining", user_id))
                    raise InvalidTargetUser("Not eligible for chaining.")
                return super().chaining(user_id)

        cl = NotEligibleSeed({}, {"seed": "1"}, chaining_graph={"2": [{"pk": "4", "username": "peer_of_a"}]})
        cl.following_graph = {"1": [short(2, "followed_a"), short(3, "followed_b")]}
        await self.run_job(cl, app.build_params("seed", 10, "", False))

        rows = {r["username"]: r for r in app.STATE.results}
        self.assertEqual(set(rows), {"followed_a", "followed_b", "peer_of_a"})
        self.assertEqual(rows["followed_a"]["source"], "seed (подписки)")
        self.assertEqual(rows["peer_of_a"]["source"], "followed_a")
        self.assertEqual(app.STATE.following_fallbacks, 1)
        self.assertNotIn(("following", "2"), cl.calls, "запасной путь только для seed")

    async def test_no_recommendations_explained(self):
        cl = FakeInstagram({}, {"seed": "1"})
        await self.run_job(cl, app.build_params("seed", 10, "", False))
        self.assertEqual(app.STATE.results, [])
        self.assertIn("/diag seed", app.STATE.error_text)
        self.assertIn("/diag seed", self.bot.sent[-1].text)

    async def test_keyword_miss_explained(self):
        cl = FakeInstagram({"1": [short(2, "someone")]}, {"seed": "1"})
        await self.run_job(cl, app.build_params("seed", 10, "beats", False))
        self.assertIn("глубокий режим", app.STATE.error_text)

    async def test_login_required_is_fatal_but_delivers(self):
        class Breaking(FakeInstagram):
            def user_related_profiles_gql(self, user_id):
                if user_id == "2":
                    raise LoginRequired("expired")
                return super().user_related_profiles_gql(user_id)

        cl = Breaking({"1": [short(2, "a"), short(3, "b")]}, {"seed": "1"})
        await self.run_job(cl, app.build_params("seed", 10, "", False))
        self.assertEqual(app.STATE.finish_reason, "error")
        self.assertIn("IG_SESSION_JSON", app.STATE.error_text)
        self.assertEqual(len(self.bot.documents), 1)

    async def test_rate_limit_is_survived(self):
        class Limited(FakeInstagram):
            hits = 0

            def user_related_profiles_gql(self, user_id):
                if user_id == "1" and Limited.hits == 0:
                    Limited.hits += 1
                    raise PleaseWaitFewMinutes("slow down")
                return super().user_related_profiles_gql(user_id)

        cl = Limited({"1": [short(2, "a")], "9": [short(3, "b")]}, {"seed": "1", "seed2": "9"})
        await self.run_job(cl, app.build_params("seed, seed2", 10, "", False))
        self.assertEqual(app.STATE.errors, 1)
        self.assertEqual([r["username"] for r in app.STATE.results], ["b"])

    async def test_cancel_stops_and_delivers(self):
        graph = {str(i): [short(i + 1, f"user{i + 1}")] for i in range(1, 500)}
        cl = FakeInstagram(graph, {"seed": "1"})
        app.ig_client = cl
        await app.start_job(self.bot, app.build_params("seed", 200, "", False), chat_id=42)
        await asyncio.sleep(0.05)
        app.STATE.request_cancel()
        await asyncio.wait_for(app.STATE.task, timeout=5)
        self.assertEqual(app.STATE.finish_reason, "cancel")
        self.assertLess(len(app.STATE.results), 200)
        self.assertEqual(len(self.bot.documents), 1)

    async def test_request_delay_does_not_block_event_loop(self):
        class Slow(FakeInstagram):
            def user_related_profiles_gql(self, user_id):
                time.sleep(0.4)  # имитация delay_range: time.sleep внутри instagrapi
                return super().user_related_profiles_gql(user_id)

        cl = Slow({"1": [short(2, "a")]}, {"seed": "1"})
        app.ig_client = cl
        await app.start_job(self.bot, app.build_params("seed", 10, "", False), chat_id=42)
        task = app.STATE.task
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.01)
        self.assertGreater(ticks, 15, "event loop был заблокирован во время паузы")

    async def test_only_one_job_at_a_time(self):
        app.ig_client = FakeInstagram({}, {"seed": "1"})
        app.STATE.is_running = True
        ok, status, _ = await app.start_job(self.bot, app.build_params("seed", 10, "", False), 42)
        self.assertFalse(ok)
        self.assertEqual(status, 409)

    async def test_no_instagram_client(self):
        app.ig_client = None
        ok, status, _ = await app.start_job(self.bot, app.build_params("seed", 10, "", False), 42)
        self.assertFalse(ok)
        self.assertEqual(status, 503)


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = FakeBot()
        self.started = []
        self._start_job = app.start_job

        async def fake_start_job(bot, params, chat_id):
            self.started.append((params, chat_id))
            return True, 200, "ok"

        app.start_job = fake_start_job
        self.client = TestClient(TestServer(app.build_web_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        app.start_job = self._start_job
        await self.client.close()

    async def post(self, payload):
        resp = await self.client.post("/api/start", json=payload)
        return resp.status, await resp.json()

    def payload(self, **overrides):
        data = {
            "initData": sign_init_data(42),
            "seed_accounts": "seed",
            "max_accounts": 30,
            "keyword_filter": "beats",
            "deep_check": True,
            "min_followers": 100,
            "max_followers": None,
        }
        data.update(overrides)
        return data

    async def test_health_and_index(self):
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["status"], "ok")
        resp = await self.client.head("/health")
        self.assertEqual(resp.status, 200)
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("telegram-web-app.js", await resp.text())

    async def test_valid_owner_starts_job(self):
        status, body = await self.post(self.payload())
        self.assertEqual(status, 200, body)
        params, chat_id = self.started[0]
        self.assertEqual(chat_id, 42)
        self.assertEqual(params.max_accounts, 30)
        self.assertEqual(params.min_followers, 100)

    async def test_missing_init_data(self):
        status, _ = await self.post(self.payload(initData=""))
        self.assertEqual(status, 401)

    async def test_forged_signature(self):
        status, _ = await self.post(self.payload(initData=sign_init_data(42, token="999:OTHER")))
        self.assertEqual(status, 401)

    async def test_other_user_forbidden(self):
        status, _ = await self.post(self.payload(initData=sign_init_data(1337)))
        self.assertEqual(status, 403)
        self.assertEqual(self.started, [])

    async def test_stale_init_data(self):
        status, _ = await self.post(self.payload(initData=sign_init_data(42, auth_date=time.time() - 2 * 86400)))
        self.assertEqual(status, 401)

    async def test_validation_error(self):
        status, body = await self.post(self.payload(seed_accounts="!!!"))
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    async def test_bad_json(self):
        resp = await self.client.post("/api/start", data="not json", headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
