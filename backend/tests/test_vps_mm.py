from app.vps_mm import _profile_for


def test_exact_vps_profile_requires_expert_symbol_timeframe_magic_and_comment():
    deployment = {
        "ex5_name": "Bot.ex5", "symbol": "US30", "timeframe": "H1",
        "magic": 77, "expected_comment": "US30_77",
    }
    exact = {"expert_name": "Bot.ex5", "symbol": "US30", "timeframe": "H1", "magic": 77, "comment": "US30_77"}
    assert _profile_for(deployment, [exact]) == exact
    assert _profile_for(deployment, [exact, {**exact, "comment": "other"}]) == exact
    assert _profile_for(deployment, [{**exact, "magic": 78}]) is None


def test_ambiguous_exact_profiles_are_rejected():
    deployment = {"ex5_name": "Bot.ex5", "symbol": "US30", "timeframe": "H1", "magic": 77, "expected_comment": "US30_77"}
    profile = {"expert_name": "Bot.ex5", "symbol": "US30", "timeframe": "H1", "magic": 77, "comment": "US30_77"}
    assert _profile_for(deployment, [profile, dict(profile)]) is None
