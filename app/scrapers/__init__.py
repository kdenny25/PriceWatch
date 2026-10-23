from .craigslist import CraigslistScraper
from .ebay import EbayScraper
from .generic import GenericScraper

# One shared instance per type (eBay caches its OAuth token on the instance).
SCRAPERS = {s.type: s for s in (EbayScraper(), CraigslistScraper(), GenericScraper())}
