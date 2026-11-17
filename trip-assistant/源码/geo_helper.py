# -*- coding: utf-8 -*-
"""
geo_helper.py — 地点定位与出发时间估算
能力：
- 设备当前位置（CoreLocation，需 NSLocationWhenInUseUsageDescription）
- 地址→经纬度（CLGeocoder）
- 出行耗时估算（MapKit MKDirections；失败则退化按直线距离/平均车速估算并如实标注）
- 提醒时刻 = 开始时间 − 出行耗时 − 缓冲分钟
所有异步回调经主线程 CFRunLoop 泵同步等待（与 calkit 同一模式）。
"""
from __future__ import annotations

import datetime as dt
import math

import Foundation

# 完全地址启发式：命中即尝试地理编码
_ADDR_HINTS = (
    "路", "街", "道", "巷", "弄", "号", "幢", "栋", "座", "楼", "室", "层",
    "大厦", "广场", "中心", "校区", "学院", "大学", "馆", "厅", "店", "园",
    "村", "镇", "县", "区", "市", "院", "站", "机场", "医院", "酒店", "宾馆",
)


def looks_like_address(text: str) -> bool:
    """判断是否像完全地址（含通名且较具体）。"""
    t = text.strip()
    if len(t) < 4:
        return False
    return any(h in t for h in _ADDR_HINTS)


def _wait(result: dict, timeout: float = 30.0) -> dict:
    rl = Foundation.NSRunLoop.currentRunLoop()
    deadline = dt.datetime.now() + dt.timedelta(seconds=timeout)
    while not result.get("_done"):
        if not rl.runMode_beforeDate_(
            Foundation.NSDefaultRunLoopMode,
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05),
        ):
            break
        if dt.datetime.now() > deadline:
            result["_timeout"] = True
            break
    return result


def _coord(obj) -> tuple[float, float] | None:
    """取 (lat, lon)。CLPlacemark 走 location().coordinate；MKPlacemark/CLLocation 走 coordinate()。"""
    try:
        c = None
        try:
            loc = obj.location()
            if loc is not None:
                c = loc.coordinate()
        except Exception:
            c = None
        if c is None:
            c = obj.coordinate()
        lat = getattr(c, "latitude", None)
        lon = getattr(c, "longitude", None)
        if lat is None:
            lat, lon = c[0], c[1]
        return float(lat), float(lon)
    except Exception:
        return None


# ---------------- 设备定位 ----------------

def get_current_location(timeout: float = 20.0) -> dict:
    """返回 {lat, lon, desc} 或 {error}。需定位授权（首次触发系统弹窗）。"""
    try:
        import CoreLocation
    except Exception as e:
        return {"error": f"CoreLocation 不可用：{e}"}

    result = {}

    class _Del(CoreLocation.NSObject):
        def locationManager_didUpdateLocations_(self, mgr, locations):
            loc = locations[-1]
            c = _coord(loc)
            if c:
                result.update(_done=True, lat=c[0], lon=c[1])
            else:
                result.update(_done=True, error="定位返回无效坐标")

        def locationManager_didFailWithError_(self, mgr, error):
            result.update(_done=True, error=f"定位失败：{error.localizedDescription()}")

        def locationManagerDidChangeAuthorization_(self, mgr):
            st = mgr.authorizationStatus()
            # 0 notdet 1 restricted 2 denied 3 authorized(always) 4 authorized(in-use)
            if st in (2, 1):
                result.update(_done=True, error="定位权限被拒绝，可在 系统设置 → 隐私与安全性 → 定位服务 中开启")

    del_obj = _Del.alloc().init()
    mgr = CoreLocation.CLLocationManager.alloc().init()
    mgr.setDelegate_(del_obj)
    mgr.setDesiredAccuracy_(100.0)  # 出行估算用，不需米级精度

    st = mgr.authorizationStatus()
    if st == 0:
        mgr.requestWhenInUseAuthorization()

    mgr.startUpdatingLocation()
    _wait(result, timeout)
    mgr.stopUpdatingLocation()
    if result.get("_timeout"):
        return {"error": "定位超时（请检查定位服务是否开启）"}
    if "error" in result and "lat" not in result:
        return {"error": result["error"]}
    if "lat" not in result:
        return {"error": "未获得定位"}
    return {
        "lat": result["lat"],
        "lon": result["lon"],
        "desc": f"{result['lat']:.5f}, {result['lon']:.5f}",
    }


# ---------------- 地理编码 ----------------

def city_of(loc: dict, timeout: float = 12.0) -> dict:
    """坐标 → {city} 反查（用于给短地址补城市前缀）。"""
    try:
        import CoreLocation
    except Exception as e:
        return {"error": f"CoreLocation 不可用：{e}"}
    result = {}
    geo = CoreLocation.CLGeocoder.alloc().init()
    cl = CoreLocation.CLLocation.alloc().initWithCoordinate_altitude_horizontalAccuracy_verticalAccuracy_timestamp_(
        (loc["lat"], loc["lon"]), 0, 100, 100, Foundation.NSDate.date()
    )

    def cb(placemarks, error):
        if error is not None or not placemarks:
            result.update(_done=True, error="反查城市失败")
            return
        pm = placemarks[0]
        result.update(_done=True, city=str(pm.locality()) if pm.locality() else "")

    geo.reverseGeocodeLocation_completionHandler_(cl, cb)
    _wait(result, timeout)
    return result


def geocode(address: str, timeout: float = 15.0, near: tuple[float, float] | None = None) -> dict:
    """地址 → {lat, lon, name, desc} 或 {error}。

    near=(lat,lon)：地址较短可能有多个城市同名匹配时，选距 near 最近的结果。
    """
    try:
        import CoreLocation
    except Exception as e:
        return {"error": f"CoreLocation 不可用：{e}"}

    result = {}
    geo = CoreLocation.CLGeocoder.alloc().init()

    def cb(placemarks, error):
        if error is not None:
            result.update(_done=True, error=f"地理编码失败：{error.localizedDescription()}")
            return
        if not placemarks:
            result.update(_done=True, error="未解析到该地址的位置")
            return
        cands = []
        for pm in placemarks:
            c = _coord(pm)
            if not c:
                continue
            name = str(pm.name() or address)
            city = str(pm.locality()) if pm.locality() else ""
            cands.append({"lat": c[0], "lon": c[1], "name": name,
                          "desc": "、".join(x for x in (city, name) if x)})
        if not cands:
            result.update(_done=True, error="解析结果缺少坐标")
            return
        if near and len(cands) > 1:
            cands.sort(key=lambda c0: _haversine_km(near[0], near[1], c0["lat"], c0["lon"]))
        result.update(_done=True, **cands[0])

    geo.geocodeAddressString_completionHandler_(address, cb)
    _wait(result, timeout)
    if result.get("_timeout"):
        return {"error": "地理编码超时（需网络）"}
    if "error" in result and "lat" not in result:
        return result
    return result


# ---------------- 出行耗时 ----------------

def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def estimate_travel(from_loc: dict, to_loc: dict) -> dict:
    """估算出发地→目的地耗时。返回 {seconds, method, desc} 或 {error}。

    method: mapkit=导航实际估算（可信）；straight=直线距离按均速 30km/h 粗估（须告知用户）。
    """
    if "lat" not in from_loc or "lat" not in to_loc:
        return {"error": "缺少起点或终点坐标"}
    try:
        import MapKit
    except Exception as e:
        return {"error": f"MapKit 不可用：{e}"}

    result = {}
    src = MapKit.MKMapItem.mapItemForCurrentLocation()
    plc = MapKit.MKPlacemark.alloc().initWithCoordinate_addressDictionary_(
        (to_loc["lat"], to_loc["lon"]), None
    )
    dst = MapKit.MKMapItem.alloc().initWithPlacemark_(plc)

    req = MapKit.MKDirectionsRequest.alloc().init()
    req.setSource_(src)
    req.setDestination_(dst)
    req.setTransportType_(MapKit.MKDirectionsTransportTypeAutomobile)

    directions = MapKit.MKDirections.alloc().initWithRequest_(req)

    def cb(response, error):
        if error is not None or response is None:
            result["_mkfail"] = str(error.localizedDescription()) if error else "无响应"
            result["_done"] = True
            return
        result.update(
            _done=True,
            seconds=response.expectedTravelTime(),
            method="mapkit",
        )

    directions.calculateETAWithCompletionHandler_(cb)
    _wait(result, 25.0)

    if "seconds" in result:
        return result
    # 退化：直线距离粗估（均速 30 km/h，含 20% 绕行系数）
    km = _haversine_km(from_loc["lat"], from_loc["lon"], to_loc["lat"], to_loc["lon"]) * 1.2
    seconds = km / 30.0 * 3600
    return {
        "seconds": seconds,
        "method": "straight",
        "desc": f"导航估算不可用（{result.get('_mkfail', '原因未知')}），按直线距离 {km:.1f} km、均速 30 km/h 粗估",
    }


def suggest_alarm(event_start: dt.datetime, travel_seconds: float, buffer_min: int = 10) -> dt.datetime:
    """提醒时刻 = 开始 − 出行耗时 − 缓冲；不早于当前时间。"""
    alarm = event_start - dt.timedelta(seconds=travel_seconds) - dt.timedelta(minutes=buffer_min)
    now = dt.datetime.now()
    if alarm < now:
        alarm = now + dt.timedelta(minutes=1)
    return alarm
