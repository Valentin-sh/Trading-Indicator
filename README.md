# Trading Journal Pro (Kraken Pro)

Ce dépôt contient un **journal de trading professionnel** qui collecte automatiquement tes trades depuis **Kraken Pro** pour calculer des stats avancées:

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

## 2) Configuration Kraken

1. Crée une clé API Kraken Pro avec accès aux données de trading privées (`Query Closed Orders & Trades`).
2. Copie `.env.example` vers ton fichier d'environnement.
3. Exporte les variables:

```bash
export KRAKEN_API_KEY="..."
export KRAKEN_API_SECRET="..."
```

## 3) Utilisation

### Initialiser la base locale

```bash
python journal_trading.py init-db
```

### Synchroniser les trades depuis Kraken

Incremental (recommandé):

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

## 4) Structure des données

- `raw_trades`: stockage brut des exécutions Kraken (id, pair, side, price, cost, fee, volume, timestamp).
- `closed_lots` (reconstruit à la volée): rapprochement entrée/sortie pour estimer les trades réellement fermés et calculer les métriques.

## 5) Notes importantes

- Le script reconstruit les positions fermées à partir des exécutions (`TradesHistory`) avec logique long/short.
- Les résultats dépendent de la qualité des données retournées par Kraken et de ta façon de trader (spot, margin, scaling in/out, etc.).
- Pour un tracking institutionnel encore plus poussé, tu peux brancher ensuite:
  - tags de setup (breakout, pullback, mean reversion)
  - contexte marché (volatilité, session, news)
  - screenshots d'entrées/sorties
  - métriques par stratégie et par actif

## 6) Fichiers

- `journal_trading.py`: moteur principal (sync + analytics + export)
- `requirements.txt`: dépendances Python
- `.env.example`: variables d'environnement Kraken
