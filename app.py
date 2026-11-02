#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小管家 · LifeAgent
一个零依赖、本地优先的生活助理 Agent。

  python app.py            # 启动网页版（默认 http://127.0.0.1:7788）
  python app.py --cli      # 终端交互
  python app.py --self-test

设计原则：
1. 只用 Python 标准库，不需要 pip install，拿到就能跑。
2. 数据只存在本机 data/life.db，不上传任何地方。
3. 没有 API Key 也能用：内置中文指令解析器处理记账/待办/统计。
4. 配了 DEEPSEEK_API_KEY 后，听不懂的话会交给大模型做工具调用（可选增强）。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LIFEAGENT_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "life.db"
HOST = os.environ.get("LIFEAGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIFEAGENT_PORT", "7788"))
LLM_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
LLM_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

CATEGORIES = {
    "餐饮": ["饭", "午饭", "晚饭", "早餐", "外卖", "咖啡", "奶茶", "吃", "食堂", "火锅", "烧烤", "零食", "水果", "买菜"],
    "交通": ["打车", "地铁", "公交", "加油", "停车", "高铁", "火车", "机票", "滴滴", "共享单车", "充电"],
    "居住": ["房租", "水费", "电费", "燃气", "物业", "网费", "宽带", "家具", "维修"],
    "购物": ["买", "衣服", "鞋", "裤子", "数码", "耳机", "手机", "电脑", "书", "快递", "淘宝", "京东"],
    "医疗": ["药", "医院", "挂号", "体检", "牙", "看病", "诊所"],
    "娱乐": ["电影", "游戏", "演出", "门票", "KTV", "演唱会", "会员", "视频", "健身"],
    "学习": ["课", "培训", "考试", "报名", "学费", "课程", "书"],
    "人情": ["红包", "礼物", "份子", "请客", "随礼"],
}

# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    amount     REAL NOT NULL,
    category   TEXT NOT NULL,
    note       TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    due_at     TEXT,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_day ON expenses(day);
CREATE INDEX IF NOT EXISTS idx_task_done ON tasks(done);
"""


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 工具层（Agent 可调用的能力）
# --------------------------------------------------------------------------

def tool_record_expense(amount: float, category: str = "", note: str = "", day: str = "") -> dict:
    if amount <= 0:
        return {"ok": False, "error": "金额必须大于 0"}
    day = day or date.today().isoformat()
    category = category or guess_category(note) or "其他"
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO expenses(day, amount, category, note, created_at) VALUES (?,?,?,?,?)",
            (day, round(float(amount), 2), category, note.strip(), _now()),
        )
        eid = cur.lastrowid
    return {"ok": True, "id": eid, "day": day, "amount": round(float(amount), 2),
            "category": category, "note": note.strip()}


def tool_list_expenses(month: str = "", limit: int = 10) -> dict:
    limit = max(1, min(int(limit or 10), 100))
    with db() as conn:
        if month:
            rows = conn.execute(
                "SELECT * FROM expenses WHERE day LIKE ? ORDER BY day DESC, id DESC LIMIT ?",
                (month + "%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM expenses ORDER BY day DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}


def tool_stats(month: str = "", category: str = "") -> dict:
    month = month or date.today().strftime("%Y-%m")
    with db() as conn:
        if category:
            rows = conn.execute(
                "SELECT category, SUM(amount) total, COUNT(*) n FROM expenses "
                "WHERE day LIKE ? AND category=? GROUP BY category",
                (month + "%", category),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT category, SUM(amount) total, COUNT(*) n FROM expenses "
                "WHERE day LIKE ? GROUP BY category ORDER BY total DESC",
                (month + "%",),
            ).fetchall()
        total_row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM expenses WHERE day LIKE ?",
            (month + "%",),
        ).fetchone()
    return {
        "ok": True, "month": month,
        "total": round(total_row["s"], 2), "count": total_row["c"],
        "by_category": [{"category": r["category"], "total": round(r["total"], 2), "count": r["n"]} for r in rows],
    }


def tool_add_task(title: str, due_at: str = "", priority: str = "normal") -> dict:
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "待办内容不能为空"}
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(title, due_at, done, created_at) VALUES (?,?,0,?)",
            (title, due_at or None, _now()),
        )
        tid = cur.lastrowid
    return {"ok": True, "id": tid, "title": title, "due_at": due_at or None, "priority": priority}


def tool_list_tasks(scope: str = "open", limit: int = 20) -> dict:
    sql = "SELECT * FROM tasks WHERE done=0"
    args: list = []
    if scope == "today":
        today = date.today().isoformat()
        sql += " AND (due_at IS NULL OR substr(due_at,1,10) <= ?)"
        args.append(today)
    elif scope == "done":
        sql = "SELECT * FROM tasks WHERE done=1"
    sql += " ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, id ASC LIMIT ?"
    args.append(max(1, min(int(limit or 20), 100)))
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    items = []
    today = date.today().isoformat()
    for r in rows:
        d = dict(r)
        d["overdue"] = bool(d["due_at"] and d["due_at"][:10] < today and not d["done"])
        items.append(d)
    return {"ok": True, "items": items}


def tool_complete_task(task_id: int = 0, title: str = "") -> dict:
    with db() as conn:
        if task_id:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        elif title:
            row = conn.execute(
                "SELECT * FROM tasks WHERE done=0 AND title LIKE ? ORDER BY id DESC LIMIT 1",
                ("%" + title.strip() + "%",),
            ).fetchone()
        else:
            row = None
        if not row:
            return {"ok": False, "error": "没找到这条待办"}
        conn.execute("UPDATE tasks SET done=1, done_at=? WHERE id=?", (_now(), row["id"]))
    return {"ok": True, "id": row["id"], "title": row["title"]}


def tool_undo_last(kind: str = "expense") -> dict:
    with db() as conn:
        if kind == "task":
            row = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return {"ok": False, "error": "没有可撤销的待办"}
            conn.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
            return {"ok": True, "removed": dict(row)}
        row = conn.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return {"ok": False, "error": "没有可撤销的记账"}
        conn.execute("DELETE FROM expenses WHERE id=?", (row["id"],))
        return {"ok": True, "removed": dict(row)}


def tool_briefing() -> dict:
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    with db() as conn:
        week = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM expenses WHERE day >= ?", (week_start,)
        ).fetchone()
        month = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE day LIKE ?", (today.strftime("%Y-%m") + "%",)
        ).fetchone()
        prev_month = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE day LIKE ?", (
                (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m") + "%",)
        ).fetchone()
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE done=0 ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC LIMIT 50"
        ).fetchall()
        recent = conn.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 6").fetchall()
        cats = conn.execute(
            "SELECT category, SUM(amount) t, COUNT(*) n FROM expenses WHERE day LIKE ? "
            "GROUP BY category ORDER BY t DESC", (today.strftime("%Y-%m") + "%",),
        ).fetchall()
    todo_today, overdue = [], []
    for t in tasks:
        d = dict(t)
        if d["due_at"] and d["due_at"][:10] < today.isoformat():
            overdue.append(d)
        elif not d["due_at"] or d["due_at"][:10] == today.isoformat():
            todo_today.append(d)
    return {
        "ok": True,
        "date": today.isoformat(),
        "week_total": round(week["s"], 2), "week_count": week["c"],
        "month_total": round(month["s"], 2),
        "prev_month_total": round(prev_month["s"], 2),
        "todo_today": todo_today, "overdue": overdue, "open_count": len(tasks),
        "open_tasks": [dict(t) for t in tasks],
        "recent": [dict(r) for r in recent],
        "by_category": [{"category": c["category"], "total": round(c["t"], 2), "count": c["n"]} for c in cats],
    }


def tool_export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "日期", "金额", "分类", "备注"])
    with db() as conn:
        for r in conn.execute("SELECT id, day, amount, category, note FROM expenses ORDER BY day, id"):
            w.writerow([r["id"], r["day"], r["amount"], r["category"], r["note"]])
    return buf.getvalue()


# --------------------------------------------------------------------------
# 中文规则解析（离线大脑）
# --------------------------------------------------------------------------

NUM = re.compile(r"(?<![\d.])(\d{1,7}(?:\.\d{1,2})?)(?![\d.])")
DATE_WORDS = {"今天": 0, "今日": 0, "昨天": -1, "昨日": -1, "前天": -2, "明天": 1, "明日": 1, "后天": 2}


def guess_category(text: str) -> str:
    for cat, words in CATEGORIES.items():
        for w in words:
            if w in text:
                return cat
    return ""


def parse_amount(text: str):
    """返回 (金额, 匹配到的片段)。带"元/块/钱"优先，其次裸数字。"""
    m = re.search(r"(\d{1,7}(?:\.\d{1,2})?)\s*(?:元|块钱|块|rmb|RMB)", text)
    if m:
        return float(m.group(1)), m.group(0)
    # 「午饭 28」这种：取最后一个裸数字
    nums = NUM.findall(text)
    if nums:
        return float(nums[-1]), nums[-1]
    return None, ""


def parse_day(text: str) -> str:
    today = date.today()
    for w, delta in DATE_WORDS.items():
        if w in text:
            return (today + timedelta(days=delta)).isoformat()
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
    if m:
        try:
            return date(today.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return today.isoformat()


def parse_due(text: str):
    """从「明天9点」「周五下午3点」「2026-11-05 18:00」里解析出 ISO 时间。"""
    today = date.today()
    day = None
    for w, delta in DATE_WORDS.items():
        if w in text:
            day = (today + timedelta(days=delta)).isoformat()
            break
    if day is None:
        wd = re.search(r"(下?周|星期|礼拜)([一二三四五六日天])", text)
        if wd:
            idx = "一二三四五六日天".index(wd.group(2)) + 1
            idx = 7 if idx == 8 else idx
            cur = today.isoweekday()
            delta = (idx - cur) % 7
            if wd.group(1) == "下周":
                delta += 7
            if delta == 0 and not re.search(r"\d{1,2}\s*[点:：]", text):
                delta = 7
            day = (today + timedelta(days=delta)).isoformat()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        day = "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if day is None:
        return ""
    tm = re.search(r"(\d{1,2})\s*[点:：]\s*(\d{2})?", text)
    hour, minute = 9, 0
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
        if re.search(r"下午|晚上|傍晚", text) and hour < 12:
            hour += 12
        if "中午" in text and hour < 12:
            hour = 12
        if re.search(r"凌晨", text) and hour >= 12:
            hour -= 12
    return "%s %02d:%02d" % (day, min(hour, 23), min(minute, 59))


TASK_HINT = re.compile(r"提醒|记得|别忘|要交|需交|开会|预约|截止|ddl|DDL|加个待办|添加待办|记个待办|加个任务|添加任务")
STATS_HINT = re.compile(r"花了多少|花多少|支出|消费|统计|账单|开销|花了多少钱|月花|这个月|上周花|本月")
LIST_HINT = re.compile(r"待办|任务|要做什么|有什么要|还有什么没|清单|todo|Todo")
COMPLETE_HINT = re.compile(r"(完成|做完|搞定|勾掉|打勾|done)")
UNDO_HINT = re.compile(r"撤销|删掉上一|删掉最后|撤销上一|删最后|记录错了|记错了")
BRIEF_HINT = re.compile(r"今天|今日|概览|简报|汇总|情况|怎么样")


def clean_title(text: str) -> str:
    t = TASK_HINT.sub("", text)
    t = re.sub(r"^(我|你)(要|得|需要|想)?\s*", "", t.strip())
    t = re.sub(r"(提醒我|记得|别忘(了)?|帮我|一下|，|,)", "", t)
    for w in list(DATE_WORDS) + ["上午", "下午", "晚上", "中午", "凌晨"]:
        t = t.replace(w, "")
    t = re.sub(r"(下?周|星期|礼拜)[一二三四五六日天]", "", t)
    t = re.sub(r"\d{1,2}\s*[点:：]\s*\d{0,2}", "", t)
    return t.strip(" ，,。.!！")


def handle(text: str) -> dict:
    """离线规则大脑：把一句中文变成一次工具调用 + 一段回复。"""
    raw = (text or "").strip()
    if not raw:
        return {"reply": "说点什么吧，比如「午饭 28」或者「提醒我明天 9 点交材料」。", "actions": []}
    t = raw

    if re.search(r"^(帮助|help|怎么用|能干什么|\?|？)$", t.lower()):
        return {"reply": HELP_TEXT, "actions": []}

    if UNDO_HINT.search(t):
        kind = "task" if "待办" in t or "任务" in t else "expense"
        r = tool_undo_last(kind)
        if not r["ok"]:
            return {"reply": r["error"], "actions": []}
        rem = r["removed"]
        if kind == "task":
            return {"reply": "已撤销待办「%s」。" % rem["title"], "actions": ["undo_task"]}
        return {"reply": "已撤销上一笔：%s %.2f 元（%s）。" % (rem["day"], rem["amount"], rem["category"]),
                "actions": ["undo_expense"]}

    if COMPLETE_HINT.search(t) and not parse_amount(t)[0]:
        m = re.search(r"(?:完成|做完|搞定|勾掉|打勾)\s*[:：]?\s*(.+)", t)
        kw = clean_title(m.group(1)) if m else ""
        r = tool_complete_task(title=kw)
        if not r["ok"]:
            return {"reply": "没找到对应的待办。可以说「完成 交材料」。", "actions": []}
        return {"reply": "搞定了：「%s」已划掉。" % r["title"], "actions": ["complete_task"]}

    if STATS_HINT.search(t) and parse_amount(t)[0] is None:
        month = date.today().strftime("%Y-%m")
        m = re.search(r"(\d{1,2})\s*月", t)
        if m:
            month = "%04d-%02d" % (date.today().year, int(m.group(1)))
        cat = guess_category(t)
        s = tool_stats(month=month, category=cat)
        if s["count"] == 0:
            return {"reply": "%s 还没有记账记录。" % month, "actions": ["stats"]}
        if cat:
            c = s["by_category"][0]
            return {"reply": "%s 在「%s」上花了 %.2f 元，共 %d 笔。" % (month, cat, c["total"], c["count"]),
                    "actions": ["stats"], "data": s}
        lines = ["%s 共支出 %.2f 元（%d 笔）：" % (month, s["total"], s["count"])]
        for c in s["by_category"][:5]:
            lines.append("· %s %.2f（%d 笔）" % (c["category"], c["total"], c["count"]))
        return {"reply": "\n".join(lines), "actions": ["stats"], "data": s}

    if TASK_HINT.search(t):
        due = parse_due(t)
        title = clean_title(t)
        if not title:
            title = "未命名待办"
        r = tool_add_task(title=title, due_at=due)
        if not r["ok"]:
            return {"reply": r["error"], "actions": []}
        extra = ("，%s" % due) if due else "，没有设时间"
        return {"reply": "记下了：「%s」%s。" % (title, extra), "actions": ["add_task"], "data": r}

    if LIST_HINT.search(t):
        scope = "today" if re.search(r"今天|今日", t) else "open"
        r = tool_list_tasks(scope=scope)
        if not r["items"]:
            return {"reply": "目前没有未完成的待办，清爽。", "actions": ["list_tasks"]}
        lines = ["未完成待办 %d 条：" % len(r["items"])]
        for it in r["items"][:10]:
            flag = "⚠️ " if it["overdue"] else ""
            due = (" · " + it["due_at"]) if it["due_at"] else ""
            lines.append("%s#%d %s%s" % (flag, it["id"], it["title"], due))
        return {"reply": "\n".join(lines), "actions": ["list_tasks"], "data": r}

    if BRIEF_HINT.search(t) and parse_amount(t)[0] is None:
        b = tool_briefing()
        lines = ["%s 的小结：" % b["date"],
                 "· 今天待办 %d 条，逾期 %d 条" % (len(b["todo_today"]), len(b["overdue"])),
                 "· 本周花了 %.2f 元（%d 笔）" % (b["week_total"], b["week_count"]),
                 "· 本月累计 %.2f 元" % b["month_total"]]
        if b["todo_today"]:
            lines.append("今天的：" + "；".join(x["title"] for x in b["todo_today"][:5]))
        return {"reply": "\n".join(lines), "actions": ["briefing"], "data": b}

    amount, _ = parse_amount(t)
    if amount is not None:
        cat = guess_category(t) or "其他"
        note = re.sub(r"(\d{1,7}(?:\.\d{1,2})?)\s*(?:元|块钱|块|rmb|RMB)?", "", t)
        note = re.sub(r"^(记一笔|记账|花了|我花了|今天|昨天|前天)", "", note.strip()).strip(" ，,。")
        note = note or cat
        day = parse_day(t)
        r = tool_record_expense(amount, cat, note, day)
        if not r["ok"]:
            return {"reply": r["error"], "actions": []}
        return {"reply": "记好了：%s %.2f 元 → %s（%s）" % (day, amount, cat, note),
                "actions": ["add_expense"], "data": r}

    return {"reply": "我没太听懂。可以试试：\n· 午饭 28\n· 提醒我明天 9 点交材料\n· 这个月花了多少\n· 今天待办\n输入「帮助」看全部用法。",
            "actions": [], "fallback": True}


HELP_TEXT = """我可以帮你做这些（离线也能用）：
· 记账：「午饭 28」「打车 35 元」「昨天买书 60」
· 查账：「这个月花了多少」「吃饭花了多少」
· 待办：「提醒我明天 9 点交材料」「周五下午 3 点开会」
· 查看：「今天待办」「今天怎么样」
· 收尾：「完成 交材料」「撤销上一笔」
· 导出：右上角「导出 CSV」
配了 DEEPSEEK_API_KEY 之后，说法更随意也能听懂。"""

# --------------------------------------------------------------------------
# 可选：大模型工具调用
# --------------------------------------------------------------------------

LLM_TOOLS = [
    {"type": "function", "function": {"name": "record_expense", "description": "记一笔支出",
     "parameters": {"type": "object", "properties": {
         "amount": {"type": "number"}, "category": {"type": "string", "enum": list(CATEGORIES) + ["其他"]},
         "note": {"type": "string"}, "day": {"type": "string", "description": "YYYY-MM-DD，默认今天"}},
         "required": ["amount"]}}},
    {"type": "function", "function": {"name": "stats", "description": "统计某月支出，可按分类过滤",
     "parameters": {"type": "object", "properties": {
         "month": {"type": "string", "description": "YYYY-MM"}, "category": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "add_task", "description": "添加一条待办/提醒",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string"}, "due_at": {"type": "string", "description": "YYYY-MM-DD HH:MM"}},
         "required": ["title"]}}},
    {"type": "function", "function": {"name": "list_tasks", "description": "列出未完成待办",
     "parameters": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["open", "today"]}}}}},
    {"type": "function", "function": {"name": "complete_task", "description": "把某条待办标记完成",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "title": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "briefing", "description": "今日概览：待办、本周花费、本月累计",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_expenses", "description": "列出最近支出",
     "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
]


def _llm_call(messages):
    body = json.dumps({"model": LLM_MODEL, "messages": messages, "tools": LLM_TOOLS,
                       "tool_choice": "auto", "temperature": 0.2}).encode("utf-8")
    req = urllib.request.Request(
        LLM_BASE + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _exec_tool(name: str, args: dict) -> dict:
    try:
        if name == "record_expense":
            return tool_record_expense(args.get("amount", 0), args.get("category", ""), args.get("note", ""), args.get("day", ""))
        if name == "stats":
            return tool_stats(args.get("month", ""), args.get("category", ""))
        if name == "add_task":
            return tool_add_task(args.get("title", ""), args.get("due_at", ""))
        if name == "list_tasks":
            return tool_list_tasks(args.get("scope", "open"))
        if name == "complete_task":
            return tool_complete_task(args.get("task_id", 0), args.get("title", ""))
        if name == "briefing":
            return tool_briefing()
        if name == "list_expenses":
            return tool_list_expenses(limit=args.get("limit", 10))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "unknown tool"}


def handle_with_llm(text: str) -> dict:
    """有 API Key 时优先让模型决定调用哪个工具；失败自动退回规则大脑。"""
    if not LLM_KEY:
        return handle(text)
    sys_prompt = (
        "你是「小管家」，一个本地生活助理。用户说中文。"
        "先判断意图：记账、查账、加待办、查待办、完成待办、要概览。"
        "能用工具就不要只聊天。金额只取用户明确说的数字。"
        "如果用户只是闲聊，用一句简短中文回复，不要调用工具。"
    )
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}]
    try:
        for _ in range(3):
            data = _llm_call(messages)
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"reply": (msg.get("content") or "").strip() or "……", "actions": ["llm"]}
            messages.append(msg)
            for c in calls:
                fn = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _exec_tool(fn, args)
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        return {"reply": "这件事我处理完了，你看看右侧是不是对上了。", "actions": ["llm"]}
    except Exception as exc:  # noqa: BLE001
        print("[llm] fallback:", exc, file=sys.stderr)
        return handle(text)


def think(text: str) -> dict:
    result = handle_with_llm(text) if LLM_KEY else handle(text)
    result["mode"] = "llm" if LLM_KEY else "offline"
    result["state"] = tool_briefing()
    return result


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "LifeAgent/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = (BASE_DIR / "static" / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json({"ok": True, "briefing": tool_briefing(), "mode": "llm" if LLM_KEY else "offline"})
        elif path == "/api/export.csv":
            data = tool_export_csv().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="life-expenses.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if path == "/api/chat":
            text = str(payload.get("text", ""))
            self._json(think(text))
        elif path == "/api/task/toggle":
            tid = int(payload.get("id") or 0)
            with db() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if not row:
                    self._json({"ok": False, "error": "not found"}, 404)
                    return
                conn.execute("UPDATE tasks SET done=?, done_at=? WHERE id=?",
                             (0 if row["done"] else 1, None if row["done"] else _now(), tid))
            self._json({"ok": True, "briefing": tool_briefing()})
        elif path == "/api/expense/delete":
            eid = int(payload.get("id") or 0)
            with db() as conn:
                conn.execute("DELETE FROM expenses WHERE id=?", (eid,))
            self._json({"ok": True, "briefing": tool_briefing()})
        else:
            self._json({"ok": False, "error": "not found"}, 404)


def serve():
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = "http://%s:%d" % (HOST, PORT)
    print("小管家已启动 → %s" % url)
    print("数据文件：%s" % DB_PATH)
    print("模式：%s" % ("大模型（DeepSeek）" if LLM_KEY else "离线规则（可设 DEEPSEEK_API_KEY 开启大模型）"))
    print("按 Ctrl+C 停止。")
    if os.environ.get("LIFEAGENT_NO_BROWSER") != "1":
        try:
            import webbrowser
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n再见。")


# --------------------------------------------------------------------------
# CLI / 自检
# --------------------------------------------------------------------------

def cli():
    init_db()
    print("小管家（终端模式）。输入「帮助」看用法，Ctrl+C 退出。")
    while True:
        try:
            line = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return
        if not line:
            continue
        if line in ("exit", "quit", "退出"):
            return
        r = think(line)
        print("小管家 > " + r["reply"])


def self_test() -> int:
    import tempfile
    global DATA_DIR, DB_PATH
    tmp = Path(tempfile.mkdtemp(prefix="lifeagent-test-"))
    DATA_DIR = tmp
    DB_PATH = tmp / "test.db"
    init_db()
    cases = [
        ("午饭 28", "add_expense"),
        ("打车 35 元", "add_expense"),
        ("昨天买书 60", "add_expense"),
        ("提醒我明天 9 点交材料", "add_task"),
        ("今天待办", "list_tasks"),
        ("这个月花了多少", "stats"),
        ("完成 交材料", "complete_task"),
        ("撤销上一笔", "undo_expense"),
    ]
    failed = 0
    for text, expect in cases:
        r = handle(text)
        ok = expect in r.get("actions", [])
        print(("  ok  " if ok else "  FAIL") + "  " + text + "  →  " + r["reply"].replace("\n", " | ")[:70])
        failed += 0 if ok else 1
    b = tool_briefing()
    print("  briefing: 本月 %.2f 元 / 待办 %d 条" % (b["month_total"], b["open_count"]))
    csv_text = tool_export_csv()
    print("  csv: %d 行" % len(csv_text.strip().splitlines()))
    print("自检结果：%s" % ("全部通过" if failed == 0 else "%d 条失败" % failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--cli" in sys.argv:
        cli()
    else:
        serve()
