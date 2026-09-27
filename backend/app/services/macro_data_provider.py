from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.integrations.types import IntegrationOperationSpec
from app.services.github_provider import IntegrationProviderError

MAX_RESPONSE_BYTES = 4_000_000
MAX_RESULTS = 25
FRED_BASE = "https://api.stlouisfed.org"
BLS_BASE = "https://api.bls.gov"
BEA_BASE = "https://apps.bea.gov"
EIA_BASE = "https://api.eia.gov"

_BLS_SERIES = {
    "CUUR0000SA0": "Consumer Price Index for All Urban Consumers: All items",
    "LNS14000000": "Unemployment rate",
    "CES0000000001": "Total nonfarm payroll employment",
}
_EIA_SERIES = {
    "PET.RWTC.D": "Cushing, OK WTI spot price",
    "NG.RNGWHHD.D": "Henry Hub natural gas spot price",
}
_EIA_SOURCE_URLS = {
    "PET.RWTC.D": "https://www.eia.gov/dnav/pet/hist/rwtcD.htm",
    "NG.RNGWHHD.D": "https://www.eia.gov/dnav/ng/hist/rngwhhdD.htm",
}
_BEA_RELEVANT_TERMS = ("gross domestic product", "personal consumption", "price index")


class MacroDataProviderAdapter(Protocol):
    provider_id: str

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        api_key: str | None,
    ) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


@dataclass(frozen=True)
class UrllibMacroDataProviderAdapter:
    """Bounded, provider-specific transport for macroeconomic source APIs."""

    provider_id: str

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        api_key: str | None,
    ) -> dict[str, Any]:
        since = _since(input_json.get("since"))
        limit = _limit(input_json.get("limit"))
        if self.provider_id == "fred" and operation.id == "fred.release.list":
            return self._fred(since, limit, api_key)
        if self.provider_id == "bls" and operation.id == "bls.series.latest":
            return self._bls(since, limit, api_key)
        if self.provider_id == "bea" and operation.id == "bea.series.latest":
            return self._bea(since, limit, api_key)
        if self.provider_id == "eia" and operation.id == "eia.series.latest":
            return self._eia(since, limit, api_key)
        raise IntegrationProviderError("operation_undeclared", "Macro data operation is unsupported")

    def _fred(self, since: datetime, limit: int, api_key: str | None) -> dict[str, Any]:
        if not api_key:
            raise IntegrationProviderError("connection_unavailable", "FRED API key is not configured")
        query = {
            "api_key": api_key,
            "file_type": "json",
            # The endpoint is newest-first; fetch a bounded page and filter
            # by the actual release date locally. FRED's realtime_* fields
            # describe vintages, not release dates.
            "limit": 250,
            "offset": 0,
        }
        payload = self._request_json(
            f"{FRED_BASE}/fred/releases/dates?{urlencode(query)}",
            method="GET",
        )
        releases = payload.get("release_dates") if isinstance(payload, dict) else None
        if not isinstance(releases, list):
            raise IntegrationProviderError("provider_unavailable", "FRED returned an invalid release list")
        items: list[dict[str, Any]] = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            release_date = _date_value(release.get("date"))
            if release_date is None or release_date < since:
                continue
            release_id = release.get("id")
            release_id = release.get("release_id", release_id)
            name = release.get("release_name") or release.get("name")
            if not isinstance(release_id, (int, str)) or not isinstance(name, str) or not name.strip():
                continue
            url = release.get("link")
            if not isinstance(url, str) or not url.startswith("https://"):
                url = f"https://fred.stlouisfed.org/releases/{release_id}"
            items.append(
                _item(
                    f"fred:release:{release_id}:{release_date.date().isoformat()}",
                    name,
                    url,
                    release_date,
                    f"FRED release: {name}",
                    "FRED",
                )
            )
        return _result(items, limit)

    def _bls(self, since: datetime, limit: int, api_key: str | None) -> dict[str, Any]:
        current_year = datetime.now(UTC).year
        body: dict[str, Any] = {
            "seriesid": list(_BLS_SERIES),
            "startyear": str(max(current_year - 2, min(since.year, current_year))),
            "endyear": str(current_year),
        }
        if api_key:
            body["registrationkey"] = api_key
        payload = self._request_json(
            f"{BLS_BASE}/publicAPI/v2/timeseries/data/",
            method="POST",
            body=body,
        )
        if not isinstance(payload, dict) or payload.get("status") not in {None, "REQUEST_SUCCEEDED"}:
            raise IntegrationProviderError("provider_unavailable", "BLS returned an invalid series response")
        series_values = payload.get("Results", {}).get("series", []) if isinstance(payload.get("Results"), dict) else []
        if not isinstance(series_values, list):
            raise IntegrationProviderError("provider_unavailable", "BLS returned an invalid series response")
        items_by_series: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        for series in series_values:
            if not isinstance(series, dict):
                continue
            series_id = series.get("seriesID")
            if not isinstance(series_id, str):
                continue
            title = _BLS_SERIES.get(series_id, series_id)
            data = series.get("data")
            if not isinstance(data, list):
                continue
            for observation in data:
                if not isinstance(observation, dict):
                    continue
                observed_at = _bls_period(observation.get("year"), observation.get("period"))
                if observed_at is None:
                    continue
                value = observation.get("value")
                period_name = observation.get("periodName")
                period_label = f"{period_name} {observation.get('year', '')}" if isinstance(period_name, str) else str(observation.get("period", ""))
                summary = f"BLS observation for {period_label}: {value}"
                if isinstance(period_name, str) and period_name:
                    title_with_period = f"{title} ({period_label})"
                else:
                    title_with_period = title
                items_by_series.setdefault(series_id, []).append(
                    (
                        observed_at,
                        _item(
                            f"bls:{series_id}:{observation.get('year')}:{observation.get('period')}",
                            title_with_period,
                            f"https://data.bls.gov/timeseries/{series_id}",
                            None,
                            summary,
                            "BLS",
                        ),
                    )
                )
        items = [
            item
            for values in items_by_series.values()
            for _, item in sorted(values, key=lambda value: value[0], reverse=True)[:2]
        ]
        return _result(items, limit)

    def _bea(self, since: datetime, limit: int, api_key: str | None) -> dict[str, Any]:
        if not api_key:
            raise IntegrationProviderError("connection_unavailable", "BEA API key is not configured")
        current_year = datetime.now(UTC).year
        years = ",".join(str(year) for year in range(current_year - 4, current_year + 1))
        query = {
            "UserID": api_key,
            "method": "GETDATA",
            "datasetname": "NIPA",
            "TableName": "T10101",
            "Frequency": "Q",
            "Year": years,
            "ResultFormat": "JSON",
        }
        payload = self._request_json(f"{BEA_BASE}/api/data/?{urlencode(query)}", method="GET")
        if not isinstance(payload, dict):
            raise IntegrationProviderError("provider_unavailable", "BEA returned an invalid response")
        results = payload.get("BEAAPI", {}).get("Results", {}) if isinstance(payload.get("BEAAPI"), dict) else {}
        if isinstance(results, dict) and results.get("Error"):
            raise IntegrationProviderError("invalid_credential", "BEA rejected the API key")
        rows = results.get("Data", []) if isinstance(results, dict) else []
        if not isinstance(rows, list):
            raise IntegrationProviderError("provider_unavailable", "BEA returned an invalid data response")
        items_by_line: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            observed_at = _bea_period(row.get("TimePeriod"))
            if observed_at is None:
                continue
            line = row.get("LineDescription") or row.get("LineNumber")
            if not isinstance(line, (str, int)):
                continue
            line_text = str(line).lower()
            line_number = str(row.get("LineNumber") or "")
            if line_number != "1" and not any(term in line_text for term in _BEA_RELEVANT_TERMS):
                continue
            period_label = str(row.get("TimePeriod") or "")
            title = f"BEA observation: {line} ({period_label})"
            value = row.get("DataValue")
            unit = row.get("CL_UNIT")
            summary = f"{title}: {value}" + (f" {unit}" if isinstance(unit, str) and unit else "")
            items_by_line.setdefault(line_number or line_text, []).append(
                (
                    observed_at,
                    _item(
                        f"bea:NIPA:{row.get('LineNumber')}:{row.get('TimePeriod')}",
                        title,
                        "https://www.bea.gov/data/gdp/gross-domestic-product",
                        None,
                        summary,
                        "BEA",
                    ),
                )
            )
        items = [
            item
            for values in items_by_line.values()
            for _, item in sorted(values, key=lambda value: value[0], reverse=True)[:4]
        ]
        return _result(items, limit)

    def _eia(self, since: datetime, limit: int, api_key: str | None) -> dict[str, Any]:
        if not api_key:
            raise IntegrationProviderError("connection_unavailable", "EIA API key is not configured")
        items_by_series: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        for series_id, title in _EIA_SERIES.items():
            query = urlencode({"api_key": api_key})
            payload = self._request_json(f"{EIA_BASE}/v2/seriesid/{series_id}/?{query}", method="GET")
            if not isinstance(payload, dict):
                raise IntegrationProviderError("provider_unavailable", "EIA returned an invalid response")
            response = payload.get("response")
            if not isinstance(response, dict):
                raise IntegrationProviderError("provider_unavailable", "EIA returned an invalid series response")
            actual_title = response.get("description") if isinstance(response.get("description"), str) else title
            data = response.get("data")
            if not isinstance(data, list):
                continue
            for observation in data:
                if not isinstance(observation, dict):
                    continue
                period = observation.get("period")
                observed_at = _date_value(period)
                if observed_at is None:
                    continue
                value = observation.get("value")
                unit = response.get("unit") or response.get("units")
                summary = f"EIA observation for {period}: {value}" + (f" {unit}" if isinstance(unit, str) and unit else "")
                title_with_period = f"{actual_title} ({period})"
                items_by_series.setdefault(series_id, []).append(
                    (
                        observed_at,
                        _item(
                            f"eia:{series_id}:{period}",
                            title_with_period,
                            _EIA_SOURCE_URLS[series_id],
                            None,
                            summary,
                            "EIA",
                        ),
                    )
                )
        items = [
            item
            for values in items_by_series.values()
            for _, item in sorted(values, key=lambda value: value[0], reverse=True)[:2]
        ]
        return _result(items, limit)

    @staticmethod
    def _request_json(url: str, *, method: str, body: dict[str, Any] | None = None) -> Any:
        parsed = urlsplit(url)
        allowed_hosts = {"api.stlouisfed.org", "api.bls.gov", "apps.bea.gov", "api.eia.gov"}
        if parsed.scheme != "https" or parsed.netloc not in allowed_hosts:
            raise IntegrationProviderError("internal_failure", "Macro data provider URL is outside the trusted boundary")
        payload = None
        headers = {"Accept": "application/json", "User-Agent": "eidolon-macro-scout"}
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=20)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise IntegrationProviderError("provider_unavailable", "Provider redirects are not accepted") from None
            if exc.code == 401 or exc.code == 403:
                raise IntegrationProviderError("invalid_credential", "Provider rejected the configured API key") from None
            if exc.code == 429:
                raise IntegrationProviderError("rate_limited", "Macro data provider rate limited the request") from None
            raise IntegrationProviderError("provider_unavailable", "Macro data provider could not complete the request") from None
        except TimeoutError:
            raise IntegrationProviderError("provider_timeout", "Macro data provider did not respond before timeout") from None
        except (OSError, URLError):
            raise IntegrationProviderError("provider_unavailable", "Macro data provider is unavailable") from None
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            response.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise IntegrationProviderError("response_too_large", "Macro data provider response exceeded the size limit")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IntegrationProviderError("provider_unavailable", "Macro data provider returned invalid JSON") from None


def _since(value: Any) -> datetime:
    if value is None:
        return datetime.now(UTC) - timedelta(days=14)
    if not isinstance(value, str):
        raise IntegrationProviderError("invalid_input", "since must be an ISO date or date-time")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{candidate}T00:00:00+00:00")
        except ValueError:
            raise IntegrationProviderError("invalid_input", "since must be an ISO date or date-time") from None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _limit(value: Any) -> int:
    if value is None:
        return 10
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RESULTS:
        raise IntegrationProviderError("invalid_input", "limit must be an integer between 1 and 25")
    return value


def _date_value(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{candidate}T00:00:00+00:00")
        except ValueError:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _bls_period(year: Any, period: Any) -> datetime | None:
    if not isinstance(year, str) or not year.isdigit() or not isinstance(period, str):
        return None
    if period.startswith("M") and period[1:].isdigit():
        month = int(period[1:])
        if 1 <= month <= 12:
            return datetime(int(year), month, 1, tzinfo=UTC)
    if period.startswith("Q") and period[1:].isdigit():
        quarter = int(period[1:])
        if 1 <= quarter <= 4:
            return datetime(int(year), (quarter - 1) * 3 + 1, 1, tzinfo=UTC)
    if period.startswith("A"):
        return datetime(int(year), 1, 1, tzinfo=UTC)
    return None


def _bea_period(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{4})Q([1-4])", value)
    if match:
        return datetime(int(match.group(1)), (int(match.group(2)) - 1) * 3 + 1, 1, tzinfo=UTC)
    if value.startswith("Q") and len(value) == 6 and value[1:5].isdigit() and value[5] in "1234":
        return datetime(int(value[1:5]), (int(value[5]) - 1) * 3 + 1, 1, tzinfo=UTC)
    return _date_value(value)


def _item(
    item_id: str,
    title: str,
    url: str,
    published_at: datetime | None,
    summary: str,
    source: str,
) -> dict[str, Any]:
    return {
        "id": str(item_id)[:256],
        "title": " ".join(str(title).split())[:500],
        "url": url[:2048],
        "published_at": published_at.isoformat().replace("+00:00", "Z") if published_at else None,
        "summary": " ".join(str(summary).split())[:2000],
        "source": source,
    }


def _result(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    unique: dict[str, dict[str, Any]] = {item["id"]: item for item in items}
    ordered = sorted(unique.values(), key=lambda item: item["published_at"] or "", reverse=True)
    return {"items": ordered[:limit]}


__all__ = ["MacroDataProviderAdapter", "UrllibMacroDataProviderAdapter"]
