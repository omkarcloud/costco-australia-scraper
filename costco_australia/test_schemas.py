"""Offline tests for the /costco-australia/* request schemas and the
reference resolvers.

    python -m pytest costco_australia/test_schemas.py -q
"""
import os
import sys

import pytest
from marshmallow import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from costco_australia import refs, schemas  # noqa: E402
from schema_fields import load_query  # noqa: E402

PRODUCT_LINK = ("https://www.costco.com.au/Appliances/Coffee-Drink-Makers/Coffee-Espresso-Machines/"
                "Kenmore-Drip-Coffee-Maker-KKCM12WAU/p/255146")


# ---- refs --------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("255146", "255146"),
    (" 255146 ", "255146"),
    ("207732_BD", "207732_BD"),
    ("1901732-TLBLK", "1901732-TLBLK"),
    ("wedding-cake", "wedding-cake"),
    ("Bendon-Women-Brief", "Bendon-Women-Brief"),
    (PRODUCT_LINK, "255146"),
    (PRODUCT_LINK + "?foo=bar#reviews", "255146"),
    ("https://www.costco.com.au/c/Thermoflask-Coffee-Mugs-2-Piece-Set/p/1901732", "1901732"),
    ("www.costco.com.au/Business-Delivery/Beverages/Coffee/Coffex-Supremo-Coffee-Beans-1kg/p/207732_BD", "207732_BD"),
    ("//www.costco.com.au/p/255146", "255146"),
    ("/c/Bridgestone-20555R16-91V-Ecopia-EP300/tire/p/7003015", "7003015"),
    ("http://costco.com.au/p/255146/", "255146"),
])
def test_resolve_product(value, expected):
    assert refs.resolve_product(value) == expected


@pytest.mark.parametrize("value", [
    "", "a b", "255146,1", "https://www.costco.com/p/255146", "https://example.com/p/255146",
    "https://www.costco.com.au/Appliances/c/cos_2", "https://www.costco.com.au/", "x" * 81, "-abc", "a/b",
])
def test_resolve_product_rejects(value):
    with pytest.raises(ValueError) as e:
        refs.resolve_product(value)
    assert "costco.com.au" in str(e.value)


def test_resolve_products():
    assert refs.resolve_products(f"255146, 207732_BD ,{PRODUCT_LINK},,") == ["255146", "207732_BD"]
    with pytest.raises(ValueError):
        refs.resolve_products(" , ")
    with pytest.raises(ValueError):
        refs.resolve_products("255146,not a code")
    codes = ",".join(str(100000 + n) for n in range(refs.MAX_BULK_PRODUCTS))
    assert len(refs.resolve_products(codes)) == 30
    with pytest.raises(ValueError) as e:
        refs.resolve_products(codes + ",999999")
    assert str(e.value) == "At most 30 products per call."


@pytest.mark.parametrize("value, expected", [
    ("cos_2.4.4", "cos_2.4.4"),
    ("COS_2.4", "cos_2.4"),
    ("hot-buys", "hot-buys"),
    ("KIRKLAND-SIGNATURE", "KIRKLAND-SIGNATURE"),
    ("Small-Pack-Items", "Small-Pack-Items"),
    ("https://www.costco.com.au/Appliances/Coffee-Drink-Makers/c/cos_2.4", "cos_2.4"),
    ("https://www.costco.com.au/c/hot-buys?q=%3Arelevance", "hot-buys"),
    ("www.costco.com.au/KIRKLAND-SIGNATURE/c/KIRKLAND-SIGNATURE/", "KIRKLAND-SIGNATURE"),
    ("/Appliances/c/cos_2", "cos_2"),
])
def test_resolve_category(value, expected):
    assert refs.resolve_category(value) == expected


@pytest.mark.parametrize("value", [
    "", "a b", "https://example.com/c/cos_2", PRODUCT_LINK,
    "https://www.costco.com.au/c/Thermoflask-Coffee-Mugs-2-Piece-Set/p/1901732",      # a product link under /c/
    "https://www.costco.com.au/store-finder", "cos 2",
])
def test_resolve_category_rejects(value):
    with pytest.raises(ValueError) as e:
        refs.resolve_category(value)
    assert "category" in str(e.value) or "costco.com.au link" in str(e.value)


@pytest.mark.parametrize("value, expected", [
    ("109", "109"),
    ("Marsden Park", "Marsden Park"),
    ("  marsden   park ", "marsden park"),
    ("https://www.costco.com.au/store-finder/Marsden%20Park", "Marsden Park"),
    ("https://www.costco.com.au/store/Perth Airport?lat=0.0&long=0.0", "Perth Airport"),
    ("www.costco.com.au/store-finder/Auburn/", "Auburn"),
])
def test_resolve_store(value, expected):
    assert refs.resolve_store(value) == expected


@pytest.mark.parametrize("value", ["", "1", "12345", "https://example.com/store-finder/Auburn",
                                   "https://www.costco.com.au/store-finder", PRODUCT_LINK, "a@b"])
def test_resolve_store_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_store(value)


def test_links():
    assert refs.absolute_link("/Appliances/c/cos_2") == "https://www.costco.com.au/Appliances/c/cos_2"
    assert refs.absolute_link("/store/Marsden Park") == "https://www.costco.com.au/store/Marsden%20Park"
    assert refs.absolute_link("http://www.costco.com.au/x") == "https://www.costco.com.au/x"
    assert refs.absolute_link("https://cdn.example/a.jpg") == "https://cdn.example/a.jpg"
    assert refs.absolute_link("") is None and refs.absolute_link(None) is None
    assert refs.product_link("207732_BD") == "https://www.costco.com.au/p/207732_BD"
    assert refs.category_link("cos_2.4") == "https://www.costco.com.au/c/cos_2.4"
    assert refs.store_link("Marsden Park") == "https://www.costco.com.au/store-finder/Marsden%20Park"
    assert refs.product_link(None) is None and refs.store_link("") is None


# ---- products/search ---------------------------------------------------------------

def test_products_search_defaults():
    data = schemas.ProductsSearchSchema().load({"query": "  coffee  machine "})
    assert data == {"query": "coffee machine", "category": None, "page": 1, "limit": 20, "sort": "relevance",
                    "brand": None, "filter": None, "postcode": None}


def test_products_search_full():
    data = schemas.ProductsSearchSchema().load({
        "query": "tv", "category": "https://www.costco.com.au/Electronics/c/cos_21", "page": "3", "limit": "100",
        "sort": "TOP_SELLERS", "brand": "Samsung", "filter": "Smart TVs_Screen Size:65 Inch, Type:OLED",
        "postcode": "2000"})
    assert data["category"] == "cos_21" and data["page"] == 3 and data["limit"] == 100
    assert data["sort"] == "sellCount-desc" and data["brand"] == "Samsung" and data["postcode"] == "2000"
    assert data["filter"] == [("Smart TVs_Screen Size", "65 Inch"), ("Type", "OLED")]


@pytest.mark.parametrize("sort, code", [("relevance", "relevance"), ("price_asc", "price-asc"),
                                        ("price_desc", "price-desc"), ("name_asc", "name-asc"),
                                        ("name_desc", "name-desc"), ("top_sellers", "sellCount-desc"),
                                        ("top_rated", "ratings-desc")])
def test_sorts(sort, code):
    assert schemas.ProductsSearchSchema().load({"query": "tv", "sort": sort})["sort"] == code


@pytest.mark.parametrize("params, field, message", [
    ({}, "query", "Missing data for required field."),
    ({"query": " "}, "query", "Must not be empty."),
    ({"query": "tv", "sort": "cheapest"}, "sort", "Must be one of: relevance, price_asc"),
    ({"query": "tv", "limit": "101"}, "limit", "Must be greater than or equal to 1 and less than or equal to 100."),
    ({"query": "tv", "page": "0"}, "page", "Must be greater than or equal to 1"),
    ({"query": "tv", "postcode": "200"}, "postcode", "An Australian postcode has 4 digits (2000)."),
    ({"query": "tv", "postcode": "abcd"}, "postcode", "An Australian postcode has 4 digits (2000)."),
    ({"query": "tv", "filter": "Brand"}, "filter", "'Brand' must be Facet:Value (e.g. Brand:Breville)."),
    ({"query": "tv", "filter": "Format:16:9"}, "filter", "a facet value cannot contain a colon"),
    ({"query": "tv", "category": "https://example.com/c/cos_2"}, "category", "Must be a costco.com.au link."),
    ({"query": "tv", "url": "x"}, "url", "Unknown field."),
])
def test_products_search_rejects(params, field, message):
    with pytest.raises(ValidationError) as e:
        schemas.ProductsSearchSchema().load(params)
    assert field in e.value.messages and message in " ".join(e.value.messages[field])


def test_filter_limits():
    assert schemas.ProductsSearchSchema().load({"query": "tv", "filter": " , "})["filter"] is None
    assert schemas.ProductsSearchSchema().load({"query": "tv", "filter": "A:b,A:b"})["filter"] == [("A", "b")]
    too_many = ",".join(f"F{n}:v" for n in range(11))
    with pytest.raises(ValidationError) as e:
        schemas.ProductsSearchSchema().load({"query": "tv", "filter": too_many})
    assert e.value.messages["filter"] == ["At most 10 refinements."]


# ---- the other product routes ------------------------------------------------------

def test_products_details():
    assert schemas.ProductsDetailsSchema().load({"product": PRODUCT_LINK}) == {"product": "255146", "postcode": None}
    assert schemas.ProductsDetailsSchema().load({"product": "207732_BD", "postcode": "3000"}) == {
        "product": "207732_BD", "postcode": "3000"}
    for params, field in (({}, "product"), ({"product": ""}, "product"), ({"product": "a b"}, "product"),
                          ({"product": "255146", "id": "1"}, "id")):
        with pytest.raises(ValidationError) as e:
            schemas.ProductsDetailsSchema().load(params)
        assert field in e.value.messages
    with pytest.raises(ValidationError) as e:
        schemas.ProductsDetailsSchema().load({"product": "https://www.costco.com.au/Appliances/c/cos_2"})
    assert "Must be a Costco item code" in e.value.messages["product"][0]


def test_products_bulk():
    data = schemas.ProductsBulkSchema().load({"products": f"255146, 207732_BD, {PRODUCT_LINK}", "postcode": "2000"})
    assert data == {"products": ["255146", "207732_BD"], "postcode": "2000"}
    with pytest.raises(ValidationError) as e:
        schemas.ProductsBulkSchema().load({"products": ",".join(str(n) for n in range(100000, 100031))})
    assert e.value.messages["products"] == ["At most 30 products per call."]
    with pytest.raises(ValidationError) as e:
        schemas.ProductsBulkSchema().load({})
    assert "products" in e.value.messages


def test_products_reviews():
    data = schemas.ProductsReviewsSchema().load({"product": "255146"})
    assert data == {"product": "255146", "page": 1, "limit": 20, "sort": "submissiontime:desc", "rating": None}
    data = schemas.ProductsReviewsSchema().load({"product": "255146", "sort": "most_helpful", "rating": "4",
                                                 "limit": "100", "page": "2"})
    assert data["sort"] == "helpfulness:desc,submissiontime:desc" and data["rating"] == 4 and data["limit"] == 100
    for params, field in (({"product": "255146", "rating": "6"}, "rating"), ({"product": "255146", "limit": "101"}, "limit"),
                          ({"product": "255146", "sort": "best"}, "sort"), ({"sort": "newest"}, "product")):
        with pytest.raises(ValidationError) as e:
            schemas.ProductsReviewsSchema().load(params)
        assert field in e.value.messages


def test_products_suggestions():
    assert schemas.ProductsSuggestionsSchema().load({"query": "coff"}) == {"query": "coff", "limit": 10}
    with pytest.raises(ValidationError) as e:
        schemas.ProductsSuggestionsSchema().load({"query": "coff", "limit": "21"})
    assert "limit" in e.value.messages
    with pytest.raises(ValidationError) as e:
        schemas.ProductsSuggestionsSchema().load({})
    assert "query" in e.value.messages


# ---- categories / deals ------------------------------------------------------------

def test_categories_list():
    assert schemas.CategoriesListSchema().load({}) == {"category": None}
    assert schemas.CategoriesListSchema().load({"category": "https://www.costco.com.au/Appliances/c/cos_2"}) == {
        "category": "cos_2"}
    with pytest.raises(ValidationError) as e:
        schemas.CategoriesListSchema().load({"category": "cos 2"})
    assert "Must be a category code" in e.value.messages["category"][0]


def test_categories_products():
    data = schemas.CategoriesProductsSchema().load({"category": "cos_2.4", "query": "breville", "sort": "price_asc"})
    assert data == {"category": "cos_2.4", "query": "breville", "page": 1, "limit": 20, "sort": "price-asc",
                    "brand": None, "filter": None, "postcode": None}
    assert schemas.CategoriesProductsSchema().load({"category": "KIRKLAND-SIGNATURE"})["query"] is None
    with pytest.raises(ValidationError) as e:
        schemas.CategoriesProductsSchema().load({"query": "breville"})
    assert e.value.messages["category"] == ["Missing data for required field."]


def test_deals_list():
    assert schemas.DealsListSchema().load({}) == {"type": "hot-buys", "page": 1, "limit": 20, "sort": "relevance",
                                                  "brand": None}
    assert schemas.DealsListSchema().load({"type": "LAST_CHANCE", "brand": "Apple"})["type"] == "last-chance"
    assert schemas.DealsListSchema().load({"type": "new"})["type"] == "new"
    with pytest.raises(ValidationError) as e:
        schemas.DealsListSchema().load({"type": "clearance"})
    assert e.value.messages["type"] == ["Must be one of: hot_buys, new, last_chance."]
    with pytest.raises(ValidationError) as e:
        schemas.DealsListSchema().load({"postcode": "2000"})        # deals take no postcode
    assert e.value.messages["postcode"] == ["Unknown field."]


# ---- stores ------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("2000", {"postcode": "2000"}),
    ("Bondi NSW", {"suburb": "Bondi NSW"}),
    ("-33.87, 151.21", {"lat": -33.87, "lng": 151.21}),
    ("Bondi, NSW", {"suburb": "Bondi, NSW"}),
])
def test_stores_list_location(value, expected):
    assert schemas.StoresListSchema().load({"location": value}) == {"location": expected}


def test_stores_list_rejects():
    assert schemas.StoresListSchema().load({}) == {"location": None}
    for value, message in (("200", "An Australian postcode has 4 digits (2000)."),
                           ("-133.8,151.2", "Latitude must be -90..90 and longitude -180..180."),
                           ("x" * 81, "Longer than maximum length 80.")):
        with pytest.raises(ValidationError) as e:
            schemas.StoresListSchema().load({"location": value})
        assert e.value.messages["location"] == [message]


def test_stores_details():
    assert schemas.StoresDetailsSchema().load({"store": "109"}) == {"store": "109"}
    assert schemas.StoresDetailsSchema().load({"store": "https://www.costco.com.au/store-finder/Marsden%20Park"}) == {
        "store": "Marsden Park"}
    with pytest.raises(ValidationError) as e:
        schemas.StoresDetailsSchema().load({})
    assert e.value.messages["store"] == ["Missing data for required field."]
    with pytest.raises(ValidationError) as e:
        schemas.StoresDetailsSchema().load({"store": "https://example.com/store-finder/Auburn"})
    assert e.value.messages["store"] == ["Must be a costco.com.au link."]


# ---- the 400 body the route layer answers --------------------------------------------

def test_an_empty_value_of_a_param_with_a_default_means_the_default():
    """`?type=` is the same as no type: the default, never None (None went
    upstream as a category code and a review sort)."""
    deals = schemas.DealsListSchema().load({"type": "", "sort": " "})
    assert (deals["type"], deals["sort"]) == ("hot-buys", "relevance")
    assert schemas.ProductsSearchSchema().load({"query": "coffee", "sort": ""})["sort"] == "relevance"
    assert schemas.ProductsReviewsSchema().load({"product": "255146", "sort": ""})["sort"] == "submissiontime:desc"


def test_error_body_names_the_field():
    data, error = load_query(schemas.ProductsSearchSchema, {"query": "tv", "postcode": "20"})
    assert data is None
    assert error["error"] == "Invalid parameters: postcode: An Australian postcode has 4 digits (2000)."
    assert error["errors"] == {"postcode": ["An Australian postcode has 4 digits (2000)."]}


def test_every_field_is_documented():
    for name in dir(schemas):
        schema = getattr(schemas, name)
        if isinstance(schema, type) and issubclass(schema, schemas.BaseSchema) and name.endswith("Schema") \
                and not name.startswith("_") and schema is not schemas.BaseSchema:
            for field_name, field in schema().fields.items():
                assert field.metadata.get("description"), f"{name}.{field_name} has no description"
