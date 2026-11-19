# -*- coding: utf-8 -*-
"""
fuzzy_parser.py — 中文模糊行程输入解析 + 日历分类初判
原则：
- 只做规则级解析，所有解析结果必须显式回显给用户确认，解析不确定的字段留空回退手动输入，绝不静默猜。
- 日期解析全部基于 datetime 计算，支持：X月X日、X日（继承前文月年）、相对日（今天/明天/后天/周X/下周X）、
  时刻（HH:MM、X点半、X点X分、上午/下午/晚上等时段词）、跨天区间（X到Y、X-Y、X~Y、X至Y）、时长（N小时）。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

# ---------------- 数据结构 ----------------

@dataclass
class Parsed:
    raw: str = ""
    title: str = ""
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    location: str = ""
    notes: str = ""                 # 从长通知里剥离的议题/主持等非标题信息
    calendar_hint: str | None = None
    calendar_score: int = 0
    has_date: bool = False   # 输入里是否显式含日期（多事件时第二条继承日期用）
    recur: str = ""          # 重复提示：daily/weekly/monthly/yearly（空=不重复），供界面预选
    recur_custom: dict | None = None  # 自定义重复提示（如单双周/共N次），格式同界面 spec，优先于 recur
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.start is not None and bool(self.title)

# ---------------- 基础词法 ----------------

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_PERIOD = {
    "凌晨": 4, "清晨": 6, "早上": 9, "早晨": 9, "上午": 9,
    "中午": 12, "午后": 13, "下午": 14, "傍晚": 18, "晚上": 19, "夜里": 20,
}
_PERIOD_ALSO = {"今晚": 19, "明晚": 19, "昨夜": 20}

# 日期表达式（先后顺序即优先级）
_RE_ABS_DATE = re.compile(r"(?:(\d{4})\s*年)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_RE_REL_DATE = re.compile(r"(大后天|后天|明天|今天|今晚|明晚|今天晚上)")
_RE_WEEK_DATE = re.compile(r"(?:每)?(上|下下?|这)?(?:周|礼拜|星期)([一二三四五六日天])")
_RE_SLASH_DATE = re.compile(r"(?<![:\d])(\d{1,2})[/](\d{1,2})(?![\d分秒])")
_RE_DASH_DATE = re.compile(r"(?<![:\d])(\d{1,2})[-](\d{1,2})(?![\d])")
_RE_BARE_DAY = re.compile(r"(\d{1,2})\s*[日号]")
_RE_TIME = re.compile(
    r"(?:(\d{1,2})\s*[:：]\s*(\d{1,2})|(\d{1,2})\s*点\s*(?:半|(\d{1,2})\s*分?)?|(\d{1,2})\s*点半)"
)
_RE_PERIOD = re.compile(r"(凌晨|清晨|早上|早晨|上午|中午|午后|下午|傍晚|晚上|夜里)")

_DATE_ALT = (
    r"(?:(?:\d{4})\s*年)?(?:\d{1,2})\s*月\s*(?:\d{1,2})\s*[日号]"
    r"|大后天|后天|明天|今天"
    r"|(?:每)?(?:上|下下?|这)?(?:周|礼拜|星期)(?:[一二三四五六日天])"
    r"|(?:\d{1,2})[/](?:\d{1,2})"
    r"|(?:\d{1,2})[-](?:\d{1,2})"
    r"|(?:\d{1,2})\s*[日号]"
)
_PERIOD_ALT = "凌晨|清晨|早上|早晨|上午|中午|午后|下午|傍晚|晚上|夜里|今晚|明晚"
_TIME_ALT = r"(?:\d{1,2}\s*[:：]\s*\d{1,2})|(?:\d{1,2}\s*点(?:\s*半|\s*\d{1,2}\s*分?)?)|(?:\d{1,2}\s*点半)"

# 完整表达式：日期? 时段? 时刻?（约数「左右/前后」随时刻一并消费，避免漏进标题）
_RE_DATETIME = re.compile(
    rf"(?P<date>\s*(?:{_DATE_ALT}))?\s*(?P<period>(?:{_PERIOD_ALT}))?\s*(?P<time>(?:{_TIME_ALT}))?(?:\s*(?:左右|前后))?"
)

_RE_RANGE_SEP = re.compile(r"\s*(?:到|至|~|～|—|–|--|-|－)\s*")
_RE_DURATION = re.compile(r"(\d{1,3})\s*(?:个)?\s*(?:小时|钟头|h|H)")
_RE_ALLDAY = re.compile(r"全天|一整天|整天")

# ---------------- 周期性课程（节次/单双周/次数） ----------------
# 节次：「第3节」「第3-4节」「3-4节」（中文数字先归一成阿拉伯数字）。
# 裸单数字不带「第」且无区间不认（防「连上3节课」被当时刻）。
# 查不到作息表时不改写、给警告，绝不静默猜。
_RE_SLOT = re.compile(r"第?\s*(\d{1,2})\s*(?:[-~到至]\s*第?\s*(\d{1,2}))?\s*节")
_RE_DANSHUANG = re.compile(r"(单|双)周\s*(?=(?:每)?(?:周|礼拜|星期)[一二三四五六日天])")
_RE_COUNT = re.compile(r"(?:共|上)\s*(\d{1,3})\s*次(?:课)?")
_WEEKDAY_NAME = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _slot_num(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    return _cn_num_to_int(tok)


def _slot_norm(s: str, slots: dict[int, tuple[str, str]]) -> tuple[str, list[str]]:
    """节次表述 → 具体时刻区间（「第3-4节」→「14:00到15:40」），供后续链路按普通时刻解析。
    slots：{节次号: (开始"HH:MM", 结束"HH:MM")}。查不到的节次不改写、给警告。"""
    warns: list[str] = []

    def rep(m: re.Match) -> str:
        n1 = int(m.group(1))
        n2 = int(m.group(2)) if m.group(2) else None
        # 裸「3节」（无第、无区间）不认，防「连上3节课」之类误伤
        if not m.group(0).startswith("第") and n2 is None:
            return m.group(0)
        t1 = slots.get(n1)
        if not t1:
            warns.append(f"第{n1}节未在作息对照表中配置时刻，本次未换算（设置→作息对照表）")
            return m.group(0)
        if n2 is not None:
            t2 = slots.get(n2)
            if not t2:
                warns.append(f"第{n2}节未在作息对照表中配置时刻，本次未换算（设置→作息对照表）")
                return m.group(0)
            return f"{t1[0]}到{t2[1]}"
        return f"{t1[0]}到{t1[1]}"

    return _RE_SLOT.sub(rep, s), warns


def _norm_cn_slots(s: str) -> str:
    """「第三节」→「第3节」（仅限带「第」的节次，其他中文数字不碰）。"""
    def rep(m: re.Match) -> str:
        n = _cn_num_to_int(m.group(1))
        return f"第{n}节" if n is not None else m.group(0)
    return re.sub(r"第\s*([零一二两三四五六七八九十]{1,2})\s*节", rep, s)


def _parity_of(d: dt.date, term_anchor: dt.date) -> int:
    """相对学期第 1 周（周一）的周序奇偶：0=单周（第1、3、5…周），1=双周。"""
    wk0 = term_anchor - dt.timedelta(days=term_anchor.weekday())
    return ((d - wk0).days // 7) % 2


def recur_hint_label(spec: dict) -> str:
    """自定义重复 spec → 中文摘要（与界面自定义对话框的 label 同构）。"""
    unit = {"daily": "天", "weekly": "周", "monthly": "月", "yearly": "年"}.get(
        spec.get("freq") or "weekly", "周")
    n = spec.get("interval") or 1
    label = ("每" + unit) if n == 1 else f"每{n}{unit}"
    if spec.get("weekdays"):
        label += "（" + "、".join(spec["weekdays"]) + "）"
    if spec.get("count"):
        label += f"·共{spec['count']}次"
    if spec.get("until"):
        u = spec["until"]
        label += f"·至{u.strftime('%Y-%m-%d') if hasattr(u, 'strftime') else u}"
    return label

# 中文数字时刻归一：「下午二点半」→「下午2点半」。只对「X点(半|X分|X)」结构生效，
# 不碰「五中全会」「三楼会议室」这类不含「点」的数字词。
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

def _cn_num_to_int(s: str) -> int | None:
    if s == "十":
        return 10
    if len(s) == 2 and s[0] == "十":
        return 10 + _CN_NUM.get(s[1], 0) if s[1] in _CN_NUM else None
    if len(s) == 2 and s[1] == "十":
        return _CN_NUM.get(s[0], 0) * 10 if s[0] in _CN_NUM else None
    return _CN_NUM.get(s)

_RE_CN_TIME = re.compile(r"([零一二两三四五六七八九十]{1,2})\s*点(?:\s*(半|[零一二两三四五六七八九十]{1,3})\s*分?)?")

def _norm_cn_time(s: str) -> str:
    def rep(m: re.Match) -> str:
        h = _cn_num_to_int(m.group(1))
        if h is None or not 0 <= h <= 23:
            return m.group(0)
        rest = (m.group(2) or "").strip()
        if not rest:
            return f"{h}点"
        if rest == "半":
            return f"{h}点半"
        mi = _cn_num_to_int(rest)
        if mi is None or not 0 <= mi <= 59:
            return m.group(0)
        return f"{h}点{mi}分"
    return _RE_CN_TIME.sub(rep, s)

# 地点：标签式 与 「在 + 地点名」
_RE_LOC_LABEL = re.compile(r"(?:地点|地址|位置|@\s*)[:：]?\s*([^\s，。；;、]{2,40})")
_RE_LOC_ZAI = re.compile(r"在([^\s，。；;、到至~—-]{2,40}?)(?=$|[\s，。；;、]|从|，|召开|举行|举办|进行|开)")

# ---------------- 分类初判 ----------------

_KEYWORDS: dict[str, list[str]] = {
    "工作": [
        "值班", "开会", "会议", "例会", "周会", "月会", "晨会", "面试", "汇报", "述职",
        "评审", "培训", "加班", "项目", "讲座", "答辩", "办公", "出差", "调研", "接待",
        "检查", "考核", "监考", "教研", "备课", "上课", "监考",
    ],
    "家庭": [
        "生日", "家宴", "爸妈", "爸爸", "妈妈", "父母", "老婆", "老公", "孩子", "儿子",
        "女儿", "家长", "接送", "看病", "陪", "家庭", "全家", "祭", "扫墓", "探亲", "亲戚",
    ],
    "私人": ["健身", "跑步", "理发", "体检", "看牙", "打球", "游泳", "看电影", "旅行"],
    "学习": ["读书", "自习", "网课", "考试", "刷题", "课程", "学"],
}


def guess_calendar(title: str, location: str, calendar_names: list[str]) -> tuple[str | None, int]:
    """关键词打分初判分类。返回 (建议分类名或None, 得分)。得分低则不猜。"""
    text = f"{title} {location}"
    scores: dict[str, int] = {}
    for cat, words in _KEYWORDS.items():
        s = sum(2 for w in words if w in text)
        if s:
            scores[cat] = scores.get(cat, 0) + s
    if not scores:
        return None, 0
    best_cat, best_score = max(scores.items(), key=lambda kv: kv[1])
    # 与现有日历名匹配：分类词命中真实日历名 → 强信号
    for name in calendar_names:
        if best_cat in name or name in best_cat:
            return name, best_score + 8
    # 关键词与某个日历名字面重合（如日历名里含「值班」）
    for name in calendar_names:
        if any(w and w in name for w in _KEYWORDS.get(best_cat, [])):
            return name, best_score + 5
    # 命中默认三分类之一且得分够高 → 用分类名本身（用户可改）
    if best_score >= 2:
        return best_cat, best_score
    return None, best_score


# ---------------- 日期时间解析 ----------------

def _apply_period(h: int, period: str | None) -> tuple[int, str | None]:
    """按时段词校准小时，返回 (小时, 警告或None)。"""
    if not period:
        return h, None
    if period in ("凌晨",):
        return (h if h <= 6 else 12 - h if 12 <= h <= 18 else h), None
    if period in ("早上", "早晨", "清晨", "上午"):
        return (h if h < 12 else h - 12 if 12 <= h <= 23 else h), None
    if period in ("中午",):
        if h < 10 or h > 14:
            return 12, f"时段「中午」与{h}点不一致，已按12点处理，请确认"
        return h, None
    if period in ("午后", "下午", "傍晚", "晚上", "夜里", "今晚", "明晚"):
        if h < 12:
            # 「晚上7点」= 19:00，属常规读法，不告警
            return h + 12, None
        return h, None
    return h, None


def _parse_time_to_hm(t: str) -> tuple[int, int] | None:
    t = t.strip()
    m = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"(\d{1,2})\s*点\s*(?:半|(\d{1,2})\s*分?)?", t)
    if m:
        h = int(m.group(1))
        mi = 30 if (m.group(2) is None and "半" in t) else int(m.group(2) or 0)
        return h, mi
    m = re.fullmatch(r"(\d{1,2})\s*点半", t)
    if m:
        return int(m.group(1)), 30
    return None


def _resolve_date(expr: str, base: dt.date, prev: dt.date | None) -> dt.date | None:
    """把日期表达式落到具体日期。prev=区间左侧日期，用于继承「8日」这类残缺表达。"""
    expr = expr.strip()
    m = _RE_ABS_DATE.fullmatch(expr)
    if m:
        y = int(m.group(1)) if m.group(1) else (prev.year if prev else base.year)
        return dt.date(y, int(m.group(2)), int(m.group(3)))
    if expr in ("今天",):
        return base
    if expr in ("今晚",):
        return base
    if expr in ("明天",):
        return base + dt.timedelta(days=1)
    if expr in ("后天",):
        return base + dt.timedelta(days=2)
    if expr in ("大后天",):
        return base + dt.timedelta(days=3)
    m = _RE_WEEK_DATE.fullmatch(expr)
    if m:
        prefix, wd = m.group(1) or "", _WEEKDAY[m.group(2)]
        delta = (wd - base.weekday()) % 7
        if prefix == "上":
            # 「上周五」：过去引用。仅作日期上下文（纯日期片段不占起止槽），不造未来行程
            return base - dt.timedelta(days=delta if delta else 7)
        if prefix == "下下":
            return base + dt.timedelta(days=delta + 14)
        if prefix == "下":
            return base + dt.timedelta(days=delta + 7)
        # 这 / 每 / 无前缀：本周的该天（含今天本身）
        return base + dt.timedelta(days=delta)
    m = _RE_SLASH_DATE.fullmatch(expr)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = prev.year if (prev and mo < prev.month) else base.year
        return dt.date(y, mo, d)
    m = _RE_DASH_DATE.fullmatch(expr)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = prev.year if (prev and mo < prev.month) else base.year
        return dt.date(y, mo, d)
    # 裸日表达（如「8日」）：继承前文月年（B6 场景关键路径）
    m = _RE_BARE_DAY.fullmatch(expr)
    if m:
        if prev is None:
            return None
        try:
            return dt.date(prev.year, prev.month, int(m.group(1)))
        except ValueError:
            return None
    return None


def _find_datetimes(text: str):
    """扫出全部日期时间表达式片段。返回 [(span, period, date_expr, time_str)]"""
    out = []
    for m in _RE_DATETIME.finditer(text):
        period = m.group("period")
        date_e = m.group("date")
        time_e = m.group("time")
        if not date_e and not time_e:
            continue
        if not date_e and time_e and not period:
            # 纯时刻（如「18:30」），要求前面无日期——照收
            pass
        out.append((m.span(), period, date_e, time_e))
    return out


def parse(text: str, now: dt.datetime | None = None,
          slots: dict[int, tuple[str, str]] | None = None,
          term_anchor: dt.date | None = None) -> Parsed:
    p = Parsed(raw=text)
    now = now or dt.datetime.now()
    base = now.date()
    slots = slots or {}
    s = text.strip()
    # 先剥离星期/相对日括注（如「（周五，明天）」），防止括注内词被误当天时间片段
    s = re.sub(r"[（(][^（）()]*(?:周[一二三四五六日天]|星期|礼拜|今天|明天|后天)[^（）()]*[）)]", "", s)
    # 相对日后的绝对日括注（如「明天（11月17日）」）：以括号里的绝对日期为准，
    # 去掉相对日。通知里特意括注具体日期，就是为了消除「明天」随阅读日漂移的歧义
    # （例：11/17 收到「明天（11月17日）」，意图是 11/17，不是按相对词算出的 11/18）。
    # 归一成绝对日后，紧邻的「上午10:00」仍会与之合成同一时间片段，不会被拆成
    # 独立日期片段去抢占结束槽——原实现保留相对日、丢弃绝对日期，方向反了。
    # 若括注日期非法（如「2月30日」），回退保留相对日，避免抛异常或丢掉日期。
    def _use_abs_date(m: re.Match) -> str:
        rel, abs_e = m.group(1), m.group(2).strip()
        am = _RE_ABS_DATE.fullmatch(abs_e)
        if am:
            try:
                y = int(am.group(1)) if am.group(1) else base.year
                dt.date(y, int(am.group(2)), int(am.group(3)))
                return abs_e  # 合法：以括号里的绝对日期为准
            except ValueError:
                pass
        return rel  # 非法：回退相对日
    s = re.sub(
        r"(今天|明天|后天|大后天|今晚|明晚)\s*[（(]\s*"
        r"((?:\d{4}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号])\s*[）)]",
        _use_abs_date, s,
    )
    # 独立绝对日括注（无相对日在前）：只去括号符号，保留日期文本
    s = re.sub(
        r"[（(]\s*((?:\d{4}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号])\s*[）)]",
        r"\1", s,
    )
    # 中文数字时刻归一（「二点半」→「2点半」）
    s = _norm_cn_time(s)
    # 「明年/今年X月X日」：把相对年份落成4位年，防「明年1月1日」落到今年的过去日期。
    # 仅当后面紧跟「月…日」完整日期时才替换，避免「明年3月的计划」被改成「2027年3月的计划」
    s = re.sub(r"明年\s*(?=\d{1,2}\s*月\s*\d{1,2}\s*[日号])", f"{base.year + 1}年", s)
    s = re.sub(r"今年\s*(?=\d{1,2}\s*月\s*\d{1,2}\s*[日号])", f"{base.year}年", s)
    # 节次换算（周期性课程）：「第3-4节」→「14:00到15:40」，走普通时刻链路。
    # 作息表未配置时只警告不改写（宁可不起时间也不猜）。
    s = _norm_cn_slots(s)
    if _RE_SLOT.search(s):
        if slots:
            s, slot_warns = _slot_norm(s, slots)
            for w in slot_warns:
                if w not in p.warnings:
                    p.warnings.append(w)
        else:
            p.warnings.append("检测到节次表述（如第3-4节），请先到 设置→作息对照表 配置各节时刻")
    # 单双周：「单周/双周周X」——移除标记保留周X供日期解析，起始日稍后按奇偶锚定
    parity: int | None = None
    mdsw = _RE_DANSHUANG.search(s)
    if mdsw:
        parity = 0 if mdsw.group(1) == "单" else 1
        s = s[: mdsw.start()] + s[mdsw.end():]
    # 次数结束：「每周六10:00-11:30，共20次」→ 自定义重复带 count
    count_n: int | None = None
    mcnt = _RE_COUNT.search(s)
    if mcnt:
        count_n = int(mcnt.group(1))
        s = s[: mcnt.start()] + s[mcnt.end():]
    # 重复提示（循环表述）：「每周三例会」「每天晨会」——开始时间取首个出现日，
    # 是否真按重复写入由用户在「重复」下拉里确认，解析只做预选不做决断。
    # 「每周三」由日期片段整体消费；这里只消费不带动日期的「每天/每月/每年/每周」，
    # 防止它们漏进标题（「每天站会」→「站会」）
    if re.search(r"每[天日]", s):
        p.recur = "daily"
    elif re.search(r"每[周星期礼拜]", s):
        p.recur = "weekly"
    elif re.search(r"每月", s):
        p.recur = "monthly"
    elif re.search(r"每年", s):
        p.recur = "yearly"
    # 「每月10号」：裸日无月份上下文，落成「最近的下一个10号」的绝对日期
    def _monthly_day(m: re.Match) -> str:
        d = int(m.group(1))
        try:
            cand = base.replace(day=d)
            if cand < base:
                y, mo = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
                cand = dt.date(y, mo, d)
            return cand.strftime("%Y年%m月%d日")
        except ValueError:
            return m.group(0)
    s = re.sub(r"每月\s*(\d{1,2})\s*[日号]", _monthly_day, s)
    s = re.sub(r"每(?:[天日]|月|年|[周星期礼拜](?![一二三四五六日天]))", "", s)

    # 1) 地点
    m = _RE_LOC_LABEL.search(s)
    if m:
        p.location = m.group(1).strip()
        s = s[: m.start()] + s[m.end():]
    else:
        m = _RE_LOC_ZAI.search(s)
        if m:
            p.location = m.group(1).strip()
            s = s[: m.start()] + s[m.end():]

    # 2) 日期时间片段
    frags = _find_datetimes(s)
    consumed: list[tuple[int, int]] = []

    def ineligible(span: tuple[int, int], date_e: str | None = None, warn: bool = True) -> bool:
        """判别「不该占起止槽」的干扰时间片段。

        - 截止提醒：「…10:00前提交」——「前」紧跟时间，是截止而非开始；
          「请提前10分钟」的「前」不在片段末尾，不受影响。
        - 被修正的旧时间：「原定X，现推迟到Y」——X 已被 Y 取代，取 Y。
        - 被否定的时间：「明天不是后天」——「不是」后的片段无效。
        - 过去年份引用：「去年12月20日的会」——指过去，不应造出行程。
        - 节日/泛指日期（仅当片段本身无日期时）：「端午节上午9点」落不到具体日子，
          宁可不起时间也不猜一个；「国庆节10月1日」带具体日期则不受此限。
        """
        a, b = span
        after = s[b:].lstrip()
        head = s[:a].rstrip()
        if re.match(r"^(?:之?前|以前)", after) and not after.startswith("提前"):
            return True
        if re.search(r"(?:原定|原计划|原本|原先)\s*$", head) and re.search(r"(?:推迟|改|调整|变更)到", s[b:]):
            return True
        if re.search(r"(?:不是|并非)\s*$", head):
            return True
        if re.search(r"(?:去年|前年)\s*$", head):
            return True
        if date_e is None and re.search(
            r"(?:端午|中秋|春节|国庆|元旦|清明|元宵|重阳|七夕|除夕|腊八|周末|双休日?|节假日|改天|改日|择日)"
            r"[^\s，。]{0,2}$", head,
        ):
            if warn:
                p.warnings.append("日期是节日/泛指（无法落到具体日子），本次未设定时间，请手动补充")
            return True
        return False

    def take(a: int, b: int):
        consumed.append((a, b))

    def overlap(a: int, b: int) -> bool:
        return any(not (b <= x or a >= y) for x, y in consumed)

    start_dt = end_dt = None
    prev_date: dt.date | None = None
    found_date = False

    i = 0
    while i < len(frags):
        span, period, date_e, time_e = frags[i]
        if overlap(*span):
            i += 1
            continue
        if ineligible(span, date_e):
            take(*span)  # 干扰时间也从标题里移除，不留残片
            i += 1
            continue
        # 尝试「左侧 + 分隔符 + 右侧」区间
        matched_range = False
        if i + 1 < len(frags):
            span2, period2, date_e2, time_e2 = frags[i + 1]
            between = s[span[1]: span2[0]]
            msep = _RE_RANGE_SEP.fullmatch(between.strip()) if between.strip() else None
            if msep and not overlap(*span2) and not ineligible(span2, date_e2, warn=False):
                # 解析左
                d1 = _resolve_date(date_e, base, None) if date_e else (prev_date or base)
                if date_e:
                    prev_date = d1
                    found_date = True
                hm1 = _parse_time_to_hm(time_e) if time_e else None
                h1, mi1 = hm1 if hm1 else (_PERIOD.get(period or "", 9), 0)
                raw_h1 = h1  # 校准前的小时，供右侧继承时段时判断「同日递增」
                h1, warn = _apply_period(h1, period)
                if warn:
                    p.warnings.append(warn)
                sd = dt.datetime.combine(d1, dt.time(h1, mi1))
                # 解析右
                d2 = _resolve_date(date_e2, base, d1) if date_e2 else d1
                hm2 = _parse_time_to_hm(time_e2) if time_e2 else None
                if hm2:
                    h2, mi2 = hm2
                else:
                    h2, mi2 = _PERIOD.get(period2 or "", 18), 0
                # 右侧未写时段词、「小时数变大」（如「下午2点到3点」）时继承左侧时段，
                # 否则 3点 会被当成凌晨 03:00。小时变小（「晚上11点到1点」）仍按跨天处理。
                eff_period2 = period2
                if period2 is None and hm2 and raw_h1 < 12 and h2 > raw_h1:
                    eff_period2 = period
                h2, warn2 = _apply_period(h2, eff_period2)
                if warn2:
                    p.warnings.append(warn2)
                ed = dt.datetime.combine(d2, dt.time(h2, mi2))
                if not date_e2 and not time_e2:
                    ed = sd + dt.timedelta(hours=1)
                if ed <= sd and d2 == d1 and not date_e2:
                    ed = ed + dt.timedelta(days=1)
                start_dt, end_dt = sd, ed
                take(*span)
                take(*span2)
                take(span[0], span2[1])
                matched_range = True
                i += 2
                continue
        if not matched_range:
            d1 = _resolve_date(date_e, base, prev_date) if date_e else (prev_date or base)
            if date_e:
                prev_date = d1
                found_date = True
            # 纯日期片段（无时刻、无时段词）：只作日期上下文，不占开始/结束槽。
            # 否则「材料明天现场发放」里的「明天」会被当成 09:00 结束时间，
            # 制造「结束早于开始」的假警报，并覆盖真实时刻。
            if date_e and not time_e and not period:
                take(*span)
                i += 1
                continue
            hm1 = _parse_time_to_hm(time_e) if time_e else None
            h1, mi1 = hm1 if hm1 else (_PERIOD.get(period or "", 9), 0)
            h1, warn = _apply_period(h1, period)
            if warn:
                p.warnings.append(warn)
            sd = dt.datetime.combine(d1, dt.time(h1, mi1))
            if start_dt is None:
                start_dt = sd
            elif end_dt is None:
                end_dt = sd
            take(*span)
        i += 1

    # 3) 单双周锚定：把起始日挪到奇偶正确的那个周X（只向后挪，不回退到过去）
    if parity is not None and start_dt is not None:
        if term_anchor:
            if _parity_of(start_dt.date(), term_anchor) != parity:
                delta = dt.timedelta(days=7)
                start_dt = start_dt + delta
                if end_dt is not None:
                    end_dt = end_dt + delta
                p.warnings.append(
                    f"起始日已按{'单' if parity == 0 else '双'}周锚定到 {start_dt:%m月%d日}，请确认"
                )
        else:
            p.warnings.append("未设置学期第一周（设置→作息对照表），单/双周以最近一周为准，请确认")

    # 3.5) 时长（如「24小时值班」）
    mdur = _RE_DURATION.search(s)
    if mdur:
        hours = int(mdur.group(1))
        if start_dt is not None and end_dt is None:
            end_dt = start_dt + dt.timedelta(hours=hours)
        take(mdur.span()[0], mdur.span()[1])

    # 4) 全天
    mall = _RE_ALLDAY.search(s)
    if mall:
        if start_dt is not None:
            end_dt = dt.datetime.combine(start_dt.date() + dt.timedelta(days=1), dt.time(0, 0))
        take(mall.span()[0], mall.span()[1])

    # 5) 标题 = 去掉已消费片段后的剩余
    remain = ""
    last = 0
    for a, b in sorted(consumed):
        remain += s[last:a]
        last = max(last, b)
    remain += s[last:]
    # 去掉残余的星期/相对日括注，如「（周五，明天）」
    remain = re.sub(r"[（(][^）)]*(?:周[一二三四五六日天]|星期|礼拜|今天|明天|后天|上午|下午|农历)[^）)]*[）)]", "", remain)
    # 不在开头剥离「召开/举行/举办/开展」：refine_title 靠它们做会议名锚点，
    # 且它识别后本就会去掉动词；这里提前剥掉会让带后续分句的长标题降级为整段原样。
    remain = re.sub(r"^\s*(?:从|于|定在|定个|添加|安排|记一?条|记个|进行|参加)\s*", "", remain)
    # 清掉剥离括注后残留的空括号、重复标点（如「工作安排，，召开」）
    remain = re.sub(r"[（(]\s*[）)]", "", remain)
    remain = re.sub(r"([，,、;；])\s*\1+", r"\1", remain)
    remain = re.sub(r"\s{2,}", " ", remain)
    remain = re.sub(r"[\s，。；;、到至~\-—–]*$", "", remain)
    remain = re.sub(r"^[\s，。；;、~\-—–]+", "", remain)
    # 日期片段整体消费后残留的领属助词（「上周五的专题会」→「的专题会」）
    remain = re.sub(r"^[的了]+", "", remain)
    remain = remain.strip()
    # 会议通知类长文本：提炼会议名作标题，议题/主持等进备注
    title, extra_notes = refine_title(remain)
    p.title = title
    p.notes = extra_notes
    p.start = start_dt
    p.end = end_dt
    p.has_date = found_date
    if start_dt is not None and end_dt is None:
        p.end = start_dt + dt.timedelta(hours=1)
        p.warnings.append("未解析到结束时间，默认开始后1小时，请确认")
    if p.end is not None and p.end <= p.start:
        p.warnings.append("结束时间早于开始时间，请检查")
    if p.start is not None and p.start < now:
        p.warnings.append("行程时间在过去，请确认")
    # 自定义重复提示：单双周（每2周）与次数结束（共N次）。有其一即走自定义通道
    if parity is not None or count_n:
        spec: dict = {"freq": p.recur or "weekly", "interval": 2 if parity is not None else 1}
        if start_dt is not None:
            spec["weekdays"] = [_WEEKDAY_NAME[start_dt.weekday()]]
        if count_n:
            spec["count"] = count_n
        p.recur = ""
        p.recur_custom = spec
    return p


def format_parsed(p: Parsed) -> str:
    lines = [f"输入：{p.raw}"]
    lines.append(f"标题：{p.title or '（未解析到，请手动填写）'}")
    if p.start:
        lines.append(f"开始：{p.start:%Y-%m-%d %H:%M}")
    if p.end:
        lines.append(f"结束：{p.end:%Y-%m-%d %H:%M}")
    if p.location:
        lines.append(f"地点：{p.location}")
    if p.notes:
        lines.append(f"备注：{p.notes}")
    if p.recur_custom:
        lines.append(f"重复：{recur_hint_label(p.recur_custom)}（解析提示，可在表单改）")
    elif p.recur:
        label = {"daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年"}.get(p.recur, p.recur)
        lines.append(f"重复：{label}（解析提示，可在表单改）")
    if p.calendar_hint:
        lines.append(f"建议分类：{p.calendar_hint}（得分 {p.calendar_score}，可改）")
    for w in p.warnings:
        lines.append(f"⚠ {w}")
    return "\n".join(lines)


# ---------------- 多事件解析 ----------------

_SENT_SPLIT = re.compile(r"[。；;\n\r]+")

# ---------------- 标题提炼（会议通知类长文本） ----------------
# 寒暄套话：开头「处长您好！」、结尾「请您按时参会，谢谢」
_RE_HI = re.compile(r"^\s*[\u4e00-\u9fa5]{1,8}(?:您好|你好|好)\s*[！!。，,]\s*")
_RE_BYE = re.compile(r"[，,]?\s*(?:请您|请各位|请大家|请)[^。！!]{0,40}?(?:参会|参加|出席|知悉|谢谢)[^。！!]*[。！!]?\s*$")
_RE_THX = re.compile(r"[，,]?\s*(?:谢谢|特此通知|特此公告)[。！!]?\s*$")
# 带动词的会议名（「召开XX会」结构明确，名称可含「会」）
_RE_MEET = re.compile(
    r"(?:召开|举行|举办|开展)\s*([\u4e00-\u9fa5A-Za-z0-9]{2,60}?"
    r"(?:领导小组会|工作会议|工作会|全体会议|专题会议|会议|大会|例会|座谈会|讨论会|研讨会|培训会|发布会|办公会|协调会|年会|会))"
)
# 裸会议名（整句匹配；不用单字「会」收尾，防「五中全会精神方案」被误截）
_RE_MEET_BARE = re.compile(
    r"^([\u4e00-\u9fa5A-Za-z0-9「」]{1,60}?"
    r"(?:领导小组会|工作会议|工作会|全体会议|专题会议|会议|大会|例会|座谈会|讨论会|研讨会|培训会|发布会|办公会|协调会|晨会|周会|年会))$"
)
_RE_TITLE_SEG = re.compile(r"[，,；;。！!]+")
# 背景套话分句：以「根据/按照/依据…安排/要求/部署/精神」起头的状语——只交代来由、不承载行程内容
_RE_BG_CLAUSE = re.compile(r"^(?:根据|按照|依据|遵照)[^，,]{0,20}?(?:安排|要求|部署|精神|通知|要求)$")
# 后勤事务分句：材料/文件的发放、领取、准备——执行细节，不属于会议本身
_RE_LOGISTICS = re.compile(
    r"(?:材料|文件|纸质版|纸质材料)[^，,]{0,16}?(?:发放|分发|领取|准备)"
    r"|(?:发放|分发|领取)[^，,]{0,16}?(?:材料|文件|纸质版)"
    r"|现场发放|会后[^，,]{0,6}发放"
)
# 主持句尾部「会议」与标题重复（标题已是会议名），收成「主持」
_RE_HOST_TAIL = re.compile(r"(主持)会议$")


def refine_title(title: str) -> tuple[str, str]:
    """从长通知文本里提炼会议名作标题，其余（议题/主持等实质信息）作备注。

    背景套话（根据…安排）与后勤说明（材料发放）不进备注：它们不承载行程内容。
    「主持会议」收成「主持」：标题已是会议名，尾部「会议」重复。
    只在能明确识别会议名时才替换，否则标题原样返回、备注为空——遵守「不静默猜」。
    返回 (标题, 备注)。
    """
    s = title.strip()
    s = _RE_HI.sub("", s)
    s = _RE_BYE.sub("", s)
    s = _RE_THX.sub("", s)
    segs = [x.strip() for x in _RE_TITLE_SEG.split(s) if x.strip()]
    if not segs:
        return title.strip(), ""
    meet = None
    for seg in segs:
        m = _RE_MEET.search(seg)
        if m:
            meet = m.group(1).strip()
            break
    if meet is None:
        for seg in segs:
            m = _RE_MEET_BARE.fullmatch(seg)
            if m:
                meet = m.group(1).strip()
                break
    if not meet:
        return title.strip(), ""
    notes = []
    for seg in segs:
        if seg == meet or meet in seg:
            continue
        if _RE_BG_CLAUSE.match(seg) or _RE_LOGISTICS.search(seg):
            continue  # 背景套话与后勤说明不进备注：不承载议题/主持信息
        n = _RE_MEET.sub("", seg).strip(" ，,")
        n = _RE_HOST_TAIL.sub(r"\1", n)  # 「李经理主持会议」→「李经理主持」
        if n:
            notes.append(n)
    return meet, "，".join(notes)


# 课程表行展开：一门课、多个上课时段（「周一第3-4节、周三第3-4节」或「周六10:00-11:30、周日9:00-10:00」）
_RE_COURSE_OCC = re.compile(
    r"(?:(单|双)周)?(?:每)?(?:周|礼拜|星期)[一二三四五六日天]\s*"
    r"(?:第?\s*\d{1,2}\s*(?:[-~到至]\s*第?\s*\d{1,2})?\s*节"
    r"|\d{1,2}\s*[:：]\s*\d{2}(?:\s*[-~到至]\s*\d{1,2}\s*[:：]\s*\d{2})?)"
)


def _expand_course_line(seg: str) -> list[str] | None:
    """课程表行 → 多条子输入（每个上课时段一条，共享标题/地点）。

    形态：`高等数学 周一第3-4节、周三第3-4节 主楼302`。
    判定：≥2 个时段片段、且片段之间只有分隔符（、，,和与/空白）。不满足返回 None 走普通解析。
    行尾短词块含教室/场地特征字（或数字）且无时刻词时识别为地点，带警告供确认——
    这是课程表行内的启发式，其余文本不受影响。
    """
    ms = list(_RE_COURSE_OCC.finditer(seg))
    if len(ms) < 2:
        return None
    for a, b in zip(ms, ms[1:]):
        if not re.fullmatch(r"[、，,和与\s]*", seg[a.end(): b.start()]):
            return None
    head = seg[: ms[0].start()].strip()
    tail = seg[ms[-1].end():].strip()
    loc_token = None
    tail_rest = tail
    if tail and not re.search(r"在|地点[:：]|地址[:：]", tail):
        mt = re.search(r"(\S+)$", tail)
        if mt:
            tok = mt.group(1)
            if (len(tok) <= 20
                    and not re.search(r"\d{1,2}[:：]|点|周[一二三四五六日天]|节|次", tok)
                    and re.search(r"\d|教室|楼|厅|室|馆|场|房|中心|操场|园", tok)):
                loc_token = tok
                tail_rest = tail[: mt.start()].strip()
    out = []
    for m in ms:
        occ = m.group(0).strip()
        parts = [x for x in (head, occ, tail_rest) if x]
        if loc_token:
            parts.append("在" + loc_token)
        out.append(" ".join(parts))
    return out


def parse_multi(text: str, now: dt.datetime | None = None,
                slots: dict[int, tuple[str, str]] | None = None,
                term_anchor: dt.date | None = None) -> list[Parsed]:
    """一条输入解析出多条行程。

    规则：
    - 按 。；;换行 分句，每句独立解析；
    - 课程表行（一门课多个上课时段）：「高等数学 周一第3-4节、周三第3-4节 主楼302」
      按时段展开成多条，标题/地点共享；
    - 后续句无显式日期但有时刻时，继承前一句的日期（「9:10-9:50进行……」）；
    - 全局「地点：X」标签作用于全部事件；各句内「在X」地点仍按句生效；
    - 无日期时间的残句并入上一条的标题，避免丢信息。
    """
    now = now or dt.datetime.now()
    s = text.strip()
    if not s:
        return []

    # 全局地点标签（地点：X / 地址：X）取第一处，作用于全部
    shared_loc = ""
    m = _RE_LOC_LABEL.search(s)
    if m:
        shared_loc = m.group(1).strip()
        s = s[: m.start()] + s[m.end():]

    events: list[Parsed] = []
    prev_date: dt.date | None = None

    for seg in [t.strip() for t in _SENT_SPLIT.split(s) if t.strip()]:
        # 课程行展开：一门课、多个「周X+节次/时刻」时段 → 多条共享标题/地点的事件
        expanded = _expand_course_line(seg)
        if expanded is not None:
            for sub in expanded:
                p = parse(sub, now, slots, term_anchor)
                if p.start is None:
                    continue
                # 课程表行语义上就是每周上课：无重复提示时预选「每周」（界面可改）
                if not p.recur and not p.recur_custom:
                    p.recur = "weekly"
                if p.start is not None:
                    prev_date = p.start.date()
                events.append(p)
            if expanded:
                continue  # 展开成功即本行处理完毕
        p = parse(seg, now, slots, term_anchor)
        if p.start is None:
            if p.title and events:
                # 纯文字残句：并入上一条标题
                if p.title not in events[-1].title:
                    events[-1].title = f"{events[-1].title}（{p.title}）"
                continue
            if p.title and not events:
                events.append(p)  # 首句就无时间：保留，让上层提示补全
                continue
            continue
        # 无显式日期但有时刻 → 继承前一条日期
        if not p.has_date and prev_date is not None and p.start is not None:
            shift = dt.datetime.combine(prev_date, dt.time(0)) - dt.datetime.combine(
                p.start.date(), dt.time(0)
            )
            p.start = p.start + shift
            if p.end is not None:
                p.end = p.end + shift
            p.warnings.append("本条未写日期，已按上一条的日期处理，请确认")
        if p.start is not None:
            prev_date = p.start.date()
        events.append(p)

    if shared_loc:
        for p in events:
            if not p.location:
                p.location = shared_loc
    return events
