"""Use the scraper straight from Python, no server needed.

    python main.py

Each call validates its params with the same schema the API uses (so the
defaults apply) and returns the same JSON the API does, except that a list
keeps its `pagination` block instead of next / previous links. The results
are written to output/*.json.
"""
import json
import os

from costco_australia import api, schemas
from schema_fields import load_query

os.makedirs("output", exist_ok=True)


def run(schema_cls, impl, **params):
    data, error = load_query(schema_cls, {k: str(v) for k, v in params.items()})
    if error:
        raise ValueError(error)
    return impl(**data)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # an item code or any costco.com.au product link: price and saving, exact stock level, rating breakdown, barcodes, specs
    save("products_details.json", run(schemas.ProductsDetailsSchema, api.products_details, product='240974'))

    # the suggestions Costco's own search box shows for the start of a term
    save("products_suggestions.json", run(schemas.ProductsSuggestionsSchema, api.products_suggestions, query='app'))

    # 20 products per page with price, saving, rating and stock; sort, brand and any facet filter
    save("products_search.json", run(schemas.ProductsSearchSchema, api.products_search, query='iphone'))
