from olx import geo


def test_regions_are_collected_from_the_cities_reference():
    regions = geo.regions()
    assert len(regions) > 10
    names = {name for _, name in regions}
    assert any("Львівська" in n for n in names)


def test_region_name_resolves_known_id():
    kyiv_city_id = 268
    region_id = None
    for cid, city in geo._cities().items():
        if cid == kyiv_city_id:
            region_id = city["region_id"]
    assert region_id is not None
    assert geo.region_name(region_id) is not None


def test_region_name_unknown_id_returns_none():
    assert geo.region_name(99_999_999) is None


def test_find_regions_prefers_prefix_matches():
    matches = geo.find_regions("Льв")
    assert matches
    assert any("Львівська" in name for _, name in matches)
    # Совпадение с начала строки идёт первым -- тот же принцип, что у find_cities.
    assert matches[0][1].casefold().startswith("льв")


def test_find_regions_empty_query_returns_nothing():
    assert geo.find_regions("") == []


def test_find_regions_unknown_name_returns_nothing():
    assert geo.find_regions("Zzzzzzzz") == []
