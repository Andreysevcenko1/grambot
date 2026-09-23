from grambot.processing.clustering import find_matching_cluster, new_cluster_id, similarity


def test_similarity_high_for_near_duplicate_titles():
    a = "TON Foundation announces network upgrade"
    b = "TON Foundation announced a network upgrade today"
    assert similarity(a, b) > 0.6


def test_similarity_low_for_unrelated_titles():
    a = "TON Foundation announces network upgrade"
    b = "Bitcoin price drops after regulatory news"
    assert similarity(a, b) < 0.4


def test_find_matching_cluster_returns_best_match():
    candidates = [
        ("cluster-1", "TON network exploit drains funds"),
        ("cluster-2", "New exchange lists TON token"),
    ]
    match = find_matching_cluster("TON network exploit drains user funds", candidates)
    assert match == "cluster-1"


def test_find_matching_cluster_returns_none_when_no_match():
    candidates = [("cluster-1", "Completely unrelated headline about weather")]
    assert find_matching_cluster("TON network exploit", candidates) is None


def test_new_cluster_id_is_deterministic():
    assert new_cluster_id("Some Title", "bucket-1") == new_cluster_id("Some Title", "bucket-1")
    assert new_cluster_id("Some Title", "bucket-1") != new_cluster_id("Some Title", "bucket-2")
