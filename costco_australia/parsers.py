"""Raw store-API / review-service JSON -> clean dicts. Pure functions, no
I/O; every parser tolerates a partial payload.

Fixtures (fixtures/, captured 2026-11-19 from www.costco.com.au and its
review service, untrimmed):
  search_*.json            products/search result pages (text query, typo,
                           a nonsense word, past the end, a campaign, a
                           category, an unknown category, Business
                           Delivery with a postcode)
  category_*.json          products/search?category=<code> records
  category_tree_*.json     catalog category subtrees (a department, a campaign, 404)
  product_*.json           products/<code> (plain, with feature tags,
                           variants parent and child, Business Delivery
                           with and without a postcode, hidden price,
                           tyre, discounted, 404)
  export_*.json            products/search/export batches
  suggestions_*.json       products/suggestions
  stores*.json, store_*.json   the warehouse list and single warehouses
  reviews_*.json           review pages; bv_product_*.json review-service
                           product records

Shapes the endpoints share:
  * product row      parse_product_row: one product of a listing
  * product          parse_product: a product in full (a row plus
                     description, images, specifications, variants...)
  * pricing          price = what is paid now, regular_price = before the
                     current saving, discount_amount = the saving. A price
                     the site does not show is null with a reason in
                     price_hidden_reason.
"""
import re

from costco_australia import refs
from scraper_utils.dates import iso_day, iso_utc
from scraper_utils.text import clean, html_to_text, text_list
from scraper_utils.values import amount, as_dict, as_list, bool_or_none, count, g, num, rating, to_int

CURRENCY = "AUD"

# The review service stores this number for every product whose feed
# barcode it rejected (the record then also carries an INVALID_EAN
# attribute): 10 of 59 sampled records on 2026-11-19. It is not a barcode.
PLACEHOLDER_BARCODE = "400010076369"

STOCK_STATUS = {"inStock": "in_stock", "lowStock": "low_stock", "outOfStock": "out_of_stock"}

# The site's unit codes for a unit price, each checked against a product's
# pack size and price on 2026-11-19 (Dettol 2 x 750 ml at $16.98 shows
# $1.14 = per 100 ml). A code not listed here is passed through as it is.
UNIT_LABELS = {
    "EA": "each", "PR": "pair", "KG": "kg", "HG": "100g", "TG": "10g", "LT": "litre", "ML": "100ml",
    "MA": "10ml", "MT": "metre", "HN": "100 units", "HS": "100 sheets",
}

WEEKDAYS = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday",
            "Sat": "Saturday", "Sun": "Sunday"}

# the site's service codes -> (type, name), in Australian spelling like the site's own pages
SERVICES = {"TIRE_CENTER": ("tyre_centre", "Tyre Centre"), "FOOD_COURT": ("food_court", "Food Court"),
            "GAS_STATION": ("fuel_station", "Fuel Station"), "HEARING_AIDS": ("hearing_aids", "Hearing Aids"),
            "OPTICAL": ("optical", "Optical")}

# The point the site answers a store search from when it cannot place the
# text it was given: the centre of Australia.
UNPLACED_ORIGIN = (-25.274398, 133.775136)


# ---- small pieces --------------------------------------------------------------------

def _money(node):
    """A price node {value, currencyIso, ...} -> the amount."""
    return amount(g(node, "value"))


def _flag(value):
    """A real bool as it is; a missing flag is false, as on the site."""
    return bool(value) if isinstance(value, bool) else False


def _stock(node):
    node = as_dict(node)
    status = STOCK_STATUS.get(node.get("stockLevelStatus"))
    return {
        "status": status,
        "is_in_stock": None if status is None else status != "out_of_stock",
        "level": count(node.get("stockLevel")) if "stockLevel" in node else None,
    }


def _badge(entry):
    """A decal -> its label: the alt text, else the words of the image name
    ("AU-decal-trade-in-available" -> "Trade In Available")."""
    value = as_dict(g(entry, "value"))
    label = clean(value.get("altText"))
    if label and label.lower() != "null":       # most decals carry the text "null" as their alt text
        return label
    slug = str(value.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"^(AU-)?decal-", "", slug, flags=re.I)
    words = [w for w in re.split(r"[-_]+", slug) if w]
    return " ".join(w if w != w.lower() else w.capitalize() for w in words) or None


def _badges(product):
    entries = sorted(as_list(product.get("decalData")), key=lambda e: to_int(g(e, "value", "position")) or 0)
    return text_list([_badge(e) for e in entries])


def _tyre(product):
    """tireSummary [{name, frontValue}] -> {size, load_index, speed_rating, is_run_flat}."""
    rows = {clean(r.get("name")): clean(r.get("frontValue")) for r in as_list(product.get("tireSummary"))
            if isinstance(r, dict)}
    if not rows:
        return None
    run_flat = rows.get("Runflat Tire")
    return {
        "size": rows.get("Tire Size"),
        "load_index": to_int(rows.get("Load")),
        "speed_rating": rows.get("Speed Rating"),
        "is_run_flat": {"Y": True, "N": False}.get(run_flat),
    }


def _order_limits(product):
    mode = clean(product.get("maxOrderQuantityMode"))
    period = to_int(product.get("rollingTimePeriod"))
    return {
        "min_quantity": to_int(product.get("minOrderQuantity")),
        "max_quantity": to_int(product.get("maxOrderQuantity")),
        "multiple_of": to_int(product.get("modulusItemQuantity")),
        "max_quantity_applies": mode.lower() if mode else None,
        "rolling_period_days": period or None,
        "starts_at": iso_utc(product.get("maxOrderQuantityStartDate")),
        "ends_at": iso_utc(product.get("maxOrderQuantityEndDate")),
    }


def _price_hidden_reason(product, price, postcode, listing, members_only):
    """Why a product carries no price: members only (the site shows it
    after sign-in), a Business Delivery item without a postcode or outside
    its delivery area, or a tyre, whose price is only on the product itself."""
    if price is not None:
        return None
    if members_only:
        return "members_only"
    if product.get("businessDelivery"):
        return "not_delivered_to_postcode" if postcode else "postcode_required"
    if product.get("tire") and listing:
        return "shown_on_product_only"
    return "not_published"


def parse_pricing(product, postcode=None, listing=False, members_only_prices=True):
    """The pricing block of a product row or product.

    The site shows some prices only to signed-in members (hidePriceValue:
    all liquor and some sale prices). Search and product answers leave
    those out; the batch call returns them all the same, flagged. With
    members_only_prices=False such a price is dropped here too."""
    product = as_dict(product)
    members_only = _flag(product.get("hidePriceValue")) or _flag(product.get("warehouseHidePriceValue"))
    if members_only and not members_only_prices:
        product = {k: v for k, v in product.items()
                   if k not in ("price", "basePrice", "discountPrice", "couponDiscount", "pricePerUnit", "priceRange")}
    price = _money(product.get("price"))
    regular = _money(product.get("basePrice"))
    discount = _money(product.get("discountPrice"))
    if discount is None:
        discount = amount(g(product, "couponDiscount", "discountValue"))
    discount = discount or None
    if regular is None:
        regular = price
    percent = None
    if discount and regular:
        percent = round(discount / regular * 100, 1)
    unit_code = clean(product.get("unitType"))
    unit_price = _money(product.get("pricePerUnit"))
    price_type = clean(g(product, "price", "priceType"))
    return {
        "price": price,
        "regular_price": regular,
        "discount_amount": discount,
        "discount_percent": percent,
        "discount_starts_at": iso_utc(product.get("discountStartDate") or g(product, "couponDiscount", "discountStartDate")),
        "discount_ends_at": iso_utc(product.get("discountEndDate") or g(product, "couponDiscount", "discountEndDate")),
        "currency": clean(g(product, "price", "currencyIso")) or CURRENCY,
        "is_from_price": price_type == "FROM" if price_type else None,
        "min_price": _money(g(product, "priceRange", "minPrice")),
        "max_price": _money(g(product, "priceRange", "maxPrice")),
        "unit_price": unit_price,
        "unit_price_unit": (UNIT_LABELS.get(unit_code, unit_code) if unit_price is not None else None),
        "is_members_only_price": members_only,
        "is_price_hidden": price is None,
        "price_hidden_reason": _price_hidden_reason(product, price, postcode, listing, members_only),
    }


def _row_image(product):
    """The listing image of a row: the "product" size, else the first one."""
    images = [i for i in as_list(product.get("images")) if isinstance(i, dict) and i.get("url")]
    for wanted in ("product", "zoom", "superZoom", "results", "carousel", "thumbnail"):
        for image in images:
            if image.get("format") == wanted:
                return refs.absolute_link(image["url"])
    plain = [i for i in images if "webp" not in str(i.get("format")) and i.get("format") != "video"]
    return refs.absolute_link((plain or images)[0]["url"]) if (plain or images) else None


def _flags(product):
    return {
        "is_business_delivery": _flag(product.get("businessDelivery")),
        "is_tyre": _flag(product.get("tire")),
        "has_variants": _flag(product.get("isBaseProduct")) or _flag(product.get("multidimensional")),
        "is_purchasable_online": _flag(product.get("purchasable")),
        "is_preorder": _flag(product.get("availableForPreorder")),
        "is_shipping_included": _flag(product.get("shippingIncluded")),
        "is_pickup_available": _flag(product.get("availableForPickup")),
        "is_warehouse_pickup_only": _flag(product.get("warehousePickupOnly")),
        "is_members_only": _flag(product.get("membershipRestrictionApplied")),
        "is_age_restricted": _flag(product.get("requiresAgeVerification")) or _flag(product.get("ageRestricted")),
    }


def _identity(product):
    code = clean(product.get("code"))
    return {
        "code": code,
        "name": clean(product.get("name")) or clean(product.get("englishName")),
        "link": refs.absolute_link(product.get("url")) or refs.product_link(code),
    }


def _row_rating(product):
    reviews = count(product.get("numberOfReviews"))
    return {"average": rating(product.get("averageRating")) if reviews else None, "review_count": reviews or 0}


def _colours(product):
    out = []
    for colour in as_list(product.get("variantColours")):
        if isinstance(colour, dict) and clean(colour.get("name")):
            out.append({"name": clean(colour.get("name")), "hex": clean(colour.get("hexcode")),
                        "link": refs.absolute_link(colour.get("url"))})
    return out


# ---- listings ------------------------------------------------------------------------

def parse_product_row(product, postcode=None, members_only_prices=True):
    """One product of a listing (search, category, deals, batch)."""
    product = as_dict(product)
    stock = _stock(product.get("stock"))
    return {
        **_identity(product),
        "image": _row_image(product),
        "pricing": parse_pricing(product, postcode, listing=True, members_only_prices=members_only_prices),
        "rating": _row_rating(product),
        "stock_status": stock["status"],
        "is_in_stock": stock["is_in_stock"],
        "badges": _badges(product),
        **_flags(product),
        "order_limits": _order_limits(product),
        "tyre": _tyre(product),
        "colours": _colours(product),
    }


def parse_facets(payload):
    """The facets of a result page: `name` is what the `filter` param takes
    ("Brand:Breville"), `label` what the site prints."""
    out = []
    for facet in as_list(as_dict(payload).get("facets")):
        if not isinstance(facet, dict) or not clean(facet.get("name")):
            continue
        if facet.get("name") == "idVerificationEnabled":       # an internal switch, not a shopper's filter
            continue
        values = []
        for value in as_list(facet.get("values")):
            code = clean(g(value, "code")) or clean(g(value, "name"))
            if code is None:
                continue
            values.append({"value": code, "label": clean(g(value, "name")) or code,
                           "count": count(g(value, "count")), "is_selected": _flag(g(value, "selected"))})
        if values:
            out.append({"name": clean(facet.get("name")), "label": clean(facet.get("displayName")),
                        "is_multi_select": _flag(facet.get("multiSelect")), "values": values})
    return out


def _pagination(payload, page, limit):
    node = as_dict(as_dict(payload).get("pagination"))
    total = count(node.get("totalResults")) or 0
    per_page = to_int(node.get("pageSize")) or limit
    pages = to_int(node.get("totalPages"))
    if pages is None:
        pages = -(-total // per_page) if per_page else 0
    return {"page": page, "items_per_page": per_page, "total_pages": pages, "total_count": total}


def parse_listing(payload, page, limit, postcode=None):
    """A products/search answer -> {results, facets, spelling_suggestion,
    pagination}. A page past the end is an empty page."""
    payload = as_dict(payload)
    rows = [parse_product_row(p, postcode) for p in as_list(payload.get("products")) if isinstance(p, dict)]
    suggestion = clean(g(payload, "spellingSuggestion", "suggestion"))
    if suggestion and re.search(r"[{}$!]", suggestion):     # a listing without text answers its raw index query here
        suggestion = None
    return {
        "spelling_suggestion": suggestion,
        "results": rows,
        "facets": parse_facets(payload),
        "pagination": _pagination(payload, page, limit),
    }


def parse_batch(rows, codes, postcode=None, members_only_prices=True):
    """products/search/export rows -> the products in the order asked and
    the codes the site did not know."""
    by_code = {}
    for row in as_list(rows):
        if isinstance(row, dict) and clean(row.get("code")):
            by_code[clean(row.get("code"))] = parse_product_row(row, postcode, members_only_prices)
    return {
        "products": [by_code[c] for c in codes if c in by_code],
        "not_found": [c for c in codes if c not in by_code],
    }


def parse_suggestions(payload):
    """products/suggestions -> the suggested searches (the site ends some
    with a stray comma)."""
    return text_list([str(g(s, "value") or "").strip(" ,") for s in as_list(as_dict(payload).get("suggestions"))])


# ---- product -------------------------------------------------------------------------

_IMAGE_SIZES = ("superZoom", "zoom", "product", "results", "carousel", "thumbnail", "cartIcon")


def parse_images(product):
    """images[] -> (gallery links, largest size of each picture in gallery
    order; video links)."""
    by_index, videos = {}, []
    for image in as_list(as_dict(product).get("images")):
        if not isinstance(image, dict) or not image.get("url"):
            continue
        fmt = str(image.get("format") or "")
        if fmt == "video":
            videos.append(refs.absolute_link(image["url"]))
            continue
        if fmt.endswith("-webp"):
            continue
        index = to_int(image.get("galleryIndex")) or 0
        rank = _IMAGE_SIZES.index(fmt) if fmt in _IMAGE_SIZES else len(_IMAGE_SIZES)
        best = by_index.get(index)
        if best is None or rank < best[0]:
            by_index[index] = (rank, refs.absolute_link(image["url"]))
    gallery = [by_index[i][1] for i in sorted(by_index)]
    return text_list(gallery), text_list(videos)


def parse_specifications(product):
    """classifications[].features[] -> [{name, value}] in the site's order."""
    out, seen = [], set()
    for group in as_list(as_dict(product).get("classifications")):
        for feature in as_list(g(group, "features")):
            name = clean(g(feature, "name"))
            values = text_list([html_to_text(str(v.get("value"))) for v in as_list(g(feature, "featureValues"))
                                if isinstance(v, dict) and v.get("value") is not None])
            if not name or not values or name in seen:
                continue
            seen.add(name)
            unit = clean(g(feature, "featureUnit", "symbol"))
            value = ", ".join(values)
            out.append({"name": name, "value": f"{value} {unit}" if unit else value})
    return out


def _category(node):
    code = clean(g(node, "code"))
    return {"code": code, "name": clean(g(node, "name")),
            "link": refs.absolute_link(g(node, "url")) or refs.category_link(code)}


def parse_product_categories(product):
    """The named categories a product sits in. The site also files every
    product under unnamed internal buckets (tpd-...), which are dropped."""
    out = []
    for node in as_list(as_dict(product).get("categories")):
        code, name = clean(g(node, "code")), clean(g(node, "name"))
        if not code or not name or name == code or code.startswith("tpd-"):
            continue
        out.append({**_category(node), "is_campaign": _flag(g(node, "campaign"))})
    return out


def _primary_category(product, categories):
    """The department category the product's own link runs through, else
    the deepest department category."""
    link = refs.absolute_link(as_dict(product).get("url")) or ""
    departments = [c for c in categories if c["code"].startswith("cos_")]
    for cat in sorted(departments, key=lambda c: -c["code"].count(".")):
        prefix = (cat["link"] or "").rsplit("/c/", 1)[0]
        if prefix and prefix != refs.SITE and link.startswith(prefix + "/"):
            return {k: cat[k] for k in ("code", "name", "link")}
    if departments:
        cat = max(departments, key=lambda c: c["code"].count("."))
        return {k: cat[k] for k in ("code", "name", "link")}
    return None


def _variant(option):
    stock = _stock(g(option, "stock"))
    options = {}
    for qualifier in as_list(g(option, "variantOptionQualifiers")):
        name, value = clean(g(qualifier, "name")), clean(g(qualifier, "value"))
        if name and value:
            options[name] = value
    code = clean(g(option, "code"))
    return {
        "code": code,
        "link": refs.absolute_link(g(option, "url")) or refs.product_link(code),
        "options": options,
        "price": _money(g(option, "priceData")),
        "currency": clean(g(option, "priceData", "currencyIso")) or CURRENCY,
        "stock_status": stock["status"],
        "stock_level": stock["level"],
        "is_in_stock": stock["is_in_stock"],
    }


def parse_variants(product):
    """The variants of a product: its own variantOptions, or for a variant
    its siblings (baseOptions[].options)."""
    product = as_dict(product)
    options = as_list(product.get("variantOptions"))
    if not options:
        for base in as_list(product.get("baseOptions")):
            options = as_list(g(base, "options"))
            if options:
                break
    return [_variant(o) for o in options if isinstance(o, dict) and g(o, "code")]


def _promotions(product):
    out = []
    for promo in as_list(as_dict(product).get("potentialPromotions")):
        if not isinstance(promo, dict):
            continue
        out.append({"code": clean(promo.get("code")), "description": clean(promo.get("description")),
                    "type": clean(promo.get("promotionType")), "ends_at": iso_utc(promo.get("endDate"))})
    return out


def _add_on_services(product):
    out = []
    for service in as_list(as_dict(product).get("addOnServices")):
        if not isinstance(service, dict):
            continue
        code = clean(service.get("code"))
        out.append({"code": code, "name": clean(service.get("name")),
                    "link": refs.absolute_link(service.get("url")) or refs.product_link(code),
                    "price": _money(service.get("priceData")), "is_optional": _flag(service.get("optional"))})
    return out


def _delivery(product):
    """The delivery notes of a product page and its delivery / returns text."""
    return {
        "notes": text_list([html_to_text(g(m, "value")) for m in as_list(as_dict(product).get("pdpMessages"))]),
        "delivery_and_returns": html_to_text(as_dict(product).get("returns")),
    }


def parse_review_record(body):
    """The review service's products.json answer -> {brand, barcodes,
    rating} for products/details (None when it does not know the product)."""
    record = g(body, "Results", 0)
    if not isinstance(record, dict):
        return None
    barcodes = []
    for key in ("EANs", "UPCs", "ISBNs"):
        barcodes += [str(v).strip() for v in as_list(record.get(key)) if str(v).strip().isdigit()]
    barcodes = [b for b in text_list(barcodes) if b != PLACEHOLDER_BARCODE]
    stats = as_dict(record.get("ReviewStatistics")) or as_dict(record.get("FilteredReviewStatistics"))
    return {
        "brand": clean(g(record, "Brand", "Name")),
        "barcodes": barcodes,
        "model_numbers": text_list(as_list(record.get("ModelNumbers")) + as_list(record.get("ManufacturerPartNumbers"))),
        "rating": parse_review_statistics(stats) if stats else None,
    }


_TAG_KEYWORD_RE = re.compile(r'<h5[^>]*class="[^"]*product-tag-keyword[^"]*"[^>]*>(.*?)</h5>', re.S | re.I)
_TAG_TEXT_RE = re.compile(r'<p[^>]*class="[^"]*product-tag-dsc[^"]*"[^>]*>(.*?)</p>', re.S | re.I)


def _highlights(product):
    """The feature tags some descriptions open with (a keyword and a short
    line each: "Beans" / "Grind right before") -> [{title, text}]."""
    html = as_dict(product).get("description")
    if not isinstance(html, str):
        return []
    titles = [html_to_text(t) for t in _TAG_KEYWORD_RE.findall(html)]
    texts = [html_to_text(t) for t in _TAG_TEXT_RE.findall(html)]
    if len(titles) != len(texts):
        return []
    return [{"title": clean(title), "text": clean(text)} for title, text in zip(titles, texts) if clean(title)]


def _description(product):
    """The description as plain text, without its feature tags (they are
    `highlights`), the "Description" heading and the repeated product name
    the site's markup opens it with."""
    html = as_dict(product).get("description")
    if not isinstance(html, str):
        return None
    if _highlights(product):
        html = _TAG_TEXT_RE.sub("", _TAG_KEYWORD_RE.sub("", html))
    text = html_to_text(html)
    if not text:
        return None
    name = clean(product.get("name"))
    lines = text.split("\n")
    while lines and (not lines[0].strip() or lines[0].strip().lower() == "description" or clean(lines[0]) == name):
        lines.pop(0)
    return "\n".join(lines).strip() or None


def _summary(product):
    """The summary card as plain text, one line per item (the site packs
    its items into neighbouring <span>s with nothing between them)."""
    html = as_dict(product).get("summary")
    if not isinstance(html, str):
        return None
    return html_to_text(re.sub(r"</span>\s*<span", "</span>\n<span", html))


def parse_product(product, review_record=None, postcode=None):
    """A products/<code> answer (plus the review service's record of the
    product, when it has one) -> the product in full."""
    product = as_dict(product)
    review_record = as_dict(review_record)
    images, videos = parse_images(product)
    specifications = parse_specifications(product)
    categories = parse_product_categories(product)
    by_name = {s["name"].lower(): s["value"] for s in specifications}
    brand = by_name.get("brand") or by_name.get("tire manufacturer")
    stock = _stock(product.get("stock"))
    row_rating = _row_rating(product)
    stats = as_dict(review_record.get("rating"))
    variants = parse_variants(product)
    base_code = clean(product.get("baseProduct"))
    return {
        **_identity(product),
        "brand": brand or review_record.get("brand"),
        "barcodes": review_record.get("barcodes") or [],
        "model_numbers": review_record.get("model_numbers") or [],
        "summary": _summary(product),
        "highlights": _highlights(product),
        "description": _description(product),
        "image": images[0] if images else _row_image(product),
        "images": images,
        "videos": videos,
        "pricing": parse_pricing(product, postcode),
        "rating": {
            "average": row_rating["average"] if row_rating["average"] is not None else stats.get("average"),
            "review_count": row_rating["review_count"] or stats.get("review_count") or 0,
            "distribution": stats.get("distribution"),
            "recommended_count": stats.get("recommended_count"),
            "not_recommended_count": stats.get("not_recommended_count"),
        },
        "stock": stock,
        "badges": _badges(product),
        **_flags(product),
        "category": _primary_category(product, categories),
        "categories": categories,
        "specifications": specifications,
        "parent_product_code": base_code if base_code and base_code != clean(product.get("code")) else None,
        "variants": variants,
        "promotions": _promotions(product),
        "add_on_services": _add_on_services(product),
        "delivery": _delivery(product),
        "order_limits": _order_limits(product),
        "tyre": _tyre(product),
        "weight": num(product.get("weight")) or None,
        "department_number": clean(product.get("departmentNumber")),
    }


# ---- categories ----------------------------------------------------------------------

def parse_category_tree(node):
    """A catalog category node -> {code, name, link, subcategories: [...]}."""
    node = as_dict(node)
    code = clean(node.get("id"))
    children = [parse_category_tree(c) for c in as_list(node.get("subcategories")) if isinstance(c, dict)]
    return {
        "code": code,
        "name": clean(node.get("name")),
        "link": refs.absolute_link(node.get("url")) or refs.category_link(code),
        "subcategories": [c for c in children if c["code"]],
    }


def count_categories(nodes):
    """How many categories a list of tree nodes holds, children included."""
    return sum(1 + count_categories(n.get("subcategories") or []) for n in nodes)


def parse_category(payload):
    """A products/search?category=<code> answer -> the category's own
    record. `products_count` is null for a department landing page, which
    lists subcategories instead of products."""
    payload = as_dict(payload)
    node = as_dict(payload.get("category"))
    total = g(payload, "pagination", "totalResults")
    return {
        **_category(node),
        "description": clean(node.get("metaDescription")),
        "image": refs.absolute_link(g(node, "image", "url")),
        "is_landing_page": _flag(node.get("landingPage")),
        "is_campaign": _flag(node.get("campaign")),
        "products_count": count(total) if total is not None else None,
        "parents": [_category(p) for p in as_list(node.get("supercategories")) if isinstance(p, dict) and g(p, "code")],
        "subcategories": [{**_category(s), "image": refs.absolute_link(g(s, "image", "url"))}
                          for s in as_list(payload.get("subCategories")) if isinstance(s, dict) and g(s, "code")],
    }


def parse_applied_filters(payload):
    """The refinements the site says it applied to a result page (its
    `breadcrumbs`): a facet name it does not know is silently left out."""
    out = []
    for crumb in as_list(as_dict(payload).get("breadcrumbs")):
        name, value = clean(g(crumb, "facetCode")), clean(g(crumb, "facetValueCode"))
        if name and value:
            out.append({"name": name, "value": value})
    return out


def facet_values(payload, name):
    """The values of one facet of a result page, as the site writes them."""
    for facet in as_list(as_dict(payload).get("facets")):
        if g(facet, "name") == name:
            return [str(g(v, "code") or g(v, "name")) for v in as_list(g(facet, "values")) if g(v, "code") or g(v, "name")]
    return []


# ---- stores --------------------------------------------------------------------------

def _clock(node):
    """{"formattedHour": "6:00 PM"} -> "18:00" (the site's own `hour` is on a 12-hour clock)."""
    text = clean(g(node, "formattedHour")) or ""
    match = re.match(r"^(\d{1,2}):(\d{2})\s*([AP]M)$", text, re.I)
    if not match:
        return None
    hour, minute, half = int(match.group(1)) % 12, match.group(2), match.group(3).upper()
    return f"{hour + (12 if half == 'PM' else 0):02d}:{minute}"


def _week(schedule):
    out = []
    for day in as_list(g(schedule, "weekDayOpeningList")):
        if not isinstance(day, dict):
            continue
        closed = _flag(day.get("closed"))
        out.append({"day": WEEKDAYS.get(clean(day.get("weekDay")), clean(day.get("weekDay"))),
                    "opens": None if closed else _clock(day.get("openingTime")),
                    "closes": None if closed else _clock(day.get("closingTime")),
                    "is_closed": closed})
    return out


def _special_days(schedule):
    out = []
    for day in as_list(g(schedule, "specialDayOpeningList")):
        if not isinstance(day, dict):
            continue
        closed = _flag(day.get("closed"))
        # formattedDate is the local day (dd/mm/yy); `date` is the same day stamped in UTC
        local = re.match(r"^(\d{2})/(\d{2})/(\d{2})$", clean(day.get("formattedDate")) or "")
        date = f"20{local.group(3)}-{local.group(2)}-{local.group(1)}" if local else iso_day(day.get("date"))
        out.append({"date": date, "name": clean(day.get("name")) or clean(day.get("comment")),
                    "opens": None if closed else _clock(day.get("openingTime")),
                    "closes": None if closed else _clock(day.get("closingTime")),
                    "is_closed": closed})
    return out


def _state(store):
    """The state of a warehouse: the last word of its town ("Marsden Park
    NSW"), else the suffix of its opening-hours schedule code."""
    town = clean(g(store, "address", "town")) or ""
    last = town.rsplit(" ", 1)[-1].upper() if town else ""
    if last in refs.AU_STATES:
        return last
    for code in (g(store, "openingHours", "code"), g(store, "gasStationHours", "code")):
        suffix = str(code or "").rsplit("-", 1)[-1].upper()
        if suffix in refs.AU_STATES:
            return suffix
    return None


def _suburb(store, state):
    town = clean(g(store, "address", "town"))
    if town and state and town.upper().endswith(" " + state):
        return town[: -len(state)].strip() or None
    return town


def _fuel_prices(store):
    out = []
    for fuel in as_list(as_dict(store).get("gasTypes")):
        name = clean(g(fuel, "name"))
        match = re.search(r"\d+(?:\.\d+)?", str(g(fuel, "price") or ""))
        if name:
            out.append({"fuel": name, "price_per_litre": float(match.group(0)) if match else None})
    return out


def _services(store):
    out = []
    for service in as_list(as_dict(store).get("availableServices")):
        if not isinstance(service, dict):
            continue
        kind = clean(service.get("type"))
        slug, name = SERVICES.get(kind, (kind.lower() if kind else None, kind))
        out.append({"type": slug, "name": name,
                    "phone": clean(service.get("phone")), "opening_hours": html_to_text(service.get("openingHours"))})
    return out


def _distance_km(store):
    text = clean(as_dict(store).get("formattedDistance")) or ""
    match = re.match(r"^([\d,]+(?:\.\d+)?)\s*km$", text, re.I)
    return float(match.group(1).replace(",", "")) if match else None


def parse_store(store, with_distance=False):
    """One warehouse of the stores list (the list carries every field)."""
    store = as_dict(store)
    address = as_dict(store.get("address"))
    state = _state(store)
    name = clean(store.get("name")) or clean(store.get("displayName"))
    fuel_prices = _fuel_prices(store)
    return {
        "code": clean(store.get("warehouseCode")),
        "name": name,
        "link": refs.store_link(name),
        "phone": clean(address.get("phone")),
        "email": clean(address.get("email")),
        "address": {
            "line1": clean(address.get("line1")),
            "line2": clean(address.get("line2")),
            "suburb": _suburb(store, state),
            "state": state,
            "postcode": clean(address.get("postalCode")),
            "country": clean(g(address, "country", "name")),
            "country_code": clean(g(address, "country", "isocode")),
            "formatted": clean(address.get("formattedAddress")),
        },
        "latitude": num(g(store, "geoPoint", "latitude")),
        "longitude": num(g(store, "geoPoint", "longitude")),
        "distance_km": _distance_km(store) if with_distance else None,
        "opening_hours": _week(store.get("openingHours")),
        "special_hours": _special_days(store.get("openingHours")),
        "hours_note": clean(store.get("operationalHoursTooltip")),
        "services": _services(store),
        "features": text_list([clean(g(e, "value")) for e in as_list(g(store, "features", "entry"))]),
        "has_fuel_station": bool(fuel_prices),
        "fuel": {
            "prices": fuel_prices,
            "currency": CURRENCY if fuel_prices else None,
            "opening_hours": _week(store.get("gasStationHours")),
            "special_hours": _special_days(store.get("gasStationHours")),
            "note": clean(store.get("warehouseInformation")),
        },
        "tyre_centre_phone": clean(store.get("tireServiceCenterPhone")),
        "is_pickup_available": _flag(store.get("availableForBopis")),
        "images": text_list([refs.absolute_link(g(i, "url")) for i in as_list(store.get("storeImages"))]),
    }


def parse_stores(payload, with_distance=False):
    """A stores answer -> {origin, stores}. `origin` is the point distances
    are measured from (None when no place was given, or when the site
    could not find the place and fell back to the centre of Australia)."""
    payload = as_dict(payload)
    lat, lng = num(payload.get("sourceLatitude")), num(payload.get("sourceLongitude"))
    placed = with_distance and lat is not None and lng is not None and (lat, lng) != UNPLACED_ORIGIN \
        and (lat, lng) != (0.0, 0.0)
    return {
        "origin": {"latitude": lat, "longitude": lng} if placed else None,
        "stores": [parse_store(s, with_distance=placed) for s in as_list(payload.get("stores")) if isinstance(s, dict)],
    }


# ---- reviews -------------------------------------------------------------------------

def _secondary(node, key):
    """{"Quality": {"Value": 5}} / {"Quality": {"AverageRating": 4.7}} -> {"quality": 5}."""
    out = {}
    for name, value in as_dict(node).items():
        score = rating(g(value, key))
        if score is not None:
            out[str(name).strip().lower().replace(" ", "_")] = score
    return out


def parse_review_statistics(stats):
    """A ReviewStatistics block -> the rating summary."""
    stats = as_dict(stats)
    distribution = {str(star): 0 for star in (5, 4, 3, 2, 1)}
    for row in as_list(stats.get("RatingDistribution")):
        star = to_int(g(row, "RatingValue"))
        if star in (1, 2, 3, 4, 5):
            distribution[str(star)] = count(g(row, "Count")) or 0
    total = count(stats.get("TotalReviewCount")) or 0
    return {
        "average": rating(stats.get("AverageOverallRating")) if total else None,
        "review_count": total,
        "ratings_only_count": count(stats.get("RatingsOnlyReviewCount")) or 0,
        "recommended_count": count(stats.get("RecommendedCount")) or 0,
        "not_recommended_count": count(stats.get("NotRecommendedCount")) or 0,
        "distribution": distribution,
        "secondary_ratings": _secondary(stats.get("SecondaryRatingsAverages"), "AverageRating"),
        "photo_count": count(stats.get("TotalPhotoCount")) or 0,
        "video_count": count(stats.get("TotalVideoCount")) or 0,
        "first_review_at": iso_utc(stats.get("FirstSubmissionTime")),
        "last_review_at": iso_utc(stats.get("LastSubmissionTime")),
    }


def _review_photos(review):
    out = []
    for photo in as_list(review.get("Photos")):
        sizes = as_dict(g(photo, "Sizes"))
        link = g(sizes, "large", "Url") or g(sizes, "normal", "Url") or g(sizes, "thumbnail", "Url")
        if link:
            out.append({"link": link, "caption": clean(g(photo, "Caption"))})
    return out


def _review_responses(review):
    out = []
    for response in as_list(review.get("ClientResponses")):
        if isinstance(response, dict) and clean(response.get("Response")):
            out.append({"from": clean(response.get("Department")) or clean(response.get("Name")),
                        "text": html_to_text(response.get("Response")), "date": iso_utc(response.get("Date"))})
    return out


def parse_review(review, authors=None):
    review = as_dict(review)
    author = as_dict(as_dict(authors).get(str(review.get("AuthorId"))))
    badges = [str(b) for b in as_list(review.get("BadgesOrder"))]
    return {
        "id": clean(str(review["Id"])) if review.get("Id") is not None else None,
        "title": clean(review.get("Title")),
        "text": html_to_text(review.get("ReviewText")),
        "rating": to_int(review.get("Rating")),
        "is_recommended": bool_or_none(review.get("IsRecommended")),
        "submitted_at": iso_utc(review.get("SubmissionTime")),
        "author": {
            "name": clean(review.get("UserNickname")) or clean(author.get("UserNickname")),
            "location": clean(review.get("UserLocation")) or clean(author.get("Location")),
            "review_count": count(g(author, "ReviewStatistics", "TotalReviewCount")),
        },
        "is_verified_purchaser": "verifiedPurchaser" in badges,
        "is_incentivised": "incentivizedReview" in badges or "IncentivizedReview" in badges,
        "is_staff": "Staff" in badges or "staff" in badges,
        "is_featured": _flag(review.get("IsFeatured")),
        "is_syndicated": _flag(review.get("IsSyndicated")),
        "helpful_votes": count(review.get("TotalPositiveFeedbackCount")) or 0,
        "not_helpful_votes": count(review.get("TotalNegativeFeedbackCount")) or 0,
        "secondary_ratings": _secondary(review.get("SecondaryRatings"), "Value"),
        "pros": clean(review.get("Pros")),
        "cons": clean(review.get("Cons")),
        "photos": _review_photos(review),
        "videos": text_list([g(v, "VideoUrl") for v in as_list(review.get("Videos"))]),
        "responses": _review_responses(review),
        "comment_count": count(review.get("TotalCommentCount")) or 0,
        "product_code": clean(review.get("ProductId")),
        "product_name": clean(review.get("OriginalProductName")),
    }


def parse_reviews(body, code):
    """A reviews.json answer -> {total, statistics, reviews}. `total` counts
    the reviews the page's filters select; `statistics` covers every
    rating of the product (the count the site prints, ratings without
    text included), whatever star filter was asked. It is None when the
    answer carries no product record (no review matched)."""
    body = as_dict(body)
    includes = as_dict(body.get("Includes"))
    product = as_dict(as_dict(includes.get("Products")).get(str(code)))
    if not product:
        products = [p for p in as_dict(includes.get("Products")).values() if isinstance(p, dict)]
        product = products[0] if products else {}
    stats = as_dict(product.get("ReviewStatistics")) or as_dict(product.get("FilteredReviewStatistics"))
    authors = as_dict(includes.get("Authors"))
    return {
        "total": count(body.get("TotalResults")) or 0,
        "statistics": parse_review_statistics(stats) if stats else None,
        "reviews": [parse_review(r, authors) for r in as_list(body.get("Results")) if isinstance(r, dict)],
    }
