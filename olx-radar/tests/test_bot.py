import json
from pathlib import Path

import pytest

from olx import bot, storage, texts
from olx.bot import AllowedOnly, _fmt_watch, build_dispatcher
from olx.config import settings
from olx.models import Filters, NotifyMode, PollMode

# Тестовый Telegram user_id -- storage.add_watch() теперь мультипользовательский
# и требует владельца; какой именно id, для большинства тестов не важно.
USER = 999999

SAMPLE_PATH = Path(__file__).resolve().parent / "fixtures" / "sample-response.json"


class _User:
    def __init__(self, uid):
        self.id = uid


async def _handler(event, data):
    return "handled"


@pytest.mark.asyncio
async def test_owner_passes(db):
    storage.add_allowed_user(settings.telegram_owner_id, added_by=settings.telegram_owner_id)
    mw = AllowedOnly()
    data = {"event_from_user": _User(settings.telegram_owner_id)}
    assert await mw(_handler, object(), data) == "handled"


@pytest.mark.asyncio
async def test_stranger_is_dropped_silently(db):
    # Никого не добавляли в allowed_users -- таблица пуста, все чужие.
    mw = AllowedOnly()
    for user in (_User(1), _User(settings.telegram_owner_id + 1), None):
        data = {"event_from_user": user}
        assert await mw(_handler, object(), data) is None


@pytest.mark.asyncio
async def test_allowed_users_in_db_pass_others_dont(db):
    # Источник истины -- таблица allowed_users, не .env (см. AllowedOnly): того,
    # кого не добавили через add_allowed_user/seed_allowed_users, не пускает.
    for uid in (111, 222, 333):
        storage.add_allowed_user(uid, added_by=settings.telegram_owner_id)

    mw = AllowedOnly()
    for uid in (111, 222, 333):
        data = {"event_from_user": _User(uid)}
        assert await mw(_handler, object(), data) == "handled"

    data = {"event_from_user": _User(444)}
    assert await mw(_handler, object(), data) is None


def test_dispatcher_builds():
    assert build_dispatcher() is not None


def test_bot_renders_cards_through_the_texts_module():
    # Раскладка карточки проверяется в test_texts.py; здесь достаточно убедиться,
    # что bot.py не завёл собственный формат в обход общего.
    assert _fmt_watch is texts.watch_card


@pytest.mark.asyncio
async def test_wizard_step_totals_cover_the_optional_category_step():
    # Шаг с разделом появляется не всегда: 9 вопросов без него, 10 с ним. Если счётчик
    # разъедется, владелец увидит «Шаг 9 из 9».
    assert "Шаг 10 из 10" in texts.step(10, 10, "Уведомления", "тело")
    assert "Шаг 9 из 9" in texts.step(9, 9, "Уведомления", "тело")


@pytest.fixture
def fsm_state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    return FSMContext(storage=storage, key=key)


@pytest.mark.asyncio
async def test_category_browsing_does_not_advance_the_step_counter_per_level(fsm_state):
    # Дерево категорий может быть сколь угодно глубоким -- счётчик шага не должен
    # расти с каждым уровнем погружения (иначе «Шаг 15 из 9» на третьей марке авто).
    await fsm_state.update_data(step=1, steps=9)

    msg = _FakeMessage()
    await bot._show_category_level(msg, fsm_state, parent_id=None)  # корень дерева
    assert "Шаг 2 из 9" in msg.sent[-1]

    # Спускаемся на реальный подраздел ("Авто" -> "Легкові автомобілі" не лист --
    # берём первый корневой раздел с детьми, чтобы не зависеть от точных id).
    from olx import categories

    root_with_children = next(r for r in categories.children(None) if categories.children(r["id"]))
    msg2 = _FakeMessage()
    await bot._show_category_level(msg2, fsm_state, parent_id=root_with_children["id"])
    assert "Шаг 2 из 9" in msg2.sent[-1], "номер шага не должен был сдвинуться"


@pytest.mark.asyncio
async def test_add_mode_category_only_watch_has_empty_query(db):
    # Полный путь storage.add_watch() для режима «раздел без фразы» уже покрыт
    # storage-тестами; здесь проверяем именно то, что видит владелец в карточке.
    watch = storage.add_watch(
        "", Filters(category_id=108), PollMode.FAST, NotifyMode.NEW, user_id=USER
    )
    assert watch.query == ""
    label = texts.watch_label(watch)
    assert label and "Легков" in label


# --- Мультипользовательская изоляция на уровне команд бота: каждый видит и
# трогает только свои запросы. storage.py уже проверен отдельно (test_storage.py) --
# здесь важно, что bot.py действительно подставляет message.from_user.id/
# call.from_user.id, а не что-то одно на всех.

OTHER_USER = 555555


@pytest.mark.asyncio
async def test_cmd_list_only_shows_the_caller_own_watches(db):
    storage.add_watch("mine", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    storage.add_watch(
        "someone else's", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=OTHER_USER
    )

    message = _FakeMessage(user_id=USER)
    await bot.cmd_list(message)

    assert "mine" in message.sent[-1]
    assert "someone else's" not in message.sent[-1]


@pytest.mark.asyncio
async def test_cmd_del_picker_does_not_offer_someone_elses_watch(db):
    storage.add_watch("mine", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    storage.add_watch(
        "someone else's", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=OTHER_USER
    )

    kb = bot._watch_picker("delask", USER)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("mine" in label for label in labels)
    assert not any("else" in label for label in labels)


@pytest.mark.asyncio
async def test_cmd_export_list_only_includes_the_caller_own_watches(db):
    storage.add_watch("mine", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    storage.add_watch(
        "someone else's", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=OTHER_USER
    )

    watches = storage.list_watches(user_id=USER, enabled_only=False)
    assert [w.query for w in watches] == ["mine"]


class _FakeUser:
    def __init__(self, uid=USER):
        self.id = uid


class _FakeMessage:
    def __init__(self, user_id=USER):
        self.sent: list[str] = []
        self.from_user = _FakeUser(user_id)

    async def answer(self, text, **kwargs):
        self.sent.append(text)


@pytest.fixture
def db(tmp_path):
    storage.init_db(tmp_path / "resume.db")


@pytest.fixture
def sample():
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


class _FakeCommand:
    def __init__(self, args=None):
        self.args = args


# --- /users, /adduser, /removeuser: доступны только реальному владельцу
# (settings.telegram_owner_id, он же USER в этих тестах -- см. .env в conftest),
# не любому, кто просто есть в allowed_users.

FRIEND = 555555
NOT_OWNER = 777777  # обычный разрешённый пользователь, не владелец


@pytest.mark.asyncio
async def test_cmd_users_lists_everyone_for_owner(db):
    storage.seed_allowed_users(USER, [FRIEND])
    message = _FakeMessage(user_id=USER)

    await bot.cmd_users(message)

    assert str(USER) in message.sent[-1]
    assert str(FRIEND) in message.sent[-1]


@pytest.mark.asyncio
async def test_cmd_users_silently_ignored_for_non_owner(db):
    storage.seed_allowed_users(USER, [NOT_OWNER])
    message = _FakeMessage(user_id=NOT_OWNER)

    await bot.cmd_users(message)

    assert message.sent == []


@pytest.mark.asyncio
async def test_cmd_adduser_grants_access(db):
    storage.seed_allowed_users(USER)
    message = _FakeMessage(user_id=USER)

    await bot.cmd_adduser(message, _FakeCommand(str(FRIEND)))

    assert storage.is_allowed(FRIEND)
    assert str(FRIEND) in message.sent[-1]


@pytest.mark.asyncio
async def test_cmd_adduser_twice_reports_already_present(db):
    storage.seed_allowed_users(USER)
    message = _FakeMessage(user_id=USER)

    await bot.cmd_adduser(message, _FakeCommand(str(FRIEND)))
    await bot.cmd_adduser(message, _FakeCommand(str(FRIEND)))

    assert "уже был" in message.sent[-1]


@pytest.mark.asyncio
async def test_cmd_adduser_rejects_non_numeric_args(db):
    storage.seed_allowed_users(USER)
    message = _FakeMessage(user_id=USER)

    for bad_args in (None, "", "не число", "12abc"):
        await bot.cmd_adduser(message, _FakeCommand(bad_args))

    assert not storage.is_allowed(FRIEND)
    assert all("Формат" in text for text in message.sent)


@pytest.mark.asyncio
async def test_cmd_adduser_ignored_for_non_owner(db):
    storage.seed_allowed_users(USER, [NOT_OWNER])
    message = _FakeMessage(user_id=NOT_OWNER)

    await bot.cmd_adduser(message, _FakeCommand(str(FRIEND)))

    assert not storage.is_allowed(FRIEND)
    assert message.sent == []


@pytest.mark.asyncio
async def test_cmd_removeuser_revokes_access_and_pauses_watches(db):
    storage.seed_allowed_users(USER, [FRIEND])
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=FRIEND)
    message = _FakeMessage(user_id=USER)

    await bot.cmd_removeuser(message, _FakeCommand(str(FRIEND)))

    assert not storage.is_allowed(FRIEND)
    [reloaded] = storage.list_watches(user_id=FRIEND, enabled_only=False)
    assert reloaded.id == watch.id
    assert not reloaded.enabled
    assert "1" in message.sent[-1]  # "приостановлено: 1"


@pytest.mark.asyncio
async def test_cmd_removeuser_cannot_remove_the_owner(db):
    storage.seed_allowed_users(USER)
    message = _FakeMessage(user_id=USER)

    await bot.cmd_removeuser(message, _FakeCommand(str(USER)))

    assert storage.is_allowed(USER)
    assert "владелец" in message.sent[-1]


@pytest.mark.asyncio
async def test_cmd_removeuser_reports_when_not_in_list(db):
    storage.seed_allowed_users(USER)
    message = _FakeMessage(user_id=USER)

    await bot.cmd_removeuser(message, _FakeCommand(str(FRIEND)))

    assert "и так не было" in message.sent[-1]


@pytest.mark.asyncio
async def test_cmd_removeuser_ignored_for_non_owner(db):
    storage.seed_allowed_users(USER, [NOT_OWNER, FRIEND])
    message = _FakeMessage(user_id=NOT_OWNER)

    await bot.cmd_removeuser(message, _FakeCommand(str(FRIEND)))

    assert storage.is_allowed(FRIEND)
    assert message.sent == []


@pytest.mark.asyncio
async def test_resume_reports_nothing_to_do_when_all_watches_are_active(db):
    storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    message = _FakeMessage()

    await bot.cmd_resume(message)

    assert message.sent == ["Всё уже в работе."]


@pytest.mark.asyncio
async def test_resume_reactivates_a_watch_that_parses_cleanly_again(db, sample, monkeypatch):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    storage.update_watch(watch.id, enabled=False)
    monkeypatch.setattr(bot.api, "search_raw", lambda *a, **kw: sample)

    await bot.cmd_resume(_FakeMessage())

    assert storage.list_watches(enabled_only=False)[0].enabled


@pytest.mark.asyncio
async def test_resume_keeps_a_schema_broken_watch_paused_and_warns(db, monkeypatch):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    storage.update_watch(watch.id, enabled=False)
    # {"results": []} -- ровно та форма, которую поймало бы SchemaError у монитора:
    # ключа "data" нет, значит формат ответа сломан, а не выдача пуста.
    monkeypatch.setattr(bot.api, "search_raw", lambda *a, **kw: {"results": []})

    message = _FakeMessage()
    await bot.cmd_resume(message)

    assert not storage.list_watches(enabled_only=False)[0].enabled
    assert any("не включил" in text.lower() for text in message.sent)


@pytest.mark.asyncio
async def test_resume_does_not_block_on_a_transient_network_failure(db, monkeypatch):
    # Сбой сети при проверке -- не поломка формата: честно приостановленный запрос
    # не должен застрять из-за того, что OLX именно сейчас недоступен.
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    storage.update_watch(watch.id, enabled=False)

    def _boom(*a, **kw):
        raise ConnectionError("сеть недоступна")

    monkeypatch.setattr(bot.api, "search_raw", _boom)

    await bot.cmd_resume(_FakeMessage())

    assert storage.list_watches(enabled_only=False)[0].enabled
