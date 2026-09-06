"""Tests for the gateway-side clarify primitive (tools/clarify_gateway.py).

The clarify tool needs to ask the user a question and block the agent
thread until they respond.  These tests cover the module-level state
machine: register, wait, resolve via button, resolve via text-fallback,
"Other"-button text-capture flip, timeout, session boundary cleanup.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor


def _clear_clarify_state():
    """Reset module-level state between tests."""
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


class TestClarifyPrimitive:
    """Core register/wait/resolve mechanics."""

    def setup_method(self):
        _clear_clarify_state()

    def test_button_choice_resolves_wait(self):
        """resolve_gateway_clarify unblocks wait_for_response with the chosen string."""
        from tools import clarify_gateway as cm

        cm.register("id1", "sk1", "Pick one", ["A", "B", "C"])

        def resolver():
            time.sleep(0.05)
            cm.resolve_gateway_clarify("id1", "B")

        threading.Thread(target=resolver).start()
        result = cm.wait_for_response("id1", timeout=10.0)
        assert result == "B"

    def test_first_resolution_wins(self):
        """A late cancellation must not overwrite an already-selected choice."""
        from tools import clarify_gateway as cm

        entry = cm.register("id-race", "sk-race", "Pick one", ["A", "B"])

        assert cm.resolve_gateway_clarify("id-race", "A") is True
        assert cm.resolve_gateway_clarify("id-race", "") is False
        assert entry.response == "A"

    def test_open_ended_auto_awaits_text(self):
        """Clarify with no choices is in text-capture mode immediately."""
        from tools import clarify_gateway as cm

        entry = cm.register("id2", "sk2", "Free form?", None)
        assert entry.awaiting_text is True

        # get_pending_for_session returns the entry so the gateway
        # text-intercept can find it.
        pending = cm.get_pending_for_session("sk2")
        assert pending is not None
        assert pending.clarify_id == "id2"

    def test_button_choice_does_not_auto_await(self):
        """Multi-choice clarify should NOT be in text-capture mode initially."""
        from tools import clarify_gateway as cm

        entry = cm.register("id3", "sk3", "Pick", ["X", "Y"])
        assert entry.awaiting_text is False
        assert cm.get_pending_for_session("sk3") is None

    def test_include_choice_prompts_returns_multi_choice_entry(self):
        """Gateway typed replies must see active choice prompts too."""
        from tools import clarify_gateway as cm

        cm.register("id3b", "sk3b", "Pick", ["X", "Y"])
        pending = cm.get_pending_for_session("sk3b", include_choice_prompts=True)
        assert pending is not None
        assert pending.clarify_id == "id3b"


    def test_clear_session_cancels_pending_entries(self):
        """clear_session unblocks blocked threads with empty response."""
        from tools import clarify_gateway as cm

        cm.register("id7", "sk7", "Q?", ["A"])

        def waiter():
            return cm.wait_for_response("id7", timeout=10.0)

        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)
            cancelled = cm.clear_session("sk7")
            assert cancelled == 1
            result = fut.result(timeout=10.0)
            # clear_session sets response="" then the wait returns it
            assert result == ""


    def test_clear_session_preserves_resolved_response(self):
        """clear_session must not clobber an answer that already won.

        First-writer-wins (doryani-ai on PR #75732): a button callback that
        resolved the entry before session cleanup must keep its response.
        clear_session only cancels entries whose event is not yet set, so
        the racing waiter observes the real answer, not the empty sentinel.
        """
        from tools import clarify_gateway as cm

        cm.register("id-race", "sk-race", "Pick one", ["A", "B"])

        def waiter():
            return cm.wait_for_response("id-race", timeout=10.0)

        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)
            # Button wins the race first...
            assert cm.resolve_gateway_clarify("id-race", "B") is True
            # ...then session cleanup runs before the waiter wakes.
            cancelled = cm.clear_session("sk-race")
            assert cancelled == 0
            result = fut.result(timeout=10.0)
            # The real answer must survive cleanup, not the "" cancellation.
            assert result == "B"


    def test_notify_register_unregister_clears_pending(self):
        """unregister_notify cancels any pending clarify so threads unwind."""
        from tools import clarify_gateway as cm

        cm.register("id9", "sk9", "Q?", ["A"])

        def waiter():
            return cm.wait_for_response("id9", timeout=10.0)

        with ThreadPoolExecutor(1) as pool:
            fut = pool.submit(waiter)
            time.sleep(0.05)

            cm.register_notify("sk9", lambda entry: None)
            cm.unregister_notify("sk9")

            # unregister_notify calls clear_session; thread unwinds
            result = fut.result(timeout=10.0)
            assert result == ""

    def test_session_index_isolation(self):
        """Entries from different sessions don't leak across get_pending lookups."""
        from tools import clarify_gateway as cm

        cm.register("idA", "alpha", "Q?", None)  # auto-await text
        cm.register("idB", "beta", "Q?", None)   # auto-await text

        a = cm.get_pending_for_session("alpha")
        b = cm.get_pending_for_session("beta")
        assert a is not None and a.clarify_id == "idA"
        assert b is not None and b.clarify_id == "idB"

    def test_clarify_timeout_config_default(self):
        """get_clarify_timeout returns a positive int (default 3600)."""
        from tools import clarify_gateway as cm

        timeout = cm.get_clarify_timeout()
        # Default 3600s OR whatever is in the user's loaded config.
        # Floor check: must be a positive int, not crashed.
        assert isinstance(timeout, int)
        assert timeout > 0


class TestGatewayTextIntercept:
    """The gateway's _handle_message intercepts text replies to pending clarifies."""

    def setup_method(self):
        _clear_clarify_state()

    def test_get_pending_for_session_returns_oldest_text_awaiting(self):
        """When two clarifies are pending, get_pending_for_session returns the
        first that is awaiting_text (the older one if both)."""
        from tools import clarify_gateway as cm

        # Older multi-choice (not awaiting text)
        cm.register("first", "sk", "Q1?", ["A"])
        # Newer open-ended (awaiting text)
        cm.register("second", "sk", "Q2?", None)

        pending = cm.get_pending_for_session("sk")
        # The newer one is awaiting text; the older isn't.
        assert pending is not None
        assert pending.clarify_id == "second"

        # Now flip the first to text mode too.  Both are awaiting text,
        # FIFO returns the older one.
        cm.mark_awaiting_text("first")
        pending2 = cm.get_pending_for_session("sk")
        assert pending2 is not None
        assert pending2.clarify_id == "first"
    def test_text_fallback_enables_awaiting_text_for_multi_choice(self):
        """When base send_clarify renders choices as text, mark_awaiting_text
        is called so the gateway text-intercept can capture the reply."""
        from tools import clarify_gateway as cm

        entry = cm.register("id-tf", "sk-tf", "Pick one", ["A", "B", "C"])
        # Initially, multi-choice does NOT await text (button path)
        assert entry.awaiting_text is False

        # After the base send_clarify text fallback calls mark_awaiting_text:
        flipped = cm.mark_awaiting_text("id-tf")
        assert flipped is True

        # Now get_pending_for_session should find it
        pending = cm.get_pending_for_session("sk-tf")
        assert pending is not None
        assert pending.clarify_id == "id-tf"
        
        # Clean up
        cm.clear_session("sk-tf")


class TestCoverageGaps:
    """Cover remaining branches: signature(), get_entry miss, find_awaiting
    with deleted entry, cancel with None entry, timeout exception, get_notify."""

    def setup_method(self):
        _clear_clarify_state()

    def test_entry_signature(self):
        """_ClarifyEntry.signature() returns the expected dict."""
        from tools import clarify_gateway as cm

        entry = cm.register("sig1", "sk", "Q?", ["A", "B"])
        sig = entry.signature()
        assert sig["clarify_id"] == "sig1"
        assert sig["session_key"] == "sk"
        assert sig["question"] == "Q?"
        assert sig["choices"] == ["A", "B"]


    def test_wait_for_response_unknown_id_returns_none(self):
        """wait_for_response on a non-existent id returns None immediately."""
        from tools import clarify_gateway as cm

        assert cm.wait_for_response("nonexistent-id", timeout=0.1) is None


    def test_get_clarify_timeout_exception_returns_default(self, monkeypatch):
        """get_clarify_timeout returns 3600 when load_config raises."""
        from tools import clarify_gateway as cm

        monkeypatch.setattr("hermes_cli.config.load_config",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cm.get_clarify_timeout() == 3600


    def test_get_notify_returns_none_when_not_registered(self):
        """get_notify returns None for an unregistered session."""
        from tools import clarify_gateway as cm

        assert cm.get_notify("unregistered") is None


class TestClarifyTimeoutResolution:
    """resolve_clarify_timeout is the single source of truth for the clarify
    timeout, shared by the CLI, TUI/desktop, and messaging-gateway paths."""

    def test_canonical_agent_key(self):
        from tools import clarify_gateway as cm

        assert cm.resolve_clarify_timeout({"agent": {"clarify_timeout": 900}}) == 900


    def test_non_positive_preserved_as_unlimited_sentinel(self):
        """<= 0 is passed through verbatim — the waiting loops read it as
        'unlimited', so the resolver must not clamp it to a positive default."""
        from tools import clarify_gateway as cm

        assert cm.resolve_clarify_timeout({"agent": {"clarify_timeout": 0}}) == 0
        assert cm.resolve_clarify_timeout({"clarify": {"timeout": -1}}) == -1


class TestUnlimitedWait:
    """timeout <= 0 makes wait_for_response block until the answer arrives
    instead of auto-skipping."""

    def setup_method(self):
        _clear_clarify_state()

    def test_zero_timeout_waits_until_resolved(self):
        from tools import clarify_gateway as cm

        cm.register("u1", "sk", "Q?", ["A", "B"])
        result_box = {}

        def waiter():
            result_box["r"] = cm.wait_for_response("u1", timeout=0)

        t = threading.Thread(target=waiter)
        t.start()
        # An unlimited wait cannot finish while nothing resolves it: still
        # running after a comfortable margin (old code auto-skipped at once).
        t.join(timeout=1.5)
        assert t.is_alive()

        # Once resolved, the unlimited wait returns the real answer.
        cm.resolve_gateway_clarify("u1", "B")
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert result_box["r"] == "B"


class TestMultiSelectTextFallback:
    """Multi-select clarifies via the gateway text fallback.

    The adapter's numbered-list fallback asks the user to reply with
    comma/space-separated numbers; _coerce_text_response must map those to a
    JSON array of choice labels (which _parse_multi_select_response on the
    tool side decodes into a list).
    """

    def setup_method(self):
        _clear_clarify_state()

    def _register_multi(self, cid="m1", choices=("A", "B", "C")):
        from tools import clarify_gateway as cm
        entry = cm.register(cid, "sk", "Pick some", list(choices), multi_select=True)
        # Text fallback path always flips awaiting_text on.
        cm.mark_awaiting_text(cid)
        return entry

    def test_register_stores_multi_select_flag(self):
        entry = self._register_multi()
        assert entry.multi_select is True
        assert entry.signature()["multi_select"] is True


    def test_multi_select_without_choices_is_ignored(self):
        """multi_select on an open-ended clarify is meaningless — dropped."""
        from tools import clarify_gateway as cm
        entry = cm.register("s2", "sk", "Q?", None, multi_select=True)
        assert entry.multi_select is False


    def test_duplicate_selections_deduped(self):
        import json
        from tools import clarify_gateway as cm
        entry = self._register_multi()
        coerced = cm._coerce_text_response(entry, "1, 1, 2")
        assert json.loads(coerced) == ["A", "B"]

    def test_resolve_text_response_end_to_end(self):
        """resolve_text_response_for_session delivers the JSON array to the waiter."""
        import json
        from tools import clarify_gateway as cm
        self._register_multi(cid="m3")
        result_box = {}

        def waiter():
            result_box["r"] = cm.wait_for_response("m3", timeout=5)

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        assert cm.resolve_text_response_for_session("sk", "1,2") is True
        t.join(timeout=5)
        assert json.loads(result_box["r"]) == ["A", "B"]

    def test_single_select_regression_numeric(self):
        """Single-select coercion unchanged: '2' maps to the choice label string."""
        from tools import clarify_gateway as cm
        entry = cm.register("s3", "sk", "Q?", ["A", "B", "C"])
        assert cm._coerce_text_response(entry, "2") == "B"

    def test_single_select_regression_label(self):
        from tools import clarify_gateway as cm
        entry = cm.register("s4", "sk", "Q?", ["A", "B"])
        assert cm._coerce_text_response(entry, "b") == "B"


class TestNativeRejectClassification:
    """Rejected typed replies must distinguish free prose from bad selections.

    Free prose cancels/falls through (deadlock break). Selection-shaped but
    invalid replies (out-of-range number, unrecognised comma-list) keep the
    pending clarify armed so the user can retry.
    """

    def setup_method(self):
        _clear_clarify_state()

    def test_multi_select_out_of_range_is_invalid_selection(self):
        from tools import clarify_gateway as cm

        entry = cm.register(
            "ms-oor", "sk-ms", "Pick some", ["A", "B", "C"], multi_select=True,
        )
        assert entry.awaiting_text is False
        value, reason = cm._coerce_text_response_detailed(entry, "99")
        assert value is None
        assert reason == "invalid_selection"
        assert cm.attempt_text_response_for_session("sk-ms", "99") == (
            cm.TEXT_REJECTED_SELECTION
        )
        pending = cm.get_pending_for_session("sk-ms", include_choice_prompts=True)
        assert pending is not None
        assert not pending.event.is_set()

    def test_multi_select_bad_comma_list_is_invalid_selection(self):
        from tools import clarify_gateway as cm

        entry = cm.register(
            "ms-bad", "sk-ms2", "Pick some", ["A", "B", "C"], multi_select=True,
        )
        value, reason = cm._coerce_text_response_detailed(entry, "1,99")
        assert value is None
        assert reason == "invalid_selection"
        assert cm.attempt_text_response_for_session("sk-ms2", "nope,nope") == (
            cm.TEXT_REJECTED_SELECTION
        )
        pending = cm.get_pending_for_session("sk-ms2", include_choice_prompts=True)
        assert pending is not None
        assert not pending.event.is_set()

    def test_multi_select_free_prose_is_rejected_prose(self):
        from tools import clarify_gateway as cm

        entry = cm.register(
            "ms-prose", "sk-ms3", "Pick some", ["A", "B"], multi_select=True,
        )
        value, reason = cm._coerce_text_response_detailed(
            entry, "just checking the visual UI, no need to pass any data",
        )
        assert value is None
        assert reason == "prose"
        assert cm.attempt_text_response_for_session(
            "sk-ms3", "just checking the visual UI, no need to pass any data",
        ) == cm.TEXT_REJECTED_PROSE

    def test_single_select_out_of_range_is_invalid_selection(self):
        from tools import clarify_gateway as cm

        entry = cm.register("ss-oor", "sk-ss", "Pick one", ["A", "B"])
        value, reason = cm._coerce_text_response_detailed(entry, "9")
        assert value is None
        assert reason == "invalid_selection"
        assert cm.attempt_text_response_for_session("sk-ss", "9") == (
            cm.TEXT_REJECTED_SELECTION
        )

    def test_single_select_prose_is_rejected_prose(self):
        from tools import clarify_gateway as cm

        entry = cm.register("ss-prose", "sk-ss2", "Pick one", ["A", "B"])
        value, reason = cm._coerce_text_response_detailed(
            entry, "one more unrelated thought",
        )
        assert value is None
        assert reason == "prose"


# =========================================================================
# P16 — persistent pending clarifies (disk store, restore, resumed dispatch)
# =========================================================================


def _clear_clarify_state_full():
    """Reset module state AND the P16 resume dispatcher between tests."""
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()
    cm.set_resume_dispatcher(None)


class TestClarifyPersistence:
    """P16: write-through persistence + delete lifecycle.

    Persistence is flag-gated (agent.clarify_persist, default OFF). The
    conftest sandboxes HERMES_HOME to a per-test tempdir, so the on-disk
    store lands there. Each test force-enables the flag via monkeypatch so it
    exercises the persisted path deterministically without a config.yaml.
    """

    def setup_method(self):
        _clear_clarify_state_full()

    def _enable(self, monkeypatch):
        from tools import clarify_gateway as cm
        monkeypatch.setattr(cm, "_persist_enabled", lambda: True)
        return cm

    def test_register_writes_through_to_disk(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register(
            "p-reg", "sk-p", "Deploy where?", ["staging", "prod"],
            routing={"platform": "slack", "chat_id": "C1"},
            platform="slack",
        )
        path = cm._persist_path("p-reg")
        assert path.exists()
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["clarify_id"] == "p-reg"
        assert data["session_key"] == "sk-p"
        assert data["question"] == "Deploy where?"
        assert data["choices"] == ["staging", "prod"]
        assert data["routing"] == {"platform": "slack", "chat_id": "C1"}
        assert data["platform"] == "slack"
        assert "created_at" in data

    def test_disabled_flag_writes_nothing(self, monkeypatch):
        from tools import clarify_gateway as cm
        # Default OFF (no monkeypatch): nothing should hit disk.
        cm.register("p-off", "sk-off", "Q?", ["A"])
        assert not cm._persist_path("p-off").exists()
        assert not cm._pending_dir().exists()

    def test_delete_on_resolve(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register("p-res", "sk-r", "Q?", ["A", "B"])
        assert cm._persist_path("p-res").exists()
        assert cm.resolve_gateway_clarify("p-res", "A") is True
        assert not cm._persist_path("p-res").exists()

    def test_delete_on_timeout(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register("p-to", "sk-to", "Q?", None)  # open-ended, no waiter resolves
        assert cm._persist_path("p-to").exists()
        # Short timeout, never resolved → wait_for_response cleans the file up.
        result = cm.wait_for_response("p-to", timeout=0.05)
        assert result is None
        assert not cm._persist_path("p-to").exists()

    def test_delete_on_clear_session(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register("p-c1", "sk-clear", "Q1?", None)
        cm.register("p-c2", "sk-clear", "Q2?", ["A"])
        assert cm._persist_path("p-c1").exists()
        assert cm._persist_path("p-c2").exists()
        cm.clear_session("sk-clear")
        assert not cm._persist_path("p-c1").exists()
        assert not cm._persist_path("p-c2").exists()


class TestClarifyRestore:
    """P16: restore_pending rehydration + resumed-turn dispatch."""

    def setup_method(self):
        _clear_clarify_state_full()

    def _enable(self, monkeypatch):
        from tools import clarify_gateway as cm
        monkeypatch.setattr(cm, "_persist_enabled", lambda: True)
        return cm

    def _simulate_restart(self, cm):
        """Drop in-memory state (as a fresh process would) but keep disk files."""
        with cm._lock:
            cm._entries.clear()
            cm._session_index.clear()

    def test_restore_loads_entries_as_restored(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register(
            "r1", "sk-restore", "Pick region", ["us", "eu"],
            routing={"platform": "slack", "chat_id": "C7"}, platform="slack",
        )
        self._simulate_restart(cm)
        # After "restart" the entry is only on disk.
        assert cm.get_pending_for_session("sk-restore") is None
        n = cm.restore_pending()
        assert n == 1
        entry = cm.get_pending_for_session(
            "sk-restore", include_choice_prompts=True,
        )
        assert entry is not None
        assert entry.restored is True
        assert entry.question == "Pick region"
        assert entry.choices == ["us", "eu"]
        assert entry.routing == {"platform": "slack", "chat_id": "C7"}
        # No waiter thread: the event must be unset.
        assert entry.event.is_set() is False

    def test_restore_then_match_dispatches_resumed_turn(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register(
            "r2", "sk-match", "Deploy where?", ["staging", "prod"],
            routing={"platform": "slack", "chat_id": "C9"}, platform="slack",
        )
        self._simulate_restart(cm)
        cm.restore_pending()

        captured = []
        cm.set_resume_dispatcher(lambda entry, seed: captured.append((entry, seed)))

        outcome = cm.attempt_text_response_for_session("sk-match", "prod")
        assert outcome == cm.TEXT_RESUMED
        assert len(captured) == 1
        dispatched_entry, seed = captured[0]
        assert dispatched_entry.clarify_id == "r2"
        # Seed carries the original question and the coerced answer.
        assert "Deploy where?" in seed
        assert "prod" in seed
        # Entry + file are gone after a successful resume.
        assert cm.get_pending_for_session("sk-match", include_choice_prompts=True) is None
        assert not cm._persist_path("r2").exists()

    def test_restore_numeric_reply_coerces_to_choice_in_seed(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register(
            "r3", "sk-num", "Pick one", ["alpha", "beta", "gamma"],
            routing={"platform": "telegram", "chat_id": "42"},
        )
        self._simulate_restart(cm)
        cm.restore_pending()
        captured = []
        cm.set_resume_dispatcher(lambda entry, seed: captured.append(seed))
        assert cm.attempt_text_response_for_session("sk-num", "2") == cm.TEXT_RESUMED
        assert "beta" in captured[0]

    def test_restore_invalid_selection_keeps_entry_armed(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register("r4", "sk-armed", "Pick one", ["A", "B"])
        self._simulate_restart(cm)
        cm.restore_pending()
        captured = []
        cm.set_resume_dispatcher(lambda entry, seed: captured.append(seed))
        # Out-of-range selection → armed retry, no dispatch, file survives.
        assert cm.attempt_text_response_for_session("sk-armed", "9") == (
            cm.TEXT_REJECTED_SELECTION
        )
        assert captured == []
        assert cm.get_pending_for_session(
            "sk-armed", include_choice_prompts=True,
        ) is not None
        assert cm._persist_path("r4").exists()


class TestClarifyRestoreTripwire:
    """P16 hard tripwire: restore/resume emit ZERO outbound lifecycle chatter.

    Policy indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages
    forbids the fleet agent from telling the user things like "restored",
    "resuming", or "gateway restarted". Restore must be silent (no notify
    callback fired) and the resumed seed must carry no lifecycle banner words.
    """

    _BANNED = ("restored", "restoring", "resuming", "resumed", "gateway", "restart")

    def setup_method(self):
        _clear_clarify_state_full()

    def _enable(self, monkeypatch):
        from tools import clarify_gateway as cm
        monkeypatch.setattr(cm, "_persist_enabled", lambda: True)
        return cm

    def test_restore_fires_no_notify_callback(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register(
            "t1", "sk-silent", "Pick", ["A", "B"],
            routing={"platform": "slack", "chat_id": "C1"},
        )
        with cm._lock:
            cm._entries.clear()
            cm._session_index.clear()

        sent = []
        cm.register_notify("sk-silent", lambda entry: sent.append(entry))
        n = cm.restore_pending()
        assert n == 1
        # restore_pending must NOT push any prompt/banner to the adapter.
        assert sent == []

    def test_resumed_seed_has_no_lifecycle_banner(self, monkeypatch):
        cm = self._enable(monkeypatch)
        cm.register(
            "t2", "sk-seed", "Which environment should I target?",
            ["staging", "prod"],
            routing={"platform": "slack", "chat_id": "C1"},
        )
        with cm._lock:
            cm._entries.clear()
            cm._session_index.clear()
        cm.restore_pending()
        captured = []
        cm.set_resume_dispatcher(lambda entry, seed: captured.append(seed))
        cm.attempt_text_response_for_session("sk-seed", "prod")
        assert len(captured) == 1
        seed_lower = captured[0].lower()
        for word in self._BANNED:
            assert word not in seed_lower, f"seed leaked banner word: {word!r}"

    def test_build_resume_seed_is_clean(self):
        from tools import clarify_gateway as cm
        entry = cm._ClarifyEntry(
            clarify_id="t3", session_key="sk", question="Pick a color?",
            choices=["red", "blue"],
        )
        seed = cm.build_resume_seed(entry, "blue")
        seed_lower = seed.lower()
        for word in self._BANNED:
            assert word not in seed_lower
        assert "blue" in seed
        assert "Pick a color?" in seed
