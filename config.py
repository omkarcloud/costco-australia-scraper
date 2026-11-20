"""Configuration for the Costco Australia Scraper. Everything can be set with an environment
variable; the defaults work out of the box.

    PORT                     port the API listens on (default 8000)
    COSTCO_AUSTRALIA_PROXY   proxy URL for every request, e.g. http://user:pass@host:port
                             (default: none, requests go out from your own connection).
                             Costco Australia's own JSON API answers home, office and
                             cloud connections alike, so you very likely don't need this.
                             Set a datacenter proxy here only if your server's address
                             is ever refused.

Everything else below is a plain constant with a working default.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

COSTCO_AUSTRALIA_PROXY = os.environ.get("COSTCO_AUSTRALIA_PROXY") or None


# ---- what the costco_australia module reads: defaults for a direct run ----
COSTCO_AUSTRALIA_EXIT = COSTCO_AUSTRALIA_PROXY      # the exit every session leaves through: your proxy, or direct
# products/bulk only. The site shows some prices (all liquor, some sale prices) to signed-in
# members only: its search and product calls leave them out, but its batch call returns them
# to anyone, with the product flagged hidePriceValue. True = pass that price on, flagged
# is_members_only_price; False = null it, as the other endpoints do.
COSTCO_AUSTRALIA_MEMBERS_ONLY_PRICES = True
MAX_RETRIES = 3
RETRY_BACKOFF = 2

# The display code of costco.com.au's own reviews widget (<displayCode>,<siteId>,<locale>). It is
# public: the storefront ships it to every visitor in its swat_reviews-config.js. Sent as the
# Bv-Bfd-Token header; without it the review service answers 400.
COSTCO_AUSTRALIA_REVIEWS_TOKEN = "12402,main_site,en_AU"
