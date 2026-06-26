from armature.loop.carry import resolve_carry


def test_carry_none_result_returns_empty():
    assert resolve_carry(None, "*") == {}
    assert resolve_carry({}, "*") == {}


def test_carry_star_returns_whole_dict_copy():
    result = {"s": {"x": 1}, "t": {"y": 2}}
    carried = resolve_carry(result, "*")
    assert carried == {"s": {"x": 1}, "t": {"y": 2}}
    # mutating carried must not mutate the source
    carried["s"]["x"] = 99
    assert result["s"]["x"] == 1


def test_carry_dot_paths_select_named_keys():
    result = {"s": {"x": 1, "y": 2}, "t": {"y": 3}}
    carried = resolve_carry(result, "s.x,t.y")
    assert carried == {"s": {"x": 1}, "t": {"y": 3}}


def test_carry_missing_path_skipped():
    result = {"s": {"x": 1}}
    assert resolve_carry(result, "s.missing,s.x") == {"s": {"x": 1}}


def test_carry_whitespace_and_empty_segments_tolerated():
    result = {"s": {"x": 1}}
    assert resolve_carry(result, "  s.x , , ") == {"s": {"x": 1}}
