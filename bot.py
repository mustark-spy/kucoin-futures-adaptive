#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
bot_dual_rsi_ema.py

KuCoin Dual Position Bot – RSI + EMA Trend Strategy

Fonctionnalités :
- Ouverture simultanée d’une position LONG (SYMBOL_LONG) et d’une position SHORT (SYMBOL_SHORT).
- Rééquilibrage périodique toutes les X minutes selon la tendance (RSI + EMA).
- Gestion automatique du Take Profit, Stop Loss et Trailing Stop.
- Persistance complète de l’état (budget, PnL, positions ouvertes, historique) dans un fichier JSON.
- Notifications Telegram à chaque action (ouverture, clôture, dashboard horaire).
- Dashboard synthétique envoyé chaque heure avec la tendance, le capital actif, et les PnL.

Configuration via `.env` :

```env
# === PAIRES UTILISÉES ===
SYMBOL_LONG=XBTUSDTM
SYMBOL_SHORT=XBTUSDM

# === BUDGET ET RÉPARTITION INITIALE ===
BUDGET=1000
REPARTITION_LONG=0.5
REPARTITION_SHORT=0.5

# === LEVIER ===
LEVERAGE=6

# === SL / TP / TRAILING ===
TAKE_PROFIT=0.02        # 2% de profit
STOP_LOSS=0.01          # 1% de perte maximale
TRAILING_STOP=0.01      # 1% de trailing stop
TRAILING_ENABLED=true   # true ou false

# === INDICATEURS DE TENDANCE ===
RSI_PERIOD=14
EMA_LONG=50
RSI_THRESHOLD_HIGH=70
RSI_THRESHOLD_LOW=30

# === INTERVALLE D’AJUSTEMENT (en minutes) ===
UPDATE_INTERVAL_MIN=15

# === TELEGRAM ===
TELEGRAM_BOT_TOKEN=VOTRE_TOKEN_TELEGRAM
TELEGRAM_CHAT_ID=VOTRE_CHAT_ID

# === PERSITANCE ===
DATA_DIR=./datas
```

Pour installer les dépendances :

```bash
pip install kucoin-universal-sdk python-telegram-bot pandas numpy python-dotenv
```
"""

import os
import json
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# ------------------------------------------------------------
# 1. Lecture des variables d’environnement
# ------------------------------------------------------------
SYMBOL_LONG        = os.getenv("SYMBOL_LONG", "XBTUSDTM")
SYMBOL_SHORT       = os.getenv("SYMBOL_SHORT", "XBTUSDM")

BUDGET             = float(os.getenv("BUDGET", "1000"))
REPARTITION_LONG   = float(os.getenv("REPARTITION_LONG", "0.5"))
REPARTITION_SHORT  = float(os.getenv("REPARTITION_SHORT", "0.5"))

LEVERAGE           = int(os.getenv("LEVERAGE", "6"))

TAKE_PROFIT        = float(os.getenv("TAKE_PROFIT", "0.02"))
STOP_LOSS          = float(os.getenv("STOP_LOSS", "0.01"))
TRAILING_STOP      = float(os.getenv("TRAILING_STOP", "0.01"))
TRAILING_ENABLED   = os.getenv("TRAILING_ENABLED", "true").lower() == "true"

RSI_PERIOD         = int(os.getenv("RSI_PERIOD", "14"))
EMA_SHORT_PERIOD = int(os.getenv("EMA_SHORT_PERIOD", "20"))
EMA_LONG_PERIOD  = int(os.getenv("EMA_LONG_PERIOD",  "50"))
RSI_THRESHOLD_HIGH = float(os.getenv("RSI_THRESHOLD_HIGH", "70"))
RSI_THRESHOLD_LOW  = float(os.getenv("RSI_THRESHOLD_LOW", "30"))

UPDATE_INTERVAL_MIN = int(os.getenv("UPDATE_INTERVAL_MIN", "15"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

DATA_DIR           = os.getenv("DATA_DIR", "./datas")
STATE_FILE_PATH    = Path(DATA_DIR) / "state_trailing_persist.json"

# On garantit que le répertoire de persistance existe
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# Timezone Europe/Paris
TZ_PARIS = timezone(timedelta(hours=2))  # UTC+2 en été

# ------------------------------------------------------------
# 2. Librairies externes KuCoin + Telegram
# ------------------------------------------------------------
from kucoin_universal_sdk.api import DefaultClient
from kucoin_universal_sdk.generate.futures.market import GetMarkPriceReqBuilder
from kucoin_universal_sdk.api import DefaultClient
from kucoin_universal_sdk.generate.futures.market import GetKlinesReqBuilder as FuturesKlinesReqBuilder
from kucoin_universal_sdk.generate.futures.market import GetSymbolReqBuilder
from kucoin_universal_sdk.generate.futures.order import (
    AddOrderReqBuilder as FuturesAddOrderReqBuilder,
    CancelOrderByIdReqBuilder as FuturesCancelOrderReqBuilder,
    GetOrderByOrderIdReqBuilder as FuturesGetOrderReqBuilder,
)
from kucoin_universal_sdk.generate.futures.order import GetOrderListReqBuilder
from kucoin_universal_sdk.generate.futures.positions import GetPositionListData

from kucoin_universal_sdk.generate.account.account import GetFuturesAccountReqBuilder
from kucoin_universal_sdk.generate.service import FuturesService, AccountService
from kucoin_universal_sdk.model import (
    ClientOptionBuilder,
    TransportOptionBuilder,
    GLOBAL_API_ENDPOINT,
    GLOBAL_FUTURES_API_ENDPOINT,
    GLOBAL_BROKER_API_ENDPOINT,
)
from telegram import Bot
from telegram.error import TelegramError

# POUR LES COMMAND HANDLERS
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ------------------------------------------------------------
# 3. Configuration du logging
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DualRSIEMA-Bot")

# ------------------------------------------------------------
# 4. Classe principale du bot
# ------------------------------------------------------------
class DualRSIEMABot:
    def __init__(self):
        # Initialisation du client KuCoin
        key = os.getenv("KUCOIN_API_KEY", "")
        secret = os.getenv("KUCOIN_API_SECRET", "")
        passphrase = os.getenv("KUCOIN_API_PASSPHRASE", "")
        transport = TransportOptionBuilder().set_keep_alive(True).set_max_pool_size(10).build()
        client_opts = (
            ClientOptionBuilder()
            .set_key(key)
            .set_secret(secret)
            .set_passphrase(passphrase)
            .set_spot_endpoint(GLOBAL_API_ENDPOINT)
            .set_futures_endpoint(GLOBAL_FUTURES_API_ENDPOINT)
            .set_broker_endpoint(GLOBAL_BROKER_API_ENDPOINT)
            .set_transport_option(transport)
        )
        self.client = DefaultClient(client_opts.build())
        rest = self.client.rest_service()
        self.futures_service: FuturesService = rest.get_futures_service()
        self.account_service: AccountService = rest.get_account_service()

        # Initialisation du bot Telegram
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Variables TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID non définies.")
            raise RuntimeError("Configuration Telegram manquante.")
        self.telegram_bot = Bot(token=TELEGRAM_TOKEN)
        self.telegram_chat_id = TELEGRAM_CHAT_ID
        # Création de l’Application (pour gérer les commandes /statut, etc.)
        self.telegram_app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .build()
        )

        # Enregistrement du handler /statut
        # Quand l’utilisateur envoie “/statut”, on appelle la méthode `cmd_statut`
        self.telegram_app.add_handler(CommandHandler("statut", self.cmd_statut))
        self.telegram_app.add_handler(CommandHandler("check", self.cmd_check))

        # État persistant
        self.state = {
            "budget_restant": BUDGET,
            "pertes_cumulees": 0.0,
            "profits_proteges": 0.0,
            "positions_actives": {},  # clés : SYMBOL_LONG / SYMBOL_SHORT
            "historique": []           # liste d’événements PnL
        }
        self._load_state()

        # Pour le trailing stop, on stocke le prix extrême le plus favorable atteint depuis l’ouverture
        self.trailing_high_low = {
            SYMBOL_LONG: None,   # pour les longs, prix max atteint
            SYMBOL_SHORT: None   # pour les shorts, prix min atteint
        }

    # --------------------------------------------------------
    # 4.1. Chargement et sauvegarde de l’état
    # --------------------------------------------------------
    def _load_state(self):
        if STATE_FILE_PATH.exists():
            try:
                with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                logger.info("État chargé depuis %s.", STATE_FILE_PATH)
            except Exception as e:
                logger.error("Impossible de charger l’état : %s", e)

    def _save_state(self):
        try:
            with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=4, ensure_ascii=False)
            logger.debug("État sauvegardé dans %s.", STATE_FILE_PATH)
        except Exception as e:
            logger.error("Impossible de sauvegarder l’état : %s", e)

    # --------------------------------------------------------
    # 4.2. Envoi de messages Telegram
    # --------------------------------------------------------
    async def send_telegram_message(self, texte: str):
        """
        Envoie un message texte au chat configuré.
        """
        try:
            await self.telegram_bot.send_message(chat_id=self.telegram_chat_id, text=texte)
            logger.debug("Message Telegram envoyé : %s", texte)
        except TelegramError as e:
            logger.error("Erreur d’envoi Telegram : %s", e)

    async def cmd_statut(self, update: "telegram.Update", context: ContextTypes.DEFAULT_TYPE):
        """
        Callback pour la commande /statut.
        Appelle la méthode de dashboard et renvoie le résumé au chat.
        """
        # On envoie d’abord un message d’accusé de réception si vous voulez.
        await update.message.reply_text("📋 Voici l’état actuel du bot :")

        # Puis on appelle la méthode interne qui envoie le dashboard.
        # On peut soit renvoyer un message séparé, soit reformater pour l’utilisateur.
        await self.send_hourly_dashboard()

    async def cmd_check(self, update: "telegram.Update", context: ContextTypes.DEFAULT_TYPE):
        """
        Callback pour la commande /statut.
        Appelle la méthode de dashboard et renvoie le résumé au chat.
        """
        # On envoie d’abord un message d’accusé de réception si vous voulez.
        await update.message.reply_text("📋 Vérification tendance manuelle")

        # Puis on appelle la méthode interne qui envoie le dashboard.
        # On peut soit renvoyer un message séparé, soit reformater pour l’utilisateur.
        await self.rebalance()

    # --------------------------------------------------------
    # 4.3. Récupération des bougies (candles)
    # --------------------------------------------------------
    async def fetch_klines(self, symbol: str, interval_min: int = 1, limit: int = 200) -> pd.DataFrame:
        """
        Récupère les bougies OHLCV pour un symbole donné sur KuCoin Futures.

        - symbol:         ex. "XBTUSDTM" ou "XBTUSDM"
        - interval_min:   intervalle en minutes (ex. 1 pour 1min, 15 pour 15min, 60 pour 1h, etc.)
        - limit:          nombre maximal de bougies à récupérer (<= 500)

        Retourne un DataFrame pandas avec les colonnes ['timestamp', 'open', 'high', 'low', 'close', 'volume'].
        """

        # 1) Construire le builder avec symbol + granularité (en secondes)
        granularity = interval_min * 60
        builder = (
            FuturesKlinesReqBuilder()
            .set_symbol(symbol)
            .set_granularity(granularity)
        )

        # 2) Si possible, ajouter la plage from/to pour limiter à (limit) bougies récentes
        try:
            # On récupère les (limit) dernières bougies jusqu’à maintenant
            ts_to   = int(datetime.now(timezone.utc).timestamp() * 1000)
            ts_from = int((datetime.now(timezone.utc) - timedelta(minutes=interval_min * limit)).timestamp() * 1000)
            builder = builder.set_from(ts_from).set_to(ts_to)
        except AttributeError:
            # set_from / set_to non implémentés : on se contente de récupérer
            # les dernières bougies (jusqu’à `limit`)
            logger.debug("FuturesKlinesReqBuilder.set_from/set_to indisponible, on récupère les dernières bougies.")

        # 3) Build + appel API
        req = builder.build()
        try:
            resp = self.futures_service.get_market_api().get_klines(req)
            klines = resp.data  # liste de listes : [timestamp, open, high, low, close, volume, ...]
        except Exception as e:
            logger.error("Erreur fetch_klines(%s) : %s", symbol, e)
            return pd.DataFrame()

        # 4) Conversion en DataFrame pandas
        df = pd.DataFrame(
            klines,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                *["_"] * (len(klines[0]) - 6)
            ]
        )
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]

        # 5) Conversion des types
        df["timestamp"] = (
            pd.to_datetime(df["timestamp"], unit="ms", utc=True)
              .dt.tz_convert(TZ_PARIS)
        )
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)

        return df


    # --------------------------------------------------------
    # 4.4. Calcul des indicateurs RSI et EMA
    # --------------------------------------------------------
    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ajoute trois colonnes au DataFrame :
         - 'EMA_SHORT' : l’EMA sur la période EMA_SHORT_PERIOD (ex. 20)
         - 'EMA_LONG'  : l’EMA sur la période EMA_LONG_PERIOD (ex. 50)
         - 'RSI'       : le RSI sur la période RSI_PERIOD
        """
        # On vérifie qu’il y a suffisamment de barres pour au moins
        # la période la plus longue entre EMA_LONG_PERIOD et RSI_PERIOD
        min_len = max(RSI_PERIOD, EMA_LONG_PERIOD) + 1
        if df.empty or len(df) < min_len:
            return df

        # Calcul de l’EMA « courte » (ex. EMA20)
        df["EMA_SHORT"] = df["close"].ewm(span=EMA_SHORT_PERIOD, adjust=False).mean()

        # Calcul de l’EMA « longue »  (ex. EMA50)
        df["EMA_LONG"]  = df["close"].ewm(span=EMA_LONG_PERIOD,  adjust=False).mean()

        # Calcul du RSI (inchangé)
        delta = df["close"].diff()
        gain  = delta.where(delta > 0, 0.0)
        loss  = -delta.where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean()
        avg_loss = loss.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean()
        rs = avg_gain / (avg_loss.replace(0, np.nan))
        df["RSI"] = 100 - (100 / (1 + rs))
        df["RSI"] = df["RSI"].fillna(50)


        return df


    # --------------------------------------------------------
    # 4.5. Détermination de la tendance (bullish / bearish / neutre)
    # --------------------------------------------------------
    def determine_trend(self, df: pd.DataFrame) -> str:
        """
        Détermine la tendance à partir des deux derniers points :
         - Si EMA_SHORT > EMA_LONG ET RSI < RSI_THRESHOLD_HIGH : bullish
         - Si EMA_SHORT < EMA_LONG ET RSI > RSI_THRESHOLD_LOW  : bearish
         - Sinon : neutral
        """
        if df.empty or len(df) < 2:
            return "neutral"

        dernier = df.iloc[-1]
        ema_short = dernier["EMA_SHORT"]
        ema_long  = dernier["EMA_LONG"]
        rsi       = dernier["RSI"]

        if (ema_short > ema_long) and (rsi < RSI_THRESHOLD_HIGH):
            return "bullish"
        elif (ema_short < ema_long) and (rsi > RSI_THRESHOLD_LOW):
            return "bearish"
        else:
            return "neutral"


    # --------------------------------------------------------
    # 4.6. Récupérer le prix mark (pour calcul PnL ou trailling)
    # --------------------------------------------------------
    async def get_mark_price(self, symbol: str) -> float:
        """
        Récupère le prix mark (value) du contrat sur KuCoin Futures.
        """
        try:
            req = (
                GetMarkPriceReqBuilder()
                .set_symbol(symbol)
                .build()
            )
            resp = self.futures_service.get_market_api().get_mark_price(req)

            # Dans la version actuelle du SDK, le prix mark se trouve dans resp.value
            return float(resp.value)

        except Exception as e:
            logger.error("Erreur get_mark_price(%s) : %s", symbol, e)
            return 0.0


    # --------------------------------------------------------
    # 4.7. Gestion de l’ouverture de position (market order)
    # --------------------------------------------------------
    async def open_position(self, symbol: str, side: str, notional_usdt: float):
        """
        Ouvre une position market sur `symbol` (LONG ou SHORT) en allouant `notional_usdt` USDT.
        Puis lit directement la liste des positions pour récupérer le prix d'entrée (avg_entry_price)
        et les données de PnL / marge.
        """

        try:
            # 1) Récupérer le prix mark pour dimensionner la position
            mark_price = await self.get_mark_price(symbol)
            if mark_price <= 0:
                await self.send_telegram_message(f"❌ Impossible d’obtenir mark_price pour {symbol}.")
                return None

            # 2) Calculer la taille (raw_size) selon le type de contrat
            if symbol.endswith("USDM"):
                # Inverse / coin-margined : 1 contrat = 1 USD de BTC
                raw_size = notional_usdt * LEVERAGE
            elif symbol.endswith("USDTM"):
                # Linéaire USDT-margined : 1 contrat = 0.001 BTC
                raw_size = (notional_usdt * LEVERAGE) / (mark_price * 0.001)
            else:
                # Cas générique linéaire (1 contrat = 1 unité de base)
                raw_size = (notional_usdt / mark_price) * LEVERAGE

            size = math.floor(raw_size)
            if size < 1:
                await self.send_telegram_message(
                    f"❗ Taille trop faible ({raw_size:.4f}) pour ouvrir une position sur {symbol}."
                )
                return None

            # 3) Envoi de l’ordre MARKET
            add_req = (
                FuturesAddOrderReqBuilder()
                .set_client_oid(f"dualrsiema-{symbol}-{datetime.now(TZ_PARIS).strftime('%Y%m%d%H%M%S')}")
                .set_symbol(symbol)
                .set_side("buy" if side == "LONG" else "sell")
                .set_type("market")
                .set_size(str(size))
                .set_leverage(str(LEVERAGE))
                .build()
            )
            resp = self.futures_service.get_order_api().add_order(add_req)
            order_id = getattr(resp, "order_id", None)
            if order_id is None:
                logger.error("Aucun order_id retourné pour %s", symbol)
                return None

            # 4) Attendre très brièvement pour laisser le temps à l’ordre d’apparaître dans la liste de positions
            await asyncio.sleep(0.5)

            # 5) Lire la liste des positions ouvertes
            req_pos = GetPositionListData()
            positions = self.futures_service.get_positions_api().get_position_list(req_pos).data

            # 6) Rechercher la position correspondant à `symbol` et à notre SIDE
            entry_price = None
            pnl          = 0.0
            pos_margin   = 0.0
            found        = False

            for pos in positions:
                # Pos.symbol = "XBTUSDTM" ou "XBTUSDM", etc.
                if pos.symbol != symbol:
                    continue

                # current_qty positif = long, négatif = short
                size_live = float(pos.current_qty)
                if size_live == 0:
                    continue

                # On déduit le sens de la position uniquement d’après le signe de size_live
                direction = "LONG" if size_live > 0 else "SHORT"

                # Si ce sens ne correspond pas à celui qu’on vient d’ouvrir, on passe à la suivante
                if direction != side:
                    continue

                # On a trouvé la position active pour ce symbol + side
                found = True
                entry_price = float(pos.avg_entry_price)
                pnl         = float(pos.unrealised_pnl)
                pos_margin  = float(pos.pos_margin)
                break

            if not found:
                logger.error(
                    "Impossible de retrouver la position %s %s dans la liste des positions après 0.5s",
                    symbol, side
                )
                return None

            # 7) Mise à jour de l’état persistant
            timestamp = datetime.now(TZ_PARIS).isoformat()
            logger.info(
                "Position ouverte %s %s %s contrats @ %f, PnL=%.6f, Margin=%.6f",
                symbol, side, size, entry_price, pnl, pos_margin
            )

            # Sauvegarde des données utiles dans state
            self.state["positions_actives"][symbol] = {
                "side": side,
                "entry_price": entry_price,
                "size": size,
                "order_id": order_id,
                "timestamp": timestamp,
                # Pour trailing stop, on stocke le prix d’entrée comme "best_price"
                "best_price": entry_price,
                "unrealised_pnl": pnl,
                "pos_margin": pos_margin
            }
            self._save_state()

            # 8) Notification Telegram
            await self.send_telegram_message(
                f"🚀 OUVERTURE {side} {symbol} : size={size}, entry_price={entry_price:.2f} "
                f"Pnl={pnl:.6f}, Margin={pos_margin:.6f}"
            )
            return order_id

        except Exception as e:
            logger.error("Erreur open_position(%s, %s) : %s", symbol, side, e)
            await self.send_telegram_message(f"❌ Erreur ouverture {side} pour {symbol} : {e}")
            return None


    # --------------------------------------------------------
    # 4.8. Gestion de clôture de position
    # --------------------------------------------------------
    async def close_position(self, symbol: str):
        """
        Clôture la position active sur `symbol` (market order inverse),
        en extrayant le fill_price depuis l'objet AddOrderResp (et non plus un dict).
        Puis calcule le PnL réalisé, met à jour le budget, l'état, et envoie la notification Telegram.
        """
        # 1) Vérifier qu’il y a bien une position active dans self.state
        info = self.state["positions_actives"].get(symbol)
        if not info:
            # Pas de position à fermer
            return

        side   = info["side"]          # "LONG" ou "SHORT"
        size   = info["size"]          # nombre de contrats
        entry  = info["entry_price"]   # prix d’entrée stocké

        # On détermine le side inverse pour la fermeture du marché
        close_side = "sell" if side == "LONG" else "buy"

        try:
            # 2) Envoi de l’ordre MARKET inverse pour fermer la position
            resp = self.futures_service.get_order_api().add_order(
                FuturesAddOrderReqBuilder()
                .set_client_oid(f"dualrsiema-close-{symbol}-{datetime.now(TZ_PARIS).strftime('%Y%m%d%H%M%S')}")
                .set_symbol(symbol)
                .set_side(close_side)
                .set_type("market")
                .set_size(str(size))
                .set_leverage(str(LEVERAGE))
                .build()
            )

            # 3) Extraction du fill_price depuis l’objet AddOrderResp
            # On teste plusieurs attributs possibles selon la version du SDK :
            fill_price = None

            # a) Si le SDK expose directement resp.fill_price
            if hasattr(resp, "fill_price"):
                fill_price = float(resp.fill_price)
            # b) Sinon, si c’est resp.fillPrice
            elif hasattr(resp, "fillPrice"):
                fill_price = float(resp.fillPrice)
            # c) Sinon, si le SDK stocke dans resp.common_response.data (un dict)
            else:
                common_data = getattr(resp, "common_response", None)
                if common_data and isinstance(common_data, object):
                    data_dict = getattr(common_data, "data", None)
                    if isinstance(data_dict, dict) and "fillPrice" in data_dict:
                        fill_price = float(data_dict["fillPrice"])

            if fill_price is None:
                # On n’a pas réussi à extraire le fill price → log et on abandonne
                logger.error(
                    "Impossible de récupérer 'fillPrice' dans l’AddOrderResp pour %s. Contenu : %r",
                    symbol, resp
                )
                return

            timestamp = datetime.now(TZ_PARIS).isoformat()

            # 4) Calcul du PnL réalisé
            # Sur USDT-marged (XBTUSDTM), entry et fill_price sont en USDT, size correspond à nombre de contrats,
            # et 1 contrat = 0.001 BTC ; mais comme on stocke entry_price déjà en USDT par contrat,
            # on peut calculer le PnL USDT directement :
            #    PnL % = (fill_price - entry) / entry      si LONG
            #    PnL % = (entry - fill_price) / entry      si SHORT
            if side == "LONG":
                pnl_pct = (fill_price - entry) / entry
            else:
                pnl_pct = (entry - fill_price) / entry

            # PnL en USDT : on convertit selon le type de contrat :
            if symbol.endswith("USDTM"):
                # USDT-marged : 1 contrat = 0.001 BTC, mais entry & fill sont déjà en USDT par contrat,
                # donc la formule simplifiée est :
                #    PnL_USDT = pnl_pct * (entry * size / leverage) 
                pnl_usdt = pnl_pct * (entry * size / LEVERAGE)
            else:
                # Coin-marged (XBTUSDM) : le PnL brut est en BTC :
                #    PnL_BTC = pnl_pct * (entry * size / leverage) 
                # puis on convertit en USDT pour reporter dans budget :
                pnl_btc = pnl_pct * (entry * size / LEVERAGE)
                mark_price = await self.get_mark_price(symbol)
                pnl_usdt  = pnl_btc * mark_price

            pnl_usdt = round(pnl_usdt, 2)

            # 5) Mise à jour du budget
            self.state["budget_restant"] += pnl_usdt
            if pnl_usdt >= 0:
                self.state["profits_proteges"] += pnl_usdt
            else:
                self.state["pertes_cumulees"] += abs(pnl_usdt)

            # 6) Ajout à l’historique
            self.state["historique"].append({
                "symbol": symbol,
                "side": side,
                "entry_price": entry,
                "exit_price": fill_price,
                "size": size,
                "pnl_usdt": pnl_usdt,
                "timestamp": timestamp
            })

            # 7) Suppression de la position active du state
            del self.state["positions_actives"][symbol]
            self._save_state()

            # 8) Notification Telegram
            await self.send_telegram_message(
                f"✅ CLÔTURE {side} {symbol} : exit_price={fill_price:.2f}, PnL={pnl_usdt:.2f} USDT.\n"
                f"Budget restant = {self.state['budget_restant']:.2f} USDT."
            )
            logger.info("Position clôturée %s %s PnL=%f USDT", symbol, side, pnl_usdt)

        except Exception as e:
            logger.error("Erreur close_position(%s) : %s", symbol, e)
            await self.send_telegram_message(f"❌ Erreur fermeture position {symbol} : {e}")
            return


    # --------------------------------------------------------
    # 4.9. Vérification Take Profit / Stop Loss / Trailing Stop
    # --------------------------------------------------------
    async def check_risk_management(self, symbol: str):
        """
        Pour la position active sur 'symbol', on récupère le PnL live via l’API KuCoin
        (avgEntryPrice, unrealisedPnl et posMargin), on détermine le side d’après currentQty,
        et on vérifie si TP, SL ou Trailing Stop sont atteints.
        - TP => clôture si PnL % >= TAKE_PROFIT
        - SL => clôture si PnL % <= -STOP_LOSS
        - Trailing => si activé, on met à jour best_price et on clôture si drawdown > TRAILING_STOP
        """
        # 1) On récupère toutes les positions futures
        try:
            req = GetPositionListData()
            positions = self.futures_service.get_positions_api().get_position_list(req).data
        except Exception as e:
            logger.error("Erreur récupération liste positions (%s) : %s", symbol, e)
            return

        # 2) On cherche la ligne de position qui nous intéresse : même symbol et currentQty != 0
        pos_live = None
        for p in positions:
            if p.symbol != symbol:
                continue

            # Nombre de contrats en position : "currentQty" (placeholder pour JSON)
            # Certains retours d'API peuvent utiliser "current_qty" ou "currentQty"
            qty = 0.0
            qty = float(p.current_qty)

            if qty == 0:
                # pas de position ouverte sur ce symbol
                continue

            # on a trouvé la position active (hedging possible => side="both", on ignore)
            pos_live = p
            break

        if pos_live is None:
            # La position n’existe plus chez KuCoin → la supprimer du state pour la ré-ouvrir plus tard
            logger.info("Position %s n’existe plus en live. Suppression du state.", symbol)
            del self.state["positions_actives"][symbol]
            self._save_state()
            return

        # 3) Déterminer le side d’après currentQty
        size_live = 0.0
        size_live = float(pos_live.current_qty)

        # Si size_live > 0 => long, si < 0 => short
        direction = "LONG" if size_live > 0 else "SHORT"

        # 4) Extraire avgEntryPrice, unrealisedPnl et posMargin
        try:
            entry_price_live = float(pos_live.avg_entry_price)
            unrealised_pnl   = float(pos_live.unrealised_pnl)
            pos_margin       = float(pos_live.pos_margin)
        except Exception as e:
            logger.error("Erreur extraction PnL/marge (%s) : %s", symbol, e)
            return

        if pos_margin <= 0:
            return

        pnl_pct = unrealised_pnl / pos_margin

        # 5) Vérifier Take Profit / Stop Loss
        if pnl_pct >= TAKE_PROFIT:
            await self.send_telegram_message(
                f"🎯 TP atteint pour {symbol} ({direction}), PnL % = {pnl_pct:.4f}."
            )
            await self.close_position(symbol)
            return

        if pnl_pct <= -STOP_LOSS:
            await self.send_telegram_message(
                f"🛑 SL atteint pour {symbol} ({direction}), PnL % = {pnl_pct:.4f}."
            )
            await self.close_position(symbol)
            return

        # 6) Vérifier Trailing Stop si activé
        if TRAILING_ENABLED:
            best = self.trailing_high_low.get(symbol)
            # Si pas encore initialisé, on prend entry_price_live comme extrême initial
            if best is None:
                self.trailing_high_low[symbol] = entry_price_live
                return

            # Récupérer le mark_price actuel pour calculer drawdown/drawup
            # On peut soit ré-appeler get_mark_price(), soit lire pos_live["markPrice"] si disponible
            mark_price = None
            if pos_live.mark_price is not None:
                mark_price = float(pos_live.mark_price)
            else:
                try:
                    mark_price = await self.get_mark_price(symbol)
                except:
                    mark_price = None

            if mark_price is None or mark_price <= 0:
                return

            if direction == "LONG":
                # On met à jour si le prix monte (meilleur)
                if mark_price > best:
                    self.trailing_high_low[symbol] = mark_price
                    return
                # Sinon, calculer le drawdown par rapport au maximum
                drawdown = (self.trailing_high_low[symbol] - mark_price) / self.trailing_high_low[symbol]
                if drawdown >= TRAILING_STOP:
                    await self.send_telegram_message(
                        f"⏳ Trailing Stop LONG {symbol} déclenché (drawdown {drawdown:.4f})."
                    )
                    await self.close_position(symbol)
                    return

            else:  # direction == "SHORT"
                # On met à jour si le prix descend (meilleur pour short)
                if mark_price < best:
                    self.trailing_high_low[symbol] = mark_price
                    return
                # Sinon, calculer le drawup
                drawup = (mark_price - self.trailing_high_low[symbol]) / self.trailing_high_low[symbol]
                if drawup >= TRAILING_STOP:
                    await self.send_telegram_message(
                        f"⏳ Trailing Stop SHORT {symbol} déclenché (drawup {drawup:.4f})."
                    )
                    await self.close_position(symbol)
                    return

    # --------------------------------------------------------
    # 4.10. Rééquilibrage périodique des positions
    # --------------------------------------------------------
    async def rebalance(self):
        """
        Cette méthode est appelée toutes les UPDATE_INTERVAL_MIN minutes.
        - Récupère les dernières bougies, calcule RSI+EMA, détermine la tendance.
        - Calcule la répartition notionnelle pour chaque position (long & short).
        - Pour chaque symbole :
            • Si position active → check_risk_management()
            • Sinon, si la part notionnelle > 0 → open_position()
        """

        # 1) Récupérer les indicateurs sur SYMBOL_LONG (on l’utilise comme proxy pour BTC)
        df = await self.fetch_klines(SYMBOL_LONG, interval_min=1, limit=EMA_LONG_PERIOD + RSI_PERIOD + 5)
        df = self.compute_indicators(df)
        tendance = self.determine_trend(df)
        now_str = datetime.now(TZ_PARIS).strftime("%Y-%m-%d %H:%M:%S")
        await self.send_telegram_message(f"⏱️ Rééquilibrage à {now_str} — Tendance estimée : {tendance.upper()}")

        # 2) Déterminer la répartition notionnelle (en USDT) pour chaque symbole
        # Stratégie simple :
        #   - Si bullish : tout vers le long
        #   - Si bearish : tout vers le short
        #   - Sinon (neutral) : on répartit selon REPARTITION_LONG / REPARTITION_SHORT
        budget_disponible = self.state["budget_restant"]
        if tendance == "bullish":
            notional_long  = self.state["budget_restant"] * REPARTITION_LONG
            notional_short = self.state["budget_restant"] * REPARTITION_SHORT
        elif tendance == "bearish":
            notional_long  = self.state["budget_restant"] * REPARTITION_SHORT
            notional_short = self.state["budget_restant"] * REPARTITION_LONG
        else:
            notional_long  = self.state["budget_restant"] * 0.5
            notional_short = self.state["budget_restant"] * 0.5

        await self.send_telegram_message(f"⏱️ Allocation de : {notional_long} USD en LONG")
        await self.send_telegram_message(f"⏱️ Allocation de : {notional_short} USD en SHORT")

        # 3) Traiter chaque symbole séparément
        # On stocke dans un dict pour factoriser le code
        targets = {
            SYMBOL_LONG:  {"notional": notional_long,  "side": "LONG"},
            SYMBOL_SHORT: {"notional": notional_short, "side": "SHORT"},
        }

        for symbol, info_sym in targets.items():
            notional_usdt = info_sym["notional"]
            side_desire   = info_sym["side"]

            # 3.1. Si une position active existe dans self.state, on appelle check_risk_management
            if symbol in self.state["positions_actives"]:
                # Il y a déjà une position ouverte pour ce symbole : on la gère
                await self.check_risk_management(symbol)

            else:
                # 3.2. Aucune position active pour ce symbole : on regarde si on doit en ouvrir une
                if notional_usdt > 0:
                    # On ouvre une nouvelle position avec la partie notionnelle calculée
                    await self.send_telegram_message(
                        f"🔎 Aucune position active sur {symbol}. Ouverture d'une position {side_desire} pour notional={notional_usdt:.2f} USDT."
                    )
                    await self.open_position(symbol, side_desire, notional_usdt)
                else:
                    # Si notional_usdt == 0, on n'ouvre rien (tendance ne le requiert pas)
                    await self.send_telegram_message(
                        f"ℹ️ Pas de position {side_desire} pour {symbol} (notional={notional_usdt:.2f} USDT)."
                    )


    # --------------------------------------------------------
    # 4.11. Dashboard horaire
    # --------------------------------------------------------
    async def send_hourly_dashboard(self):
        """
        Envoi un résumé chaque heure à HH:00.
        - Tendance courante
        - Budget actuel
        - Positions ouvertes et leurs PnL non réalisés (en USDT ou BTC selon le contrat)
        - Historique simplifié (nombre de trades, dernier PnL)
        """
        # 1) Tendance actuelle (inchangé)
        df = await self.fetch_klines(SYMBOL_LONG, interval_min=1, limit=EMA_LONG_PERIOD + RSI_PERIOD + 5)
        df = self.compute_indicators(df)
        tendance = self.determine_trend(df)

        # 2) Budget
        budget = self.state["budget_restant"]

        # 3) Positions ouvertes : on va utiliser get_position_list pour récupérer PnL/marge
        try:
            req_pos = GetPositionListData()
            positions = self.futures_service.get_positions_api().get_position_list(req_pos).data
        except Exception as e:
            logger.error("Erreur get_position_list : %s", e)
            positions = []

        messages = []
        for pos in positions:
            # On ne garde que nos deux symboles
            if pos.symbol not in (SYMBOL_LONG, SYMBOL_SHORT):
                continue

            # Si pas de qty, pas de position active
            size_live = float(getattr(pos, "current_qty", 0.0))
            if size_live == 0:
                continue

            # Déduire side uniquement d'après current_qty
            direction = "LONG" if size_live > 0 else "SHORT"

            # Lire le prix d'entrée moyen, le mark price, PnL non réalisé et marge
            entry_price = float(getattr(pos, "avg_entry_price", 0.0))

            # Le champ `markPrice` est souvent directement accessible sur pos (selon la version du SDK)
            try:
                mark_price = float(getattr(pos, "mark_price", 0.0))
            except Exception:
                # Si `mark_price` n’existe pas, on retombe sur notre get_mark_price
                mark_price = await self.get_mark_price(pos.symbol)

            # Unrealised PnL brut
            pnl_raw = float(getattr(pos, "unrealised_pnl", 0.0))
            pos_margin = float(getattr(pos, "pos_margin", 0.0))

            # Affichage adapté selon le type de contrat :
            if pos.symbol.endswith("USDTM"):
                # XBTUSDTM : PnL déjà en USDT
                pnl_usdt = pnl_raw
                messages.append(
                    f"{pos.symbol} {direction} | entry={entry_price:.2f} | mark={mark_price:.2f} | PnL={pnl_usdt:.2f} USDT"
                )
            else:
                # XBTUSDM (coin-marged) : PnL est en BTC
                pnl_btc = pnl_raw
                # On convertit en USDT pour homogénéiser, si vous le souhaitez
                pnl_in_usdt = pnl_btc * mark_price
                messages.append(
                    f"{pos.symbol} {direction} | entry={entry_price:.2f} | mark={mark_price:.2f} | "
                    f"PnL={pnl_btc:.6f} BTC ({pnl_in_usdt:.2f} USDT)"
                )

        if not messages:
            positions_msg = "Aucune position active."
        else:
            positions_msg = "\n".join(messages)

        # 4) Historique simplifié
        n_trades = len(self.state["historique"])
        dernier_pnl = self.state["historique"][-1]["pnl_usdt"] if n_trades > 0 else 0.0

        texte = (
            f"📊 **DASHBOARD HORAIRE**\n\n"
            f"• Tendance : {tendance.upper()}\n"
            f"• Budget restant : {budget:.2f} USDT\n"
            f"• Positions ouvertes :\n{positions_msg}\n\n"
            f"• Nombre de trades effectués : {n_trades}\n"
            f"• Dernier PnL réalisé : {dernier_pnl:.2f} USDT\n"
        )
        # Envoi Telegram
        try:
            await self.telegram_bot.send_message(chat_id=self.telegram_chat_id, text=texte, parse_mode="Markdown")
        except Exception as e:
            logger.error("Erreur envoi dashboard Telegram : %s", e)


    # --------------------------------------------------------
    # 4.12. Boucle principale asyncio
    # --------------------------------------------------------
    async def run(self):
        logger.info("Démarrage du DualRSIEMA-Bot.")
        await self.send_telegram_message("Démarrage du DualRSIEMA-Bot.")
        # 1) À l’initialisation, on lance éventuellement l’ouverture initiale si pas de position existante
        #    (on attend le premier intervalle pour décider de la tendance).
        
        # --- Lancement du “listener” Telegram en background ---
        #   On démarre le polling pour récupérer les commandes /statut
        #   ( Vous pouvez aussi passer par webhook, mais le plus simple est polling )
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        # Lance polling en tâche de fond
        await self.telegram_app.updater.start_polling()

        # 2) On programme la tâche de rééquilibrage en boucle infinie
        async def boucle_rebalance():
            while True:
                try:
                    await self.rebalance()
                except Exception as e:
                    logger.error("Erreur dans boucle_rebalance : %s", e)
                await asyncio.sleep(UPDATE_INTERVAL_MIN * 60)

        # 3) On programme la tâche du dashboard horaire
        async def boucle_dashboard():
            while True:
                now = datetime.now(TZ_PARIS)
                # On calcule le temps restant jusqu’à la prochaine heure pile
                demain_prochaine_heure = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
                delta = (demain_prochaine_heure -	now).total_seconds()
                await asyncio.sleep(delta)
                try:
                    await self.send_hourly_dashboard()
                except Exception as e:
                    logger.error("Erreur dans boucle_dashboard : %s", e)

        # Lancement parallèle des deux boucles
        await asyncio.gather(boucle_rebalance(), boucle_dashboard())

# ------------------------------------------------------------
# 5. Lancement du bot
# ------------------------------------------------------------
if __name__ == "__main__":
    bot = DualRSIEMABot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Arrêt manuel demandé. Sauvegarde de l’état avant exit.")
        bot._save_state()
        exit(0)
