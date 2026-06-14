"""
学习通作业/考试桌面通知小工具（GUI 版）
=====================================
功能：扫码学习通课程中的未完成作业/考试任务，GUI 界面展示
原理：基于逆向的学习通 API（mooc1/mooc2 系列接口）

依赖：requests, pycryptodome（内置：tkinter）
用法：python -m xxt_gui  或  start.bat
"""

from pathlib import Path
import json
import time
import re
import sys
import logging
import threading
import winreg  # Windows 注册表操作（用于开机自启管理）
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import base64


# ============================================================
# 配置区
# ============================================================

# 默认轮询间隔（秒），可由 config.json 的 poll_interval 覆盖
POLL_INTERVAL = 300  # 5 分钟

# 默认配置模板
DEFAULT_CONFIG = {
    "phone": "",                    # 手机号
    "password": "",                 # 密码
    "cookies": [],                  # 已保存的 cookies 会话
    "poll_interval": 300,           # 轮询间隔（秒）
    "only_courses": [],             # 课程白名单：空列表=监控所有课程；非空=只监控指定课程名称
    "max_workers": 8,               # 并发扫描线程数
    "rate_limit_delay": 1.0,        # 单课程请求间隔（秒）
    "token_cooldown": 60,        # Token 触发反爬虫后的冷却时间（秒）
    "hide_no_deadline": False,      # 是否隐藏无截止时间的任务（部分课程仅列表不显示截止时间）
}

# API 端点
LOGIN_URL = "https://passport2.chaoxing.com/fanyalogin"
NOTICE_COUNT_URL = "https://i.chaoxing.com/base/getNoticeCount"
COURSE_LIST_URL = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
COURSE_DETAIL_URL = "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
SIGN_IN_URL = "https://mobilelearn.chaoxing.com/pptSign/stuSignajax"
ACTIVE_LIST_URL = "https://mobilelearn.chaoxing.com/v2/apis/active/student/activelist"
# 章节任务卡片（mArg.attachments 含视频/作业/考试）
CHAPTER_CARDS_URL = "https://mooc1.chaoxing.com/knowledge/cards"
# 作业页与考试页（用 workid 区分类型：先试 /exam/test，404 则为 /work/doHomeWorkNew）
WORK_HOMEWORK_URL = "https://mooc1.chaoxing.com/work/doHomeWorkNew"
EXAM_TEST_URL = "https://mooc1.chaoxing.com/exam/test"

# AES 加密密钥（从 chaoxing 项目逆向得出）
AES_KEY = "u2oh6Vu^HWe4_AES"

# 文件路径
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

# ============================================================
# 日志系统
# ============================================================

# 全局日志记录器
LOG_FILE = BASE_DIR / "xxt_notifier.log"

def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """
    配置日志系统：同时输出到控制台和文件 xxt_notifier.log
    被 main() 和 GUI 入口调用，全局只需初始化一次
    """
    logger = logging.getLogger("xxt")
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    logger.setLevel(level)

    # 文件处理器（UTF-8，保留详细日志）
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    # 控制台处理器（简单格式，无时间戳）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


logger = setup_logging()


# HTTP 请求头
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

XHR_HEADERS = {
    **HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
}


# ============================================================
# 加密工具
# ============================================================

def aes_encrypt(plaintext: str) -> str:
    """
    AES/CBC/PKCS7 加密，输出 Base64 字符串
    被 passport2.chaoxing.com/fanyalogin 登录接口调用
    密钥来自 chaoxing 项目（Samueli924/chaoxing）逆向结果
    """
    key = AES_KEY.encode("utf-8")
    iv = key  # IV 与密钥相同
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plaintext.encode("utf-8"), AES.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("utf-8")


# ============================================================
# HTML 解析工具
# ============================================================

class CourseListParser(HTMLParser):
    """
    解析课程列表 HTML（courselistdata 接口返回的 HTML），提取课程信息。
    实际页面结构（学习通 mooc2-ans）：
        <div class="course clearfix learnCourse stu_146920689"
             id="c_249925713" info="146920689_406147479" roleId="stu_146920689">
            <input class="clazzId" value="146920689"/>
            <input class="courseId" value="249925713"/>
            <a href="...?cpi=406147479&...">...</a>
            <span class="course-name ..." title="机器学习">机器学习</span>
            <p class="line2 color3" title="高山">高山</p>
        </div>

    说明：HTMLParser 对部分嵌套或 class 多值标签解析有边界情况，
    这里用 HTMLParser 做框架，遇到关键字段时回退用正则精确提取。
    """

    def __init__(self):
        super().__init__()
        self.courses = []
        self._current = {}
        self._capturing_title = False
        self._capturing_teacher = False
        self._title_buf = []
        self._teacher_buf = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls_attr = attrs_dict.get("class") or ""
        cls = cls_attr.split() if isinstance(cls_attr, str) else []

        # 外层课程 div（class 包含 "course"）
        if tag == "div" and "course" in cls and self._current.get("courseId") is None:
            div_id = attrs_dict.get("id") or ""
            course_id = div_id[2:] if div_id.startswith("c_") else ""
            self._current = {
                "id": div_id,
                "info": attrs_dict.get("info", "") or "",
                "roleId": attrs_dict.get("roleId", "") or "",
                "courseId": course_id,
            }

        elif tag == "input":
            val = attrs_dict.get("value", "") or ""
            if "courseId" in cls:
                self._current["courseId"] = val
            elif "clazzId" in cls:
                self._current["clazzId"] = val

        elif tag == "a":
            href_attr = attrs_dict.get("href")
            if isinstance(href_attr, str):
                # 从 href 提取 cpi 参数
                import urllib.parse as _u
                parsed = _u.urlparse(href_attr)
                qs = _u.parse_qs(parsed.query)
                cpi_vals = qs.get("cpi") or []
                if cpi_vals:
                    self._current["cpi"] = cpi_vals[0]

        elif tag == "span" and any("course-name" in c for c in cls):
            self._capturing_title = True
            self._title_buf = []
            t = attrs_dict.get("title", "") or ""
            if t:
                self._title_buf.append(t)

        elif tag == "p" and "color3" in cls:
            self._capturing_teacher = True
            self._teacher_buf = []
            t = attrs_dict.get("title", "") or ""
            if t:
                self._teacher_buf.append(t)

    def handle_data(self, data):
        if self._capturing_title:
            s = data.strip()
            if s:
                self._title_buf.append(s)
        elif self._capturing_teacher:
            s = data.strip()
            if s:
                self._teacher_buf.append(s)

    def handle_endtag(self, tag):
        if tag == "span" and self._capturing_title:
            self._current["title"] = "".join(self._title_buf).strip()
            self._capturing_title = False
        elif tag == "p" and self._capturing_teacher:
            self._current["teacher"] = "".join(self._teacher_buf).strip()
            self._capturing_teacher = False
        elif tag == "div" and self._current.get("courseId"):
            self.courses.append(self._current)
            self._current = {}

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def get_courses(self) -> list:
        """
        返回解析结果。如果 HTMLParser 漏掉某些字段（如 title/teacher），
        用正则从原始 HTML 中再补一次。
        """
        # 暂存原始 HTML 通过 feed 传入，HTMLParser 不保留原始 HTML。
        # 解析器需要的回退提取在 parse_fallback 中实现（见模块级函数）。
        return self.courses


# --- 模块级辅助：正则回退解析（HTMLParser 漏字段时使用） ---

def _decode_entities(s: str) -> str:
    """解码常见 HTML 实体（&nbsp;、&amp;、&#39; 等）"""
    import html as _html
    return _html.unescape(s)


def parse_courses_from_html(html: str) -> list:
    """
    用正则从课程列表 HTML 中提取课程信息。
    每个课程块形如：
        <div class="course clearfix  learnCourse stu_146920689" ... id="c_249925713" ...>
            <input ... class="clazzId" value="146920689" />
            <input ... class="courseId" value="249925713" />
            <a href="...&cpi=406147479&...">...</a>
            <span class="course-name ..." title="机器学习">机器学习</span>
            <p class="line2 color3" title="高山">高山</p>
        </div>
    """
    courses = []
    # 用 class 中含 "course clearfix" 的外层 div 作为课程块
    block_re = re.compile(
        r'<div\s+class="course[^"]*"\s+info="([^"]*)"\s+roleId="([^"]*)"\s+id="(c_\d+)"[^>]*>(.*?)(?=<div\s+class="course\s+clearfix|</div>\s*</div>\s*</div>)',
        re.DOTALL,
    )
    # 备用匹配（结尾没有下一门课程时）
    block_re2 = re.compile(
        r'<div\s+class="course[^"]*"\s+info="([^"]*)"\s+roleId="([^"]*)"\s+id="(c_\d+)"[^>]*>(.*?)(?=\s*</div>\s*</div>)',
        re.DOTALL,
    )

    # 简化：按 <div class="course" ...> 切分
    parts = re.split(r'(?=<div\s+class="course\s+clearfix)', html)

    for part in parts:
        if 'class="course clearfix' not in part:
            continue
        # 提取外层 div 的 id
        m_id = re.search(r'id="(c_\d+)"', part)
        if not m_id:
            continue
        course_id = m_id.group(1)[2:]  # 去 "c_"
        m_info = re.search(r'info="([^"]*)"', part)
        m_role = re.search(r'roleId="([^"]*)"', part)
        m_clazz = re.search(r'class="clazzId"[^>]*value="(\d+)"', part)
        m_cpi = re.search(r'[?&]cpi=(\d+)', part)

        # 课程名：<span class="course-name ..." title="X">Y</span>
        m_title = re.search(
            r'<span[^>]*class="[^"]*course-name[^"]*"[^>]*(?:title="([^"]*)")?[^>]*>([^<]*)</span>',
            part,
        )
        # 老师：<p class="line2 color3" title="X">Y</p>
        m_teacher = re.search(
            r'<p[^>]*class="[^"]*color3[^"]*"[^>]*(?:title="([^"]*)")?[^>]*>([^<]*)</p>',
            part,
        )

        title = ""
        if m_title:
            title = m_title.group(1) or m_title.group(2) or ""
        teacher = ""
        if m_teacher:
            teacher = m_teacher.group(1) or m_teacher.group(2) or ""

        title = _decode_entities(title).strip()
        teacher = _decode_entities(teacher).strip()
        # 清理姓名之间不间断空格（&nbsp; → \xa0）
        teacher = teacher.replace("\xa0", " ").strip()

        courses.append({
            "courseId": course_id,
            "clazzId": m_clazz.group(1) if m_clazz else "",
            "cpi": m_cpi.group(1) if m_cpi else "",
            "title": title,
            "teacher": teacher,
            "info": m_info.group(1) if m_info else "",
            "roleId": m_role.group(1) if m_role else "",
            "is_ended": "课程已结束" in part or "已结束" in part
        })

    return courses


# ============================================================
# 学习通客户端
# ============================================================


def _parse_attachments_from_cards_html(html: str) -> list:
    """
    从 /knowledge/cards 响应 HTML 的 mArg.attachments 数组中提取每条 attachment 的关键字段。
    用括号配对定位数组边界，避免非贪婪正则被嵌套对象截断。
    返回: [{"type", "module", "objectid", "jobid", "workid", "name", "title", "worktype"}, ...]
    - title: 任务名（如 "学情调查"），从 property.title 提取
    - worktype: 任务类型标识（如 "workA"、"examA"），从 property.worktype 提取
    """
    idx = html.find('"attachments":[')
    if idx < 0:
        return []
    start = html.find('[', idx)
    if start < 0:
        return []
    # 配对找数组右括号
    depth = 0
    end = -1
    for i in range(start, len(html)):
        c = html[i]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return []
    arr_str = html[start:end+1]

    def field(name, src):
        m = re.search(r'"' + re.escape(name) + r'"\s*:\s*"([^"]*)"', src)
        return m.group(1) if m else None

    results = []
    pos = 0
    while True:
        obj_start = arr_str.find('{', pos)
        if obj_start < 0:
            break
        depth = 0
        obj_end = -1
        for i in range(obj_start, len(arr_str)):
            c = arr_str[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    obj_end = i
                    break
        if obj_end < 0:
            break
        obj_str = arr_str[obj_start:obj_end+1]
        # 提取 property 对象（用括号配对定位嵌套对象）
        prop_m = re.search(r'"property"\s*:\s*\{', obj_str)
        prop = {}
        if prop_m:
            prop_start = prop_m.end() - 1
            depth = 0
            prop_end = -1
            for i in range(prop_start, len(obj_str)):
                c = obj_str[i]
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        prop_end = i
                        break
            if prop_end > 0:
                prop_str = obj_str[prop_start:prop_end+1]
                prop = {
                    "module": field("module", prop_str),
                    "title": field("title", prop_str),
                    "worktype": field("worktype", prop_str),
                    "workid": field("workid", prop_str),
                    "jobid": field("jobid", prop_str),
                }
        # name 字段优先用 property.title（任务真实名称）
        title = prop.get("title") or field("name", obj_str)
        results.append({
            "type": field("type", obj_str),
            "module": prop.get("module"),
            "objectid": field("objectid", obj_str) or field("objectId", obj_str),
            "jobid": field("jobid", obj_str),
            "workid": prop.get("workid") or field("workid", obj_str),
            "name": title,
            "worktype": prop.get("worktype"),
        })
        pos = obj_end + 1
    return results


class XuexitongClient:
    """封装学习通 API 的 HTTP 客户端"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._uid = ""
        self._fid = ""
        self._cache_uid = None
        self.tokens_cache_file = BASE_DIR / "tokens_cache.json"
        self.tokens_cache = {}
        # 线程本地存储：每个线程持有一个独立的 session 副本
        self._thread_local = threading.local()
        # 保护 tokens_cache 并发写入的锁
        self._cache_lock = threading.Lock()
        self.load_tokens_cache()

    def _get_session(self) -> requests.Session:
        """
        获取当前线程的 session 副本（懒加载）。
        并发优化：每个线程持有独立 session，避免共享 session 的 cookie 竞争条件。
        不做此优化的后果：多个线程同时使用同一 session 并发请求时，
        cookie jar 的读写可能产生竞争，导致请求使用错误的 cookie 或丢失 set-cookie。
        """
        if not hasattr(self._thread_local, 'session'):
            sess = requests.Session()
            # 从主 session 复制 cookie 到线程 session
            for cookie in self.session.cookies:
                sess.cookies.set(
                    cookie.name, cookie.value,
                    domain=cookie.domain,
                    path=cookie.path,
                )
            for k, v in self.session.headers.items():
                sess.headers[k] = v
            self._thread_local.session = sess
        return self._thread_local.session

    def load_tokens_cache(self):
        try:
            if self.tokens_cache_file.exists():
                with open(self.tokens_cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "uid" in data and "tokens" in data:
                        self._cache_uid = data.get("uid")
                        self.tokens_cache = data.get("tokens", {})
                    else:
                        self._cache_uid = None
                        self.tokens_cache = data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"加载Token缓存失败: {e}")
            self.tokens_cache = {}
            self._cache_uid = None

    def save_tokens_cache(self):
        try:
            data = {
                "uid": self._uid,
                "tokens": self.tokens_cache
            }
            with open(self.tokens_cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存Token缓存失败: {e}")

    def clear_tokens_cache(self):
        self.tokens_cache = {}
        self._cache_uid = None
        if self.tokens_cache_file.exists():
            try:
                self.tokens_cache_file.unlink()
            except Exception:
                pass

    def clear_cooldowns(self):
        """
        清除所有课程的冷却标记，保留有效的 token 缓存。
        被 GUI 手动刷新时调用，允许用户强制重新请求被反爬拦截的课程。
        不做此操作的后果：被反爬的课程会持续 1800 秒冷却期，期间手动刷新也无法获取数据。
        """
        with self._cache_lock:
            changed = False
            for course_id in list(self.tokens_cache.keys()):
                entry = self.tokens_cache.get(course_id, {})
                if "cooldown_until" in entry:
                    del self.tokens_cache[course_id]
                    changed = True
                    logger.info(f"清除课程 {course_id} 的冷却标记")
            if changed:
                self.save_tokens_cache()

    # ----- 二维码登录 -----

    def get_qr_code_params(self) -> tuple[Optional[str], Optional[str], Optional[bytes]]:
        """
        获取二维码登录参数及图片数据
        返回: (uuid, enc, qr_image_bytes)
        """
        url = 'https://passport2.chaoxing.com/login?fid=-1&newversion=true&refer=http://i.chaoxing.com'
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                logger.error(f"获取登录页面失败，状态码: {resp.status_code}")
                return None, None, None

            def extract_field(html, field_id):
                # Pattern 1: value="..." id="field_id"
                m1 = re.search(r'value="([^"]+)"\s+id="' + field_id + r'"', html)
                if m1:
                    return m1.group(1)
                # Pattern 2: id="field_id" value="..."
                m2 = re.search(r'id="' + field_id + r'"\s+value="([^"]+)"', html)
                if m2:
                    return m2.group(1)
                # Pattern 3: general tag search
                m3 = re.search(r'<input[^>]+?id="' + field_id + r'"[^>]*?>', html)
                if m3:
                    val = re.search(r'value="([^"]+)"', m3.group(0))
                    if val:
                        return val.group(1)
                return None

            uuid = extract_field(resp.text, 'uuid')
            enc = extract_field(resp.text, 'enc')

            if not uuid or not enc:
                logger.error("解析 uuid 或 enc 失败")
                return None, None, None

            # 请求二维码图片，激活该 session 对应的二维码
            qr_url = f"https://passport2.chaoxing.com/createqr?uuid={uuid}&fid=-1"
            qr_resp = self.session.get(qr_url, timeout=10)
            if qr_resp.status_code != 200:
                logger.error(f"下载二维码图片失败，状态码: {qr_resp.status_code}")
                return None, None, None

            return uuid, enc, qr_resp.content
        except Exception as e:
            logger.error(f"获取二维码参数出错: {e}")
            return None, None, None

    def check_qr_login_status(self, uuid: str, enc: str) -> dict:
        """
        轮询检测二维码扫码状态
        返回: json 字典 (含有 status, mes, type 等字段)
        """
        poll_url = 'https://passport2.chaoxing.com/getauthstatus'
        data = {
            'uuid': uuid,
            'enc': enc
        }
        try:
            resp = self.session.post(poll_url, data=data, timeout=10)
            if resp.status_code != 200:
                return {"status": False, "mes": "网络错误", "type": "99"}
            
            result = resp.json()
            if result.get("status") == True:
                # 登录成功，保存相关状态
                new_uid = self.session.cookies.get("_uid", "")
                if self._cache_uid and new_uid != self._cache_uid:
                    logger.info(f"检测到登录账户变更 (旧UID: {self._cache_uid} -> 新UID: {new_uid})，清除旧 Token 缓存")
                    self.clear_tokens_cache()
                self._uid = new_uid
                self._fid = self.session.cookies.get("fid", "")
                logger.info(f"扫码登录成功，UID={self._uid}")
            return result
        except Exception as e:
            logger.error(f"检测二维码状态出错: {e}")
            return {"status": False, "mes": f"出错: {e}", "type": "99"}

    # ----- 登录 -----

    def login(self, phone: str, password: str) -> bool:
        """
        使用手机号+密码登录
        API: passport2.chaoxing.com/fanyalogin
        uname 和 password 都使用 AES/CBC 加密传输（密钥 u2oh6Vu^HWe4_AES）
        """
        data = {
            "fid": "-1",
            "uname": aes_encrypt(phone),
            "password": aes_encrypt(password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": "true",
            "forbidotherlogin": "0",
            "validate": "",
            "doubleFactorLogin": "0",
            "independentId": "0",
        }
        resp = self.session.post(LOGIN_URL, data=data)
        if resp.status_code != 200:
            logger.error(f"登录请求失败，状态码: {resp.status_code}")
            return False

        try:
            result = resp.json()
        except json.JSONDecodeError:
            logger.error(f"登录响应不是有效的 JSON: {resp.text[:200]}")
            return False

        if result.get("status") == True:
            new_uid = resp.cookies.get("_uid", "")
            if self._cache_uid and new_uid != self._cache_uid:
                logger.info(f"检测到登录账户变更 (旧UID: {self._cache_uid} -> 新UID: {new_uid})，清除旧 Token 缓存")
                self.clear_tokens_cache()
            self._uid = new_uid
            self._fid = resp.cookies.get("fid", "")
            logger.info(f"登录成功，UID={self._uid}")
            return True
        else:
            msg = result.get("msg2", result.get("msg", "未知错误"))
            logger.warning(f"登录失败: {msg}")
            return False

    def _try_auto_relogin(self) -> bool:
        """
        尝试从配置文件加载账号密码并进行自动登录，成功后保存新 Cookie
        """
        try:
            config = load_config()
            phone = config.get("phone")
            password = config.get("password")
            if phone and password:
                masked_phone = f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone
                logger.info(f"检测到 Cookie 失效，正在尝试使用保存的账号 {masked_phone} 重新登录以自动刷新...")
                if self.login(phone, password):
                    new_cookies = self.get_cookies()
                    config["cookies"] = new_cookies
                    save_config(config)
                    logger.info("自动重新登录成功，已保存新 Cookie 到配置文件")
                    return True
                else:
                    logger.warning("自动重新登录失败：用户名或密码错误，或触发了人机验证")
            else:
                logger.warning("自动重新登录失败：未在配置中找到手机号或密码")
        except Exception:
            logger.exception("自动重新登录时发生异常")
        return False

    def load_cookies(self, cookie_list: list) -> bool:
        """从 cookie 列表加载已保存的 cookies（每个元素含 name/value/domain/path）"""
        for c in cookie_list:
            self.session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ""), path=c.get("path", "/"),
            )
        # 优先用列表里的 _uid/fid；没有再回退到当前 session
        self._uid = next((c["value"] for c in cookie_list if c["name"] == "_uid"), self._uid)
        self._fid = next((c["value"] for c in cookie_list if c["name"] == "fid"), self._fid)
        if self._uid:
            return True
        return False

    def get_cookies(self) -> list:
        """导出 cookies 为列表（带域名，避免同名 cookie 冲突）"""
        out = []
        for c in self.session.cookies:
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
            })
        return out

    # ----- 通知数查询（轻量）-----

    def get_notice_count(self) -> int:
        """
        获取未读通知数量
        API: i.chaoxing.com/base/getNoticeCount
        返回通知条数，失败返回 -1
        """
        # 时间戳格式：JavaScript 的 Date().toLocaleString()
        now = datetime.now()
        timestamp_str = now.strftime("%a %b %d %Y %H:%M:%S") + " GMT+0800 (中国标准时间)"
        try:
            resp = self.session.post(
                NOTICE_COUNT_URL,
                data={"_t": timestamp_str},
                headers=XHR_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == True:
                    return int(data.get("count", 0))
                return 0
        except Exception as e:
            logger.warning(f"通知数请求失败: {e}")
        return -1

    # ----- 课程列表 -----

    def get_course_list(self) -> list:
        """
        获取所有课程列表
        API: mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata
        返回课程字典列表
        """
        data = {
            "courseType": "1",
            "courseFolderId": "0",
            "query": "",
            "superstarClass": "0",
        }
        headers = {
            **HEADERS,
            "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction",
        }
        
        # 最多尝试 2 次（第一次使用现有 Cookie，失效时尝试自动登录并重试一次）
        for attempt in range(2):
            try:
                resp = self.session.post(COURSE_LIST_URL, data=data, headers=headers, timeout=15)
                if resp.status_code == 200:
                    html = resp.content.decode('utf-8', errors='ignore')
                    
                    # 检查是否被重定向到登录页，或者 HTML 包含登录关键字且未匹配到课程
                    is_login_page = "passport" in resp.url or "login" in resp.url or "登录" in html
                    courses = parse_courses_from_html(html)
                    
                    if not courses and is_login_page:
                        logger.info("检测到 Cookie 已过期/失效")
                        if attempt == 0 and self._try_auto_relogin():
                            logger.info("自动刷新 Cookie 成功，正在重试获取课程列表...")
                            continue # 重试
                        else:
                            logger.warning("无法通过自动登录刷新 Cookie，获取课程列表失败")
                            return []
                    
                    logger.info(f"获取到 {len(courses)} 门课程")
                    return courses
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                logger.warning(f"课程列表网络请求失败/超时 (尝试 {attempt+1}/2): {e}")
                # 直接重试，不执行自动重新登录
            except Exception as e:
                logger.warning(f"课程列表请求失败 (尝试 {attempt+1}/2): {e}")
                if attempt == 0 and self._try_auto_relogin():
                    logger.info("自动刷新 Cookie 成功，正在重试获取课程列表...")
                    continue
                
        return []

    def _set_course_cooldown(self, course_id: str, cooldown_sec: int = 60):
        """
        设置课程冷却时间（线程安全）。
        被 get_course_tokens、_get_homework_list、_get_exam_list 在检测到反爬虫时调用。
        """
        now = time.time()
        with self._cache_lock:
            self.tokens_cache[str(course_id)] = {"cooldown_until": now + cooldown_sec}
            self.save_tokens_cache()

    def get_course_tokens(self, course: dict, cooldown_sec: int = 60) -> dict:
        course_id = course.get("courseId", "") or course.get("id", "")
        clazz_id = course.get("clazzId", "")
        cpi = course.get("cpi", "")

        if not all([course_id, clazz_id, cpi]):
            logger.warning(f"课程 {course.get('title')} 缺少参数: courseId={course_id!r} clazzId={clazz_id!r} cpi={cpi!r}")
            return {}

        now = time.time()
        # 带锁读取缓存，避免与 save_tokens_cache 的数据竞争
        with self._cache_lock:
            cached = self.tokens_cache.get(course_id, {})

        # 1. 检查冷却期：如果之前被反爬虫拦截且冷却未结束，直接跳过
        cooldown_until = cached.get("cooldown_until", 0)
        if cooldown_until > now:
            logger.debug(f"课程 {course.get('title')} 在冷却中（剩余{int(cooldown_until - now)}秒），跳过")
            return {}

        # 2. 缓存命中（且不在冷却期）
        if all(cached.get(k) for k in ["openc", "workEnc", "examEnc", "enc", "t"]):
            logger.debug(f"课程 {course.get('title')} 使用缓存token")
            return cached

        logger.debug(f"课程 {course.get('title')} 缓存不完整或不存在，准备请求 (cache_keys={list(cached.keys())})")

        # 3. 缓存不存在或不完整，请求服务器获取
        # 请求前延迟：测试证实 0.3s 间隔跨课程安全（反爬受总次数限制，不受频率限制）
        time.sleep(0.3)
        sess = self._get_session()  # 线程安全 session
        url = f"https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ismooc2=1&v=2"
        try:
            resp = sess.get(url, timeout=15)

            # 检查是否被防爬虫拦截 → 设置冷却期
            if "antispiderShowVerify.ac" in resp.url or "antispider" in resp.text:
                logger.warning(f"课程 {course.get('title')} 获取Token被防爬虫拦截，冷却{cooldown_sec}秒")
                self._set_course_cooldown(course_id, cooldown_sec)
                return {}

            # 使用 content.decode('utf-8') 而非 resp.text，防止响应编码检测错误导致乱码
            html = resp.content.decode('utf-8', errors='ignore')
            
            def extract_val(id_name):
                m = re.search(r'id="' + re.escape(id_name) + r'"[^>]*value="([^"]*)"', html)
                if not m:
                    m = re.search(r'name="' + re.escape(id_name) + r'"[^>]*value="([^"]*)"', html)
                return m.group(1) if m else None

            tokens = {
                "openc": extract_val("openc"),
                "workEnc": extract_val("workEnc"),
                "examEnc": extract_val("examEnc"),
                "enc": extract_val("enc"),
                "t": extract_val("t"),
            }

            if all(tokens.values()):
                with self._cache_lock:
                    self.tokens_cache[course_id] = tokens
                    self.save_tokens_cache()
                return tokens
            else:
                missing = [k for k, v in tokens.items() if not v]
                logger.warning(f"课程 {course.get('title')} Token提取不完整，缺少: {missing}")
                return {}
        except Exception as e:
            logger.error(f"获取课程 {course.get('title')} 的Token失败: {e}")
            return {}

    def _get_homework_list(self, course: dict, tokens: dict) -> list:
        """
        Fetch the homework page, decode as UTF-8, and parse list items.
        """
        course_id = course.get("courseId", "")
        clazz_id = course.get("clazzId", "")
        cpi = course.get("cpi", "")
        work_enc = tokens.get("workEnc")
        enc = tokens.get("enc")
        t = tokens.get("t")

        if not all([course_id, clazz_id, cpi, work_enc, enc, t]):
            return []

        url = f"https://mooc1.chaoxing.com/mooc2/work/list?courseId={course_id}&classId={clazz_id}&cpi={cpi}&ut=s&t={t}&stuenc={enc}&enc={work_enc}"
        try:
            sess = self._get_session()  # 线程安全 session
            resp = sess.get(url, timeout=15)
            # Homework page is UTF-8 encoded
            html = resp.content.decode('utf-8', errors='ignore')
            
            # 检查是否被防爬虫拦截 → 设置冷却期，避免下一轮重复请求
            if "antispiderShowVerify.ac" in resp.url or "antispider" in html:
                logger.warning(f"课程 {course.get('title')} 获取作业列表被防爬虫拦截，设置冷却")
                self._set_course_cooldown(course_id)
                return []
            
            tasks = []
            # Find list items with onclick="goTask(this);"
            li_pattern = re.compile(r'<li[^>]*onclick="goTask\(this\);"[^>]*data="([^"]+)"[^>]*>(.*?)</li>', re.DOTALL)
            for match in li_pattern.finditer(html):
                url_task = match.group(1).strip()
                li_content = match.group(2)
                
                title_m = re.search(r'class="overHidden2[^"]*"[^>]*>(.*?)</p>', li_content, re.DOTALL)
                title = title_m.group(1).strip() if title_m else "未命名作业"
                title = re.sub(r'<[^>]+>', '', title).strip()
                
                status_m = re.search(r'class="status[^"]*"[^>]*>(.*?)</p>', li_content, re.DOTALL)
                status = status_m.group(1).strip() if status_m else "未知"
                status = re.sub(r'<[^>]+>', '', status).strip()
                
                time_m = re.search(r'class="time[^"]*"[^>]*>(.*?)</div>', li_content, re.DOTALL)
                deadline = "无截止时间"
                if time_m:
                    deadline = re.sub(r'<[^>]+>', '', time_m.group(1)).strip()
                    deadline = re.sub(r'\s+', ' ', deadline).strip()

                tasks.append({
                    "course": course.get("title", ""),
                    "teacher": course.get("teacher", ""),
                    "course_id": course_id,
                    "clazz_id": clazz_id,
                    "cpi": cpi,
                    "chapter": "独立作业",
                    "knowledge_id": "",
                    "workid": "",
                    "type": "作业",
                    "status": status,
                    "deadline": deadline,
                    "url": url_task,
                    "name": title
                })
            return tasks
        except Exception as e:
            logger.error(f"获取课程 {course.get('title')} 的作业列表失败: {e}")
            return []

    def _get_exam_list(self, course: dict, tokens: dict) -> list:
        """
        Fetch the exam page, decode as GB18030, and parse list items.
        """
        course_id = course.get("courseId", "")
        clazz_id = course.get("clazzId", "")
        cpi = course.get("cpi", "")
        exam_enc = tokens.get("examEnc") or ""
        openc = tokens.get("openc") or ""
        enc = tokens.get("enc") or ""
        t = tokens.get("t") or ""

        if not all([course_id, clazz_id, cpi, exam_enc, enc, t]):
            return []

        url = f"https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ut=s&t={t}&stuenc={enc}&enc={exam_enc}&openc={openc}"
        try:
            sess = self._get_session()  # 线程安全 session
            resp = sess.get(url, timeout=15)
            # Exam page is UTF-8 encoded
            html = resp.content.decode('utf-8', errors='ignore')
            
            # 检查是否被防爬虫拦截 → 设置冷却期
            if "antispiderShowVerify.ac" in resp.url or "antispider" in html:
                logger.warning(f"课程 {course.get('title')} 获取考试列表被防爬虫拦截，设置冷却")
                self._set_course_cooldown(course_id)
                return []
            
            tasks = []
            li_pattern = re.compile(r'<li[^>]*>(.*?)</li>', re.DOTALL)
            for match in li_pattern.finditer(html):
                li_content = match.group(1)
                if "goTest(" not in li_content:
                    continue
                
                title_m = re.search(r'class="overHidden2[^"]*"[^>]*>(.*?)</p>', li_content, re.DOTALL)
                title = title_m.group(1).strip() if title_m else "未命名考试"
                title = re.sub(r'<[^>]+>', '', title).strip()
                
                status_m = re.search(r'class="status[^"]*"[^>]*>(.*?)</p>', li_content, re.DOTALL)
                status = status_m.group(1).strip() if status_m else "未知"
                status = re.sub(r'<[^>]+>', '', status).strip()
                
                time_m = re.search(r'class="time[^"]*"[^>]*>(.*?)</div>', li_content, re.DOTALL)
                deadline = "无截止时间"
                if time_m:
                    deadline = re.sub(r'<[^>]+>', '', time_m.group(1)).strip()
                    deadline = re.sub(r'\s+', ' ', deadline).strip()

                onclick_m = re.search(r'onclick\s*=\s*["\']goTest\((.*?)\);?["\']', li_content)
                url_task = "未知URL"
                if onclick_m:
                    args = [a.strip().strip("'").strip('"') for a in onclick_m.group(1).split(',')]
                    if len(args) >= 2:
                        exam_id = args[1]
                        url_task = f"https://mooc1.chaoxing.com/exam-ans/exam/test/examcode/examnotes?courseId={course_id}&classId={clazz_id}&examId={exam_id}&cpi={cpi}"

                tasks.append({
                    "course": course.get("title", ""),
                    "teacher": course.get("teacher", ""),
                    "course_id": course_id,
                    "clazz_id": clazz_id,
                    "cpi": cpi,
                    "chapter": "独立考试",
                    "knowledge_id": "",
                    "workid": "",
                    "type": "考试",
                    "status": status,
                    "deadline": deadline,
                    "url": url_task,
                    "name": title
                })
            return tasks
        except Exception as e:
            logger.error(f"获取课程 {course.get('title')} 的考试列表失败: {e}")
            return []

    # ----- 章节/任务信息 -----

    # ----- 章节卡片（未完成任务清单）-----


    def get_chapter_attachments(self, course_id: str, clazz_id: str, knowledge_id: str) -> list:
        """
        获取章节详情页（/knowledge/cards）中的 attachments 数组
        每个 attachment 含 type/module/objectid/jobid/workid/name/worktype 等字段
        返回 attachments 字典列表（按出现顺序）
        行为：服务器在大量请求后会返回 202 占位符（无 mArg JSON），此时需重试
        """
        url = (
            f"{CHAPTER_CARDS_URL}"
            f"?knowledgeid={knowledge_id}&courseid={course_id}&clazzid={clazz_id}&ut=s"
        )
        headers = {
            **HEADERS,
            "Referer": f"{COURSE_DETAIL_URL}?courseid={course_id}&clazzid={clazz_id}&ut=s",
        }
        for attempt in range(5):
            try:
                resp = self.session.get(url, headers=headers, timeout=15)
            except Exception as e:
                logger.warning(f"[卡片] knowledgeid={knowledge_id} 请求失败: {e}")
                return []
            # 200 = 正常含数据；202 = 服务器限流/异步占位（无 mArg），需重试
            if resp.status_code == 202 or '"attachments":[' not in resp.text:
                # 指数退避：1s, 2s, 3s, 4s, 5s
                wait = 1.0 + attempt
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                logger.warning(f"[卡片] knowledgeid={knowledge_id} 状态码={resp.status_code}")
                return []
            return _parse_attachments_from_cards_html(resp.text)
        return []

    def classify_task(self, worktype: Optional[str]) -> str:
        """
        根据 attachment 的 property.worktype 字段判断任务类型
        实测 worktype 取值：
        - "workA" / "workB" ... → 作业
        - "examA" / "examB" ... → 考试
        - "train"  → 训练（暂归类为"作业"）
        返回 "作业" / "考试" / "未知"
        """
        if not worktype:
            return "未知"
        wt = worktype.lower()
        if "exam" in wt:
            return "考试"
        if "work" in wt or "train" in wt:
            return "作业"
        return "未知"

    def get_unfinished_tasks(self, courses: Optional[list] = None, config: Optional[dict] = None, progress_callback=None) -> list:
        """
        两阶段扫描所有课程，返回未完成作业/考试的任务列表

        阶段 1（串行）: 逐个获取课程的 Token，避免并发 Token 请求触发反爬。
                       Token 已经缓存的课程瞬间完成，只有未缓存的课程需要实际 HTTP 请求。
        阶段 2（并发）: 用 ThreadPoolExecutor 并行抓取每门课程的作业/考试列表。

        被 list_unfinished_tasks()、GUI 调用

        参数:
            courses: 课程列表，None 时自动获取
            config: 配置字典（max_workers、only_courses、rate_limit_delay）
            progress_callback: 进度回调函数，用于汇报多轮重试进度
        """
        if courses is None:
            courses = self.get_course_list()

        cfg = config or {}
        max_workers = cfg.get("max_workers", 8)
        rate_limit_delay = cfg.get("rate_limit_delay", 1.0)
        only_courses = cfg.get("only_courses", [])
        hide_nd = cfg.get("hide_no_deadline", False)

        # 过滤：跳过已结束的课程 + 白名单过滤
        filtered = []
        for course in courses:
            if course.get("is_ended"):
                continue
            title = course.get("title") or course.get("courseId", "未知课程")
            if only_courses:
                match = any(kw.lower() in title.lower() for kw in only_courses)
                if not match:
                    continue
            filtered.append(course)

        is_first_scan = cfg.get("is_first_scan", False)
        max_rounds = 5 if is_first_scan else 1
        remaining_courses = list(filtered)
        all_tasks = []

        for round_idx in range(1, max_rounds + 1):
            if not remaining_courses:
                break

            msg = f"开始第 {round_idx}/{max_rounds} 轮扫描，剩余 {len(remaining_courses)} 门课程"
            logger.info(msg)
            if progress_callback:
                progress_callback({
                    "status": "round_start",
                    "round": round_idx,
                    "max_rounds": max_rounds,
                    "remaining": len(remaining_courses),
                    "message": msg
                })

            # 清理剩余课程的缓存冷却标记
            with self._cache_lock:
                cleared_count = 0
                for course in remaining_courses:
                    cid = str(course.get("courseId", "") or course.get("id", ""))
                    if cid in self.tokens_cache:
                        cached = self.tokens_cache[cid]
                        if "cooldown_until" in cached:
                            cached.pop("cooldown_until", None)
                            cleared_count += 1
                if cleared_count > 0:
                    self.save_tokens_cache()
            if cleared_count > 0:
                logger.info(f"已清理 {cleared_count} 门剩余课程的缓存冷却标记")

            # ================================================================
            # 阶段 1：串行获取 Token（避免并发 Token 请求触发反爬）
            # ================================================================
            course_token_pairs = []
            failed_in_phase1 = []
            total_remaining = len(remaining_courses)
            logger.info(f"[阶段1] 串行获取 {total_remaining} 门课程的 Token...")
            token_fetch_start = time.time()

            for idx, course in enumerate(remaining_courses):
                # 汇报阶段 1 进度
                if progress_callback:
                    progress_callback({
                        "status": "scanning",
                        "phase": 1,
                        "current": idx + 1,
                        "total": total_remaining,
                        "message": f"正在获取第 {idx+1}/{total_remaining} 门课程 Token: {course.get('title')}"
                    })

                # 检查是否已有缓存以避免不必要的网络请求和防反爬拦截
                cid = str(course.get("courseId", "") or course.get("id", ""))
                with self._cache_lock:
                    cached = self.tokens_cache.get(cid, {})
                has_cache = all(cached.get(k) for k in ["openc", "workEnc", "examEnc", "enc", "t"])
                
                # 如果没有缓存且是首次扫描，使用 3.0 秒的防反爬安全延迟
                if not has_cache and is_first_scan:
                    time.sleep(3.0)

                cooldown = cfg.get("token_cooldown", 60)
                tokens = self.get_course_tokens(course, cooldown_sec=cooldown)
                
                # 检查是否成功获取 Token 且未被冷却
                cid = str(course.get("courseId", "") or course.get("id", ""))
                with self._cache_lock:
                    cached = self.tokens_cache.get(cid, {})
                
                if tokens and tokens.get("workEnc") and tokens.get("examEnc") and "cooldown_until" not in cached:
                    course_token_pairs.append((course, tokens))
                else:
                    failed_in_phase1.append(course)

                if (idx + 1) % 5 == 0 or idx == total_remaining - 1:
                    logger.info(f"  [阶段1进度] {idx+1}/{total_remaining} 门课程，已获 Token: {len(course_token_pairs)} 门")

            token_fetch_elapsed = time.time() - token_fetch_start
            logger.info(
                f"[阶段1完成] {len(course_token_pairs)}/{total_remaining} 门课程获 Token，"
                f"耗时 {token_fetch_elapsed:.1f}s"
            )

            if not course_token_pairs:
                logger.warning("[阶段1] 没有任何课程获取到 Token，跳过阶段2")
                remaining_courses = list(failed_in_phase1)
                
                if remaining_courses and round_idx < max_rounds:
                    retry_wait = cfg.get("retry_round_wait", 30)
                    logger.info(f"第 {round_idx} 轮扫描结束（全部未获取 Token）。等待 {retry_wait} 秒后开始下一轮...")
                    for s in range(retry_wait, 0, -1):
                        if progress_callback:
                            progress_callback({
                                "status": "wait",
                                "phase": "round_retry",
                                "remaining_seconds": s,
                                "message": f"正在等待 {s} 秒后开始下一轮重试..."
                            })
                        time.sleep(1)
                continue

            # ================================================================
            # 阶段间恢复期：等待 15 秒让 session 的请求配额重置，
            # 避免阶段 2 的并发请求立即触发反爬。
            # ================================================================
            wait_time = 15
            logger.info(f"[阶段间] 等待 {wait_time}s 让 session 请求配额恢复...")
            for s in range(wait_time, 0, -1):
                if progress_callback:
                    progress_callback({
                        "status": "wait",
                        "phase": "recovery",
                        "remaining_seconds": s,
                        "message": f"等待 {s} 秒让 session 请求配额恢复..."
                    })
                time.sleep(1)
            logger.info("[阶段间] 恢复完成，开始阶段2")

            # ================================================================
            # 阶段 2：并发获取作业/考试列表（错峰启动，避免突发请求）
            # ================================================================
            scanned = 0
            pair_total = len(course_token_pairs)
            lock = threading.Lock()
            failed_courses_in_phase2 = []

            def _fetch_course_data(course: dict, tokens: dict, start_delay: float = 0) -> dict:
                nonlocal scanned
                if start_delay > 0:
                    time.sleep(start_delay)

                cid = str(course.get("courseId", "") or course.get("id", ""))
                is_blocked = False
                hws = []
                exams = []
                try:
                    hws = self._get_homework_list(course, tokens)
                    exams = self._get_exam_list(course, tokens)
                    
                    with self._cache_lock:
                        cached = self.tokens_cache.get(cid, {})
                    if "cooldown_until" in cached:
                        is_blocked = True
                except Exception as e:
                    logger.error(f"获取课程 {course.get('title')} 的作业/考试列表异常: {e}")
                    is_blocked = True

                unfinished = []
                if not is_blocked:
                    for h in hws:
                        if _is_unfinished(h["status"]) and not _is_overdue(h.get("deadline", ""), hide_nd):
                            unfinished.append(h)
                    for e in exams:
                        if _is_unfinished(e["status"]) and not _is_overdue(e.get("deadline", ""), hide_nd):
                            unfinished.append(e)

                with lock:
                    scanned += 1
                    title = course.get("title", "未知课程")
                    if progress_callback:
                        progress_callback({
                            "status": "scanning",
                            "phase": 2,
                            "current": scanned,
                            "total": pair_total,
                            "message": f"已扫描 {scanned}/{pair_total} 门课程: {title}"
                        })

                    if is_blocked:
                        logger.warning(f"[{scanned}/{pair_total}] {title} - 被反爬拦截或出错，将放入下一轮重试")
                    else:
                        if unfinished:
                            logger.info(f"[{scanned}/{pair_total}] {title} - 未完成: {len(unfinished)}项")
                        else:
                            logger.debug(f"[{scanned}/{pair_total}] {title} - 全部完成")

                time.sleep(rate_limit_delay)
                return {"tasks": unfinished, "is_blocked": is_blocked}

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = []
                for i, (course, tokens) in enumerate(course_token_pairs):
                    delay = i * 1.0
                    future = executor.submit(_fetch_course_data, course, tokens, delay)
                    futures.append((future, course))

                for f, course in futures:
                    try:
                        res = f.result()
                        if res["is_blocked"]:
                            failed_courses_in_phase2.append(course)
                        else:
                            all_tasks.extend(res["tasks"])
                    except Exception as e:
                        logger.error(f"扫描课程 {course.get('title')} 线程异常: {e}")
                        failed_courses_in_phase2.append(course)

            remaining_courses = failed_in_phase1 + failed_courses_in_phase2
            
            if remaining_courses and round_idx < max_rounds:
                retry_wait = cfg.get("retry_round_wait", 30)
                logger.info(f"第 {round_idx} 轮扫描结束。有 {len(remaining_courses)} 门课程需要重试，等待 {retry_wait} 秒后开始下一轮...")
                for s in range(retry_wait, 0, -1):
                    if progress_callback:
                        progress_callback({
                            "status": "wait",
                            "phase": "round_retry",
                            "remaining_seconds": s,
                            "message": f"有课程重试，等待 {s} 秒后开始下一轮..."
                        })
                    time.sleep(1)

        return all_tasks

    # ----- 活跃活动（签到等）-----

    def get_active_activities(self, course: dict) -> list:
        """
        获取课程中活跃的活动（签到、抢答等）
        """
        course_id = course.get("courseId", "")
        clazz_id = course.get("clazzId", "")
        if not all([course_id, clazz_id]):
            return []

        params = {
            "fid": self._fid or "1024",
            "courseId": course_id,
            "classId": clazz_id,
            "showNotStartedActive": "0",
            "_": str(int(time.time() * 1000)),
        }
        try:
            resp = self.session.get(ACTIVE_LIST_URL, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("result") == 1:
                    return data.get("data", {}).get("activeList", [])
        except Exception as e:
            logger.warning(f"[活动] {course.get('title', course_id)} 查询失败: {e}")
        return []


# ============================================================
# 未完成任务判断（解决硬编码中文匹配的脆弱性）
# ============================================================

# 已知的"未完成"状态集合（精确匹配）
_KNOWN_UNFINISHED = frozenset({"未交", "进行中", "待做"})




def _is_unfinished(status: str) -> bool:
    """
    判断作业/考试状态是否为"未完成"
    严格匹配 _KNOWN_UNFINISHED 集合（当前：未交 / 进行中 / 待做）
    被 get_unfinished_tasks()、check_and_notify() 的扫描线程调用
    """
    return status in _KNOWN_UNFINISHED


def _is_overdue(deadline: str, hide_no_deadline: bool = False) -> bool:
    """
    判断任务的截止时间是否已过（通过解析倒计时文本）
    被 get_unfinished_tasks()、check_and_notify() 的扫描线程调用，
    用于过滤已过期的任务

    支持的 deadline 格式（实测学习通使用"还剩"前缀）：
    - "还剩 X 天 Y 小时" / "剩余 X 天 Y 小时" → 计算总剩余小时
    - "还剩 X 小时 Y 分钟" / "剩余 X 小时"     → 同上，忽略分钟
    - "无截止时间"                              → 根据 hide_no_deadline 决定
    - 无法解析的格式                            → 保守不算过期

    参数:
        hide_no_deadline: 为 True 时，"无截止时间"也算过期（隐藏）
    返回 True 表示已过期，应该隐藏
    """
    if not deadline or deadline == "无截止时间":
        return hide_no_deadline

    found = False
    total_hours = 0

    # 匹配 "剩余/还剩 X 天"
    m = re.search(r'(?:剩余|还剩)\s*(\d+)\s*天', deadline)
    if m:
        total_hours += int(m.group(1)) * 24
        found = True

    # 匹配 "X 小时"（可能是"5 小时"或"682 小时25 分钟"中的小时部分）
    m = re.search(r'(\d+)\s*小时', deadline)
    if m:
        total_hours += int(m.group(1))
        found = True

    # 没解析到任何时间 → 保守处理，不认定为过期
    if not found:
        return False

    # 解析到时间但剩余 ≤ 0 → 已过期
    return total_hours <= 0



def load_config() -> dict:
    """
    加载配置文件，与 DEFAULT_CONFIG 合并（用户配置优先覆盖默认值）
    被 main()、run_list_tasks()、GUI 入口调用
    """
    config = dict(DEFAULT_CONFIG)  # 深拷贝默认值
    if CONFIG_FILE.exists():
        try:
            user_config: dict = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(user_config, dict):
                config.update(user_config)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"加载 config.json 失败，使用默认配置: {e}")
    return config


def save_config(config: dict):
    """
    保存配置文件（与现有文件合并，避免丢失未涉及的字段）
    被 login、GUI logout、main() 等调用
    """
    # 读取现有配置，保留不在本次写入中的字段
    if CONFIG_FILE.exists():
        try:
            existing = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                existing.update(config)
                config = existing
        except (json.JSONDecodeError, OSError):
            pass
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# 开机自启管理（Windows 注册表）
# ============================================================

# Windows 注册表 Run 键路径，用于存储开机自启条目
_AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 注册表中的条目名称
_AUTOSTART_NAME = "XuexitongNotifier"


def is_autostart_enabled() -> bool:
    """
    检查开机自启是否已开启。
    通过查询注册表 Run 键判断是否存在本程序条目。
    被 GUI 的自启开关初始化时调用。
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _AUTOSTART_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def set_autostart(enable: bool) -> bool:
    """
    设置/取消开机自启。
    enable=True：在注册表 Run 键中创建条目
    - 若以打包后的 exe 运行，则直接指向 exe
    - 若以脚本运行，则指向 pythonw.exe 并传入 xxt_gui.py 的绝对路径
    enable=False：删除该注册表条目
    被 GUI 的自启开关点击时调用。

    使用 pythonw.exe 而非 python.exe，避免开机启动时弹出控制台窗口。
    返回值表示操作是否成功。
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enable:
                if getattr(sys, 'frozen', False):
                    # 如果是打包后的可执行文件（PyInstaller），直接运行该 exe
                    cmd = f'"{sys.executable}" --minimized'
                else:
                    # 如果是脚本运行，使用 pythonw.exe 运行 xxt_gui.py
                    # 优先获取当前 Python 解释器同目录下的 pythonw.exe
                    python_exe = Path(sys.executable)
                    pythonw = python_exe.with_name("pythonw.exe")
                    if not pythonw.exists():
                        # 回退到当前 python 解释器
                        pythonw = python_exe
                    
                    script_path = BASE_DIR / "xxt_gui.py"
                    cmd = f'"{pythonw}" "{script_path}" --minimized'
                
                # 写入注册表 Run 键，用引号括起路径，防止路径含空格时解析错误
                winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
                logger.info(f"开机自启已开启: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, _AUTOSTART_NAME)
                    logger.info("开机自启已关闭")
                except FileNotFoundError:
                    pass  # 本来就没有条目
            return True
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        logger.error(f"设置开机自启失败: {e}")
        return False


# ============================================================
# 任务状态持久化（用于检测关闭期间发布的新任务）
# ============================================================

STATE_FILE = BASE_DIR / "state.json"


def load_task_state() -> dict:
    """
    从 state.json 加载已保存的任务状态。
    返回格式: {"seen_keys": ["课程|任务名|类型", ...], "last_scan": 时间戳}
    被 GUI 启动时调用，用于对比检测新任务。
    """
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"加载 state.json 失败: {e}")
    return {"seen_keys": []}


def save_task_state(tasks: list):
    """
    将当前任务列表写入 state.json。
    seen_keys 累积保存（旧 key 保留 + 新 key 追加），避免重复通知。
    被 GUI 每次扫描完成后调用。
    """
    # 计算当前所有任务的特征 key
    current_keys = set()
    for t in tasks:
        key = f"{t.get('course','')}|{t.get('name','')}|{t.get('type','')}"
        current_keys.add(key)

    try:
        old = load_task_state()
        old_seen = set(old.get("seen_keys", []))
        # 合并旧 key（历史已通知的）和新 key（当前任务）
        old_seen.update(current_keys)
        state = {
            "seen_keys": sorted(old_seen),
            "last_scan": time.time(),
        }
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"保存 state.json 失败: {e}")


def find_new_tasks(current_tasks: list) -> list:
    """
    对比 state.json 中的 seen_keys，找出 current_tasks 中从未见过的新任务。
    被 GUI 启动时调用，用于弹窗通知。
    返回新任务列表（按 current_tasks 顺序）。
    """
    state = load_task_state()
    seen = set(state.get("seen_keys", []))
    new_tasks = []
    for t in current_tasks:
        key = f"{t.get('course','')}|{t.get('name','')}|{t.get('type','')}"
        if key not in seen:
            new_tasks.append(t)
    return new_tasks


def show_notification(title: str, message: str, tk_root=None):
    """
    弹出右下角自定义浮窗通知。
    使用 tkinter 窗口实现，不依赖 Windows 通知系统，
    保证在所有 Windows 版本上可靠弹出。

    参数:
        title: 通知标题
        message: 通知内容（支持 \\n 换行）
        tk_root: 可选的 tkinter 根窗口。
                 从 GUI 调用时传入 root，使用 Toplevel 挂载（不阻塞）。
                 独立调用时不传，自动创建临时 Tk 窗口。
    """
    import tkinter as tk
    import winsound

    # 日志
    safe_title = title.encode('ascii', errors='replace').decode('ascii')
    safe_msg = message.replace('\n', ' | ').encode('ascii', errors='replace').decode('ascii')
    logger.info(f"[通知] {safe_title}: {safe_msg}")

    # 播放系统提示音（异步，不阻塞）
    try:
        winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass

    # ---------- 决定窗口类型 ----------
    standalone = tk_root is None
    if standalone:
        popup = tk.Tk()
    else:
        popup = tk.Toplevel(tk_root)

    popup.withdraw()  # 先隐藏，计算布局后再显示
    popup.overrideredirect(True)        # 无标题栏/边框
    popup.attributes("-topmost", True)  # 始终置顶
    popup.configure(bg="#1e1e2e")

    # ---------- 尺寸与位置 ----------
    W, H = 380, 140
    screen_w = popup.winfo_screenwidth()
    screen_h = popup.winfo_screenheight()

    # 堆叠偏移：追踪当前显示的通知数
    if not hasattr(show_notification, '_stack_offset'):
        show_notification._stack_offset = 0
    offset = show_notification._stack_offset * (H + 10)
    show_notification._stack_offset += 1

    x = screen_w - W - 20
    y = screen_h - H - 60 - offset  # 60 = 任务栏高度预留
    popup.geometry(f"{W}x{H}+{x}+{y}")

    # ---------- 外框（模拟边框） ----------
    border_frame = tk.Frame(popup, bg="#5966f3", padx=2, pady=2)
    border_frame.pack(fill="both", expand=True)

    card = tk.Frame(border_frame, bg="#212130", padx=14, pady=10)
    card.pack(fill="both", expand=True)

    # ---------- 标题行 ----------
    top_row = tk.Frame(card, bg="#212130")
    top_row.pack(fill="x")

    # 清理 emoji（避免 tkinter 字体问题）
    clean_title = title
    for ch in "📚📝📋🔔⚠️":
        clean_title = clean_title.replace(ch, "")
    clean_title = clean_title.strip()

    lbl_icon = tk.Label(top_row, text="\u2709", bg="#212130", fg="#5966f3",
                        font=("Microsoft YaHei", 14))
    lbl_icon.pack(side="left", padx=(0, 6))

    lbl_title = tk.Label(top_row, text=clean_title, bg="#212130", fg="#ffffff",
                         font=("Microsoft YaHei", 11, "bold"), anchor="w")
    lbl_title.pack(side="left", fill="x", expand=True)

    # 关闭按钮
    btn_close = tk.Label(top_row, text="\u2715", bg="#212130", fg="#6c7086",
                         font=("Consolas", 12, "bold"), cursor="hand2", padx=4)
    btn_close.pack(side="right")

    # ---------- 分隔线 ----------
    sep = tk.Frame(card, bg="#313145", height=1)
    sep.pack(fill="x", pady=(6, 6))

    # ---------- 消息内容 ----------
    display_msg = message if len(message) <= 120 else message[:117] + "..."
    lbl_msg = tk.Label(card, text=display_msg, bg="#212130", fg="#bac2de",
                       font=("Microsoft YaHei", 9), anchor="w", justify="left",
                       wraplength=W - 40)
    lbl_msg.pack(fill="x", expand=True)

    # ---------- 底部标签 ----------
    lbl_app = tk.Label(card, text="学习通扫描器", bg="#212130", fg="#45475a",
                       font=("Microsoft YaHei", 8), anchor="e")
    lbl_app.pack(fill="x", pady=(4, 0))

    # ---------- 关闭与淡出 ----------
    def _close():
        show_notification._stack_offset = max(0, show_notification._stack_offset - 1)
        try:
            popup.destroy()
        except Exception:
            pass

    def _fade_out(alpha=1.0):
        if alpha <= 0:
            _close()
            return
        try:
            popup.attributes("-alpha", alpha)
            popup.after(50, lambda: _fade_out(alpha - 0.05))
        except Exception:
            pass

    # 绑定关闭按钮
    btn_close.bind("<Button-1>", lambda e: _close())
    btn_close.bind("<Enter>", lambda e: btn_close.configure(fg="#f38ba8"))
    btn_close.bind("<Leave>", lambda e: btn_close.configure(fg="#6c7086"))

    # 8 秒后开始淡出
    popup.after(8000, _fade_out)

    # 点击窗口任意处也可关闭
    for widget in [card, border_frame, lbl_msg, lbl_title, lbl_app, sep, lbl_icon]:
        widget.bind("<Button-1>", lambda e: _close())

    # ---------- 显示 ----------
    popup.deiconify()

    # 独立模式需要自己的 mainloop；GUI 模式下由主窗口 mainloop 驱动
    if standalone:
        popup.mainloop()


def run_gui():
    """GUI 入口点（被 start.bat 或直接调用）"""
    from xxt_gui import main as gui_main
    gui_main()
