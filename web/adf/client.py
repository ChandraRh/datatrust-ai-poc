"""
Read-only client for the Azure Data Factory management REST API (api-version 2018-06-01).

Authentication (per factory, see store.sources.config):
    auth_mode "service_principal"  tenant_id + client_id + the NAME of the env variable that holds the client secret
    auth_mode "default"            DefaultAzureCredential: managed identity in Azure, AZURE_* env variables,
                                   or your `az login` on a laptop

Permissions needed on the factory (read only): Microsoft.DataFactory/factories/read, .../pipelines/read,
.../datasets/read, .../linkedservices/read, .../triggers/read, .../pipelineruns/read,
.../querypipelineruns/action, .../pipelineruns/queryactivityruns/action, .../querytriggerruns/action.
The built-in "Data Factory Contributor" role has them; docs/ADF_CONNECTOR.md has a smaller custom read-only role.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

ARM = "https://management.azure.com"
API_VERSION = "2018-06-01"
SCOPE = "https://management.azure.com/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"


class AdfError(Exception):
    """A readable error from Azure (status code + Azure's own error code and message)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _secret(env_name: str) -> str:
    from dotenv import dotenv_values
    from pathlib import Path
    root = next(f for f in Path(__file__).resolve().parents if (f / "config.py").exists())
    value = os.getenv(env_name) or dotenv_values(root / ".env").get(env_name)
    if not value:
        raise ValueError(f"{env_name} is not set (add it to .env, or as an environment variable / secret in the cloud)")
    return value


def credential_for(cfg: dict):
    """azure-identity credential for this factory's settings (also used for the storage checks)."""
    if cfg.get("auth_mode") == "service_principal":
        from azure.identity import ClientSecretCredential
        missing = [k for k in ("tenant_id", "client_id", "client_secret_env") if not cfg.get(k)]
        if missing:
            raise ValueError(f"service principal settings missing: {', '.join(missing)}")
        return ClientSecretCredential(cfg["tenant_id"], cfg["client_id"], _secret(cfg["client_secret_env"]))
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


class AdfClient:
    def __init__(self, cfg: dict, credential=None, session: requests.Session | None = None, timeout: int = 30):
        for key in ("subscription_id", "resource_group", "factory_name"):
            if not cfg.get(key):
                raise ValueError(f"{key} is required")
        self.cfg = cfg
        self.credential = credential or credential_for(cfg)
        self.http = session or requests.Session()
        self.timeout = timeout
        self._token = None
        self.base = (f"{ARM}/subscriptions/{cfg['subscription_id']}/resourceGroups/{cfg['resource_group']}"
                     f"/providers/Microsoft.DataFactory/factories/{cfg['factory_name']}")

    # ------------------------------------------------------------------ http
    def _bearer(self) -> str:
        if not self._token or self._token.expires_on - 120 < time.time():
            self._token = self.credential.get_token(SCOPE)
        return self._token.token

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        if not url.startswith("http"):
            url = self.base + url
        sep = "&" if "?" in url else "?"
        if "api-version=" not in url:
            url = f"{url}{sep}api-version={API_VERSION}"
        for attempt in range(5):
            r = self.http.request(method, url, json=body, timeout=self.timeout,
                                  headers={"Authorization": f"Bearer {self._bearer()}", "Content-Type": "application/json"})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:     # throttled / transient: wait and retry
                wait = r.headers.get("Retry-After")
                time.sleep(min(float(wait), 60) if wait and wait.replace(".", "").isdigit() else 2 ** attempt)
                continue
            if r.status_code >= 400:
                try:
                    err = r.json().get("error") or {}
                except ValueError:
                    err = {}
                raise AdfError(r.status_code, err.get("code") or r.reason, err.get("message") or r.text[:300])
            return r.json() if r.content else {}
        raise AdfError(503, "RetriesExhausted", url)

    def _list(self, path: str) -> list:
        """GET a collection, following nextLink."""
        out, url = [], path
        while url:
            page = self._call("GET", url)
            out.extend(page.get("value", []))
            url = page.get("nextLink")
        return out

    def _query(self, path: str, body: dict) -> list:
        """POST a run query, following continuationToken."""
        out, token = [], None
        while True:
            page = self._call("POST", path, {**body, **({"continuationToken": token} if token else {})})
            out.extend(page.get("value", []))
            token = page.get("continuationToken")
            if not token:
                return out

    # ------------------------------------------------------------------ definitions
    def factory(self) -> dict:
        return self._call("GET", "")

    def pipelines(self) -> list:
        return self._list("/pipelines")

    def datasets(self) -> list:
        return self._list("/datasets")

    def linked_services(self) -> list:
        return self._list("/linkedservices")

    def triggers(self) -> list:
        return self._list("/triggers")

    # ------------------------------------------------------------------ runs
    def pipeline_runs(self, updated_after: datetime, updated_before: datetime) -> list:
        return self._query("/queryPipelineRuns", {"lastUpdatedAfter": iso(updated_after), "lastUpdatedBefore": iso(updated_before),
                                                  "orderBy": [{"orderBy": "RunStart", "order": "ASC"}]})

    def activity_runs(self, run_id: str, updated_after: datetime, updated_before: datetime) -> list:
        return self._query(f"/pipelineruns/{run_id}/queryActivityruns",
                           {"lastUpdatedAfter": iso(updated_after), "lastUpdatedBefore": iso(updated_before),
                            "orderBy": [{"orderBy": "ActivityRunStart", "order": "ASC"}]})

    def trigger_runs(self, updated_after: datetime, updated_before: datetime) -> list:
        return self._query("/queryTriggerRuns", {"lastUpdatedAfter": iso(updated_after), "lastUpdatedBefore": iso(updated_before)})
