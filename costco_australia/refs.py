"""Reference resolvers, link builders and the choice tables for
/costco-australia/*.

ONE param per input: `product` accepts an item code OR any costco.com.au
product link; `category` a category code OR a category link; `store` a
warehouse name, a warehouse code OR a store-finder link. Resolvers raise
ValueError with a user-facing message (RefField turns it into a 400).

Identifier shapes (www.costco.com.au, 2026-11-19, 6,009 products walked):
  * product   the item code, the last segment of a product link
              (/<category path>/<name>/p/<code>). 5,335 are digits only
              (255146), 626 end in _BD (207732_BD: the Business Delivery
              copy of an item), a variant carries its parent's code and a
              suffix (1901732-TLBLK) and 48 are slugs (wedding-cake,
              Bendon-Women-Brief). Codes are case-sensitive: 207732_bd is
              unknown to the site.
  * category  cos_<n>[.<n>[.<n>]] for the department tree (cos_2.4.4),
              a slug for a campaign (hot-buys, new, last-chance) or a brand
              (KIRKLAND-SIGNATURE); the last segment of a category link
              (/<path>/c/<code>). Case-sensitive as well, except the cos_
              prefix, which is always lower case.
  * store     the warehouse name is the site's own key ("Marsden Park",
              /store-finder/Marsden%20Park); each warehouse also has a
              three-digit code (109) that the site prints but does not
              accept as a key, so the API maps it through the list.
"""
import re
from urllib.parse import quote, unquote, urlparse

SITE = "https://www.costco.com.au"
HOSTS = ("costco.com.au",)

PRODUCT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
CATEGORY_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_COS_RE = re.compile(r"^cos_[0-9]+(\.[0-9]+)*$", re.I)
WAREHOUSE_CODE_RE = re.compile(r"^[0-9]{2,4}$")
_STORE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,59}$")
POSTCODE_RE = re.compile(r"^[0-9]{4}$")

MAX_BULK_PRODUCTS = 30      # the site's own cap: "cannot exceed 30 products" (2026-11-19)

_PRODUCT_HELP = ("Must be a Costco item code (255146, 207732_BD) or a costco.com.au product link "
                 "(https://www.costco.com.au/.../p/255146).")
_CATEGORY_HELP = ("Must be a category code (cos_2.4.4, hot-buys, KIRKLAND-SIGNATURE) or a costco.com.au "
                  "category link (https://www.costco.com.au/.../c/cos_2.4.4).")
_STORE_HELP = ("Must be a warehouse name (Marsden Park), a warehouse code (109) or a costco.com.au "
               "store-finder link.")


def _site_path(value):
    """The path of a costco.com.au link or of a bare site path (None when
    the value is neither); a link to another host raises ValueError."""
    if value.startswith("/") and not value.startswith("//"):
        return unquote(value.split("?", 1)[0].split("#", 1)[0])
    if not value.startswith(("http://", "https://", "//", "www.costco.com.au/", "costco.com.au/")):
        return None
    url = value
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in HOSTS):
        raise ValueError("Must be a costco.com.au link.")
    return unquote(parsed.path or "/")


def resolve_product(value):
    """Item code or product link -> the item code, as the site writes it."""
    value = (value or "").strip()
    path = _site_path(value)
    if path is not None:
        match = re.search(r"/p/([^/]+)/?$", path)
        code = match.group(1).strip() if match else ""
        if PRODUCT_CODE_RE.match(code):
            return code
        raise ValueError(_PRODUCT_HELP)
    if PRODUCT_CODE_RE.match(value):
        return value
    raise ValueError(_PRODUCT_HELP)


def resolve_products(value):
    """'255146, 207732_BD, <link>' -> the item codes in the order given,
    repeats dropped. At most MAX_BULK_PRODUCTS (the site's own cap)."""
    codes = []
    for raw in str(value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        code = resolve_product(item)
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError(_PRODUCT_HELP)
    if len(codes) > MAX_BULK_PRODUCTS:
        raise ValueError(f"At most {MAX_BULK_PRODUCTS} products per call.")
    return codes


def resolve_category(value):
    """Category code or category link -> the category code."""
    value = (value or "").strip()
    path = _site_path(value)
    if path is not None:
        match = None if "/p/" in path else re.search(r"/c/([^/]+)/?$", path)
        code = match.group(1).strip() if match else ""
        if not CATEGORY_CODE_RE.match(code):
            raise ValueError(_CATEGORY_HELP)
    elif CATEGORY_CODE_RE.match(value):
        code = value
    else:
        raise ValueError(_CATEGORY_HELP)
    return code.lower() if _COS_RE.match(code) else code


def resolve_store(value):
    """Warehouse name, warehouse code or store-finder link -> the code
    (digits) or the name, which api.py matches against the warehouse list."""
    value = " ".join((value or "").split())
    path = _site_path(value)
    if path is not None:
        match = re.search(r"/(?:store-finder|store)/(?:store/)?([^/]+)/?$", path)
        name = " ".join(match.group(1).split()) if match else ""
        if _STORE_NAME_RE.match(name):
            return name
        raise ValueError(_STORE_HELP)
    if WAREHOUSE_CODE_RE.match(value) or _STORE_NAME_RE.match(value):
        return value
    raise ValueError(_STORE_HELP)


# ---- links -------------------------------------------------------------------

def absolute_link(href):
    """A path the site returns ("/Appliances/c/cos_2", "/medias/...jpg", a
    name with spaces) or an absolute URL -> an absolute, encoded link."""
    if not href or not isinstance(href, str):
        return None
    href = href.strip()
    if not href:
        return None
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://www.costco.com.au"):
        return "https" + href[4:]
    if href.startswith(("http://", "https://")):
        return href
    return SITE + quote("/" + href.lstrip("/"), safe="/:?&=%#+,;@()!*'~$-._")


def product_link(code):
    return f"{SITE}/p/{quote(code, safe='')}" if code else None


def category_link(code):
    return f"{SITE}/c/{quote(code, safe='')}" if code else None


def store_link(name):
    return f"{SITE}/store-finder/{quote(name, safe='')}" if name else None


# ---- choice tables ------------------------------------------------------------

# public sort name -> the site's sort code (the `sorts` list of a search
# answer on 2026-11-19; each one answered in that order)
SORTS = {
    "relevance": "relevance",
    "price_asc": "price-asc",
    "price_desc": "price-desc",
    "name_asc": "name-asc",
    "name_desc": "name-desc",
    "top_sellers": "sellCount-desc",
    "top_rated": "ratings-desc",
}

# public deal type -> the site's campaign category (2026-11-19: Hot Buys
# 451 products, What's New 199, Last Chance 86)
DEAL_TYPES = {
    "hot_buys": "hot-buys",
    "new": "new",
    "last_chance": "last-chance",
}

# public review sort -> the review service's sort expression
REVIEW_SORTS = {
    "newest": "submissiontime:desc",
    "oldest": "submissiontime:asc",
    "highest_rating": "rating:desc",
    "lowest_rating": "rating:asc",
    "most_helpful": "helpfulness:desc,submissiontime:desc",
}

AU_STATES = ("ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA")
