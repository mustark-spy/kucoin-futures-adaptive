#!/usr/bin/env python3
"""
Bot de Trading Telegram Futures ATR avec Persistence, Sandbox, Notifications Enrichies et Commandes
Version utilisant kucoin-universal-sdk & python-telegram-bot
Inclut ATR-based grid, SL/TP, reporting PnL, sandbox mode, persistence via JSON, commandes /pnl /statut /balance
"""
import asyncio
import math
import os
import logging
import uuid
import json
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

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
from kucoin_universal_sdk.generate.service import SpotService, FuturesService, AccountService
from kucoin_universal_sdk.model import (
    ClientOptionBuilder,
    TransportOptionBuilder,
    GLOBAL_API_ENDPOINT,
    GLOBAL_FUTURES_API_ENDPOINT,
    GLOBAL_BROKER_API_ENDPOINT,
)
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    JobQueue,
)

# Configuration
SYMBOL = os.getenv("SYMBOL", "BTCUSDTM")
# Si trailing "M" (perpétuel), on l'enlève pour déterminer la devise
if SYMBOL.endswith("M"):
    _sym = SYMBOL[:-1]
else:
   _sym = SYMBOL
BASE_CURRENCY = _sym[-4:]  # "USDT" plutôt que "SDTM"

GRID_SIZE = int(os.getenv("GRID_SIZE", "10"))
ADJUST_INTERVAL_MIN = int(os.getenv("ADJUST_INTERVAL_MIN", "15"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.01"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.02"))
BUDGET = float(os.getenv("BUDGET", "1000"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
PNL_REPORT_INTERVAL_H = int(os.getenv("PNL_REPORT_INTERVAL_H", "1"))

# Sandbox endpoints
SANDBOX = os.getenv("SANDBOX", "false").lower() in ("1","true","yes")
SPOT_ENDPOINT = "https://openapi-sandbox.kucoin.com" if SANDBOX else GLOBAL_API_ENDPOINT
FUTURES_ENDPOINT = GLOBAL_FUTURES_API_ENDPOINT

# Persistence
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# Spot symbol for ATR calculation
SPOT_SYMBOL = f"{SYMBOL[:-len(BASE_CURRENCY)-1]}-{BASE_CURRENCY}"

class MarketSide(Enum):
    BUY = "buy"
    SELL = "sell"

class GridTradingBotFutures:
    def __init__(self):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s")
        self.logger = logging.getLogger(__name__)
        
        # Telegram setup
        self.telegram_token = os.getenv("TELEGRAM_TOKEN")
        if not self.telegram_token:
            raise RuntimeError("TELEGRAM_TOKEN n'est pas défini")
        self.chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
        self.app = ApplicationBuilder().token(self.telegram_token).build()
        if self.app.job_queue is None:
            jq = JobQueue()
            jq.set_dispatcher(self.app.dispatcher)
            jq.start()
            self.app.job_queue = jq

        # Handlers
        self.app.add_handler(CommandHandler("pnl", self.cmd_pnl))
        self.app.add_handler(CommandHandler("statut", self.cmd_statut))
        self.app.add_handler(CommandHandler("balance", self.cmd_balance))
        self.app.add_handler(CommandHandler("position", self.cmd_position))

        # KuCoin SDK
        key = os.getenv("KUCOIN_API_KEY", "")
        secret = os.getenv("KUCOIN_API_SECRET", "")
        passphrase = os.getenv("KUCOIN_API_PASSPHRASE", "")
        transport = TransportOptionBuilder().set_keep_alive(True).set_max_pool_size(10).build()
        client_opts = (
            ClientOptionBuilder()
            .set_key(key)
            .set_secret(secret)
            .set_passphrase(passphrase)
            .set_spot_endpoint(SPOT_ENDPOINT)
            .set_futures_endpoint(FUTURES_ENDPOINT)
            .set_broker_endpoint(GLOBAL_BROKER_API_ENDPOINT)
            .set_transport_option(transport)
        )
        client = DefaultClient(client_opts.build())
        rest = client.rest_service()
        self.spot_service: SpotService = rest.get_spot_service()
        self.futures_service: FuturesService = rest.get_futures_service()
        self.account_service: AccountService = rest.get_account_service()

        # State
        self.grid_prices: List[float] = []
        self.active_orders: List[Dict] = []
        self.last_adjust: datetime = datetime.utcnow() - timedelta(minutes=ADJUST_INTERVAL_MIN)
        self.last_balance: float = 0.0
        self.pnl_history: List[Dict] = []
        self.last_pnl_report: datetime = datetime.utcnow() - timedelta(hours=PNL_REPORT_INTERVAL_H)
        self.load_state()

    def load_state(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.grid_prices = data.get("grid_prices", [])
                self.active_orders = data.get("active_orders", [])
                self.last_balance = data.get("last_balance", self.last_balance)
                self.pnl_history = data.get("pnl_history", [])
                self.last_pnl_report = datetime.fromisoformat(data.get("last_pnl_report"))
                self.last_adjust = datetime.fromisoformat(data.get("last_adjust"))
                self.logger.info("State loaded from %s", STATE_FILE)
            except Exception as e:
                self.logger.error(f"load_state error: {e}")

    def save_state(self):
        try:
            data = {
                "grid_prices": self.grid_prices,
                "active_orders": self.active_orders,
                "last_balance": self.last_balance,
                "pnl_history": self.pnl_history,
                "last_pnl_report": self.last_pnl_report.isoformat(),
                "last_adjust": self.last_adjust.isoformat(),
            }
            STATE_FILE.write_text(json.dumps(data, indent=2))
            self.logger.info("State saved to %s", STATE_FILE)
        except Exception as e:
            self.logger.error(f"save_state error: {e}")

    async def startup_notify(self, context=None) -> None:
        await self.app.bot.send_message(
            chat_id=self.chat_id,
            text=(f"🚀 <b>Bot Futures ATR démarré</b>\n"
                  f"SYM: {SYMBOL} LEV: {LEVERAGE} GRID: {GRID_SIZE}\n"
                  f"Sandbox: {SANDBOX}"),
            parse_mode='HTML'
        )

    async def send_telegram_message(self, message: str) -> None:
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="HTML"
            )
        except Exception as e:
            self.logger.error(f"Erreur envoi Telegram : {e}")

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.pnl_history:
            await self.send_telegram_message("📉 Aucun trade enregistré pour le moment.")
            return

        report = "\U0001F4C8 Historique PnL (TP/SL) :\n"
        for entry in self.pnl_history[-10:]:
            report += f"{entry['timestamp']} - {entry['type']} {entry['side']} : {entry['pnl_pct']:.2%}\n"

        await self.send_telegram_message(report)

    async def cmd_statut(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            msg = "\U0001F4DD <b>STATUT ACTUEL</b>\n"

            # --- Dernières positions ouvertes ---
            req = GetPositionListData()
            positions = self.futures_service.get_positions_api().get_position_list(req).data
            open_pos = [p for p in positions if p.symbol == SYMBOL and float(p.current_qty) != 0]

            if open_pos:
                msg += "\n<b>📌 Positions ouvertes :</b>\n"
                for p in open_pos:
                    pnl_pct = float(p.unrealised_pnl) / (float(p.avg_entry_price) * abs(float(p.current_qty)))
                    msg += f"{p.position_side.value.upper()} {p.current_qty} @ {p.avg_entry_price:.2f} | PnL: {p.unrealised_pnl:.2f} USDT ({pnl_pct:.2%})\n"
            else:
                msg += "\n<b>📌 Aucune position ouverte.</b>\n"

            # --- Ordres actifs ---
            if self.active_orders:
                msg += "\n<b>📦 Ordres en attente :</b>\n"
                for o in self.active_orders:
                    arrow = "⬇️" if o['side'] == "buy" else "⬆️"
                    msg += f"{arrow} {o['side'].upper()} {o['size']} @ {o['price']:.2f} USDT\n"
            else:
                msg += "\n<b>📦 Aucun ordre actif.</b>\n"

            # --- Dernier PnL ---
            if self.pnl_history:
                last = self.pnl_history[-1]
                msg += f"\n<b>Dernier PnL :</b> {last['type']} {last['side']} - {last['pnl_pct']:.2%} ({last['timestamp']})\n"

            await self.send_telegram_message(msg)
        except Exception as e:
            self.logger.error(f"cmd_statut error: {e}")


    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        bal = float(
            self.account_service.get_account_api()
            .get_futures_account(GetFuturesAccountReqBuilder().set_currency(BASE_CURRENCY).build())
            .available_balance
        )
        await update.message.reply_text(f"💰 Balance futures: {bal:.4f} {BASE_CURRENCY}")

    async def cmd_position(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            symbol_info = self.futures_service.get_market_api().get_symbol(
                GetSymbolReqBuilder().set_symbol(SYMBOL).build()
            )
            last_price = float(symbol_info.mark_price)
            multiplier = float(symbol_info.multiplier)
            usdt_per = float(BUDGET) / 10  # 10% du budget pour chaque sens
            btc_amount = usdt_per * float(LEVERAGE) / last_price
            size_f = btc_amount / multiplier
            size = math.floor(size_f)

            if size < 1:
                await self.send_telegram_message("❗ Budget trop faible pour une position de test.")
                return

            # Long market
            self.futures_service.get_order_api().add_order(
                FuturesAddOrderReqBuilder()
                .set_client_oid(str(uuid.uuid4()))
                .set_symbol(SYMBOL)
                .set_side("buy")
                .set_type("market")
                .set_size(str(size))
                .set_leverage(LEVERAGE)
                .set_remark("forced-position")
                .build()
            )

            # Short market
            self.futures_service.get_order_api().add_order(
                FuturesAddOrderReqBuilder()
                .set_client_oid(str(uuid.uuid4()))
                .set_symbol(SYMBOL)
                .set_side("sell")
                .set_type("market")
                .set_size(str(size))
                .set_leverage(LEVERAGE)
                .set_remark("forced-position")
                .build()
            )

            await self.send_telegram_message(f"🚀 Position forcée : LONG et SHORT {size} contrats chacun.")

        except Exception as e:
            self.logger.error(f"cmd_position error: {e}")
            await self.send_telegram_message(f"❌ Erreur ouverture position : {e}")


    def get_klines(self) -> List[Dict]:
        """
        Récupère les bougies horaires pour le contrat futures (SPOT_SYMBOL n'est plus utilisé).
        """
        self.logger.info(f"Fetching {ATR_PERIOD+1} futures klines for {SYMBOL} (1h)")
        builder = (
            FuturesKlinesReqBuilder()
            .set_symbol(SYMBOL)
            .set_granularity(60)           # 1h = 60 minutes
        )
        try:
            builder = builder.set_from(
                int((datetime.utcnow() - timedelta(hours=ATR_PERIOD+1)).timestamp() * 1000)
            ).set_to(int(datetime.utcnow().timestamp() * 1000))
        except AttributeError:
            # si votre version du SDK n'implémente pas set_from/set_to, vous récupérez
            # par défaut les dernières données (jusqu'à 500 bougies)
            self.logger.debug("FuturesKlinesReqBuilder.set_from/set_to unavailable")
        req = builder.build()
        resp = self.futures_service.get_market_api().get_klines(req)
        return resp.data

    def get_order_status(self, order_id: str) -> str:
        try:
            response = self.futures_service.get_order_api().get_order_by_order_id(
                FuturesGetOrderReqBuilder().set_order_id(order_id).build()
            )
            return response.status  # ex : "FILLED", "OPEN", "CANCELLED"
        except Exception as e:
            self.logger.error(f"Erreur récupération statut ordre {order_id} : {e}")
            return "UNKNOWN"

    def calculate_atr_bounds(self) -> Tuple[float, float]:
        """
        Calcule les bornes [lower, upper] = price ± ATR directement sur le marché futures.
        """
        # 1) on récupère les klines futures
        # le endpoint GET /api/v1/kline/query supporte granularity=60 (1 h)
        builder = (
            FuturesKlinesReqBuilder()
            .set_symbol(SYMBOL)
            .set_granularity(60)      # 1h = 60 minutes
        )
        # optionnel : définir from/to ; si votre SDK le supporte, sinon on prend le défaut
        try:
            ts_end = int(datetime.utcnow().timestamp())
            ts_start = ts_end - (ATR_PERIOD + 1) * 3600
            builder = builder.set_start_at(ts_start).set_end_at(ts_end)
        except AttributeError:
            self.logger.debug("set_start_at/set_end_at non disponible, on prend les dernières bougies par défaut")

        req = builder.build()
        resp = self.futures_service.get_market_api().get_klines(req)
        klines = resp.data  # liste de [time, open, close, high, low, volume, turnover]

        # 2) on extrait high, low, close et on calcule le True Range
        highs  = [float(candle[3]) for candle in klines]
        lows   = [float(candle[4]) for candle in klines]
        closes = [float(candle[2]) for candle in klines]

        trs = []
        for i in range(1, len(klines)):
            h, l, prev_c = highs[i], lows[i], closes[i-1]
            trs.append(max(
                h - l,
                abs(h - prev_c),
                abs(l - prev_c),
            ))
        atr = sum(trs[-ATR_PERIOD:]) / ATR_PERIOD

        # 3) on récupère le dernier prix futures
        symbol_info = self.futures_service.get_market_api().get_symbol(
            GetSymbolReqBuilder().set_symbol(SYMBOL).build()
        )
        price = float(symbol_info.last_trade_price)

        return price - atr, price + atr

    def place_futures_order(self, side: MarketSide, size: float, price: float) -> Optional[str]:
        """
        Place un ordre futures LIMIT et renvoie l'order_id ou None en cas d'erreur.
        """
        try:
            order = self.futures_service.get_order_api().add_order(
                FuturesAddOrderReqBuilder()
                .set_client_oid(str(uuid.uuid4()))
                .set_symbol(SYMBOL)
                .set_side(side)         # "buy" ou "sell"
                .set_type("limit")            # chaîne "limit"
                .set_price(str(price))
                .set_size(size)
                .set_leverage(LEVERAGE)
                .set_remark("atr-grid")
                .build()
            )
            if order.order_id:
                self.logger.info(f"✅ Ordre {side.upper()} placé à {price} pour {size} contrats. ID: {order.order_id}")
                self.send_telegram_message(f"✅ Ordre {side.upper()} placé à {price} pour {size} contrats. ID: {order.order_id}")
                return order.order_id
            else:
                self.logger.error(f"❌ Réponse inattendue: {result}")
                return None
        except Exception as e:
            self.logger.error(f"place_futures_order error: {e}")
            return None

    def cancel_futures_order(self, oid: str) -> None:
        """
        Annule l'ordre futures correspondant à order_id.
        """
        try:
            self.futures_service.get_order_api().cancel_order_by_id(
                FuturesCancelOrderReqBuilder().set_order_id(oid).build()
            )
        except Exception as e:
            self.logger.error(f"cancel_futures_order error: {e}")


    async def adjust_grid(self, context=None) -> None:
            # --- Récupération des infos du symbole ---
            symbol_info = self.futures_service.get_market_api().get_symbol(
                GetSymbolReqBuilder().set_symbol(SYMBOL).build()
            )
            tick = float(symbol_info.tick_size)
            multiplier = float(symbol_info.multiplier)  # valeur en BTC d’un contrat (ex: 0.001)

            try:
                decimals = int(round(-math.log10(tick)))
            except Exception:
                decimals = 6

            # --- Annulation de tous les ordres ouverts sur KuCoin ---
            try:
                req = GetOrderListReqBuilder().set_symbol(SYMBOL).build()
                orders_resp = self.futures_service.get_order_api().get_order_list(req)
                orders = orders_resp.items  # ✅ ou .get_items() selon version
                for order in orders:
                    if order.status == "open":
                        self.cancel_futures_order(order.id)
                        self.logger.info(f"❌ Ordre annulé : {order.side.upper()} à {order.price}")
            except Exception as e:
                self.logger.error(f"Erreur lors de l’annulation des ordres ouverts : {e}")

            # Réinitialisation de l’état local
            self.active_orders.clear()
            self.grid_prices.clear()

            await self.send_telegram_message("📛 Tous les ordres ouverts ont été annulés pour réinitialisation de la grille.")

            # --- Calcul des bornes ATR ---
            lower, upper = self.calculate_atr_bounds()
            center = (lower + upper) / 2

            # --- Grilles BUY sous le prix et SELL au-dessus
            buy_grid = [center - i * (center - lower) / GRID_SIZE for i in range(1, GRID_SIZE + 1)]
            sell_grid = [center + i * (upper - center) / GRID_SIZE for i in range(1, GRID_SIZE + 1)]

            self.grid_prices = buy_grid + sell_grid

            # ❇️ Répartition du budget
            total_orders = GRID_SIZE * 2
            usdt_per = BUDGET / total_orders

            btc_amount = usdt_per * LEVERAGE / center
            size_f = btc_amount / multiplier
            size = math.floor(size_f)

            self.logger.info(f"BTC amount par ordre: {btc_amount:.6f}, Multiplier: {multiplier}, Size float: {size_f}")

            if size < 1:
                self.logger.warning(
                    f"Budget insuffisant pour 1 contrat par ordre (size_f={size_f:.2f} contrats) – skip adjust_grid. "
                    f"Réduisez GRID_SIZE ou augmentez BUDGET/LEVERAGE."
                )
                return

            # --- Placement des ordres ---
            for price in buy_grid:
                buy_price = round(round(price / tick) * tick, decimals)
                order_id = self.place_futures_order("buy", size, buy_price)
                if order_id:
                    self.active_orders.append({"id": order_id, "side": "buy", "price": buy_price, "size": size})

            for price in sell_grid:
                sell_price = round(round(price / tick) * tick, decimals)
                order_id = self.place_futures_order("sell", size, sell_price)
                if order_id:
                    self.active_orders.append({"id": order_id, "side": "sell", "price": sell_price, "size": size})

            # --- Sauvegarde de l'état ---
            self.save_state()

            # --- Message Telegram clair ---
            message = f"\n\U0001F4CA Nouvelle grille ajustée :\n"
            for o in self.active_orders:
                direction = "⬇️ LONG" if o['side'] == "buy" else "⬆️ SHORT"
                message += f"{direction} {o['size']} contrat(s) à {o['price']:.2f} USDT\n"

            await self.send_telegram_message(message)
            self.logger.info(f"📊 Grille ajustée: {GRID_SIZE} BUY + {GRID_SIZE} SELL ordres placés.")


    async def monitor_orders(self, context=None) -> None:
        try:
            filled_orders = []
            for o in list(self.active_orders):
                order_id = o['id']
                status = self.get_order_status(order_id)
                if status == "FILLED":
                    filled_orders.append(o)
                    self.active_orders.remove(o)
                    await self.send_telegram_message(f"✅ Ordre exécuté : {o['side'].upper()} {o['size']} @ {o['price']:.2f}")
            if filled_orders:
                self.save_state()
        except Exception as e:
            self.logger.error(f"monitor_orders error: {e}")

   
    async def check_position_pnl(self, context=None) -> None:
        try:
            req = GetPositionListData()
            positions = self.futures_service.get_positions_api().get_position_list(req).data
            positions = [p for p in positions if p.symbol == SYMBOL and float(p.unrealised_pnl) != 0]

            self.logger.info(f"Checking Position Data -> {[p.__dict__ for p in positions]}")

            for pos in positions:
                pnl = float(pos.unrealised_pnl)
                entry_price = float(pos.avg_entry_price)
                size = float(pos.current_qty)
                direction = str(pos.position_side.value).lower()

                if size == 0:
                    continue

                pnl_pct = pnl / (entry_price * abs(size))

                now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                if direction == "long" or (direction == "both" and size > 0):
                    if pnl_pct >= TAKE_PROFIT:
                        msg = f"💰 TP LONG: +{pnl_pct:.2%}, fermeture {size} contrats."
                        self.logger.info(msg)
                        await self.send_telegram_message(msg)
                        self.pnl_history.append({"type": "TP", "side": "LONG", "pnl_pct": pnl_pct, "timestamp": now})
                        self.close_position(SYMBOL, "sell", abs(size))
                    elif pnl_pct <= -STOP_LOSS:
                        msg = f"❌ SL LONG: {pnl_pct:.2%}, fermeture {size} contrats."
                        self.logger.info(msg)
                        await self.send_telegram_message(msg)
                        self.pnl_history.append({"type": "SL", "side": "LONG", "pnl_pct": pnl_pct, "timestamp": now})
                        self.close_position(SYMBOL, "sell", abs(size))

                elif direction == "short" or (direction == "both" and size < 0):
                    if pnl_pct >= TAKE_PROFIT:
                        msg = f"💰 TP SHORT: +{pnl_pct:.2%}, fermeture {abs(size)} contrats."
                        self.logger.info(msg)
                        await self.send_telegram_message(msg)
                        self.pnl_history.append({"type": "TP", "side": "SHORT", "pnl_pct": pnl_pct, "timestamp": now})
                        self.close_position(SYMBOL, "buy", abs(size))
                    elif pnl_pct <= -STOP_LOSS:
                        msg = f"❌ SL SHORT: {pnl_pct:.2%}, fermeture {abs(size)} contrats."
                        self.logger.info(msg)
                        await self.send_telegram_message(msg)
                        self.pnl_history.append({"type": "SL", "side": "SHORT", "pnl_pct": pnl_pct, "timestamp": now})
                        self.close_position(SYMBOL, "buy", abs(size))

            self.save_state()

        except Exception as e:
            self.logger.error(f"check_position_pnl error: {e}")


    def run(self) -> None:
                jq = self.app.job_queue
                # Notification de démarrage
                jq.run_once(self.startup_notify, when=0)
                self.logger.info(f"Active orders on startup: {self.active_orders}")
                if not self.active_orders:
                    # Construction initiale de la grille dès startup
                    jq.run_once(self.adjust_grid, when=1)
                else:
                    self.logger.info("Ordres rechargés, skip initial grid adjustment")
                # Planification des ajustements périodiques
                jq.run_repeating(self.adjust_grid, interval=ADJUST_INTERVAL_MIN * 60, first=ADJUST_INTERVAL_MIN * 60)
                # Monitoring des ordres
                jq.run_repeating(self.monitor_orders, interval=5, first=10)
                # Rapport PnL
                jq.run_repeating(self.cmd_pnl, interval=PNL_REPORT_INTERVAL_H * 3600, first=PNL_REPORT_INTERVAL_H * 3600)
                jq.run_repeating(self.check_position_pnl, interval=10, first=15)
                # Démarrage du bot
                self.app.run_polling()

if __name__ == '__main__':
    GridTradingBotFutures().run()
