from kg_collector.cli import repository_matches_glob


def test_repository_matches_target_repository_glob() -> None:
    assert repository_matches_glob("delivery-api", "delivery-*")
    assert not repository_matches_glob("inventory-api", "delivery-*")
    assert repository_matches_glob("inventory-api", "")


def test_repository_matches_extended_glob() -> None:
    pattern = "delivery-@(api|web)"

    assert repository_matches_glob("delivery-api", pattern)
    assert repository_matches_glob("delivery-web", pattern)
    assert not repository_matches_glob("delivery-worker", pattern)