"""Costco Australia transport: plain curl_cffi with chrome TLS on the JSON
API the storefront's own JavaScript calls. No browser, no login, no cookie
and no token. One session per request thread on the exit
config.COSTCO_AUSTRALIA_EXIT names (the server's own IP in production); a
retryable failure drops the session, so the retry rides a fresh connection.

Two upstreams (every surface answered 200 on 2026-11-19):

  the store API   https://www.costco.com.au/rest/v2/australia  (send
                  Accept: application/json; the default answer is XML)
    products/search?query=<text>:<sort>:<Facet>:<Value>...&currentPage&pageSize
                  a result page with facets. currentPage is 0-based and
                  pageSize is capped at 100 by the site. The facet
                  allCategories:<code> lists a category with everything
                  under it (a department, a campaign such as hot-buys, a
                  brand category).
    products/search?category=<code>
                  the category's own record (name, link, parents, direct
                  subcategories, meta text). Unknown code: 404.
    catalogs/australiaProductCatalog/Online/categories/<code>
                  the category with its whole subtree, in ~0.5 s. Unknown
                  code: 404. The root category `costco` would answer every
                  department in one call (1,396 categories) but takes ~10 s
                  and failed 4 times in 5 with a server-side
                  StackOverflowError (reported as HTTP 400), so the tree is
                  read one department at a time (cos_1 ... cos_99; 28
                  exist, numbered up to 70).
    products/<code>?fields=FULL
                  one product. Unknown code: 404 with a JSON error.
    products/search/export?productCodes=a,b,c
                  listing rows for up to 30 codes in one call; unknown
                  codes are left out, and none found answers 204.
    products/suggestions?term&max
                  search suggestions.
    stores?fields=FULL&pageSize=100[&query=<postcode or suburb>|&latitude&longitude]
                  every warehouse in full (hours, services, fuel prices),
                  nearest first when a place is given. A place the site
                  cannot find is answered from the centre of Australia.
    `postcode=<4 digits>` on a product call fills the price, unit price
    and stock of Business Delivery items, which are empty without one.

  the review service  the reviews widget's own data calls, authorised by
                  the widget's display token in a header
    reviews.json   one page of reviews with the product's statistics
                  (limit at most 100)
    products.json  the review service's record of a product: brand,
                  barcodes, rating statistics. Unknown product: no results.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  CostcoAustraliaUpstreamError  transport failure / 5xx / unexpected body  retryable
  CostcoAustraliaBlocked        401 / 403 / 429 / a non-JSON page           retryable
  CostcoAustraliaBadRequest     the site rejected the parameters (400)      never retried
  CostcoAustraliaNotFound       unknown product / category                  never retried
"""
import threading

import config
import egress
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from costco_australia.refs import SITE
from scraper_utils import retry, sessions

IMPERSONATE = "chrome"
API_TIMEOUT = 25

API = SITE + "/rest/v2/australia"
CATALOG_CATEGORIES = API + "/catalogs/australiaProductCatalog/Online/categories"
MAX_DEPARTMENT_NUMBER = 99      # departments are cos_<n>: 28 of them on 2026-11-19, the highest cos_70
SITE_PARAMS = {"lang": "en_AU", "curr": "AUD"}      # what the storefront adds to every call
MAX_PAGE_SIZE = 100

REVIEWS_BASE = "https://apps.bazaarvoice.com/bfd/v1/clients/costco-au/api-products/cv2/resources/data"
REVIEWS_LOCALES = "en*,en_AU"
MAX_REVIEWS_PAGE = 100

JSON_HEADERS = {"accept": "application/json"}


class CostcoAustraliaUpstreamError(UpstreamError):
    """Transport failure, 5xx or an unexpected payload — retryable."""


class CostcoAustraliaBlocked(CostcoAustraliaUpstreamError, Blocked):
    """401 / 403 / 429, or a page where JSON was expected — retryable."""


class CostcoAustraliaBadRequest(BadRequest):
    """The site rejected the parameters (HTTP 400) — never retried."""


class CostcoAustraliaNotFound(NotFound):
    """No such product or category — never retried."""


# ---- sessions ------------------------------------------------------------------------

# One curl session per request thread (a curl handle must not be shared
# across threads). The calls are stateless: no cookie or token is carried.
_local = threading.local()


def _session():
    return sessions.thread_session(
        _local, lambda: egress.exit_session(config.COSTCO_AUSTRALIA_EXIT, impersonate=IMPERSONATE))


def _drop_session():
    """Close the thread's session so the retry rides a fresh connection / exit."""
    sessions.drop(_local)


def _retrying(fn):
    """fn() under the shared retry policy: only CostcoAustraliaUpstreamError
    (Blocked included) is retried; NotFound and BadRequest surface at once."""
    return retry.run(fn, config, CostcoAustraliaUpstreamError, lambda e: _drop_session())


# ---- one request ---------------------------------------------------------------------

def _error_message(resp):
    """The first message of the store API's {"errors": [{message, type}]} body."""
    try:
        errors = resp.json().get("errors")
        return str(errors[0].get("message") or "")[:200]
    except Exception:
        return ""


def _json(resp, label):
    try:
        payload = resp.json()
    except Exception:
        raise CostcoAustraliaBlocked(f"non-JSON answer for {label}: {resp.text[:120]!r}")
    if not isinstance(payload, dict):
        raise CostcoAustraliaUpstreamError(f"unexpected payload for {label}: {resp.text[:120]!r}")
    return payload


def _classify(resp, label):
    """A 200 passes. 404 with the API's JSON error -> NotFound; 400 ->
    BadRequest with the site's message; 401 / 403 / 429 -> Blocked;
    anything else (a 404 page of the edge included) -> upstream error."""
    status = resp.status_code
    if status == 200:
        return
    if status == 404 and "json" in (resp.headers.get("content-type") or ""):
        raise CostcoAustraliaNotFound(f"{label} not found")
    if status == 400:
        raise CostcoAustraliaBadRequest(_error_message(resp) or f"the site rejected {label}")
    if status in (401, 403, 429):
        raise CostcoAustraliaBlocked(f"HTTP {status} for {label}")
    raise CostcoAustraliaUpstreamError(f"HTTP {status} for {label}")


def _get(url, params, label, headers=JSON_HEADERS, timeout=API_TIMEOUT, empty=()):
    """GET -> the decoded JSON object. A status listed in `empty` answers
    None (the batch call says 204 when it found none of the codes)."""
    def once():
        try:
            resp = _session().get(url, params=params, headers=headers, timeout=timeout)
        except Exception as e:
            raise CostcoAustraliaUpstreamError(f"request failed for {label}: {type(e).__name__}: {e}")
        if resp.status_code in empty:
            return None
        _classify(resp, label)
        return _json(resp, label)
    return _retrying(once)


def _api(path, params, label, **kw):
    return _get(API + path, {**params, **SITE_PARAMS}, label, **kw)


# ---- the store API -------------------------------------------------------------------

def search_expression(text=None, sort="relevance", facets=()):
    """The site's query expression: <text>:<sort>:<Facet>:<Value>:... (an
    empty text lists everything the facets select)."""
    parts = [(text or "").replace(":", " ").strip(), sort or "relevance"]
    for name, value in facets:
        parts += [str(name), str(value)]
    return ":".join(parts)


def fetch_search(expression, page, limit, postcode=None):
    """One result page (page is 1-based here, 0-based upstream)."""
    params = {"query": expression, "fields": "FULL", "currentPage": max(page - 1, 0),
              "pageSize": min(limit, MAX_PAGE_SIZE)}
    if postcode:
        params["postcode"] = postcode
    payload = _api("/products/search", params, f"search {expression!r}")
    if "pagination" not in payload:
        raise CostcoAustraliaUpstreamError(f"search {expression!r} answered no pagination: {str(payload)[:120]!r}")
    return payload


def fetch_count(expression):
    """How many products an expression selects (no rows, no facets)."""
    params = {"query": expression, "fields": "pagination(FULL)", "currentPage": 0, "pageSize": 1}
    payload = _api("/products/search", params, f"count {expression!r}")
    total = (payload.get("pagination") or {}).get("totalResults")
    return total if isinstance(total, int) else 0


def fetch_facet(expression, name):
    """The facets of an expression's result, without its rows (used to read
    how the site writes a facet value)."""
    params = {"query": expression, "fields": "facets(FULL),pagination(FULL)", "currentPage": 0, "pageSize": 1}
    return _api("/products/search", params, f"{name} facet of {expression!r}")


def fetch_category(code):
    """A category's own record: name, link, parents, direct subcategories,
    meta text and, unless it is a department landing page, its product count."""
    params = {"category": code, "fields": "category(FULL),subCategories(FULL),pagination(FULL)", "pageSize": 1}
    payload = _api("/products/search", params, f"category {code}")
    if not isinstance(payload.get("category"), dict):
        raise CostcoAustraliaUpstreamError(f"category {code} answered no category: {str(payload)[:120]!r}")
    return payload


def department_codes():
    """Every code a department can have (most do not exist and answer 404)."""
    return [f"cos_{n}" for n in range(1, MAX_DEPARTMENT_NUMBER + 1)]


def fetch_category_tree(code):
    """A category with its whole subtree. Unknown code -> NotFound."""
    payload = _get(f"{CATALOG_CATEGORIES}/{code}", dict(SITE_PARAMS), f"category {code}")
    if "id" not in payload:
        raise CostcoAustraliaUpstreamError(f"category tree {code} answered no id: {str(payload)[:120]!r}")
    return payload


def fetch_product(code, postcode=None):
    """One product in full. Unknown code -> NotFound."""
    params = {"fields": "FULL"}
    if postcode:
        params["postcode"] = postcode
    payload = _api(f"/products/{code}", params, f"product {code}")
    if "code" not in payload:
        raise CostcoAustraliaUpstreamError(f"product {code} answered no code: {str(payload)[:120]!r}")
    return payload


def fetch_products(codes, postcode=None):
    """Listing rows for up to 30 codes in one call -> the raw rows the site
    knows, in the order asked (unknown codes are simply absent)."""
    params = {"productCodes": ",".join(codes), "fields": "FULL"}
    if postcode:
        params["postcode"] = postcode
    payload = _api("/products/search/export", params, f"products {','.join(codes)[:60]}", empty=(204,))
    rows = (payload or {}).get("product")
    return rows if isinstance(rows, list) else []


def fetch_suggestions(term, limit):
    payload = _api("/products/suggestions", {"term": term, "max": limit}, f"suggestions {term!r}")
    return payload


def fetch_stores(query=None, latitude=None, longitude=None):
    """Every warehouse in full, nearest first when a place is given."""
    params = {"fields": "FULL", "pageSize": 100}
    if latitude is not None and longitude is not None:
        params.update({"latitude": latitude, "longitude": longitude})
    elif query:
        params["query"] = query
    payload = _api("/stores", params, "stores")
    if not isinstance(payload.get("stores"), list):
        raise CostcoAustraliaUpstreamError(f"stores answered no list: {str(payload)[:120]!r}")
    return payload


# ---- the review service --------------------------------------------------------------

def _reviews_headers():
    return {
        "accept": "application/json, text/plain, */*",
        "bv-bfd-token": config.COSTCO_AUSTRALIA_REVIEWS_TOKEN,
        "origin": SITE,
        "referer": SITE + "/",
    }


def _reviews_get(resource, params, label):
    payload = _get(f"{REVIEWS_BASE}/{resource}", params, label, headers=_reviews_headers())
    body = payload.get("response")
    if not isinstance(body, dict) or "Results" not in body:
        raise CostcoAustraliaUpstreamError(f"unexpected {label} payload: {str(payload)[:160]!r}")
    if body.get("HasErrors"):
        raise CostcoAustraliaUpstreamError(f"{label} error: {str(body.get('Errors'))[:200]}")
    return body


def _display_code():
    """The widget's display code, e.g. "12402-en_au", from its token "12402,main_site,en_AU"."""
    parts = [p.strip() for p in config.COSTCO_AUSTRALIA_REVIEWS_TOKEN.split(",")]
    return f"{parts[0]}-{parts[-1].lower()}"


def fetch_reviews(code, limit, offset, sort, rating=None):
    """One page of a product's reviews plus its review statistics. A
    product without reviews (or unknown to the service) answers no rows."""
    params = [("resource", "reviews"), ("action", "REVIEWS_N_STATS"),
              ("filter", f"productid:eq:{code}"), ("filter", f"contentlocale:eq:{REVIEWS_LOCALES}"),
              ("filter", "isratingsonly:eq:false")]
    if rating:
        params.append(("filter", f"rating:eq:{rating}"))
    params += [("filter_reviews", f"contentlocale:eq:{REVIEWS_LOCALES}"), ("include", "authors,products,comments"),
               ("filteredstats", "reviews"), ("Stats", "Reviews"), ("limit", min(limit, MAX_REVIEWS_PAGE)),
               ("offset", offset), ("limit_comments", 3), ("sort", sort), ("apiversion", "5.5"),
               ("displaycode", _display_code())]
    return _reviews_get("reviews.json", params, f"reviews {code}")


def fetch_review_product(code):
    """The review service's record of a product (brand, barcodes, rating
    statistics); a product it does not know answers no results."""
    params = [("resource", "products"), ("filter", f"id:eq:{code}"),
              ("filter_reviews", f"contentlocale:eq:{REVIEWS_LOCALES}"), ("stats", "reviews"),
              ("filteredstats", "reviews"), ("apiversion", "5.5"), ("displaycode", _display_code())]
    return _reviews_get("products.json", params, f"review record {code}")
