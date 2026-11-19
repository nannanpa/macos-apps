#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行程助手 — macOS 日历行程添加工具
- 写入层：EventKit（PyObjC），日期用 NSDate 数字构造，无字符串拼日期
- 交互层：Tkinter GUI（模糊输入 → 解析回显确认 → 写入），另有 CLI 模式供验收
- 附加：中文模糊解析、日历分类初判、设备定位＋出发时间提醒、按标题读回/删除

历史教训（继承自 2026-03-26 版本，勿重蹈）：
- B1/B2：中文 macOS 下 AppleScript 英文月份串/ISO 串构造日期均失败 → 本版不拼日期串
- B4：Calendar 未激活报 -600 → 本版走 EventKit，不依赖 Calendar.app 前台
- D2：跨天结束时间偏移 → NSDate 数字构造，验收必须从 Calendar 读回（见 verify 流程）
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import re
import sys

from calkit import CalendarKit, CalendarError
from fuzzy_parser import (parse as fuzzy_parse, parse_multi as fuzzy_parse_multi,
                          guess_calendar, format_parsed, recur_hint_label)
import geo_helper


TEST_PREFIX = "WB验收-可删"

# 作息对照表窗口单例引用（防止从菜单重复打开多个窗口）
_periods_win = None


# ==================== 写入台账（「日程管理→一键读取」的依据） ====================
# EventKit 无法区分事件出自哪个 App，故每次经本工具写入成功后把事件标识记入本地台账；
# 管理窗口只按台账读取，口径=「经本 App 填报的日程」。

def _ledger_path() -> str:
    d = os.path.expanduser("~/Library/Application Support/CalendarAssistant")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "written.json")


def _ledger_load() -> list[dict]:
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _ledger_save(items: list[dict]) -> None:
    with open(_ledger_path(), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def _ledger_add(ident: str, title: str) -> None:
    items = _ledger_load()
    if not any(x.get("id") == ident for x in items):
        items.append({"id": ident, "title": title,
                      "at": dt.datetime.now().isoformat(timespec="seconds")})
        _ledger_save(items)


def _ledger_remove(ident: str) -> None:
    items = [x for x in _ledger_load() if x.get("id") != ident]
    _ledger_save(items)


# ==================== 作息对照表（节次 ↔ 时刻，周期性课程解析用） ====================
# 定位：为「按节次排课」的课程（学校/家长/跟班进修）把节次换算成时刻；
# 直接写钟点的课（如培训兴趣班）无需本表。解析端只认配置过的节次，
# 查不到不改写不猜（宁可回退手动填写）。

def _periods_path() -> str:
    d = os.path.expanduser("~/Library/Application Support/CalendarAssistant")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "periods.json")


def _periods_load() -> dict:
    """返回 {"term_anchor": date|None, "periods": [{"name","start","end"}]}（文件缺失/损坏→全空）。"""
    try:
        with open(_periods_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"term_anchor": None, "periods": []}
    except Exception:
        return {"term_anchor": None, "periods": []}
    out = {"term_anchor": None, "periods": []}
    ta = data.get("term_anchor")
    if isinstance(ta, str) and ta:
        try:
            out["term_anchor"] = dt.datetime.strptime(ta, "%Y-%m-%d").date()
        except ValueError:
            pass
    if isinstance(data.get("periods"), list):
        out["periods"] = [x for x in data["periods"]
                          if isinstance(x, dict) and x.get("name") and x.get("start")]
    return out


def _periods_save(conf: dict) -> None:
    with open(_periods_path(), "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)


def _periods_conf() -> tuple[dict[int, tuple[str, str]], dt.date | None]:
    """解析用视图：({节次号: (开始,结束)}, 学期第一周一)。节次号取名称里的数字。"""
    conf = _periods_load()
    slots: dict[int, tuple[str, str]] = {}
    for x in conf["periods"]:
        m = re.search(r"\d{1,2}", str(x.get("name", "")))
        if not m:
            continue
        n = int(m.group())
        try:
            dt.datetime.strptime(str(x["start"]), "%H:%M")
            dt.datetime.strptime(str(x["end"]), "%H:%M")
        except (ValueError, KeyError):
            continue
        slots.setdefault(n, (str(x["start"]), str(x["end"])))
    return slots, conf["term_anchor"]


_PERIOD_TPL_COLLEGE = [
    ("第1节", "08:00", "08:45"), ("第2节", "08:55", "09:40"),
    ("第3节", "10:00", "10:45"), ("第4节", "10:55", "11:40"),
    ("第5节", "13:30", "14:15"), ("第6节", "14:25", "15:10"),
    ("第7节", "15:20", "16:05"), ("第8节", "16:15", "17:00"),
    ("第9节", "18:00", "18:45"), ("第10节", "18:55", "19:40"),
]
_PERIOD_TPL_SCHOOL = [
    ("第1节", "08:00", "08:40"), ("第2节", "08:50", "09:30"),
    ("第3节", "09:50", "10:30"), ("第4节", "10:40", "11:20"),
    ("第5节", "11:30", "12:10"), ("第6节", "14:00", "14:40"),
    ("第7节", "14:50", "15:30"), ("第8节", "15:40", "16:20"),
    ("第9节", "16:30", "17:10"), ("第10节", "17:20", "18:00"),
    ("第11节", "18:30", "19:10"), ("第12节", "19:20", "20:00"),
]


def _fmt_dt(d: dt.datetime | None) -> str:
    return f"{d:%Y-%m-%d %H:%M}" if d else ""


def _parse_dt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")


# ==================== 出行/提醒辅助 ====================

def plan_alarm(location: str, start: dt.datetime, buffer_min: int) -> dict:
    """设备位置 → 目的地编码 → 出行耗时 → 提醒时刻。返回供确认展示的明细。"""
    plan: dict = {"alarm": None, "lines": [], "ok": False}
    cur = geo_helper.get_current_location()
    if "error" in cur:
        plan["lines"].append(f"⚠ 未取得设备位置：{cur['error']}；提醒未按出发时间设定")
        return plan
    plan["lines"].append(f"设备位置：{cur['desc']}")
    geo = geo_helper.geocode(location, near=(cur["lat"], cur["lon"]))
    # 短地址补全：解析点离设备超过 100 km 且地址未含省市区字样时，按设备所在城市重试
    if "lat" in geo and location and not any(k in location for k in ("省", "市", "区", "县")):
        import math
        d_km = geo_helper._haversine_km(cur["lat"], cur["lon"], geo["lat"], geo["lon"])
        if d_km > 100:
            city = geo_helper.city_of(cur)
            if "city" in city and city["city"] and not location.startswith(city["city"]):
                geo2 = geo_helper.geocode(city["city"] + location, near=(cur["lat"], cur["lon"]))
                if "lat" in geo2:
                    geo = geo2
                    plan["lines"].append(f"（短地址已按所在城市「{city['city']}」补全）")
    if "error" in geo:
        plan["lines"].append(f"⚠ 地址未能解析：{geo['error']}；提醒未按出发时间设定")
        return plan
    plan["lines"].append(f"目的地解析：{geo.get('desc', location)}（{geo['lat']:.5f}, {geo['lon']:.5f}）")
    est = geo_helper.estimate_travel(cur, geo)
    if "error" in est:
        plan["lines"].append(f"⚠ 耗时估算失败：{est['error']}；提醒未按出发时间设定")
        return plan
    mins = round(est["seconds"] / 60)
    if est.get("method") == "straight":
        plan["lines"].append(f"⚠ {est.get('desc', '')}")
    plan["lines"].append(f"预计出行耗时：约 {mins} 分钟（驾；实际以当时路况为准）")
    alarm = geo_helper.suggest_alarm(start, est["seconds"], buffer_min)
    plan["alarm"] = alarm
    plan["ok"] = True
    plan["lines"].append(
        f"提醒时刻 = 开始 {_fmt_dt(start)} − {mins} 分钟 − 缓冲 {buffer_min} 分钟 = {_fmt_dt(alarm)}"
    )
    return plan


# ==================== GUI ====================

def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    kit = CalendarKit()

    root = tk.Tk()
    root.title("")  # 标题栏不显示文字（对齐原生应用如日历/备忘录的隐藏标题做法）
    root.geometry("900x860")
    root.minsize(800, 720)
    root.configure(bg="#f5f6f8")

    # 官方页面：GitHub 仓库子目录（作者信息、使用说明与发布的入口）
    official_url = "https://github.com/nannanpa/macos-apps/releases/latest"
    readme_url = "https://github.com/nannanpa/macos-apps/tree/main/calendar-assistant"

    import webbrowser

    # 帮助文档：优先打开包内自带的离线帮助页（--cli/开发环境退回源码目录）
    def _help_html_path() -> str | None:
        import os
        for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(__file__))):
            if not base:
                continue
            p = os.path.join(base, "帮助.html")
            if os.path.exists(p):
                # PyInstaller 的 _MEIPASS 在 Frameworks 下放的是指向 Resources 的符号链接；
                # realpath 解析成真实文件，避免 open 命令对符号链接的行为不确定
                return os.path.realpath(p)
        return None

    def open_help():
        import urllib.parse
        p = _help_html_path()
        if p:
            # file:// URL 必须对路径做百分号编码，否则中文路径会被 macOS 的 open
            # 当成无法解析的 URL，回退成「在 Finder 中显示该文件」（实测跳 Frameworks 目录）
            webbrowser.open("file://" + urllib.parse.quote(p))
        else:  # 兜底：包内没带帮助页时打开在线版
            webbrowser.open(readme_url)

    # 菜单栏顺序 = 注册顺序（本机实测 name="help" 并未固定到最右）：
    # 先注册「作息对照」，再注册「帮助」，显示才是「作息对照 → 帮助」。
    # 注意 open_periods_dialog 定义在下方，此处用 lambda 延迟引用避免 NameError。
    menubar = tk.Menu(root)
    setmenu = tk.Menu(menubar, tearoff=0)
    setmenu.add_command(label="作息对照表…", command=lambda: open_periods_dialog())
    menubar.add_cascade(label="作息对照", menu=setmenu)
    # 自定义 Help 菜单（name="help" 成为系统帮助菜单）。注意 macOS 会在帮助菜单顶部
    # 固定放一个「行程助手帮助」项：不接管时点击它弹「Help isn't available」（无 Help Book），
    # 必须注册 tk::mac::ShowHelp 回调把这一项接到本地帮助页上。
    # 本地帮助页就由这个系统项承担，不再重复加「使用说明（本机）」——两者本就是同一页。
    help_menu = tk.Menu(menubar, name="help", tearoff=0)
    help_menu.add_command(label="使用说明（在线）",
                          command=lambda: webbrowser.open(readme_url))
    help_menu.add_command(label="官方页面（发布与下载）",
                          command=lambda: webbrowser.open(official_url))
    menubar.add_cascade(label="帮助", menu=help_menu)
    root.config(menu=menubar)
    root.createcommand("tk::mac::ShowHelp", open_help)

    def _pick_date(parent, initial: str, on_pick) -> None:
        """轻量月历弹窗：点日期→回调 on_pick('YYYY-MM-DD')。用标准库 calendar，无新依赖、单例。"""
        if getattr(parent, "_date_ref", None) is not None and parent._date_ref.winfo_exists():
            parent._date_ref.lift()
            parent._date_ref.focus_force()
            return
        base = dt.date.today()
        if initial:
            try:
                base = dt.datetime.strptime(initial, "%Y-%m-%d").date()
            except ValueError:
                pass
        state = {"y": base.year, "m": base.month}
        win = tk.Toplevel(parent)
        parent._date_ref = win
        win.title("选择日期")
        win.transient(parent)
        win.resizable(False, False)
        # 居中于父窗口，避免落在屏幕外
        try:
            px, py = parent.winfo_x(), parent.winfo_y()
            win.geometry(f"+{max(0, px + 80)}+{max(0, py + 120)}")
        except Exception:
            pass
        win.configure(bg="#f5f6f8")
        win.config(menu=parent.nametowidget(parent.cget("menu")))  # 沿用主菜单，避免英文菜单闪现

        def close():
            if getattr(parent, "_date_ref", None) is win:
                parent._date_ref = None
            win.destroy()

        head = tk.Frame(win, bg="#f5f6f8")
        head.pack(fill="x", padx=10, pady=(10, 4))
        lbl = tk.Label(head, text="", font=("PingFang SC", 13, "bold"), bg="#f5f6f8", width=14)
        lbl.pack(side="left", expand=True)
        tk.Button(head, text="‹", font=("PingFang SC", 14), width=2, relief="flat",
                  command=lambda: shift(-1)).pack(side="left")
        tk.Button(head, text="›", font=("PingFang SC", 14), width=2, relief="flat",
                  command=lambda: shift(1)).pack(side="left")

        grid = tk.Frame(win, bg="#f5f6f8")
        grid.pack(padx=10, pady=4)
        for i, w in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            tk.Label(grid, text=w, font=("PingFang SC", 11), bg="#f5f6f8",
                     fg="#6b7280", width=4).grid(row=0, column=i)

        def render():
            if not win.winfo_exists():
                return
            y, m = state["y"], state["m"]
            lbl.config(text=f"{y}年{m}月")
            for w in grid.grid_slaves():
                if int(w.grid_info().get("row", 0)) >= 1:
                    w.destroy()
            ndays = calendar.monthrange(y, m)[1]
            pos = calendar.monthcalendar(y, m)
            for day in range(1, ndays + 1):
                # 按 day 在月历中的位置定位（周一为首列）
                r, c = None, None
                for ri, wk in enumerate(pos, start=1):
                    if day in wk:
                        r, c = ri, wk.index(day)
                        break
                d = dt.date(y, m, day)
                is_today = (d == dt.date.today())
                tk.Button(grid, text=str(day), width=4,
                          font=("PingFang SC", 11, "bold" if is_today else "normal"),
                          fg="#2563eb" if is_today else "#1f2937", relief="flat",
                          command=lambda dd=d: (on_pick(dd.strftime("%Y-%m-%d")), close())
                          ).grid(row=r, column=c, padx=1, pady=1)

        def shift(delta):
            m = state["m"] + delta
            y = state["y"]
            if m < 1:
                m, y = 12, y - 1
            elif m > 12:
                m, y = 1, y + 1
            state["y"], state["m"] = y, m
            render()

        foot = tk.Frame(win, bg="#f5f6f8")
        foot.pack(fill="x", padx=10, pady=(4, 10))
        tk.Button(foot, text="今天", font=("PingFang SC", 11), relief="flat",
                  command=lambda: (on_pick(dt.date.today().strftime("%Y-%m-%d")), close())
                  ).pack(side="left", padx=4)
        tk.Button(foot, text="清空", font=("PingFang SC", 11), relief="flat",
                  command=lambda: (on_pick(""), close())).pack(side="left", padx=4)
        tk.Button(foot, text="取消", font=("PingFang SC", 11), relief="flat",
                  command=close).pack(side="right", padx=4)
        win.protocol("WM_DELETE_WINDOW", close)
        render()

    # ---------- 作息对照表设置（节次 ↔ 时刻，周期性课程解析用） ----------
    def open_periods_dialog():
        # 单例：已打开则抬到前台，不再开第二个窗口
        global _periods_win
        if _periods_win is not None and _periods_win.winfo_exists():
            _periods_win.lift()
            _periods_win.focus_force()
            return

        conf = _periods_load()
        win = tk.Toplevel(root)
        _periods_win = win
        win.title("作息对照表（节次 ↔ 时刻）")
        # 固定高度：10/12节都能容纳，切换模板按钮不跳；超出靠输入区滚动。
        # 显式居中于主窗口，避免 macOS 记住坏坐标把对话框摆到屏幕外。
        wx = root.winfo_x() + (root.winfo_width() - 600) // 2
        wy = root.winfo_y() + 60
        win.geometry(f"600x580+{max(0, wx)}+{max(0, wy)}")
        win.transient(root)
        win.configure(bg=BG)
        # 关键：对话框也挂主菜单。否则它拿到焦点时 macOS 会显示默认英文菜单
        # （File/Edit/Window/Help），看起来像「菜单变了」。
        win.config(menu=menubar)
        win.protocol("WM_DELETE_WINDOW", lambda: _close_periods(win))

        def _close_periods(w):
            global _periods_win
            if _periods_win is w:
                _periods_win = None
            w.destroy()

        # 提示（wraplength 自动换行，防止右侧溢出被裁）
        tk.Label(win, text="本表把节次（如「第3-4节」）换算成具体时刻，供按节次排课的课程使用；直接写时刻的课不必设置。",
                 font=FONT_SM, bg=BG, fg=MUTED, wraplength=560, justify="left",
                 anchor="w").pack(fill="x", padx=14, pady=(12, 6))

        # 锚点行：紧贴提示下方、表格上方（用户要求的位置），带日历选择按钮
        anchf = tk.Frame(win, bg=BG)
        anchf.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(anchf, text="学期第一周周一（单/双周锚定用，可空）：", font=FONT_SM,
                 bg=BG, fg=MUTED).pack(side="left")
        ent_anchor = ttk.Entry(anchf, width=14, justify="center")
        ent_anchor.insert(0, conf["term_anchor"].strftime("%Y-%m-%d") if conf["term_anchor"] else "")
        ent_anchor.pack(side="left", padx=4)

        def _on_date_pick(d):
            ent_anchor.delete(0, "end")
            if d:
                ent_anchor.insert(0, d)

        tk.Button(anchf, text="日历选择", font=FONT_SM, relief="flat", fg="#2563eb",
                  activeforeground="#1d4ed8", bg=BG, activebackground="#eef2f7", bd=0,
                  command=lambda: _pick_date(win, ent_anchor.get().strip(), _on_date_pick)
                  ).pack(side="left", padx=2)

        # 输入区卡片：表头 + 可滚动的行区，整体用白卡包起来
        card = tk.Frame(win, bg=CARD, highlightbackground=LINE, highlightthickness=1, bd=0)
        card.pack(fill="both", expand=True, padx=14, pady=2)

        # 列宽常量：表头与输入框共用，确保逐列对齐（列可拉伸铺满卡片宽度）
        COL_W = (14, 10, 10)

        # 注意：变量名必须区别于 run_gui 主窗口的 canvas（同一作用域链，重名会误操作主窗口）
        dlg_canvas = tk.Canvas(card, bg=CARD, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(card, orient="vertical", command=dlg_canvas.yview)
        # 滚动条不预先占位：内容超出可视区才 pack，否则右侧不留浅灰槽
        dlg_canvas.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        inner = tk.Frame(dlg_canvas, bg=CARD)
        inner_id = dlg_canvas.create_window((0, 0), window=inner, anchor="nw")
        dlg_canvas.configure(yscrollcommand=sb.set)

        def _sync_sb():
            """行区内容高于可视区才显示滚动条；否则撤掉，避免右侧常驻浅灰槽。"""
            dlg_canvas.update_idletasks()
            need = inner.winfo_reqheight() > dlg_canvas.winfo_height()
            if need and not sb.winfo_ismapped():
                sb.pack(side="right", fill="y", padx=(0, 4), pady=6)
            elif not need and sb.winfo_ismapped():
                sb.pack_forget()

        def _on_canvas_config(e=None):
            # inner 宽度跟随 canvas，否则「删」按钮列会被裁掉
            dlg_canvas.itemconfigure(inner_id, width=e.width if e else dlg_canvas.winfo_width())
            _sync_sb()
        dlg_canvas.bind("<Configure>", _on_canvas_config)

        def _on_inner_config(_e=None):
            dlg_canvas.configure(scrollregion=dlg_canvas.bbox("all"))
            _on_canvas_config()
            _sync_sb()
        inner.bind("<Configure>", _on_inner_config)
        # 滚轮只在指针位于输入区时滚动，不劫持页面其它区域
        def _on_wheel(e):
            dlg_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        dlg_canvas.bind("<MouseWheel>", _on_wheel)
        inner.bind("<MouseWheel>", _on_wheel)

        # 表头与输入行共用同一个 grid：列宽由同一组列定义分配，天然逐列对齐。
        # 旧写法表头和输入框分属两个 Frame，字体宽度差异导致表头整体右偏。
        inner.columnconfigure(0, weight=1)
        for col, txt in enumerate(("节次名", "开始", "结束")):
            tk.Label(inner, text=txt, font=FONT_SM, bg=CARD, fg=MUTED,
                     anchor="center").grid(row=0, column=col, sticky="ew", padx=3, pady=(2, 6))
        # 第4列空表头占位（与「删」按钮列同宽），避免末列错位
        tk.Label(inner, text="", bg=CARD, width=3).grid(row=0, column=3, padx=3)
        for col in range(3):
            inner.columnconfigure(col, weight=1)

        rows: list[list[tk.StringVar]] = []
        row_widgets: dict[int, list] = {}
        FIRST_ROW = 1  # inner 内数据行起点（0=表头）

        def add_row(name="", start="", end=""):
            r = len(rows)
            vs = [tk.StringVar(value=name), tk.StringVar(value=start), tk.StringVar(value=end)]
            rows.append(vs)
            ws = []
            for col, v in enumerate(vs):
                ent = ttk.Entry(inner, textvariable=v, width=COL_W[col], justify="center")
                ent.grid(row=r + FIRST_ROW, column=col, sticky="ew", padx=3, pady=2)
                ws.append(ent)
            btn_del = tk.Button(inner, text="删", font=FONT_SM, relief="flat", fg="#dc2626",
                                bg=CARD, activebackground="#eef2f7", bd=0,
                                command=lambda: del_row(r))
            btn_del.grid(row=r + FIRST_ROW, column=3, sticky="w", padx=3)
            ws.append(btn_del)
            row_widgets[r] = ws

        def del_row(idx):
            if not (0 <= idx < len(rows)):
                return
            for wdg in row_widgets.pop(idx, []):
                wdg.destroy()
            rows[idx] = None  # 占位，保存时跳过空行

        for x in conf["periods"]:
            add_row(x.get("name", ""), x.get("start", ""), x.get("end", ""))
        if not rows:
            for x in _PERIOD_TPL_COLLEGE:
                add_row(*x)

        # 按钮行：固定贴窗口底部；不去 fill=x，让整行按内容宽度居中
        btnf = tk.Frame(win, bg=BG)
        btnf.pack(side="bottom", pady=(8, 12))

        def load_tpl(tpl):
            for r in list(row_widgets):
                for wdg in row_widgets.pop(r):
                    wdg.destroy()
            rows.clear()
            for x in tpl:
                add_row(*x)
            win.update_idletasks()
            _on_inner_config()

        FlatButton(btnf, "大学10节模板", lambda: load_tpl(_PERIOD_TPL_COLLEGE), kind="ghost").pack(side="left", padx=5)
        FlatButton(btnf, "中学12节模板", lambda: load_tpl(_PERIOD_TPL_SCHOOL), kind="ghost").pack(side="left", padx=5)
        FlatButton(btnf, "加一行", lambda: (add_row(), win.update_idletasks(), _on_inner_config()),
                   kind="ghost").pack(side="left", padx=5)

        def on_save():
            periods = []
            for vs in rows:
                if vs is None:
                    continue
                name = vs[0].get().strip()
                st, en = vs[1].get().strip(), vs[2].get().strip()
                if not name and not st:
                    continue
                if not name or not st or not en:
                    messagebox.showwarning("作息对照表", f"「{name or '（未命名）'}」行信息不全（节次名/开始/结束都要填）。", parent=win)
                    return
                for t in (st, en):
                    try:
                        dt.datetime.strptime(t, "%H:%M")
                    except ValueError:
                        messagebox.showwarning("作息对照表", f"「{name}」的时刻格式应为 HH:MM（如 08:00）。", parent=win)
                        return
                periods.append({"name": name, "start": st, "end": en})
            ta = ent_anchor.get().strip()
            if ta:
                try:
                    dt.datetime.strptime(ta, "%Y-%m-%d")
                except ValueError:
                    messagebox.showwarning("作息对照表", "学期第一周格式应为 YYYY-MM-DD。", parent=win)
                    return
            _periods_save({"term_anchor": ta or None, "periods": periods})
            _close_periods(win)
            set_status("作息对照表已保存，节次表述即可换算（如「明天第3-4节××课」）。")

        FlatButton(btnf, "保存", on_save, kind="primary").pack(side="left", padx=6)
        FlatButton(btnf, "取消", lambda: _close_periods(win), kind="ghost").pack(side="left", padx=5)
        win.update_idletasks()
        _on_inner_config()

    # 作息对照菜单已于菜单栏创建时注册（必须先于帮助，详见上方 menubar 处）。
    # 切勿用 insert_cascade(0)：索引0是 macOS 应用菜单位，占用会让菜单变英文默认项。

    # 视觉：贴近本机网页工作台的浅色卡片风格（浅灰底 / 白卡 / 细边框 / 蓝色主按钮）
    style = ttk.Style()
    style.configure(".", font=("PingFang SC", 13), background="#f5f6f8", foreground="#1f2937")
    style.configure("TEntry", fieldbackground="#ffffff")
    style.configure("TCombobox", fieldbackground="#ffffff")

    INK, MUTED, BLUE = "#1f2937", "#6b7280", "#2563eb"
    BG, CARD, LINE = "#f5f6f8", "#ffffff", "#e3e6ea"
    FONT, FONT_SM = ("PingFang SC", 13), ("PingFang SC", 11)

    def make_card(parent, title, subtitle=""):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1, bd=0)
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", padx=16, pady=(8, 4))
        tk.Label(head, text=title, font=("PingFang SC", 14, "bold"), bg=CARD, fg=INK).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, font=FONT_SM, bg=CARD, fg=MUTED).pack(side="left", padx=(10, 0))
        return outer

    state: dict = {"calendars": [], "events": [], "eidx": 0, "plan": None}

    import tkinter.font as tkfont

    # 圆角浅边框按钮：用 Canvas 自绘拿回 Aqua 风格（圆角 + 浅边框），与 ttk 输入框观感一致。
    # macOS 会忽略 tk.Button/tk.Label 的配色，Canvas 自绘颜色 100% 生效，
    # 保留当前高对比度：primary=蓝底白字、ghost=白底深字；hover 只加深底色、绝不反转字色。
    class FlatButton(tk.Canvas):
        def __init__(self, master, text, command=None, kind="ghost", state="normal", **kw):
            self._text = text
            self._primary = kind == "primary"
            self._state = state
            self._cmd = command
            try:
                self._parent_bg = master.cget("bg")
            except tk.TclError:
                self._parent_bg = BG
            # 配色保持现有对比度；边框统一用浅色 LINE，与输入框/卡片一致
            self._fill = BLUE if self._primary else CARD
            self._fg = "white" if self._primary else INK
            self._hover_fill = "#1d4ed8" if self._primary else "#eef2f7"
            self._hover_fg = "white" if self._primary else BLUE
            self._dis_fill, self._dis_fg = "#eef0f3", "#9aa3af"
            self._outline = LINE
            self._hover = False
            f = tkfont.Font(font=FONT)
            w = f.measure(text) + 30
            h = f.metrics("linespace") + 12
            super().__init__(master, width=w, height=h, bg=self._parent_bg,
                             highlightthickness=0, bd=0, **kw)
            self.bind("<Button-1>", self._on_click)
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<Configure>", self._on_configure)
            self._paint()

        def _round_rect(self, x1, y1, x2, y2, r):
            """圆角矩形：用四段四分之一圆弧的采样点构成多边形，避免依赖 Canvas 角度约定。"""
            import math
            pts = []
            for cx, cy, a0, a1 in (
                (x2 - r, y1 + r, -90, 0),   # 右上
                (x2 - r, y2 - r, 0, 90),    # 右下
                (x1 + r, y2 - r, 90, 180),  # 左下
                (x1 + r, y1 + r, 180, 270), # 左上
            ):
                for k in range(6):
                    a = math.radians(a0 + (a1 - a0) * k / 5)
                    pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
            self.create_polygon(pts, fill=self._cur_fill, outline=self._outline,
                                width=1, smooth=True)

        def _paint(self):
            self.delete("all")
            w = self.winfo_width() or self.winfo_reqwidth()
            h = self.winfo_height() or self.winfo_reqheight()
            disabled = self._state == "disabled"
            self._cur_fill = (self._dis_fill if disabled else
                              (self._hover_fill if self._hover else self._fill))
            fg = (self._dis_fg if disabled else
                  (self._hover_fg if self._hover else self._fg))
            self._round_rect(2, 2, w - 2, h - 2, 6)
            self.create_text(w / 2, h / 2, text=self._text, font=FONT, fill=fg)

        def _on_configure(self, _e):
            self._paint()

        def configure(self, **kw):
            if "state" in kw:
                self._state = kw.pop("state")
            super().configure(**kw)
            self._paint()

        config = configure

        def _on_click(self, _e):
            if self._state != "disabled" and self._cmd:
                self._cmd()

        def _on_enter(self, _e):
            if self._state != "disabled":
                self._hover = True
                self._paint()

        def _on_leave(self, _e):
            self._hover = False
            self._paint()

    # macOS 右键默认不弹 ttk 输入框的编辑菜单；手动绑定，支持鼠标右键粘贴
    def bind_context_menu(w):
        menu = tk.Menu(w, tearoff=0)
        menu.add_command(label="剪切", command=lambda: w.event_generate("<<Cut>>"))
        menu.add_command(label="复制", command=lambda: w.event_generate("<<Copy>>"))
        menu.add_command(label="粘贴", command=lambda: w.event_generate("<<Paste>>"))
        menu.add_separator()
        if isinstance(w, tk.Text):
            menu.add_command(label="全选", command=lambda: w.tag_add("sel", "1.0", "end"))
        else:
            menu.add_command(label="全选", command=lambda: w.select_range(0, "end"))

        def popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        w.bind("<Button-2>", popup)  # macOS 右键
        w.bind("<Button-3>", popup)  # 部分环境右键

    header = tk.Frame(root, bg=CARD, height=46)
    header.pack(fill="x", side="top")
    # macOS 惯例：banner 不放应用名（窗口标题栏已有），只留一句功能说明
    tk.Label(header, text="一句话生成日程 · 本机日历写入 · 数据不上传",
             font=FONT_SM, bg=CARD, fg=MUTED).pack(side="left", padx=(22, 12), pady=10)

    # 滚动容器：内容高于屏幕工作区时在窗口内滚动，底部按钮/状态栏永不被裁
    view = tk.Frame(root, bg=BG)
    view.pack(fill="both", expand=True)
    canvas = tk.Canvas(view, bg=BG, highlightthickness=0, bd=0)
    view_sb = ttk.Scrollbar(view, orient="vertical", command=canvas.yview)
    canvas.pack(side="left", fill="both", expand=True)
    view_sb.pack(side="right", fill="y")
    canvas.config(yscrollcommand=view_sb.set)

    body = tk.Frame(canvas, bg=BG)
    # 内容整体下移：顶部留 22px 缝隙（原 10px 贴 banner），底部余量同步收窄
    body_win = canvas.create_window((16, 22), window=body, anchor="nw")

    def _fit_canvas(_e=None):
        cw = canvas.winfo_width()
        if cw > 1:
            canvas.itemconfig(body_win, width=max(1, cw - 32))
        canvas.configure(scrollregion=canvas.bbox("all"))
        # 滚动条按需显示：内容不超过窗口高度时不出现，避免「一打开就有滚动条」的观感
        region = canvas.bbox("all")
        if region and canvas.winfo_ismapped():
            if region[3] - region[1] <= canvas.winfo_height():
                view_sb.pack_forget()
            elif not view_sb.winfo_ismapped():
                view_sb.pack(side="right", fill="y")

    canvas.bind("<Configure>", _fit_canvas)
    body.bind("<Configure>", _fit_canvas)

    def _on_wheel(e):
        # 内容不超容器时完全不劫持滚轮；指针在解析框等自带滚动的文本域上时也不干预
        region = canvas.bbox("all")
        if not region or region[3] - region[1] <= canvas.winfo_height():
            return
        try:
            w = root.winfo_containing(e.x_root, e.y_root)
        except KeyError:
            w = None
        while w is not None:
            if isinstance(w, tk.Text):
                return  # 解析框自己滚
            w = w.master
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    canvas.bind_all("<MouseWheel>", _on_wheel)

    # ---------- 顶部：模糊输入 ----------
    frm_top = make_card(body, "① 输入", "粘贴或输入一句话通知")
    frm_top.pack(fill="x", pady=(0, 10))
    input_row = tk.Frame(frm_top, bg=CARD)
    input_row.pack(fill="x", padx=16, pady=(0, 10))
    input_row.columnconfigure(0, weight=1)  # 输入框吃掉剩余宽度，右侧只留「解析」按钮
    ent_raw = ttk.Entry(input_row, font=FONT)
    ent_raw.grid(row=0, column=0, sticky="ew")

    frm_preview = make_card(body, "② 解析结果", "逐项核对，下方表单可直接修改")
    frm_preview.pack(fill="x", pady=(0, 10))
    # 解析框固定行数＋滚动条：内容多时框内滚动，不再弹性撑高把「行程信息」挤出窗口
    prev_wrap = tk.Frame(frm_preview, bg=CARD)
    prev_wrap.pack(fill="x", padx=16, pady=(0, 10))
    txt_preview = tk.Text(prev_wrap, height=4, wrap="word", font=("PingFang SC", 12),
                          bg="#ffffff", fg=INK, relief="flat", highlightbackground=LINE,
                          highlightthickness=1, padx=10, pady=8)
    txt_preview.pack(side="left", fill="both", expand=True)
    prev_sb = ttk.Scrollbar(prev_wrap, orient="vertical", command=txt_preview.yview)
    txt_preview.config(yscrollcommand=prev_sb.set)
    prev_sb.pack(side="right", fill="y")
    for _w in (ent_raw, txt_preview):
        bind_context_menu(_w)

    # ---------- 中部：表单 ----------
    frm_form = make_card(body, "③ 行程信息", "已按解析结果预填")
    frm_form.pack(fill="x", pady=(0, 10))
    grid = tk.Frame(frm_form, bg=CARD)
    grid.pack(fill="x", padx=16, pady=(0, 10))

    lbl = lambda r, t: tk.Label(grid, text=t, font=FONT_SM, bg=CARD, fg=MUTED).grid(
        row=r, column=0, sticky="e", padx=(8, 6), pady=2)

    lbl(0, "日历分类：")
    cmb_cal = ttk.Combobox(grid, state="readonly", width=30, font=FONT)
    cmb_cal.grid(row=0, column=1, sticky="w", pady=2)

    lbl(1, "标题：")
    ent_title = ttk.Entry(grid, width=50, font=FONT)
    ent_title.grid(row=1, column=1, sticky="w", pady=3, columnspan=2)

    lbl(2, "开始时间：")
    ent_start = ttk.Entry(grid, width=22, font=FONT)
    ent_start.grid(row=2, column=1, sticky="w", pady=2, columnspan=2)

    lbl(3, "结束时间：")
    ent_end = ttk.Entry(grid, width=22, font=FONT)
    ent_end.grid(row=3, column=1, sticky="w", pady=2)

    lbl(4, "地点：")
    ent_loc = ttk.Entry(grid, width=50, font=FONT)
    ent_loc.grid(row=4, column=1, sticky="w", pady=3, columnspan=2)

    lbl(5, "备注：")
    ent_notes = ttk.Entry(grid, width=50, font=FONT)
    ent_notes.grid(row=5, column=1, sticky="w", pady=3, columnspan=2)

    lbl(6, "重复：")
    cmb_recur = ttk.Combobox(grid, state="readonly", width=14, font=FONT,
                             values=("不重复", "每天", "每周", "每月", "每年", "自定义…"))
    cmb_recur.set("不重复")
    cmb_recur.grid(row=6, column=1, sticky="w", pady=2)
    tk.Label(grid, text="循环表述自动预选", font=FONT_SM, bg=CARD, fg=MUTED).grid(
        row=6, column=2, sticky="w", padx=(10, 0))

    # 提醒1（出发提醒）＋提前缓冲合并一行；提醒2 为第二道提醒（同 EventKit 的「另一次提醒」）
    var_alarm = tk.BooleanVar(value=False)
    lbl(7, "提醒1：")
    row7 = tk.Frame(grid, bg=CARD)
    row7.grid(row=7, column=1, sticky="w", columnspan=2)
    ttk.Checkbutton(row7, text="按出发时间设提醒（完全地址＋可定位时自动计算）",
                    variable=var_alarm).pack(side="left")
    tk.Label(row7, text="提前", font=FONT_SM, bg=CARD, fg=MUTED).pack(side="left", padx=(12, 3))
    ent_buffer = ttk.Entry(row7, width=4, font=FONT, justify="center")
    ent_buffer.insert(0, "10")
    ent_buffer.pack(side="left")
    tk.Label(row7, text="分钟", font=FONT_SM, bg=CARD, fg=MUTED).pack(side="left", padx=(3, 0))

    lbl(8, "提醒2：")
    row8 = tk.Frame(grid, bg=CARD)
    row8.grid(row=8, column=1, sticky="w", columnspan=2)
    tk.Label(row8, text="提前", font=FONT_SM, bg=CARD, fg=MUTED).pack(side="left")
    ent_alarm2 = ttk.Entry(row8, width=4, font=FONT, justify="center")
    ent_alarm2.pack(side="left", padx=3)
    tk.Label(row8, text="分钟（留空不设）", font=FONT_SM, bg=CARD, fg=MUTED).pack(side="left")

    lbl(9, "受邀人：")
    ent_att = ttk.Entry(grid, width=50, font=FONT)
    ent_att.grid(row=9, column=1, sticky="w", pady=2)
    tk.Label(grid, text="逗号分隔，写入备注", font=FONT_SM, bg=CARD, fg=MUTED).grid(
        row=9, column=2, sticky="w", padx=(10, 0))

    lbl(10, "URL：")
    ent_url = ttk.Entry(grid, width=50, font=FONT)
    ent_url.grid(row=10, column=1, sticky="w", pady=2)
    tk.Label(grid, text="会议链接/资料地址", font=FONT_SM, bg=CARD, fg=MUTED).grid(
        row=10, column=2, sticky="w", padx=(10, 0))

    for _w in (cmb_cal, ent_title, ent_start, ent_end, ent_loc, ent_notes,
               ent_buffer, ent_alarm2, ent_att, ent_url):
        bind_context_menu(_w)

    frm_btn = tk.Frame(body, bg=BG)
    frm_btn.pack(fill="x", pady=(0, 2))
    # 多条导航（解析出多条时显示）
    var_multi = tk.StringVar(value="")
    tk.Label(frm_btn, textvariable=var_multi, font=FONT_SM, bg=BG, fg=BLUE).pack(side="right", padx=6)
    status_var = tk.StringVar(value="就绪。首次使用会弹出日历授权，请点「允许」.")

    def set_status(s: str, err=False):
        status_var.set(s)
        print(("[ERR] " if err else "") + s)

    # ---------- 日历加载 ----------
    def load_calendars():
        try:
            kit.request_access()
            names = kit.list_calendars()
            state["calendars"] = names
            if names:
                cmb_cal.config(values=names, state="readonly")
                cmb_cal.set(names[0])
                set_status(f"已读取 {len(names)} 个日历分类（实时）。")
                return
            # 授权通过但确实没有分类：给默认值并允许手填
            state["calendars"] = []
            cmb_cal.config(values=(), state="normal")
            cmb_cal.set("")
            set_status("本机日历里暂无分类，可直接手填分类名，或到「日历」App 建一个后点刷新。", err=True)
        except CalendarError as e:
            # 授权未决/被拒：给可编辑框＋默认值，避免空着让人以为程序坏了；不替用户改授权
            state["calendars"] = []
            cmb_cal.config(values=(), state="normal")
            if not cmb_cal.get():
                cmb_cal.set("日历")
            set_status("日历授权未完成，分类未能读取；已可手填，请到 系统设置→隐私→日历 允许后点「刷新日历分类」。", err=True)

    # ---------- 解析 ----------
    _RECUR_LABEL = {"daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年"}

    def apply_parsed_to_form(p):
        if p.title:
            ent_title.delete(0, "end")
            ent_title.insert(0, p.title)
        if p.start:
            ent_start.delete(0, "end")
            ent_start.insert(0, _fmt_dt(p.start))
        if p.end:
            ent_end.delete(0, "end")
            ent_end.insert(0, _fmt_dt(p.end))
        if p.location:
            ent_loc.delete(0, "end")
            ent_loc.insert(0, p.location)
        if p.notes:
            ent_notes.delete(0, "end")
            ent_notes.insert(0, p.notes)
        cmb_recur.set(_RECUR_LABEL.get(p.recur, "不重复"))
        state["recur_custom"] = None
        if p.recur_custom:
            # 解析端识别出的自定义重复（单双周/共N次）：直接进自定义通道，界面可改
            spec = dict(p.recur_custom)
            state["recur_custom"] = {"spec": spec, "label": recur_hint_label(spec)}
            cmb_recur.set(state["recur_custom"]["label"])
        if p.calendar_hint:
            for name in state["calendars"]:
                if p.calendar_hint in name or name in p.calendar_hint:
                    cmb_cal.set(name)
                    break

    # ---------- 自定义重复 ----------
    def on_recur_selected(event=None):
        val = cmb_recur.get()
        if val == "自定义…":
            open_custom_recur()
        else:
            state["recur_last"] = val
            state["recur_custom"] = None

    def open_custom_recur():
        prev_spec = dict((state.get("recur_custom") or {}).get("spec") or {})
        dlg = tk.Toplevel(root)
        dlg.title("自定义重复")
        dlg.configure(bg=CARD)
        dlg.transient(root)
        dlg.resizable(False, False)
        frm = tk.Frame(dlg, bg=CARD, padx=16, pady=12)
        frm.pack(fill="both", expand=True)

        # 第 1 行：每 [N] [天/周/月/年]
        row1 = tk.Frame(frm, bg=CARD)
        row1.pack(anchor="w", pady=(0, 10))
        tk.Label(row1, text="每", font=FONT, bg=CARD, fg=INK).pack(side="left")
        spin_n = tk.Spinbox(row1, from_=1, to=99, width=4, font=FONT,
                            justify="center")
        spin_n.delete(0, "end")
        spin_n.insert(0, str(prev_spec.get("interval") or 1))
        spin_n.pack(side="left", padx=4)
        cmb_unit = ttk.Combobox(row1, state="readonly", width=5, font=FONT,
                                values=("天", "周", "月", "年"))
        cmb_unit.set({"daily": "天", "weekly": "周", "monthly": "月", "yearly": "年"}.get(
            prev_spec.get("freq") or "weekly", "周"))
        cmb_unit.pack(side="left")

        # 第 2 行：每周时勾星期几（其余频率置灰）
        row2 = tk.Frame(frm, bg=CARD)
        row2.pack(anchor="w", pady=(0, 10))
        tk.Label(row2, text="在：", font=FONT, bg=CARD, fg=INK).pack(side="left")
        wd_vars = {}
        for name in ("周一", "周二", "周三", "周四", "周五", "周六", "周日"):
            v = tk.BooleanVar(value=name in (prev_spec.get("weekdays") or []))
            wd_vars[name] = v
            tk.Checkbutton(row2, text=name[1:], variable=v, font=FONT_SM,
                           bg=CARD, fg=INK, activebackground=CARD,
                           selectcolor="#ffffff").pack(side="left")

        def sync_wd(*_):
            for w in row2.winfo_children():
                if isinstance(w, tk.Checkbutton):
                    w.config(state="normal" if cmb_unit.get() == "周" else "disabled")
        cmb_unit.bind("<<ComboboxSelected>>", sync_wd)

        # 第 3 行：结束条件
        row3 = tk.Frame(frm, bg=CARD)
        row3.pack(anchor="w", pady=(0, 10))
        tk.Label(row3, text="结束：", font=FONT, bg=CARD, fg=INK).pack(side="left")
        var_end = tk.StringVar(
            value="count" if prev_spec.get("count") else ("until" if prev_spec.get("until") else "forever"))
        for text, val in (("永不", "forever"), ("共", "count"), ("直到", "until")):
            tk.Radiobutton(row3, text=text, value=val, variable=var_end,
                           font=FONT_SM, bg=CARD, fg=INK, activebackground=CARD,
                           selectcolor="#ffffff").pack(side="left")
        ent_count = tk.Entry(row3, width=4, font=FONT, justify="center")
        ent_count.insert(0, str(prev_spec.get("count") or 10))
        ent_count.pack(side="left", padx=2)
        tk.Label(row3, text="次  ", font=FONT, bg=CARD, fg=INK).pack(side="left")
        ent_until = tk.Entry(row3, width=12, font=FONT, justify="center")
        _pu = prev_spec.get("until")
        ent_until.insert(0, _pu.strftime("%Y-%m-%d") if hasattr(_pu, "strftime") else "2027-12-31")
        ent_until.pack(side="left", padx=2)
        sync_wd()

        def on_ok():
            try:
                n = int(spin_n.get())
                if not 1 <= n <= 99:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("自定义重复", "间隔须为 1–99 的整数。", parent=dlg)
                return
            unit = cmb_unit.get()
            weekdays = [k for k, v in wd_vars.items() if v.get()] if unit == "周" else []
            mode = var_end.get()
            count = until = None
            try:
                if mode == "count":
                    count = int(ent_count.get())
                    if not count >= 1:
                        raise ValueError
                elif mode == "until":
                    until = dt.datetime.strptime(ent_until.get().strip(), "%Y-%m-%d")
            except ValueError:
                messagebox.showwarning(
                    "自定义重复",
                    "次数须为正整数；截止日期格式为 YYYY-MM-DD。" if mode == "until"
                    else "次数须为正整数。",
                    parent=dlg)
                return
            spec = {"freq": {"天": "daily", "周": "weekly", "月": "monthly", "年": "yearly"}[unit],
                    "interval": n}
            if weekdays:
                spec["weekdays"] = weekdays
            if count:
                spec["count"] = count
            if until:
                spec["until"] = until
            label = ("每" + unit) if n == 1 else f"每{n}{unit}"
            if weekdays:
                label += "（" + "、".join(weekdays) + "）"
            if count:
                label += f"·共{count}次"
            if until:
                label += f"·至{until.strftime('%Y-%m-%d')}"
            state["recur_custom"] = {"spec": spec, "label": label}
            cmb_recur.set(label)
            dlg.destroy()

        def on_cancel():
            # 取消：恢复进入对话框前的显示（自定义摘要 > 上次预设 > 不重复）
            if state.get("recur_custom"):
                cmb_recur.set(state["recur_custom"]["label"])
            else:
                cmb_recur.set(state.get("recur_last", "不重复"))
            dlg.destroy()

        rowbtn = tk.Frame(frm, bg=CARD)
        rowbtn.pack(anchor="e", pady=(4, 0))
        FlatButton(rowbtn, "取消", on_cancel, kind="ghost").pack(side="left", padx=6)
        FlatButton(rowbtn, "确定", on_ok, kind="primary").pack(side="left")
        dlg.grab_set()

    cmb_recur.bind("<<ComboboxSelected>>", on_recur_selected)

    def on_parse():
        raw = ent_raw.get().strip()
        if not raw:
            messagebox.showinfo("提示", "请先输入行程描述。")
            return
        slots, anchor = _periods_conf()
        evs = fuzzy_parse_multi(raw, slots=slots, term_anchor=anchor)
        if not evs:
            messagebox.showinfo("提示", "未能从输入中解析出任何行程，请调整表述或改用表单手填。")
            return
        state["events"] = evs
        state["eidx"] = 0
        txt_preview.delete("1.0", "end")
        if len(evs) == 1:
            txt_preview.insert("1.0", format_parsed(evs[0]))
        else:
            body = "\n\n".join(
                f"【第 {i} 条】\n{format_parsed(p)}" for i, p in enumerate(evs, 1)
            )
            txt_preview.insert("1.0", f"共解析出 {len(evs)} 条行程：\n\n{body}")
        apply_parsed_to_form(evs[0])
        update_multi_nav()
        if len(evs) == 1:
            set_status("解析完成，请核对下方表单后添加。")
        else:
            set_status(f"解析出 {len(evs)} 条行程，请用「◀ 上一条 / 下一条 ▶」逐条核对后分别添加。")

    # ---------- 多条导航 ----------
    def update_multi_nav():
        n = len(state["events"])
        if n > 1:
            var_multi.set(f"第 {state['eidx'] + 1}/{n} 条")
            btn_prev.config(state="normal" if state["eidx"] > 0 else "disabled")
            btn_next.config(state="normal" if state["eidx"] < n - 1 else "disabled")
        else:
            var_multi.set("")

    def on_prev():
        if state["events"] and state["eidx"] > 0:
            state["eidx"] -= 1
            apply_parsed_to_form(state["events"][state["eidx"]])
            update_multi_nav()

    def on_next():
        if state["events"] and state["eidx"] < len(state["events"]) - 1:
            state["eidx"] += 1
            apply_parsed_to_form(state["events"][state["eidx"]])
            update_multi_nav()

    # ---------- 添加 ----------
    def on_add():
        title = ent_title.get().strip()
        try:
            start = _parse_dt(ent_start.get())
            end = _parse_dt(ent_end.get())
        except ValueError:
            messagebox.showerror("格式错误", "时间格式应为 YYYY-MM-DD HH:MM")
            return
        if not title:
            messagebox.showerror("缺少标题", "标题不能为空。")
            return
        if end <= start:
            messagebox.showerror("时间错误", "结束时间必须晚于开始时间。")
            return
        cal = cmb_cal.get()
        if not cal:
            messagebox.showerror("缺少日历", "请选择日历分类。")
            return
        loc = ent_loc.get().strip()
        notes = ent_notes.get().strip()
        att = ent_att.get().strip()
        if att:
            # EventKit 无公开 API 写受邀人（只能读），受邀人记入备注随事件保存
            notes = (notes + "\n" if notes else "") + "受邀人：" + att
        url = ent_url.get().strip()
        buffer_min = 10
        try:
            buffer_min = int(ent_buffer.get().strip() or "10")
        except ValueError:
            pass

        # 出行/提醒计划
        alarm_at = None
        plan_lines = []
        if var_alarm.get() and loc:
            plan = plan_alarm(loc, start, buffer_min)
            plan_lines = plan["lines"]
            if plan["ok"]:
                alarm_at = plan["alarm"]
        elif var_alarm.get() and not loc:
            plan_lines = ["⚠ 勾选了出发提醒但未填地点，未设提醒"]
        # 提醒2：事件开始前 N 分钟，绝对时间
        alarm2_at = None
        try:
            a2 = int(ent_alarm2.get().strip())
            if a2 > 0:
                alarm2_at = start - dt.timedelta(minutes=a2)
        except ValueError:
            pass

        sel = cmb_recur.get()
        cust = state.get("recur_custom") or {}
        preset = {"每天": "daily", "每周": "weekly", "每月": "monthly", "每年": "yearly"}.get(sel)
        if sel not in ("不重复", "每天", "每周", "每月", "每年"):
            # 自定义摘要（如「每2周（周一、周三）·共10次」）
            recur, recur_label = cust.get("spec"), cust.get("label") or sel
        elif preset:
            recur, recur_label = preset, sel
        else:
            recur, recur_label = None, ""

        msg = (
            f"日历：{cal}\n标题：{title}\n开始：{_fmt_dt(start)}\n结束：{_fmt_dt(end)}"
            + (f"\n地点：{loc}" if loc else "")
            + (f"\n提醒：{_fmt_dt(alarm_at)}" if alarm_at else (f"\n提醒：无" if not var_alarm.get() else "\n提醒：未能自动计算"))
            + (f"\n提醒2：开始前{int(ent_alarm2.get().strip())}分钟（{_fmt_dt(alarm2_at)}）" if alarm2_at else "")
            + (f"\n重复：{recur_label}" if recur_label else "")
            + (f"\n受邀人：{att}" if att else "")
            + (f"\nURL：{url}" if url else "")
            + (f"\n备注：{notes.splitlines()[0] if notes.splitlines() else ''}" if notes else "")
        )
        if plan_lines:
            msg += "\n\n" + "\n".join(plan_lines)
        msg += "\n\n确认写入日历？"
        if not messagebox.askyesno("确认添加", msg):
            return
        try:
            ident = kit.add_event(cal, title, start, end, loc, notes,
                                  alarm_at, alarm2_at, url, recur)
        except CalendarError as e:
            messagebox.showerror("写入失败", str(e))
            set_status(str(e), err=True)
            return
        _ledger_add(ident, title)  # 登记「本 App 填报」台账，供日程管理一键读取
        # 写入后立即读回（终点自校验，防 D2 复发）
        back = kit.find_events_by_title(title, days_back=1, days_ahead=730)
        line = f"✅ 已写入「{title}」（标识 {ident[:16]}…）。"
        for ev in back:
            if ev["identifier"] == ident:
                line += (
                    f"\n读回核对：开始 {_fmt_dt(ev['start'])}，结束 {_fmt_dt(ev['end'])}"
                    + (f"，提醒×{ev['n_alarms']}" if ev.get("n_alarms") else "")
                    + (f"，URL {ev['url']}" if ev.get("url") else "")
                    + (f"，重复 {ev['recur']}" if ev.get("recur") else "")
                )
        set_status(line)
        # 多条时自动跳到下一条待核对
        n = len(state["events"])
        if n > 1 and state["eidx"] < n - 1:
            state["eidx"] += 1
            apply_parsed_to_form(state["events"][state["eidx"]])
            update_multi_nav()
            messagebox.showinfo("完成", line + f"\n\n本条已添加，已切到第 {state['eidx'] + 1}/{n} 条，请核对后继续添加。")
        else:
            # 单条或最后一条：清空输入区，备下一条（成功信息保留在状态栏）
            clear_input_area()
            messagebox.showinfo("完成", line + "\n\n输入区已清空，可直接输入下一条行程。")

    # ---------- 清空输入区 ----------
    def clear_input_area():
        ent_raw.delete(0, "end")
        txt_preview.delete("1.0", "end")
        for w in (ent_title, ent_start, ent_end, ent_loc, ent_notes, ent_alarm2, ent_att, ent_url):
            w.delete(0, "end")
        cmb_recur.set("不重复")
        state["recur_custom"] = None
        state["events"] = []
        state["eidx"] = 0
        update_multi_nav()

    def on_clear():
        clear_input_area()
        set_status("已清空，可输入下一条行程。")

    # ---------- 管理（一键读取本 App 填报的日程；标题搜索为辅） ----------
    def on_manage():
        win = tk.Toplevel(root)
        win.title("日程管理")
        win.transient(root)  # 从属主窗口：不单占 Dock 项，且不会被压到主窗口后面
        win.configure(bg=BG)
        # 对话框也挂主菜单：否则它拿到焦点时 macOS 显示默认英文菜单，看起来像「菜单变了」
        win.config(menu=menubar)
        # 显式居中于主窗口，避免 macOS 记住坏坐标把窗口摆到屏幕外
        wx = root.winfo_x() + (root.winfo_width() - 720) // 2
        wy = root.winfo_y() + 60
        win.geometry(f"720x460+{max(0, wx)}+{max(0, wy)}")
        win.lift()

        # 顶部提示
        tk.Label(win, text="打开即列出经本 App 填报的日程；也可按标题精确查找：",
                 font=FONT_SM, bg=BG, fg=MUTED, anchor="w").pack(
            fill="x", padx=16, pady=(14, 6))

        # 搜索行（卡片内）：输入框 + 查找按钮
        row_card = tk.Frame(win, bg=CARD, highlightbackground=LINE, highlightthickness=1, bd=0)
        row_card.pack(fill="x", padx=16, pady=(0, 8))
        row = tk.Frame(row_card, bg=CARD)
        row.pack(fill="x", padx=10, pady=8)
        ent_q = ttk.Entry(row, width=40, font=FONT)
        ent_q.pack(side="left", padx=(0, 8))
        # 命令在下方定义，用 lambda 延迟引用（与作息表菜单同一做法）
        FlatButton(row, "查找", lambda: do_find(), kind="primary").pack(side="left")

        # 列表卡片：浅边框白底、统一字体、滚动条按需显示
        list_card = tk.Frame(win, bg=CARD, highlightbackground=LINE, highlightthickness=1, bd=0)
        list_card.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        lst = tk.Listbox(list_card, height=14, font=FONT_SM, bg=CARD, fg=INK,
                         relief="flat", highlightthickness=0, bd=0,
                         selectbackground="#dbeafe", selectforeground=INK,
                         activestyle="none")
        lst.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(list_card, orient="vertical", command=lst.yview)

        found: list[dict] = []
        var_src = tk.StringVar(value="")

        def _sync_sb():
            # 列表项超出可视区才显示滚动条，条目少时保持干净
            if lst.size() > 0 and lst.yview()[1] < 0.999:
                sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
            else:
                sb.pack_forget()
        lst.bind("<Configure>", lambda e: _sync_sb())

        def render():
            lst.delete(0, "end")
            for i, ev in enumerate(found):
                lst.insert("end", f"{_fmt_dt(ev['start'])} ~ {_fmt_dt(ev['end'])}  [{ev['calendar']}]  {ev['title']}"
                           + (f"（{ev['recur']}）" if ev.get("recur") else ""))
            _sync_sb()

        def load_mine():
            nonlocal found
            var_src.set("读取中…")
            ids = [x["id"] for x in _ledger_load()]
            try:
                found = kit.get_events_by_identifiers(ids) if ids else []
            except CalendarError as e:
                messagebox.showerror("读取失败", str(e), parent=win)
                found = []
            # 台账里已不存在（被手动删掉）的标识顺手清掉，保持台账与日历一致
            live = {ev["identifier"] for ev in found}
            removed = 0
            for x in _ledger_load():
                if x["id"] not in live:
                    _ledger_remove(x["id"])
                    removed += 1
            render()
            if removed:
                var_src.set(f"共 {len(found)} 条（另有 {removed} 条已在日历中不存在，台账已清理）")
            else:
                var_src.set(f"共 {len(found)} 条")
            win.lift()  # 对话框关闭后焦点会回落到主窗口，把管理窗抬回前台

        def do_find():
            nonlocal found
            q = ent_q.get().strip()
            try:
                found = kit.find_events_by_title(q) if q else []
            except CalendarError as e:
                messagebox.showerror("查询失败", str(e), parent=win)
                return
            render()
            var_src.set(f"按标题找到 {len(found)} 条")
            win.lift()

        def do_del():
            sel = lst.curselection()
            if not sel:
                messagebox.showinfo("提示", "请先在列表中选中一条。", parent=win)
                return
            ev = found[sel[0]]
            if not messagebox.askyesno(
                "确认删除",
                f"将删除：\n{ev['title']}\n{_fmt_dt(ev['start'])} ~ {_fmt_dt(ev['end'])}\n\n确认？",
                parent=win,
            ):
                return
            try:
                kit.delete_event(ev["identifier"])
            except CalendarError as e:
                messagebox.showerror("删除失败", str(e), parent=win)
                win.lift()
                return
            _ledger_remove(ev["identifier"])  # 经本工具删除的条目同步出台账
            load_mine()

        # 底部操作按钮（FlatButton 与主界面一致）+ 状态条
        frm_m = tk.Frame(win, bg=BG)
        frm_m.pack(pady=(0, 6))
        FlatButton(frm_m, "删除选中日程", do_del, kind="ghost").pack(side="left", padx=5)
        FlatButton(frm_m, "重新读取", load_mine, kind="ghost").pack(side="left", padx=5)
        tk.Label(win, textvariable=var_src, font=FONT_SM, bg=BG, fg=MUTED,
                 anchor="w").pack(fill="x", padx=16, pady=(0, 12))

        load_mine()

    # ---------- 布局按钮 ----------
    def btn(parent, text, cmd, kind="ghost", **kw):
        return FlatButton(parent, text, cmd, kind=kind, **kw)

    btn(input_row, "解析", on_parse, kind="primary").grid(row=0, column=1, padx=(8, 0))

    frm_add = tk.Frame(body, bg=BG)
    frm_add.pack(fill="x", pady=(2, 0))
    btn(frm_add, "添加到日历", on_add, kind="primary").pack(side="left")
    btn_prev = btn(frm_add, "◀ 上一条", on_prev, state="disabled")
    btn_prev.pack(side="left", padx=(8, 0))
    btn_next = btn(frm_add, "下一条 ▶", on_next, state="disabled")
    btn_next.pack(side="left", padx=8)
    btn(frm_add, "刷新日历分类", load_calendars).pack(side="left", padx=(0, 8))
    btn(frm_add, "清空输入", on_clear).pack(side="left", padx=(0, 8))
    btn(frm_add, "日程管理", on_manage).pack(side="left")

    # 状态提示条：浅色卡片，紧跟按钮行
    tk.Label(body, textvariable=status_var, font=FONT_SM, bg="#eef2f7", fg=MUTED,
             anchor="w", justify="left", padx=12, pady=7).pack(fill="x", pady=(10, 0))

    # A6 验收路径：新增/删除日历分类后点「刷新日历分类」即重读
    load_calendars()

    # 自适应窗口高度：canvas 结构下 winfo_reqheight 不含内容，改用 body 实际内容高度；
    # 窗口封顶到 screen_h-30 避开菜单栏，剩余放不下的部分由 canvas 滚动兜底，绝不裁控件。
    root.update_idletasks()
    _fit_canvas()
    root.geometry(f"900x{max(620, header.winfo_reqheight() + body.winfo_reqheight() + 60)}")
    root.update_idletasks()
    _fit_canvas()
    # 二次校正：定宽布局后 body 实际高度与 reqheight 有偏差（实测 ~60px），
    # 按实际高度重设，保证内容恰好放得下、滚动条只在真超高时出现
    body_real = body.winfo_height()
    screen_h = root.winfo_screenheight()
    # 顶部已下移 12px，总高只加 4px：底部余量从 17px 收到 9px，视觉更均衡
    want_h = min(max(620, header.winfo_reqheight() + body_real + 31), screen_h - 30)
    # 显式定位到主屏左上留边，避免 macOS 记住被 pkill 时的坏坐标（窗口跑到屏幕外）
    root.geometry(f"900x{want_h}+60+40")
    root.update_idletasks()
    _fit_canvas()
    root.minsize(800, min(620, want_h))

    root.mainloop()


# ==================== CLI（验收/脚本通道） ====================
# 注意：windowed .app 内 stdout 不可见（旧版 D1 的坑），CLI 输出一律同步落文件。

def run_cli(args) -> int:
    out_path = getattr(args, "out", None) or "./wb-cli-out.txt"

    def say(*parts: str) -> None:
        line = " ".join(str(p) for p in parts)
        try:
            print(line)  # 终端直跑时可见；.app 内被吞但不报错
        except Exception:
            pass
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    try:
        open(out_path, "w", encoding="utf-8").close()  # 清空旧输出
    except Exception:
        pass

    kit = CalendarKit()
    if args.cmd == "probe":
        say("auth:", kit.auth_status_name())
        try:
            kit.request_access()
            say(json.dumps(kit.list_calendars(), ensure_ascii=False))
            return 0
        except CalendarError as e:
            say("ERROR:", e)
            return 1
    if args.cmd == "add":
        _slots, _anchor = _periods_conf()
        events = fuzzy_parse_multi(args.text, slots=_slots, term_anchor=_anchor)
        if not events:
            say("ERROR: 未能从输入解析出任何行程")
            return 2
        n = len(events)
        ok = 0
        for i, p in enumerate(events, 1):
            # 多条时 --title 不适用（会重名），用各自解析标题＋统一前缀
            title = (args.title_prefix or "") + (
                args.title if (args.title and n == 1) else p.title
            )
            if not (p.start and title):
                say(f"ERROR [{i}/{n}]: 解析不完整", json.dumps(format_parsed(p), ensure_ascii=False))
                continue
            alarm_at = None
            if args.alarm and p.location:
                plan = plan_alarm(p.location, p.start, args.buffer)
                for ln in plan["lines"]:
                    say(ln)
                alarm_at = plan["alarm"]
            try:
                # 备注优先级：命令行显式 --notes > 解析出的议题等（p.notes）> 空
                notes = args.notes or p.notes or ""
                ident = kit.add_event(
                    args.calendar, title, p.start, p.end or p.start + dt.timedelta(hours=1),
                    p.location, notes, alarm_at,
                    (p.start - dt.timedelta(minutes=args.alarm2)) if args.alarm2 else None,
                    args.url or "", args.recur or None,
                )
                _ledger_add(ident, title)  # CLI 写入同样登记台账
                say(f"[{i}/{n}] ADDED", ident, "«" + title + "»",
                    f"{_fmt_dt(p.start)}~{_fmt_dt(p.end or p.start + dt.timedelta(hours=1))}")
                ok += 1
            except CalendarError as e:
                say(f"ERROR [{i}/{n}]:", e)
        say(f"DONE {ok}/{n}")
        return 0 if ok == n else 2
    if args.cmd == "readback":
        evs = kit.find_events_by_title(args.title, days_back=args.days_back, days_ahead=args.days_ahead)
        for ev in evs:
            ev["start"] = _fmt_dt(ev["start"])
            ev["end"] = _fmt_dt(ev["end"])
            if ev.get("alarm_time"):
                ev["alarm_time"] = _fmt_dt(ev["alarm_time"])
        say(json.dumps(evs, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "register-title":
        # 维护命令：按标题找回日历事件并登记进「本 App 填报」台账
        # （用于台账损坏/被误清理后的补救，不写入新事件）
        evs = kit.find_events_by_title(args.title, days_back=30, days_ahead=3650)
        for ev in evs:
            _ledger_add(ev["identifier"], ev["title"])
            say("REGISTERED", ev["identifier"], "«" + ev["title"] + "»", _fmt_dt(ev["start"]))
        say(f"DONE {len(evs)}")
        return 0 if evs else 1
    if args.cmd == "delete-title":
        evs = kit.find_events_by_title(args.title)
        if not evs:
            say("NOT FOUND")
            return 0
        n = 0
        for ev in evs:
            if kit.delete_event(ev["identifier"]):
                n += 1
        say(f"DELETED {n}")
        return 0
    return 1


def main():
    ap = argparse.ArgumentParser(description="行程助手 v2")
    ap.add_argument("--cli", dest="cli", action="store_true", help="命令行模式（验收用）")
    ap.add_argument("--out", default=None, help="CLI 输出文件路径（.app 内 stdout 不可见，务必指定）")
    sub = ap.add_subparsers(dest="cmd")

    def _sub(name):
        p = sub.add_parser(name)
        p.add_argument("--out", dest="out", default=None, help="输出文件（子命令级）")
        return p

    _sub("probe")
    p_add = _sub("add")
    p_add.add_argument("--text", required=True)
    p_add.add_argument("--calendar", required=True)
    p_add.add_argument("--title")
    p_add.add_argument("--title-prefix", default="")
    p_add.add_argument("--notes")
    p_add.add_argument("--alarm", action="store_true")
    p_add.add_argument("--buffer", type=int, default=10)
    p_add.add_argument("--recur", choices=("daily", "weekly", "monthly", "yearly"), default=None)
    p_add.add_argument("--url", default="")
    p_add.add_argument("--alarm2", type=int, default=None, help="第二次提醒：事件开始前分钟数")
    p_rb = _sub("readback")
    p_rb.add_argument("--title", required=True)
    p_rb.add_argument("--days-back", type=int, default=30)
    p_rb.add_argument("--days-ahead", type=int, default=90)
    p_del = _sub("delete-title")
    p_del.add_argument("--title", required=True)
    p_reg = _sub("register-title")
    p_reg.add_argument("--title", required=True)

    args = ap.parse_args()
    if args.cli:
        sys.exit(run_cli(args))
    run_gui()


if __name__ == "__main__":
    main()
