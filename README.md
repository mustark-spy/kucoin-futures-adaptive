# 📈 KuCoin Dual Position Bot – RSI + EMA Trend Strategy

Ce bot permet d’ouvrir et de maintenir deux positions simultanées (LONG et SHORT) sur deux paires KuCoin Futures (ex : XBTUSDTM et XBTUSDM), avec rééquilibrage automatique en fonction de la tendance détectée via RSI et EMA. Il gère le Take Profit, le Stop Loss, le trailing stop, et sauvegarde les profits tout en assurant une persistance complète.

---

## ⚙️ Fonctionnalités
- 📊 Stratégie à double position (long/short)
- 📊 Récupération des klines en temps réel
- 🔄 Rebalancing toutes les X minutes (paramétrable)
- 🔍 Détection de la tendance via RSI et EMA
- 🧠 Allocation automatique du capital (en USDT)
- 💰 Prise de bénéfices, Stop loss, Trailing Stop
- 🧾 Historique PnL et capital actif
- 💾 Persistance du budget et des profits dans `state_trailing_persist.json`
- 📩 Notifications Telegram
- ⏱️ Dashboard horaire automatique
- 🔄 Redémarrage résilient

---

## 🔧 Configuration via `.env`

```env
# Paires utilisées
SYMBOL_LONG=XBTUSDTM
SYMBOL_SHORT=XBTUSDM

# Budget total à répartir
BUDGET=1000

# Répartition initiale
REPARTITION_LONG=0.5
REPARTITION_SHORT=0.5

# Levier
LEVERAGE=6

# SL/TP
TAKE_PROFIT=0.02
STOP_LOSS=0.01
TRAILING_STOP=0.01
TRAILING_ENABLED=true

# Tendance
RSI_PERIOD=14
EMA_PERIOD=50
RSI_THRESHOLD_HIGH=70
RSI_THRESHOLD_LOW=30

# Intervalle d'ajustement
UPDATE_INTERVAL_MIN=15

# Telegram
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx

# Persistance directory
DATA_DIR=./datas

```

---

## 🚀 Lancer le bot

```bash
pip install -r requirements.txt
python bot.py
```

---

## 🧠 Principe de la stratégie

- Une position LONG et une SHORT sont ouvertes simultanément.
- Le capital est réparti dynamiquement selon la tendance BTC.
- Une position est clôturée si TP, SL ou trailing stop est atteint.
- Le unrealized_pnl affiché par Kucoin est utilisé pour monitorer TP/SL
- Lorsqu’une position se ferme, une nouvelle est ouverte immédiatement.
- Le capital est ajusté selon les pertes (déduites) ou les gains (reconsolidés uniquement jusqu’à récupérer le capital initial).

---

## 💾 Persistance

- Le fichier `state_trailing_persist.json` contient :
  - Le budget actuel restant
  - Les pertes cumulées
  - Les profits protégés
  - Les positions actives
  - L’historique de profit/perte

---

## 📩 Notifications

- Chaque décision est loguée dans Telegram.
- Un dashboard complet est envoyé chaque heure avec :
  - La tendance actuelle
  - Le capital actif
  - Les PnL de chaque position

---

## 📁 Fichiers
- `bot.py` : cœur de la logique
- `state_trailing_persist.json` : fichier persistant
- `.env` : variables de configuration (à créer)

---
