"""Marshmallow request schemas for every /costco-australia/* route.

ONE param per input (never a sibling `url` / `id` pair):

    product    an item code (255146, 207732_BD) OR a costco.com.au product link
    products   up to 30 of those, comma-separated
    category   a category code (cos_2.4.4, hot-buys, KIRKLAND-SIGNATURE)
               OR a costco.com.au category link
    store      a warehouse name (Marsden Park), a warehouse code (109)
               OR a store-finder link
    location   a 4-digit postcode, a suburb ("Bondi", "Bondi NSW") or "lat,lng"

The listing routes (products/search, categories/products) share sort,
brand, `filter` (comma-separated "Facet:Value" refinements over the facets
a result page returns) and `postcode`, which fills the prices of Business
Delivery items. The site serves at most 100 products per page.
"""
from marshmallow import ValidationError, fields

from schema_fields import (BaseSchema, ChoiceField, LimitField, PageField, PositiveInt, QueryField, RefField,
                           StrippedString)
from costco_australia import refs
from scraper_utils.schema import max_length as _max_length

MAX_LIMIT = 100             # the site caps pageSize at 100 (a larger value is served as 100)
MAX_PAGE = 10000            # the whole catalog is 6,009 products; a page past the end is an empty page
MAX_REVIEWS_LIMIT = 100     # the review service refuses a limit above 100
MAX_REVIEWS_PAGE = 5000
MAX_SUGGESTIONS = 20
MAX_LOCATION_LENGTH = 80


class ProductRefField(RefField):
    resolver = staticmethod(refs.resolve_product)


class ProductsRefField(RefField):
    """'255146, 207732_BD, <link>' -> the item codes, in order, repeats dropped."""
    resolver = staticmethod(refs.resolve_products)


class CategoryRefField(RefField):
    resolver = staticmethod(refs.resolve_category)


class StoreRefField(RefField):
    resolver = staticmethod(refs.resolve_store)


class PostcodeField(StrippedString):
    """An Australian postcode, 4 digits."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        if value is None:
            return None
        if not refs.POSTCODE_RE.match(value):
            raise ValidationError("An Australian postcode has 4 digits (2000).")
        return value


class DefaultedChoice(ChoiceField):
    """A choice whose empty value means its default, as a missing one does
    (StrippedString hands back None for `?sort=`)."""

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        if value is None and isinstance(self.load_default, str):
            return self.load_default
        return value


class FacetFilterField(fields.Field):
    """'Facet:Value, Facet:Value' -> [(facet, value)] over the facets a
    result page returns (the facet's `name` and a value's `value`)."""

    def __init__(self, max_items=10, **kwargs):
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)
        self.max_items = max_items

    def _deserialize(self, value, attr, data, **kwargs):
        pairs = []
        for raw in str(value).split(","):
            item = raw.strip()
            if not item:
                continue
            name, sep, val = item.partition(":")
            name, val = name.strip(), val.strip()
            if not sep or not name or not val:
                raise ValidationError(f"'{item}' must be Facet:Value (e.g. Brand:Breville).")
            if ":" in val:
                raise ValidationError(f"'{item}': a facet value cannot contain a colon.")
            if (name, val) not in pairs:
                pairs.append((name, val))
        if not pairs:
            return None
        if len(pairs) > self.max_items:
            raise ValidationError(f"At most {self.max_items} refinements.")
        return pairs


class LocationField(StrippedString):
    """'lat,lng' -> {"lat", "lng"}; a postcode -> {"postcode"}; anything
    else -> {"suburb"} (the site's own store finder places it)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        if value is None:
            return None
        if len(value) > MAX_LOCATION_LENGTH:
            raise ValidationError(f"Longer than maximum length {MAX_LOCATION_LENGTH}.")
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 2:
            try:
                lat, lng = float(parts[0]), float(parts[1])
            except ValueError:
                lat = lng = None
            if lat is not None:
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    raise ValidationError("Latitude must be -90..90 and longitude -180..180.")
                return {"lat": lat, "lng": lng}
        if value.isdigit():
            if not refs.POSTCODE_RE.match(value):
                raise ValidationError("An Australian postcode has 4 digits (2000).")
            return {"postcode": value}
        return {"suburb": value}


def _sort_field():
    return DefaultedChoice(dict(refs.SORTS), load_default=refs.SORTS["relevance"],
                       metadata={"description": "relevance, price_asc, price_desc, name_asc, name_desc, "
                                                "top_sellers or top_rated"})


def _brand_field():
    return StrippedString(load_default=None, validate=_max_length(80),
                          metadata={"description": "brand as the site writes it in the Brand facet "
                                                   "(Breville, Kirkland Signature)"})


def _page_field():
    return PageField(max_page=MAX_PAGE, metadata={"description": "page number, from 1"})


def _limit_field():
    return LimitField(default=20, max_size=MAX_LIMIT, metadata={"description": "products per page, up to 100"})


# ---- products ------------------------------------------------------------------------

class _ListingSchema(BaseSchema):
    page = _page_field()
    limit = _limit_field()
    sort = _sort_field()
    brand = _brand_field()
    filter = FacetFilterField(metadata={"description": "comma-separated Facet:Value refinements, using the "
                                                       "facet names and values a result page returns "
                                                       "(Type:Coffee Machine)"})
    postcode = PostcodeField(metadata={"description": "delivery postcode; Business Delivery items show their "
                                                      "price and stock only with one"})


class ProductsSearchSchema(_ListingSchema):
    query = QueryField(metadata={"description": "search keywords"})
    category = CategoryRefField(required=False, load_default=None,
                                metadata={"description": "search inside this category: a category code or link"})


class CategoriesProductsSchema(_ListingSchema):
    category = CategoryRefField(metadata={"description": "category code (cos_2.4, KIRKLAND-SIGNATURE) or a "
                                                         "costco.com.au category link; its subcategories are included"})
    query = QueryField(required=False, load_default=None, validate=_max_length(200),
                       metadata={"description": "keywords within the category"})


class ProductsDetailsSchema(BaseSchema):
    product = ProductRefField(metadata={"description": "item code (255146, 207732_BD) or a costco.com.au product link"})
    postcode = PostcodeField(metadata={"description": "delivery postcode; a Business Delivery item shows its "
                                                      "price and stock only with one"})


class ProductsBulkSchema(BaseSchema):
    products = ProductsRefField(metadata={"description": "up to 30 item codes or product links, comma-separated"})
    postcode = PostcodeField(metadata={"description": "delivery postcode; Business Delivery items show their "
                                                      "price and stock only with one"})


class ProductsReviewsSchema(BaseSchema):
    product = ProductRefField(metadata={"description": "item code (255146) or a costco.com.au product link"})
    page = PageField(max_page=MAX_REVIEWS_PAGE, metadata={"description": "page number, from 1"})
    limit = LimitField(default=20, max_size=MAX_REVIEWS_LIMIT, metadata={"description": "reviews per page, up to 100"})
    sort = DefaultedChoice(dict(refs.REVIEW_SORTS), load_default=refs.REVIEW_SORTS["newest"],
                       metadata={"description": "newest, oldest, highest_rating, lowest_rating or most_helpful"})
    rating = PositiveInt(max_value=5, metadata={"description": "only reviews with this star rating (1-5)"})


class ProductsSuggestionsSchema(BaseSchema):
    query = QueryField(max_length=100, metadata={"description": "the start of a search term"})
    limit = LimitField(default=10, max_size=MAX_SUGGESTIONS, metadata={"description": "suggestions to return, up to 20"})


# ---- categories / deals --------------------------------------------------------------

class CategoriesListSchema(BaseSchema):
    category = CategoryRefField(required=False, load_default=None,
                                metadata={"description": "a category code or link: answers that category with "
                                                         "everything under it; without it, every department"})


class DealsListSchema(BaseSchema):
    type = DefaultedChoice(dict(refs.DEAL_TYPES), load_default=refs.DEAL_TYPES["hot_buys"],
                       metadata={"description": "hot_buys (current savings), new (new arrivals) or last_chance"})
    page = _page_field()
    limit = _limit_field()
    sort = _sort_field()
    brand = _brand_field()


# ---- stores --------------------------------------------------------------------------

class StoresListSchema(BaseSchema):
    location = LocationField(metadata={"description": "postcode, suburb or lat,lng: warehouses come nearest "
                                                      "first, with their distance"})


class StoresDetailsSchema(BaseSchema):
    store = StoreRefField(metadata={"description": "warehouse name (Marsden Park), warehouse code (109) or a "
                                                   "costco.com.au store-finder link"})
