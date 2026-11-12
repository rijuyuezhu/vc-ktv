"""Recover a stuck child process without timing out healthy long songs."""

import subprocess
import threading
import time
from unittest.mock import patch

from vc_ktv.core.store import Store
from vc_ktv.infra.process_cancel import thread_external_activity
from vc_ktv.processing.worker import AutoPrep


def seed(store, song_id=1, *, state="downloading", stage="download"):
    with store.lock:
        store.db.execute("INSERT OR IGNORE INTO guests(id,name) VALUES('g','Guest')")
        store.db.execute(
            """INSERT INTO songs(id,bvid,page,title,uploader,cover,duration,
               requester,requested_at,status,prep_state,prep_stage)
               VALUES(?, 'BV0000000001',1,'Test','Up','',20,'g',?,'waiting',?,?)""",
            (song_id, song_id, state, stage),
        )
        store.db.commit()


def watch_fixture(tmp_path):
    store = Store(tmp_path / "room")
    seed(store)
    worker = AutoPrep(store, lambda _: 20)
    worker.active_tasks[1] = (12345, threading.Event())
    return store, worker


def test_no_progress_subprocess_gets_killed_only_after_idle_threshold(tmp_path):
    store, worker = watch_fixture(tmp_path)
    sample = ((10, 100, 7, 111),)
    killed = []
    with (
        patch("vc_ktv.processing.worker.thread_external_activity", return_value=sample),
        patch(
            "vc_ktv.processing.worker.terminate_thread_children",
            side_effect=lambda tid: killed.append(tid),
        ),
    ):
        worker.watch_external_idle(now=0, idle_seconds=300)
        worker.watch_external_idle(now=299, idle_seconds=300)
        assert killed == []
        worker.watch_external_idle(now=301, idle_seconds=300)
        assert killed == [12345]
        worker.watch_external_idle(now=302, idle_seconds=300)
        assert killed == [12345], "avoid repeating the same kill immediately"
    assert not worker.active_tasks[1][1].is_set(), (
        "worker must commit its normal retry state"
    )
    store.db.close()


def test_slow_but_progressing_external_process_never_gets_a_total_duration_cap(
    tmp_path,
):
    store, worker = watch_fixture(tmp_path)
    killed = []
    samples = [((10, 100, i, 1000 + i * 100),) for i in range(20)]
    with (
        patch("vc_ktv.processing.worker.thread_external_activity", side_effect=samples),
        patch(
            "vc_ktv.processing.worker.terminate_thread_children",
            side_effect=lambda tid: killed.append(tid),
        ),
    ):
        for i in range(20):
            worker.watch_external_idle(now=i * 270, idle_seconds=300)
    assert killed == []  # Overall processing elapsed > 85 minutes.
    store.db.close()


def test_alignment_without_child_progress_and_unknown_proc_are_never_killed(tmp_path):
    store, worker = watch_fixture(tmp_path)
    killed = []
    with (
        patch(
            "vc_ktv.processing.worker.thread_external_activity",
            return_value=((10, 100, 0, 0),),
        ),
        patch(
            "vc_ktv.processing.worker.terminate_thread_children",
            side_effect=lambda tid: killed.append(tid),
        ),
    ):
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='ready',prep_state='aligning',prep_stage='align'"
            )
            store.db.commit()
        worker.watch_external_idle(now=0, idle_seconds=300)
        worker.watch_external_idle(now=10000, idle_seconds=300)
        assert not killed  # GPU alignment can be externally quiet.
        with store.lock:
            store.db.execute("UPDATE songs SET prep_stage='render'")
            store.db.commit()
        worker.watch_external_idle(now=10100, idle_seconds=300)
        worker.watch_external_idle(now=10180, idle_seconds=300)
        assert not killed
    with patch("vc_ktv.processing.worker.thread_external_activity", return_value=None):
        worker.watch_external_idle(now=20000, idle_seconds=300)
    assert not killed
    store.db.close()


def test_linux_real_sleep_subprocess_is_terminated_and_worker_retry_recovers(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.execute(
            """INSERT INTO songs(id,bvid,page,title,uploader,cover,duration,
               requester,requested_at,status,prep_state)
               VALUES(1,'BV0000000001',1,'Test','Up','',20,'g',1,'waiting','pending')"""
        )
        store.db.commit()
    ready = threading.Event()
    children = []

    def stuck_download(*_args):
        proc = subprocess.Popen(["sleep", "40"])
        children.append(proc)
        ready.set()
        proc.wait()
        raise RuntimeError("external download stalled")

    worker = AutoPrep(store, lambda _: 20, download=stuck_download)
    worker.start()
    try:
        assert ready.wait(5)
        for _ in range(60):
            with worker.active_tasks_lock:
                job = worker.active_tasks.get(1)
            if job and thread_external_activity(job[0]):
                break
            time.sleep(0.03)
        else:
            raise AssertionError("no observed external subprocess")
        # A newly launched child can have a brief startup CPU tick or file I/O.
        # Establish the idle baseline only after two identical /proc snapshots.
        for _ in range(40):
            first = thread_external_activity(job[0])
            time.sleep(0.03)
            second = thread_external_activity(job[0])
            if first and first == second:
                break
        else:
            raise AssertionError("child did not stabilize to an idle state")
        worker.watch_external_idle(now=0, idle_seconds=300)
        # Simulate 301 seconds with no external I/O; do not actually wait 5min.
        worker.watch_external_idle(now=301, idle_seconds=300)
        for _ in range(200):
            if worker.active_claims == 0:
                break
            time.sleep(0.02)
        assert worker.active_claims == 0
        assert children[0].poll() == -9
        with store.lock:
            row = store.db.execute(
                "SELECT prep_state,prep_attempts,prep_retry_at,status FROM songs WHERE id=1"
            ).fetchone()
        assert row["prep_state"] == "unavailable"
        assert row["status"] == "waiting"
        assert row["prep_attempts"] == 1
        assert row["prep_retry_at"] > time.time()
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
        worker.stop()
        store.db.close()


def test_only_stalled_song_is_terminated_when_parallel_worker_makes_progress(tmp_path):
    store, worker = watch_fixture(tmp_path)
    seed(store, song_id=2)
    worker.active_tasks[2] = (12346, threading.Event())
    killed = []
    progress = {
        12345: ((71, 333, 0, 100),),
        12346: ((72, 444, 0, 100),),
    }
    with (
        patch(
            "vc_ktv.processing.worker.thread_external_activity",
            side_effect=lambda tid: progress[tid],
        ),
        patch(
            "vc_ktv.processing.worker.terminate_thread_children",
            side_effect=lambda tid: killed.append(tid),
        ),
    ):
        worker.watch_external_idle(now=0, idle_seconds=300)
        progress[12346] = ((72, 444, 5, 150),)
        worker.watch_external_idle(now=299, idle_seconds=300)
        progress[12346] = ((72, 444, 11, 350),)
        worker.watch_external_idle(now=301, idle_seconds=300)
    assert killed == [12345]
    assert not worker.active_tasks[2][1].is_set()
    store.db.close()


def test_cancel_or_replaced_claim_does_not_kill_stale_external_tree(tmp_path):
    store, worker = watch_fixture(tmp_path)
    killed = []
    with (
        patch(
            "vc_ktv.processing.worker.thread_external_activity",
            return_value=((10, 100, 0, 100),),
        ),
        patch(
            "vc_ktv.processing.worker.terminate_thread_children",
            side_effect=lambda tid: killed.append(tid),
        ),
    ):
        worker.watch_external_idle(now=0, idle_seconds=300)
        worker.active_tasks[1][1].set()
        worker.watch_external_idle(now=400, idle_seconds=300)
        assert not killed
        worker.active_tasks[1] = (12347, threading.Event())
        worker.watch_external_idle(now=700, idle_seconds=300)
        worker.watch_external_idle(now=750, idle_seconds=300)
        assert not killed
    store.db.close()
