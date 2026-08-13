"""Tests for perpetual agents: CronSchedule kind='self' + agent_sleep.

RFC rev 3 Phase 1 floor items 1/4/5: the code-owned §7 contract preamble with
the ranking step, agent_sleep's wake-sooner half, and the §9 inheritance
decisions. Each §9 row pinned here is a path that would otherwise silently
kill or distort an agent nobody is watching.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron import (
    _AUTO_PAUSE_THRESHOLD,
    _AUTO_PAUSE_THRESHOLD_SELF,
    _MIN_INTERVAL_SECS,
    _SELF_CONTRACT_PREAMBLE,
    CronJob,
    CronSchedule,
    CronService,
    build_cron_session_context,
    compute_next_run_ts,
    format_schedule,
)


def _svc(tmp_path: Path) -> CronService:
    svc = CronService(base_dir=tmp_path)
    svc._load()
    return svc


def _add_self(svc: CronService, name: str = "warden", every: int = 3600) -> CronJob:
    return svc.add_job(name=name, message="pursue the goal", every_secs=every, perpetual=True)


class TestSelfScheduleCreation:
    def test_perpetual_creates_kind_self(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        assert job.schedule.kind == "self"
        assert job.schedule.every_secs == 3600
        assert job.next_wake_ts is None

    def test_perpetual_requires_every(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="every_secs"):
            _svc(tmp_path).add_job(name="w", message="m", at_ts=time.time() + 60, perpetual=True)

    def test_perpetual_refuses_delete_after_run(self, tmp_path: Path) -> None:
        """§9: one-shot semantics are refused at validation."""
        with pytest.raises(ValueError, match="delete_after_run"):
            _svc(tmp_path).add_job(
                name="w", message="m", every_secs=3600, perpetual=True, delete_after_run=True
            )

    def test_perpetual_forces_strict_schedule_and_persistence(self, tmp_path: Path) -> None:
        """§9: jitter off (strict_schedule), continuity on (persistent_session)."""
        job = _svc(tmp_path).add_job(
            name="w", message="m", every_secs=3600, perpetual=True,
            strict_schedule=False, persistent_session=False,
        )
        assert job.strict_schedule is True
        assert job.persistent_session is True

    def test_round_trips_through_store(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did a thing", "next thing")
        svc2 = _svc(tmp_path)
        got = svc2.list_jobs()[0]
        assert got.schedule.kind == "self"
        assert got.next_wake_ts is not None
        assert "did: did a thing" in got.last_sleep_record

    def test_format_schedule(self) -> None:
        assert "self-scheduled" in format_schedule(CronSchedule(kind="self", every_secs=3600))


class TestSelfNextRun:
    def test_fallback_is_operator_ceiling(self, tmp_path: Path) -> None:
        """No agent choice -> behaves like 'every' (the ceiling)."""
        job = _add_self(_svc(tmp_path))
        job.last_run_ts = 1000.0
        assert compute_next_run_ts(job, now=1100.0) == 1000.0 + 3600

    def test_agent_choice_wins(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        job.last_run_ts = 1000.0
        job.next_wake_ts = 1600.0
        assert compute_next_run_ts(job, now=1100.0) == 1600.0

    def test_missed_wake_fires_on_recovery(self, tmp_path: Path) -> None:
        """A deadline that passed while the host was down fires NOW, not one
        interval later — the property that made cron the host."""
        job = _add_self(_svc(tmp_path))
        job.next_wake_ts = 1000.0
        assert compute_next_run_ts(job, now=5000.0) == 5000.0


class TestAgentSleep:
    def test_records_wake_and_result(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        t0 = time.time()
        got = svc.record_agent_sleep(job.id, 600, "fixed the flake", "verify at population level")
        assert got.next_wake_ts is not None
        assert t0 + 595 <= got.next_wake_ts <= t0 + 605
        assert "did: fixed the flake" in got.last_sleep_record
        assert "next: verify at population level" in got.last_sleep_record

    def test_wake_sooner_allowed_sleep_longer_clamped(self, tmp_path: Path) -> None:
        """The wake-sooner half: earlier than the ceiling is allowed, past it
        is clamped TO the ceiling (RFC rev 3: sleep-longer is §4 config, not
        agent-chosen distant deadlines)."""
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        t0 = time.time()
        got = svc.record_agent_sleep(job.id, 86400, "idle", "")
        assert got.next_wake_ts is not None
        assert got.next_wake_ts <= t0 + 3600 + 5

    def test_floor_clamped(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        t0 = time.time()
        got = svc.record_agent_sleep(job.id, 1, "quick continue", "")
        assert got.next_wake_ts is not None
        assert got.next_wake_ts >= t0 + _MIN_INTERVAL_SECS - 5

    def test_rejected_for_non_self_jobs(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="kind='self'"):
            svc.record_agent_sleep(job.id, 600, "did", "")

    def test_unknown_job(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            _svc(tmp_path).record_agent_sleep("deadbeef", 600, "did", "")


class TestConsumeOnFire:
    def test_consume_clears_matching_deadline(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        target = svc.list_jobs()[0]
        assert target.next_wake_ts is not None
        svc._consume_self_wake_locked(target)
        assert target.next_wake_ts is None
        svc2 = _svc(tmp_path)
        assert svc2.list_jobs()[0].next_wake_ts is None

    def test_consume_preserves_newer_choice(self, tmp_path: Path) -> None:
        """An agent_sleep landing during the run must survive the clear."""
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        fired = svc.list_jobs()[0]
        stale = CronJob(id=fired.id, name=fired.name, message=fired.message,
                        schedule=fired.schedule)
        stale.next_wake_ts = (fired.next_wake_ts or 0) - 100  # a DIFFERENT value
        svc._consume_self_wake_locked(stale)
        # Disk value differed from what this fire consumed -> preserved.
        assert svc.list_jobs()[0].next_wake_ts is not None


class TestSelfPromptAssembly:
    def test_contract_preamble_prepended(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            key, prompt = build_cron_session_context(job)
        assert key == f"cron:{job.id}"
        assert prompt.startswith("[Perpetual agent contract]")
        assert "RANK FIRST" in prompt
        assert prompt.rstrip().endswith("pursue the goal")

    def test_life_and_journal_included_when_present(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("## 1. The goal\nKeep CI trustworthy.\n", encoding="utf-8")
        (base / "JOURNAL.md").write_text(
            "\n".join(f"line {i}" for i in range(50)) + "\n", encoding="utf-8"
        )
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "Keep CI trustworthy." in prompt
        assert "line 49" in prompt
        assert "line 0" not in prompt  # tail only

    def test_missing_life_dir_degrades_gracefully(self, tmp_path: Path) -> None:
        job = _add_self(_svc(tmp_path))
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "nope"):
            _, prompt = build_cron_session_context(job)
        assert "[Perpetual agent contract]" in prompt

    def test_life_md_capped(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("x" * 50_000, encoding="utf-8")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "[truncated at cap]" in prompt
        assert len(prompt) < 40_000

    def test_no_idle_punishment_language(self) -> None:
        """§7: the preamble must never claim idle will be refused/punished —
        that instruction is what produces invented work."""
        low = _SELF_CONTRACT_PREAMBLE.lower()
        assert "honest idle is a legitimate outcome" in low
        for banned in ("will be refused", "punish", "must produce"):
            assert banned not in low.replace("never punished", "")

    def test_plain_jobs_unchanged(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="hello", every_secs=3600)
        _, prompt = build_cron_session_context(job)
        assert "[Perpetual agent contract]" not in prompt


class TestSelfAutoPause:
    def test_higher_threshold_for_self(self, tmp_path: Path) -> None:
        """§9: a self job survives the ordinary threshold and pauses only at
        the raised one — it must never die quietly on an ordinary bad day."""
        job = _add_self(_svc(tmp_path))
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is False  # survived the ordinary threshold
        for _ in range(_AUTO_PAUSE_THRESHOLD_SELF - _AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True

    def test_plain_jobs_keep_ordinary_threshold(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = svc.add_job(name="plain", message="m", every_secs=3600)
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True


class TestSelfIsDue:
    """GPT round-1 F5: _is_due must support kind='self' or nothing ever fires."""

    def test_due_when_agent_deadline_passed(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        job.next_wake_ts = 1000.0
        assert CronService._is_due(job, now=1001.0) is True
        assert CronService._is_due(job, now=999.0) is False

    def test_fallback_ceiling_when_no_choice(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        job.last_run_ts = 1000.0
        assert CronService._is_due(job, now=1000.0 + 3599) is False
        assert CronService._is_due(job, now=1000.0 + 3601) is True


class TestLifeContextSafety:
    """GPT round-1 F1: the job name must not escape the agents directory."""

    @pytest.mark.parametrize("bad", ["/tmp/private", "../outside", "a/../../b"])
    def test_hostile_names_cannot_select_files(self, tmp_path: Path, bad: str) -> None:
        """GPT round-4: the life dir is keyed by generated job.id, so a name
        — hostile or colliding with an existing agent — selects nothing."""
        svc = _svc(tmp_path)
        job = svc.add_job(name=bad, message="m", every_secs=3600, perpetual=True)
        outside = tmp_path / "private"
        outside.mkdir()
        (outside / "LIFE.md").write_text("SECRET", encoding="utf-8")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "SECRET" not in prompt
        assert "[Perpetual agent contract]" in prompt

    def test_name_collision_cannot_steal_another_agents_life(self, tmp_path: Path) -> None:
        """A job named after an existing agent must NOT read that agent's
        LIFE.md — directories are keyed by id, not name."""
        svc = _svc(tmp_path)
        victim = _add_self(svc, name="warden")
        base = tmp_path / "agents" / victim.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("VICTIM GOAL", encoding="utf-8")
        impostor = _add_self(svc, name="warden")  # same NAME, different id
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(impostor)
        assert "VICTIM GOAL" not in prompt

    def test_reads_are_byte_bounded(self, tmp_path: Path) -> None:
        """A huge journal must not be read whole — only the tail cap."""
        from kiro_crew.cron import _JOURNAL_TAIL_CAP_BYTES, _read_tail_bytes

        big = tmp_path / "JOURNAL.md"
        big.write_text("x" * 5_000_000 + "\nlast line", encoding="utf-8")
        tail = _read_tail_bytes(big, _JOURNAL_TAIL_CAP_BYTES, tmp_path)
        assert tail is not None
        assert len(tail.encode("utf-8")) <= _JOURNAL_TAIL_CAP_BYTES
        assert tail.endswith("last line")


class TestSleepRecordSurvivesTurnMerge:
    """GPT round-1 F4: the sleep record must not be clobbered by the
    turn-completion merge (last_result belongs to the turn)."""

    def test_merge_preserves_sleep_record(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "the real record", "next step")
        # Simulate the gateway's turn-completion merge with a stale in-memory
        # snapshot whose last_result is the turn's own text.
        snapshot = svc.list_jobs()[0]
        snapshot.set_run_result("turn output text")
        svc._merge_job_result(snapshot)
        svc2 = _svc(tmp_path)
        got = svc2.list_jobs()[0]
        assert "the real record" in got.last_sleep_record
        assert got.last_result == "turn output text"

    def test_sleep_record_reaches_next_prompt(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did the thing", "verify it")
        target = svc.list_jobs()[0]
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(target)
        assert "did: did the thing" in prompt
        assert "next: verify it" in prompt


class TestLifeContextSymlinkGuard:
    """GPT round-2: a symlinked LIFE.md must never pull outside bytes into
    the prompt — O_NOFOLLOW at the open plus a realpath containment check."""

    def test_symlinked_life_md_refused(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY BYTES", encoding="utf-8")
        base = tmp_path / "agents" / "warden"
        base.mkdir(parents=True)
        (base / "LIFE.md").symlink_to(secret)
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "PRIVATE KEY BYTES" not in prompt
        assert "[Perpetual agent contract]" in prompt

    def test_symlinked_parent_dir_refused(self, tmp_path: Path) -> None:
        """A symlinked intermediate directory is caught by the realpath
        containment re-check (O_NOFOLLOW only guards the final component)."""
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "LIFE.md").write_text("OUTSIDE BYTES", encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / job.id).symlink_to(outside, target_is_directory=True)
        with patch("kiro_crew.cron._agents_dir", return_value=agents):
            _, prompt = build_cron_session_context(job)
        assert "OUTSIDE BYTES" not in prompt

    def test_regular_files_still_read(self, tmp_path: Path) -> None:
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        (base / "LIFE.md").write_text("normal goal text", encoding="utf-8")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)
        assert "normal goal text" in prompt


class TestRound3Fixes:
    """GPT round-3: FIFO refusal, contention propagation, last_run advance."""

    def test_fifo_life_md_refused(self, tmp_path: Path) -> None:
        """A FIFO at LIFE.md must not hang the open — refused via O_NONBLOCK
        + regular-file check."""
        import os as _os
        import sys

        if sys.platform == "win32":
            pytest.skip("mkfifo is POSIX-only")
        svc = _svc(tmp_path)
        job = _add_self(svc, name="warden")
        base = tmp_path / "agents" / job.id
        base.mkdir(parents=True)
        _os.mkfifo(base / "LIFE.md")
        with patch("kiro_crew.cron._agents_dir", return_value=tmp_path / "agents"):
            _, prompt = build_cron_session_context(job)  # must return, not hang
        assert "[Perpetual agent contract]" in prompt
        assert "[LIFE.md" not in prompt.replace("[LIFE.md truncated", "")

    def test_consume_propagates_store_busy(self, tmp_path: Path) -> None:
        """Contention must propagate — an in-memory-only clear would leave the
        past-due deadline live on disk and refire every tick."""
        from kiro_crew.cron import CronStoreBusy

        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        target = svc.list_jobs()[0]
        fired_value = target.next_wake_ts

        class _Busy:
            def __enter__(self):
                raise CronStoreBusy("contended")

            def __exit__(self, *a):
                return False

        with patch.object(svc, "_file_lock", return_value=_Busy()):
            with pytest.raises(CronStoreBusy):
                svc._consume_self_wake_locked(target)
        # In-memory value untouched on the failure path too.
        assert target.next_wake_ts == fired_value

    def test_self_last_run_advances_like_every(self, tmp_path: Path) -> None:
        """A completed self wake must advance last_run_ts so the fallback
        deadline moves forward even when agent_sleep was never called."""
        import asyncio as _aio

        svc = _svc(tmp_path)
        job = _add_self(svc, every=3600)
        job.last_run_ts = None

        async def _noop(_j: CronJob) -> None:
            return None

        svc._on_job = _noop
        svc._job_run_meta[job.id] = (1234.5, "scheduled")
        svc._executing.add(job.id)
        _aio.run(svc._run_job_isolated(job))
        assert job.last_run_ts == 1234.5


class TestSkippedWakeNotFinalized:
    """GPT round-4: a wake skipped on consumption contention must not be
    finalized — no last_run_ts advance, no phantom history row."""

    def test_contention_skip_leaves_run_state_untouched(self, tmp_path: Path) -> None:
        import asyncio as _aio

        from kiro_crew.cron import CronStoreBusy

        svc = _svc(tmp_path)
        job = _add_self(svc)
        svc.record_agent_sleep(job.id, 600, "did", "")
        target = svc.list_jobs()[0]
        target.last_run_ts = None
        ran = []

        async def _mark(_j: CronJob) -> None:
            ran.append(True)

        svc._on_job = _mark
        svc._job_run_meta[target.id] = (999.0, "scheduled")
        svc._executing.add(target.id)
        with patch.object(
            svc, "_consume_self_wake_locked", side_effect=CronStoreBusy("busy")
        ):
            _aio.run(svc._run_job_isolated(target))
        assert ran == []  # never executed
        assert target.last_run_ts is None  # not finalized as a run


class TestLifeMdWriteDeny:
    """GPT round-5: LIFE.md is agent-read-only — both tool gates hard-deny
    writes so a perpetual agent cannot self-modify its goal."""

    def test_edit_gate_denies_agents_life_md(self, tmp_path: Path) -> None:
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_write_path

        home = _P.home()
        life = home / ".kiro" / "crew" / "agents" / "abcd1234" / "LIFE.md"
        assert is_sensitive_write_path(str(life)) is True

    def test_edit_gate_allows_journal_md(self) -> None:
        from pathlib import Path as _P

        from kiro_crew.security import is_sensitive_write_path

        home = _P.home()
        journal = home / ".kiro" / "crew" / "agents" / "abcd1234" / "JOURNAL.md"
        assert is_sensitive_write_path(str(journal)) is False

    def test_bash_gate_catches_life_md_write(self) -> None:
        from kiro_crew.security import _build_sensitive_regex

        rx = _build_sensitive_regex()
        assert rx.search('echo hacked > ~/.kiro/crew/agents/ab12cd34/LIFE.md')
        assert rx.search("tee $HOME/.kiro/crew/agents/x/LIFE.md")
        # JOURNAL.md writes stay allowed.
        assert not rx.search("echo entry >> ~/.kiro/crew/agents/ab12cd34/JOURNAL.md")

    # ── GPT round-6 ──────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # single-dot segment right before the leaf (the reported bypass)
            "echo hacked > ~/.kiro/crew/agents/ab12cd34/./LIFE.md",
            # dot segment inside the crew prefix
            "echo hacked > ~/.kiro/./crew/agents/ab12cd34/LIFE.md",
            # same-level down-up excursion re-entering the id segment
            "echo hacked > ~/.kiro/crew/agents/x/../x/LIFE.md",
            # excursion through the agents dir itself
            "tee $HOME/.kiro/crew/agents/./ab12cd34/LIFE.md",
        ],
    )
    def test_bash_gate_catches_dot_segment_spellings(self, cmd: str) -> None:
        from kiro_crew.security import _build_sensitive_regex

        assert _build_sensitive_regex().search(cmd), cmd

    def test_bash_gate_dot_segments_leave_journal_alone(self) -> None:
        from kiro_crew.security import _build_sensitive_regex

        rx = _build_sensitive_regex()
        assert not rx.search("echo e >> ~/.kiro/crew/agents/ab12cd34/./JOURNAL.md")

    def test_edit_gate_denies_life_md_under_kirocrew_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import is_sensitive_write_path

        crew = tmp_path / "crew-home"
        life = crew / "agents" / "ab12cd34" / "LIFE.md"
        life.parent.mkdir(parents=True)
        life.write_text("goal", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        assert is_sensitive_write_path(str(life)) is True
        # JOURNAL.md in the same env-anchored dir stays writable.
        journal = life.parent / "JOURNAL.md"
        assert is_sensitive_write_path(str(journal)) is False

    def test_bash_gate_catches_life_md_under_kirocrew_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import _build_sensitive_regex

        crew = tmp_path / "crew-home"
        (crew / "agents" / "ab12cd34").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        rx = _build_sensitive_regex()
        assert rx.search(f"echo hacked > {crew}/agents/ab12cd34/LIFE.md")
        assert rx.search(f"echo hacked > {crew}/agents/ab12cd34/./LIFE.md")
        assert not rx.search(f"echo e >> {crew}/agents/ab12cd34/JOURNAL.md")

    def test_full_bash_gate_denies_env_anchored_write_via_normalizer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live gate (regex pass may be process-cached from before the env
        was set) still denies via the normalizer second-pass, because
        ``_is_agent_life_md`` consults ``KIROCREW_HOME`` at call time."""
        from kiro_crew.security import is_sensitive_write_path

        crew = tmp_path / "crew-home"
        life = crew / "agents" / "ab12cd34" / "LIFE.md"
        life.parent.mkdir(parents=True)
        life.write_text("goal", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        # dot-segment spelling resolves to the same guarded file
        dotted = crew / "agents" / "ab12cd34" / "." / "LIFE.md"
        assert is_sensitive_write_path(str(dotted)) is True

    # ── GPT round-7 ──────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "cmd",
        [
            # the reported bypass: cd into the agents dir, write relatively
            "cd ~/.kiro/crew/agents/ab12cd34 && echo hacked > LIFE.md",
            # other chained-relative verbs
            "cd $HOME/.kiro/crew/agents/x; cp /tmp/evil LIFE.md",
            "cd ~/.kirocrew/agents/ab12cd34 && tee LIFE.md < /tmp/evil",
            # dot-segment spelling of the cd target
            "cd ~/.kiro/crew/agents/./ab12cd34 && echo hacked > LIFE.md",
        ],
    )
    def test_bash_gate_catches_chained_relative_write(self, cmd: str) -> None:
        from kiro_crew.security import _build_sensitive_regex

        assert _build_sensitive_regex().search(cmd), cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            r"cmd /c echo hacked > C:\Users\u\.kiro\crew\agents\ab12\LIFE.md",
            r"Set-Content $env:USERPROFILE\.kirocrew\agents\x\LIFE.md evil",
            r"echo hacked > %USERPROFILE%\.kiro\crew\agents\ab12\LIFE.md",
            # native cd + relative write chain
            r"cd C:\Users\u\.kiro\crew\agents\ab12 && echo hacked > LIFE.md",
        ],
    )
    def test_bash_gate_catches_windows_native_spellings(self, cmd: str) -> None:
        from kiro_crew.security import _build_sensitive_regex

        assert _build_sensitive_regex().search(cmd), cmd

    def test_chained_journal_work_stays_allowed(self) -> None:
        from kiro_crew.security import _build_sensitive_regex

        rx = _build_sensitive_regex()
        assert not rx.search(
            "cd ~/.kiro/crew/agents/ab12cd34 && echo entry >> JOURNAL.md"
        )

    def test_env_anchored_chain_caught_when_regex_built_with_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import _build_sensitive_regex

        crew = tmp_path / "crew-home"
        crew.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        rx = _build_sensitive_regex()
        assert rx.search(f"cd {crew}/agents/ab12cd34 && echo hacked > LIFE.md")


class TestPerpetualCreationAllowlist:
    """GPT round-5: perpetual creation is a POSITIVE operator allowlist —
    empty and automation identities are refused, not just cron:."""

    @pytest.mark.parametrize(
        "caller,allowed",
        [
            ("dashboard:abc123", True),
            ("slack:C1:169.1", True),
            # GPT round-7: every human messaging channel is an operator
            # surface, via the shared is_channel_session_key predicate.
            ("webex:room1", True),
            ("wecom:u1", True),
            ("teams:conv1", True),
            ("weixin:u1", True),
            ("whatsapp:u1", True),
            ("unified:kirocrew:dm:u1", True),
            ("discord:guild1:chan1", True),
            ("telegram:chat1", True),
            # legacy un-namespaced Slack thread_ts
            ("1785370133.085469", True),
            # automation identities stay refused
            ("cron:ab12cd34", False),
            ("subagent:xyz", False),
            ("webhook:h1", False),
            ("heartbeat:h1", False),
            ("taskrunner:t1", False),
            ("", False),
        ],
    )
    def test_caller_gating(self, tmp_path: Path, caller: str, allowed: bool) -> None:
        import kiro_crew.mcp_cron as mc

        svc = _svc(tmp_path)
        args = {
            "name": "w",
            "message": "goal",
            "every": 3600,
            "perpetual": True,
        }
        with (
            patch.object(mc, "_resolve_session_key_strict", return_value=caller),
            patch.object(mc, "_resolve_session_key", return_value=caller or "x"),
            patch.object(mc, "get_service", return_value=svc, create=True),
        ):
            # Route through the real handler if its service accessor matches;
            # otherwise call the inner tool with the svc patched in place.
            with patch.object(mc, "svc", svc, create=True):
                out = mc._call_tool_inner("cron_add", dict(args))
        if allowed:
            assert "Added job" in out, out
        else:
            assert "operator session" in out, out
