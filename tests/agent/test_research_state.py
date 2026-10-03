"""Tests for the durable research ledger (P1.3).

The headline guarantee: state persists in SQLite outside the message list, so a
fresh ledger instance (simulating the agent after a context compaction) reads
back everything that was saved.
"""

from agent.research_state import ResearchLedger


def _ledger(tmp_path, session="s1"):
    return ResearchLedger(session, db_path=str(tmp_path / "ledger.db"))


class TestPersistence:
    def test_survives_new_instance(self, tmp_path):
        led = _ledger(tmp_path)
        led.set_brief("I will determine X.")
        led.add_learning("Fact one", source_url="https://a.com/x", date="2026", confidence="high")
        # New instance = the agent after compaction. State must still be there.
        reopened = _ledger(tmp_path)
        state = reopened.get_state()
        assert state["brief"] == "I will determine X."
        assert state["learnings"][0]["claim"] == "Fact one"

    def test_session_isolation(self, tmp_path):
        a = ResearchLedger("sess-a", db_path=str(tmp_path / "l.db"))
        b = ResearchLedger("sess-b", db_path=str(tmp_path / "l.db"))
        a.set_brief("brief A")
        b.set_brief("brief B")
        assert a.get_state()["brief"] == "brief A"
        assert b.get_state()["brief"] == "brief B"


class TestMutations:
    def test_learning_with_source_marks_visited(self, tmp_path):
        led = _ledger(tmp_path)
        led.add_learning("c", source_url="https://a.com/p")
        assert led.is_visited("https://a.com/p")

    def test_visited_dedup_canonical(self, tmp_path):
        led = _ledger(tmp_path)
        assert led.add_visited_url("https://a.com/p") is True
        # Same URL with tracking param + trailing slash -> canonical dup.
        assert led.add_visited_url("https://a.com/p/?utm_source=x") is False
        assert len(led.get_state()["visited_urls"]) == 1

    def test_add_claim(self, tmp_path):
        led = _ledger(tmp_path)
        led.add_claim("The sky is blue", status="supported", evidence_url="https://a.com")
        assert led.get_state()["claims"][0]["status"] == "supported"

    def test_empty_inputs_ignored(self, tmp_path):
        led = _ledger(tmp_path)
        led.add_learning("   ")
        led.add_claim("")
        st = led.get_state()
        assert st["learnings"] == [] and st["claims"] == []

    def test_clear(self, tmp_path):
        led = _ledger(tmp_path)
        led.set_brief("b")
        led.add_visited_url("https://a.com")
        led.clear()
        assert led.get_state() == {"brief": "", "learnings": [], "visited_urls": [], "claims": []}


class TestDigest:
    def test_digest_contains_state(self, tmp_path):
        led = _ledger(tmp_path)
        led.set_brief("I will compare A and B.")
        led.add_learning("A is faster", source_url="https://a.com", confidence="high")
        led.add_visited_url("https://b.com")
        digest = led.render_digest()
        assert "I will compare A and B." in digest
        assert "A is faster" in digest
        assert "https://b.com" in digest
        assert "RESEARCH LEDGER" in digest

    def test_empty_digest_is_safe(self, tmp_path):
        digest = _ledger(tmp_path).render_digest()
        assert "LEARNINGS (0)" in digest


class TestBounds:
    def test_visited_is_bounded(self, tmp_path):
        from agent.research_state import _MAX_VISITED
        led = _ledger(tmp_path)
        for i in range(_MAX_VISITED + 25):
            led.add_visited_url(f"https://a.com/page-{i}")
        assert len(led.get_state()["visited_urls"]) <= _MAX_VISITED


class TestConcurrency:
    def test_parallel_mutations_from_separate_instances_do_not_lose_writes(self, tmp_path):
        """The tool layer builds a FRESH ledger instance per call, so parallel
        tool calls are parallel instances. The read-modify-write must be atomic
        at the DB level or interleaved writers silently drop each other's data."""
        import threading

        n = 16
        errors = []

        def add(i):
            try:
                # New instance per thread — mirrors research_ledger tool dispatch.
                _ledger(tmp_path).add_learning(
                    f"claim {i}", source_url=f"https://a.com/{i}"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        state = _ledger(tmp_path).get_state()
        assert len(state["learnings"]) == n
        assert len(state["visited_urls"]) == n
