"""Exercise Items paging, room isolation, offline reads and coordinator resolution with real SQLite."""

import asyncio
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from doom_eap.launcher.items import ItemsQuery, LedgerAvailability
from doom_eap.launcher.launcher_supervisor import LedgerReadAdapter
from doom_eap.launcher.presenters.items_presenter import ItemsPresenter
from doom_eap.runtime.delivery import DeliveryCoordinator, handle_delivery_control
from doom_eap.runtime.ledger import (
    Authority,
    CommandState,
    EffectSource,
    Ledger,
    NetworkReceipt,
    ReceiptDecision,
    ReceiptState,
    Resolution,
    ResolutionAction,
    RoomKey,
    SourceKind,
    StorageError,
)
from tests.test_delivery_coordinator import command
from tests.test_launcher_setup import dispatch_window

pytest_plugins = ("tests.test_delivery_coordinator", "tests.test_launcher_setup")


ROOM = RoomKey("room", 0, 1, "a" * 64)


@pytest.mark.parametrize("failure", [None, "transport"])
def test_capabilities_before_coordinator_or_during_transport_failure_are_unavailable(failure):
    from types import SimpleNamespace

    from doom_eap.runtime.native_transport import TransportError

    async def unavailable():
        raise TransportError("disconnected")

    async def check():
        coordinator = None if failure is None else SimpleNamespace(binding=SimpleNamespace(room=ROOM), require_owner=unavailable)
        emitted = []
        assert await handle_delivery_control(coordinator, {
            "type": "delivery_capabilities", "request_id": str(uuid4()), "room": asdict(ROOM),
        }, lambda kind, **data: emitted.append(data))
        assert emitted[-1]["can_resolve"] is False and emitted[-1]["ok"] is False
    asyncio.run(check())


def test_items_recovers_unpublished_ammo_with_same_operation_and_revision(owner, tmp_path):
    from doom_eap.launcher.events import adapt_event
    from doom_eap.runtime.ammo import AmmoCommandScope, LedgerAmmoRefill
    from doom_eap.runtime.ledger import AmmoKind, AmmoState

    class Storage:
        def __init__(self):
            self.values = {}
        async def read(self, key, timeout):
            return self.values.get(key)
        async def create(self, key, value, timeout):
            return self.values.setdefault(key, value)
        async def maximum(self, key, value, timeout):
            self.values[key] = max(self.values.get(key, 0), value)
            return self.values[key]
        async def counter(self, key, timeout):
            return self.values.get(key, 0)

    async def exercise(delivery, transport):
        ammo = LedgerAmmoRefill(delivery, Storage(), "room")
        delivery.accounting = ammo
        await ammo.claim()
        await ammo._synchronize(2)
        row = await ammo._reserve(AmmoKind.SPEND, 1, AmmoCommandScope("room", "map", "save", False))
        assert not await ammo.recover()
        row = await delivery.ledger.ammo_operation(delivery.binding.room, row.operation.operation_id)
        assert row.state is AmmoState.UNKNOWN
        presenter = ItemsPresenter()
        presenter.activate(delivery.binding.room, online=True)
        reader = LedgerReadAdapter(tmp_path / "delivery.sqlite3")
        presenter.observe(await reader.read(presenter.query(), online=True))
        presenter.capabilities(delivery.binding.room, can_resolve=True)
        event = adapt_event({"type": "delivery_local_status", "room": asdict(delivery.binding.room),
                            "command_id": "ammo-operation:" + row.operation.operation_id,
                            "expected_revision": row.revision, "item_id": 7770024})
        presenter.local_status(event.room, event.item_id, event.actions)
        assert len(presenter.local_actions) == 1
        presenter.select_action(presenter.local_actions[0])
        request = presenter.confirm()
        control = dict(type="item_resolution", room=asdict(request.room), request_id=request.request_id,
                       command_id=request.action.command_id, expected_revision=request.action.expected_revision,
                       action=request.action.kind.value)
        emitted = []
        await handle_delivery_control(delivery, {**control, "expected_revision": row.revision + 1},
                                      lambda kind, **data: emitted.append(data))
        assert not emitted[-1]["ok"]
        await handle_delivery_control(delivery, control, lambda kind, **data: emitted.append(data))
        assert emitted[-1]["ok"]
        totals = await delivery.ledger.ammo_totals(delivery.binding.room)
        assert totals.consumed == 1 and totals.reserved == 0
        operations = await delivery.ledger.ammo_operations(delivery.binding.room, tuple(AmmoState), None, 10)
        assert len(operations) == 1 and operations[0].operation.operation_id == row.operation.operation_id
    owner(exercise)


def test_expected_marker_reports_a_missing_owner_ledger_before_the_first_ui_read(tmp_path):
    path = tmp_path / "delivery.sqlite3"
    path.with_suffix(".expected").write_text("delivery ledger\n")
    reader = LedgerReadAdapter(path, expected=False)
    snapshot = asyncio.run(reader.read(ItemsQuery(ROOM, str(uuid4()))))
    assert snapshot.availability is LedgerAvailability.MISSING and snapshot.page is None


@pytest.mark.parametrize("action", list(ResolutionAction))
def test_local_uncertainty_uses_the_same_scoped_two_step_resolution_without_an_ap_index(owner, tmp_path, action):
    from doom_eap.launcher.events import adapt_event

    async def _assert_uncertain_local_resolution(action, delivery, original):
        assert original.state is CommandState.EFFECT_UNKNOWN and original.plan.receipt_index is None
        assert len(await delivery.ledger.commands(delivery.binding.room, tuple(CommandState), None, 10)) == (2 if action is ResolutionAction.TRY_AGAIN else 1)


    async def exercise(delivery, transport):
        transport.effect = 2
        source = EffectSource("starting", SourceKind.STARTING_GRANT, "starting:health", b"local acquisition")
        local = replace(command(delivery.context), receipt_index=None, local_source_id=source.source_id)
        await delivery.plan((local,), sources=(source,))
        await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        await delivery.recover("grant")
        row = await delivery.ledger.command(delivery.binding.room, "grant")
        presenter = ItemsPresenter()
        presenter.activate(delivery.binding.room, online=True)
        reader = LedgerReadAdapter(tmp_path / "delivery.sqlite3")
        presenter.observe(await reader.read(presenter.query(), online=True))
        presenter.capabilities(delivery.binding.room, can_resolve=True, can_spend=True)
        event = adapt_event({"type": "delivery_local_status", "room": asdict(delivery.binding.room),
                             "command_id": "grant", "expected_revision": row.revision, "item_id": 7770025})
        presenter.local_status(event.room, event.item_id, event.actions)
        chosen = next(a for a in presenter.local_actions if a.kind is action)
        presenter.select_action(chosen)
        request = presenter.confirm()
        assert request is not None and presenter.confirm() is None
        emitted = []
        control = {"type": "item_resolution", "room": asdict(request.room), "request_id": request.request_id,
                   "command_id": chosen.command_id, "expected_revision": chosen.expected_revision, "action": action.value}
        await handle_delivery_control(delivery, control, lambda kind, **data: emitted.append(data))
        await handle_delivery_control(delivery, control, lambda kind, **data: emitted.append(data))
        assert all(event["ok"] for event in emitted)
        original = await delivery.ledger.command(delivery.binding.room, "grant")
        await _assert_uncertain_local_resolution(action, delivery, original)
    owner(exercise)


def receipt(item=7770025):
    return NetworkReceipt(item, 1, 2, 0)


def test_items_view_labels_payment_recovery_and_requires_confirmation(ui):
    from doom_eap.launcher.events import adapt_event
    from doom_eap.launcher.ui.items_view import ItemsView
    from doom_eap.strings import text

    presenter = ItemsPresenter()
    presenter.activate(ROOM, online=True)
    presenter.authority = Authority.LEDGER
    presenter.model = replace(presenter.model, availability=LedgerAvailability.AVAILABLE)
    presenter.capabilities(ROOM, can_resolve=True)
    event = adapt_event(dict(type="delivery_local_status", room=asdict(ROOM), item_id=7770024,
                             command_id="ammo-operation:payment", expected_revision=1))
    presenter.local_status(event.room, event.item_id, event.actions)
    view = ItemsView(presenter)
    requests = []
    view.resolution_requested.connect(requests.append)
    try:
        button = view.local_buttons[ResolutionAction.TRY_AGAIN]
        assert button.text() == text("launcher.items", "recover_ammo_operation")
        assert view.local_buttons[ResolutionAction.MARK_AS_RECEIVED].isHidden()
        button.click()
        assert not requests
        assert view.details.text() == text("launcher.items", "confirm_recover_ammo_operation")
        view.confirm.click()
        assert len(requests) == 1 and requests[0].action.expected_revision == 1
    finally:
        view.close()


def test_newest_pages_pin_watermark_and_poll_old_rows_without_crossing_rooms(tmp_path):
    def _assert_paginated_old_updates(presenter):
        assert len(presenter.model.rows) == 153
        assert presenter.model.rows[-1].reason == "map_changed"
        assert all(row.item != 123 for row in presenter.model.rows)


    def _assert_offline_item_actions(presenter):
        assert [row.ap_index for row in presenter.model.rows] == list(range(149, 85, -1))
        assert not presenter.actions()


    async def exercise():
        path = tmp_path / "delivery.sqlite3"
        ledger = await Ledger.create(path)
        try:
            binding = await ledger.bind(ROOM, "connection", compatibility_revision=1, authority=Authority.LEDGER)
            await ledger.ingest(binding, 0, (receipt(),) * 150)
            other = await ledger.bind(replace(ROOM, seed="other"), "other", compatibility_revision=1)
            await ledger.ingest(other, 0, (receipt(123),))
            reader = LedgerReadAdapter(path)
            presenter = ItemsPresenter()
            presenter.activate(ROOM, online=True)
            first = await reader.read(presenter.query(), online=True)
            presenter.observe(first)
            _assert_offline_item_actions(presenter)
            await ledger.ingest(binding, 150, (receipt(99),) * 3)
            await ledger.apply(binding, (), (ReceiptDecision(0, ReceiptState.DEFERRED, "map_changed", b""),), ())
            while presenter.model.more:
                page = await reader.read(presenter.query(more=True), online=True)
                assert page.page.watermark == first.page.watermark
                assert all(row.ap_index < 150 for row in page.page)
                presenter.observe(page)
            assert len(presenter.model.rows) == 150
            presenter.observe(await reader.read(presenter.query(), online=True))
            _assert_paginated_old_updates(presenter)
            stale = await reader.read(presenter.query(), online=True)
            presenter.activate(other.room, online=False)
            query = presenter.query()
            presenter.observe(stale)
            assert presenter.model.rows == ()
            presenter.observe(await reader.read(query))
            assert [row.item for row in presenter.model.rows] == [123]
            assert not presenter.actions()
        finally:
            await ledger.close()

    asyncio.run(exercise())


def test_change_pages_keep_their_watermark_until_all_old_rows_are_read(tmp_path):
    async def exercise():
        path = tmp_path / "delivery.sqlite3"
        ledger = await Ledger.create(path)
        try:
            binding = await ledger.bind(ROOM, "connection", compatibility_revision=1, authority=Authority.LEDGER)
            await ledger.ingest(binding, 0, (receipt(),) * 150)
            reader, presenter = LedgerReadAdapter(path), ItemsPresenter()
            presenter.activate(ROOM, online=True)
            presenter.observe(await reader.read(presenter.query(), online=True))
            decisions = tuple(ReceiptDecision(index, ReceiptState.DEFERRED, "waiting", b"") for index in range(150))
            await ledger.apply(binding, (), decisions, ())
            page = await reader.read(presenter.query(), online=True)
            next_query = presenter.observe(page)
            assert next_query.watermark == page.page.watermark
            await ledger.apply(binding, (), (ReceiptDecision(0, ReceiptState.SATISFIED, None, b"proof", 1),), ())
            while next_query is not None:
                next_page = await reader.read(next_query, online=True)
                assert next_page.page.watermark == page.page.watermark
                next_query = presenter.observe(next_page)
            presenter.observe(await reader.read(presenter.query(), online=True))
            assert presenter.model.rows[-1].status.value == "delivered"
            assert len(presenter.model.rows) == 150
        finally:
            await ledger.close()

    asyncio.run(exercise())


def test_missing_corrupt_and_wrong_room_are_not_empty_delivery_history(tmp_path):
    async def exercise():
        path = tmp_path / "delivery.sqlite3"
        reader = LedgerReadAdapter(path)
        query = ItemsQuery(ROOM, str(uuid4()))
        missing = await reader.read(query)
        assert missing.availability is LedgerAvailability.MISSING and missing.page is None
        assert not path.exists()
        passive = await LedgerReadAdapter(path, expected=False).read(query)
        assert passive.availability is LedgerAvailability.INACTIVE and passive.page is None
        path.write_bytes(b"damaged database")
        damaged = await reader.read(query)
        assert damaged.availability is LedgerAvailability.UNAVAILABLE and damaged.page is None
        path.unlink()
        ledger = await Ledger.create(path)
        try:
            await ledger.bind(replace(ROOM, seed="other"), "connection", compatibility_revision=1)
            absent = await reader.read(query)
            assert absent.availability is LedgerAvailability.INACTIVE and absent.page is None
        finally:
            await ledger.close()

    asyncio.run(exercise())


def test_shadow_projection_cannot_claim_delivery_or_authorize_actions(tmp_path):
    async def exercise():
        path = tmp_path / "delivery.sqlite3"
        ledger = await Ledger.create(path)
        try:
            binding = await ledger.bind(ROOM, "connection", compatibility_revision=1)
            await ledger.ingest(binding, 0, (receipt(),))
            await ledger.apply(binding, (), (ReceiptDecision(0, ReceiptState.SATISFIED, None, b"legacy"),), ())
            presenter = ItemsPresenter()
            presenter.activate(ROOM, online=True)
            presenter.observe(await LedgerReadAdapter(path).read(presenter.query(), online=True))
            presenter.capabilities(ROOM, can_resolve=True)
            assert presenter.model.rows[0].status.value == "received"
            assert not presenter.model.can_resolve and not presenter.actions()
            assert presenter.model.rows[0].reason_code.value == "planning"
        finally:
            await ledger.close()

    asyncio.run(exercise())


def test_blocked_filter_finds_old_items_and_observes_their_resolution(tmp_path):
    async def exercise():
        path = tmp_path / "delivery.sqlite3"
        ledger = await Ledger.create(path)
        try:
            binding = await ledger.bind(ROOM, "connection", compatibility_revision=1, authority=Authority.LEDGER)
            await ledger.ingest(binding, 0, (receipt(),) * 150)
            await ledger.apply(binding, (), (ReceiptDecision(0, ReceiptState.REJECTED, "rejected", b""),), ())
            reader, presenter = LedgerReadAdapter(path), ItemsPresenter()
            presenter.activate(ROOM, online=True)
            presenter.observe(await reader.read(presenter.query(), online=True))
            assert presenter.model.attention_count == 1
            assert all(row.ap_index != 0 for row in presenter.model.rows)
            query = presenter.filter_blocked(True)
            presenter.observe(await reader.read(query, online=True))
            assert [row.ap_index for row in presenter.model.rows] == [0]
            await ledger.apply(
                binding,
                (),
                (
                    ReceiptDecision(0, ReceiptState.SATISFIED, None, b"proof", 1),
                    ReceiptDecision(1, ReceiptState.REJECTED, "rejected", b""),
                ),
                (),
            )
            presenter.observe(await reader.read(presenter.query(), online=True))
            assert [row.ap_index for row in presenter.model.rows if row.status.value == "blocked"] == [1]
            assert presenter.model.rows[-1].status.value == "delivered"
        finally:
            await ledger.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("action", list(ResolutionAction))
def test_resolution_requires_owner_room_revision_and_keeps_player_evidence(owner, tmp_path, action):
    def _assert_sent_command_confirmation(sent):
        assert sent[0].status.value == "waiting" and sent[0].reason_code.value == "confirmation"


    def _assert_single_resolution_request(presenter, request):
        assert request is not None and presenter.confirm() is None


    def _assert_successful_resolution_reply(emitted, presenter, request):
        assert emitted[-1]["ok"]
        assert presenter.resolution_result(request.room, request.request_id, ok=True)


    def _assert_offline_resolution_actions(presenter, transport):
        assert not presenter.actions()
        assert len(transport.effects) == 1


    def _assert_replacement_command_identity(commands, original):
        assert len(commands) == 2 and original.replacement_id is not None


    async def _assert_resolution_rejections(action, chosen, delivery, emit, emitted, request):
        control = {
            "type": "item_resolution",
            "request_id": request.request_id,
            "room": asdict(request.room),
            "command_id": chosen.command_id,
            "expected_revision": chosen.expected_revision,
            "action": action.value,
        }
        await handle_delivery_control(DeliveryCoordinator(), control, emit)
        assert not emitted[-1]["ok"]
        await handle_delivery_control(delivery, {**control, "room": asdict(replace(request.room, seed="other"))}, emit)
        assert not emitted[-1]["ok"]
        await handle_delivery_control(delivery, {**control, "expected_revision": chosen.expected_revision + 99}, emit)
        assert not emitted[-1]["ok"]
        return control


    async def _assert_planned_execution(delivery):
        planned = await delivery.ledger.items(delivery.binding.room, -1, 1)
        assert planned[0].reason_code.value == "execution"


    async def exercise(delivery, transport):
        transport.effect = 2
        await delivery.plan((command(delivery.context),))
        await _assert_planned_execution(delivery)
        await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        sent = await delivery.ledger.items(delivery.binding.room, -1, 1)
        _assert_sent_command_confirmation(sent)
        await delivery.recover("grant")
        presenter = ItemsPresenter()
        presenter.activate(delivery.binding.room, online=True)
        reader = LedgerReadAdapter(tmp_path / "delivery.sqlite3")
        presenter.observe(await reader.read(presenter.query(), online=True))
        assert not presenter.actions()
        emitted = []

        def emit(kind, **payload):
            emitted.append({"type": kind, **payload})

        control = {"type": "delivery_capabilities", "request_id": str(uuid4()), "room": asdict(delivery.binding.room)}
        await handle_delivery_control(delivery, control, emit)
        assert emitted[-1]["can_resolve"]
        presenter.capabilities(delivery.binding.room, can_resolve=emitted[-1]["can_resolve"])
        chosen = next(entry for entry in presenter.actions(0) if entry.kind is action)
        presenter.select_action(chosen)
        request = presenter.confirm()
        _assert_single_resolution_request(presenter, request)
        control = await _assert_resolution_rejections(action, chosen, delivery, emit, emitted, request)
        await handle_delivery_control(delivery, control, emit)
        _assert_successful_resolution_reply(emitted, presenter, request)
        await handle_delivery_control(delivery, control, emit)
        assert emitted[-1]["ok"]
        original = await delivery.ledger.command(request.room, chosen.command_id)
        assert original.state is CommandState.EFFECT_UNKNOWN
        if action is ResolutionAction.MARK_AS_RECEIVED:
            assert original.resolution is Resolution.ACCEPTED_BY_PLAYER
            presenter.observe(await reader.read(presenter.query(), online=True))
            assert presenter.model.rows[0].resolution is Resolution.ACCEPTED_BY_PLAYER
        else:
            commands = await delivery.ledger.commands(request.room, tuple(CommandState), None, 256)
            _assert_replacement_command_identity(commands, original)
        presenter.activate(request.room, online=False)
        _assert_offline_resolution_actions(presenter, transport)

    owner(exercise)


def _assert_visible_setup_progress(ui):
    assert ui.session_setup_detail.isVisible()
    assert all(label.isVisible() for label in ui.play_view.milestones)
    assert ui.play_view.rect().contains(ui.play_view.tabs.geometry())


def test_play_keyboard_navigation_settings_and_progress_fit_window(ui):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    ui.show()
    ui.resize(1280, 720)
    QApplication.processEvents()
    assert ui.pages.currentWidget() is ui.play_view
    QTest.keyClick(ui, Qt.Key.Key_S, Qt.KeyboardModifier.AltModifier)
    assert ui.pages.currentWidget() is ui.settings_view
    assert ui.ammo_refill_keybind.isVisible() and ui.death_link_grace.isVisible()
    QTest.keyClick(ui, Qt.Key.Key_C, Qt.KeyboardModifier.AltModifier)
    assert ui.pages.currentWidget() is not ui.play_view
    QTest.keyClick(ui, Qt.Key.Key_H, Qt.KeyboardModifier.AltModifier)
    assert ui.help_view.doctor_status.isVisible()
    QTest.keyClick(ui, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)
    assert ui.pages.currentWidget() is ui.play_view
    dispatch_window(ui, {"type": "injector_started"})
    _assert_visible_setup_progress(ui)
    option = 'env DOOM_AP_PATH="fixture path" %command%'
    dispatch_window(
        ui, {"type": "setup_ready", "adapter_state": "applied", "steam_launch_option": option, "meathook_ok": True}
    )
    ui.copy_launch_option_button.click()
    assert ui.launch_option.text() == option
    assert QApplication.clipboard().text() == option


@pytest.mark.parametrize("kind", list(ResolutionAction))
def test_items_confirmation_uses_selected_ap_identity_and_disables_double_click(ui, owner, tmp_path, monkeypatch, kind):
    from PySide6.QtWidgets import QApplication, QMessageBox

    def _assert_selected_item_resolution(delivery, kind, page, request):
        assert request.room == delivery.binding.room
        assert request.action == next(action for action in page.page[0].actions if action.kind is kind)


    async def exercise(delivery, transport):
        transport.effect = 2
        await delivery.plan((command(delivery.context),))
        await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        await delivery.recover("grant")
        presenter, view = ui.items_presenter, ui.items_view
        presenter.activate(delivery.binding.room, online=True)
        page = await LedgerReadAdapter(tmp_path / "delivery.sqlite3").read(presenter.query(), online=True)
        ui.controller.events.put(page)
        ui.timer.start(0)
        QApplication.processEvents()
        ui.timer.stop()
        presenter.capabilities(delivery.binding.room, can_resolve=True)
        view.render()
        ui.show()
        view.list.setCurrentRow(0)
        QApplication.processEvents()
        index = page.page[0].ap_index
        button = view.row_actions[index][kind]
        assert button.isVisible()
        if kind is ResolutionAction.MARK_AS_RECEIVED:
            monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
            button.click()
            ui.controller.resolve_item.assert_not_called()
            assert presenter.resolution is None
            monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
            view.row_actions[index][kind].click()
            view._confirm()
        else:
            button.click()
            assert view.row_confirm[index][0].isVisible()
            ui.controller.resolve_item.assert_not_called()
            view.row_confirm[index][0].click()
            view.row_confirm[index][0].click()
        ui.controller.resolve_item.assert_called_once()
        request = ui.controller.resolve_item.call_args.args[0]
        _assert_selected_item_resolution(delivery, kind, page, request)
        presenter.activate(replace(request.room, seed="other"), online=False)
        view.render()
        assert not view.row_actions
        assert view.confirm.isHidden()

    import queue

    ui.controller.events = queue.Queue()
    owner(exercise)


def _assert_hidden_delivered_items(delivered, presenter, view, waiting):
    assert not presenter.status(delivered)
    assert presenter.status(waiting)
    assert view.blocked.isHidden()


def test_items_hide_delivered_status_and_filter_without_attention(ui):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    from doom_eap.runtime.ledger import ItemReason, ItemRow, ItemStatus

    presenter, view = ui.items_presenter, ui.items_view
    presenter.activate(ROOM, online=True)
    delivered = ItemRow(2, 7770003, 1, ItemStatus.DELIVERED, None, (), 2)
    waiting = replace(delivered, ap_index=1, status=ItemStatus.WAITING)
    presenter.model = replace(presenter.model, rows=(delivered, waiting))
    view.render()
    _assert_hidden_delivered_items(delivered, presenter, view, waiting)
    marked = replace(delivered, resolution=Resolution.ACCEPTED_BY_PLAYER)
    assert presenter.status(marked)
    blocked = replace(waiting, status=ItemStatus.BLOCKED, reason_code=ItemReason.EFFECT_UNKNOWN)
    view.item_names[7770003] = "<b>external item</b>"
    view.player_names[2] = "<b>external player</b>"
    blocked = replace(blocked, sender=2)
    presenter.model = replace(presenter.model, rows=(delivered, blocked), attention_count=1)
    view.render()
    assert not view.blocked.isHidden()
    external_labels = [label for label in view.list.findChildren(QLabel) if "<b>" in label.text()]
    assert external_labels
    assert all(label.textFormat() is Qt.TextFormat.PlainText for label in external_labels)


def test_save_copy_contains_consistent_ledger_and_owner_reset_preserves_sources(tmp_path, monkeypatch):
    import zipfile
    from types import SimpleNamespace

    from doom_eap.launcher.launcher_session import APSessionOwner
    from doom_eap.runtime.inventory import namespace_id

    snapshot = SimpleNamespace(
        seed_name=ROOM.seed,
        team=ROOM.team,
        slot=ROOM.slot,
        slot_data={"native_generation_fingerprint": ROOM.generation_fp, "runtime": {"difficulty": {"engine": 0}}},
    )
    owner = APSessionOwner(tmp_path / "client", tmp_path / "data", tmp_path / "state")
    namespace = namespace_id(ROOM.seed, ROOM.team, ROOM.slot, ROOM.generation_fp)
    directory = owner.data_dir / "campaigns" / namespace
    directory.mkdir(parents=True)
    native = "ap-" + namespace[:40]
    (directory / "native.root").write_text(
        f"sentinel-native-root-v1\nnamespace={namespace}\nroot={native}\n", newline="\n"
    )
    (directory / "campaign.contract").write_text(
        f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={ROOM.generation_fp}\n"
        "provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\ndifficulty=0\nslot=AUTOSAVE0\n",
        newline="\n",
    )
    save = tmp_path / "remote" / native / "GAME-AUTOSAVE0/game.details"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"fixture save")
    ledger_path = tmp_path / "delivery.sqlite3"
    ledger = asyncio.run(Ledger.create(ledger_path))
    config = {
        "steam_remote_dir": str(tmp_path / "remote"),
        "save_games_dir": str(tmp_path / "local"),
        "delivery_ledger_file": str(ledger_path),
        "delivery_ledger_expected": True,
    }
    monkeypatch.setattr(owner, "game_processes", lambda config: ())
    try:
        binding = asyncio.run(ledger.bind(ROOM, "connection", compatibility_revision=1, authority=Authority.LEDGER))
        asyncio.run(ledger.ingest(binding, 0, (receipt(),) * 3))
        archive = owner.backup_local_files(snapshot, config)
        with zipfile.ZipFile(archive) as saved:
            copied = tmp_path / "copied.sqlite3"
            copied.write_bytes(saved.read("delivery.sqlite3"))
        page = asyncio.run(LedgerReadAdapter(copied).read(ItemsQuery(ROOM, str(uuid4()))))
        assert len(page.page) == 3
        with pytest.raises((RuntimeError, StorageError)):
            owner.restart_campaign(snapshot, config)
        assert save.read_bytes() == b"fixture save"
        assert len(asyncio.run(ledger.items(ROOM, -1, 10))) == 3
    finally:
        asyncio.run(ledger.close())


def test_ammo_spending_is_hidden_until_the_owning_profile_capability_arrives(ui):
    ui.items_presenter.activate(ROOM, online=True)
    ui.items_presenter.query()
    ui._update_ammo_refill_button()
    assert ui.ammo_refill_button.isHidden()
    ui.items_presenter.authority = Authority.LEDGER
    ui.items_presenter.capabilities(ROOM, can_resolve=True, can_spend=False)
    ui._update_ammo_refill_button()
    assert ui.ammo_refill_button.isHidden()
    ui.items_presenter.capabilities(ROOM, can_resolve=True, can_spend=True)
    ui._update_ammo_refill_button()
    assert not ui.ammo_refill_button.isHidden()


def test_capability_poll_waits_for_the_correlated_reply(ui):
    from PySide6.QtWidgets import QApplication

    from doom_eap.launcher.events import adapt_event

    ui.controller.operation_generation = 3
    ui.controller.connection_attempt_id = 2
    ui.controller.request_delivery_capabilities.return_value = True
    ui.items_presenter.activate(ROOM, online=True)
    ui.lifecycle_timer.start(0)
    for _ in range(5):
        QApplication.processEvents()
    ui.lifecycle_timer.stop()
    ui.controller.request_delivery_capabilities.assert_called_once()
    request_id = ui.controller.request_delivery_capabilities.call_args.args[1]
    dispatch_window(
        ui,
        adapt_event(
            {
                "type": "accounting_capability_changed",
                "room": asdict(ROOM),
                "request_id": request_id,
                "ok": False,
                "can_resolve": False,
            }
        ),
    )
    ui.lifecycle_timer.start(0)
    QApplication.processEvents()
    ui.lifecycle_timer.stop()
    assert ui.controller.request_delivery_capabilities.call_count == 2
