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
import datetime as dt
import json
import sys

from calkit import CalendarKit, CalendarError
from fuzzy_parser import parse as fuzzy_parse, parse_multi as fuzzy_parse_multi, guess_calendar, format_parsed
import geo_helper


TEST_PREFIX = "WB验收-可删"


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
    root.title("行程助手")
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
                return p
        return None

    def open_help():
        p = _help_html_path()
        if p:
            webbrowser.open("file://" + p)
        else:  # 兜底：包内没带帮助页时打开在线版
            webbrowser.open(readme_url)

    # 自定义 Help 菜单（name="help" 成为系统帮助菜单）。注意 macOS 会在帮助菜单顶部
    # 固定放一个「行程助手帮助」项：不接管时点击它弹「Help isn't available」（无 Help Book），
    # 必须注册 tk::mac::ShowHelp 回调把这一项接到本地帮助页上。
    menubar = tk.Menu(root)
    help_menu = tk.Menu(menubar, name="help", tearoff=0)
    help_menu.add_command(label="使用说明（本机）", command=open_help)
    help_menu.add_command(label="使用说明（在线）",
                          command=lambda: webbrowser.open(readme_url))
    help_menu.add_command(label="官方页面（发布与下载）",
                          command=lambda: webbrowser.open(official_url))
    menubar.add_cascade(label="帮助", menu=help_menu)
    root.config(menu=menubar)
    root.createcommand("tk::mac::ShowHelp", open_help)

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
        head.pack(fill="x", padx=16, pady=(11, 6))
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
    tk.Label(header, text="行程助手", font=("PingFang SC", 16, "bold"), bg=CARD, fg=INK).pack(
        side="left", padx=(22, 12), pady=10)
    tk.Label(header, text="一句话生成日程 · 本机日历写入 · 数据不上传",
             font=FONT_SM, bg=CARD, fg=MUTED).pack(side="left", pady=10)

    # 滚动容器：内容高于屏幕工作区时在窗口内滚动，底部按钮/状态栏永不被裁
    view = tk.Frame(root, bg=BG)
    view.pack(fill="both", expand=True)
    canvas = tk.Canvas(view, bg=BG, highlightthickness=0, bd=0)
    view_sb = ttk.Scrollbar(view, orient="vertical", command=canvas.yview)
    canvas.pack(side="left", fill="both", expand=True)
    view_sb.pack(side="right", fill="y")
    canvas.config(yscrollcommand=view_sb.set)

    body = tk.Frame(canvas, bg=BG)
    body_win = canvas.create_window((16, 10), window=body, anchor="nw")

    def _fit_canvas(_e=None):
        cw = canvas.winfo_width()
        if cw > 1:
            canvas.itemconfig(body_win, width=max(1, cw - 32))
        canvas.configure(scrollregion=canvas.bbox("all"))

    canvas.bind("<Configure>", _fit_canvas)
    body.bind("<Configure>", _fit_canvas)

    def _on_wheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    canvas.bind_all("<MouseWheel>", _on_wheel)

    # ---------- 顶部：模糊输入 ----------
    frm_top = make_card(body, "① 输入", "粘贴或输入一句话通知")
    frm_top.pack(fill="x", pady=(0, 12))
    input_row = tk.Frame(frm_top, bg=CARD)
    input_row.pack(fill="x", padx=16, pady=(0, 14))
    input_row.columnconfigure(0, weight=1)  # 输入框吃掉剩余宽度，右侧只留「解析」按钮
    ent_raw = ttk.Entry(input_row, font=FONT)
    ent_raw.grid(row=0, column=0, sticky="ew")

    frm_preview = make_card(body, "② 解析结果", "逐项核对，下方表单可直接修改")
    frm_preview.pack(fill="x", pady=(0, 12))
    # 解析框固定行数＋滚动条：内容多时框内滚动，不再弹性撑高把「行程信息」挤出窗口
    prev_wrap = tk.Frame(frm_preview, bg=CARD)
    prev_wrap.pack(fill="x", padx=16, pady=(0, 14))
    txt_preview = tk.Text(prev_wrap, height=6, wrap="word", font=("PingFang SC", 12),
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
    grid.pack(fill="x", padx=16, pady=(0, 14))

    lbl = lambda r, t: tk.Label(grid, text=t, font=FONT_SM, bg=CARD, fg=MUTED).grid(
        row=r, column=0, sticky="e", padx=(8, 6), pady=5)

    lbl(0, "日历分类：")
    cmb_cal = ttk.Combobox(grid, state="readonly", width=30, font=FONT)
    cmb_cal.grid(row=0, column=1, sticky="w", pady=5)

    lbl(1, "标题：")
    ent_title = ttk.Entry(grid, width=50, font=FONT)
    ent_title.grid(row=1, column=1, sticky="w", pady=5, columnspan=2)

    lbl(2, "开始时间：")
    ent_start = ttk.Entry(grid, width=22, font=FONT)
    ent_start.grid(row=2, column=1, sticky="w", pady=5)
    tk.Label(grid, text="格式 YYYY-MM-DD HH:MM", font=FONT_SM, bg=CARD, fg=MUTED).grid(
        row=2, column=2, sticky="w", padx=(10, 0))

    lbl(3, "结束时间：")
    ent_end = ttk.Entry(grid, width=22, font=FONT)
    ent_end.grid(row=3, column=1, sticky="w", pady=5)

    lbl(4, "地点：")
    ent_loc = ttk.Entry(grid, width=50, font=FONT)
    ent_loc.grid(row=4, column=1, sticky="w", pady=5, columnspan=2)

    lbl(5, "备注：")
    ent_notes = ttk.Entry(grid, width=50, font=FONT)
    ent_notes.grid(row=5, column=1, sticky="w", pady=5, columnspan=2)

    var_alarm = tk.BooleanVar(value=False)
    chk_alarm = ttk.Checkbutton(grid, text="按出发时间设提醒（完全地址＋可定位时自动计算）",
                                variable=var_alarm)
    chk_alarm.grid(row=6, column=1, sticky="w", pady=(6, 2), columnspan=2)
    lbl(7, "提前缓冲(分钟)：")
    ent_buffer = ttk.Entry(grid, width=8, font=FONT)
    ent_buffer.insert(0, "10")
    ent_buffer.grid(row=7, column=1, sticky="w", pady=(2, 0))
    for _w in (cmb_cal, ent_title, ent_start, ent_end, ent_loc, ent_notes, ent_buffer):
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
        if p.calendar_hint:
            for name in state["calendars"]:
                if p.calendar_hint in name or name in p.calendar_hint:
                    cmb_cal.set(name)
                    break

    def on_parse():
        raw = ent_raw.get().strip()
        if not raw:
            messagebox.showinfo("提示", "请先输入行程描述。")
            return
        evs = fuzzy_parse_multi(raw)
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

        msg = (
            f"日历：{cal}\n标题：{title}\n开始：{_fmt_dt(start)}\n结束：{_fmt_dt(end)}"
            + (f"\n地点：{loc}" if loc else "")
            + (f"\n提醒：{_fmt_dt(alarm_at)}" if alarm_at else (f"\n提醒：无" if not var_alarm.get() else "\n提醒：未能自动计算"))
            + (f"\n备注：{notes}" if notes else "")
        )
        if plan_lines:
            msg += "\n\n" + "\n".join(plan_lines)
        msg += "\n\n确认写入日历？"
        if not messagebox.askyesno("确认添加", msg):
            return
        try:
            ident = kit.add_event(cal, title, start, end, loc, notes, alarm_at)
        except CalendarError as e:
            messagebox.showerror("写入失败", str(e))
            set_status(str(e), err=True)
            return
        # 写入后立即读回（终点自校验，防 D2 复发）
        back = kit.find_events_by_title(title, days_back=1, days_ahead=730)
        line = f"✅ 已写入「{title}」（标识 {ident[:16]}…）。"
        for ev in back:
            if ev["identifier"] == ident:
                line += (
                    f"\n读回核对：开始 {_fmt_dt(ev['start'])}，结束 {_fmt_dt(ev['end'])}"
                    + (f"，提醒 {_fmt_dt(ev['alarm_time'])}" if ev.get("alarm_time") else "")
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
        for w in (ent_title, ent_start, ent_end, ent_loc, ent_notes):
            w.delete(0, "end")
        state["events"] = []
        state["eidx"] = 0
        update_multi_nav()

    def on_clear():
        clear_input_area()
        set_status("已清空，可输入下一条行程。")

    # ---------- 管理（读回/删除，供验收与测试数据清理） ----------
    def on_manage():
        win = tk.Toplevel(root)
        win.title("日程管理（读回 / 删除）")
        win.geometry("680x420")
        ttk.Label(win, text="按标题精确查找：").pack(anchor="w", padx=8, pady=(8, 2))
        row = ttk.Frame(win)
        row.pack(fill="x", padx=8)
        ent_q = ttk.Entry(row, width=40)
        ent_q.pack(side="left")
        lst = tk.Listbox(win, height=14)
        lst.pack(fill="both", expand=True, padx=8, pady=6)
        found: list[dict] = []

        def refresh():
            lst.delete(0, "end")
            q = ent_q.get().strip()
            nonlocal found
            found = kit.find_events_by_title(q) if q else []
            for i, ev in enumerate(found):
                lst.insert("end", f"{_fmt_dt(ev['start'])} ~ {_fmt_dt(ev['end'])}  [{ev['calendar']}]  {ev['title']}")

        def do_find():
            try:
                refresh()
            except CalendarError as e:
                messagebox.showerror("查询失败", str(e), parent=win)

        ttk.Button(row, text="查找", command=do_find).pack(side="left", padx=4)

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
            kit.delete_event(ev["identifier"])
            do_find()

        ttk.Button(win, text="删除选中日程", command=do_del).pack(pady=4)

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
    content_h = body.winfo_reqheight() + 32  # 左右/上下边距
    want_h = header.winfo_reqheight() + content_h + 16
    screen_h = root.winfo_screenheight()
    want_h = min(want_h, screen_h - 30)
    want_h = max(want_h, 620)
    root.geometry(f"900x{want_h}")
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
        events = fuzzy_parse_multi(args.text)
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
                )
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
    p_rb = _sub("readback")
    p_rb.add_argument("--title", required=True)
    p_rb.add_argument("--days-back", type=int, default=30)
    p_rb.add_argument("--days-ahead", type=int, default=90)
    p_del = _sub("delete-title")
    p_del.add_argument("--title", required=True)

    args = ap.parse_args()
    if args.cli:
        sys.exit(run_cli(args))
    run_gui()


if __name__ == "__main__":
    main()
