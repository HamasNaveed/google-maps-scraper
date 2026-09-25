"""
Lead scraper: given a country, city, and search term (service), finds businesses via the
bundled google_maps_scraper.exe, then crawls each business's website (homepage + contact/
about/footer-linked pages) to collect as many emails and phone numbers as possible.

Output CSV columns: Business Name, Website, Emails, Mobile Numbers.
"""
import argparse
import concurrent.futures
import csv
import json
import re
import subprocess
import sys
from email.utils import parseaddr
from urllib.parse import urljoin, urlparse

import phonenumbers
import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRAPER_EXE = ".\\google_maps_scraper.exe"

SOCIAL_DOMAINS = ("facebook", "instagram", "twitter", "linkedin", "tiktok", "youtube")

CONTACT_KEYWORDS = (
    "contact", "about", "support", "get-in-touch", "reach-us", "impressum", "kontakt",
)

ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".ico", ".css", ".js",
)

EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_REGEX = re.compile(r"[+(]?\d[\d().\-\s]{7,}\d")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 8
MAX_EXTRA_PAGES = 4
MAX_WORKERS = 5


def run_maps_scraper(query: str, max_results: int) -> list[dict]:
    cmd = [
        SCRAPER_EXE,
        "-input", "stdin",
        "-results", "stdout",
        "-json",
        "-email",
        "-c", "4",
        "-pages-per-browser", "4",
        "-depth", "2",
        "-exit-on-inactivity", "1m",
    ]

    proc = subprocess.run(
        cmd,
        input=query + "\n",
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return entries[:max_results]


def is_valid_email(candidate: str) -> bool:
    lower = candidate.lower()
    if any(lower.endswith(ext) for ext in ASSET_EXTENSIONS):
        return False
    if "@2x" in lower or "@3x" in lower:
        return False
    _, addr = parseaddr(candidate)
    if not addr or "@" not in addr:
        return False
    domain = addr.split("@", 1)[1]
    return "." in domain


def extract_emails(html: str, soup: BeautifulSoup) -> set[str]:
    found = set()

    for a in soup.select("a[href^='mailto:']"):
        value = a["href"][len("mailto:"):].split("?")[0].strip()
        if is_valid_email(value):
            found.add(value.lower())

    for match in EMAIL_REGEX.findall(html):
        if is_valid_email(match):
            found.add(match.lower())

    return found


def extract_phones(html: str, soup: BeautifulSoup, region: str | None) -> set[str]:
    found = set()

    candidates = []
    for a in soup.select("a[href^='tel:']"):
        candidates.append(a["href"][len("tel:"):].strip())
    candidates.extend(PHONE_REGEX.findall(html))

    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            found.add(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))

    return found


def is_social_domain(url: str) -> bool:
    lower = url.lower()
    return any(domain in lower for domain in SOCIAL_DOMAINS)


def fetch(url: str) -> requests.Response | None:
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            return resp
    except requests.RequestException:
        pass
    return None


def find_contact_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    base_domain = urlparse(base_url).netloc
    links = set()

    footer = soup.find("footer")
    footer_anchors = set(footer.find_all("a", href=True)) if footer else set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True).lower()
        full = urljoin(base_url, href)
        parsed = urlparse(full)

        if parsed.netloc != base_domain:
            continue
        if parsed.scheme not in ("http", "https"):
            continue

        haystack = f"{href.lower()} {text}"
        if any(keyword in haystack for keyword in CONTACT_KEYWORDS) or a in footer_anchors:
            links.add(full)

    return list(links)[:MAX_EXTRA_PAGES]


def crawl_website(url: str, region: str | None) -> tuple[set[str], set[str]]:
    emails: set[str] = set()
    phones: set[str] = set()

    home = fetch(url)
    if home is None:
        return emails, phones

    soup = BeautifulSoup(home.text, "html.parser")
    emails |= extract_emails(home.text, soup)
    phones |= extract_phones(home.text, soup, region)

    for link in find_contact_links(soup, home.url):
        resp = fetch(link)
        if resp is None:
            continue
        sub_soup = BeautifulSoup(resp.text, "html.parser")
        emails |= extract_emails(resp.text, sub_soup)
        phones |= extract_phones(resp.text, sub_soup, region)

    return emails, phones


def region_code(country: str) -> str | None:
    overrides = {
        "usa": "US", "united states": "US", "uk": "GB", "united kingdom": "GB",
        "pakistan": "PK", "india": "IN", "canada": "CA", "australia": "AU",
    }
    key = country.strip().lower()
    if key in overrides:
        return overrides[key]
    if len(country.strip()) == 2:
        return country.strip().upper()
    return None


def process_entry(entry: dict, region: str | None) -> dict:
    name = entry.get("title") or "Unknown"
    website = (entry.get("web_site") or "").strip()
    maps_phone = (entry.get("phone") or "").strip()

    emails: set[str] = set(e.lower() for e in (entry.get("emails") or []) if is_valid_email(e))
    phones: set[str] = set()

    if maps_phone:
        try:
            parsed = phonenumbers.parse(maps_phone, region)
            if phonenumbers.is_valid_number(parsed):
                phones.add(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))
            else:
                phones.add(maps_phone)
        except phonenumbers.NumberParseException:
            phones.add(maps_phone)

    if website and not is_social_domain(website):
        site_emails, site_phones = crawl_website(website, region)
        emails |= site_emails
        phones |= site_phones

    return {
        "Business Name": name,
        "Website": website if website else "Not present",
        "Emails": ", ".join(sorted(emails)),
        "Mobile Numbers": ", ".join(sorted(phones)),
    }


def main():
    parser = argparse.ArgumentParser(description="Scrape business leads with deep website crawling.")
    parser.add_argument("--country", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--output", default="leads.csv")
    args = parser.parse_args()

    query = f"{args.service} in {args.city}, {args.country}"
    region = region_code(args.country)

    print(f"Searching Google Maps for: \"{query}\"...")
    entries = run_maps_scraper(query, args.max_results)
    print(f"Found {len(entries)} businesses. Crawling websites for contact info...")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_entry, entry, region) for entry in entries]
        for i, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            row = future.result()
            results.append(row)
            print(f"  [{i}/{len(entries)}] {row['Business Name']}")

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Business Name", "Website", "Emails", "Mobile Numbers"])
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone. Wrote {len(results)} leads to {args.output}.")


if __name__ == "__main__":
    main()
