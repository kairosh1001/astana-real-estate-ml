import requests
import csv
import random
import time
import sys
from typing import Optional, List, Dict
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import json
import re

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]


class ApartmentScraper:
    def __init__(self, timeout: int = 10):
        self.base_url = "https://krisha.kz"
        self.timeout = timeout
        self.session = self._create_session()
        self.complex_developer_cache: Dict[str, str] = {}
    
    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            'User-Agent': random.choice(USER_AGENTS)
        })
        
        return session
    
    def fetch_page(self, url: str) -> Optional[str]:
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException:
            return None
    
    def safe_get_text(self, soup, selector: str, default: str = "N/A") -> str:
        try:
            element = soup.select_one(selector)
            return element.get_text(strip=True) if element else default
        except (AttributeError, IndexError):
            return default
    
    def parse_apartment_page(self, url: str) -> Optional[Dict]:
        html = self.fetch_page(url)
        if not html:
            return None
        
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
            
            #Addition! To get latitude and longitude:
            script_tag = soup.find('script', {'id': 'jsdata'})
            if script_tag:
                match = re.search(r'window\.data\s*=\s*(\{.+\});', script_tag.string, re.DOTALL)
                if match:
                    data = json.loads(match.group(1))
                    advert = data.get('advert', {})
                    row_data['lat'] = advert.get('map', {}).get('lat')
                    row_data['lon'] = advert.get('map', {}).get('lon')
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
                        val = val_elem.get_text(separator=" ", strip=True).replace('показать на карте', '').strip()
                        row_data[key_elem.text.strip()] = val
            
            # 4. Parameters
            params_container = soup.select_one('.offer__parameters')
            if params_container:
                for dl in params_container.select('dl'):
                    dt, dd = dl.select_one('dt'), dl.select_one('dd')
                    if dt and dd:
                        row_data[dt.text.strip()] = dd.text.strip()

            if not row_data.get('\u0417\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a'):
                complex_url = self.find_complex_url(soup)
                if complex_url:
                    developer = self.fetch_complex_developer(complex_url)
                    if developer:
                        row_data['\u0417\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a'] = developer
            
            return row_data
        except Exception:
            return None

    def find_complex_url(self, soup) -> Optional[str]:
        for link in soup.select('a[href*="/complex/show/"]'):
            href = link.get('href')
            if href:
                return urljoin(self.base_url, href)
        return None

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
    
    def get_listing_urls(self, page_url: str) -> List[str]:
        html = self.fetch_page(page_url)
        if not html:
            return []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            card_links = soup.select('.a-card__title[href]')
            urls = []
            
            for link in card_links:
                href = link.get('href')
                if href:
                    full_url = urljoin(self.base_url, href)
                    urls.append(full_url)
            
            return urls
        except Exception:
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
