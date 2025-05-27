# Bot de Trading Futures ATR (KuCoin)

**Bot de trading en grille adaptative** utilisant l'**Average True Range (ATR)** sur le marché **futures** de KuCoin, avec :
- Ajustement automatique de la grille toutes les 15 minutes
- Stop-Loss et Take-Profit par niveau
- Reporting PnL et historique
- Mode **sandbox** pour tests sans risques
- Persistence via JSON (`DATA_DIR/state.json`)
- Notifications enrichies sur Telegram
- Commandes `/pnl`, `/statut`, `/balance`, `/startup_notify`

## Prérequis

- Python 3.8+
- Un compte KuCoin avec clé API (spot/futures) et passphrase
- Un bot Telegram (token & chat_id)
- Variables d'environnement configurées (via un fichier `.env`)

## Installation

1. Clonez ou copiez le dossier du bot :
   ```bash
   git clone <votre_repo>
   cd grid-bot
   ```

2. Créez un environnement virtuel et installez les dépendances :
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

## Configuration

Créez un fichier `.env` à la racine du projet :

```ini
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_API_PASSPHRASE=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
SYMBOL=BTCUSDTM            # Contrat futures (ex: XBTUSDTM pour BTC)
BUDGET=1000                # Budget USDT total
LEVERAGE=10                # Levier (ex: 10)
GRID_SIZE=10               # Nombre de niveaux de grille
ATR_PERIOD=14              # Période ATR (en heures)
STOP_LOSS=0.01             # Stop-Loss 1%
TAKE_PROFIT=0.02           # Take-Profit 2%
ADJUST_INTERVAL_MIN=15     # Intervalle de réajustement (minutes)
PNL_REPORT_INTERVAL_H=1    # Intervalle reporting PnL (heures)
SANDBOX=false              # true = mode sandbox (spot uniquement)
DATA_DIR=./data            # Dossier de persistence
```

## Utilisation

Une fois le `.env` en place :
```bash
python bot.py
```

### Commandes Telegram

- `/startup_notify` : confirmation de démarrage
- `/pnl`          : affiche l’historique (5 derniers) des rapports PnL
- `/statut`       : état actuel de la grille (niveaux, spread, ordres actifs)
- `/balance`      : balance USDT disponible en futures

## Fichiers générés

- `state.json` dans `DATA_DIR/` : sauvegarde de l’état (grille, orders, PnL...)

## Avertissement

Ce bot est fourni **à titre éducatif**. Utiliser à vos risques et périls. Consultez un conseiller financier si nécessaire.
