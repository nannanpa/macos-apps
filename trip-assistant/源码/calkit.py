# -*- coding: utf-8 -*-
"""
calkit.py — EventKit 日历读写层（PyObjC）
设计要点：
- 日期一律用 NSDate（由 python datetime 的 timestamp() 直接构造），
  不存在任何 AppleScript 字符串拼日期路径（根治 D2/B1/B2/B3）。
- 日历分类实时读取（继承 B5）。
- 提供读回/删除能力（补 D6），供验收与测试数据清理。
- 所有 ObjC 异步回调经主线程 CFRunLoop 泵同步等待。
"""
from __future__ import annotations

import datetime as _dt
import threading

import Foundation
from EventKit import (
    EKAlarm,
    EKEvent,
    EKEventStore,
    EKEntityTypeEvent,  # noqa: F401  (常量=0，EventKit 模块提供)
)

EntityTypeEvent = 0

_AUTH_NAME = {0: "未决定", 1: "受限", 2: "拒绝", 3: "已授权(完整)", 4: "已授权(仅写入)"}


class CalendarError(Exception):
    pass


def _nsdate(dt: _dt.datetime) -> Foundation.NSDate:
    """本地时间 datetime → NSDate。唯一日期构造通道，全程数字时间戳，无字符串。"""
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _from_nsdate(ns) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ns.timeIntervalSince1970())


def _wait(result: dict, timeout: float = 60.0) -> dict:
    """泵主 runloop，直到 result 里出现 '_done' 或超时。"""
    rl = Foundation.NSRunLoop.currentRunLoop()
    deadline = _dt.datetime.now() + _dt.timedelta(seconds=timeout)
    while not result.get("_done"):
        if not rl.runMode_beforeDate_(
            Foundation.NSDefaultRunLoopMode,
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05),
        ):
            break  # runloop 退出
        if _dt.datetime.now() > deadline:
            result["_timeout"] = True
            break
    return result


class CalendarKit:
    def __init__(self):
        self.store = EKEventStore.alloc().init()

    # ---------- 授权 ----------
    def auth_status(self) -> int:
        from EventKit import EKEventStore as _S
        return _S.authorizationStatusForEntityType_(EntityTypeEvent)

    def auth_status_name(self) -> str:
        return _AUTH_NAME.get(self.auth_status(), f"未知({self.auth_status()})")

    def request_access(self, timeout: float = 120.0) -> bool:
        """请求完整访问权限（首次会触发系统弹窗）。同步等待。"""
        st = self.auth_status()
        if st == 3:
            return True
        if st == 2:
            raise CalendarError(
                "日历访问被拒绝。请到 系统设置 → 隐私与安全性 → 日历 中允许本应用。"
            )
        result = {}
        self.store.requestFullAccessToEventsWithCompletion_(
            lambda granted, error: result.update(
                _done=True, granted=bool(granted), error=str(error) if error else None
            )
        )
        _wait(result, timeout)
        if result.get("_timeout"):
            raise CalendarError("等待日历授权超时。")
        if not result.get("granted"):
            raise CalendarError(
                f"日历授权未通过（{result.get('error') or '无错误信息'}）。"
                f"当前状态：{self.auth_status_name()}"
            )
        return True

    # ---------- 日历分类 ----------
    def list_calendars(self) -> list[str]:
        """实时读取日历分类名（继承 B5：不硬编码）。"""
        cals = self.store.calendarsForEntityType_(EntityTypeEvent)
        return [str(c.title()) for c in cals]

    def _calendar_by_name(self, name: str):
        cals = self.store.calendarsForEntityType_(EntityTypeEvent)
        for c in cals:
            if str(c.title()) == name:
                return c
        raise CalendarError(f"未找到日历「{name}」。现有分类：{self.list_calendars()}")

    # ---------- 写入 ----------
    def add_event(
        self,
        calendar_name: str,
        title: str,
        start: _dt.datetime,
        end: _dt.datetime,
        location: str = "",
        notes: str = "",
        alarm_at: _dt.datetime | None = None,
    ) -> str:
        """写入事件。日期用 NSDate 直接构造，不经字符串拼接。返回事件标识。"""
        if end <= start:
            raise CalendarError("结束时间必须晚于开始时间。")
        self.request_access()
        ev = EKEvent.eventWithEventStore_(self.store)
        ev.setTitle_(title)
        ev.setStartDate_(_nsdate(start))
        ev.setEndDate_(_nsdate(end))
        ev.setCalendar_(self._calendar_by_name(calendar_name))
        if location:
            ev.setLocation_(location)
        if notes:
            ev.setNotes_(notes)
        if alarm_at is not None:
            ev.addAlarm_(EKAlarm.alarmWithAbsoluteDate_(_nsdate(alarm_at)))
        ok, error = self.store.saveEvent_span_error_(ev, 0, None)
        if not ok:
            raise CalendarError(f"写入日历失败：{error}")
        return str(ev.eventIdentifier() or ev.calendarItemIdentifier())

    # ---------- 读回（终点验收通道） ----------
    def find_events_by_title(
        self, title: str, days_back: int = 30, days_ahead: int = 90
    ) -> list[dict]:
        """按标题精确匹配读回事件（真实日历存储值，非脚本内部变量）。"""
        self.request_access()
        now = _dt.datetime.now()
        start = Foundation.NSDate.dateWithTimeIntervalSince1970_(
            (now - _dt.timedelta(days=days_back)).timestamp()
        )
        end = Foundation.NSDate.dateWithTimeIntervalSince1970_(
            (now + _dt.timedelta(days=days_ahead)).timestamp()
        )
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            start, end, None
        )
        events = self.store.eventsMatchingPredicate_(pred) or []
        out = []
        for ev in events:
            if str(ev.title()) == title:
                out.append(
                    {
                        "identifier": str(ev.calendarItemIdentifier()),
                        "title": str(ev.title()),
                        "calendar": str(ev.calendar().title()),
                        "start": _from_nsdate(ev.startDate()),
                        "end": _from_nsdate(ev.endDate()),
                        "location": str(ev.location()) if ev.location() else "",
                        "notes": str(ev.notes()) if ev.notes() else "",
                        "has_alarm": bool(ev.alarms() and len(ev.alarms()) > 0),
                        "alarm_time": (
                            _from_nsdate(ev.alarms()[0].absoluteDate())
                            if ev.alarms() and ev.alarms()[0].absoluteDate()
                            else None
                        ),
                    }
                )
        out.sort(key=lambda d: d["start"])
        return out

    # ---------- 删除（测试数据清理用，仅限指定标识/标题） ----------
    def delete_event(self, identifier: str) -> bool:
        """按 calendarItemIdentifier 删除单个事件。"""
        self.request_access()
        ev = self.store.calendarItemWithIdentifier_(identifier)
        if ev is None:
            return False
        ok, error = self.store.removeEvent_span_error_(ev, 0, None)
        if not ok:
            raise CalendarError(f"删除失败：{error}")
        return True
