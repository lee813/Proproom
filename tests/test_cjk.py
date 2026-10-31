from proproom.core.cjk import has_cjk, rewrite


def test_long_run_trigrams():
    assert rewrite("托克维尔").match == '"托克维" OR "克维尔"'


def test_mixed_and_particles():
    q = rewrite("托克维尔的肖像 portrait")
    assert '"托克维"' in q.match and '"尔的肖"' in q.match


def test_short_runs_go_to_like():
    q = rewrite("勒庞 肖像")
    assert q.match is None and q.like == ["勒庞", "肖像"]


def test_latin_only():
    assert rewrite("aerial factory").match is None
    assert not has_cjk("aerial factory") and has_cjk("ポートレート")
