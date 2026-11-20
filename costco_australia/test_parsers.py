"""Offline tests for costco_australia/parsers.py against the raw captures
in fixtures/ (taken 2026-11-19, untrimmed), plus the transport's reading
of the site's error bodies.

    python -m pytest costco_australia/test_parsers.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from costco_australia import fetch, parsers  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def raw(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def row(listing, code):
    return next(r for r in listing["results"] if r["code"] == code)


# ---- listings ------------------------------------------------------------------------

def test_search_page():
    listing = parsers.parse_listing(load("search_coffee.json"), 1, 8)
    assert listing["pagination"] == {"page": 1, "items_per_page": 8, "total_pages": 5, "total_count": 37}
    assert [r["code"] for r in listing["results"]][:5] == ["42943", "42943_BD", "1901732", "207732_BD", "255146"]
    assert listing["spelling_suggestion"] is None
    kenmore = row(listing, "255146")
    assert kenmore["name"] == "Kenmore Drip Coffee Maker KKCM12WAU"
    assert kenmore["link"] == ("https://www.costco.com.au/Appliances/Coffee-Drink-Makers/Coffee-Espresso-Machines/"
                               "Kenmore-Drip-Coffee-Maker-KKCM12WAU/p/255146")
    assert kenmore["image"].startswith("https://www.costco.com.au/medias/") and kenmore["image"].endswith(".jpg")
    assert kenmore["pricing"]["price"] == 79.99 and kenmore["pricing"]["regular_price"] == 79.99
    assert kenmore["pricing"]["currency"] == "AUD" and kenmore["pricing"]["is_price_hidden"] is False
    assert kenmore["pricing"]["discount_amount"] is None and kenmore["pricing"]["price_hidden_reason"] is None
    assert kenmore["rating"] == {"average": 4.4, "review_count": 7}
    assert kenmore["stock_status"] == "in_stock" and kenmore["is_in_stock"] is True
    assert kenmore["is_purchasable_online"] is True and kenmore["is_business_delivery"] is False
    assert kenmore["order_limits"]["min_quantity"] == 1 and kenmore["order_limits"]["max_quantity_applies"] == "per_order"
    assert kenmore["tyre"] is None and kenmore["colours"] == []


def test_search_rows_without_a_price_say_why():
    listing = parsers.parse_listing(load("search_coffee.json"), 1, 8)
    liquor = row(listing, "42943")
    assert liquor["pricing"]["price"] is None and liquor["pricing"]["is_price_hidden"] is True
    assert liquor["pricing"]["price_hidden_reason"] == "members_only"
    assert liquor["pricing"]["is_members_only_price"] is True
    business = row(listing, "207732_BD")
    assert business["is_business_delivery"] is True
    assert business["pricing"]["price"] is None and business["pricing"]["price_hidden_reason"] == "postcode_required"
    assert business["rating"] == {"average": None, "review_count": 0}
    parent = row(listing, "1901732")
    assert parent["has_variants"] is True and parent["pricing"]["price"] == 39.99


def test_search_with_a_postcode_fills_business_delivery_prices():
    payload = load("search_coffee_postcode_2000.json")
    listing = parsers.parse_listing(payload, 1, 4, "2000")
    beans = row(listing, "207732_BD")
    assert beans["pricing"]["price"] == 22.99
    assert beans["pricing"]["unit_price"] == 2.3 and beans["pricing"]["unit_price_unit"] == "100g"
    assert beans["pricing"]["price_hidden_reason"] is None
    # liquor stays members only, whatever the postcode
    assert row(listing, "42943_BD")["pricing"]["price_hidden_reason"] == "members_only"
    assert parsers.parse_applied_filters(payload) == [{"name": "delivery", "value": "Business Delivery"}]


def test_search_facets():
    listing = parsers.parse_listing(load("search_coffee.json"), 1, 8)
    names = [f["name"] for f in listing["facets"]]
    assert names[:3] == ["delivery", "category", "Brand"]
    assert "idVerificationEnabled" not in names
    brand = next(f for f in listing["facets"] if f["name"] == "Brand")
    assert brand["label"] == "Brand" and brand["is_multi_select"] is True and len(brand["values"]) == 27
    assert {"value": "Breville", "label": "Breville", "count": 3, "is_selected": False} in brand["values"]
    category = next(f for f in listing["facets"] if f["name"] == "category")
    assert category["values"][0] == {"value": "cos_6.5.2", "label": "Camping Equipment", "count": 1,
                                     "is_selected": False}
    assert "Breville" in parsers.facet_values(load("search_coffee.json"), "Brand")
    assert parsers.facet_values(load("search_coffee.json"), "No Such Facet") == []


def test_search_typo_carries_the_sites_suggestion():
    listing = parsers.parse_listing(load("search_typo.json"), 1, 2)
    assert listing["spelling_suggestion"] == "coffee machine"
    assert listing["pagination"]["total_count"] == 15 and len(listing["results"]) == 2


def test_search_nonsense_word_still_answers_the_sites_nearest_match():
    # the site's search is semantic: a word that matches nothing still ranks a product
    listing = parsers.parse_listing(load("search_nonsense.json"), 1, 8)
    assert listing["pagination"]["total_count"] == 1 and len(listing["results"]) == 1


def test_page_past_the_end_is_an_empty_page():
    listing = parsers.parse_listing(load("search_past_end.json"), 51, 8)
    assert listing["results"] == []
    assert listing["pagination"] == {"page": 51, "items_per_page": 8, "total_pages": 0, "total_count": 0}


def test_unknown_category_listing_is_empty():
    listing = parsers.parse_listing(load("search_unknown_category.json"), 1, 5)
    assert listing["results"] == [] and listing["facets"] == []
    assert listing["pagination"]["total_count"] == 0


def test_campaign_listing_carries_the_saving():
    listing = parsers.parse_listing(load("search_hot_buys.json"), 1, 5)
    assert listing["pagination"]["total_count"] == 451 and listing["pagination"]["total_pages"] == 91
    assert listing["spelling_suggestion"] is None       # the raw index query the site puts there is dropped
    tv = row(listing, "2026212")
    assert tv["pricing"]["price"] == 4449.99 and tv["pricing"]["regular_price"] == 5299.99
    assert tv["pricing"]["discount_amount"] == 850 and tv["pricing"]["discount_percent"] == 16.0
    assert tv["pricing"]["discount_starts_at"] == "2026-11-18T13:00:00Z"
    assert tv["pricing"]["discount_ends_at"] == "2026-11-30T12:59:59Z"
    assert tv["badges"] == ["Hot Buy"]
    members = row(listing, "2026068")       # a sale price shown to members only: the saving is public, the price is not
    assert members["pricing"]["price"] is None and members["pricing"]["discount_amount"] == 1800
    assert members["pricing"]["discount_percent"] is None
    assert members["pricing"]["price_hidden_reason"] == "members_only"


def test_category_listing_unit_price():
    listing = parsers.parse_listing(load("search_category_cos_2_4.json"), 1, 5)
    assert listing["pagination"]["total_count"] == 32
    pods = row(listing, "244110")
    assert pods["pricing"]["unit_price"] == 0.42 and pods["pricing"]["unit_price_unit"] == "each"


def test_tyre_listing():
    listing = parsers.parse_listing(load("search_tyres.json"), 1, 3)
    assert listing["pagination"]["total_count"] == 1458
    tyre = listing["results"][0]
    assert tyre["code"] == "9012419" and tyre["is_tyre"] is True
    assert tyre["tyre"] == {"size": "265/65 R17", "load_index": 112, "speed_rating": "T", "is_run_flat": False}
    assert tyre["pricing"]["price"] is None and tyre["pricing"]["price_hidden_reason"] == "shown_on_product_only"
    assert tyre["badges"] == ["Costco Shop Card"]


def test_listing_tolerates_junk():
    for junk in (None, {}, [], {"products": "x", "pagination": None, "facets": [None, {}]}):
        listing = parsers.parse_listing(junk, 1, 20)
        assert listing["results"] == [] and listing["facets"] == []
        assert listing["pagination"] == {"page": 1, "items_per_page": 20, "total_pages": 0, "total_count": 0}
    blank = parsers.parse_product_row({})
    assert blank["code"] is None and blank["pricing"]["price"] is None and blank["badges"] == []
    assert blank["stock_status"] is None and blank["is_in_stock"] is None


@pytest.mark.parametrize("decal, label", [
    ({"value": {"altText": "Hot Buy", "url": "/mediapermalink/AU-decal-hot-buy"}}, "Hot Buy"),
    ({"value": {"altText": "null", "url": "/mediapermalink/AU-decal-200-off-4-tyres"}}, "200 Off 4 Tyres"),
    ({"value": {"url": "https://x.example/v3/assets/a/b/AU-decal-trade-in-available"}}, "Trade In Available"),
    ({"value": {"altText": "null", "url": "/mediapermalink/decal-last-chance"}}, "Last Chance"),
    ({"value": {"altText": "null", "url": "/mediapermalink/AU-decal-BMW"}}, "BMW"),
    ({"value": {}}, None),
])
def test_badge_labels(decal, label):
    assert parsers._badge(decal) == label


# ---- batch ---------------------------------------------------------------------------

def test_batch_keeps_the_order_asked_and_names_the_unknown():
    batch = parsers.parse_batch(load("export_three.json")["product"], ["42943", "nope", "255146", "1901732"])
    assert [p["code"] for p in batch["products"]] == ["42943", "255146", "1901732"]
    assert batch["not_found"] == ["nope"]
    batch = parsers.parse_batch(load("export_with_unknown.json")["product"], ["255146", "99999999"])
    assert [p["code"] for p in batch["products"]] == ["255146"] and batch["not_found"] == ["99999999"]


def test_batch_none_found():
    assert raw("export_all_unknown.json") == ""         # the site answers 204 with no body
    assert parsers.parse_batch([], ["99999999"]) == {"products": [], "not_found": ["99999999"]}
    assert parsers.parse_batch(None, []) == {"products": [], "not_found": []}


def test_batch_members_only_price_is_flagged_and_can_be_dropped():
    rows = load("export_three.json")["product"]
    liquor = parsers.parse_batch(rows, ["42943"])["products"][0]["pricing"]
    assert liquor["price"] == 89.99 and liquor["unit_price"] == 5.15 and liquor["unit_price_unit"] == "100ml"
    assert liquor["is_members_only_price"] is True and liquor["is_price_hidden"] is False
    dropped = parsers.parse_batch(rows, ["42943"], members_only_prices=False)["products"][0]["pricing"]
    assert dropped["price"] is None and dropped["unit_price"] is None
    assert dropped["is_price_hidden"] is True and dropped["price_hidden_reason"] == "members_only"
    # an ordinary price is untouched by the switch
    plain = parsers.parse_batch(rows, ["255146"], members_only_prices=False)["products"][0]["pricing"]
    assert plain["price"] == 79.99 and plain["is_members_only_price"] is False


# ---- suggestions ---------------------------------------------------------------------

def test_suggestions():
    assert parsers.parse_suggestions(load("suggestions_coff.json")) == [
        "coffee & drink", "coffee capsules", "coffee & espresso", "coffee", "coffee tables"]
    assert parsers.parse_suggestions(load("suggestions_empty.json")) == ["zzqqxx"]
    assert parsers.parse_suggestions({}) == [] and parsers.parse_suggestions(None) == []


# ---- product -------------------------------------------------------------------------

def test_product():
    product = parsers.parse_product(load("product_255146.json"))
    assert product["code"] == "255146" and product["name"] == "Kenmore Drip Coffee Maker KKCM12WAU"
    assert product["brand"] == "Kenmore"
    assert product["description"].startswith("There's nothing better in the world")     # heading and name stripped
    assert "- Large 12 coffee cup capacity" in product["description"]
    assert len(product["images"]) == 4 and product["image"] == product["images"][0]
    assert product["videos"] == []
    assert product["pricing"]["price"] == 79.99 and product["pricing"]["is_from_price"] is False
    assert product["stock"] == {"status": "in_stock", "is_in_stock": True, "level": 61}
    assert product["category"] == {
        "code": "cos_2.4.4", "name": "Coffee & Espresso Machines",
        "link": "https://www.costco.com.au/Appliances/Coffee-Drink-Makers/Coffee-Espresso-Machines/c/cos_2.4.4"}
    codes = [c["code"] for c in product["categories"]]
    assert "cos_2.4.4" in codes and not any(c.startswith("tpd-") for c in codes)
    assert {"name": "Brand", "value": "Kenmore"} in product["specifications"]
    assert product["variants"] == [] and product["parent_product_code"] is None
    assert product["delivery"]["notes"][0].startswith("Please allow 2-5 business days")
    assert product["delivery"]["delivery_and_returns"].startswith("Delivery")
    assert product["department_number"] == "33"
    # without the review service's record: no barcode, the site's own rating
    assert product["barcodes"] == [] and product["rating"]["average"] == 4.4
    assert product["rating"]["review_count"] == 7 and product["rating"]["distribution"] is None


def test_product_with_review_record():
    record = parsers.parse_review_record(load("bv_product_50634.json")["response"])
    assert record["brand"] == "BREVILLE" and record["barcodes"] == ["9312432034241"]
    assert record["rating"]["review_count"] == 94 and record["rating"]["distribution"]["5"] == 78
    product = parsers.parse_product(load("product_255146.json"), record)
    assert product["brand"] == "Kenmore"                 # the site's own spec wins over the review service's
    assert product["barcodes"] == ["9312432034241"]
    assert product["rating"]["distribution"] == {"5": 78, "4": 12, "3": 2, "2": 1, "1": 1}
    assert product["rating"]["recommended_count"] == 69


def test_review_record_drops_the_placeholder_barcode():
    record = parsers.parse_review_record(load("bv_product_255146_placeholder_barcode.json")["response"])
    assert record["brand"] == "KENMORE" and record["barcodes"] == []
    assert parsers.parse_review_record(load("bv_product_42943.json")["response"])["barcodes"] == ["7610594252094"]
    assert parsers.parse_review_record(load("bv_product_unknown.json")["response"]) is None
    assert parsers.parse_review_record(None) is None


def test_product_variants_parent_and_child():
    parent = parsers.parse_product(load("product_1901732_variants.json"))
    assert parent["has_variants"] is True and parent["pricing"]["is_from_price"] is True
    assert [v["code"] for v in parent["variants"]] == ["1901732-TLBLK", "1901732-VLTGRN"]
    assert parent["variants"][0] == {
        "code": "1901732-TLBLK",
        "link": "https://www.costco.com.au/c/Thermoflask-Coffee-Mugs-2-Piece-Set-Teal-Black/p/1901732-TLBLK",
        "options": {"Option": "Teal Black"}, "price": 39.99, "currency": "AUD",
        "stock_status": "in_stock", "stock_level": 44, "is_in_stock": True}
    child = parsers.parse_product(load("product_1901732_TLBLK_variant.json"))
    assert child["parent_product_code"] == "1901732"
    assert [v["code"] for v in child["variants"]] == ["1901732-TLBLK", "1901732-VLTGRN"]      # its siblings
    assert child["stock"]["level"] == 44


def test_product_business_delivery_needs_a_postcode():
    without = parsers.parse_product(load("product_207732_BD.json"))
    assert without["is_business_delivery"] is True
    assert without["pricing"]["price"] is None and without["pricing"]["price_hidden_reason"] == "postcode_required"
    with_postcode = parsers.parse_product(load("product_207732_BD_postcode_2000.json"), None, "2000")
    assert with_postcode["pricing"]["price"] == 22.99
    assert with_postcode["pricing"]["unit_price"] == 2.3 and with_postcode["pricing"]["unit_price_unit"] == "100g"
    assert with_postcode["stock"]["level"] == 189
    # the same empty answer, asked WITH a postcode, means the postcode is outside the delivery area
    outside = parsers.parse_product(load("product_207732_BD.json"), None, "0800")
    assert outside["pricing"]["price_hidden_reason"] == "not_delivered_to_postcode"


def test_product_members_only_price():
    product = parsers.parse_product(load("product_42943_hidden_price.json"))
    assert product["pricing"]["price"] is None and product["pricing"]["is_price_hidden"] is True
    assert product["pricing"]["price_hidden_reason"] == "members_only"
    assert product["stock"]["level"] == 57 and product["rating"]["review_count"] == 24


def test_product_tyre():
    tyre = parsers.parse_product(load("product_7003015_tyre.json"))
    assert tyre["is_tyre"] is True and tyre["is_warehouse_pickup_only"] is True
    assert tyre["pricing"]["price"] == 189.99                 # a tyre's price is on the product, not in listings
    assert tyre["tyre"] == {"size": "205/55 R16", "load_index": 91, "speed_rating": "V", "is_run_flat": False}
    assert tyre["brand"] == "Bridgestone"                     # from the "Tire Manufacturer" spec
    assert tyre["summary"].split("\n")[:2] == ["Rotation, Balancing, Flat Repairs", "Nitrogen Tyre Inflation"]
    assert tyre["badges"] == ["4 for 3", "Costco Shop Card"]
    assert tyre["promotions"][0]["code"] == "288845" and tyre["promotions"][0]["ends_at"] == "2026-11-30T12:59:36Z"
    assert tyre["add_on_services"] == [{
        "code": "926", "name": "Tyre Fit and Balance at no charge",
        "link": "https://www.costco.com.au/Tyres-Automotive/Tyre-Fit-and-Balance-at-no-charge/p/926",
        "price": 19.99, "is_optional": False}]


def test_product_discount():
    product = parsers.parse_product(load("product_240974_discount.json"))
    pricing = product["pricing"]
    assert pricing["price"] == 289.99 and pricing["regular_price"] == 389.99
    assert pricing["discount_amount"] == 100 and pricing["discount_percent"] == 25.6
    assert pricing["discount_starts_at"] == "2026-11-11T13:00:00Z" and pricing["discount_ends_at"] == "2026-12-02T12:59:59Z"
    assert product["badges"] == ["Hot Buy", "Trade In Available"]
    assert product["category"]["code"] == "cos_21.9.10"


def test_product_highlights_are_split_from_the_description():
    product = parsers.parse_product(load("product_50634_highlights.json"))
    assert product["highlights"][0] == {"title": "Beans", "text": "Grind right before"}
    assert len(product["highlights"]) == 7
    assert product["description"].startswith("Breville's Barista Express coffee machine")
    assert "Grind right before" not in product["description"][:200]
    assert parsers.parse_product(load("product_255146.json"))["highlights"] == []


def test_product_tolerates_a_partial_payload():
    blank = parsers.parse_product({})
    assert blank["code"] is None and blank["images"] == [] and blank["specifications"] == []
    assert blank["category"] is None and blank["variants"] == [] and blank["description"] is None
    sparse = parsers.parse_product({"code": "1", "images": [None, {"url": None}], "classifications": [None, {"features": [{}]}],
                                    "categories": [None, {"code": "tpd-1", "name": "tpd-1"}], "stock": "x",
                                    "variantOptions": [None, {}], "potentialPromotions": [None], "pdpMessages": [None]})
    assert sparse["code"] == "1" and sparse["link"] == "https://www.costco.com.au/p/1"
    assert sparse["categories"] == [] and sparse["stock"]["status"] is None and sparse["variants"] == []


# ---- categories ----------------------------------------------------------------------

def test_category_department_landing_page():
    category = parsers.parse_category(load("category_cos_2.json"))
    assert category["code"] == "cos_2" and category["name"] == "Appliances"
    assert category["link"] == "https://www.costco.com.au/Appliances/c/cos_2"
    assert category["is_landing_page"] is True and category["products_count"] is None
    assert category["parents"] == [] and len(category["subcategories"]) == 9
    first = category["subcategories"][0]
    assert first["code"] == "cos_2.1" and first["name"] == "Fridges & Freezers" and first["image"].endswith(".jpg")


def test_category_with_products_and_parents():
    category = parsers.parse_category(load("category_cos_2_4_4.json"))
    assert category["products_count"] == 10 and category["is_landing_page"] is False
    assert [p["code"] for p in category["parents"]] == ["cos_2.4", "cos_2"]
    campaign = parsers.parse_category(load("category_hot_buys.json"))
    assert campaign["code"] == "hot-buys" and campaign["is_campaign"] is True and campaign["products_count"] == 451
    assert parsers.parse_category({})["code"] is None


def test_category_tree():
    tree = parsers.parse_category_tree(load("category_tree_cos_2.json"))
    assert tree["code"] == "cos_2" and tree["name"] == "Appliances" and len(tree["subcategories"]) == 9
    assert parsers.count_categories(tree["subcategories"]) == 41
    assert tree["subcategories"][0]["subcategories"][0] == {
        "code": "cos_2.1.3", "name": "Freezers",
        "link": "https://www.costco.com.au/Appliances/Fridges-Freezers/Freezers/c/cos_2.1.3", "subcategories": []}
    leaf = parsers.parse_category_tree(load("category_tree_hot_buys.json"))
    assert leaf == {"code": "hot-buys", "name": "Hot Buys", "link": "https://www.costco.com.au/c/hot-buys",
                    "subcategories": []}
    assert parsers.parse_category_tree(None)["code"] is None


# ---- stores --------------------------------------------------------------------------

def test_stores_list():
    stores = parsers.parse_stores(load("stores.json"))
    assert stores["origin"] is None and len(stores["stores"]) == 15
    store = next(s for s in stores["stores"] if s["code"] == "109")
    assert store["name"] == "Marsden Park" and store["link"] == "https://www.costco.com.au/store-finder/Marsden%20Park"
    assert store["phone"] == "(02) 9854 3700" and store["email"] == "marsdenpark@costco.com.au"
    assert store["address"] == {
        "line1": "10 Langford Drive", "line2": None, "suburb": "Marsden Park", "state": "NSW", "postcode": "2765",
        "country": "Australia", "country_code": "AU", "formatted": "10 Langford Drive Marsden Park NSW Australia 2765"}
    assert store["latitude"] == -33.72141 and store["longitude"] == 150.839951 and store["distance_km"] is None
    assert store["opening_hours"][0] == {"day": "Sunday", "opens": "10:00", "closes": "18:00", "is_closed": False}
    assert store["opening_hours"][1] == {"day": "Monday", "opens": "10:00", "closes": "20:30", "is_closed": False}
    assert store["special_hours"][0] == {"date": "2026-12-24", "name": "Christmas Eve", "opens": "08:30",
                                         "closes": "17:00", "is_closed": False}
    assert store["special_hours"][1] == {"date": "2026-12-25", "name": "Christmas Day", "opens": None,
                                         "closes": None, "is_closed": True}
    assert [s["type"] for s in store["services"]] == ["tyre_centre", "food_court", "fuel_station", "hearing_aids", "optical"]
    assert store["services"][0]["phone"] == "(02) 9854 3780"
    assert store["services"][0]["opening_hours"].startswith("- Mon-Fri 10:00am - 8:30pm")
    assert "Fresh Sushi" in store["features"]


def test_store_fuel_prices():
    stores = {s["name"]: s for s in parsers.parse_stores(load("stores.json"))["stores"]}
    fuel = stores["Marsden Park"]["fuel"]
    assert stores["Marsden Park"]["has_fuel_station"] is True
    assert fuel["prices"] == [{"fuel": "E10", "price_per_litre": 1.987}, {"fuel": "Premium 98", "price_per_litre": 2.287},
                              {"fuel": "Diesel", "price_per_litre": 2.487}]
    assert fuel["currency"] == "AUD"
    assert fuel["opening_hours"][1] == {"day": "Monday", "opens": "06:00", "closes": "21:30", "is_closed": False}
    assert fuel["note"].startswith("Fuel prices shown here are updated frequently")
    ringwood = stores["Ringwood"]           # the one warehouse without a fuel station
    assert ringwood["has_fuel_station"] is False and ringwood["fuel"]["prices"] == []
    assert ringwood["fuel"]["currency"] is None


def test_store_states_and_suburbs():
    stores = {s["name"]: s["address"] for s in parsers.parse_stores(load("stores.json"))["stores"]}
    assert (stores["Canberra"]["suburb"], stores["Canberra"]["state"]) == ("Canberra Airport", "ACT")
    assert (stores["Auburn"]["suburb"], stores["Auburn"]["state"]) == ("Lidcombe", "NSW")
    # "Perth Airport " carries no state in its town: taken from its opening-hours schedule
    assert (stores["Perth Airport"]["suburb"], stores["Perth Airport"]["state"]) == ("Perth Airport", "WA")
    assert stores["Casuarina"]["postcode"] == "6167"        # the site pads it with a space
    assert {a["state"] for a in stores.values()} == {"NSW", "ACT", "VIC", "SA", "QLD", "WA"}


def test_stores_near_a_place_carry_distances():
    near = parsers.parse_stores(load("stores_near_3000.json"), with_distance=True)
    assert near["origin"] == {"latitude": -37.8152065, "longitude": 144.963937}
    assert [(s["name"], s["distance_km"]) for s in near["stores"]] == [
        ("Ardeer", 15.3), ("Epping", 17.8), ("Moorabbin", 22.1)]


def test_stores_unplaced_location_has_no_origin():
    payload = load("stores_near_3000.json")
    payload["sourceLatitude"], payload["sourceLongitude"] = parsers.UNPLACED_ORIGIN
    unplaced = parsers.parse_stores(payload, with_distance=True)
    assert unplaced["origin"] is None and unplaced["stores"][0]["distance_km"] is None
    assert parsers.parse_stores({}) == {"origin": None, "stores": []}


def test_single_store_matches_the_list_row():
    single = parsers.parse_store(load("store_marsden_park.json"))
    listed = next(s for s in parsers.parse_stores(load("stores.json"))["stores"] if s["code"] == "109")
    assert single == listed
    assert parsers.parse_store({})["code"] is None


@pytest.mark.parametrize("text, clock", [("6:00 PM", "18:00"), ("10:00 AM", "10:00"), ("12:44 AM", "00:44"),
                                         ("12:00 PM", "12:00"), ("8:30 pm", "20:30"), ("", None), (None, None)])
def test_clock(text, clock):
    assert parsers._clock({"formattedHour": text}) == clock


# ---- reviews -------------------------------------------------------------------------

def test_reviews_page():
    parsed = parsers.parse_reviews(load("reviews_255146.json")["response"], "255146")
    assert parsed["total"] == 6 and len(parsed["reviews"]) == 5
    stats = parsed["statistics"]
    assert stats["average"] == 4.43 and stats["review_count"] == 7 and stats["ratings_only_count"] == 1
    assert stats["distribution"] == {"5": 6, "4": 0, "3": 0, "2": 0, "1": 1}
    assert stats["recommended_count"] == 5 and stats["not_recommended_count"] == 1
    assert stats["secondary_ratings"] == {"quality": 4.0, "value": 3.75}
    assert stats["first_review_at"] == "2026-05-06T11:10:12Z" and stats["last_review_at"] == "2026-10-09T04:03:40Z"
    review = parsed["reviews"][0]
    assert review["id"] == "1232938090" and review["title"] == "Good product" and review["rating"] == 5
    assert review["text"] == "Good product, makes a really good coffee and it's easy to clean"
    assert review["is_recommended"] is True and review["submitted_at"] == "2026-10-09T04:03:40Z"
    assert review["author"] == {"name": "C Santana", "location": None, "review_count": 2}
    assert review["is_verified_purchaser"] is True and review["is_syndicated"] is False
    assert review["helpful_votes"] == 0 and review["photos"] == [] and review["responses"] == []
    assert review["product_code"] == "255146" and review["product_name"] == "Kenmore Drip Coffee Maker KKCM12WAU"


def test_reviews_secondary_ratings_and_locations():
    parsed = parsers.parse_reviews(load("reviews_50634_helpful.json")["response"], "50634")
    assert parsed["total"] == 87 and parsed["statistics"]["review_count"] == 94
    first = parsed["reviews"][0]
    assert first["author"]["name"] == "Bek4012" and first["author"]["location"] == "Brisbane"
    assert first["secondary_ratings"] == {"value": 5.0, "quality": 5.0}


def test_reviews_photos():
    parsed = parsers.parse_reviews(load("reviews_50634_with_photos.json")["response"], "50634")
    photos = parsed["reviews"][0]["photos"]
    assert photos and photos[0]["link"].startswith("https://photos-eu.bazaarvoice.com/photo/") and photos[0]["caption"] is None


def test_reviews_no_match():
    for name in ("reviews_240974_rating_1.json", "reviews_unknown_product.json"):
        parsed = parsers.parse_reviews(load(name)["response"], "240974")
        assert parsed == {"total": 0, "statistics": None, "reviews": []}
    empty = parsers.parse_review_statistics({})
    assert empty["average"] is None and empty["review_count"] == 0
    assert empty["distribution"] == {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
    assert parsers.parse_reviews(None, "1") == {"total": 0, "statistics": None, "reviews": []}


# ---- the transport's reading of the site's answers ------------------------------------

class _Resp:
    def __init__(self, status, text, content_type="application/json;charset=UTF-8"):
        self.status_code, self.text, self.headers = status, text, {"content-type": content_type}

    def json(self):
        return json.loads(self.text)


@pytest.mark.parametrize("name", ["product_not_found.json", "category_not_found.json", "category_tree_not_found.json"])
def test_unknown_id_is_not_found(name):
    with pytest.raises(fetch.CostcoAustraliaNotFound) as e:
        fetch._classify(_Resp(404, raw(name)), "product 99999999")
    assert str(e.value) == "product 99999999 not found"


def test_error_statuses():
    with pytest.raises(fetch.CostcoAustraliaBadRequest) as e:
        fetch._classify(_Resp(400, raw("store_not_found.json")), "store Nowhere")
    assert "No PointOfService with name Nowhere" in str(e.value)
    for status in (401, 403, 429):
        with pytest.raises(fetch.CostcoAustraliaBlocked):
            fetch._classify(_Resp(status, "<html>denied</html>", "text/html"), "search")
    # the edge's own 404 page (an unknown path) is our fault or the site's, never "not found" for the caller
    with pytest.raises(fetch.CostcoAustraliaUpstreamError) as e:
        fetch._classify(_Resp(404, "<html>Error</html>", "text/html; charset=utf-8"), "search")
    assert not isinstance(e.value, fetch.CostcoAustraliaNotFound)
    with pytest.raises(fetch.CostcoAustraliaUpstreamError):
        fetch._classify(_Resp(503, "busy", "text/plain"), "search")
    assert fetch._classify(_Resp(200, "{}"), "search") is None


def test_a_page_where_json_was_expected_is_a_block():
    with pytest.raises(fetch.CostcoAustraliaBlocked):
        fetch._json(_Resp(200, "<!doctype html><title>Costco</title>", "text/html"), "search")
    with pytest.raises(fetch.CostcoAustraliaUpstreamError):
        fetch._json(_Resp(200, "[1, 2]"), "search")
    assert fetch._json(_Resp(200, '{"a": 1}'), "search") == {"a": 1}


def test_search_expression():
    assert fetch.search_expression("coffee", "price-asc", [("Brand", "Breville")]) == "coffee:price-asc:Brand:Breville"
    assert fetch.search_expression(None, None, [("allCategories", "cos_2")]) == ":relevance:allCategories:cos_2"
    assert fetch.search_expression(" a:b ", "relevance") == "a b:relevance"      # a colon would end the text
    assert fetch.department_codes()[0] == "cos_1" and len(fetch.department_codes()) == fetch.MAX_DEPARTMENT_NUMBER


def test_retry_drops_the_session_and_stops_at_not_found(monkeypatch):
    calls, dropped = [], []
    monkeypatch.setattr(fetch.config, "MAX_RETRIES", 3)
    monkeypatch.setattr(fetch.config, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(fetch, "_drop_session", lambda: dropped.append(1))

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise fetch.CostcoAustraliaBlocked("HTTP 403")
        return "ok"
    assert fetch._retrying(flaky) == "ok" and len(calls) == 3 and len(dropped) == 2

    def missing():
        calls.append(1)
        raise fetch.CostcoAustraliaNotFound("product x not found")
    calls.clear()
    with pytest.raises(fetch.CostcoAustraliaNotFound):
        fetch._retrying(missing)
    assert len(calls) == 1
