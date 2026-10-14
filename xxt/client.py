"""学习通 Web 端只读接口客户端。

全部请求都是查询语义（GET/POST 均为只读表单），不提交任何作业、不参加考试、
不刷课。请求之间强制限速，避免触发风控。

协议细节来自对学习通网页端的实测逆向（参考公开项目 Xuexitong-mcp 的协议结论，
代码为本项目自行实现）：
    * 登录：POST passport2.chaoxing.com/fanyalogin，uname/password 为
      AES-128-CBC(PKCS7) + base64，key=iv=b"u2oh6Vu^HWe4_AES"
    * 作业列表的 enc 必须用课程中间页隐藏域 workEnc（页面通用 enc 不通用），
      且必须带 t 与 stuenc 伴随参数
    * 作业截止时间只存在于详情页文本"作答时间: MM-DD HH:MM 至 MM-DD HH:MM"
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from . import config
from .parse import (aes_encrypt, hidden_value, parse_courses, parse_exams,
                    parse_homework_detail, parse_homework_list, parse_notices,
                    strip_tags)

log = logging.getLogger("xxt.client")

TIMEOUT = 20.0
# 课程页签名能缓存多久（签名只跟会话绑定，缓存久一点可以省掉一半请求）
SIGNATURE_TTL = 6 * 3600.0
# 课程页签名落盘位置（换会话自动作废，见 _cookie_fingerprint）
SIGNATURE_PATH = config.DATA_DIR / "signatures.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

URL_LOGIN = "https://passport2.chaoxing.com/fanyalogin"
URL_BASE = "https://i.chaoxing.com/base"
URL_COURSELIST = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
URL_STUMIDDLE = "https://mooc1.chaoxing.com/visit/stucoursemiddle"
URL_WORKLIST = "https://mooc1.chaoxing.com/mooc2/work/list"
URL_EXAMLIST = "https://mooc1-api.chaoxing.com/exam-ans/exam/test/examcode/examlist"
URL_NOTICELIST = "https://notice.chaoxing.com/pc/notice/getNoticeList"
URL_HOME = "https://i.chaoxing.com"

LOGIN_MARKERS = ("passport2.chaoxing.com/login", "用户登录")
NO_PERMISSION_MARKER = "没有此页面访问权限"


class XxtError(Exception):
    """采集层通用异常。"""


class NotLoggedIn(XxtError):
    """登录态失效且无法自动登录。"""


class EncMissing(XxtError):
    """课程页没有生成作业/考试签名，通常代表该课未开放此功能。"""


class _SignatureExpired(XxtError):
    """缓存下来的课程签名（workEnc/enc）服务端已经不认了，需要重拉课程页。"""


@dataclass
class LoginResult:
    ok: bool
    msg: str = ""


class ChaoxingClient:
    """学习通只读客户端。"""

    def __init__(self, request_delay: float = 0.7, timeout: float = TIMEOUT):
        self.timeout = timeout
        self.request_delay = max(0.0, request_delay)
        self._last_request_at = 0.0
        self._login_checked_at = 0.0
        self._courses_cache: list[dict] | None = None
        self._courses_at = 0.0
        self._hw_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._work_enc_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self.session = self._build_session()
        self.load_signature_cache()

    # -------------------------------------------------- 会话

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": URL_HOME + "/",
        })
        retry = Retry(total=2, backoff_factor=0.6,
                      status_forcelist=(429, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "POST"]))
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        config.ensure_dirs()
        if config.COOKIE_PATH.exists():
            try:
                s.cookies.update(json.loads(config.COOKIE_PATH.read_text("utf-8")))
            except Exception:
                pass
        return s

    def save_cookies(self) -> None:
        try:
            config.ensure_dirs()
            jar = {c.name: c.value for c in self.session.cookies}
            config.COOKIE_PATH.write_text(json.dumps(jar, ensure_ascii=False),
                                          encoding="utf-8")
        except Exception as exc:  # pragma: no cover
            log.warning("保存 cookie 失败：%s", exc)

    def clear_cookies(self) -> None:
        self.session.cookies.clear()
        config.COOKIE_PATH.unlink(missing_ok=True)
        SIGNATURE_PATH.unlink(missing_ok=True)   # 签名跟着会话走，一起作废

    def reset_caches(self) -> None:
        """丢弃课程列表、作业列表与课程页签名缓存。"""
        self._courses_cache = None
        self._courses_at = 0.0
        self._hw_cache.clear()
        self._work_enc_cache.clear()

    # -------------------------------------------------- 签名缓存落盘

    def _cookie_fingerprint(self) -> str:
        """cookie 指纹：换账号/重新登录后签名就不能再用了。

        只用"名字=值"：cookie 存盘时不带 domain/path，带上的话
        存下来的指纹和重新加载后的指纹永远对不上。
        """
        jar = {c.name: c.value for c in self.session.cookies}
        blob = "\n".join(f"{k}={jar[k]}" for k in sorted(jar))
        return hashlib.md5(blob.encode("utf-8")).hexdigest()

    def load_signature_cache(self) -> int:
        """从磁盘恢复课程页签名，让程序重启后的第一趟同步也不用重拉课程页。

        只在 cookie 没变、且没超过 SIGNATURE_TTL 时使用；服务端不认的话
        fetch_homework_list 会自己重拉课程页兜底。
        """
        if not SIGNATURE_PATH.exists():
            return 0
        try:
            data = json.loads(SIGNATURE_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            return 0
        if data.get("fingerprint") != self._cookie_fingerprint():
            return 0
        if time.time() - float(data.get("saved_at") or 0) > SIGNATURE_TTL:
            return 0
        items = data.get("items") or {}
        now = time.time()
        for raw_key, sig in items.items():
            courseid, _, clazzid = str(raw_key).partition("|")
            if courseid and clazzid and isinstance(sig, dict):
                self._work_enc_cache[(courseid, clazzid)] = (now, dict(sig))
        if items:
            log.info("复用上次的课程签名 %d 门（省掉重拉课程页）", len(items))
        return len(items)

    def save_signature_cache(self) -> None:
        try:
            config.ensure_dirs()
            items = {}
            for (courseid, clazzid), (_at, sig) in self._work_enc_cache.items():
                # 没签到作业签名的课不落盘：下次还是重拉一次课程页，
                # 免得"某一刻取不到"被记 6 小时
                if sig and sig.get("workEnc"):
                    items[f"{courseid}|{clazzid}"] = sig
            payload = {
                "fingerprint": self._cookie_fingerprint(),
                "saved_at": time.time(),
                "items": items,
            }
            tmp = SIGNATURE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, SIGNATURE_PATH)
        except Exception as exc:  # pragma: no cover
            log.warning("保存课程签名失败：%s", exc)

    # -------------------------------------------------- 请求

    def _throttle(self) -> None:
        """请求限速：保证两次请求之间至少间隔 request_delay（带 ±30% 抖动）。"""
        if self.request_delay <= 0:
            return
        gap = self.request_delay * random.uniform(0.7, 1.3)
        wait = gap - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.time()

    def _raw_get(self, url: str, **kw) -> requests.Response:
        self._throttle()
        kw.setdefault("timeout", self.timeout)
        return self.session.get(url, **kw)

    def _raw_post(self, url: str, **kw) -> requests.Response:
        self._throttle()
        kw.setdefault("timeout", self.timeout)
        return self.session.post(url, **kw)

    @staticmethod
    def _looks_like_login(resp: requests.Response) -> bool:
        head = resp.text[:4000] if resp.text else ""
        return any(marker in resp.url or marker in head for marker in LOGIN_MARKERS)

    # -------------------------------------------------- 登录

    def login(self, phone: str | None = None, password: str | None = None
              ) -> LoginResult:
        """账号密码登录；成功后持久化 cookie。"""
        if not phone or not password:
            creds = config.load_credentials()
            if not creds:
                return LoginResult(False, "本地没有保存账号密码，请先登录一次")
            phone, password = creds
        try:
            r = self._raw_post(URL_LOGIN, data={
                "fid": "-1",
                "uname": aes_encrypt(phone),
                "password": aes_encrypt(password),
                "refer": "https%3A%2F%2Fi.chaoxing.com",
                "t": "true",
                "forbidotherlogin": "0",
                "validate": "",
                "doubleFactorLogin": "0",
                "independentId": "0",
            }, headers={"Referer": "https://passport2.chaoxing.com/login"})
        except requests.RequestException as exc:
            return LoginResult(False, f"网络错误：{exc}")
        try:
            body = r.json()
        except Exception:
            return LoginResult(False, "登录响应异常：" + strip_tags(r.text)[:120])
        if body.get("status") is True:
            self.save_cookies()
            self.reset_caches()          # 新会话意味着旧签名不能再用
            self._login_checked_at = time.time()
            return LoginResult(True, body.get("msg2") or "登录成功")
        msg = body.get("msg2") or body.get("msg") or "账号或密码错误（也可能需要验证码）"
        return LoginResult(False, str(msg))

    def check_login(self, force: bool = False) -> bool:
        """探测 cookie 是否仍有效（结果缓存 5 分钟）。"""
        if not force and time.time() - self._login_checked_at < 300:
            return True
        try:
            r = self._raw_get(URL_BASE, allow_redirects=True)
            if not self._looks_like_login(r):
                self._login_checked_at = time.time()
                return True
        except requests.RequestException as exc:
            log.warning("登录态探测失败：%s", exc)
            return False
        return False

    def ensure_login(self, force: bool = False) -> LoginResult:
        """cookie 有效就直接用，否则用本地保存的凭据重新登录。"""
        if self.check_login(force=force):
            return LoginResult(True, "登录态有效")
        res = self.login()
        if res.ok:
            return res
        return LoginResult(False, "未登录且自动登录失败：" + res.msg)

    def _get(self, url: str, **kw) -> requests.Response:
        """带一次自动重登的 GET。"""
        try:
            r = self._raw_get(url, **kw)
        except requests.TooManyRedirects as exc:
            # 会话被顶掉时学习通会在登录页和 /base 之间来回跳
            log.warning("跳转异常，尝试重新登录：%s", url)
            if not self.ensure_login(force=True).ok:
                raise NotLoggedIn("会话失效且重新登录失败，请在设置里重新登录") from exc
            r = self._raw_get(url, **kw)
        if self._looks_like_login(r) and self.ensure_login(force=True).ok:
            r = self._raw_get(url, **kw)
        return r

    def _post(self, url: str, **kw) -> requests.Response:
        try:
            r = self._raw_post(url, **kw)
        except requests.TooManyRedirects as exc:
            log.warning("跳转异常，尝试重新登录：%s", url)
            if not self.ensure_login(force=True).ok:
                raise NotLoggedIn("会话失效且重新登录失败，请在设置里重新登录") from exc
            r = self._raw_post(url, **kw)
        if self._looks_like_login(r) and self.ensure_login(force=True).ok:
            r = self._raw_post(url, **kw)
        return r

    # -------------------------------------------------- 个人与课程

    def fetch_profile(self) -> dict:
        """姓名 / 学校 / fid。"""
        base = self._get(URL_BASE).text
        name = ""
        m = (re.search(r'aria-label="账号：([^"<]{1,20})"', base)
             or re.search(r"账号：([^<\s]{1,20})", base))
        if m:
            name = m.group(1)
        school = ""
        m = re.search(r'id="siteName"[^>]*title="([^"]+)"', base)
        if m:
            school = m.group(1)
        fid = ""
        m = (re.search(r'"fid"\s*:\s*"?(\d+)"?', base)
             or re.search(r"var fid = '(\d+)'", base))
        if m:
            fid = m.group(1)
        return {"name": name, "school": school, "fid": fid}

    def fetch_courses(self, refresh: bool = False) -> list[dict]:
        """我学的全部课程（带 5 分钟缓存）。"""
        now = time.time()
        if not refresh and self._courses_cache is not None and now - self._courses_at < 300:
            return self._courses_cache
        base = self._get(URL_BASE).text
        m = (re.search(r'"fid"\s*:\s*"?(\d+)"?', base)
             or re.search(r"var fid = '(\d+)'", base))
        fid = m.group(1) if m else "427"
        r = self._get(URL_COURSELIST, params={
            "courseType": 1, "courseFid": fid,
            "courseTeacherid": 0, "courseOrderBy": 0})
        courses = parse_courses(r.text)
        self._courses_cache = courses
        self._courses_at = now
        return courses

    def fetch_course_page(self, course: dict) -> str:
        """课程中间页（含 enc/openc/workEnc/examEnc 等隐藏域）。"""
        return self._get(URL_STUMIDDLE, params={
            "courseid": course["courseid"],
            "clazzid": course["clazzid"],
            "cpi": course["cpi"],
            "ismooc2": 1,
            "v": 2,
        }).text

    def course_signatures(self, course: dict, refresh: bool = False) -> dict:
        """取课程页的签名参数。

        签名只跟会话有关，可以缓存很久 —— 这样每轮同步每门课只要 1 个请求
        （作业列表），而不是"中间页 + 作业列表" 2 个，49 门课能省一半时间。
        万一服务端不认了，fetch_homework_list 会强制刷新重试一次。
        """
        key = (course["courseid"], course["clazzid"])
        now = time.time()
        cached = self._work_enc_cache.get(key)
        if not refresh and cached and now - cached[0] < SIGNATURE_TTL:
            return cached[1]
        page = self.fetch_course_page(course)
        sig = {
            "enc": hidden_value(page, "enc"),
            "openc": hidden_value(page, "openc"),
            "oldenc": hidden_value(page, "oldenc"),
            "workEnc": hidden_value(page, "workEnc"),
            "examEnc": hidden_value(page, "examEnc"),
        }
        self._work_enc_cache[key] = (now, sig)
        return sig

    # -------------------------------------------------- 作业

    def fetch_homework_list(self, course: dict, refresh: bool = False) -> dict:
        """某门课的作业列表：{progress, items:[{index,title,status,url}]}。

        缓存 2 分钟；refresh=True 强制刷新。
        """
        key = (course["courseid"], course["clazzid"])
        now = time.time()
        cached = self._hw_cache.get(key)
        if not refresh and cached and now - cached[0] < 120:
            return cached[1]

        try:
            data = self._fetch_work_list(course)
        except _SignatureExpired:
            log.info("课程签名失效，重拉课程页：%s", course.get("name") or key)
            data = self._fetch_work_list(course, refresh_sig=True)
        self._hw_cache[key] = (now, data)
        return data

    def _fetch_work_list(self, course: dict, refresh_sig: bool = False) -> dict:
        sig = self.course_signatures(course, refresh=refresh_sig)
        if not sig["workEnc"]:
            raise EncMissing("课程页未生成作业签名（该课可能未开放作业）")
        r = self._get(URL_WORKLIST, params={
            "courseId": course["courseid"],
            "classId": course["clazzid"],
            "cpi": course["cpi"],
            "ut": "s",
            "openc": sig["openc"],
            "enc": sig["workEnc"],
            "t": int(time.time() * 1000),
            "stuenc": sig["enc"],
            "isdisplaytable": 2,
        })
        html = r.text
        if NO_PERMISSION_MARKER in html:
            raise _SignatureExpired("作业页签名校验失败（enc 已过期）")
        return parse_homework_list(html)

    def fetch_homework_detail(self, url: str) -> dict:
        """作业详情（题量/满分/作答起止时间/得分）。url 来自列表条目的 data 属性。"""
        if not url or not url.startswith("http"):
            raise XxtError("该作业没有可用的详情链接")
        return parse_homework_detail(self._get(url).text)

    # -------------------------------------------------- 考试

    def fetch_exams(self) -> list[dict]:
        """全校考试安排（跨课程汇总）。"""
        html = self._get(URL_EXAMLIST).text
        return parse_exams(html)

    # -------------------------------------------------- 通知

    def fetch_notices(self, limit: int = 30, max_pages: int = 4) -> list[dict]:
        """通知中心列表（游标翻页，content 字段就是正文全文）。"""
        items: list[dict] = []
        last_id = ""
        for _ in range(max(1, max_pages)):
            r = self._post(URL_NOTICELIST, data={
                "type": 0, "notice_type": 0, "lastValue": last_id, "sort": 0,
                "folderUUID": "", "kw": "", "startTime": "", "endTime": "",
                "gKw": "", "gName": "", "year": "", "tag": "", "fidsCode": "",
                "filterSenderPuids": "", "filterTags": "",
            }, headers={"Referer": "https://notice.chaoxing.com/pc/notice/myNotice"})
            try:
                payload = r.json()
            except Exception as exc:
                raise XxtError("通知接口返回的不是 JSON：" + strip_tags(r.text)[:120]) from exc
            if not payload.get("status"):
                raise XxtError("通知接口返回失败：" + str(payload.get("msg") or "")[:120])
            page = parse_notices(payload)
            items.extend(page["items"])
            last_id = page["last_id"]
            if not page["items"] or page["last_page"] or len(items) >= limit:
                break
        return items[:limit]

    # -------------------------------------------------- 调试

    def dump(self, kind: str, course: dict | None = None) -> str:
        """把原始响应存到 data/dump/，页面改版时用来定位解析问题。"""
        out_dir = config.DATA_DIR / "dump"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if kind == "courses":
            content = self._get(URL_COURSELIST, params={
                "courseType": 1, "courseFid": "427"}).text
        elif kind == "coursepage":
            if not course:
                raise XxtError("需要指定课程")
            content = self.fetch_course_page(course)
        elif kind == "worklist":
            if not course:
                raise XxtError("需要指定课程")
            sig = self.course_signatures(course, refresh=True)
            content = self._get(URL_WORKLIST, params={
                "courseId": course["courseid"], "classId": course["clazzid"],
                "cpi": course["cpi"], "ut": "s", "openc": sig["openc"],
                "enc": sig["workEnc"], "t": int(time.time() * 1000),
                "stuenc": sig["enc"], "isdisplaytable": 2}).text
        elif kind == "exams":
            content = self._get(URL_EXAMLIST).text
        elif kind == "notices":
            content = json.dumps(self._post(URL_NOTICELIST, data={
                "type": 0, "notice_type": 0, "lastValue": "", "sort": 0,
                "folderUUID": "", "kw": "", "startTime": "", "endTime": "",
                "gKw": "", "gName": "", "year": "", "tag": "", "fidsCode": "",
                "filterSenderPuids": "", "filterTags": ""}).json(),
                ensure_ascii=False, indent=2)
        else:
            raise XxtError(f"未知的 dump 类型：{kind}")
        path = out_dir / f"{kind}-{stamp}.html"
        path.write_text(content, encoding="utf-8")
        return str(path)
