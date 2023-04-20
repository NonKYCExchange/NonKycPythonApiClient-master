# README  stub
This is the python API client for NonKYC exchange API. [Reference link](https://htmlpreview.github.io/?https://github.com/KarolTrzeszczkowski/NonKYCPythonApiClient/blob/master/docs/nonkyc.html)

<a name="settings"></a>
## Api Keys 
To use account endpoints and login to the websocket enerate api keys and put them in `nonkyc_settings.json` in the working directory. If you don't you'll still be able to use public methods.

nonkyc_settings.json format:
```
{"access_key": "your_access_key_here", "secret_key": "your_secret_key_here"}
```


## Examples in apython console (`pip install apython`)

### Using public methods
```
>>> from nonkyc import NonKYCClient
>>> x = NonKYCClient()
>>> await x.get_assets()
```
### Using private methods,  ([xeggegs_settings.json](#settings) required)
```
>>> from nonkyc import NonKYCClient
>>> x = NonKYCClient()
>>> await x.get_balances()
```
### Websocket subscriptions
```
>>> from nonkyc import NonKYCClient
>>> x = NonKYCClient()
>>> async def main():
...     async with x.websocket_context() as ws:
...         async for msg in x.subscribe_trades_generator(ws,'XRG/USDT'):
...             print(msg)
... 
>>> await main()
```

### Websocket private subscriptions,  ([xeggegs_settings.json](#settings) required):
```
>>> from nonkyc import NonKYCClient
>>> x = NonKYCClient()
>>> async def main():
...     async with x.websocket_context() as ws:
...         await x.ws_login(ws)
...         async for msg in x.subscribe_reports_generator(ws):
...             print(msg)
... 
>>> await main()
```
### Websocket public metods
```
>>> from nonkyc import NonKYCClient
>>> x = NonKYCClient()
>>> 
... async with x.websocket_context() as ws:
...     data = await x.ws_get_asset(ws, 'XRG')

```
### Websocket private metods,  ([xeggegs_settings.json](#settings) required)
```
>>> from nonkyc import NonKYCClient
>>> x = NonKYCClient()
>>> 
... async with x.websocket_context() as ws:
...     await x.ws_login(ws)
...     data = await x.ws_get_active_orders(ws)

```
### Reading multiple streams at once
```
>>> from lib.clients.nonkyc import NonKYCClient
>>> x = NonKYCClient()
>>> async with x.websocket_context() as ws:
...     xrg_trades = [
...         x.subscribe_trades_generator(ws, 'DOGE/USDT'),
...         x.subscribe_trades_generator(ws, 'LTC/USDT')
...     ]
...     async for msg in x.combine_streams(xrg_trades):
...         print(msg['params']['data'])
... 
```
## Examples

Run `immediate_or_cancel_example.py` according to the instruction in help. `python immediate_or_cancell_example.py --help`

## Contrinuting
Generate documentation: 
```
pdoc --html --output-dir docs --config show_source_code=False --force nonkyc.py
```
