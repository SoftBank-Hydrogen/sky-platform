import pytest

from adapters.database.promotion_copy import PostgresPromotionCopy
from adapters.database.promotion_sync import PostgresPromotionSynchronizer
from tests.contract.test_promotion_copy import databases as databases  # noqa: PLC0414
from tests.contract.test_promotion_copy import seed


def test_final_sync_and_reverse_preserve_new_writes(databases):
    (src, dst), plan = databases
    seed(src)
    baseline = PostgresPromotionCopy(src, dst).copy(plan, guard=lambda: None)
    with src() as db:
        db.execute("INSERT INTO scores VALUES(3,100,'late',NULL)")
    sync = PostgresPromotionSynchronizer(src, dst)
    final = sync.refresh(plan, expected_destination=baseline, guard=lambda: None)
    assert final["tables"][0]["rows"] == 3
    with dst() as db:
        db.execute("INSERT INTO scores VALUES(4,200,'after cutover',NULL)")
    recovered = sync.refresh(plan, expected_destination=final, guard=lambda: None, reverse=True)
    assert recovered["tables"][0]["rows"] == 4
    with src() as a, dst() as b:
        assert (
            a.execute("SELECT * FROM scores ORDER BY id").fetchall()
            == b.execute("SELECT * FROM scores ORDER BY id").fetchall()
        )


def test_unexpected_destination_write_blocks_overwrite(databases):
    (src, dst), plan = databases
    seed(src)
    baseline = PostgresPromotionCopy(src, dst).copy(plan, guard=lambda: None)
    with dst() as db:
        db.execute("UPDATE scores SET score=999 WHERE id=1")
    with pytest.raises(ValueError, match="changed since baseline"):
        PostgresPromotionSynchronizer(src, dst).refresh(
            plan, expected_destination=baseline, guard=lambda: None
        )
    with dst() as db:
        assert db.execute("SELECT score FROM scores WHERE id=1").fetchone() == (999,)


def test_guard_loss_after_truncate_rolls_back(databases):
    (src, dst), plan = databases
    seed(src)
    baseline = PostgresPromotionCopy(src, dst).copy(plan, guard=lambda: None)
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        PostgresPromotionSynchronizer(src, dst).refresh(plan, expected_destination=baseline, guard=guard)
    with dst() as db:
        assert db.execute("SELECT count(*) FROM scores").fetchone() == (2,)
