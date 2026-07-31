"""Official provider adapters for ETF holdings downloads.

The exact public URLs move over time, so these adapters prefer product-page
link discovery over hard-coded one-off download URLs. They never authenticate
or bypass paywalls; they only follow public pages/download links.
"""
from __future__ import annotations

import html
import io
import csv
import json
import re
from datetime import date, datetime, timezone, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from .base import EtfCandidate, HoldingRow, HoldingsResult, ProductRef, ProductResolutionError
from .parser import parse_holdings


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,text/csv,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}


def _hrefs(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"""href=["']([^"']+)["']""", html, re.I):
        href = match.group(1).replace("&amp;", "&")
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        if absolute not in links:
            links.append(absolute)
    return links


def _filename_from_headers(headers: dict, fallback_url: str | None = None) -> str | None:
    disposition = headers.get("content-disposition") or headers.get("Content-Disposition")
    if disposition:
        match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", disposition, re.I)
        if match:
            return match.group(1).strip('"')
    if fallback_url:
        return fallback_url.rsplit("/", 1)[-1].split("?", 1)[0] or None
    return None


class LinkDiscoveryAdapter:
    provider_ids: tuple[str, ...] = ()
    search_url_templates: tuple[str, ...] = ()
    product_link_patterns: tuple[str, ...] = ()
    holdings_link_patterns: tuple[str, ...] = (
        "holding", "holdings", "bestand", "portfolio", "constituent", "constituents",
        "download", "csv", "xlsx", "xls", "ajax",
    )

    def __init__(self, session=None) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        try:
            self.session.headers.update(_HEADERS)
        except Exception:
            pass

    def supports(self, provider_id: str) -> bool:
        return provider_id in self.provider_ids

    def _get(self, url: str):
        response = self.session.get(url, timeout=30, allow_redirects=True)
        response.raise_for_status()
        return response

    def _candidate_search_urls(self, candidate: EtfCandidate) -> list[str]:
        token = quote(candidate.isin)
        return [template.format(isin=token) for template in self.search_url_templates]

    def _is_product_link(self, link: str, candidate: EtfCandidate) -> bool:
        lower = link.lower()
        if candidate.isin.lower() in lower:
            return True
        return any(pattern.lower() in lower for pattern in self.product_link_patterns)

    def _is_holdings_link(self, link: str) -> bool:
        lower = link.lower()
        return any(pattern.lower() in lower for pattern in self.holdings_link_patterns)

    def _download_from_page(self, page_url: str) -> str | None:
        page = self._get(page_url)
        content_type = page.headers.get("content-type", "")
        if not content_type.lower().startswith("text/html"):
            return page.url
        html = page.text
        links = _hrefs(html, page.url)
        for link in links:
            if self._is_holdings_link(link):
                return link
        return None

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        seen: set[str] = set()
        for search_url in self._candidate_search_urls(candidate):
            if search_url in seen:
                continue
            seen.add(search_url)
            response = self._get(search_url)
            content_type = response.headers.get("content-type", "")
            if not content_type.lower().startswith("text/html"):
                return ProductRef(
                    isin=candidate.isin,
                    provider_id=candidate.provider_id,
                    product_url=response.url,
                    download_url=response.url,
                    source_name=response.url,
                )
            html = response.text
            links = _hrefs(html, response.url)
            direct = next((link for link in links if self._is_holdings_link(link)), None)
            if direct:
                return ProductRef(candidate.isin, candidate.provider_id, response.url, direct, direct)
            for link in links:
                if not self._is_product_link(link, candidate):
                    continue
                try:
                    download = self._download_from_page(link)
                except Exception:
                    continue
                if download:
                    return ProductRef(candidate.isin, candidate.provider_id, link, download, download)
        raise ProductResolutionError(f"{candidate.provider_id}: no public holdings link resolved for {candidate.isin}")

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"{product.provider_id}: no download URL for {product.isin}")
        response = self._get(product.download_url)
        filename = _filename_from_headers(dict(response.headers), product.download_url)
        holdings = parse_holdings(
            response.content,
            content_type=response.headers.get("content-type"),
            filename=filename,
        )
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=holdings,
            source_url=response.url,
        )


class IsharesAdapter(LinkDiscoveryAdapter):
    provider_ids = ("ishares",)
    search_url_templates = (
        "https://www.ishares.com/de/privatanleger/de/suche?searchText={isin}",
        "https://www.ishares.com/de/professionelle-anleger/de/suche?searchText={isin}",
    )
    product_link_patterns = ("/produkte/", "/products/")
    holdings_link_patterns = (
        "fileType=csv", "dataType=fund", "holdings", "portfolio", ".ajax",
    )
    known_downloads = {
        "IE00B4L5Y983": (
            "https://www.ishares.com/de/privatanleger/de/produkte/251882/"
            "etf-investments/1478358465952.ajax?fileType=csv&"
            "fileName=iShares-Core-MSCI-World-UCITS-ETF-Reg-Shs-USD-Acc_fund&"
            "dataType=fund"
        ),
    }
    site_entry_url = (
        "https://www.ishares.com/de/privatanleger/de/produkte/251882/"
        "ishares-msci-world-ucits-etf-acc-fund?switchLocale=y&siteEntryPassthrough=true"
    )
    autocomplete_url = "https://www.ishares.com/de/privatanleger/de/autoComplete.search"
    us_autocomplete_url = "https://www.ishares.com/us/autoComplete.search"
    us_product_sitemap_url = "https://www.ishares.com/us/product-sitemap.xml"
    us_holdings_url = (
        "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
        "product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&"
        "appSubType=ISHARES&targetSite=us-ishares&locale=en_US&"
        "portfolioId={product_id}&component={component}&userType=individual"
    )

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._site_entry_ready = False
        self._us_product_urls_by_id: dict[str, str] | None = None

    def _ensure_site_entry(self) -> None:
        if self._site_entry_ready:
            return
        self._get(self.site_entry_url)
        self._site_entry_ready = True

    def _autocomplete_product_urls(self, candidate: EtfCandidate) -> list[str]:
        self._ensure_site_entry()
        urls: list[str] = []
        terms = [candidate.isin]
        for term in (candidate.full_name, candidate.short_name):
            if term and term not in terms:
                terms.append(term)
        endpoints = [self.autocomplete_url]
        if candidate.isin.upper().startswith("US"):
            endpoints.insert(0, self.us_autocomplete_url)
        for term in terms:
            for endpoint in endpoints:
                response = self.session.get(
                    endpoint,
                    params={"type": "autocomplete", "term": term},
                    timeout=30,
                    allow_redirects=True,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, list):
                    continue
                for item in payload:
                    if not isinstance(item, dict) or item.get("category") != "productAutocomplete":
                        continue
                    url = str(item.get("id") or "").strip()
                    if url and url not in urls:
                        urls.append(url)
        return urls

    def _us_document_url(self, product_id: str, component: str) -> str:
        return self.us_holdings_url.format(product_id=product_id, component=component)

    def _us_product_url_from_sitemap(self, product_id: str) -> str | None:
        if self._us_product_urls_by_id is None:
            response = self._get(self.us_product_sitemap_url)
            self._us_product_urls_by_id = {
                match.group(1): match.group(0)
                for match in re.finditer(
                    r"https://www\.ishares\.com/us/products/(\d+)/[^<]+",
                    response.text,
                )
            }
        return self._us_product_urls_by_id.get(product_id)

    def _resolve_us_product(self, candidate: EtfCandidate, product_url: str) -> ProductRef | None:
        match = re.search(r"/us/products/(\d+)/?", product_url)
        if not match:
            return None
        product_id = match.group(1)
        verification_url = self._us_product_url_from_sitemap(product_id)
        if not verification_url:
            return None
        verification = self._get(verification_url)
        if candidate.isin.upper() not in verification.text.upper():
            return None
        download_url = self._us_document_url(product_id, "holdings")
        return ProductRef(
            isin=candidate.isin,
            provider_id=candidate.provider_id,
            product_url=product_url,
            download_url=download_url,
            source_name=download_url,
        )

    @staticmethod
    def _page_isin(page_html: str) -> str | None:
        match = re.search(r"\bvar\s+isin\s*=\s*[\"']([A-Z0-9]{12})[\"']", page_html)
        return match.group(1).upper() if match else None

    @staticmethod
    def _holdings_download_from_page(page_html: str, page_url: str) -> str | None:
        links = [
            html.unescape(match.group(1)).replace("&amp;", "&")
            for match in re.finditer(r"""href=["']([^"']+)["']""", page_html, re.I)
        ]
        for href in links:
            lower = href.lower()
            if (
                "1478358465952.ajax" in lower
                and "filetype=csv" in lower
                and "datatype=fund" in lower
            ):
                return urljoin(page_url, href)
        return None

    def _resolve_from_product_page(self, candidate: EtfCandidate, product_url: str) -> ProductRef | None:
        us_resolved = self._resolve_us_product(candidate, product_url)
        if us_resolved:
            return us_resolved
        self._ensure_site_entry()
        page = self._get(product_url)
        page_html = page.text
        if self._page_isin(page_html) != candidate.isin.upper():
            return None
        download_url = self._holdings_download_from_page(page_html, page.url)
        if not download_url:
            return None
        return ProductRef(
            isin=candidate.isin,
            provider_id=candidate.provider_id,
            product_url=page.url,
            download_url=download_url,
            source_name=download_url,
        )

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        url = self.known_downloads.get(candidate.isin.upper())
        if url:
            return ProductRef(
                isin=candidate.isin,
                provider_id=candidate.provider_id,
                product_url="https://www.ishares.com/",
                download_url=url,
                source_name=url,
            )
        errors: list[str] = []
        for product_url in self._autocomplete_product_urls(candidate):
            try:
                resolved = self._resolve_from_product_page(candidate, product_url)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                continue
            if resolved:
                return resolved
        if errors:
            raise ProductResolutionError(
                f"ishares: no verified holdings URL for {candidate.isin}; "
                f"{len(errors)} candidate page errors"
            )
        return super().resolve_product(candidate)


class XtrackersAdapter(LinkDiscoveryAdapter):
    provider_ids = ("xtrackers",)
    api_url_template = "https://etf.dws.com/api/pdp/en-gb/etf/{isin}/holdings"
    product_url_template = "https://etf.dws.com/en-gb/{isin}/"

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        download_url = self.api_url_template.format(isin=quote(isin))
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=self.product_url_template.format(isin=quote(isin)),
            download_url=download_url,
            source_name=download_url,
        )

    @staticmethod
    def _column_key(columns: list[dict[str, Any]], aliases: tuple[str, ...], fallback: str) -> str:
        for column in columns:
            label = str(column.get("value") or "").strip().lower()
            key = str(column.get("key") or "").strip()
            if key and any(alias in label for alias in aliases):
                return key
        return fallback

    @staticmethod
    def _cell_value(row: dict[str, Any], key: str) -> Any:
        cell = row.get(key)
        if isinstance(cell, dict):
            return cell.get("value")
        return cell

    @classmethod
    def _cell_text(cls, row: dict[str, Any], key: str) -> str | None:
        value = cls._cell_value(row, key)
        if value is None:
            return None
        text = str(value).strip()
        if not text or text in {"--", "-"} or text.lower() in {"none", "null", "nan"}:
            return None
        return text

    @staticmethod
    def _cell_sort_value(row: dict[str, Any], key: str) -> Any:
        cell = row.get(key)
        if isinstance(cell, dict):
            return cell.get("sortValue")
        return None

    @classmethod
    def _cell_weight(cls, row: dict[str, Any], key: str) -> float | None:
        raw = cls._cell_sort_value(row, key)
        if raw is None:
            raw = cls._cell_value(row, key)
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = raw.strip().replace("%", "").replace(",", ".")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value != value:
            return None
        # Xtrackers labels this column as a percentage and returns values like
        # 5.074 for 5.074%, while the internal holdings tables store fractions.
        return value / 100.0

    @staticmethod
    def _parse_as_of_date(value: Any) -> date | None:
        if not value:
            return None
        text = str(value).strip()
        for candidate in (
            text[:10],
            text.replace("/", "-")[:10],
        ):
            try:
                return date.fromisoformat(candidate)
            except ValueError:
                pass
        match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text)
        if match:
            day, month, year = match.groups()
            try:
                return date(int(year), int(month), int(day))
            except ValueError:
                return None
        return None

    @classmethod
    def _product_as_of_date(cls, payload: dict[str, Any]) -> date | None:
        parsed = cls._parse_as_of_date(payload.get("asOfDate"))
        if parsed:
            return parsed
        for table in payload.get("tables") or []:
            for key in ("headlineText", "subHeadlineText", "introduction", "summary"):
                parsed = cls._parse_as_of_date(table.get(key))
                if parsed:
                    return parsed
        return None

    @classmethod
    def _rows_from_payload(cls, payload: dict[str, Any]) -> list[HoldingRow]:
        for table in payload.get("tables") or []:
            values = table.get("values") or []
            if not values:
                continue
            columns = table.get("columns") or []
            isin_key = cls._column_key(columns, ("isin",), "header")
            name_key = cls._column_key(columns, ("name", "bezeichnung"), "column_0")
            weight_key = cls._column_key(columns, ("weight", "gewicht"), "column_1")
            rows: list[HoldingRow] = []
            for item in values:
                if not isinstance(item, dict):
                    continue
                holding_isin = cls._cell_text(item, isin_key)
                name = cls._cell_text(item, name_key)
                weight = cls._cell_weight(item, weight_key)
                if not holding_isin and not name and weight is None:
                    continue
                rows.append(
                    HoldingRow(
                        rank=len(rows) + 1,
                        symbol=None,
                        holding_isin=holding_isin,
                        name=name,
                        weight=weight,
                    )
                )
            return rows
        return []

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"xtrackers: no API URL for {product.isin}")
        response = self.session.get(
            product.download_url,
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/json",
                "client-id": "passive-frontend",
                "Referer": product.product_url or "https://etf.dws.com/en-gb/",
            },
        )
        response.raise_for_status()
        payload = response.json()
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=self._rows_from_payload(payload),
            source_url=response.url,
            as_of_date=self._product_as_of_date(payload),
        )


class AmundiAdapter(LinkDiscoveryAdapter):
    provider_ids = ("amundi",)
    api_url = "https://www.amundietf.de/mapi/ProductAPI/getProductsData"
    referer_url = "https://www.amundietf.de/de/professionell"
    context = {
        "countryCode": "DEU",
        "countryName": "Germany",
        "googleCountryCode": "DE",
        "domainName": "www.amundietf.de",
        "bcp47Code": "de-DE",
        "languageName": "German",
        "gtmCode": "GTM-KJZTQF7",
        "languageCode": "de",
        "userProfileName": "INSTIT",
        "userProfileSlug": "instit",
        "portalProfileName": None,
        "portalProfileSlug": None,
    }
    characteristics = (
        "ISIN",
        "SHARE_MARKETING_NAME",
        "FUND_FUND_NAME",
        "POSITION_AS_OF_DATE",
        "FUND_BREAKDOWNS_AS_OF_DATE",
        "ARE_FUND_HOLDINGS_DISPLAYED",
        "FUND_REPLICATION_METHODOLOGY",
        "ASSET_CLASS",
        "CURRENCY",
    )
    composition_fields = (
        "date",
        "type",
        "bbg",
        "isin",
        "name",
        "weight",
        "quantity",
        "currency",
        "sector",
        "country",
        "countryOfRisk",
    )

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        return ProductRef(
            isin=candidate.isin,
            provider_id=candidate.provider_id,
            product_url=f"{self.referer_url}/products/{candidate.isin.lower()}",
            download_url=self.api_url,
            source_name=self.api_url,
        )

    @classmethod
    def _payload(cls, isin: str) -> dict[str, Any]:
        return {
            "context": cls.context,
            "productIds": [isin.upper()],
            "characteristics": list(cls.characteristics),
            "historics": [],
            "metrics": [],
            "breakDown": {
                "aggregationFields": [
                    "FUND_TOP10",
                    "INDEX_TOP10",
                    "FUND_SECTORS",
                    "FUND_COUNTRIES",
                    "FUND_CURRENCIES",
                ],
            },
            "productType": "PRODUCT",
            "composition": {"compositionFields": list(cls.composition_fields)},
        }

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    @classmethod
    def _product_as_of_date(cls, product_doc: dict[str, Any]) -> date | None:
        characteristics = product_doc.get("characteristics") or {}
        for item in (product_doc.get("composition") or {}).get("compositionData") or []:
            composition = item.get("compositionCharacteristics") or item
            parsed = cls._parse_date(composition.get("date"))
            if parsed:
                return parsed
        for key in ("POSITION_AS_OF_DATE", "FUND_BREAKDOWNS_AS_OF_DATE"):
            parsed = cls._parse_date(characteristics.get(key))
            if parsed:
                return parsed
        return None

    @staticmethod
    def _row_from_composition(item: dict[str, Any], rank: int) -> HoldingRow:
        composition = item.get("compositionCharacteristics") or item
        weight = composition.get("weight")
        if weight is None:
            weight = item.get("weight")
        return HoldingRow(
            rank=rank,
            symbol=composition.get("bbg"),
            holding_isin=composition.get("isin"),
            name=composition.get("name"),
            weight=float(weight) if weight is not None else None,
        )

    @staticmethod
    def _row_from_top10(item: dict[str, Any], rank: int) -> HoldingRow:
        props = item.get("additionalProperties") or {}
        weight = item.get("adjustedWeight")
        if weight is None:
            weight = item.get("weight")
        return HoldingRow(
            rank=rank,
            symbol=props.get("bbg"),
            holding_isin=props.get("isin"),
            name=item.get("aggregationName"),
            weight=float(weight) if weight is not None else None,
        )

    @classmethod
    def _rows_from_product(cls, product_doc: dict[str, Any]) -> list[HoldingRow]:
        composition_rows = (product_doc.get("composition") or {}).get("compositionData") or []
        if composition_rows:
            return [
                cls._row_from_composition(item, rank)
                for rank, item in enumerate(composition_rows, start=1)
                if isinstance(item, dict)
            ]
        for breakdown in product_doc.get("breakDowns") or []:
            if breakdown.get("aggregationField") == "FUND_TOP10":
                return [
                    cls._row_from_top10(item, rank)
                    for rank, item in enumerate(breakdown.get("breakDownData") or [], start=1)
                    if isinstance(item, dict)
                ]
        return []

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        response = self.session.post(
            self.api_url,
            json=self._payload(product.isin),
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/json",
                "Origin": "https://www.amundietf.de",
                "Referer": self.referer_url,
            },
        )
        response.raise_for_status()
        payload = response.json()
        products = payload.get("products") or []
        if not products:
            raise ProductResolutionError(f"amundi: no ProductAPI result for {product.isin}")
        product_doc = products[0]
        resolved_isin = str(
            (product_doc.get("characteristics") or {}).get("ISIN")
            or product_doc.get("productId")
            or ""
        ).upper()
        if resolved_isin and resolved_isin != product.isin.upper():
            raise ProductResolutionError(
                f"amundi: ProductAPI returned {resolved_isin} for {product.isin}"
            )
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=self._rows_from_product(product_doc),
            source_url=response.url,
            as_of_date=self._product_as_of_date(product_doc),
        )


class SpdrAdapter(LinkDiscoveryAdapter):
    provider_ids = ("spdr",)
    fundfinder_url = "https://www.ssga.com/bin/v1/ssmp/fund/fundfinder"
    fundfinder_params = (
        {"country": "uk", "language": "en_gb", "role": "institutional", "ui": "fund-finder"},
        {"country": "de", "language": "en_gb", "role": "institutional", "ui": "fund-finder"},
        {"country": "de", "language": "de", "role": "intermediary", "ui": "fund-finder"},
        {"country": "us", "language": "en", "role": "institutional", "ui": "fund-finder"},
        {"country": "us", "language": "en", "role": "intermediary", "ui": "fund-finder"},
        {"country": "us", "language": "en", "role": "individual", "ui": "fund-finder"},
    )

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._fund_rows: list[dict[str, Any]] | None = None

    def _load_fund_rows(self) -> list[dict[str, Any]]:
        if self._fund_rows is not None:
            return self._fund_rows
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for params in self.fundfinder_params:
            response = self.session.get(
                self.fundfinder_url,
                params=params,
                timeout=60,
                allow_redirects=True,
                headers={
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": self._fundfinder_referer(params),
                },
            )
            response.raise_for_status()
            payload = response.json()
            funds = (payload.get("data") or {}).get("funds") or {}
            for key, fund_group in funds.items():
                if "etf" not in key.lower():
                    continue
                for row in fund_group.get("datas") or []:
                    if not isinstance(row, dict):
                        continue
                    fund_filter = str(row.get("fundFilter") or "")
                    fund_uri = str(row.get("fundUri") or "")
                    dedupe_key = f"{fund_filter}|{fund_uri}"
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    rows.append(row)
        self._fund_rows = rows
        return rows

    @staticmethod
    def _fundfinder_referer(params: dict[str, str]) -> str:
        country = params.get("country", "uk")
        language = params.get("language", "en_gb")
        role = params.get("role", "institutional")
        return f"https://www.ssga.com/{country}/{language}/{role}/fund-finder"

    @staticmethod
    def _row_matches_isin(row: dict[str, Any], isin: str) -> bool:
        needle = isin.upper()
        if needle in str(row.get("keywords") or "").upper():
            return True
        for doc_group in row.get("documentPdf") or []:
            for doc in doc_group.get("docs") or []:
                if needle in str(doc.get("path") or "").upper():
                    return True
        return False

    @staticmethod
    def _holdings_url_from_row(row: dict[str, Any]) -> str | None:
        for doc_group in row.get("documentPdf") or []:
            doc_type = str(doc_group.get("docType") or "").lower()
            if "holdings" not in doc_type:
                continue
            for doc in doc_group.get("docs") or []:
                path = str(doc.get("path") or "").strip()
                if path:
                    return urljoin("https://www.ssga.com", path)
        return None

    @staticmethod
    def _as_of_date_from_row(row: dict[str, Any]) -> date | None:
        value = row.get("asOfDate")
        if isinstance(value, list) and len(value) > 1:
            try:
                return date.fromisoformat(str(value[1])[:10])
            except ValueError:
                return None
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
        return None

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        for row in self._load_fund_rows():
            if not self._row_matches_isin(row, candidate.isin):
                continue
            download_url = self._holdings_url_from_row(row)
            if not download_url:
                raise ProductResolutionError(f"spdr: no daily holdings document for {candidate.isin}")
            return ProductRef(
                isin=candidate.isin.upper(),
                provider_id=candidate.provider_id,
                product_url=urljoin("https://www.ssga.com", str(row.get("fundUri") or "")),
                download_url=download_url,
                source_name=download_url,
            )
        raise ProductResolutionError(f"spdr: no fund finder match for {candidate.isin}")

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"spdr: no download URL for {product.isin}")
        response = self._get(product.download_url)
        filename = _filename_from_headers(dict(response.headers), product.download_url)
        holdings = parse_holdings(
            response.content,
            content_type=response.headers.get("content-type"),
            filename=filename,
        )
        as_of_date = None
        for row in self._load_fund_rows():
            if self._row_matches_isin(row, product.isin):
                as_of_date = self._as_of_date_from_row(row)
                break
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=holdings,
            source_url=response.url,
            as_of_date=as_of_date,
        )


class JpmorganAdapter(LinkDiscoveryAdapter):
    provider_ids = ("jpmorgan",)
    download_url = "https://am.jpmorgan.com/FundsMarketingHandler/excel"
    product_url_template = "https://am.jpmorgan.com/gb/en/asset-management/adv/products/dynamic-pdp/{isin}"

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        query = urlencode({
            "type": "dailyMFHoldings",
            "cusip": isin,
            "country": "GB",
            "role": "adv",
            "locale": "en_GB",
        })
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=self.product_url_template.format(isin=isin.lower()),
            download_url=f"{self.download_url}?{query}",
            source_name=self.download_url,
        )

    @staticmethod
    def _as_of_date_from_filename(filename: str | None) -> date | None:
        if not filename:
            return None
        match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", filename)
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(0))
        except ValueError:
            return None

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"jpmorgan: no download URL for {product.isin}")
        response = self._get(product.download_url)
        filename = _filename_from_headers(dict(response.headers), product.download_url)
        holdings = parse_holdings(
            response.content,
            content_type=response.headers.get("content-type"),
            filename=filename,
        ) if response.content else []
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=holdings,
            source_url=response.url,
            as_of_date=self._as_of_date_from_filename(filename),
        )


class HsbcAdapter(LinkDiscoveryAdapter):
    provider_ids = ("hsbc",)
    download_url_template = "https://www.assetmanagement.hsbc.co.uk/api/v1/download/document/{isin}/gb/en/holdings"
    product_url_template = "https://www.assetmanagement.hsbc.co.uk/en/intermediary/funds/{isin}"

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        isin_path = isin.lower()
        download_url = self.download_url_template.format(isin=quote(isin_path))
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=self.product_url_template.format(isin=quote(isin_path)),
            download_url=download_url,
            source_name=download_url,
        )

    @staticmethod
    def _coerce_date(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None
        for candidate in (text[:10], text.replace("/", "-")[:10]):
            try:
                return date.fromisoformat(candidate)
            except ValueError:
                pass
        return None

    @classmethod
    def _as_of_date_from_excel(cls, content: bytes) -> date | None:
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover - parser will raise if Excel support is absent
            return None
        try:
            raw = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None, nrows=12)
        except Exception:  # noqa: BLE001 - metadata is best-effort, holdings parsing still decides success
            return None
        for row_idx in range(len(raw)):
            values = raw.iloc[row_idx].tolist()
            for col_idx, value in enumerate(values):
                if str(value or "").strip().lower() != "date":
                    continue
                for next_value in values[col_idx + 1:]:
                    parsed = cls._coerce_date(next_value)
                    if parsed:
                        return parsed
        return None

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"hsbc: no download URL for {product.isin}")
        response = self.session.get(
            product.download_url,
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
                "Referer": product.product_url or "https://www.assetmanagement.hsbc.co.uk/en/intermediary/funds",
            },
        )
        response.raise_for_status()
        filename = _filename_from_headers(dict(response.headers), product.download_url)
        holdings = parse_holdings(
            response.content,
            content_type=response.headers.get("content-type"),
            filename=filename,
        ) if response.content else []
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=holdings,
            source_url=response.url,
            as_of_date=self._as_of_date_from_excel(response.content),
        )


class FidelityAdapter(LinkDiscoveryAdapter):
    provider_ids = ("fidelity",)
    factsheet_url_template = "https://www.fidelity.co.uk/factsheet-data/factsheet/{isin}"

    @staticmethod
    def _portfolio_url_from_url(url: str) -> str:
        base_url = url.split("?", 1)[0].rstrip("/")
        if base_url.endswith("/portfolio"):
            return base_url
        if "/" in base_url:
            return base_url.rsplit("/", 1)[0] + "/portfolio"
        return base_url + "/portfolio"

    @staticmethod
    def _next_data_from_html(page_html: str) -> dict[str, Any]:
        match = re.search(
            r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
            page_html,
            re.S | re.I,
        )
        if not match:
            raise ProductResolutionError("fidelity: __NEXT_DATA__ not found on factsheet page")
        try:
            return json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError as exc:
            raise ProductResolutionError("fidelity: factsheet JSON could not be parsed") from exc

    @classmethod
    def _state_from_html(cls, page_html: str) -> dict[str, Any]:
        data = cls._next_data_from_html(page_html)
        state = data.get("props", {}).get("pageProps", {}).get("initialState", {})
        if not isinstance(state, dict):
            raise ProductResolutionError("fidelity: missing factsheet state")
        return state

    @staticmethod
    def _as_of_date_from_state(state: dict[str, Any]) -> date | None:
        fund_data = state.get("fund", {}).get("fundData", {})
        if not isinstance(fund_data, dict):
            return None
        for key in ("portfolioDate", "asOfDate", "holdingsDate"):
            value = fund_data.get(key)
            if not value:
                continue
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
        return None

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text if text and text not in {"-", "--"} else None

    @staticmethod
    def _percent_weight(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None
        text = text.replace("%", "").replace(",", "")
        try:
            return float(text) / 100
        except ValueError:
            return None

    @classmethod
    def _rows_from_state(cls, state: dict[str, Any]) -> list[HoldingRow]:
        holdings = (
            state.get("fund", {})
            .get("portfolio", {})
            .get("holdings", {})
            .get("portfolioHoldings", [])
        )
        if not isinstance(holdings, list):
            return []
        rows: list[HoldingRow] = []
        for item in holdings:
            if not isinstance(item, dict):
                continue
            name = cls._text(item.get("securityName") or item.get("name"))
            symbol = cls._text(item.get("ticker") or item.get("symbol"))
            if not name and not symbol:
                continue
            rows.append(
                HoldingRow(
                    rank=len(rows) + 1,
                    symbol=symbol,
                    holding_isin=cls._text(item.get("isin")),
                    name=name,
                    weight=cls._percent_weight(item.get("weighting") or item.get("weight")),
                )
            )
        return rows

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        response = self._get(self.factsheet_url_template.format(isin=quote(isin)))
        self._state_from_html(response.text)
        portfolio_url = self._portfolio_url_from_url(response.url)
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=portfolio_url,
            download_url=portfolio_url,
            source_name=response.url,
        )

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"fidelity: no portfolio URL for {product.isin}")
        response = self._get(product.download_url)
        state = self._state_from_html(response.text)
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=self._rows_from_state(state),
            source_url=response.url,
            as_of_date=self._as_of_date_from_state(state),
        )


class DekaAdapter(LinkDiscoveryAdapter):
    provider_ids = ("deka",)
    product_url_template = "https://www.deka-etf.de/etfs/{isin}"

    @staticmethod
    def _parse_weight(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number / 100 if abs(number) > 1 else number
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None
        is_percent = "%" in text
        text = text.replace("%", "").strip()
        if "," in text and "." not in text:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
        try:
            number = float(text)
        except ValueError:
            return None
        return number / 100 if is_percent or abs(number) > 1 else number

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text if text and text.lower() not in {"nan", "none", "null"} else None

    @staticmethod
    def _holding_isin(value: Any) -> str | None:
        text = str(value or "").strip().upper()
        return text if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", text) else None

    @classmethod
    def _rows_from_records(cls, records: list[dict[str, Any]]) -> list[HoldingRow]:
        rows: list[HoldingRow] = []
        for record in records:
            name = cls._text(record.get("Holding Name") or record.get("holding name"))
            holding_isin = cls._holding_isin(record.get("ISIN") or record.get("isin"))
            if not name and not holding_isin:
                continue
            rows.append(
                HoldingRow(
                    rank=len(rows) + 1,
                    symbol=None,
                    holding_isin=holding_isin,
                    name=name,
                    weight=cls._parse_weight(record.get("Gewichtung") or record.get("gewichtung")),
                )
            )
        return rows

    @classmethod
    def _rows_from_excel(cls, content: bytes) -> list[HoldingRow]:
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - pandas is part of the local ETF stack
            raise ProductResolutionError("deka: pandas is required to parse composition XLSX") from exc
        try:
            frame = pd.read_excel(io.BytesIO(content), sheet_name="Fondszusammensetzung")
        except Exception as exc:  # noqa: BLE001
            raise ProductResolutionError("deka: could not read Fondszusammensetzung sheet") from exc
        return cls._rows_from_records(frame.to_dict("records"))

    @staticmethod
    def _as_of_date_from_text(value: str | None) -> date | None:
        if not value:
            return None
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", value)
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None

    @classmethod
    def _download_url_from_composition(cls, page_html: str, base_url: str) -> str | None:
        match = re.search(r"""href=["']([^"']*composition_download\?date=[^"']+)["']""", page_html, re.I)
        if not match:
            return None
        return urljoin(base_url, html.unescape(match.group(1)))

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        response = self._get(self.product_url_template.format(isin=quote(isin)))
        if isin not in response.text:
            raise ProductResolutionError(f"deka: product page did not contain {isin}")
        composition_url = response.url.rstrip("/") + "/composition_data"
        composition = self.session.get(
            composition_url,
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "text/html,*/*",
                "Referer": response.url,
                "Turbo-Frame": "composition-data",
            },
        )
        composition.raise_for_status()
        download_url = self._download_url_from_composition(composition.text, composition.url)
        if not download_url:
            raise ProductResolutionError(f"deka: composition download link not found for {isin}")
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=response.url,
            download_url=download_url,
            source_name=download_url,
        )

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"deka: no composition download URL for {product.isin}")
        response = self.session.get(
            product.download_url,
            timeout=90,
            allow_redirects=True,
            headers={
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
                "Referer": product.product_url or "https://www.deka-etf.de/etfs",
            },
        )
        response.raise_for_status()
        filename = _filename_from_headers(dict(response.headers), response.url)
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=self._rows_from_excel(response.content),
            source_url=response.url,
            as_of_date=self._as_of_date_from_text(response.url) or self._as_of_date_from_text(filename),
        )


class OssiamAdapter(LinkDiscoveryAdapter):
    provider_ids = ("ossiam",)
    api_base_url = "https://api.ossiam.net/front.shareClass"
    country_keys = (
        "france",
        "germany",
        "luxembourg",
        "italy",
        "switzerland",
        "united kingdom",
        "ireland",
        "spain",
        "netherlands",
        "austria",
        "sweden",
        "finland",
        "denmark",
        "singapore",
        "czech republic",
        "slovakia",
    )

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._shareclasses_by_isin: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text if text and text.lower() not in {"nan", "none", "null", "-"} else None

    @staticmethod
    def _holding_isin(value: Any) -> str | None:
        text = str(value or "").strip().upper()
        return text if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", text) else None

    @staticmethod
    def _parse_weight(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number / 100 if abs(number) > 1 else number
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null", "-"}:
            return None
        text = text.replace("%", "").replace("\u00a0", "").replace(" ", "")
        if "," in text and "." not in text:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
        try:
            number = float(text)
        except ValueError:
            return None
        return number / 100 if abs(number) > 1 else number

    @classmethod
    def _rows_from_records(cls, records: list[dict[str, Any]]) -> list[HoldingRow]:
        rows: list[HoldingRow] = []
        for record in records:
            name = cls._text(record.get("EntityName") or record.get("entityName"))
            holding_isin = cls._holding_isin(record.get("ISINCode") or record.get("isinCode"))
            weight = cls._parse_weight(
                record.get("weight_in_percentage(non collateralized)")
                if "weight_in_percentage(non collateralized)" in record
                else record.get("weight_in_percentage(collateralized)")
            )
            if not name and not holding_isin:
                continue
            rows.append(
                HoldingRow(
                    rank=len(rows) + 1,
                    symbol=None,
                    holding_isin=holding_isin,
                    name=name,
                    weight=weight,
                )
            )
        return rows

    @classmethod
    def _rows_from_excel(cls, content: bytes) -> list[HoldingRow]:
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - pandas is part of the local ETF stack
            raise ProductResolutionError("ossiam: pandas is required to parse components XLSX") from exc
        try:
            frame = pd.read_excel(io.BytesIO(content), sheet_name="Compos")
        except Exception as exc:  # noqa: BLE001
            raise ProductResolutionError("ossiam: could not read Compos sheet") from exc
        return cls._rows_from_records(frame.to_dict("records"))

    @staticmethod
    def _as_of_date_from_filename(filename: str | None) -> date | None:
        if not filename:
            return None
        match = re.search(r"-(\d{2})(\d{2})(\d{2})\.xlsx\b", filename, re.I)
        if not match:
            return None
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(2000 + year, month, day)
        except ValueError:
            return None

    @staticmethod
    def _as_of_date_from_url(url: str | None) -> date | None:
        if not url:
            return None
        match = re.search(r"/(\d{12,14})(?:\D|$)", url)
        if not match:
            return None
        try:
            timestamp_ms = int(match.group(1))
        except ValueError:
            return None
        # Ossiam dates are encoded as local-midnight epoch millis; adding noon
        # avoids a UTC conversion landing on the previous day.
        return (datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc) + timedelta(hours=12)).date()

    def _request_json(self, url: str) -> Any:
        response = self.session.get(
            url,
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Origin": "https://www.ossiam.com",
                "Referer": "https://www.ossiam.com/EN/",
            },
        )
        response.raise_for_status()
        return response.json()

    def _load_shareclasses(self) -> dict[str, dict[str, Any]]:
        if self._shareclasses_by_isin is not None:
            return self._shareclasses_by_isin
        by_isin: dict[str, dict[str, Any]] = {}
        for country in self.country_keys:
            url = f"{self.api_base_url}/byCountry/{quote(country, safe='')}"
            try:
                payload = self._request_json(url)
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict) or not item.get("isEtf"):
                    continue
                isin = str(item.get("isin") or "").strip().upper()
                if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
                    by_isin.setdefault(isin, item)
        self._shareclasses_by_isin = by_isin
        return by_isin

    def _latest_composition_date_ms(self, shareclass_id: int) -> int:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        url = f"{self.api_base_url}/latestCompositionDate/{shareclass_id}/{shareclass_id}/{now_ms}"
        payload = self._request_json(url)
        if not isinstance(payload, list) or not payload:
            raise ProductResolutionError(f"ossiam: no latest composition date for shareclass {shareclass_id}")
        try:
            return int(payload[0])
        except (TypeError, ValueError) as exc:
            raise ProductResolutionError(f"ossiam: invalid composition date for shareclass {shareclass_id}") from exc

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        product = self._load_shareclasses().get(isin)
        if not product:
            raise ProductResolutionError(f"ossiam: no public shareclass found for {isin}")
        try:
            shareclass_id = int(product["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductResolutionError(f"ossiam: invalid shareclass id for {isin}") from exc
        date_ms = self._latest_composition_date_ms(shareclass_id)
        download_url = f"{self.api_base_url}/componentsFile/{shareclass_id}/{shareclass_id}/{date_ms}"
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=f"https://www.ossiam.com/EN/product/{shareclass_id}",
            download_url=download_url,
            source_name=str(product.get("name") or download_url),
        )

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"ossiam: no components download URL for {product.isin}")
        response = self.session.get(
            product.download_url,
            timeout=90,
            allow_redirects=True,
            headers={
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
                "Origin": "https://www.ossiam.com",
                "Referer": product.product_url or "https://www.ossiam.com/EN/",
            },
        )
        response.raise_for_status()
        filename = _filename_from_headers(dict(response.headers), response.url)
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=self._rows_from_excel(response.content),
            source_url=response.url,
            as_of_date=self._as_of_date_from_filename(filename) or self._as_of_date_from_url(response.url),
        )


class GlobalXAdapter(LinkDiscoveryAdapter):
    provider_ids = ("global_x",)
    explore_url = "https://globalxetfs.eu/explore"
    fund_url_template = "https://globalxetfs.eu/funds/{slug}"
    csv_path_pattern = re.compile(r"/api/funds/[a-z0-9-]+/(?:topholdingscsv|basketconstituentscsv)", re.I)

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._slugs: list[str] | None = None
        self._indexed_slugs: set[str] = set()
        self._products_by_isin: dict[str, dict[str, Any]] = {}
        self._all_products_indexed = False

    @staticmethod
    def _extract_explore_products(page_html: str) -> list[dict[str, str]]:
        products: list[dict[str, str]] = []
        pattern = re.compile(
            r'\{\\"ETF_NAME\\":\\"(?P<name>.*?)\\",\\"PRIMARY_TICKER\\":\\"(?P<ticker>.*?)\\",'
            r'\\"PRIMARY_ISIN\\":\\"(?P<isin>[A-Z]{2}[A-Z0-9]{10})\s*\\".*?'
            r'\\"PARENT_ID\\":\\"(?P<parent_id>\d+)\\"',
            re.S,
        )
        for match in pattern.finditer(page_html):
            ticker = html.unescape(match.group("ticker")).strip()
            isin = match.group("isin").upper()
            if not ticker or not isin:
                continue
            products.append({
                "name": html.unescape(match.group("name")).replace("\\u0026", "&").strip(),
                "ticker": ticker,
                "isin": isin,
                "parent_id": match.group("parent_id"),
                "slug": ticker.lower(),
            })
        return products

    @staticmethod
    def _extract_shareclass_isins(page_html: str) -> list[str]:
        isins: list[str] = []
        pattern = re.compile(
            r'\\?"(?:isin|accIsin|distIsin)\\?"\s*:\s*\\?"([A-Z]{2}[A-Z0-9]{10})\\?"',
            re.I,
        )
        for match in pattern.finditer(page_html):
            isin = match.group(1).upper()
            if isin not in isins:
                isins.append(isin)
        return isins

    @classmethod
    def _extract_csv_links(cls, page_html: str) -> list[str]:
        links: list[str] = []
        for match in cls.csv_path_pattern.finditer(page_html):
            path = match.group(0)
            if path not in links:
                links.append(path)
        links.sort(key=lambda value: 0 if value.lower().endswith("/topholdingscsv") else 1)
        return links

    def _load_explore_products(self) -> list[str]:
        if self._slugs is not None:
            return self._slugs
        response = self._get(self.explore_url)
        slugs: list[str] = []
        for product in self._extract_explore_products(response.text):
            slug = product["slug"]
            if slug not in slugs:
                slugs.append(slug)
            self._products_by_isin.setdefault(product["isin"], {
                "slug": slug,
                "ticker": product["ticker"],
                "product_url": self.fund_url_template.format(slug=slug),
                "download_url": None,
            })
        self._slugs = slugs
        return slugs

    def _index_slug(self, slug: str) -> None:
        if slug in self._indexed_slugs:
            return
        product_url = self.fund_url_template.format(slug=quote(slug.lower()))
        try:
            response = self._get(product_url)
        except Exception:
            self._indexed_slugs.add(slug)
            return
        page_url = response.url
        if "/not-available/" in page_url or "/not-available" in page_url:
            self._indexed_slugs.add(slug)
            return
        csv_links = self._extract_csv_links(response.text)
        isins = self._extract_shareclass_isins(response.text)
        if not csv_links:
            csv_links = [f"/api/funds/{slug}/topholdingscsv"]
        product = {
            "slug": slug,
            "ticker": slug.upper(),
            "product_url": page_url,
            "download_url": urljoin(page_url, csv_links[0]),
        }
        for isin in isins:
            self._products_by_isin[isin] = product
        self._indexed_slugs.add(slug)

    def _load_products_by_isin(self) -> dict[str, dict[str, Any]]:
        if self._all_products_indexed:
            return self._products_by_isin
        for slug in self._load_explore_products():
            self._index_slug(slug)
        self._all_products_indexed = True
        return self._products_by_isin

    def _find_product_by_isin(self, isin: str) -> dict[str, Any] | None:
        self._load_explore_products()
        product = self._products_by_isin.get(isin)
        if product and product.get("download_url"):
            return product
        if product and product.get("slug"):
            self._index_slug(str(product["slug"]))
            product = self._products_by_isin.get(isin)
            if product and product.get("download_url"):
                return product
        for slug in self._slugs or []:
            self._index_slug(slug)
            product = self._products_by_isin.get(isin)
            if product and product.get("download_url"):
                return product
        self._all_products_indexed = True
        return None

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        product = self._find_product_by_isin(isin)
        if not product:
            raise ProductResolutionError(f"global_x: no EU product page resolved for {candidate.isin}")
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=str(product.get("product_url") or ""),
            download_url=str(product.get("download_url") or ""),
            source_name=str(product.get("slug") or ""),
        )

    @staticmethod
    def _as_of_date_from_csv(content: bytes) -> date | None:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1", errors="replace")
        first_data = next((line for line in text.splitlines()[1:] if line.strip()), "")
        if not first_data:
            return None
        value = first_data.split(",", 1)[0].strip().strip('"')
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"global_x: no download URL for {product.isin}")
        response = self.session.get(
            product.download_url,
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "text/csv,*/*",
                "Referer": product.product_url or "https://globalxetfs.eu/explore",
            },
        )
        response.raise_for_status()
        filename = _filename_from_headers(dict(response.headers), product.download_url)
        holdings = parse_holdings(
            response.content,
            content_type=response.headers.get("content-type"),
            filename=filename,
        ) if response.content else []
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=holdings,
            source_url=response.url,
            as_of_date=self._as_of_date_from_csv(response.content),
        )


class ProSharesAdapter(LinkDiscoveryAdapter):
    provider_ids = ("proshares",)
    holdings_csv_url = "https://accounts.profunds.com/etfdata/psdlyhld.csv"
    product_categories = ("leveraged-and-inverse", "strategic")

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._holdings_by_ticker: dict[str, list[dict[str, str]]] | None = None
        self._as_of_date: date | None = None
        self._product_tickers: list[str] | None = None
        self._cusip_to_product: dict[str, dict[str, str]] = {}
        self._indexed_tickers: set[str] = set()
        self._all_products_indexed = False

    @staticmethod
    def _normalize_header(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    @staticmethod
    def _parse_as_of_line(value: str | None) -> date | None:
        text = str(value or "").strip()
        match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if not match:
            return None
        month, day, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        try:
            return date(year, month, day)
        except ValueError:
            return None

    @classmethod
    def _parse_holdings_csv(cls, content: bytes) -> tuple[dict[str, list[dict[str, str]]], date | None]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1", errors="replace")
        lines = text.splitlines()
        as_of_date = None
        for line in lines[:10]:
            if "AS OF" in line.upper():
                as_of_date = cls._parse_as_of_line(line)
                break
        header_idx = next((idx for idx, line in enumerate(lines[:20]) if line.lower().startswith("fund ticker")), None)
        if header_idx is None:
            raise ProductResolutionError("proshares: daily holdings CSV header not found")
        reader = csv.DictReader(lines[header_idx:])
        holdings: dict[str, list[dict[str, str]]] = {}
        for raw_row in reader:
            row = {cls._normalize_header(key): str(value or "").strip() for key, value in raw_row.items() if key is not None}
            ticker = row.get("fundticker", "").upper()
            if not ticker:
                continue
            holdings.setdefault(ticker, []).append(row)
        return holdings, as_of_date

    def _load_holdings_by_ticker(self) -> dict[str, list[dict[str, str]]]:
        if self._holdings_by_ticker is not None:
            return self._holdings_by_ticker
        response = self.session.get(
            self.holdings_csv_url,
            timeout=120,
            allow_redirects=True,
            headers={"Accept": "text/csv,*/*"},
        )
        response.raise_for_status()
        self._holdings_by_ticker, self._as_of_date = self._parse_holdings_csv(response.content)
        self._product_tickers = sorted(self._holdings_by_ticker)
        return self._holdings_by_ticker

    @staticmethod
    def _cusip_from_isin(isin: str) -> str | None:
        value = isin.strip().upper()
        if not re.fullmatch(r"US[A-Z0-9]{9}\d", value):
            return None
        return value[2:11]

    @staticmethod
    def _extract_cusip(page_html: str) -> str | None:
        patterns = (
            r'id="snapshot-cusip"[^>]*>\s*([A-Z0-9]{9})\s*<',
            r">\s*CUSIP\s*</span>\s*<div><span[^>]*>\s*([A-Z0-9]{9})\s*<",
        )
        for pattern in patterns:
            match = re.search(pattern, page_html, re.I)
            if match:
                return match.group(1).upper()
        return None

    def _product_url(self, category: str, ticker: str) -> str:
        return f"https://www.proshares.com/our-etfs/{category}/{quote(ticker.lower())}/"

    def _index_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker in self._indexed_tickers:
            return
        for category in self.product_categories:
            url = self._product_url(category, ticker)
            try:
                response = self.session.get(
                    url,
                    timeout=45,
                    allow_redirects=True,
                    headers={"Accept": "text/html,*/*"},
                )
            except Exception:
                continue
            if response.status_code == 404:
                continue
            cusip = self._extract_cusip(response.text)
            if cusip:
                self._cusip_to_product[cusip] = {
                    "ticker": ticker,
                    "product_url": response.url,
                    "category": category,
                }
                break
        self._indexed_tickers.add(ticker)

    def _find_product_by_cusip(self, cusip: str) -> dict[str, str] | None:
        self._load_holdings_by_ticker()
        product = self._cusip_to_product.get(cusip)
        if product:
            return product
        for ticker in self._product_tickers or []:
            self._index_ticker(ticker)
            product = self._cusip_to_product.get(cusip)
            if product:
                return product
        self._all_products_indexed = True
        return None

    @staticmethod
    def _text(value: str | None) -> str | None:
        text = str(value or "").strip()
        return text if text and text != "--" else None

    @classmethod
    def _rows_from_records(cls, records: list[dict[str, str]]) -> list[HoldingRow]:
        rows: list[HoldingRow] = []
        for row in records:
            name = cls._text(row.get("securitydescription"))
            symbol = cls._text(row.get("securityticker"))
            if not name and not symbol:
                continue
            rows.append(
                HoldingRow(
                    rank=len(rows) + 1,
                    symbol=symbol,
                    holding_isin=None,
                    name=name,
                    weight=None,
                )
            )
        return rows

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        cusip = self._cusip_from_isin(candidate.isin)
        if not cusip:
            raise ProductResolutionError(f"proshares: cannot derive CUSIP from {candidate.isin}")
        product = self._find_product_by_cusip(cusip)
        if not product:
            raise ProductResolutionError(f"proshares: no product ticker resolved for CUSIP {cusip}")
        return ProductRef(
            isin=candidate.isin.upper(),
            provider_id=candidate.provider_id,
            product_url=product["product_url"],
            download_url=self.holdings_csv_url,
            source_name=product["ticker"],
        )

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        ticker = str(product.source_name or "").upper()
        records = self._load_holdings_by_ticker().get(ticker, [])
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=self._rows_from_records(records),
            source_url=self.holdings_csv_url,
            as_of_date=self._as_of_date,
        )


class LgAdapter(LinkDiscoveryAdapter):
    provider_ids = ("lg",)
    fund_center_url = "https://fundcentres.landg.com/srp/api/fund-centre/47?audience=146&language=1"
    portfolio_part_url = "https://fundcentres.landg.com/srp/api/part"
    portfolio_part_id = 12035
    audience_id = 146
    language_id = 1
    route_id = 6612

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._products_by_isin: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def _field_names(metadata: dict[str, Any], key: str) -> list[str]:
        fields = metadata.get(key) or []
        return [str(field.get("code_name") or "") for field in fields if isinstance(field, dict)]

    @staticmethod
    def _record(names: list[str], values: list[Any] | tuple[Any, ...] | None) -> dict[str, Any]:
        return dict(zip(names, values or []))

    @classmethod
    def _products_from_fund_center(cls, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        metadata = payload.get("metadata") or {}
        fund_names = cls._field_names(metadata, "fund_fields")
        share_names = cls._field_names(metadata, "share_class_fields")
        products: dict[str, dict[str, Any]] = {}
        for fund in payload.get("funds") or []:
            if not isinstance(fund, dict):
                continue
            fund_id = fund.get("id")
            fund_data = cls._record(fund_names, fund.get("data"))
            for share_class in fund.get("share_classes") or []:
                if not isinstance(share_class, dict):
                    continue
                share_data = cls._record(share_names, share_class.get("data"))
                isin = str(share_data.get("shareclassISIN") or "").strip().upper()
                share_class_id = share_class.get("id")
                if not isin or not fund_id or not share_class_id:
                    continue
                page_url = share_data.get("shareclassPageURL") or fund_data.get("fundPageURL")
                products[isin] = {
                    "fund_id": str(fund_id),
                    "share_class_id": str(share_class_id),
                    "fund_name": fund_data.get("name"),
                    "shareclass_name": share_data.get("shareclassDescriptor") or share_data.get("shortName"),
                    "product_url": urljoin("https://fundcentres.landg.com", str(page_url or "")),
                }
        return products

    def _load_products(self) -> dict[str, dict[str, Any]]:
        if self._products_by_isin is not None:
            return self._products_by_isin
        response = self._get(self.fund_center_url)
        self._products_by_isin = self._products_from_fund_center(response.json())
        return self._products_by_isin

    @staticmethod
    def _extract_route_id(page_html: str) -> str | None:
        match = re.search(r"""data-route-id=["'](\d+)["']""", page_html, re.I)
        return match.group(1) if match else None

    @staticmethod
    def _extract_fund_id(page_html: str) -> str | None:
        match = re.search(r"""data-fund-id=["'](\d+)["']""", page_html, re.I)
        return match.group(1) if match else None

    @staticmethod
    def _download_url_for_shareclass(portfolio_html: str, share_class_id: str) -> str | None:
        block_match = re.search(
            rf'"{re.escape(str(share_class_id))}"\s*:\s*\[(?P<body>.*?)\]\s*,?\s*(?:"\d+"\s*:|[}}])',
            portfolio_html,
            re.S,
        )
        if not block_match:
            return None
        body = block_match.group("body")
        for url_match in re.finditer(r"""url\s*:\s*["']([^"']+)["']""", body, re.I):
            url = html.unescape(url_match.group(1))
            if "holding" in url.lower() and (url.lower().endswith(".csv") or "fundholdings" in url.lower()):
                return url
        return None

    def _resolve_download_url(self, product_meta: dict[str, Any]) -> str:
        fund_id = str(product_meta["fund_id"])
        share_class_id = str(product_meta["share_class_id"])
        route_id = str(product_meta.get("route_id") or self.route_id)
        response = self.session.get(
            self.portfolio_part_url,
            params={
                "id": self.portfolio_part_id,
                "audience": self.audience_id,
                "route": route_id,
                "version": "live",
                "languageId": self.language_id,
                "fund_id": fund_id,
                "share_class_id": share_class_id,
            },
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()
        download_url = self._download_url_for_shareclass(response.text, share_class_id)
        if not download_url:
            raise ProductResolutionError(f"lg: no full holdings CSV link for share class {share_class_id}")
        return download_url

    @staticmethod
    def _as_of_date_from_csv(content: bytes) -> date | None:
        text = content.decode("utf-8-sig", errors="replace")
        match = re.search(r"Basket Trade Date\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
        if match:
            try:
                return date.fromisoformat(match.group(1))
            except ValueError:
                return None
        return None

    @staticmethod
    def _clean_rows(rows: list[HoldingRow]) -> list[HoldingRow]:
        cleaned: list[HoldingRow] = []
        for row in rows:
            if not row.holding_isin and (row.weight is None or abs(row.weight) < 1e-12):
                continue
            cleaned.append(
                HoldingRow(
                    rank=len(cleaned) + 1,
                    symbol=row.symbol,
                    holding_isin=row.holding_isin,
                    name=row.name,
                    weight=row.weight,
                )
            )
        return cleaned

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        product_meta = self._load_products().get(isin)
        if not product_meta:
            raise ProductResolutionError(f"lg: ISIN {isin} not found in public fund centre")
        product_url = str(product_meta.get("product_url") or "")
        if product_url:
            try:
                page = self._get(product_url)
                route_id = self._extract_route_id(page.text)
                fund_id = self._extract_fund_id(page.text)
                if route_id:
                    product_meta = {**product_meta, "route_id": route_id}
                if fund_id:
                    product_meta = {**product_meta, "fund_id": fund_id}
            except Exception:
                pass
        download_url = self._resolve_download_url(product_meta)
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=product_url,
            download_url=download_url,
            source_name=str(product_meta.get("share_class_id") or ""),
        )

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        if not product.download_url:
            raise ProductResolutionError(f"lg: missing holdings download URL for {product.isin}")
        response = self._get(product.download_url)
        rows = self._clean_rows(parse_holdings(
            response.content,
            content_type=response.headers.get("content-type"),
            filename=_filename_from_headers(response.headers, response.url),
        ))
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=rows,
            source_url=response.url,
            as_of_date=self._as_of_date_from_csv(response.content),
        )


class VanEckAdapter(LinkDiscoveryAdapter):
    provider_ids = ("vaneck",)
    listing_url = "https://www.vaneck.com/uk/en/prospectuses/etfs"
    holdings_content_url = "https://www.vaneck.com/Main/HoldingsBlock/GetContent/"

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._product_urls: list[str] | None = None
        self._indexed_urls: set[str] = set()
        self._products_by_isin: dict[str, dict[str, Any]] = {}
        self._all_products_indexed = False

    @staticmethod
    def _isin_is_valid(value: str) -> bool:
        isin = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
            return False
        digits = ""
        for char in isin:
            if char.isdigit():
                digits += char
            else:
                digits += str(ord(char) - 55)
        total = 0
        reverse_digits = digits[::-1]
        for idx, char in enumerate(reverse_digits):
            number = int(char)
            if idx % 2 == 1:
                number *= 2
            total += number // 10 + number % 10
        return total % 10 == 0

    @classmethod
    def _extract_isins(cls, page_html: str) -> list[str]:
        isins: list[str] = []
        for match in re.finditer(r"\b[A-Z]{2}[A-Z0-9]{10}\b", page_html):
            isin = match.group(0).upper()
            if cls._isin_is_valid(isin) and isin not in isins:
                isins.append(isin)
        return isins

    @staticmethod
    def _extract_product_urls(listing_html: str) -> list[str]:
        urls: list[str] = []
        for raw_path in re.findall(r"/uk/en/investments/[^\"'<\s?#]+", listing_html):
            path = raw_path.rstrip("/")
            if not path.startswith("/uk/en/investments/"):
                continue
            if path.endswith("/overview"):
                url = f"https://www.vaneck.com{path}/"
            else:
                url = f"https://www.vaneck.com{path}/overview/"
            if url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def _extract_ticker(page_html: str) -> str | None:
        patterns = (
            r'data-ticker="([^"]+)"',
            r'"Ticker"\s*:\s*"([^"]+)"',
        )
        for pattern in patterns:
            match = re.search(pattern, page_html, re.I)
            if match:
                ticker = html.unescape(match.group(1)).strip()
                if ticker:
                    return ticker
        return None

    @staticmethod
    def _extract_blocks(page_html: str) -> list[dict[str, str]]:
        blocks: list[dict[str, str]] = []
        for tag in re.findall(r"<ve-holdingsblock[^>]+>", page_html, re.I):
            block_match = re.search(r'data-blockid="(\d+)"', tag, re.I)
            page_match = re.search(r'data-pageid="(\d+)"', tag, re.I)
            if not block_match or not page_match:
                continue
            block = {"blockid": block_match.group(1), "pageid": page_match.group(1)}
            if block not in blocks:
                blocks.append(block)
        return blocks

    @classmethod
    def _product_from_page(cls, page_url: str, page_html: str) -> dict[str, Any] | None:
        ticker = cls._extract_ticker(page_html)
        blocks = cls._extract_blocks(page_html)
        isins = cls._extract_isins(page_html)
        if not ticker or not blocks or not isins:
            return None
        return {"product_url": page_url, "ticker": ticker, "blocks": blocks, "isins": isins}

    def _load_product_urls(self) -> list[str]:
        if self._product_urls is not None:
            return self._product_urls
        response = self._get(self.listing_url)
        self._product_urls = self._extract_product_urls(response.text)
        return self._product_urls

    def _index_product_url(self, product_url: str) -> None:
        if product_url in self._indexed_urls:
            return
        try:
            page = self._get(product_url)
            product = self._product_from_page(page.url, page.text)
        except Exception:
            product = None
        if product:
            for isin in product["isins"]:
                self._products_by_isin[isin.upper()] = product
        self._indexed_urls.add(product_url)

    def _load_products_by_isin(self) -> dict[str, dict[str, Any]]:
        if self._all_products_indexed:
            return self._products_by_isin
        for product_url in self._load_product_urls():
            self._index_product_url(product_url)
        self._all_products_indexed = True
        return self._products_by_isin

    def _find_product_by_isin(self, isin: str) -> dict[str, Any] | None:
        product = self._products_by_isin.get(isin)
        if product:
            return product
        for product_url in self._load_product_urls():
            self._index_product_url(product_url)
            product = self._products_by_isin.get(isin)
            if product:
                return product
        self._all_products_indexed = True
        return None

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        isin = candidate.isin.upper()
        product = self._find_product_by_isin(isin)
        if not product:
            raise ProductResolutionError(f"vaneck: no UK UCITS product page resolved for {candidate.isin}")
        source_name = json.dumps(
            {
                "ticker": product["ticker"],
                "blocks": product["blocks"],
                "product_url": product["product_url"],
            },
            separators=(",", ":"),
        )
        return ProductRef(
            isin=isin,
            provider_id=candidate.provider_id,
            product_url=product["product_url"],
            download_url=self.holdings_content_url,
            source_name=source_name,
        )

    @staticmethod
    def _source_payload(product: ProductRef) -> dict[str, Any]:
        if product.source_name:
            try:
                payload = json.loads(product.source_name)
            except json.JSONDecodeError as exc:
                raise ProductResolutionError(f"vaneck: invalid product source payload for {product.isin}") from exc
            if isinstance(payload, dict):
                return payload
        raise ProductResolutionError(f"vaneck: no product source payload for {product.isin}")

    @staticmethod
    def _parse_vaneck_date(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None
        for fmt in ("%d %b %Y", "%m/%d/%y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:19], fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _weight(value: Any) -> float | None:
        if value is None:
            return None
        raw = str(value).replace("%", "").replace(",", "").strip()
        if not raw:
            return None
        try:
            number = float(raw)
        except ValueError:
            return None
        if number != number:
            return None
        return number / 100.0 if number > 1.5 else number

    @classmethod
    def _rows_from_payload(cls, payload: dict[str, Any]) -> tuple[list[HoldingRow], date | None]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        holdings = data.get("Holdings") or data.get("holdings") or []
        rows: list[HoldingRow] = []
        as_of_date = cls._parse_vaneck_date(data.get("AsOfDate"))
        if not isinstance(holdings, list):
            return rows, as_of_date
        for item in holdings:
            if not isinstance(item, dict):
                continue
            if as_of_date is None:
                as_of_date = cls._parse_vaneck_date(item.get("AsOfDate") or item.get("DataDate"))
            symbol = str(item.get("HoldingTicker") or item.get("Label") or "").strip() or None
            name = str(item.get("HoldingName") or "").strip() or None
            holding_isin = str(item.get("ISIN") or "").strip().upper() or None
            rows.append(
                HoldingRow(
                    rank=len(rows) + 1,
                    symbol=symbol,
                    holding_isin=holding_isin,
                    name=name,
                    weight=cls._weight(item.get("Weight")),
                )
            )
        return [row for row in rows if row.name or row.symbol or row.holding_isin], as_of_date

    def _fetch_block(self, product: ProductRef, ticker: str, block: dict[str, str]) -> tuple[dict[str, Any], str]:
        response = self.session.get(
            self.holdings_content_url,
            params={
                "blockid": block["blockid"],
                "pageid": block["pageid"],
                "ticker": ticker,
                "reactlang": "en",
                "reactctr": "uk",
            },
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": product.product_url or self.listing_url,
            },
        )
        response.raise_for_status()
        return response.json(), response.url

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        source = self._source_payload(product)
        ticker = str(source.get("ticker") or "").strip()
        blocks = source.get("blocks") or []
        if not ticker or not isinstance(blocks, list):
            raise ProductResolutionError(f"vaneck: missing ticker or holdings blocks for {product.isin}")

        best_rows: list[HoldingRow] = []
        best_as_of_date: date | None = None
        best_url: str | None = None
        best_is_top_ten = True
        for block in blocks:
            if not isinstance(block, dict) or not block.get("blockid") or not block.get("pageid"):
                continue
            payload, source_url = self._fetch_block(product, ticker, block)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            rows, as_of_date = self._rows_from_payload(payload)
            is_top_ten = bool(data.get("IsTopTen"))
            if rows and ((not is_top_ten and best_is_top_ten) or len(rows) > len(best_rows)):
                best_rows = rows
                best_as_of_date = as_of_date
                best_url = source_url
                best_is_top_ten = is_top_ten
            if rows and not is_top_ten:
                break

        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=best_rows,
            source_url=best_url or product.product_url,
            as_of_date=best_as_of_date,
        )


class VanguardAdapter(LinkDiscoveryAdapter):
    provider_ids = ("vanguard",)
    product_list_url = "https://www.de.vanguard/professionell/anlageprodukte"
    gpx_url = "https://www.de.vanguard/gpx/graphql"
    resolve_query = """
      query ResolveVanguardFunds($portIds: [String!]!) {
        funds(portIds: $portIds) {
          portId
          profile {
            fundFullName
            productTypeLevel1
            assetClassificationLevel1
            identifiers(altIds: ["ISIN", "WKN Code", "Ticker"]) {
              altId
              altIdValue
            }
          }
        }
      }
    """
    holdings_query = """
      query FundsHoldingsQuery($portIds: [String!], $securityTypes: [String!], $lastItemKey: String) {
        funds(portIds: $portIds) {
          profile {
            fundFullName
            fundCurrency
            primarySectorEquityClassification
          }
        }
        borHoldings(portIds: $portIds) {
          holdings(limit: 1500, securityTypes: $securityTypes, lastItemKey: $lastItemKey) {
            items {
              issuerName
              securityLongDescription
              gicsSectorDescription
              icbSectorDescription
              icbIndustryDescription
              marketValuePercentage
              sedol1
              quantity
              ticker
              securityType
              finalMaturity
              effectiveDate
              marketValueBaseCurrency
              bloombergIsoCountry
              couponRate
            }
            totalHoldings
            lastItemKey
          }
        }
      }
    """

    def __init__(self, session=None) -> None:
        super().__init__(session=session)
        self._port_ids: list[str] | None = None
        self._products_by_isin: dict[str, dict[str, Any]] | None = None

    def _graphql(self, query: str, variables: dict[str, Any], operation_name: str) -> dict[str, Any]:
        response = self.session.post(
            self.gpx_url,
            json={"operationName": operation_name, "query": query, "variables": variables},
            timeout=60,
            allow_redirects=True,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/json",
                "Origin": "https://www.de.vanguard",
                "Referer": self.product_list_url,
                "X-Consumer-ID": "europe-ui",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise ProductResolutionError(f"vanguard: GraphQL errors {payload['errors'][:1]}")
        return payload.get("data") or {}

    @staticmethod
    def _extract_port_ids(page_html: str) -> list[str]:
        matches = re.findall(r'"portIds"\s*:\s*"([^"]+)"', page_html)
        if not matches:
            matches = re.findall(r"&quot;portIds&quot;\s*:\s*&quot;([^&]+)&quot;", page_html)
        ids: list[str] = []
        for raw in matches:
            for item in html.unescape(raw).split(","):
                port_id = item.strip()
                if port_id and port_id not in ids:
                    ids.append(port_id)
        return ids

    def _load_port_ids(self) -> list[str]:
        if self._port_ids is not None:
            return self._port_ids
        response = self._get(self.product_list_url)
        port_ids = self._extract_port_ids(response.text)
        if not port_ids:
            raise ProductResolutionError("vanguard: no portIds found in product list")
        self._port_ids = port_ids
        return port_ids

    @staticmethod
    def _chunks(values: list[str], size: int) -> list[list[str]]:
        return [values[idx: idx + size] for idx in range(0, len(values), size)]

    @staticmethod
    def _identifier(profile: dict[str, Any], alt_id: str) -> str | None:
        for item in profile.get("identifiers") or []:
            if str(item.get("altId") or "").lower() == alt_id.lower():
                value = str(item.get("altIdValue") or "").strip()
                return value or None
        return None

    def _load_products_by_isin(self) -> dict[str, dict[str, Any]]:
        if self._products_by_isin is not None:
            return self._products_by_isin
        products: dict[str, dict[str, Any]] = {}
        for chunk in self._chunks(self._load_port_ids(), 100):
            data = self._graphql(
                self.resolve_query,
                {"portIds": chunk},
                "ResolveVanguardFunds",
            )
            for fund in data.get("funds") or []:
                if not isinstance(fund, dict):
                    continue
                profile = fund.get("profile") or {}
                if str(profile.get("productTypeLevel1") or "").upper() != "ETF":
                    continue
                isin = self._identifier(profile, "ISIN")
                port_id = str(fund.get("portId") or "").strip()
                if isin and port_id:
                    products[isin.upper()] = {
                        "port_id": port_id,
                        "fund_name": profile.get("fundFullName"),
                        "asset_class": profile.get("assetClassificationLevel1"),
                    }
        self._products_by_isin = products
        return products

    @staticmethod
    def _product_url(port_id: str) -> str:
        return "https://www.de.vanguard/professionell/anlageprodukte?" + urlencode({"portId": port_id})

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        product = self._load_products_by_isin().get(candidate.isin.upper())
        if not product:
            raise ProductResolutionError(f"vanguard: no portId resolved for {candidate.isin}")
        port_id = str(product["port_id"])
        return ProductRef(
            isin=candidate.isin.upper(),
            provider_id=candidate.provider_id,
            product_url=self._product_url(port_id),
            download_url=f"{self.gpx_url}?{urlencode({'portId': port_id})}",
            source_name=port_id,
        )

    @staticmethod
    def _port_id_from_product(product: ProductRef) -> str:
        if product.source_name:
            return product.source_name
        parsed = urlparse(product.download_url or "")
        value = (parse_qs(parsed.query).get("portId") or [None])[0]
        if not value:
            raise ProductResolutionError(f"vanguard: no portId for {product.isin}")
        return value

    @staticmethod
    def _weight(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:
            return None
        return number / 100.0

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    @classmethod
    def _rows_from_items(cls, items: list[dict[str, Any]]) -> tuple[list[HoldingRow], date | None]:
        rows: list[HoldingRow] = []
        as_of_date: date | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            if as_of_date is None:
                as_of_date = cls._parse_date(item.get("effectiveDate"))
            name = item.get("securityLongDescription") or item.get("issuerName")
            symbol = item.get("ticker")
            rows.append(
                HoldingRow(
                    rank=len(rows) + 1,
                    symbol=str(symbol).strip() if symbol else None,
                    holding_isin=None,
                    name=str(name).strip() if name else None,
                    weight=cls._weight(item.get("marketValuePercentage")),
                )
            )
        return [row for row in rows if row.name or row.symbol or row.weight is not None], as_of_date

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        port_id = self._port_id_from_product(product)
        all_items: list[dict[str, Any]] = []
        last_item_key: str | None = None
        total_holdings: int | None = None
        seen_keys: set[str] = set()
        while True:
            data = self._graphql(
                self.holdings_query,
                {"portIds": [port_id], "securityTypes": None, "lastItemKey": last_item_key},
                "FundsHoldingsQuery",
            )
            holdings_doc = None
            for group in data.get("borHoldings") or []:
                if isinstance(group, dict) and group.get("holdings"):
                    holdings_doc = group.get("holdings")
                    break
            if not holdings_doc:
                break
            items = holdings_doc.get("items") or []
            all_items.extend(item for item in items if isinstance(item, dict))
            total_holdings = holdings_doc.get("totalHoldings") or total_holdings
            last_item_key = holdings_doc.get("lastItemKey")
            if not last_item_key or last_item_key in seen_keys:
                break
            seen_keys.add(last_item_key)
            if total_holdings is not None and len(all_items) >= int(total_holdings):
                break
        rows, as_of_date = self._rows_from_items(all_items)
        return HoldingsResult(
            isin=product.isin,
            provider_id=product.provider_id,
            holdings=rows,
            source_url=f"{self.gpx_url}#portId={port_id}",
            as_of_date=as_of_date,
        )


def default_adapters(session=None) -> list[LinkDiscoveryAdapter]:
    return [
        IsharesAdapter(session=session),
        XtrackersAdapter(session=session),
        AmundiAdapter(session=session),
        SpdrAdapter(session=session),
        JpmorganAdapter(session=session),
        HsbcAdapter(session=session),
        FidelityAdapter(session=session),
        DekaAdapter(session=session),
        OssiamAdapter(session=session),
        GlobalXAdapter(session=session),
        ProSharesAdapter(session=session),
        LgAdapter(session=session),
        VanEckAdapter(session=session),
        VanguardAdapter(session=session),
    ]
