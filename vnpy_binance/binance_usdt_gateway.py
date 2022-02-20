"""
1. 只支持全仓模式
2. 只支持单向持仓模式
3. 只支持正向合约
"""

import urllib
import hashlib
import hmac
import time
from copy import copy
from datetime import datetime, timedelta
from enum import Enum
from threading import Lock
from typing import Any, Dict, List, Tuple
from vnpy.trader.utility import round_to
import pytz

from requests.exceptions import SSLError

from vnpy.trader.constant import (
    Direction,
    Exchange,
    Product,
    Status,
    OrderType,
    Interval,
    Offset
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    TickData,
    OrderData,
    TradeData,
    AccountData,
    ContractData,
    PositionData,
    BarData,
    OrderRequest,
    CancelRequest,
    SubscribeRequest,
    HistoryRequest,
    LeverageRequest,
    PositionSideRequest,
    MarginTypeRequest
)
from vnpy.trader.event import EVENT_TIMER
from vnpy.event import Event, EventEngine

from vnpy_rest import Request, RestClient, Response
from vnpy_websocket import WebsocketClient


# 中国时区
CHINA_TZ = pytz.timezone("Asia/Shanghai")

# 实盘正向合约REST API地址
F_REST_HOST: str = "https://fapi.binance.com"

# 实盘正向合约Websocket API地址
F_WEBSOCKET_TRADE_HOST: str = "wss://fstream.binance.com/ws/"
F_WEBSOCKET_DATA_HOST: str = "wss://fstream.binance.com/stream"

# 模拟盘正向合约REST API地址
F_TESTNET_REST_HOST: str = "https://testnet.binancefuture.com"

# 模拟盘正向合约Websocket API地址
F_TESTNET_WEBSOCKET_TRADE_HOST: str = "wss://stream.binancefuture.com/ws/"
F_TESTNET_WEBSOCKET_DATA_HOST: str = "wss://stream.binancefuture.com/stream"

# 委托状态映射
STATUS_BINANCES2VT: Dict[str, Status] = {
    "NEW": Status.NOTTRADED,
    "PARTIALLY_FILLED": Status.PARTTRADED,
    "FILLED": Status.ALLTRADED,
    "CANCELED": Status.CANCELLED,
    "REJECTED": Status.REJECTED,
    "EXPIRED": Status.CANCELLED
}

# 委托类型映射
ORDERTYPE_VT2BINANCES: Dict[OrderType, Tuple[str, str]] = {
    OrderType.LIMIT: ("LIMIT", "GTC"),
    OrderType.MARKET: ("MARKET", "GTC"),
    OrderType.FAK: ("LIMIT", "IOC"),
    OrderType.FOK: ("LIMIT", "FOK"),
}
ORDERTYPE_BINANCES2VT: Dict[Tuple[str, str], OrderType] = {v: k for k, v in ORDERTYPE_VT2BINANCES.items()}

# 买卖方向映射
DIRECTION_VT2BINANCES: Dict[Direction, str] = {
    Direction.LONG: "LONG",
    Direction.SHORT: "SHORT",
    Direction.NET: "BOTH"
}
DIRECTION_BINANCES2VT: Dict[str, Direction] = {v: k for k, v in DIRECTION_VT2BINANCES.items()}

SIDE_VT2BINANCES: Dict[Tuple[Direction, Offset], str] = {
    (Direction.LONG, Offset.OPEN): "BUY",
    (Direction.LONG, Offset.CLOSE): "SELL",
    (Direction.SHORT, Offset.OPEN): "SELL",
    (Direction.SHORT, Offset.CLOSE): "BUY"
}

OFFSET_BINANCES2VT: Dict[Tuple[Direction, str], Offset] = {
    (Direction.LONG, "BUY"): Offset.OPEN,
    (Direction.LONG, "SELL"): Offset.CLOSE,
    (Direction.SHORT, "BUY"): Offset.CLOSE,
    (Direction.SHORT, "SELL"): Offset.OPEN
}

# 数据频率映射
INTERVAL_VT2BINANCES: Dict[Interval, str] = {
    Interval.MINUTE: "1m",
    Interval.FIVE_MINUTE: "5m",
    Interval.HOUR: "1h",
    Interval.DAILY: "1d",
}

# 时间间隔映射
TIMEDELTA_MAP: Dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}

# 合约数据全局缓存字典
symbol_contract_map: Dict[str, ContractData] = {}


# 鉴权类型
class Security(Enum):
    NONE: int = 0
    SIGNED: int = 1
    API_KEY: int = 2


class MarginType(Enum):
    """
    逐仓、全仓
    """
    ISOLATED: str = "ISOLATED"
    CROSSED: str = "CROSSED"

class BinanceUsdtGateway(BaseGateway):
    """
    vn.py用于对接币安正向合约的交易接口。
    """

    default_name: str = "BINANCE_USDT"

    default_setting: Dict[str, Any] = {
        "key": "",
        "secret": "",
        "服务器": ["REAL", "TESTNET"],
        "代理地址": "",
        "代理端口": 0,
    }

    exchanges: Exchange = [Exchange.BINANCE]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        """构造函数"""
        super().__init__(event_engine, gateway_name)

        self.trade_ws_api: "BinanceUsdtTradeWebsocketApi" = BinanceUsdtTradeWebsocketApi(self)
        self.market_ws_api: "BinanceUsdtDataWebsocketApi" = BinanceUsdtDataWebsocketApi(self)
        self.rest_api: "BinanceUsdtRestApi" = BinanceUsdtRestApi(self)

        self.orders: Dict[str, OrderData] = {}

    def connect(self, setting: dict) -> None:
        """连接交易接口"""
        key: str = setting["key"]
        secret: str = setting["secret"]
        server: str = setting["服务器"]
        proxy_host: str = setting["代理地址"]
        proxy_port: int = setting["代理端口"]

        self.rest_api.connect(key, secret, server, proxy_host, proxy_port)
        self.market_ws_api.connect(proxy_host, proxy_port, server)

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        self.market_ws_api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        return self.rest_api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        self.rest_api.cancel_order(req)

    def set_leverage(self, req: LeverageRequest) -> Request:
        """设置杠杆"""
        self.rest_api.set_leverage(req)

    def set_position_side(self, req: PositionSideRequest) -> Request:
        """设置持仓方向"""
        self.rest_api.set_position_side(req)

    def set_margin_type(self, req: MarginTypeRequest) -> Request:
        """设置逐仓全仓"""
        self.rest_api.set_margin_type(req)

    def query_account(self) -> None:
        """查询资金"""
        pass

    def query_position(self) -> None:
        """查询持仓"""
        pass

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        return self.rest_api.query_history(req)

    def close(self) -> None:
        """关闭连接"""
        self.rest_api.stop()
        self.trade_ws_api.stop()
        self.market_ws_api.stop()

    def process_timer_event(self, event: Event) -> None:
        """定时事件处理"""
        self.rest_api.keep_user_stream()

    def on_order(self, order: OrderData) -> None:
        """推送委托数据"""
        self.orders[order.orderid] = copy(order)
        super().on_order(order)

    def get_order(self, orderid: str) -> OrderData:
        """查询委托数据"""
        return self.orders.get(orderid, None)


class BinanceUsdtRestApi(RestClient):
    """币安正向合约的REST API"""

    def __init__(self, gateway: BinanceUsdtGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: BinanceUsdtGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.trade_ws_api: BinanceUsdtTradeWebsocketApi = self.gateway.trade_ws_api

        self.key: str = ""
        self.secret: str = ""

        self.user_stream_key: str = ""
        self.keep_alive_count: int = 0
        self.recv_window: int = 5000
        self.time_offset: int = 0

        self.order_count: int = 1_000_000
        self.order_count_lock: Lock = Lock()
        self.connect_time: int = 0

    def sign(self, request: Request) -> Request:
        """生成币安签名"""
        security: Security = request.data["security"]
        if security == Security.NONE:
            request.data = None
            return request

        if request.params:
            path: str = request.path + "?" + urllib.parse.urlencode(request.params)
        else:
            request.params = dict()
            path: str = request.path

        if security == Security.SIGNED:
            timestamp: int = int(time.time() * 1000)

            if self.time_offset > 0:
                timestamp -= abs(self.time_offset)
            elif self.time_offset < 0:
                timestamp += abs(self.time_offset)

            request.params["timestamp"] = timestamp

            query: str = urllib.parse.urlencode(sorted(request.params.items()))
            signature: bytes = hmac.new(self.secret, query.encode(
                "utf-8"), hashlib.sha256).hexdigest()

            query += "&signature={}".format(signature)
            path: str = request.path + "?" + query

        request.path = path
        request.params = {}
        request.data = {}

        # 添加请求头
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "X-MBX-APIKEY": self.key,
            "Connection": "close"
        }

        if security in [Security.SIGNED, Security.API_KEY]:
            request.headers = headers

        return request

    def connect(
        self,
        key: str,
        secret: str,
        server: str,
        proxy_host: str,
        proxy_port: int
    ) -> None:
        """连接REST服务器"""
        self.key = key
        self.secret = secret.encode()
        self.proxy_port = proxy_port
        self.proxy_host = proxy_host
        self.server = server

        self.connect_time = (
            int(datetime.now().strftime("%y%m%d%H%M%S")) * self.order_count
        )

        if self.server == "REAL":
            self.init(F_REST_HOST, proxy_host, proxy_port)
        else:
            self.init(F_TESTNET_REST_HOST, proxy_host, proxy_port)

        self.start()

        self.gateway.write_log("REST API启动成功")

        self.query_time()
        self.query_account()
        self.query_position()
        self.query_order()
        self.query_contract()
        self.start_user_stream()

    def query_time(self) -> None:
        """查询时间"""
        data: dict = {
            "security": Security.NONE
        }

        path: str = "/fapi/v1/time"

        return self.add_request(
            "GET",
            path,
            callback=self.on_query_time,
            data=data
        )

    def query_account(self) -> None:
        """查询资金"""
        data: dict = {"security": Security.SIGNED}

        path: str = "/fapi/v1/account"

        self.add_request(
            method="GET",
            path=path,
            callback=self.on_query_account,
            data=data
        )

    def query_position(self) -> None:
        """查询持仓"""
        data: dict = {"security": Security.SIGNED}

        path: str = "/fapi/v1/positionRisk"

        self.add_request(
            method="GET",
            path=path,
            callback=self.on_query_position,
            data=data
        )

    def query_order(self) -> None:
        """查询未成交委托"""
        data: dict = {"security": Security.SIGNED}

        path: str = "/fapi/v1/openOrders"

        self.add_request(
            method="GET",
            path=path,
            callback=self.on_query_order,
            data=data
        )

    def query_contract(self) -> None:
        """查询合约信息"""
        data: dict = {
            "security": Security.NONE
        }

        path: str = "/fapi/v1/exchangeInfo"

        self.add_request(
            method="GET",
            path=path,
            callback=self.on_query_contract,
            data=data
        )

    def _new_order_id(self) -> int:
        """生成本地委托号"""
        with self.order_count_lock:
            self.order_count += 1
            return self.order_count

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        # 生成本地委托号
        orderid: str = str(self.connect_time + self._new_order_id())

        # 推送提交中事件
        order: OrderData = req.create_order_data(
            orderid,
            self.gateway_name
        )
        self.gateway.on_order(order)

        data: dict = {
            "security": Security.SIGNED
        }

        # 生成委托请求
        params: dict = {
            "symbol": req.symbol,
            "side": SIDE_VT2BINANCES.get((req.direction, req.offset), None),
            "positionSide": DIRECTION_VT2BINANCES[req.direction],
            "quantity": float(req.volume),
            "newClientOrderId": orderid,
        }

        if req.type == OrderType.MARKET:
            params["type"] = "MARKET"
        else:
            order_type, time_condition = ORDERTYPE_VT2BINANCES[req.type]
            params["type"] = order_type
            params["timeInForce"] = time_condition
            params["price"] = float(req.price)

        path: str = "/fapi/v1/order"

        self.add_request(
            method="POST",
            path=path,
            callback=self.on_send_order,
            data=data,
            params=params,
            extra=order,
            on_error=self.on_send_order_error,
            on_failed=self.on_send_order_failed
        )

        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        data: dict = {
            "security": Security.SIGNED
        }

        params: dict = {
            "symbol": req.symbol,
            "origClientOrderId": req.orderid
        }

        path: str = "/fapi/v1/order"

        order: OrderData = self.gateway.get_order(req.orderid)

        self.add_request(
            method="DELETE",
            path=path,
            callback=self.on_cancel_order,
            params=params,
            data=data,
            on_failed=self.on_cancel_failed,
            extra=order
        )

    def set_leverage(self, req: LeverageRequest) -> Request:
        data: dict = {
            "security": Security.SIGNED
        }

        params: dict = {
            "symbol": req.symbol,
            "leverage": req.leverage
        }

        path: str = "/fapi/v1/leverage"

        self.add_request(
            method="POST",
            path=path,
            callback=self.on_set_leverage,
            params=params,
            data=data
        )

    def set_position_side(self, req: PositionSideRequest) -> Request:
        data: dict = {
            "security": Security.SIGNED
        }

        params: dict = {
            "dualSidePosition": "true" if req.dual_side_position else "false"
        }

        path: str = "/fapi/v1/positionSide/dual"

        self.add_request(
            method="POST",
            path=path,
            callback=self.on_set_position_side,
            params=params,
            data=data
        )

    def set_margin_type(self, req: MarginTypeRequest) -> Request:
        data: dict = {
            "security": Security.SIGNED
        }

        params: dict = {
            "symbol": req.symbol,
            "marginType": req.margin_type
        }

        path: str = "/fapi/v1/marginType"

        self.add_request(
            method="POST",
            path=path,
            callback=self.on_set_margin_type,
            params=params,
            data=data
        )

    def start_user_stream(self) -> Request:
        """生成listenKey"""
        data: dict = {
            "security": Security.API_KEY
        }

        path: str = "/fapi/v1/listenKey"

        self.add_request(
            method="POST",
            path=path,
            callback=self.on_start_user_stream,
            data=data
        )

    def keep_user_stream(self) -> Request:
        """延长listenKey有效期"""
        self.keep_alive_count += 1
        if self.keep_alive_count < 600:
            return
        self.keep_alive_count = 0

        data: dict = {
            "security": Security.API_KEY
        }

        params: dict = {
            "listenKey": self.user_stream_key
        }

        path: str = "/fapi/v1/listenKey"

        self.add_request(
            method="PUT",
            path=path,
            callback=self.on_keep_user_stream,
            params=params,
            data=data,
            on_error=self.on_keep_user_stream_error
        )

    def on_query_time(self, data: dict, request: Request) -> None:
        """时间查询回报"""
        local_time: int = int(time.time() * 1000)
        server_time: int = int(data["serverTime"])
        self.time_offset: int = local_time - server_time

    def on_set_leverage(self, data: dict, request: Request) -> None:
        """"""
        self.gateway.write_log(f"设置杠杆比例：gateway={self.gateway_name} symbol={data['symbol']} leverage={data['leverage']}")

    def on_set_position_side(self, data: dict, request: Request) -> None:
        """"""
        if data['code'] == 200:
            self.gateway.write_log(f"设置持仓模式成功：gateway={self.gateway_name}")

    def on_set_margin_type(self, data: dict, request: Request) -> None:
        """"""
        if data['code'] == 200:
            self.gateway.write_log(f"设置逐全仓模式成功：gateway={self.gateway_name} symbol={data['symbol']}")

    def on_query_account(self, data: dict, request: Request) -> None:
        """资金查询回报"""
        for asset in data["assets"]:
            account: AccountData = AccountData(
                accountid=asset["asset"],
                balance=float(asset["walletBalance"]),
                frozen=float(asset["maintMargin"]),
                gateway_name=self.gateway_name
            )

            if account.balance:
                self.gateway.on_account(account)

        self.gateway.write_log("账户资金查询成功")

    def on_query_position(self, data: dict, request: Request) -> None:
        """持仓查询回报"""
        for d in data:
            position: PositionData = PositionData(
                symbol=d["symbol"],
                exchange=Exchange.BINANCE,
                direction=DIRECTION_BINANCES2VT[d["positionSide"]],
                volume=float(d["positionAmt"]),
                price=float(d["entryPrice"]),
                pnl=float(d["unRealizedProfit"]),
                gateway_name=self.gateway_name,
            )

            if position.volume:
                volume = d["positionAmt"]
                if '.' in volume:
                    position.volume = float(d["positionAmt"])
                else:
                    position.volume = int(d["positionAmt"])

                self.gateway.on_position(position)

        self.gateway.write_log("持仓信息查询成功")

    def on_query_order(self, data: dict, request: Request) -> None:
        """未成交委托查询回报"""
        for d in data:
            key: Tuple[str, str] = (d["type"], d["timeInForce"])
            order_type: OrderType = ORDERTYPE_BINANCES2VT.get(key, None)
            if not order_type:
                continue

            order: OrderData = OrderData(
                orderid=d["clientOrderId"],
                symbol=d["symbol"],
                exchange=Exchange.BINANCE,
                price=float(d["price"]),
                avg_price=float(d["avgPrice"]),
                volume=float(d["origQty"]),
                type=order_type,
                direction=DIRECTION_BINANCES2VT[d["positionSide"]],
                offset=OFFSET_BINANCES2VT[(DIRECTION_BINANCES2VT[d["positionSide"]], d["side"])],
                traded=float(d["executedQty"]),
                status=STATUS_BINANCES2VT.get(d["status"], None),
                datetime=generate_datetime(d["time"]),
                gateway_name=self.gateway_name,
            )
            self.gateway.on_order(order)

        self.gateway.write_log("委托信息查询成功")

    def on_query_contract(self, data: dict, request: Request) -> None:
        """合约信息查询回报"""
        for d in data["symbols"]:
            base_currency: str = d["baseAsset"]
            quote_currency: str = d["quoteAsset"]
            name: str = f"{base_currency.upper()}/{quote_currency.upper()}"

            pricetick: int = 1
            min_volume: int = 1

            for f in d["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    pricetick = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    min_volume = float(f["stepSize"])

            contract: ContractData = ContractData(
                symbol=d["symbol"],
                exchange=Exchange.BINANCE,
                name=name,
                pricetick=pricetick,
                size=1,
                min_volume=min_volume,
                product=Product.FUTURES,
                net_position=False,
                history_data=True,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_contract(contract)

            symbol_contract_map[contract.symbol] = contract

        self.gateway.write_log("合约信息查询成功")

    def on_send_order(self, data: dict, request: Request) -> None:
        """委托下单回报"""
        pass

    def on_send_order_failed(self, status_code: str, request: Request) -> None:
        """委托下单失败服务器报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        msg: str = f"委托失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_send_order_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """委托下单回报函数报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        if not issubclass(exception_type, (ConnectionError, SSLError)):
            self.on_error(exception_type, exception_value, tb, request)

    def on_cancel_order(self, data: dict, request: Request) -> None:
        """委托撤单回报"""
        pass

    def on_cancel_failed(self, status_code: str, request: Request) -> None:
        """撤单回报函数报错回报"""
        if request.extra:
            order = request.extra
            order.status = Status.REJECTED
            self.gateway.on_order(order)

        msg = f"撤单失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_start_user_stream(self, data: dict, request: Request) -> None:
        """生成listenKey回报"""
        self.user_stream_key = data["listenKey"]
        self.keep_alive_count = 0

        if self.server == "REAL":
            url = F_WEBSOCKET_TRADE_HOST + self.user_stream_key
        else:
            url = F_TESTNET_WEBSOCKET_TRADE_HOST + self.user_stream_key

        self.trade_ws_api.connect(url, self.proxy_host, self.proxy_port)

    def on_keep_user_stream(self, data: dict, request: Request) -> None:
        """延长listenKey有效期回报"""
        pass

    def on_keep_user_stream_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """延长listenKey有效期函数报错回报"""
        # 当延长listenKey有效期时，忽略超时报错
        if not issubclass(exception_type, TimeoutError):
            self.on_error(exception_type, exception_value, tb, request)

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        history: List[BarData] = []
        limit: int = 1500

        start_time: int = int(datetime.timestamp(req.start))

        while True:
            # 创建查询参数
            params: dict = {
                "symbol": req.symbol,
                "interval": INTERVAL_VT2BINANCES[req.interval],
                "limit": limit
            }

            params["startTime"] = start_time * 1000
            path: str = "/fapi/v1/klines"
            if req.end:
                end_time = int(datetime.timestamp(req.end))
                params["endTime"] = end_time * 1000     # 转换成毫秒

            resp: Response = self.request(
                "GET",
                path=path,
                data={"security": Security.NONE},
                params=params
            )

            # 如果请求失败则终止循环
            if resp.status_code // 100 != 2:
                msg: str = f"获取历史数据失败，状态码：{resp.status_code}，信息：{resp.text}"
                self.gateway.write_log(msg)
                break
            else:
                data: dict = resp.json()
                if not data:
                    msg: str = f"获取历史数据为空，开始时间：{start_time}"
                    self.gateway.write_log(msg)
                    break

                buf: List[BarData] = []

                for row in data:
                    bar: BarData = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=generate_datetime(row[0]),
                        interval=req.interval,
                        volume=float(row[5]),
                        turnover=float(row[7]),
                        open_price=float(row[1]),
                        high_price=float(row[2]),
                        low_price=float(row[3]),
                        close_price=float(row[4]),
                        gateway_name=self.gateway_name
                    )
                    buf.append(bar)

                begin: datetime = buf[0].datetime
                end: datetime = buf[-1].datetime

                history.extend(buf)
                msg: str = f"获取历史数据成功，{req.symbol} - {req.interval.value}，{begin} - {end}"
                self.gateway.write_log(msg)

                # 如果收到了最后一批数据则终止循环
                if len(data) < limit:
                    break

                # 更新开始时间
                start_dt = bar.datetime + TIMEDELTA_MAP[req.interval]
                start_time = int(datetime.timestamp(start_dt))

        return history


class BinanceUsdtTradeWebsocketApi(WebsocketClient):
    """币安正向合约的交易Websocket API"""

    def __init__(self, gateway: BinanceUsdtGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: BinanceUsdtGateway = gateway
        self.gateway_name: str = gateway.gateway_name

    def connect(self, url: str, proxy_host: str, proxy_port: int) -> None:
        """连接Websocket交易频道"""
        self.init(url, proxy_host, proxy_port)
        self.start()

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("交易Websocket API连接成功")

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        if packet["e"] == "ACCOUNT_UPDATE":
            self.on_account(packet)
        elif packet["e"] == "ORDER_TRADE_UPDATE":
            self.on_order(packet)

    def on_account(self, packet: dict) -> None:
        """资金更新推送"""
        for acc_data in packet["a"]["B"]:
            account: AccountData = AccountData(
                accountid=acc_data["a"],
                balance=float(acc_data["wb"]),
                frozen=float(acc_data["wb"]) - float(acc_data["cw"]),
                gateway_name=self.gateway_name
            )

            if account.balance:
                self.gateway.on_account(account)

        for pos_data in packet["a"]["P"]:
            volume = pos_data["pa"]
            if '.' in volume:
                volume = float(volume)
            else:
                volume = int(volume)

            position: PositionData = PositionData(
                symbol=pos_data["s"],
                exchange=Exchange.BINANCE,
                direction=DIRECTION_BINANCES2VT[pos_data["ps"]],
                volume=volume,
                price=float(pos_data["ep"]),
                pnl=float(pos_data["up"]),
                gateway_name=self.gateway_name,
            )
            self.gateway.on_position(position)

    def on_order(self, packet: dict) -> None:
        """委托更新推送"""
        ord_data: dict = packet["o"]
        key: Tuple[str, str] = (ord_data["o"], ord_data["f"])
        order_type: OrderType = ORDERTYPE_BINANCES2VT.get(key, None)
        if not order_type:
            return

        order: OrderData = OrderData(
            symbol=ord_data["s"],
            exchange=Exchange.BINANCE,
            orderid=str(ord_data["c"]),
            type=order_type,
            direction=DIRECTION_BINANCES2VT[ord_data["ps"]],
            offset=OFFSET_BINANCES2VT[(DIRECTION_BINANCES2VT[ord_data["ps"]], ord_data["S"])],
            price=float(ord_data["p"]),
            avg_price=float(ord_data["ap"]),
            volume=float(ord_data["q"]),
            traded=float(ord_data["z"]),
            profit=float(ord_data["rp"]),
            status=STATUS_BINANCES2VT[ord_data["X"]],
            datetime=generate_datetime(packet["E"]),
            gateway_name=self.gateway_name
        )

        self.gateway.on_order(order)

        # 将成交数量四舍五入到正确精度
        trade_volume: float = float(ord_data["l"])
        contract: ContractData = symbol_contract_map.get(order.symbol, None)
        if contract:
            trade_volume = round_to(trade_volume, contract.min_volume)

        if not trade_volume:
            return

        trade: TradeData = TradeData(
            symbol=order.symbol,
            exchange=order.exchange,
            orderid=order.orderid,
            tradeid=ord_data["t"],
            direction=order.direction,
            offset=order.offset,
            price=float(ord_data["L"]),
            volume=trade_volume,
            datetime=generate_datetime(ord_data["T"]),
            gateway_name=self.gateway_name,
        )
        self.gateway.on_trade(trade)


class BinanceUsdtDataWebsocketApi(WebsocketClient):
    """币安正向合约的行情Websocket API"""

    def __init__(self, gateway: BinanceUsdtGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: BinanceUsdtGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.ticks: Dict[str, TickData] = {}
        self.reqid: int = 0

    def connect(
        self,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """连接Websocket行情频道"""
        if server == "REAL":
            self.init(F_WEBSOCKET_DATA_HOST, proxy_host, proxy_port)
        else:
            self.init(F_TESTNET_WEBSOCKET_DATA_HOST, proxy_host, proxy_port)

        self.start()

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("行情Websocket API连接成功")

        # 重新订阅行情
        if self.ticks:
            channels = []
            for symbol in self.ticks.keys():
                channels.append(f"{symbol}@ticker")
                channels.append(f"{symbol}@depth20")

            req: dict = {
                "method": "SUBSCRIBE",
                "params": channels,
                "id": self.reqid
            }
            self.send_packet(req)

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        if req.symbol in self.ticks:
            return

        if req.symbol not in symbol_contract_map:
            self.gateway.write_log(f"找不到该合约代码{req.symbol}")
            return

        self.reqid += 1

        # 创建TICK对象
        tick: TickData = TickData(
            symbol=req.symbol,
            name=symbol_contract_map[req.symbol].name,
            exchange=Exchange.BINANCE,
            datetime=datetime.now(CHINA_TZ),
            gateway_name=self.gateway_name,
        )
        self.ticks[req.symbol.lower()] = tick

        channels = [
            f"{req.symbol.lower()}@ticker",
            f"{req.symbol.lower()}@depth20"
        ]

        req: dict = {
            "method": "SUBSCRIBE",
            "params": channels,
            "id": self.reqid
        }
        self.send_packet(req)

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        stream: str = packet.get("stream", None)

        if not stream:
            return

        data: dict = packet["data"]

        symbol, channel = stream.split("@")
        tick: TickData = self.ticks[symbol]

        if channel == "ticker":
            tick.volume = float(data['v'])
            tick.turnover = float(data['q'])
            tick.open_price = float(data['o'])
            tick.high_price = float(data['h'])
            tick.low_price = float(data['l'])
            tick.last_price = float(data['c'])
            tick.last_volume = float(data['Q'])
            tick.datetime = generate_datetime(float(data['E']))
        else:
            tick.depth_datetime = generate_datetime(float(data['E']))
            bids: list = data["b"]
            tick.bid_prices = []
            tick.bid_volumes = []
            for n in range(min(20, len(bids))):
                price, volume = bids[n]
                tick.bid_prices.append(float(price))
                tick.bid_volumes.append(float(volume))

            asks: list = data["a"]
            tick.ask_prices = []
            tick.ask_volumes = []
            for n in range(min(20, len(asks))):
                price, volume = asks[n]
                tick.ask_prices.append(float(price))
                tick.ask_volumes.append(float(volume))

        if tick.last_price:
            tick.localtime = datetime.now()
            self.gateway.on_tick(copy(tick))


def generate_datetime(timestamp: float) -> datetime:
    """生成时间"""
    dt: datetime = datetime.fromtimestamp(timestamp / 1000)
    dt: datetime = CHINA_TZ.localize(dt)
    return dt
