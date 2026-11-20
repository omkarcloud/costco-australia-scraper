"""Cache TTL per /costco-australia/* surface (cache.py, keyed on the
validated params). The site itself serves its JSON with a 90-minute
max-age, so nothing here is fresher than that upstream anyway.
"""
from datetime import timedelta

# products/search, categories/products, deals/list: prices change on
# promotion boundaries (days) and the site caches a listing for 90 minutes
LISTING_CACHE = timedelta(hours=1)
# the product record behind products/details and products/reviews: price
# and the exact stock level, which moves through the day
PRODUCT_CACHE = timedelta(hours=1)
# products/bulk: the same rows as a listing, asked by code
BULK_CACHE = timedelta(hours=1)
# products/suggestions: the suggestion index follows the catalog
SUGGESTIONS_CACHE = timedelta(hours=12)
# products/reviews pages: a product gains a review every few days at most
REVIEWS_CACHE = timedelta(hours=6)
# the review service's record of a product (brand, barcodes, rating
# distribution): changes only with a new review or a feed update
REVIEW_RECORD_CACHE = timedelta(hours=12)
# the category tree and a category's own record: move with merchandising
# changes; the whole tree takes the site ~10 s to build
CATEGORIES_CACHE = timedelta(hours=24)
# the warehouse list: opening hours change rarely, but the fuel prices in
# it are updated through the day
STORES_CACHE = timedelta(minutes=30)
