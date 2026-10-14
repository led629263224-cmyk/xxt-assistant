"""纯解析函数：HTML/JSON -> 结构化数据。

本模块不联网、不依赖会话，便于单独测试（tests/test_parse.py）。
所有学习通页面都为服务端渲染，因此统一用正则抽取，并对关键位置给出
宽松的容错写法——页面小改版时尽量不直接崩掉。
"""

from __future__ import annotations

import base64
import hashlib
import html as html_mod
import re
from datetime import date, datetime, timedelta
from urllib.parse import unquote

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# 学习通登录参数加密用的固定 key/iv（前端硬编码值，2026-09 实测有效）
AES_KEY = AES_IV = b"u2oh6Vu^HWe4_AES"

# 视为"已经交过"的状态词
_DONE_MARKS = ("已完成", "已提交", "待批阅", "已批阅", "已批改", "已交卷", "已评分")
# 视为"考试已参加"的状态词
_EXAM_DONE_MARKS = ("已提交", "已交卷", "已完成", "已批阅", "已批改", "已评分")


# ------------------------------------------------------------------ 加解密

def aes_encrypt(text: str) -> str:
    """AES-128-CBC + PKCS7 + base64（学习通登录表单的 uname/password 格式）。"""
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    return base64.b64encode(cipher.encrypt(pad(text.encode("utf-8"), 16))).decode()


def aes_decrypt(token: str) -> str:
    """aes_encrypt 的逆运算（仅测试用）。"""
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    return unpad(cipher.decrypt(base64.b64decode(token)), 16).decode("utf-8")


# ------------------------------------------------------------------ HTML 小工具

def strip_tags(text: str) -> str:
    """去掉 script/style/标签，反转义实体并压缩空白。"""
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    # 先反转义再压缩空白：否则 &nbsp; 会变成 \xa0 混在文本里
    return " ".join(html_mod.unescape(text).split())


def hidden_value(page_html: str, input_id: str) -> str:
    """取 <input type="hidden" id="X" value="Y"> 的 value（属性顺序两种都试）。"""
    page_html = page_html or ""
    patterns = (
        r'id="%s"[^>]*?value="([^"]*)"' % re.escape(input_id),
        r'value="([^"]*)"[^>]*?id="%s"' % re.escape(input_id),
        r"id='%s'[^>]*?value='([^']*)'" % re.escape(input_id),
    )
    for pat in patterns:
        m = re.search(pat, page_html)
        if m:
            return m.group(1)
    return ""


def _first_tag_text(block: str, class_word: str) -> str:
    """取块内 class 含指定词的第一个标签的纯文本。"""
    m = re.search(r'<[^>]*class="[^"]*\b%s\b[^"]*"[^>]*>([\s\S]*?)<' % class_word,
                  block)
    if not m:
        m = re.search(r'<[^>]*class="[^"]*\b%s\b[^"]*"[^>]*>([^<]*)' % class_word,
                      block)
    return strip_tags(m.group(1)) if m else ""


# ------------------------------------------------------------------ 课程

def parse_courses(html: str) -> list[dict]:
    """解析课程列表页，返回 [{name, teacher, courseid, clazzid, cpi}]，按出现顺序去重。"""
    courses: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for m in re.finditer(
            r"stucoursemiddle\?courseid=(\d+)&(?:amp;)?clazzid=(\d+)&(?:amp;)?cpi=(\d+)",
            html or ""):
        cid, clazz, cpi = m.groups()
        if (cid, clazz, cpi) in seen:
            continue
        seen.add((cid, clazz, cpi))
        seg = html[m.end():m.end() + 2000]
        nm = re.search(r'course-name[^>]*title="([^"]+)"', seg) or \
            re.search(r'title="([^"]+)"[^>]*class="[^"]*course-name', seg)
        teacher = ""
        for pm in re.finditer(r'<p class="[^"]*overHidden2[^"]*"[^>]*>([^<]{1,20})</p>', seg):
            txt = pm.group(1).strip()
            if txt and txt != (nm.group(1) if nm else ""):
                teacher = txt
                break
        courses.append({
            "name": html_mod.unescape(nm.group(1)) if nm else "未命名课程",
            "teacher": html_mod.unescape(teacher),
            "courseid": cid,
            "clazzid": clazz,
            "cpi": cpi,
        })
    return courses


# ------------------------------------------------------------------ 作业

def parse_homework_list(html: str) -> dict:
    """解析作业列表页，返回 {progress: (done,total)|None, items: [...]}。

    item: {index, title, status, url}
    """
    html = html or ""
    progress = None
    for pat in (r"<span>(\d+)\s*/\s*(\d+)</span>\s*</div>\s*</div>",
                r"已完成[^0-9]{0,12}(\d+)\s*/\s*(\d+)"):
        m = re.search(pat, html)
        if m:
            progress = (int(m.group(1)), int(m.group(2)))
            break

    items: list[dict] = []
    for li in re.finditer(r"<li\b([^>]*)>([\s\S]*?)</li>", html):
        attrs, block = li.group(1), li.group(2)
        if "goTask" not in attrs or "data=" not in attrs:
            continue
        m_url = re.search(r'data="([^"]+)"', attrs)
        if not m_url:
            continue
        title = _first_tag_text(block, "overHidden2")
        status = _first_tag_text(block, "status")
        if not title:
            title = strip_tags(block)[:60]
        items.append({
            "index": len(items) + 1,
            "title": title or "未命名作业",
            "status": status,
            "url": unquote(html_mod.unescape(m_url.group(1))),
        })
    return {"progress": progress, "items": items}


def parse_homework_detail(html: str) -> dict:
    """从作业详情页取 题量/满分/作答起止时间/得分/标题。

    不同题型页面结构有差异，取不到的字段留 None，不抛异常。
    """
    text = strip_tags(html)
    out = {"title": None, "question_count": None, "full_score": None,
           "start_time": None, "deadline": None, "score": None}

    m = re.search(r"题量[:：]", text)
    if m:
        # 标题夹在"作业详情"和"题量"之间
        k = text[:m.start()].rfind("作业详情")
        if k >= 0:
            title = text[k + len("作业详情"):m.start()].strip(" ：:·-")
            if title:
                out["title"] = title

    m = re.search(r"题量[:：]\s*(\d+)", text)
    if m:
        out["question_count"] = int(m.group(1))
    m = re.search(r"满分[:：]\s*(\d+(?:\.\d+)?)", text)
    if m:
        out["full_score"] = m.group(1)

    m = re.search(r"作答时间[:：]\s*(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})\s*至\s*"
                  r"(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", text)
    if m:
        out["start_time"], out["deadline"] = m.group(1), m.group(2)
    if not out["deadline"]:
        m = re.search(r"(?:截止时间|结束时间)[:：]\s*(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", text)
        if m:
            out["deadline"] = m.group(1)

    m = (re.search(r"智能分析\s*(\d+(?:\.\d+)?)\s*分", text)
         or re.search(r"(?:得分|总评|成绩|批阅)[:：]?\s*(\d+(?:\.\d+)?)\s*分", text))
    if m:
        out["score"] = m.group(1)
    return out


def is_homework_pending(status: str) -> bool:
    """作业是否仍需关注：已交/待批阅/已批阅都算完成，其余（含空状态）算未完成。"""
    s = (status or "").strip()
    return not any(mark in s for mark in _DONE_MARKS)


def work_id_from_url(url: str) -> str:
    """从作业详情直链里抽稳定的 workId（用于跨次同步识别同一份作业）。"""
    m = re.search(r"[?&]workId=(\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&]workId=([0-9a-zA-Z]+)", url or "")
    if m:
        return m.group(1)
    return ""


def assignment_key(courseid: str, clazzid: str, url: str, title: str) -> str:
    """作业的稳定主键：优先 workId，其次 title 摘要。"""
    wid = work_id_from_url(url)
    if wid:
        return f"{courseid}:{clazzid}:{wid}"
    digest = hashlib.md5(f"{courseid}:{title}".encode("utf-8")).hexdigest()[:12]
    return f"{courseid}:{clazzid}:t{digest}"


# ------------------------------------------------------------------ 时间

def nearest_datetime_for(month_day: str, today: date) -> datetime | None:
    """把只有 "MM-DD HH:MM"、没有年份的时间补成完整 datetime。

    取去年/今年/明年里离今天最近的那个年份，这样 12 月留的作业 1 月截止
    也能排对顺序。解析失败返回 None。
    """
    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", str(month_day or "").strip())
    if not m:
        return None
    mo, day, hh, mm = map(int, m.groups())
    best: datetime | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cand = datetime(year, mo, day, hh, mm)
        except ValueError:
            continue
        if best is None or abs((cand.date() - today).days) < abs((best.date() - today).days):
            best = cand
    return best


def parse_full_datetime(text: str) -> datetime | None:
    """解析带年份的时间（考试时间列通常是 '2026-03-20 14:00~16:00'）。"""
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})",
                  str(text or ""))
    if not m:
        return None
    y, mo, d, hh, mi = map(int, m.groups())
    try:
        return datetime(y, mo, d, hh, mi)
    except ValueError:
        return None


def humanize_delta(delta: timedelta | None) -> str:
    """把剩余时间说成人话：'还有 3 天 5 小时' / '已过期 2 小时'。"""
    if delta is None:
        return "截止未知"
    seconds = int(delta.total_seconds())
    overdue = seconds < 0
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days >= 1:
        text = f"{days} 天" + (f" {hours} 小时" if hours else "")
    elif hours >= 1:
        text = f"{hours} 小时" + (f" {minutes} 分" if minutes and hours < 6 else "")
    else:
        text = f"{minutes} 分钟" if minutes else "不到 1 分钟"
    return f"已过期 {text}" if overdue else f"还有 {text}"


# ------------------------------------------------------------------ 考试

def parse_exams(html: str) -> list[dict]:
    """解析考试列表页的 <tr class="dataTr"> 表格。

    列序（2026-09 实测）：编号 | 名称 | 时间 | 时长(min) | 考试状态 | 作答状态 | 分数 | 方式
    """
    rows: list[dict] = []
    for raw in re.findall(r'<tr[^>]*class="[^"]*dataTr[^"]*"[^>]*>([\s\S]*?)</tr>',
                          html or ""):
        tds = [strip_tags(td) for td in
               re.findall(r"<td[^>]*>([\s\S]*?)</td>", raw)]
        if len(tds) < 8:
            continue
        name = tds[1]
        rows.append({
            "index": tds[0],
            "name": name,
            "course": _course_from_exam_name(name),
            "exam_time": tds[2],
            "duration": tds[3],
            "exam_status": tds[4],
            "answer_status": tds[5],
            "score": tds[6],
            "method": tds[7],
            "start_dt": parse_full_datetime(tds[2]),
        })
    return rows


def _course_from_exam_name(name: str) -> str:
    """考试名称通常形如 '《数字电路》期中考试'，尝试抽出课程名。"""
    m = re.search(r"[《【\[]([^》】\]]+)[》】\]]", name or "")
    return m.group(1) if m else ""


def is_exam_unfinished(exam: dict) -> bool:
    """考试是否"还没完成"：作答状态未表示已交卷即算未完成。"""
    s = (exam.get("answer_status") or "").strip()
    return not any(mark in s for mark in _EXAM_DONE_MARKS)


def exam_key(exam: dict) -> str:
    """考试的稳定主键（名称+时间+编号）。"""
    raw = f"{exam.get('name','')}|{exam.get('exam_time','')}|{exam.get('index','')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 通知

def parse_notices(payload: dict) -> dict:
    """解析通知接口 JSON，返回 {items, last_id, last_page}。"""
    data = (payload or {}).get("notices") or {}
    items = []
    for it in data.get("list") or []:
        title = (it.get("title") or "").strip()
        content = (it.get("content") or "").strip()
        nid = str(it.get("id") or it.get("noticeId") or "")
        if not nid:
            nid = hashlib.md5(f"{title}|{it.get('completeTime','')}".encode("utf-8")
                              ).hexdigest()[:16]
        items.append({
            "notice_id": nid,
            "title": title or "(无标题)",
            "content": content,
            "sender": (it.get("createrName") or "").strip(),
            "publish_time": str(it.get("completeTime") or ""),
            "isread": bool(it.get("isread")),
        })
    return {
        "items": items,
        "last_id": data.get("lastGetId") or "",
        "last_page": bool(data.get("lastPage")),
    }
