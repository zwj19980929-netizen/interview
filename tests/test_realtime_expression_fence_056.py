"""Lean reads still revoke a held stream from durable cross-instance changes."""
import asyncio
from copy import deepcopy

import pytest

from app.persistence.provider import persistence_for
from app.repositories.sqlite import SQLiteStore
from test_streaming_expression_lifecycle import _select, _signal, _streaming_harness, _session


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("revoked", ["status", "turn", "floor", "performance", "selection", "skill", "owner"])
def test_held_stream_sees_durable_revocation_without_local_cancel(monkeypatch, tmp_path, backend, revoked):
    async def scenario():
        database_path = str(tmp_path / "two_instances.sqlite3")
        if backend == "sqlite":
            import test_expression_stale_fence
            monkeypatch.setattr(test_expression_stale_fence, "InMemoryStore", lambda: SQLiteStore(database_path))
        h = _streaming_harness(monkeypatch, gate_before_first=True)
        skill_snapshot = None
        if revoked == "skill":
            skills = h.runtime.interviews.skills
            skill = skills.create({"package": {"name": "synthetic", "instructions": "使用简短问题。"}},
                                  actor_id="synthetic_author", organization_id=h.channel.organization_id)
            with h.runtime.persistence.transaction(h.channel.organization_id) as tx:
                skill_snapshot = skills.freeze_snapshot(tx, skill["id"])
                session = tx.interview_sessions.get(h.channel.interview_id)
                session["plan_snapshot"] = {"enterprise_skill_snapshot": skill_snapshot}
                tx.interview_sessions.update(session, expected_version=session["version"])
        try:
            performance = await _select(h)
            item = h.outputs[0]
            task = h.channel._speech_output_task
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await asyncio.wait_for(item.raw.chunk_reached.wait(), 1)
            # SQLite writes come from an independent adapter instance, so an
            # in-process read-model cache cannot satisfy this regression.
            writer = persistence_for(SQLiteStore(database_path)) if backend == "sqlite" else h.runtime.persistence
            with writer.transaction(h.channel.organization_id) as tx:
                if revoked == "owner":
                    owner = tx.evidence_ownerships.get(h.grant.ownership_id)
                    owner["owner_instance_id"] = "replacement_instance"
                    tx.evidence_ownerships.update(owner, expected_version=owner["version"])
                elif revoked == "skill":
                    revision = tx.interview_skill_revisions.get(skill_snapshot["revision_id"])
                    revision["status"] = "revoked"
                    revision["authorization_epoch"] += 1
                    tx.interview_skill_revisions.update(revision, expected_version=revision["version"])
                else:
                    session = tx.interview_sessions.get(h.channel.interview_id)
                    if revoked == "status":
                        session["status"] = "paused"
                    elif revoked == "turn":
                        session["current_turn_id"] = "turn_new"
                    elif revoked == "floor":
                        session["agent_runtime"]["floor"] = "human"
                    elif revoked == "performance":
                        session["agent_runtime"]["active_performance_id"] = "replacement_performance"
                    else:
                        latest = next(event for event in reversed(session["agent_events"])
                                      if event["type"] == "conversation.act.selected")
                        replacement = deepcopy(latest)
                        replacement["event_id"] = "replacement_selection_same_act"
                        session["agent_events"].append(replacement)
                    tx.interview_sessions.update(session, expected_version=session["version"])
            before = _session(h)
            await asyncio.wait_for(task, 1)
            item.raw.read_gate.set()
            assert item.publisher.published == [] and item.archives == []
            assert item.raw.read_cancelled and item.publisher.abort_calls >= 1
            after = _session(h)
            assert after["status"] == before["status"]
            assert after["current_turn_id"] == before["current_turn_id"]
            assert after["agent_runtime"] == before["agent_runtime"]
            assert after["agent_events"] == before["agent_events"]
        finally:
            await h.channel.close("synthetic_test_finished")

    asyncio.run(scenario())
