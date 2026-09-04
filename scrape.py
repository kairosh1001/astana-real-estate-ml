import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import csv
import random
import time
import sys
from typing import Optional, List, Dict
from urllib.parse import parse_qs, urljoin, urlparse
from bs4 import BeautifulSoup
import json
import re

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

LISTING_CATEGORY_PATHS = {
    "sale": "/prodazha/kvartiry/{city}/",
    "rent_monthly": "/arenda/kvartiry/{city}/",
    "rent_daily": "/arenda/kvartiry-posutochno/{city}/",
}

class ApartmentScraper:
    def __init__(self, timeout: int = 15, retry_total: int = 4):
        self.base_url = "https://krisha.kz"
        self.timeout = timeout
        self.retry_total = retry_total
        self.session = self._create_session()
        self.complex_developer_cache: Dict[str, str] = {}
        self.last_fetch_error: Optional[str] = None
        self.last_fetch_status: Optional[int] = None
        self.last_parse_error: Optional[str] = None
    
    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            'User-Agent': random.choice(USER_AGENTS)
        })
        retry = Retry(
            total=self.retry_total,
            connect=self.retry_total,
            read=self.retry_total,
            status=self.retry_total,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            # Return the final HTTP response so refresh diagnostics retain its status.
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
    
    def fetch_page(self, url: str) -> Optional[str]:
        self.last_fetch_error = None
        self.last_fetch_status = None
        try:
            response = self.session.get(url, timeout=self.timeout)
            self.last_fetch_status = response.status_code
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as exc:
            if self.last_fetch_status is not None:
                self.last_fetch_error = f"HTTP {self.last_fetch_status}"
            else:
                self.last_fetch_error = type(exc).__name__
            return None

    def reset_session(self) -> None:
        """Drop cookies and rotate the browser identity after a blocked page."""
        self.session.close()
        self.session = self._create_session()
    
    def safe_get_text(self, soup, selector: str, default: str = "N/A") -> str:
        try:
            element = soup.select_one(selector)
            return element.get_text(strip=True) if element else default
        except (AttributeError, IndexError):
            return default
    
    def parse_apartment_page(self, url: str) -> Optional[Dict]:
        self.last_parse_error = None
        html = self.fetch_page(url)
        if not html:
            self.last_parse_error = self.last_fetch_error or "Empty HTTP response"
            return None

        return self.parse_apartment_html(url, html)

    def parse_apartment_html(self, url: str, html: str) -> Optional[Dict]:
        self.last_parse_error = None
        try:
            soup = BeautifulSoup(html, 'html.parser')
            row_data = {'url': url}
            
            # 1. Title
            row_data['title'] = self.safe_get_text(soup, '.offer__advert-title h1')
            
            # 2. Price
            price_text = self.safe_get_text(soup, '.offer__price')
            row_data['price'] = (
                price_text.replace('\xa0', '').replace('₸', '').strip() 
                if price_text != "N/A" else "N/A"
            )
            
            # Structured page data is more stable than presentation selectors for
            # coordinates and seller type. JSONDecoder avoids a greedy regex over
            # any JavaScript that Krisha may append after window.data.
            data = self.extract_page_data(soup)
            advert_value = data.get('advert') if isinstance(data, dict) else None
            advert = advert_value if isinstance(advert_value, dict) else {}
            advert_map = advert.get('map')
            advert_map = advert_map if isinstance(advert_map, dict) else {}
            row_data['lat'] = advert_map.get('lat')
            row_data['lon'] = advert_map.get('lon')
            if isinstance(advert.get('price'), (int, float)):
                row_data['price'] = advert['price']
            if row_data['title'] == 'N/A' and isinstance(advert.get('title'), str):
                row_data['title'] = advert['title']
            if row_data['title'] == 'N/A' or row_data['price'] == 'N/A':
                self.last_parse_error = (
                    "Missing listing title or price: removed listing, access page, "
                    "or changed page markup"
                )
                return None
            row_data['listing_id'] = advert.get('id')
            row_data['section_alias'] = advert.get('sectionAlias')
            row_data['category_alias'] = advert.get('categoryAlias')
            row_data['rooms_structured'] = advert.get('rooms')
            row_data['area_m2_structured'] = advert.get('square')
            row_data['photo_count'] = len(advert.get('photos') or [])
            row_data['is_estate_verified'] = bool(advert.get('isEstateVerified'))
            row_data['seller_type'] = advert.get('userType')
            row_data['addressTitle'] = advert.get('addressTitle')
            row_data['rental_period'] = self.rental_period_from_advert(advert)
            row_data['description'] = self.normalize_listing_text(
                self.safe_get_text(soup, '.offer__description', default='')
            )
            developer = self.find_developer_name(advert)
            if not developer and advert.get('userType') == 'builder':
                developer = self.extract_name_from_value(advert.get('ownerName'))
            if developer:
                row_data['Застройщик'] = developer

            # 3. Apartment Info
            info_container = soup.select_one('.offer__advert-info')
            if info_container:
                for item in info_container.select('.offer__info-item'):
                    key_elem = item.select_one('.offer__info-title')
                    val_elem = item.select_one('.offer__advert-short-info')
                    if key_elem and val_elem:
                        key = self.normalize_listing_text(
                            key_elem.get_text(' ', strip=True)
                        )
                        value = self.normalize_listing_text(
                            val_elem.get_text(' ', strip=True).replace(
                                'показать на карте',
                                '',
                            )
                        )
                        if key and value:
                            row_data[key] = value
            
            # 4. Parameters
            params_container = soup.select_one('.offer__parameters')
            if params_container:
                for dl in params_container.select('dl'):
                    dt, dd = dl.select_one('dt'), dl.select_one('dd')
                    if dt and dd:
                        key = self.normalize_listing_text(dt.get_text(' ', strip=True))
                        value = self.normalize_listing_text(dd.get_text(' ', strip=True))
                        if key and value:
                            row_data[key] = value

            # Krisha identifies primary-market offers with a builder seller and
            # with its canonical das[novostroiki]=1 links. Store an explicit flag
            # so downstream filtering never has to guess from construction year.
            row_data['Новостройка'] = self.is_new_build_listing(soup, advert)

            if (
                not row_data.get('\u0417\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a')
                and row_data.get('rental_period') is None
            ):
                complex_url = self.find_complex_url(soup)
                if complex_url:
                    developer = self.fetch_complex_developer(complex_url)
                    if developer:
                        row_data['\u0417\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a'] = developer
            
            return row_data
        except Exception as exc:
            self.last_parse_error = f"{type(exc).__name__}: {exc}"
            return None

    @staticmethod
    def normalize_listing_text(value: object) -> str:
        return re.sub(r'\s+', ' ', str(value or '')).strip()

    @staticmethod
    def extract_page_data(soup) -> Dict:
        script_tag = soup.find('script', {'id': 'jsdata'})
        if not script_tag:
            return {}
        script_text = script_tag.string or script_tag.get_text() or ''
        match = re.search(r'window\.data\s*=\s*', script_text)
        if not match:
            return {}
        try:
            data, _ = json.JSONDecoder().raw_decode(script_text[match.end():].lstrip())
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def is_new_build_listing(self, soup, advert: Dict | None = None) -> bool:
        advert = advert if isinstance(advert, dict) else {}
        if str(advert.get('userType') or '').casefold() == 'builder':
            return True

        for link in soup.select('a[href]'):
            href = link.get('href') or ''
            query = parse_qs(urlparse(urljoin(self.base_url, href)).query)
            if '1' in query.get('das[novostroiki]', []):
                return True
        return False

    def find_complex_url(self, soup) -> Optional[str]:
        for link in soup.select('a[href*="/complex/show/"]'):
            href = link.get('href')
            if href:
                return urljoin(self.base_url, href)
        return None

    @staticmethod
    def rental_period_from_advert(advert: Dict | None) -> Optional[str]:
        advert = advert if isinstance(advert, dict) else {}
        if str(advert.get('sectionAlias') or '').casefold() != 'arenda':
            return None
        category = str(advert.get('categoryAlias') or '').casefold()
        if category == 'kvartiry':
            return 'monthly'
        if category == 'kvartiry-posutochno':
            return 'daily'
        return None

    def category_page_url(
        self,
        category: str,
        page: int,
        *,
        city: str = "astana",
        rooms: str | None = None,
    ) -> str:
        try:
            path = LISTING_CATEGORY_PATHS[category].format(city=city)
        except KeyError as exc:
            choices = ', '.join(sorted(LISTING_CATEGORY_PATHS))
            raise ValueError(
                f"Unknown category {category!r}; expected one of: {choices}"
            ) from exc
        query: list[tuple[str, object]] = [("page", page)]
        if rooms:
            query.append(("das[live.rooms]", rooms))
        from urllib.parse import urlencode

        return f"{self.base_url}{path}?{urlencode(query)}"

    def fetch_complex_developer(self, complex_url: str) -> Optional[str]:
        if complex_url in self.complex_developer_cache:
            return self.complex_developer_cache[complex_url] or None

        html = self.fetch_page(complex_url)
        developer = None
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            developer = self.parse_complex_developer(soup)

        self.complex_developer_cache[complex_url] = developer or ""
        return developer

    def parse_complex_developer(self, soup) -> Optional[str]:
        developer_label = '\u0437\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a'

        for container in soup.select('.complex__sidebar-info'):
            text = container.get_text(' ', strip=True)
            if developer_label in text.casefold():
                value = container.select_one('.complex__sidebar-info-text')
                if value:
                    return value.get_text(' ', strip=True) or None

                cleaned = re.sub(
                    r'^\s*\u0417\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a\s*',
                    '',
                    text,
                    flags=re.IGNORECASE,
                ).strip()
                if cleaned:
                    return cleaned

        for meta in soup.find_all('meta'):
            content = meta.get('content') or ''
            match = re.search(
                r'\u043e\u0442\s+\u0437\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a\u0430\s+(.+?)(?:\s+-|,|\.)',
                content,
                flags=re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()

        if soup.title:
            title = soup.title.get_text(' ', strip=True)
            match = re.search(r'\|\s*(.+?)\s*-\s*\u041a\u0440\u044b\u0448\u0430', title)
            if match:
                return match.group(1).strip()

        return None

    def find_developer_name(self, value) -> Optional[str]:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key).lower()
                if any(marker in key_text for marker in ["застрой", "developer"]):
                    cleaned = self.extract_name_from_value(item)
                    if cleaned:
                        return cleaned
                if any(marker in key_text for marker in ["buildername", "builder_name"]):
                    cleaned = self.extract_name_from_value(item)
                    if cleaned:
                        return cleaned
                nested = self.find_developer_name(item)
                if nested:
                    return nested
        elif isinstance(value, list):
            for item in value:
                nested = self.find_developer_name(item)
                if nested:
                    return nested
        return None

    @staticmethod
    def extract_name_from_value(value) -> Optional[str]:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        if isinstance(value, dict):
            for key in ["name", "title", "label"]:
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None
    
    def get_listing_page(self, page_url: str) -> tuple[List[str], Optional[int]]:
        html = self.fetch_page(page_url)
        if not html:
            raise RuntimeError(f"Could not fetch category page: {page_url}")
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            card_links = soup.select('.a-card__title[href]')
            urls = []
            seen = set()

            for link in card_links:
                href = link.get('href')
                if href:
                    parsed = urlparse(urljoin(self.base_url, href))
                    full_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if full_url not in seen:
                        seen.add(full_url)
                        urls.append(full_url)

            page_numbers = []
            category_path = urlparse(page_url).path.rstrip('/')
            for link in soup.select('a[href]'):
                parsed_link = urlparse(urljoin(self.base_url, link.get('href') or ''))
                if parsed_link.path.rstrip('/') != category_path:
                    continue
                query = parse_qs(parsed_link.query)
                for raw_page in query.get('page', []):
                    try:
                        page_numbers.append(int(raw_page))
                    except (TypeError, ValueError):
                        continue

            return urls, max(page_numbers, default=None)
        except Exception as exc:
            raise RuntimeError(f"Could not parse category page: {page_url}") from exc

    def get_listing_urls(self, page_url: str) -> List[str]:
        try:
            urls, _ = self.get_listing_page(page_url)
            return urls
        except RuntimeError:
            return []
    
    def save_to_csv(self, data_list: List[Dict], filename: str = "krisha_data_raw.csv"):
        if not data_list:
            return
        
        try:
            headers = set()
            for item in data_list:
                headers.update(item.keys())
            
            sorted_headers = sorted(list(headers))
            priority = ['url', 'title', 'price']
            
            for field in reversed(priority):
                if field in sorted_headers:
                    sorted_headers.remove(field)
                    sorted_headers.insert(0, field)
            
            with open(filename, mode='w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=sorted_headers)
                writer.writeheader()
                writer.writerows(data_list)
        except Exception:
            pass
    
    def scrape_krisha(self, pages_to_scrape: int = 3, start_page: int = 1):
        all_apartments = []
        total_parsed = 0
        checkpoint_every = 100
        try:
            for page in range(start_page, pages_to_scrape + 1):
                page_url = f"{self.base_url}/prodazha/kvartiry/astana/?page={page}"
                print(f"[INFO] Scraping page {page}/{pages_to_scrape}: {page_url}")
                listing_urls = self.get_listing_urls(page_url)
                if not listing_urls:
                    print(f"[WARN] No listings found on page {page}, stopping.")
                    break
                print(f"[INFO] Found {len(listing_urls)} listings on page {page}.")
                for idx, url in enumerate(listing_urls, start=1):
                    print(f"[INFO] Fetching listing {idx}/{len(listing_urls)} on page {page}: {url}")
                    data = self.parse_apartment_page(url)
                    if data:
                        all_apartments.append(data)
                        total_parsed += 1
                    else:
                        print(f"[ERROR] Failed to parse listing: {url}")
                    time.sleep(random.uniform(1, 2))
                print(f"[INFO] Completed page {page}. Parsed total apartments so far: {total_parsed}.")

                if page % checkpoint_every == 0:
                    self.save_to_csv(all_apartments, "krisha_data_raw.csv")
                    print(f"[CHECKPOINT] Saved {total_parsed} listings after page {page}.")
                    
                time.sleep(random.uniform(2, 3))
        finally:
            print(f"[INFO] Scraping complete: {total_parsed} apartments parsed. Saving CSV...")
            self.save_to_csv(all_apartments)
            self.session.close()
            print("[INFO] Session closed.")


if __name__ == "__main__":
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    scraper = ApartmentScraper()  
    scraper.scrape_krisha(pages_to_scrape=pages, start_page=start)
