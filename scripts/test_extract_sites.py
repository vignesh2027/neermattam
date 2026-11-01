"""Tests for scripts/extract_sites.py (run with pytest from the repository root)."""

import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("extract_sites", pathlib.Path(__file__).with_name("extract_sites.py"))
es = importlib.util.module_from_spec(spec)
spec.loader.exec_module(es)


def test_kinds_come_from_tags_only():
    assert es.kind_of({"telecom": "data_center"}) == ("data_centre", "telecom=data_center")
    assert es.kind_of({"building": "data_centre"}) == ("data_centre", "building=data_centre")
    assert es.kind_of({"amenity": "hospital"}) == ("hospital", "amenity=hospital")
    assert es.kind_of({"telecom": "exchange"}) == ("telephone_exchange", "telecom=exchange")
    assert es.kind_of({"telecom": "landing_station"})[0] == "cable_landing_station"
    assert es.kind_of({"amenity": "fuel"}) == ("fuel", "amenity=fuel")
    assert es.kind_of({"name": "Data Centre Road"}) == (None, None)
    assert es.kind_of({"building": "yes"}) == (None, None)


def test_osm_references_and_cities():
    assert es.osm_ref("n42") == "node/42"
    assert es.osm_ref("w7") == "way/7"
    assert es.osm_ref("a14") == "way/7"  # osmium area ids: 2 x way id
    assert es.osm_ref("a15") == "relation/7"  # 2 x relation id + 1
    assert es.city_of(13.1, 80.16) == "chennai"
    assert es.city_of(12.83, 80.22) == "chennai"  # Siruseri, inside the widened box
    assert es.city_of(12.79, 80.22) is None  # just south of it
    assert es.near_a_box(12.79, 80.22, "southern")
    assert es.city_of(17.72, 83.30) == "visakhapatnam"


def test_centroid_and_point_in_polygon():
    square = [[80.0, 13.0], [80.002, 13.0], [80.002, 13.002], [80.0, 13.002], [80.0, 13.0]]
    lat, lon = es.centre({"type": "Polygon", "coordinates": [square]})
    assert abs(lat - 13.001) < 1e-9 and abs(lon - 80.001) < 1e-9
    geom = {"type": "MultiPolygon", "coordinates": [[square]]}
    assert es.inside(13.001, 80.001, geom)
    assert not es.inside(13.01, 80.001, geom)
    assert es.centre({"type": "Point", "coordinates": [80.1, 13.2]}) == (13.2, 80.1)
