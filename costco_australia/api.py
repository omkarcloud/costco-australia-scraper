"""The /costco-australia/* endpoint functions (route_glue's route_table
calls them with the schema's loaded kwargs).

Caching (cache.cached_call) sits here, on the upstream pieces as well as
on the answers, so endpoints that read the same thing pay one fetch:

  costco_australia.details    a product in full; products/reviews takes
                              its name and link from the same row
  costco_australia.category   a category's own record; products/search,
                              categories/products, categories/list and
                              deals/list all read it
  costco_australia.stores     the warehouse list, which carries every
                              field of every warehouse: stores/list and
                              stores/details are two views of one fetch

A listing lists a category WITH everything under it (the site's
allCategories facet): a department page such as cos_2 lists no products
of its own on the site, here it lists all 338 of its subcategories'.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import config
from cache import cached_call
from costco_australia import cache_config as ttl
from costco_australia import parsers
from costco_australia import refs
from costco_australia.fetch import (CostcoAustraliaNotFound, CostcoAustraliaUpstreamError, department_codes,
                                    fetch_category, fetch_category_tree, fetch_count, fetch_facet,
                                    fetch_product, fetch_products, fetch_review_product, fetch_reviews,
                                    fetch_search, fetch_stores, fetch_suggestions, search_expression)
from scraper_utils import fanout

TREE_WORKERS = 6        # 99 department codes, ~0.4 s each: the whole tree in ~7 s, once a day (CATEGORIES_CACHE)

_SORT_NAMES = {code: name for name, code in refs.SORTS.items()}
_DEAL_NAMES = {code: name for name, code in refs.DEAL_TYPES.items()}
_REVIEW_SORT_NAMES = {code: name for name, code in refs.REVIEW_SORTS.items()}
_CATEGORY_KEYS = ("code", "name", "link")


# ---- categories (shared) -------------------------------------------------------------

def _fetch_category(code):
    return parsers.parse_category(fetch_category(code))


def _category(code):
    """A category's own record (cached). Unknown code -> NotFound."""
    try:
        return cached_call("costco_australia.category", {"code": code}, _fetch_category, ttl.CATEGORIES_CACHE)
    except CostcoAustraliaNotFound:
        raise CostcoAustraliaNotFound(f"category {code} not found")


def _all_under(code):
    """The facet that selects a category with everything under it."""
    return ("allCategories", code)


# ---- listings ------------------------------------------------------------------------

def _user_facets(data):
    facets = []
    if data.get("brand"):
        facets.append(("Brand", data["brand"]))
    facets += [tuple(pair) for pair in data.get("filter") or []]
    return facets


def _check_facets(payload, facets):
    """The site silently drops a refinement on a facet it does not have,
    and would answer the unrefined listing: say so instead."""
    applied = {f["name"] for f in parsers.parse_applied_filters(payload)}
    unknown = sorted({name for name, _ in facets if name not in applied})
    if unknown:
        raise ValueError(f"filter: no facet named {', '.join(repr(n) for n in unknown)} on this listing; "
                         "use the `name` of a facet the result page returns.")


def _site_brand(text, base, brand):
    """How the site writes a brand ("breville" -> "Breville"): the Brand
    facet is case-sensitive, so a brand that matched nothing is looked up
    in the unrefined listing's own Brand values. None when it is not there."""
    payload = fetch_facet(search_expression(text, "relevance", base), "Brand")
    wanted = brand.casefold()
    for value in parsers.facet_values(payload, "Brand"):
        if value.casefold() == wanted and value != brand:
            return value
    return None


def _listing(data, text, base):
    """One listing page: `base` are the route's own facets (the category or
    campaign), then the caller's brand and filters."""
    page, limit, sort, postcode = data["page"], data["limit"], data.get("sort") or "relevance", data.get("postcode")
    facets = _user_facets(data)
    payload = fetch_search(search_expression(text, sort, [*base, *facets]), page, limit, postcode)
    _check_facets(payload, facets)
    brand = data.get("brand")
    if brand and not (payload.get("pagination") or {}).get("totalResults") and page == 1:
        site_brand = _site_brand(text, base, brand)
        if site_brand:
            brand = site_brand
            facets = [("Brand", site_brand)] + facets[1:]
            payload = fetch_search(search_expression(text, sort, [*base, *facets]), page, limit, postcode)
    listing = parsers.parse_listing(payload, page, limit, postcode)
    return {
        "sort": _SORT_NAMES.get(sort, sort),
        "brand": brand,
        "filters": [f for f in parsers.parse_applied_filters(payload) if f["name"] != "Brand"],
        "postcode": postcode,
        **listing,
    }


def _products_search(**data):
    category = _category(data["category"]) if data.get("category") else None
    base = [_all_under(category["code"])] if category else []
    return {
        "query": data["query"],
        "category": {k: category[k] for k in _CATEGORY_KEYS} if category else None,
        **_listing(data, data["query"], base),
    }


def products_search(**data):
    return cached_call("costco_australia.search", data, _products_search, ttl.LISTING_CACHE)


def _categories_products(**data):
    category = _category(data["category"])
    return {
        "category": {**{k: category[k] for k in _CATEGORY_KEYS}, "parents": category["parents"],
                     "subcategories": [{k: s[k] for k in _CATEGORY_KEYS} for s in category["subcategories"]]},
        "query": data.get("query"),
        **_listing(data, data.get("query"), [_all_under(category["code"])]),
    }


def categories_products(**data):
    return cached_call("costco_australia.category_products", data, _categories_products, ttl.LISTING_CACHE)


def _deals_list(**data):
    campaign = _category(data["type"])
    listing = _listing({**data, "filter": None, "postcode": None}, None, [_all_under(campaign["code"])])
    listing.pop("filters", None)
    listing.pop("postcode", None)
    return {
        "type": _DEAL_NAMES.get(data["type"], data["type"]),
        "name": campaign["name"],
        "link": campaign["link"],
        **listing,
    }


def deals_list(**data):
    return cached_call("costco_australia.deals", data, _deals_list, ttl.LISTING_CACHE)


# ---- products/details ----------------------------------------------------------------

def _fetch_review_record(code):
    return parsers.parse_review_record(fetch_review_product(code)) or {}


def _review_record(code):
    """The review service's record of a product (brand, barcodes, rating
    distribution), cached. It only adds to a product: when the service
    fails, the product is answered without it rather than not at all."""
    try:
        return cached_call("costco_australia.review_record", {"code": code}, _fetch_review_record,
                           ttl.REVIEW_RECORD_CACHE)
    except CostcoAustraliaUpstreamError as e:
        print(f"costco-australia: review record of {code} skipped: {type(e).__name__}: {e}")
        return {}


def _product_details(product, postcode):
    raw = fetch_product(product, postcode)
    return parsers.parse_product(raw, _review_record(product), postcode)


def products_details(**data):
    return cached_call("costco_australia.details", data, _product_details, ttl.PRODUCT_CACHE)


# ---- products/bulk -------------------------------------------------------------------

def _products_bulk(products, postcode):
    batch = parsers.parse_batch(fetch_products(products, postcode), products, postcode,
                                members_only_prices=config.COSTCO_AUSTRALIA_MEMBERS_ONLY_PRICES)
    return {
        "postcode": postcode,
        "requested_count": len(products),
        "found_count": len(batch["products"]),
        "not_found": batch["not_found"],
        "products": batch["products"],
    }


def products_bulk(**data):
    return cached_call("costco_australia.bulk", data, _products_bulk, ttl.BULK_CACHE)


# ---- products/reviews ----------------------------------------------------------------

def _products_reviews(product, page, limit, sort, rating):
    # the product's own row: an unknown code is a 404 here, where the
    # review service would answer an empty list for it
    details = products_details(product=product, postcode=None)
    parsed = parsers.parse_reviews(fetch_reviews(product, limit, (page - 1) * limit, sort, rating), product)
    total = parsed["total"]
    return {
        "code": details["code"],
        "name": details["name"],
        "link": details["link"],
        "sort": _REVIEW_SORT_NAMES.get(sort, sort),
        "rating_filter": rating,
        "statistics": parsed["statistics"] or parsers.parse_review_statistics({}),
        "reviews": parsed["reviews"],
        "pagination": {"page": page, "items_per_page": limit, "total_count": total,
                       "total_pages": -(-total // limit) if total else 0},
    }


def products_reviews(**data):
    return cached_call("costco_australia.reviews", data, _products_reviews, ttl.REVIEWS_CACHE)


# ---- products/suggestions ------------------------------------------------------------

def _products_suggestions(query, limit):
    rows = parsers.parse_suggestions(fetch_suggestions(query, limit))[:limit]
    return {"query": query, "count": len(rows), "suggestions": rows}


def products_suggestions(**data):
    return cached_call("costco_australia.suggestions", data, _products_suggestions, ttl.SUGGESTIONS_CACHE)


# ---- categories/list -----------------------------------------------------------------

_tree_pool = None
_tree_pool_lock = threading.Lock()


def _pool():
    """The long-lived workers that read the department trees side by side.
    Long-lived, so each worker keeps its own HTTP session between calls."""
    global _tree_pool
    with _tree_pool_lock:
        if _tree_pool is None:
            _tree_pool = ThreadPoolExecutor(max_workers=TREE_WORKERS, thread_name_prefix="costco-au-tree")
        return _tree_pool


def _department_tree(code):
    """One department with everything under it; None for a number the site does not use."""
    try:
        return parsers.parse_category_tree(fetch_category_tree(code))
    except CostcoAustraliaNotFound:
        return None


def _departments():
    """Every department's tree. The site has no working call for all of
    them at once (fetch.py), so each possible department code is asked
    for: the ones that exist answer their tree, the rest a quick 404. Any
    other failure fails the whole answer, so a partial tree is never cached."""
    trees = fanout.on_pool(_pool(), [partial(_department_tree, code) for code in department_codes()])
    return [t for t in trees if t and t["code"]]


def _categories_list(category):
    if not category:
        departments = _departments()
        return {
            "departments_count": len(departments),
            "categories_count": parsers.count_categories(departments),
            "categories": departments,
        }
    record = _category(category)
    tree = parsers.parse_category_tree(fetch_category_tree(category))
    products_count = record["products_count"]
    if products_count is None:              # a department landing page lists no products of its own
        products_count = fetch_count(search_expression(None, "relevance", [_all_under(category)]))
    return {
        **{k: v for k, v in record.items() if k != "subcategories"},
        "products_count": products_count,
        "subcategories_count": parsers.count_categories(tree["subcategories"]),
        "subcategories": tree["subcategories"],
    }


def categories_list(**data):
    return cached_call("costco_australia.categories", data, _categories_list, ttl.CATEGORIES_CACHE)


# ---- stores --------------------------------------------------------------------------

def _fetch_warehouses(location):
    """Every warehouse; with a location, nearest first with distances."""
    if not location:
        rows = parsers.parse_stores(fetch_stores())
        rows["stores"].sort(key=lambda s: (s["name"] or "").lower())
        return rows
    if "lat" in location:
        return parsers.parse_stores(fetch_stores(latitude=location["lat"], longitude=location["lng"]),
                                    with_distance=True)
    text = location.get("postcode") or location.get("suburb")
    rows = parsers.parse_stores(fetch_stores(query=text), with_distance=True)
    if rows["origin"] is None:
        raise ValueError(f"location: the site could not place {text!r}; give an Australian postcode, "
                         "a suburb (Bondi NSW) or lat,lng.")
    return rows


def _warehouses(location=None):
    return cached_call("costco_australia.stores", {"location": location}, _fetch_warehouses, ttl.STORES_CACHE)


def _location_label(location):
    if not location:
        return None
    if "lat" in location:
        return f"{location['lat']},{location['lng']}"
    return location.get("postcode") or location.get("suburb")


def stores_list(location=None):
    rows = _warehouses(location)
    return {
        "location": _location_label(location),
        "origin": rows["origin"],
        "count": len(rows["stores"]),
        "stores": rows["stores"],
    }


def stores_details(store):
    wanted = store.casefold()
    for row in _warehouses()["stores"]:
        if (row["code"] or "") == store or (row["name"] or "").casefold() == wanted:
            return row
    raise CostcoAustraliaNotFound(f"store {store} not found")
