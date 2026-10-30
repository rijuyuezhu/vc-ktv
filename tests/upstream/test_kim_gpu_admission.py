"""Optional Kim subprocess admission controls GPU contention, not model semantics."""

import threading

from ktv_app.separation import kim_gpu_slot


def test_single_flight_blocks_second_kim_until_first_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("VC_KTV_KIM_SINGLE_FLIGHT", "1")
    monkeypatch.setenv("VC_KTV_PIPELINE_ROOT", str(tmp_path))
    first_entered, release_first, second_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def first():
        with kim_gpu_slot():
            first_entered.set()
            assert release_first.wait(5)

    def second():
        with kim_gpu_slot():
            second_entered.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    try:
        assert first_entered.wait(2)
        t2.start()
        assert not second_entered.wait(0.1)
        release_first.set()
        assert second_entered.wait(2)
    finally:
        release_first.set()
        t1.join(2)
        if t2.ident is not None:
            t2.join(2)
    assert not t1.is_alive() and not t2.is_alive()
    assert (tmp_path / "kim-gpu.lock").is_file()


def test_kim_parallel_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VC_KTV_KIM_SINGLE_FLIGHT", raising=False)
    monkeypatch.setenv("VC_KTV_PIPELINE_ROOT", str(tmp_path))
    entered = threading.Event()
    with kim_gpu_slot():
        t = threading.Thread(target=lambda: (kim_gpu_slot_entry(), entered.set()))

        # The parallel path is a no-op even while this call is active.
        def kim_gpu_slot_entry():
            with kim_gpu_slot():
                return

        t.start()
        assert entered.wait(2)
        t.join(2)
    assert not (tmp_path / "kim-gpu.lock").exists()
