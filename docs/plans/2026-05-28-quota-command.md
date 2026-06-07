# `/quota` Command — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Ajouter une commande `/quota` qui affiche le solde/quota de *tous* les providers configurés (et pas seulement le provider actif comme `/usage`).

**Architecture:**
- `agent/account_usage.py` : ajouter `_fetch_deepseek_balance()` + une nouvelle fonction publique `fetch_all_providers_quota()` qui itère les providers détectés automatiquement depuis les env vars.
- `hermes_cli/commands.py` : ajouter `CommandDef("quota", ...)`.
- `cli.py` : ajouter `_handle_quota_command()` + dispatch dans `process_command()`.
- `gateway/run.py` : ajouter `_handle_quota_command()` async + dispatch dans le bloc `if canonical == ...`.
- Tests : `tests/test_account_usage.py` (DeepSeek fetcher) + `tests/gateway/test_quota_command.py`.

**Tech Stack:** Python, httpx (déjà utilisé), env vars via `os.getenv`, pattern exact du code existant dans `account_usage.py`.

**Providers couverts dans ce plan :**
| Provider | Mécanisme | API endpoint | Statut |
|---|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `GET https://api.deepseek.com/user/balance` | À ajouter |
| OpenRouter | `OPENROUTER_API_KEY` | `/credits` + `/key` | ✅ Déjà impl. |
| Anthropic | `ANTHROPIC_API_KEY` | OAuth usage API | ✅ Déjà impl. |
| OpenAI Codex | OAuth (`auth.json`) | `/wham/usage` session + weekly | ✅ Déjà impl. |
| OpenCode Go | `OPENCODE_GO_API_KEY` | Pas d'API quota publique connue → "clé configurée" | Key-only |
| Google Antigravity | OAuth (`antigravity_oauth.json`) | `POST /v1internal:retrieveUserQuota` (même endpoint que `/gquota`) | À ajouter (Task 2b) |
| NVIDIA | `NVIDIA_API_KEY` | Pas d'API quota publique → "clé configurée" | Key-only |

---

## Task 1 : Ajouter `_fetch_deepseek_balance()` dans `account_usage.py`

**Objective:** Fetcher DeepSeek solde via `GET /user/balance` en suivant exactement le pattern des fetchers existants.

**Files:**
- Modify: `agent/account_usage.py` (après la fn `_fetch_openrouter_account_usage`, avant `fetch_account_usage`)

**Step 1 : Écrire le test qui échoue**

Dans `tests/test_account_usage.py`, ajouter en fin de fichier :

```python
def test_fetch_account_usage_deepseek_with_balance(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.os.getenv",
        lambda key, default=None: "sk-fake-key" if key == "DEEPSEEK_API_KEY" else default,
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=10.0: _Client({
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "4.23", "granted_balance": "0.00", "topped_up_balance": "4.23"},
            ],
        }),
    )

    snapshot = fetch_account_usage("deepseek")

    assert snapshot is not None
    assert snapshot.provider == "deepseek"
    assert len(snapshot.details) == 1
    assert "$4.23" in snapshot.details[0]


def test_fetch_account_usage_deepseek_no_api_key(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.os.getenv",
        lambda key, default=None: default,
    )
    snapshot = fetch_account_usage("deepseek")
    assert snapshot is None
```

**Step 2 : Run pour vérifier l'échec**

```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/test_account_usage.py::test_fetch_account_usage_deepseek_with_balance -v -o 'addopts='
```
Expected: `FAILED — AttributeError: module 'agent.account_usage' has no attribute '_fetch_deepseek_balance'` (ou NameError sur `fetch_account_usage("deepseek")` → retourne None au lieu d'un snapshot)

**Step 3 : Implémenter `_fetch_deepseek_balance()` dans `agent/account_usage.py`**

Ajouter `import os` en haut si absent, puis insérer la fonction **avant** `fetch_account_usage` :

```python
def _fetch_deepseek_balance() -> Optional[AccountUsageSnapshot]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        response = client.get("https://api.deepseek.com/user/balance", headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    details: list[str] = []
    for info in payload.get("balance_infos") or []:
        currency = str(info.get("currency") or "").upper()
        total = info.get("total_balance", "0.00")
        symbol = "¥" if currency == "CNY" else "$"
        details.append(f"Balance ({currency}): {symbol}{total}")
    if not details and payload.get("is_available") is not None:
        details.append(f"Status: {'Available' if payload.get('is_available') else 'Low balance'}")
    return AccountUsageSnapshot(
        provider="deepseek",
        source="balance_api",
        fetched_at=_utc_now(),
        details=tuple(details),
    )
```

Et ajouter dans `fetch_account_usage()` le cas deepseek **avant** le `return None` final :

```python
        if normalized == "deepseek":
            return _fetch_deepseek_balance()
```

**Step 4 : Vérifier les tests passent**

```bash
python -m pytest tests/test_account_usage.py -v -o 'addopts='
```
Expected: tous PASS (y compris les anciens)

**Step 5 : Commit**

```bash
git add agent/account_usage.py tests/test_account_usage.py
git commit -m "feat: add DeepSeek balance fetcher to account_usage"
```

---

## Task 2 : Ajouter `_fetch_antigravity_quota()` dans `account_usage.py`

**Objective:** Fetcher la quota Google Antigravity via `POST /v1internal:retrieveUserQuota` — même endpoint que `/gquota`, mais avec le token OAuth Antigravity. Retourner les buckets par modèle (% restant).

**Files:**
- Modify: `agent/antigravity_code_assist.py` — ajouter `retrieve_user_quota_antigravity()`
- Modify: `agent/account_usage.py` — ajouter `_fetch_antigravity_quota()` + dispatch

**Step 1 : Écrire les tests**

Dans `tests/test_account_usage.py`, ajouter :

```python
from unittest.mock import MagicMock

def test_fetch_account_usage_antigravity_with_quota(monkeypatch):
    from agent.google_code_assist import QuotaBucket

    monkeypatch.setattr(
        "agent.account_usage.antigravity_oauth.get_valid_access_token",
        lambda: "fake-access-token",
    )
    monkeypatch.setattr(
        "agent.account_usage.antigravity_oauth.load_credentials",
        lambda: MagicMock(project_id="my-project"),
    )
    monkeypatch.setattr(
        "agent.account_usage.antigravity_code_assist.retrieve_user_quota_antigravity",
        lambda token, project_id="": [
            QuotaBucket(model_id="gemini-pro-agent", token_type="token", remaining_fraction=0.75),
            QuotaBucket(model_id="claude-sonnet-4-6", token_type="token", remaining_fraction=0.50),
        ],
    )

    snapshot = fetch_account_usage("google-antigravity")

    assert snapshot is not None
    assert snapshot.provider == "google-antigravity"
    assert any("gemini-pro-agent" in d for d in snapshot.details)
    assert any("75%" in d for d in snapshot.details)


def test_fetch_account_usage_antigravity_not_logged_in(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.antigravity_oauth.get_valid_access_token",
        lambda: (_ for _ in ()).throw(Exception("not logged in")),
    )

    snapshot = fetch_account_usage("google-antigravity")

    assert snapshot is not None
    assert snapshot.unavailable_reason is not None
```

**Step 2 : Run pour vérifier l'échec**

```bash
python -m pytest tests/test_account_usage.py::test_fetch_account_usage_antigravity_with_quota -v -o 'addopts='
```
Expected: `FAILED`

**Step 3 : Ajouter `retrieve_user_quota_antigravity()` dans `antigravity_code_assist.py`**

Le module `google_code_assist` a `retrieve_user_quota()` sur `cloudcode-pa.googleapis.com`. Antigravity utilise `daily-cloudcode-pa.sandbox.googleapis.com` comme endpoint primaire. Ajouter en bas de `antigravity_code_assist.py` :

```python
from agent.google_code_assist import QuotaBucket

def retrieve_user_quota_antigravity(
    access_token: str,
    *,
    project_id: str = "",
    endpoint: str = ANTIGRAVITY_CODE_ASSIST_ENDPOINT,
) -> List[QuotaBucket]:
    """Fetch quota buckets for the Antigravity provider (for /quota command).

    Same v1internal:retrieveUserQuota endpoint as /gquota,
    with Antigravity OAuth token and endpoint.
    """
    body: Dict[str, Any] = {}
    if project_id:
        body["project"] = project_id
    url = f"{endpoint}/v1internal:retrieveUserQuota"
    resp = _post_json(url, body, access_token)
    raw_buckets = resp.get("buckets") or []
    buckets: List[QuotaBucket] = []
    if not isinstance(raw_buckets, list):
        return buckets
    for b in raw_buckets:
        if not isinstance(b, dict):
            continue
        buckets.append(QuotaBucket(
            model_id=str(b.get("modelId") or ""),
            token_type=str(b.get("tokenType") or ""),
            remaining_fraction=float(b.get("remainingFraction") or 0.0),
            reset_time_iso=str(b.get("resetTime") or ""),
            raw=b,
        ))
    return buckets
```

**Step 4 : Implémenter `_fetch_antigravity_quota()` dans `account_usage.py`**

Ajouter les imports lazy en tête du module (pattern existant — voir `# NOTE: lazy import` dans le fichier) :

```python
def _fetch_antigravity_quota() -> Optional[AccountUsageSnapshot]:
    try:
        from agent import antigravity_oauth, antigravity_code_assist
    except ImportError as exc:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Antigravity modules unavailable: {exc}",
        )
    try:
        access_token = antigravity_oauth.get_valid_access_token()
    except Exception as exc:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Not logged in — run `hermes auth` and pick Google Antigravity ({exc})",
        )
    creds = antigravity_oauth.load_credentials()
    project_id = (creds.project_id if creds else "") or ""
    buckets = antigravity_code_assist.retrieve_user_quota_antigravity(
        access_token, project_id=project_id
    )
    if not buckets:
        return AccountUsageSnapshot(
            provider="google-antigravity", source="oauth_quota_api",
            fetched_at=_utc_now(),
            unavailable_reason="No quota buckets reported (free-tier or unmetered).",
        )
    details: list[str] = []
    for b in sorted(buckets, key=lambda x: (x.model_id, x.token_type)):
        pct = int(round(b.remaining_fraction * 100))
        label = b.model_id + (f" [{b.token_type}]" if b.token_type else "")
        details.append(f"{label}: {pct}% remaining")
    return AccountUsageSnapshot(
        provider="google-antigravity", source="oauth_quota_api",
        fetched_at=_utc_now(),
        details=tuple(details),
    )
```

Et dans `fetch_account_usage()`, ajouter :

```python
        if normalized == "google-antigravity":
            return _fetch_antigravity_quota()
```

**Step 5 : Run les tests**

```bash
python -m pytest tests/test_account_usage.py -v -o 'addopts='
```
Expected: tous PASS

**Step 6 : Commit**

```bash
git add agent/account_usage.py agent/antigravity_code_assist.py tests/test_account_usage.py
git commit -m "feat: add Google Antigravity OAuth quota fetcher"
```

---

## Task 3 : Ajouter `fetch_all_providers_quota()` dans `account_usage.py`

**Objective:** Fonction qui détecte automatiquement les providers configurés (via env vars) et retourne une liste de snapshots pour tous.

**Files:**
- Modify: `agent/account_usage.py`

**Step 1 : Écrire le test**

Dans `tests/test_account_usage.py`, ajouter :

```python
from agent.account_usage import fetch_all_providers_quota

def test_fetch_all_providers_quota_detects_deepseek_and_openrouter(monkeypatch):
    # Mock DeepSeek
    monkeypatch.setattr(
        "agent.account_usage._fetch_deepseek_balance",
        lambda: AccountUsageSnapshot(
            provider="deepseek", source="balance_api",
            fetched_at=datetime.now(timezone.utc),
            details=("Balance (USD): $4.23",),
        ),
    )
    # Mock OpenRouter
    monkeypatch.setattr(
        "agent.account_usage._fetch_openrouter_account_usage",
        lambda base_url, api_key: AccountUsageSnapshot(
            provider="openrouter", source="credits_api",
            fetched_at=datetime.now(timezone.utc),
            details=("Credits balance: $15.00",),
        ),
    )
    # Env: DeepSeek + OpenRouter configurés, rien d'autre
    env = {"DEEPSEEK_API_KEY": "sk-x", "OPENROUTER_API_KEY": "sk-or-y"}
    monkeypatch.setattr("agent.account_usage.os.getenv", lambda k, d=None: env.get(k, d))

    results = fetch_all_providers_quota()

    providers = {s.provider for s in results}
    assert "deepseek" in providers
    assert "openrouter" in providers


def test_fetch_all_providers_quota_includes_key_only_providers(monkeypatch):
    """Providers with no quota API → listed as 'key configured' snapshots."""
    env = {"GOOGLE_API_KEY": "AIzaSy-fake", "NVIDIA_API_KEY": "nvapi-fake"}
    monkeypatch.setattr("agent.account_usage.os.getenv", lambda k, d=None: env.get(k, d))
    monkeypatch.setattr("agent.account_usage._fetch_deepseek_balance", lambda: None)
    monkeypatch.setattr(
        "agent.account_usage._fetch_openrouter_account_usage",
        lambda b, k: None,
    )

    results = fetch_all_providers_quota()

    providers = {s.provider for s in results}
    assert "google" in providers
    assert "nvidia" in providers
    for s in results:
        if s.provider in {"google", "nvidia"}:
            assert s.unavailable_reason is not None
```

**Step 2 : Run pour vérifier l'échec**

```bash
python -m pytest tests/test_account_usage.py::test_fetch_all_providers_quota_detects_deepseek_and_openrouter -v -o 'addopts='
```
Expected: `FAILED — ImportError: cannot import name 'fetch_all_providers_quota'`

**Step 3 : Implémenter `fetch_all_providers_quota()` dans `account_usage.py`**

```python
# Providers with a real quota/balance API
# Note: openai-codex and google-antigravity use OAuth — detected via auth.json, not env var
_QUOTA_FETCHERS: list[tuple[str, str, callable]] = [
    ("DEEPSEEK_API_KEY",    "deepseek",         lambda: _fetch_deepseek_balance()),
    ("OPENROUTER_API_KEY",  "openrouter",        lambda: _fetch_openrouter_account_usage(None, None)),
    ("ANTHROPIC_API_KEY",   "anthropic",         lambda: _fetch_anthropic_account_usage()),
]

# OAuth-based providers — detected via auth.json presence, not env var
_OAUTH_QUOTA_FETCHERS: list[tuple[str, str, callable]] = [
    ("openai-codex",       lambda: _fetch_codex_account_usage()),
    ("google-antigravity", lambda: _fetch_antigravity_quota()),
]

# Providers with a key but no public quota API
_KEY_ONLY_PROVIDERS: list[tuple[str, str, str]] = [
    ("GOOGLE_API_KEY",        "google",        "Gemini/Google AI Studio — quota N/A via API"),
    ("NVIDIA_API_KEY",        "nvidia",        "NVIDIA NIM — quota N/A via API"),
    ("OPENCODE_GO_API_KEY",   "opencode-go",   "OpenCode Go — quota N/A via API"),
]

```python
def fetch_all_providers_quota() -> list[AccountUsageSnapshot]:
    """Fetch quota/balance for every configured provider.

    - API-key providers: detected via env vars
    - OAuth providers (openai-codex, google-antigravity): always attempted,
      return unavailable_reason if not logged in (non-fatal)
    - Key-only providers: listed with unavailable_reason if key is set
    """
    results: list[AccountUsageSnapshot] = []

    # API-key providers
    for env_key, provider_id, fetcher in _QUOTA_FETCHERS:
        if not os.getenv(env_key, "").strip():
            continue
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

    # OAuth providers — always try, gracefully fail
    for provider_id, fetcher in _OAUTH_QUOTA_FETCHERS:
        try:
            snapshot = fetcher()
        except Exception:
            snapshot = None
        if snapshot is not None:
            results.append(snapshot)

    # Key-only providers
    for env_key, provider_id, reason in _KEY_ONLY_PROVIDERS:
        if not os.getenv(env_key, "").strip():
            continue
        results.append(AccountUsageSnapshot(
            provider=provider_id,
            source="env_detected",
            fetched_at=_utc_now(),
            unavailable_reason=reason,
        ))

    return results
```

**Step 4 : Run les tests**

```bash
python -m pytest tests/test_account_usage.py -v -o 'addopts='
```
Expected: tous PASS

**Step 5 : Commit**

```bash
git add agent/account_usage.py tests/test_account_usage.py
git commit -m "feat: add fetch_all_providers_quota() scanning all configured providers"
```

---

## Task 3 : Ajouter `CommandDef("quota", ...)` dans le registre

**Objective:** Enregistrer `/quota` comme commande officielle pour qu'elle apparaisse dans `/help`, l'autocomplete CLI et le menu Telegram.

**Files:**
- Modify: `hermes_cli/commands.py` ligne ~128 (bloc "Info")

**Step 1 : Pas de test (registre de données pur)**

**Step 2 : Ajouter dans `COMMAND_REGISTRY` juste après `CommandDef("gquota", ...)`**

```python
CommandDef("quota", "Show quota/balance for all configured providers", "Info"),
```

**Step 3 : Vérifier que le registre compile**

```bash
python -c "from hermes_cli.commands import COMMAND_REGISTRY; print([c.name for c in COMMAND_REGISTRY if c.name in {'quota','gquota','usage'}])"
```
Expected: `['gquota', 'quota', 'usage']`

**Step 4 : Commit**

```bash
git add hermes_cli/commands.py
git commit -m "feat: register /quota slash command in COMMAND_REGISTRY"
```

---

## Task 4 : Handler `/quota` dans `cli.py` (CLI interactif)

**Objective:** Implémenter `_handle_quota_command()` dans `cli.py` pour le CLI interactif (Rich output avec barres et couleurs), dispatché par `process_command()`.

**Files:**
- Modify: `cli.py` — ajouter méthode `_handle_quota_command` après `_handle_gquota_command` (~ligne 7410), et ajouter dispatch dans `process_command()`.

**Step 1 : Pas de test unitaire (output Rich trop couplé à l'UI)**

**Step 2 : Implémenter `_handle_quota_command` dans `cli.py`**

Ajouter après `_handle_gquota_command` (ligne ~7410) :

```python
def _handle_quota_command(self) -> None:
    """Show quota/balance for all configured providers."""
    from agent.account_usage import fetch_all_providers_quota, render_account_usage_lines

    snapshots = fetch_all_providers_quota()

    if not snapshots:
        self._console_print("  [dim]No providers configured (no API keys found in env).[/]")
        return

    self._console_print()
    self._console_print("  [bold]Provider quota overview[/]")
    self._console_print()

    for snapshot in snapshots:
        lines = render_account_usage_lines(snapshot)
        for line in lines:
            self._console_print(f"  {line}")
        self._console_print()
```

**Step 3 : Ajouter le dispatch dans `process_command()`**

Chercher le bloc `elif canonical == "gquota":` dans `process_command()` (ligne ~8001) et ajouter juste après :

```python
        elif canonical == "quota":
            self._handle_quota_command()
            return True
```

**Step 4 : Test manuel CLI**

```bash
hermes chat -q "/quota"
```
Expected : liste des providers configurés avec leur solde/status.

**Step 5 : Commit**

```bash
git add cli.py
git commit -m "feat: add /quota handler to CLI"
```

---

## Task 5 : Handler `/quota` dans `gateway/run.py` (Telegram/messaging)

**Objective:** Implémenter `_handle_quota_command()` async dans `gateway/run.py` pour Telegram, dispatché dans le bloc `if canonical ==`.

**Files:**
- Modify: `gateway/run.py` — ajouter méthode async `_handle_quota_command` après `_handle_usage_command` (~ligne 12855), et dispatch dans le bloc `if canonical == "usage":` (~ligne 7249).

**Step 1 : Écrire le test**

Créer `tests/gateway/test_quota_command.py` en copiant le pattern de `tests/gateway/test_usage_command.py` :

```python
"""Tests for /quota gateway command."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from agent.account_usage import AccountUsageSnapshot


@pytest.mark.asyncio
async def test_quota_command_returns_all_providers(make_gateway):
    """Gateway /quota returns a formatted list of all providers."""
    gw = make_gateway()

    snapshots = [
        AccountUsageSnapshot(
            provider="deepseek", source="balance_api",
            fetched_at=datetime.now(timezone.utc),
            details=("Balance (USD): $4.23",),
        ),
        AccountUsageSnapshot(
            provider="openrouter", source="credits_api",
            fetched_at=datetime.now(timezone.utc),
            details=("Credits balance: $15.00",),
        ),
    ]

    with patch("gateway.run.fetch_all_providers_quota", return_value=snapshots):
        result = await gw._handle_quota_command(make_event("/quota"))

    assert "deepseek" in result.lower()
    assert "openrouter" in result.lower()
    assert "$4.23" in result
    assert "$15.00" in result


@pytest.mark.asyncio
async def test_quota_command_no_providers(make_gateway):
    gw = make_gateway()

    with patch("gateway.run.fetch_all_providers_quota", return_value=[]):
        result = await gw._handle_quota_command(make_event("/quota"))

    assert "no providers" in result.lower() or "aucun" in result.lower()
```

> **Note :** Adapter `make_gateway` et `make_event` au pattern du fichier `test_usage_command.py` existant — copier les fixtures depuis ce fichier.

**Step 2 : Run pour vérifier l'échec**

```bash
python -m pytest tests/gateway/test_quota_command.py -v -o 'addopts='
```
Expected: `FAILED — AttributeError: 'GatewayRunner' has no attribute '_handle_quota_command'`

**Step 3 : Ajouter l'import dans `gateway/run.py`**

En haut du fichier, dans le bloc d'import de `agent.account_usage` (ligne ~53) :

```python
from agent.account_usage import fetch_account_usage, fetch_all_providers_quota, render_account_usage_lines
```

**Step 4 : Implémenter `_handle_quota_command` dans `gateway/run.py`**

Ajouter après `_handle_usage_command` (après la ligne ~12854) :

```python
async def _handle_quota_command(self, event: MessageEvent) -> str:
    """Handle /quota command -- show quota/balance for all configured providers."""
    snapshots = await asyncio.to_thread(fetch_all_providers_quota)

    if not snapshots:
        return "No providers configured (no API keys found in env)."

    parts: list[str] = ["📊 **Provider quota overview**", ""]
    for snapshot in snapshots:
        lines = render_account_usage_lines(snapshot, markdown=True)
        parts.extend(lines)
        parts.append("")

    return "\n".join(parts).strip()
```

**Step 5 : Ajouter le dispatch dans le bloc `if canonical ==`**

Juste après `if canonical == "usage":` (ligne ~7249) :

```python
        if canonical == "quota":
            return await self._handle_quota_command(event)
```

**Step 6 : Run les tests**

```bash
python -m pytest tests/gateway/test_quota_command.py -v -o 'addopts='
```
Expected: tous PASS

**Step 7 : Commit**

```bash
git add gateway/run.py tests/gateway/test_quota_command.py
git commit -m "feat: add /quota handler to gateway (Telegram/messaging)"
```

---

## Task 6 : Vérification end-to-end

**Objective:** Vérifier que `/quota` fonctionne dans Telegram et que les tests passent tous.

**Step 1 : Lancer la suite de tests complète**

```bash
cd ~/.hermes/hermes-agent
python -m pytest tests/test_account_usage.py tests/gateway/test_quota_command.py -v -o 'addopts='
```
Expected: tous PASS

**Step 2 : Restart le gateway**

Depuis Telegram : `/restart`

**Step 3 : Tester en vrai dans Telegram**

Envoyer `/quota` → doit afficher les balances DeepSeek + OpenRouter + les mentions pour Google/NVIDIA/OpenCode.

**Step 4 : Commit final (bump changelog si applicable)**

```bash
git add .
git commit -m "feat: /quota command — show all provider quotas at a glance"
```

---

## Résumé des fichiers touchés

| Fichier | Action |
|---|---|
| `agent/account_usage.py` | + `_fetch_deepseek_balance()` + `_fetch_antigravity_quota()` + `fetch_all_providers_quota()` + `_QUOTA_FETCHERS` + `_OAUTH_QUOTA_FETCHERS` + `_KEY_ONLY_PROVIDERS` + dispatch deepseek + antigravity dans `fetch_account_usage()` |
| `agent/antigravity_code_assist.py` | + `retrieve_user_quota_antigravity()` |
| `hermes_cli/commands.py` | + `CommandDef("quota", ...)` |
| `cli.py` | + `_handle_quota_command()` + dispatch dans `process_command()` |
| `gateway/run.py` | + `fetch_all_providers_quota` import + `_handle_quota_command()` async + dispatch |
| `tests/test_account_usage.py` | + tests DeepSeek fetcher + tests Antigravity fetcher + tests `fetch_all_providers_quota` |
| `tests/gateway/test_quota_command.py` | Nouveau fichier, tests gateway /quota |

**Providers supportés au final :**
- ✅ DeepSeek → solde $ (API key)
- ✅ OpenRouter → crédits + usage (API key)
- ✅ Anthropic → OAuth usage (OAuth, key non supportée)
- ✅ OpenAI Codex → session/weekly % (OAuth)
- ✅ Google Antigravity → buckets quota % par modèle (OAuth)
- ℹ️ OpenCode Go → "clé configurée, quota N/A" (API key, pas d'endpoint quota public)
- ℹ️ NVIDIA → "clé configurée, quota N/A" (API key, pas d'endpoint quota public)
- ℹ️ Google AI Studio → "clé configurée, quota N/A" si `GOOGLE_API_KEY` présente

**Estimation :** ~2-3h pour un dev qui connaît la codebase, ~4-5h pour un agent depuis zéro.
