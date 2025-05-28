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

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.pnl_history:
            return await update.message.reply_text("Aucun historique PnL disponible.")
        lines = ["📈 Historique PnL (derniers 5):"]
        for e in self.pnl_history[-5:]:
            lines.append(f"{e['time']}: {e['pnl']:+.4f} {BASE_CURRENCY} ({e['pct']:+.2f}%)")
        await update.message.reply_html("\n".join(lines))

    async def cmd_statut(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.grid_prices:
            return await update.message.reply_text("Grille non initialisée.")
        lower, upper = min(self.grid_prices), max(self.grid_prices)
        spread = upper - lower
        step = spread / GRID_SIZE
        lines = [
            "🔍 Statut Actuel:",
            f"Niveaux: {len(self.grid_prices)}",
            f"Range: {lower:.4f} - {upper:.4f}",
            f"Spread: {spread:.4f}",
            f"Increment: {step:.4f}",
            f"Ordres actifs: {len(self.active_orders)}"
        ]
        await update.message.reply_html("\n".join(lines))

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        bal = float(
            self.account_service.get_account_api()
            .get_futures_account(GetFuturesAccountReqBuilder().set_currency(BASE_CURRENCY).build())
            .available_balance
        )
        await update.message.reply_text(f"💰 Balance futures: {bal:.4f} {BASE_CURRENCY}")

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

    def place_futures_order(self, side: MarketSide, price: float, size: float) -> Optional[str]:
        """
        Place un ordre futures LIMIT et renvoie l'order_id ou None en cas d'erreur.
        """
        try:
            order = self.futures_service.get_order_api().add_order(
                FuturesAddOrderReqBuilder()
                .set_client_oid(str(uuid.uuid4()))
                .set_symbol(SYMBOL)
                .set_side(side.value)         # "buy" ou "sell"
                .set_type("limit")            # chaîne "limit"
                .set_price(str(price))
                .set_size(size)
                .set_leverage(LEVERAGE)
                .set_remark("atr-grid")
                .build()
            )
            return order.order_id
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

    def check_order_filled(self, oid: str) -> bool:
        try:
            detail = self.futures_service.get_order_api().get_order_by_order_id(
                FuturesGetOrderReqBuilder().set_order_id(oid).build()
            )
            # récupérer la valeur string de l'enum
            status = detail.status.value if hasattr(detail.status, "value") else str(detail.status)
            return status.lower() == "done"
        except Exception as e:
            self.logger.error(f"check_order_filled error: {e}")
            return False

    async def adjust_grid(self, context=None) -> None:
        # --- Fetch tick size & precision pour arrondir les prix ---
        symbol_info = self.futures_service.get_market_api().get_symbol(
            GetSymbolReqBuilder().set_symbol(SYMBOL).build()
        )
        tick = float(symbol_info.tick_size)
        multiplier= float(symbol_info.multiplier)       # valeur en BTC d’un contrat (0.001)
        try:
            decimals = int(round(-math.log10(tick)))
        except Exception:
            decimals = 6

        # --- Annulation des ordres existants ---
        for o in list(self.active_orders):
            self.cancel_futures_order(o['id'])
        self.active_orders.clear()

        # --- Calcul ATR bounds & reconstruction grille ---
        lower, upper = self.calculate_atr_bounds()
        self.grid_prices = [lower + i * (upper - lower) / GRID_SIZE for i in range(1, GRID_SIZE + 1)]

        # ❇️ BUDGET total à répartir sur tous les ordres (BUY + SELL)
        total_orders = GRID_SIZE * 2
        usdt_per     = BUDGET / total_orders       # budget USDT par ordre
        center       = (lower + upper) / 2         # prix moyen spot/futures

        # 1) calculer la quantité BTC que la marge permet
        btc_amount = usdt_per * LEVERAGE / center
        # 2) convertir en nombre de contrats
        size_f     = btc_amount / multiplier
        size       = math.floor(size_f)

        if size < 1:
            self.logger.warning(
                f"Budget insuffisant pour 1 contrat par ordre "
                f"(size_f={size_f:.2f} contrats) – skip adjust_grid. "
                f"Réduisez GRID_SIZE ou augmentez BUDGET/LEVERAGE."
            )
            return
        # --- Placement des ordres avec arrondi au tick ---
        for p in self.grid_prices:
            buy_price  = round(p / tick) * tick
            buy_price  = float(f"{buy_price:.{decimals}f}")
            sell_price = round((p * (1 + TAKE_PROFIT)) / tick) * tick
            sell_price = float(f"{sell_price:.{decimals}f}")

            bid = self.place_futures_order(MarketSide.BUY,  buy_price,  size)
            sid = self.place_futures_order(MarketSide.SELL, sell_price, size)

            if bid:
                self.active_orders.append({
                    'id': bid,
                    'side': MarketSide.BUY.value,    # stocker la chaîne "buy"
                    'price': buy_price
                })
            if sid:
                self.active_orders.append({
                    'id': sid,
                    'side': MarketSide.SELL.value,   # stocker la chaîne "sell"
                    'price': sell_price
                })

        self.last_adjust = datetime.utcnow()
        self.save_state()

        # --- Notification Telegram ---
        msg = (
            f"🔄 <b>Grid ajustée</b>\n"
            f"Range: <code>{lower:.4f}</code> - <code>{upper:.4f}</code>\n"
            f"Levels: <i>{GRID_SIZE}</i>\n"
            f"Time: <i>{self.last_adjust.strftime('%Y-%m-%d %H:%M:%S UTC')}</i>"
        )
        await self.app.bot.send_message(chat_id=self.chat_id, text=msg, parse_mode='HTML')


    async def monitor_orders(self, context=None) -> None:
        try:
            resp = self.futures_service.get_market_api().get_symbol(
                GetSymbolReqBuilder().set_symbol(SYMBOL).build()
            )
            price = float(resp.last_trade_price)
        except Exception as e:
            self.logger.error(f"monitor_orders error: {e}")
            return
        changed = False
        for o in list(self.active_orders):
            oid, side, p = o['id'], o['side'], o['price']
            if self.check_order_filled(oid):
                changed = True
                self.active_orders.remove(o)
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=f"✔ <b>Order Filled</b> ID: <code>{oid}</code> Side: <b>{side.upper()}</b> @ <code>{p:.4f}</code>",
                    parse_mode='HTML'
                )
            if side == MarketSide.BUY.value and price <= p * (1 - STOP_LOSS):
                changed = True
                self.cancel_futures_order(oid)
                self.active_orders.remove(o)
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=f"🛑 <b>Stop-Loss</b> BUY @ <code>{p:.4f}</code>",
                    parse_mode='HTML'
                )
            if side == MarketSide.SELL.value and price >= p * (1 + TAKE_PROFIT):
                changed = True
                self.cancel_futures_order(oid)
                self.active_orders.remove(o)
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=f"🎯 <b>Take-Profit</b> SELL @ <code>{p:.4f}</code>",
                    parse_mode='HTML'
                )
        if changed: self.save_state()

    async def pnl_report(self, context=None) -> None:
        try:
            bal = float(
                self.account_service.get_account_api()
                .get_futures_account(GetFuturesAccountReqBuilder().set_currency(BASE_CURRENCY).build())
                .available_balance
            )
        except Exception as e:
            self.logger.error(f"pnl_report error: {e}")
            return
        pnl = bal - self.last_balance
        pct = (pnl / self.last_balance * 100) if self.last_balance else 0
        entry = {'time': datetime.utcnow().strftime('%Y-%m-%d %H:%M'), 'balance': bal, 'pnl': pnl, 'pct': pct}
        self.pnl_history.append(entry)
        self.last_balance = bal
        self.last_pnl_report = datetime.utcnow()
        self.save_state()
        await self.app.bot.send_message(
            chat_id=self.chat_id,
            text=(f"📊 <b>PnL Report</b> {entry['time']}:\n"
                  f"Change: <b>{pnl:+.4f} {BASE_CURRENCY}</b> (<b>{pct:+.2f}%</b>)"),
            parse_mode='HTML'
        )

    
    async def check_position_pnl(self, context=None) -> None:
        try:
            positions = self.futures_service.get_account_api().get_positions().data
            positions = [p for p in positions if p.symbol == SYMBOL]
            if not position or float(position.available_qty) == 0:
                return  # Pas de position ouverte

            entry_price = float(position.entry_price)
            mark_price = float(position.mark_price)
            size = float(position.available_qty)
            side = position.side.lower()  # 'long' ou 'short'

            pnl_pct = (mark_price - entry_price) / entry_price if side == 'long' else (entry_price - mark_price) / entry_price

            if pnl_pct >= TAKE_PROFIT:
                self.logger.info(f"TP atteint {pnl_pct:.2%}, clôture position")
                self.futures_service.get_order_api().close_position(SYMBOL, "market")
                await self.app.bot.send_message(chat_id=self.chat_id, text=f"🎯 <b>TP atteint</b>: {pnl_pct:.2%} — Position clôturée", parse_mode="HTML")

            elif pnl_pct <= -STOP_LOSS:
                self.logger.info(f"SL atteint {pnl_pct:.2%}, clôture position")
                self.futures_service.get_order_api().close_position(SYMBOL, "market")
                await self.app.bot.send_message(chat_id=self.chat_id, text=f"🛑 <b>SL atteint</b>: {pnl_pct:.2%} — Position clôturée", parse_mode="HTML")

        except Exception as e:
            self.logger.error(f"check_position_pnl error: {e}")

    
    def check_position_pnl(self):
        try:
            req = GetPositionListData()
            positions = self.futures_service.get_account_api().get_positions(req).data

            positions = [p for p in positions if p.symbol == SYMBOL and float(p.unrealised_pnl) != 0]

            for pos in positions:
                pnl = float(pos.unrealised_pnl)
                entry_price = float(pos.avg_entry_price)
                size = float(pos.current_qty)
                direction = pos.side.lower()

                pnl_pct = pnl / (entry_price * abs(size))

                if direction == "long" and pnl_pct >= TAKE_PROFIT:
                    self.logger.info(f"💰 TP atteint LONG: {pnl_pct:.2%}, fermeture en cours.")
                    self.close_position(SYMBOL, "sell", abs(size))

                elif direction == "short" and pnl_pct >= TAKE_PROFIT:
                    self.logger.info(f"💰 TP atteint SHORT: {pnl_pct:.2%}, fermeture en cours.")
                    self.close_position(SYMBOL, "buy", abs(size))

                elif pnl_pct <= -STOP_LOSS:
                    self.logger.info(f"❌ SL atteint {direction.upper()}: {pnl_pct:.2%}, fermeture en cours.")
                    side = "sell" if direction == "long" else "buy"
                    self.close_position(SYMBOL, side, abs(size))

        except Exception as e:
            self.logger.error(f"check_position_pnl error: {e}")


    def run(self) -> None:
                jq = self.app.job_queue
                # Notification de démarrage
                jq.run_once(self.startup_notify, when=0)
                if not self.active_orders:
                    # Construction initiale de la grille dès startup
                    jq.run_once(self.adjust_grid, when=1)
                else:
                    self.logger.info("Ordres rechargés, skip initial grid adjust")
                # Planification des ajustements périodiques
                jq.run_repeating(self.adjust_grid, interval=ADJUST_INTERVAL_MIN * 60, first=ADJUST_INTERVAL_MIN * 60)
                # Monitoring des ordres
                jq.run_repeating(self.monitor_orders, interval=5, first=10)
                # Rapport PnL
                jq.run_repeating(self.pnl_report, interval=PNL_REPORT_INTERVAL_H * 3600, first=PNL_REPORT_INTERVAL_H * 3600)
                jq.run_repeating(self.check_position_pnl, interval=10, first=15)
                # Démarrage du bot
                self.app.run_polling()

if __name__ == '__main__':
    GridTradingBotFutures().run()
