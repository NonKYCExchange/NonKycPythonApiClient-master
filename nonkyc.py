"""The module for accessing NonKYC API written in asynchronous python"""

import json
import hashlib, hmac
try:
    from urllib import urlencode
except:
    from urllib.parse import urlencode
import string
import random
from time import time
import asyncio
import aiohttp
from aiohttp import ClientWebSocketResponse
from functools import wraps
from itertools import count
from decimal import Decimal
from datetime import datetime
from typing import Optional, List, Union
from collections import defaultdict
from asyncio import Queue

subscriptions = {
    "ticker": {
        "message": lambda symbol: {'method': 'subscribeTicker', 'params': {'symbol': symbol}},
        "methods": ['ticker']
    },
    'orderbook': {
        'message': lambda symbol, limit: {
            'method': 'subscribeOrderbook', 'params': pop_none({'symbol': symbol,'limit': limit})},
        'methods': ['snapshotOrderbook', 'updateOrderbook']
    },
    'trades': {
        'message': lambda symbol: {'method': 'subscribeTrades', 'params': {'symbol': symbol}},
        'methods': ['snapshotTrades', 'updateTrades']
    },
    'candles': {
        'message': lambda symbol, period, limit:  {
            'method': 'subscribeCandles',
            'params': pop_none({'symbol':symbol, 'period': period, 'limit': limit})},
        'methods': ['snapshotCandles', 'updateCandles']
    },
    'reports': {
        'message': lambda :{'method': 'subscribeReports', 'params': {}},
        'methods':  ['activeOrders', 'report']
    }
}

class Auth():
    """Authentication class that produces headers for api and login message for websocket."""
    def __init__(self, access_key: str, secret_key: str):
        super().__init__()
        self.secret_key = secret_key
        self.access_key = access_key

    def sign(self, payload: str) -> str:
        """Produces signature for an arbitrary string."""
        signature = hmac.new(
            self.secret_key.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return signature

    def headers(self, payload: str) -> dict:
        """Creates auth headers for rest API."""
        nonce = str(int(time()*1000))
        signature = self.sign(self.access_key+payload+nonce)
        headers =  {
            "X-API-KEY": self.access_key,
            "X-API-NONCE": nonce,
            "X-API-SIGN": signature,
            "Content-Type": "application/json",
        }
        return headers

    def ws_auth_message(self) -> str:
        """Creates a login string for websocket."""
        nonce = ''.join(random.choice(string.ascii_letters+string.digits) for i in range(20))
        return {
            'method':'login',
            'params':{
                'algo': "HS256",
                'pKey': self.access_key,
                'nonce': nonce,
                'signature': self.sign(nonce)
            }
        }

def private(func):
    """Decorator for declaring functions that require API keys.

    A decorated function will throw an assertion error if API keys aren't placed in `nonkyc_settings.json` file.
    """
    @wraps(func)
    async def wrap(*args, **kwargs):
        assert args[0].auth is not None, "Auth not found. You can't use Account endpoints without specifying API keys. Specify \"access_key\" and \"secret_key\" in \"nonkyc_settings.json\" file."
        return await func(*args, **kwargs)
    return wrap

def pop_none(params):
    """A helper function that removes parameters with a None value"""
    par = params.copy().items()
    for key, value in par:
        if not value:
            params.pop(key)
    return params

class WSException(Exception):
    pass

class WSListenerContext():
    """Websocket listener context class, wraps the asyncio websocket context by adding a listener and removes it afterwards."""
    def __init__(self, ws, listener_function):
        self.ws = ws
        self.listener = listener_function
        self.task = None

    async def __aenter__(self):
        ws = await self.ws.__aenter__()
        self.task = asyncio.create_task(self.listener(ws))
        ws.listener_task = self.task
        return ws

    async def __aexit__(self, *exc):
        self.task.cancel()
        await self.ws.__aexit__(*exc)
        return False

class NonKYCClient():
    """The class that for accessing NonKYC exchange API."""
    def __init__(self, settings_file = 'nonkyc_settings.json'):
        try:
            with open(settings_file) as f:
                settings = json.load(f)
            self.auth = Auth(settings['access_key'], settings['secret_key'])
        except (FileNotFoundError, KeyError) as ex:
            self.auth = None
        self.endpoint = "https://api.nonkyc.io/api/v2"
        self.ws_endpoint = 'wss://api.nonkyc.io'
        self.ws_responses = defaultdict(Queue)
        self.session = aiohttp.ClientSession()
        self.sending_event = asyncio.Event()
        self.ws_lock = asyncio.Lock()
        self.id = count(start=1)

    async def close(self):
        await self.session.close()

    async def get(self, path: str, params: dict = {}):
        """The basic GET query, inserts authorization header."""
        params_str = '?'+urlencode(params) if params else ''
        async with self.session.get(
            self.endpoint+path+params_str,
            headers = self.auth.headers(self.endpoint+path+params_str) if self.auth else {}
        ) as resp:
            if resp.content_type=='application/json':
                response = await resp.json()
            else:
                print(await resp.text())
                raise ValueError(f"Endpoint should be returning json, got {resp.content_type} instead.")
            return response

    async def post(self, path: str, data: dict):
        """The basic POST query, inserts authorization header"""
        data_str = json.dumps(data,separators=(',',':'))
        async with self.session.post(
            self.endpoint+path,
            data=data_str,
            headers = self.auth.headers(self.endpoint+path+data_str)
        ) as resp:
            if resp.content_type=='application/json':
                response = await resp.json()
            else:
                print(await resp.text())
                raise ValueError(f"Endpoint should be returning json, got {resp.content_type} instead.")
            return response

    async def ws_get(self, ws: ClientWebSocketResponse, message: dict):
        """Sends and receives the websocket response or throws a WSException if an error happened."""
        message['id'] = next(self.id)
        self.sending_event.set()
        async with self.ws_lock:
            await ws.send_str(json.dumps(message))
        q, e = self.ws_responses[message['id']], self.ws_responses['error']
        done, pending = await asyncio.wait([
            asyncio.create_task(q.get()),
            asyncio.create_task(e.get()),
            ws.listener_task
        ], return_when=asyncio.FIRST_COMPLETED)
        d = done.pop()
        if d.exception() is not None:
            raise d.exception()
        if 'error' in d.result().keys():
            raise WSException(d.result()['error'])
        else:
            self.ws_responses.pop(message['id'])
            return d.result()

    async def _ws_listener(self, ws: ClientWebSocketResponse):
        """A coroutine that listens on the websocket and dispatches the received messages to the queue.
        """
        while True:
            msg = None
            while not msg:
                async with self.ws_lock:
                    msg_task = asyncio.create_task(ws.receive())
                    done, pending = await asyncio.wait(
                        [msg_task, asyncio.create_task(self.sending_event.wait())],
                        return_when = asyncio.FIRST_COMPLETED
                    )
                    if msg_task in pending:
                        self.sending_event.clear()
                        msg_task.cancel()
                    else:
                        msg = msg_task.result()
                await asyncio.sleep(0)
            result = self._ws_parse_msg(ws, msg)
            if not result:
                raise WSException()


    def _ws_parse_msg(self, ws, msg):
        """Determines the type of ws message message and acts accor"""
        if msg.type == aiohttp.WSMsgType.TEXT:
            message = msg.json()
            if 'method' in message.keys():
                for stream, values in subscriptions.items():
                    if message['method'] in values['methods']:
                        self.ws_responses[stream].put_nowait(message)
                        return True
            if 'error' in message.keys():
                self.ws_responses['error'].put_nowait(message)
                print(message)
                return True
            if 'id' in message.keys():
                self.ws_responses[message['id']].put_nowait(message)
                return True
        elif msg.type == aiohttp.WSMsgType.ERROR:
            print(f"websocket connection closed with error {ws.exception()}")
            return False
        elif msg.type == aiohttp.WSMsgType.CLOSED:
            print(f"websocket connection closed.")
            return False

    def websocket_context(self):
        """Gets an entry point to a websocket, to be used with `async with ... as ws:`."""
        ws = self.session.ws_connect(self.ws_endpoint)
        return WSListenerContext(ws, self._ws_listener)

    async def ws_stream_generator(self, ws: ClientWebSocketResponse, stream, **params):
        """Creates a stream subscribtion in a form of a generator.

        The generator is meant to be iterated over with `async for` or `anext` builtin.

        Args:
            ws: Websocket response object.
            message: Stream subsctiption message.
            response_methods: The list of \"method\" keys returned from as the stream response.
                Allows you to drop some parts of the communication, like the initial snapshot.
        """
        message = subscriptions[stream]['message'](**params)

        self.sending_event.set()
        async with self.ws_lock:
            await ws.send_str(json.dumps(message))

        while True:
            q, e = self.ws_responses[stream], self.ws_responses['error']
            done, pending = await asyncio.wait([
                asyncio.create_task(q.get()),
                asyncio.create_task(e.get()),
                ws.listener_task
            ], return_when=asyncio.FIRST_COMPLETED)
            d = done.pop()
            if d.exception() is not None:
                raise d.exception()
            if 'error' in d.result().keys():
                raise WSException(d.result()['error'])
            else:
                yield d.result()

    async def combine_streams(self, stream_list: List):
        '''Lets you iterate over multiple streams at once as the messages come.

        Create a list of trade streams, `xrg_trades_stream = [self.subscribe_trades_generator(ws, 'XRG/USDT'), self.subscribe_trades_generator(ws, 'XRG/LTC')]` and listen to trades from both market at once `async for msg in combine_streams(xrg_trades_stream): ...`

        '''
        tasks = [asyncio.create_task(stream.__anext__()) for stream in stream_list]
        while True:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            d = done.pop()
            yield d.result()
            ind = tasks.index(d)
            tasks.remove(d)
            tasks.insert(ind, asyncio.create_task( stream_list[ind].__anext__()))


# Websocket methods

    @private
    async def ws_login(self, ws: ClientWebSocketResponse):
        """Executes a login on the websocket.

        Args:
            ws: Websocket response object.
        """
        login = self.auth.ws_auth_message()
        return await self.ws_get(ws, login)

    @private
    async def ws_create_order(
        self,
        ws: ClientWebSocketResponse,
        symbol: str,
        side: str,
        quantity: str,
        price: Optional[str] = None,
        order_type: Optional[str] = None,
        user_provided_id: Optional[str] = None,
        strict_validate: Optional[bool] = None,
    ):
        """Creates an order through a websocket.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\"
            side: Order side, \"sell\" or \"buy\".
            quantity: Quantity of the base asset.
            price: Price in terms of the quote asset required for the limit order.
            order_type: "limit" or \"market\" ordeer type.
            user_provided_id: Optional user-defined ID.
            strict_validate: Strict validate amount and price precision without truncation. Setting true will return an error if your quantity/price exceeds the correct decimal places. Default false will truncate values to allowed number of decimal places.
        """
        message = {
            'method': 'newOrder',
            'params': {
                'symbol':symbol,
                'side': side,
                'quantity': quantity,
                'price': price,
                'type': order_type,
                'userProvidedId': user_provided_id,
                'strictValidate': strict_validate
            }
        }
        pop_none(message['params'])
        return await self.ws_get(ws, message)

    @private
    async def ws_cancel_order(
        self,
        ws: ClientWebSocketResponse,
        order_id: str = None,
        user_provided_id: str = None
    ):
        """Cancel order through a websocket.

        Args:
            ws: Websocket response object.
            order_id: Exchange internal order ID.
            user_provided_id: Optional user-defined ID.
        """
        message = {'method': 'cancelOrder',
                   'params': {'orderId': order_id, 'userProvidedId': user_provided_id}}
        error_msg = "You have to unambiguously specify order ID to cancel it"
        assert (order_id is not None) ^ (user_provided_id is not None), error_msg 
        pop_none(message['params'])
        return await self.ws_get(ws, message)

    @private
    async def ws_get_active_orders(self, ws: ClientWebSocketResponse, symbol: str = None):
        """Get active orders through a websocket.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\".
        """
        message = {'method': 'getOrders', 'params': {'symbol': symbol}}
        pop_none(message['params'])
        return await self.ws_get(ws, message)

    @private
    async def ws_get_trading_balance(self, ws: ClientWebSocketResponse):
        """Get trading balance through a websocket.

        Args:
            ws: Websocket response object.
        """
        message = {'method':'getTradingBalance', 'params':{}}
        return await self.ws_get(ws, message)

    async def ws_get_assets_list(self, ws: ClientWebSocketResponse):
        """Get assets list through a websocket.

        Args:
            ws: Websocket response object.
        """
        message = {'method':'getAssets', 'params':{}}
        return await self.ws_get(ws, message)

    async def ws_get_asset(self, ws: ClientWebSocketResponse, ticker: str):
        """Get asset trhough a websocket.

        Args:
            ws: Websocket response object.
            ticker: Currency symbol, for example \"XRG\".
        """
        message = {'method':'getAssets', 'params':{'ticker': ticker}}
        return await self.ws_get(ws, message)

    async def ws_get_markets_list(self, ws: ClientWebSocketResponse):
        """Get markets list through a websocket.

        Args:
            ws: Websocket response object.
        """
        message = {'method':'getMarkets', 'params':{}}
        return await self.ws_get(ws, message)

    async def ws_get_market(self, ws: ClientWebSocketResponse, symbol: str):
        """Get market info through a websocket.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\".
        """
        message = {'method':'getMarket', 'params': {'symbol': symbol}}
        return await self.ws_get(ws, message)

    async def ws_get_trade_history(
        self,
        ws: ClientWebSocketResponse,
        symbol: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        sort: Optional[str] = None,
        history_from: Optional[Union[str, datetime]] = None,
        history_till: Optional[Union[str, datetime]] = None
    ):
        """Get trade history through a websocket.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\".
            limit: Number of entries, (Optional, default: 100, max: 1000).
            offset: Offset the results by this number (Optional, default: 0).
            sort: 'ASC' for ascending order, 'DESC' for decending order. (Optional, default: DESC)
            history_from: This is earliest datetime. Can be a datetime object or string formatted
                as `YYYY-MM-DDTH:MM:SSZ` (Optional). When using from or till, then both are required.
            history_till: This is latest datetime. Formatted the same as `history_from` (Optional).
                When using from or till, then both are required.

        """
        if isinstance(history_from, datetime):
            history_from = history_from.strftime("%Y-%m-%dT%H:%M:%SZ")
            history_till = history_till.strftime("%Y-%m-%dT%H:%M:%SZ")
        message = {
            'method': 'getTrades',
            'params': {
                'symbol': symbol,
                'limit': limit,
                'offset': offset,
                'sort': sort,
                'from': history_from,
                'till': history_till
            }
        }
        pop_none(message['params'])
        return await self.ws_get(ws, message)

# Public streams


    def subscribe_ticker_generator(self, ws: ClientWebSocketResponse, symbol: str):
        """Creates a ticker stream subscribtion in a form of a generator.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\".
        """
        return self.ws_stream_generator(ws, "ticker", symbol=symbol)

    async def unsubscribe_ticker(self, ws: ClientWebSocketResponse, symbol: str):
        """Unsubscribes the ticker stream subscribtion.

        Args:
            ws: Websocket response object.
        """
        message = {'method': 'unsubscribeTicker', 'params': {'symbol': symbol}}
        return await self.ws_get(ws, message)

    def subscribe_orderbook_generator(
            self,
            ws: ClientWebSocketResponse,
            symbol: str,
            limit: Optional[int] = None
    ):
        """Creates a orderbook stream subscribtion in a form of a generator.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\".
            limit: (Optional) The number of items on each side of the books. Default: 100 
        """
        return self.ws_stream_generator(ws, "orderbook", symbol=symbol, limit=limit)

    async def unsubscribe_orderbook(self, ws: ClientWebSocketResponse, symbol: str):
        """Unsubscribes the orderbook stream subscribtion.

        Args:
            ws: Websocket response object.
        """
        message = {'method': 'unsubscribeOrderbook', 'params': {'symbol': symbol}}
        return await self.ws_get(ws, message)

    def subscribe_trades_generator(self, ws: ClientWebSocketResponse, symbol: str):
        """Creates a trades stream subscribtion in a form of a generator.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\".
        """
        return self.ws_stream_generator(ws, "trades", symbol=symbol)
        
    async def unsubscribe_trades(self, ws: ClientWebSocketResponse, symbol: str):
        """Unsubscribes the trades stream subscribtion.

        Args:
            ws: Websocket response object.
        """
        message = {'method': 'unsubscribeTrades', 'params': {'symbol': symbol}}
        return await self.ws_get(ws, message)

    def subscribe_candles_generator(
            self,
            ws: ClientWebSocketResponse,
            symbol: str,
            period: int,
            limit: Optional[int] = None
    ):
        """Creates a candles stream subscribtion in a form of a generator.

        Args:
            ws: Websocket response object.
            symbol: Market symbol, two tickers joined with a \"/\". For example \"XRG/LTC\".
            period: The candlestick period you would like (Minutes). (5, 15, 30, 60, 180, 240, 480, 720, 1440)
            limit: Limit the results. (Optional, default: 100)
        """
        return self.ws_stream_generator(
            ws, "candles", symbol=symbol, period=period, limit=limit)

    async def unsubscribe_candles(self, ws: ClientWebSocketResponse, symbol: str, period: int):
        """Unsubscribes the reports stream subscribtion.

        Args:
            ws: Websocket response object.
            period: The candlestick period you would like (Minutes). (5, 15, 30, 60, 180, 240, 480, 720, 1440)
        """
        message ={'method': 'unsubscribeCandles',
                              'params': {'symbol': symbol, 'period':period}}

        return await self.ws_get(ws, message)


# Private streams

    def ws_subscribe_reports_generator(self, ws: ClientWebSocketResponse):
        """Creates a reports stream subscribtion in a form of a generator.

        Args:
            ws: Websocket response object.
        """
        #async generators are weird to handle with decorators, manually checking for auth.
        assert self.auth, "Auth not found. You can't use Account endpoints without specifying API keys. Specify \"access_key\" and \"secret_key\" in \"nonkyc_settings.json\" file."
        return self.ws_stream_generator(ws, "reports")

    @private
    async def ws_unsubscribe_reports(self, ws: ClientWebSocketResponse):
        """Unsubscribes the reports stream subscribtion.

        Args:
            ws: Websocket response object.
        """
        message = {'method': 'subscribeReports', 'params': {}}
        return await self.ws_get(ws, message)

# Public methods

    async def get_assets(self):
        """Get a list of assets."""
        path = '/asset/getlist'
        return await self.get(path)

    async def get_asset_by_id(self, asset_id: str):
        """Get asset by id.

        Args:
            asset_id: Exchange internal asset ID.
        """
        path = f'/asset/getbyid/{asset_id}'
        return await self.get(path)

    async def get_asset_by_id(self, ticker: str):
        """Get asset by ticker.

        Args:
            ticker: Currency symbol, for example \"XRG\".
        """
        path = f'/asset/getbyticker/{ticker}'
        return await self.get(path)

    async def get_markets(self):
        """Get list of markets"""
        path = '/market/getlist'
        return await self.get(path)

    async def get_market_by_id(self, market_id: str):
        """Get market by market id.

        Args:
            market_id: Exchange internal market ID.
        """
        path = f'/market/getbyid/{market_id}'
        return await self.get(path)

    async def get_market_by_symbol(self, symbol: str):
        """Get market by symbol.

        Args:
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """
        path = f'/market/getbysymbol/{symbol}'
        return await self.get(path)

    async def get_pools(self):
        """Get list of liquidity pools"""
        path = '/pool/getlist'
        return await self.get(path)

    async def get_pool_by_id(self, pool_id: str):
        """Get pool by pool id.

        Args:
            pool_id: Exchange internal market ID.
        """
        path = f'/pool/getbyid/{pool_id}'
        return await self.get(path)

    async def get_pool_by_symbol(self, pool_symbol: str):
        """Get pool by symbol.

        Args:
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """
        path = f'/pool/getbysymbol/{pool_symbol}'
        return await self.get(path)

    async def get_orderbook_by_symbol(self, symbol: str):
        """Get market orderbook by symbol.

        Args:
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """
        path = f'/market/getorderbookbysymbol/{symbol}'
        return await self.get(path)

    async def get_orderbook_by_market_id(self, market_id: str):
        """Get market orderbook by market id.

        Args:
            market_id: Exchange internal market ID.
        """
        path = f'/market/getorderbookbymarketid/{market_id}'
        return await self.get(path)

# Private methods

    @private
    async def get_balances(self):
        """Get detailed acccount balance information."""
        path = '/balances'
        return await self.get(path)

    @private
    async def get_nonzero_balances(self):
        """Get account balance information if balance is nonzero."""
        bal = await self.get_balances()
        return [
            b for b in bal
            if Decimal(b['available'])!=0 or Decimal(b['pending'])!=0 or Decimal(b['held'])!=0
        ]

    @private
    async def get_deposit_address(self, ticker: str):
        """Get your deposit address.

        Args:
            ticker: Currency symbol, for example \"XRG\".
        """
        path = f'/getdepositaddress/{ticker}'
        return await self.get(path)

    @private
    async def create_order(
        self,
        symbol: str,
        side: str,
        quantity: str,
        price: Optional[str] = None,
        order_type: Optional[str] = None,
        user_provided_id: Optional[str] = None,
        strict_validate: Optional[str] = None,
    ):
        """Creates an order.

        Args:
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
            side: Order side, \"sell\" or \"buy\".
            quantity: Quantity of the base asset.
            price: Price in terms of the quote asset required for the limit order.
            order_type: "limit" or \"market\" ordeer type.
            user_provided_id: Optional user-defined ID.
            strict_validate: Strict validate amount and price precision without truncation. Setting true will return an error if your quantity/price exceeds the correct decimal places. Default false will truncate values to allowed number of decimal places.
        """

        path = '/createorder'
        params =  {
            'userProvidedId': user_provided_id,
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': quantity,
            'price': price,
            'strictValidate': strict_validate,
        }
        if order_type in [None, 'limit']:
            assert price is not None, "Specify price for a limit order"
        pop_none(params)
        return await self.post(path, params)

    @private
    async def cancel_order(self, order_id: str):
        """Cancel an open spot trade order.

        Args:
            order_id: Order ID to cancel
        """
        path = '/cancel_order'
        params = {'id': order_id}
        return self.post(path, params)

    @private
    async def cancel_market_orders(self, symbol: str, side: str):
        """Cancel a batch of open orders in a spot market.

        Args:
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
            side: \"sell\", \"buy\" or \"all\".
        """
        path = '/cancelallorders'
        data = {
            'symbol': symbol,
            'side': side
        }
        return await self.post(path, data)

    @private
    async def create_withdrawal(
        self,
        ticker: str,
        quantity: str,
        address: str,
        payment_id: Optional[str] = None
    ):
        """Make a new withdrawal request.

        Args:
            ticker: Currency symbol, for example \"XRG\".
            quantity: Quantity to withdraw.
            address: Address to withdraw to. Must be a validated address on your account.
            paymentid: If required, provide payment id.
        """
        path = '/createwithdrawal'
        data = {
            "ticker": ticker,
            "quantity": quantity,
            "address": address,
            "paymentId": payment_id
        }
        pop_none(data)
        return self.post(path, data)

    @private
    async def get_deposits(self, limit: int, skip: int, ticker: Optional[str] = None):
        """Get a list of your account deposits.

        Args:
            ticker: Currency symbol, for example \"XRG\"(Optional).
            limit: Maximum limit is 500.
            skip: Skip this many records.
        """
        path = '/getdeposits'
        params = { 'ticker': ticker, 'limit': limit, 'skip':skip }
        pop_none(params)
        return self.get(path, params)

    @private
    async def get_withdrawals(self, limit: int, skip: int, ticker: Optional[str] = None):
        """Get a list of your account withdrawals. Ordered by created timestamp descending.

        Args:
            ticker: Currency symbol, for example \"XRG\"(Optional).
            limit: Maximum limit is 500.
            skip: Skip this many records.
        """
        path = '/getwithdrawals'
        params = { 'ticker': ticker, 'limit': limit, 'skip':skip }
        pop_none(params)
        return self.get(path, params)

    @private
    async def get_order(self, order_id: str):
        """Get an order by id.

        Args:
            order_id: NonKYC orderId or userProvidedId
        """
        path = f'/getorder/{order_id}'
        return await self.get(path)

    @private
    async def get_my_orders(
        self,
        status: str,
        limit: int,
        skip: int,
        symbol: Optional[str] = None
    ):
        """Get a list of your orders. Ordered by created timestamp descending.

        Args:
            status: Current status of orders. 'active', 'filled', or 'cancelled'.
            limit: Maximum limit is 500.
            skip: Skip this many records.
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """
        path = '/getorders'
        params = {
            'symbol': symbol,
            'status':status,
            'limit': limit,
            'skip': skip
        }
        pop_none(params)
        return await self.get(path, params)

    @private
    async def get_trades(self, limit: int, skip: int, symbol: Optional[str] = None):
        """Get a list of your spot market trades.

        Get a list of your market trades. Ordered by created timestamp descending

        Args:
            limit: Maximum limit is 500.
            skip: Skip this many records.
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """
        path = '/gettrades'
        params = {'limit':limit, 'skip':skip, 'symbol':symbol}
        pop_none(params)
        return await self.get(path, params)

    @private
    async def get_trades_since(
        self,
        since: str,
        limit: int,
        skip: int,
        symbol: Optional[str] = None
    ):
        """Get a list of your spot trades since a timestamp in millisec.

        Get a list of your trades. Ordered by created timestamp ASCENDING

        Args:
            since: A timestamp in milliseconds you want to retreive records after
            limit: Maximum limit is 500.
            skip: Skip this many records.
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """

        path = '/gettradesince'
        params = {'since':since, 'limit':limit, 'skip':skip, 'symbol':symbol}
        pop_none(params)
        return await self.get(path, params)

    @private
    async def get_pool_trades(self, limit: int, skip: int, symbol: Optional[str] = None):
        """Get a list of your pool trades.

        Get a list of your pool trades. Ordered by created timestamp descending

        Args:
            limit: Maximum limit is 500.
            skip: Skip this many records.
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """

        path = '/getpooltrades'
        params = {'limit':limit, 'skip':skip, 'symbol':symbol}
        pop_none(params)
        return await self.get(path, params)

    @private
    async def get_pool_trades_since(
        self,
        since: str,
        limit: int,
        skip: int,
        symbol: Optional[str] = None
    ):
        """Get a list of your pool trades since a timestamp in millisec.

        Get a list of your pool trades. Ordered by created timestamp ASCENDING

        Args:
            since: A timestamp in milliseconds you want to retreive records after
            limit: Maximum limit is 500.
            skip: Skip this many records.
            symbol: Market symbol, two tickers joined with a \"_\". For example \"XRG_LTC\".
        """


        path = '/getpooltradessince'
        params = {'since':since, 'limit':limit, 'skip':skip, 'symbol':symbol}
        pop_none(params)
        return await self.get(path, params)

