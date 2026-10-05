import pytest

pytest.importorskip("boto3")   # transport suite: requires the cloud extra

from armature.transport.naming import sweep_job_id, validate_work_names  # noqa: E402


def test_sweep_job_id_deterministic():
    # never-2PC (design §6/§7): the same mission+unit at the same attempt
    # always mints the same id — an overlapping sweep re-drives the same job.
    # The mission is part of the durable address: unit ids are unique only
    # within a mission, so sweep-<unit> alone collides across missions and
    # corrupts the settle/cost metering repair_job reads back.
    assert sweep_job_id("pretzel", "research", 1) == "sweep-pretzel~research~1"
    assert (sweep_job_id("pretzel", "research", 1)
            == sweep_job_id("pretzel", "research", 1))
    assert (sweep_job_id("pretzel", "research", 2)
            != sweep_job_id("pretzel", "research", 1))
    assert (sweep_job_id("m1", "research", 1)
            != sweep_job_id("m2", "research", 1))


def test_sweep_job_id_injective_across_hyphen_splits():
    # Review Important 5: '-' is a legal id char, so mission a-b/unit c and
    # mission a/unit b-c minted the SAME id under the '-delimited grammar —
    # colliding jobs/<id>/ prefixes, silent cross-mission settle/cost
    # corruption. The separators must sit outside the id grammar.
    assert sweep_job_id("a-b", "c", 1) != sweep_job_id("a", "b-c", 1)


def test_validate_work_names_matrix():
    validate_work_names("pretzel", "research")          # plain: ok
    validate_work_names("a-b.c_d", "unit-1.2")          # dots/dashes: ok
    for mission, unit in [("../evil", "u"), ("a/b", "u"), ("", "u"),
                          ("m", "../evil"), ("m", "a/b"), ("m", "with space"),
                          ("m", ""), ("-leading", "u"), ("m", "-leading"),
                          ("m", "research\n"), ("m\n", "u")]:
        try:
            validate_work_names(mission, unit)
        except ValueError:
            continue
        raise AssertionError(f"accepted hostile names: {mission!r}, {unit!r}")