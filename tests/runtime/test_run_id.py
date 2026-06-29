from armature.runtime.engine import new_run_id

HEX = set("0123456789abcdef")


def test_new_run_id_is_12_hex_chars():
    rid = new_run_id()
    assert isinstance(rid, str)
    assert len(rid) == 12, f"run_id must be 12 hex chars (48 bits), got {rid!r}"
    assert all(c in HEX for c in rid), f"run_id must be lowercase hex, got {rid!r}"


def test_new_run_id_unique_over_many_calls():
    # 48-bit space: P(collision in 2000 draws) ~ 2000^2 / (2 * 2^48) ~ 7e-9 — never flakes.
    ids = {new_run_id() for _ in range(2000)}
    assert len(ids) == 2000, "run_id must be intrinsically distinct across calls"
