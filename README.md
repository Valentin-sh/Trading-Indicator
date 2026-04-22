# Trading Journal Pro (Kraken Futures)

Ce dépôt contient un **journal de trading professionnel** qui collecte automatiquement tes exécutions depuis **Kraken Futures (dérivés)** pour calculer des stats avancées.

## Métriques calculées

- Taux de réussite (win rate)
- Gain moyen des trades gagnants
- Perte moyenne des trades perdants
- Profit factor
- Expectancy par trade
- PnL total
- Max drawdown

## 1) Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Le script principal fonctionne sans dépendance externe (stdlib Python), mais conserver `pip install -r requirements.txt` est sans danger.

## 2) Configuration Kraken Futures

Crée une clé API **Kraken Futures** (permissions `General API - Read Only` minimum pour lire les fills) et exporte les variables:

```bash
export KRAKEN_FUTURES_API_KEY="..."
export KRAKEN_FUTURES_API_SECRET="..."
```

Compatibilité legacy conservée:

```bash
export KRAKEN_API_KEY="..."
export KRAKEN_API_SECRET="..."
```

## 3) Utilisation

### Initialiser la base locale

```bash
python journal_trading.py init-db
```

### Synchroniser les trades futures depuis Kraken

Incremental (recommandé, via `lastFillTime`):

```bash
python journal_trading.py sync-kraken
```

Historique complet:

```bash
python journal_trading.py sync-kraken --full
```

### Générer un rapport pro

```bash
python journal_trading.py report
```

### Export CSV des trades fermés reconstruits

```bash
python journal_trading.py export-csv --output closed_trades.csv
```

## 4) Détails techniques importants

- API utilisée: `https://futures.kraken.com/derivatives/api/v3/fills`
- Authentification utilisée: headers `APIKey`, `Authent`, `Nonce` (format Kraken Futures v3)
- Formule de signature implémentée:
  1. `sha256(postData + nonce + endpointPath)`
  2. `hmac_sha512(secret_base64_decode, sha256_digest)`
  3. `base64(hmac_digest)`
- Décodage du secret robuste (padding base64 auto) pour éviter l'erreur `Incorrect padding`.

## 5) Structure des données SQLite

Table `raw_trades`:

- Colonnes de base: `trade_id`, `ordertxid`, `pair`, `side`, `ordertype`, `price`, `cost`, `fee`, `volume`, `timestamp`
- Colonnes source futures: `source`, `fill_time_iso`, `raw_payload`

La migration de schéma est automatique si tu avais déjà la version précédente.

## 6) Fichiers

- `journal_trading.py`: moteur principal (sync futures + analytics + export)
- `requirements.txt`: dépendances (aucune obligatoire)
- `.env.example`: variables d'environnement futures
