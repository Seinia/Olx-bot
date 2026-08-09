import pytest

from olx import categories
from olx.errors import ConfigError


def test_tree_is_loaded():
    assert len(categories._tree()) > 2000


def test_missing_reference_file_raises_config_error_not_file_not_found_error(monkeypatch):
    # /list и /export зовут label() на каждый вызов -- при отсутствующем справочнике
    # (например, после git clone без data/reference/) владелец должен увидеть понятную
    # причину из нашей таксономии, а не голый FileNotFoundError из pathlib.
    categories._tree.cache_clear()
    monkeypatch.setattr(categories, "TREE_PATH", categories.TREE_PATH.with_name("missing.json"))
    try:
        with pytest.raises(ConfigError, match="Справочник категорий не найден"):
            categories._tree()
    finally:
        categories._tree.cache_clear()


def test_known_leaves_resolve_to_real_names():
    assert categories.name(1758) == "Продаж квартир"
    assert categories.name(1760) == "Довгострокова оренда квартир"
    assert categories.name(85) == "Смартфони / мобільні телефони"


def test_path_goes_from_root_to_leaf():
    assert categories.path(1760) == [
        "Нерухомість",
        "Квартири",
        "Довгострокова оренда квартир",
    ]


def test_label_keeps_the_distinguishing_tail():
    # Продажа и аренда различаются только последним звеном -- если label его срежет,
    # кнопки в боте станут неотличимы друг от друга.
    assert categories.label(1758) != categories.label(1760)
    assert "Продаж квартир" in categories.label(1758)
    assert "оренда квартир" in categories.label(1760)


def test_unknown_id_degrades_gracefully():
    assert categories.name(99_999_999) is None
    assert categories.path(99_999_999) == []
    assert "99999999" in categories.label(99_999_999)


def test_roots_and_children():
    roots = categories.children(None)
    assert any(r["name"] == "Нерухомість" for r in roots)

    kids = {c["name"] for c in categories.children(1757)}
    assert {"Продаж квартир", "Довгострокова оренда квартир"} <= kids


def test_descendant_ids_includes_self_and_all_children():
    # 108 -- «Легкові автомобілі», объявлений сам не содержит, они лежат в
    # подразделах по маркам. Режим «весь раздел» без этого набора не находил бы ничего.
    ids = categories.descendant_ids(108)
    assert 108 in ids
    assert len(ids) > 50  # десятки марок-подкатегорий


def test_descendant_ids_of_a_leaf_is_just_itself():
    assert categories.descendant_ids(1758) == {1758}


def test_descendant_ids_of_unknown_id_is_just_itself():
    assert categories.descendant_ids(99_999_999) == {99_999_999}


def test_parent_id_walks_up_the_tree():
    assert categories.parent_id(1760) is not None
    assert categories.parent_id(99_999_999) is None


@pytest.mark.parametrize("category_id", [1, 1757, 1758, 1760, 85, 3276])
def test_every_path_is_bounded(category_id):
    assert 0 < len(categories.path(category_id)) <= 6
