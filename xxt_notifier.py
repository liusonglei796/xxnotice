"""
学习通作业/考试桌面通知小工具
============================
功能：定时检测学习通课程中的未完成作业/任务，有新任务时弹出 Windows 桌面通知
原理：基于逆向的学习通 API（mooc1/mooc2 系列接口），登录后定期轮询课程任务状态

依赖：requests, pycryptodome, plyer
用法：python xxt_notifier.py
"""

from pathlib import Path
import json
import time
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import base64
from plyer import notification


# ============================================================
# 配置区
# ============================================================

# 轮询间隔（秒）
POLL_INTERVAL = 300  # 5 分钟

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
STATE_FILE = BASE_DIR / "state.json"

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
            print(f"[错误] 登录请求失败，状态码: {resp.status_code}")
            return False

        try:
            result = resp.json()
        except json.JSONDecodeError:
            print(f"[错误] 登录响应不是有效的 JSON: {resp.text[:200]}")
            return False

        if result.get("status") == True:
            # 保存 cookies 中的用户信息
            self._uid = resp.cookies.get("_uid", "")
            self._fid = resp.cookies.get("fid", "")
            print(f"[登录] 成功，UID={self._uid}")
            return True
        else:
            msg = result.get("msg2", result.get("msg", "未知错误"))
            print(f"[登录] 失败: {msg}")
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
            print(f"[通知数] 请求失败: {e}")
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
        try:
            resp = self.session.post(COURSE_LIST_URL, data=data, headers=headers, timeout=15)
            if resp.status_code == 200:
                courses = parse_courses_from_html(resp.text)
                print(f"[课程] 获取到 {len(courses)} 门课程")
                return courses
        except Exception as e:
            print(f"[课程] 请求失败: {e}")
        return []

    def get_course_tokens(self, course: dict) -> dict:
        """
        Fetch the course landing middle page to resolve enc, workEnc, examEnc, openc, and t.
        """
        course_id = course.get("courseId", "")
        clazz_id = course.get("clazzId", "")
        cpi = course.get("cpi", "")
        if not all([course_id, clazz_id, cpi]):
            return {}

        url = f"https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ismooc2=1&v=2"
        try:
            resp = self.session.get(url, timeout=15)
            html = resp.text
            
            def extract_val(id_name):
                m = re.search(r'id="' + re.escape(id_name) + r'"[^>]*value="([^"]*)"', html)
                if not m:
                    m = re.search(r'name="' + re.escape(id_name) + r'"[^>]*value="([^"]*)"', html)
                return m.group(1) if m else None

            return {
                "openc": extract_val("openc"),
                "workEnc": extract_val("workEnc"),
                "examEnc": extract_val("examEnc"),
                "enc": extract_val("enc"),
                "t": extract_val("t")
            }
        except Exception as e:
            print(f"[错误] 获取课程 {course.get('title')} 的Token失败: {e}")
            return {}

    # ----- 章节/任务信息 -----

    def get_course_unfinished(self, course: dict) -> list:
        """
        获取课程中所有未完成的章节点
        解析 studentcourse 页面中的 knowledgeJobCount
        每个章节可能有多个任务（视频、作业、考试等）

        返回: [(章节标题, 未完成任务数), ...]
        """
        course_id = course.get("courseId", "")
        clazz_id = course.get("clazzId", "")
        cpi = course.get("cpi", "")
        if not all([course_id, clazz_id, cpi]):
            return []

        url = f"{COURSE_DETAIL_URL}?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ut=s"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return []

            unfinished = []
            html = resp.text

            # 解析章节单元
            # 每个 chapter_unit 包含多个 li，每个 li 代表一个章节点
            chapter_units = re.findall(
                r'<div class="chapter_unit"[^>]*>(.*?)</div>\s*</div>',
                html,
                re.DOTALL,
            )

            for unit_html in chapter_units:
                # 提取章节点
                points = re.findall(
                    r'<li[^>]*>.*?<div[^>]*id="(cur\d+)"[^>]*>.*?</div>\s*</li>',
                    unit_html,
                    re.DOTALL,
                )

                for point_id in points:
                    # 提取标题
                    title_match = re.search(
                        r'id="' + re.escape(point_id) + r'"[^>]*>.*?'
                        r'<a[^>]*class="clicktitle"[^>]*>(.*?)</a>',
                        html,
                        re.DOTALL,
                    )
                    title = title_match.group(1).strip() if title_match else point_id

                    # 提取未完成任务数
                    count_match = re.search(
                        r'<input[^>]*class="knowledgeJobCount"[^>]*value="(\d+)"',
                        html,
                    )
                    # 更精确：找当前 point 附近的 knowledgeJobCount
                    # 用 point_id 定位附近的 knowledgeJobCount
                    point_pos = html.find(f'id="{point_id}"')
                    if point_pos >= 0:
                        nearby = html[point_pos : point_pos + 1500]
                        count_match = re.search(
                            r'knowledgeJobCount[^>]*value="(\d+)"', nearby
                        )
                        if count_match:
                            count = int(count_match.group(1))
                            if count > 0:
                                unfinished.append((title, count))
                        else:
                            # 没有 JobCount 表示已完成，跳过
                            pass

            return unfinished

        except Exception as e:
            print(f"[章节] {course.get('title', course_id)} 查询失败: {e}")
            return []

    # ----- 章节卡片（未完成任务清单）-----

    def get_course_unfinished_chapters(self, course: dict) -> list:
        """
        获取课程中所有有未完成任务的章节（含 knowledge_id）
        解析 studentcourse 页面，提取 chapter_item 块
        返回: [{"knowledge_id": str, "title": str, "unfinished": int, "cpi": str}, ...]
        只返回 unfinished > 0 的章节
        """
        course_id = course.get("courseId", "")
        clazz_id = course.get("clazzId", "")
        cpi = course.get("cpi", "")
        if not all([course_id, clazz_id, cpi]):
            return []
        url = f"{COURSE_DETAIL_URL}?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ut=s"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return []
        except Exception as e:
            print(f"[章节] {course.get('title', course_id)} 查询失败: {e}")
            return []

        html = resp.text
        # 匹配 chapter_item 块（非贪婪，到下一个 chapter_item 或 </div></li>）
        chapter_pattern = re.compile(
            r'<div\s+class="chapter_item"\s+id="(cur\d+)"[^>]*>(.*?)(?=<div\s+class="chapter_item"|</div>\s*</li>)',
            re.DOTALL,
        )
        results = []
        for m in chapter_pattern.finditer(html):
            knowledge_id = m.group(1)[3:]  # 去 "cur"
            block = m.group(2)
            # 标题：<span class="catalog_sbar">X.Y</span> 名称
            title_m = re.search(
                r'<span\s+class="catalog_sbar">([^<]*)</span>\s*([^<]*)',
                block,
            )
            if title_m:
                full_title = f"{title_m.group(1).strip()} {title_m.group(2).strip()}".strip()
            else:
                full_title = knowledge_id
            # knowledgeJobCount
            cnt_m = re.search(
                r'<input\s+type="hidden"\s+value="(\d+)"\s+class="knowledgeJobCount"',
                block,
            )
            unfinished = int(cnt_m.group(1)) if cnt_m else 0
            if unfinished > 0:
                results.append({
                    "knowledge_id": knowledge_id,
                    "title": full_title,
                    "unfinished": unfinished,
                    "cpi": cpi,
                })
        return results

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
                print(f"[卡片] knowledgeid={knowledge_id} 请求失败: {e}")
                return []
            # 200 = 正常含数据；202 = 服务器限流/异步占位（无 mArg），需重试
            if resp.status_code == 202 or '"attachments":[' not in resp.text:
                # 指数退避：1s, 2s, 3s, 4s, 5s
                wait = 1.0 + attempt
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"[卡片] knowledgeid={knowledge_id} 状态码={resp.status_code}")
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

    def get_unfinished_tasks(self, courses: Optional[list] = None) -> list:
        """
        获取所有课程中未完成的作业/考试
        流程：遍历课程 → 取有未完成任务的章节 → 拉章节卡片 → 过滤出 workid 类型
        类型识别直接用 attachment 的 property.worktype（无需 HTTP 探测）
        返回: list[dict]，每个 dict 含 course/course_id/clazz_id/cpi/chapter/knowledge_id/workid/type/url/name
        """
        if courses is None:
            courses = self.get_course_list()
        results = []
        total_courses = len(courses)
        scanned = 0
        for course in courses:
            scanned += 1
            title = course.get("title") or course.get("courseId", "未知课程")
            course_id = course.get("courseId", "")
            clazz_id = course.get("clazzId", "")
            cpi = course.get("cpi", "")
            if not all([course_id, clazz_id, cpi]):
                continue
            chapters = self.get_course_unfinished_chapters(course)
            if not chapters:
                continue
            print(f"[{scanned}/{total_courses}] {title} - {len(chapters)} 个未完成章节")
            for ch in chapters:
                kid = ch["knowledge_id"]
                atts = self.get_chapter_attachments(course_id, clazz_id, kid)
                # 筛选作业/考试：attachment 包含 workid 字段
                work_atts = [a for a in atts if a.get("workid")]
                if work_atts:
                    print(f"  [命中] {ch['title']} - {len(work_atts)} 个 workid")
                for a in work_atts:
                    workid = a["workid"]
                    worktype = a.get("worktype")
                    ttype = self.classify_task(worktype)
                    task_name = a.get("name") or ""
                    # 构造 URL（按类型）
                    if ttype == "考试":
                        url = (
                            f"{EXAM_TEST_URL}?workId={workid}&classId={clazz_id}"
                            f"&cpi={cpi}&knowledgeId={kid}&ut=s"
                        )
                    else:
                        url = (
                            f"{WORK_HOMEWORK_URL}?workId={workid}&classId={clazz_id}"
                            f"&cpi={cpi}&knowledgeId={kid}&ut=s"
                        )
                    results.append({
                        "course": title,
                        "course_id": course_id,
                        "clazz_id": clazz_id,
                        "cpi": cpi,
                        "chapter": ch["title"],
                        "knowledge_id": kid,
                        "workid": workid,
                        "type": ttype,
                        "name": task_name,
                        "worktype": worktype,
                        "url": url,
                    })
                time.sleep(0.4)
        return results

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
            print(f"[活动] {course.get('title', course_id)} 查询失败: {e}")
        return []


# ============================================================
# 桌面通知
# ============================================================

def show_notification(title: str, message: str):
    """
    弹出 Windows 桌面通知（使用 plyer）
    plyer 在 Windows 上使用 winrt 实现原生 Toast 通知
    """
    try:
        notification.notify(
            title=title,
            message=message,
            app_name="学习通通知",
            timeout=8,
        )
    except Exception as e:
        print(f"[通知] 弹窗失败: {e}")


# ============================================================
# 状态管理
# ============================================================

def load_state() -> dict:
    """从 state.json 加载已通知状态"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "seen_notices": {},    # {课程ID: 最后已知的未完成任务列表hash}
        "last_notice_count": 0,
    }


def save_state(state: dict):
    """持久化状态到 state.json"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict:
    """加载配置文件"""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict):
    """保存配置文件"""
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# 主逻辑
# ============================================================

def initial_setup() -> tuple:
    """
    首次运行设置：提示用户输入手机号和密码
    保存到 config.json
    """
    print("=" * 50)
    print("  学习通通知小工具 - 首次设置")
    print("=" * 50)
    print("请输入学习通账号信息：")
    phone = input("手机号: ").strip()
    password = input("密码: ").strip()
    if not phone or not password:
        print("[错误] 手机号和密码不能为空")
        sys.exit(1)

    config = {"phone": phone, "password": password}
    save_config(config)
    print("[设置] 账号已保存到 config.json")
    return phone, password


def display_courses(client: XuexitongClient):
    """获取并打印所有课程名称（简洁版）"""
    courses = client.get_course_list()
    if not courses:
        print("[课程] 没有获取到课程数据")
        return

    print(f"\n  共 {len(courses)} 门课程：")
    for course in courses:
        title = course.get("title") or course.get("courseId", "未知课程")
        print(f"  · {title}")
    print()


def check_and_notify(client: XuexitongClient, state: dict) -> dict:
    """
    核心检测逻辑：
    1. 检查通知数
    2. 遍历所有课程，检查未完成任务
    3. 如果有新的未完成任务，弹出通知
    返回更新后的 state
    """
    # --- 第一步：检查通知数（轻量快速） ---
    notice_count = client.get_notice_count()
    if notice_count > 0:
        print(f"[检测] 未读通知数: {notice_count}")

    # 如果通知数有增加，标记需要深入检测
    need_deep_check = (
        notice_count > 0
        and notice_count != state.get("last_notice_count", 0)
    )
    state["last_notice_count"] = notice_count

    # --- 第二步：获取课程列表 ---
    courses = client.get_course_list()
    if not courses:
        print("[检测] 未获取到课程列表，跳过本次检测")
        return state

    # --- 第三步：遍历课程，检测未完成任务 ---
    seen = state.setdefault("seen_notices", {})
    summary_lines = []

    for course in courses:
        title = course.get("title", course.get("courseId", "未知课程"))
        course_key = course.get("courseId", "") or course.get("id", "")

        # 获取未完成的章节点
        unfinished = client.get_course_unfinished(course)
        total = sum(c for _, c in unfinished)

        if total > 0:
            # 计算当前课程的 hash 值，用于判断是否有变化
            current_hash = hash(str(sorted(unfinished)))
            last_hash = seen.get(course_key, 0)

            if current_hash != last_hash and need_deep_check:
                # 有新的未完成任务！
                items = [f"  • {ptitle}({cnt}项)" for ptitle, cnt in unfinished[:3]]
                detail = "\n".join(items)
                if len(unfinished) > 3:
                    detail += f"\n  ...还有 {len(unfinished) - 3} 个章节"

                msg = f"课程：{title}\n共有 {total} 个未完成任务\n\n{detail}"
                print(f"[新任务] {title}: {total} 个未完成")
                show_notification("📚 学习通 - 有新的作业/任务", msg)

            # 更新 state
            seen[course_key] = current_hash

        # 汇总显示
        brief = f"  {title}: {'⚠️ {}项'.format(total) if total else '✅ 完成'}"
        summary_lines.append(brief)

    # 打印本轮课程摘要
    if summary_lines:
        print("课程状态:")
        for line in summary_lines:
            print(line)

    # --- 第四步：如果有通知数但通知详情里没有找到具体任务，
    #             也可能是考试通知，发个通用提示 ---
    if notice_count > 0:
        # 检查是否至少有一个课程有未完成任务
        any_unfinished = any(
            client.get_course_unfinished(course)
            for course in courses[:3]  # 只检查前3门就够了
        )
        if not any_unfinished and need_deep_check:
            show_notification(
                "📢 学习通通知",
                f"您有 {notice_count} 条未读通知，请打开学习通查看",
            )

    return state


# ============================================================
# --list-tasks 子命令：列出未完成作业/考试
# ============================================================

def list_unfinished_tasks(client: XuexitongClient, output_file: Optional[Path] = None) -> list:
    """
    拉取所有未完成作业/考试，打印控制台表格，导出 JSON。
    output_file: JSON 写入路径，None 时用 BASE_DIR/unfinished_tasks.json
    """
    print("[扫描] 正在拉取课程列表...")
    courses = client.get_course_list()
    if not courses:
        print("[扫描] 未获取到课程列表")
        return []
    print(f"[扫描] 共 {len(courses)} 门课程，开始检查未完成任务点...")

    tasks = client.get_unfinished_tasks(courses)
    print(f"\n[完成] 共发现 {len(tasks)} 个未完成作业/考试")

    # 控制台表格
    print_tasks_table(tasks)

    # 导出 JSON
    out = output_file or (BASE_DIR / "unfinished_tasks.json")
    out.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[导出] 已写入 {out}（{out.stat().st_size} 字节）")
    return tasks


def print_tasks_table(tasks: list):
    """
    打印未完成作业/考试的控制台表格。
    格式：编号 | 课程 | 章节 | 类型 | 链接
    """
    if not tasks:
        print("  (无)")
        return
    # 计算列宽
    def truncate(s: str, n: int) -> str:
        s = str(s)
        return s if len(s) <= n else s[: n - 1] + "…"

    course_w = max(len("课程"), min(20, max(len(t["course"]) for t in tasks)))
    chapter_w = max(len("章节"), min(28, max(len(t["chapter"]) for t in tasks)))
    type_w = 6
    idx_w = max(4, len(str(len(tasks))))

    header = f"{'#':>{idx_w}}  {'课程':<{course_w}}  {'章节':<{chapter_w}}  {'类型':<{type_w}}  链接"
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)
    for i, t in enumerate(tasks, 1):
        course = truncate(t["course"], course_w)
        chapter = truncate(t["chapter"], chapter_w)
        ttype = t["type"]
        url = t["url"]
        print(f"{i:>{idx_w}}  {course:<{course_w}}  {chapter:<{chapter_w}}  {ttype:<{type_w}}  {url}")
    print(sep)


def run_list_tasks(output_file: Optional[Path] = None) -> Optional[list]:
    """
    --list-tasks 入口：登录 → 扫描 → 表格 + JSON
    """
    print("=" * 60)
    print("  学习通未完成作业/考试扫描")
    print("=" * 60)

    # 加载配置
    config = load_config()
    client = XuexitongClient()

    def do_login() -> bool:
        """执行登录，失败直接退出（不进入主循环）"""
        if client.login(config["phone"], config["password"]):
            return True
        print("[错误] 登录失败！请检查账号或加密方式")
        return False

    # 优先用 cookies
    if config.get("cookies") and client.load_cookies(config["cookies"]):
        test_count = client.get_notice_count()
        if test_count >= 0:
            print("[登录] 使用已保存的会话（有效）")
        else:
            print("[登录] 会话已过期，尝试密码登录")
            if not do_login():
                sys.exit(1)
    else:
        if not do_login():
            sys.exit(1)

    # 保存最新 cookies
    config["cookies"] = client.get_cookies()
    save_config(config)

    # 执行扫描
    list_unfinished_tasks(client, output_file)


def main():
    """主入口"""
    print("=" * 50)
    print("  学习通作业考试通知小工具")
    print("  Xuexitong Notifier v1.0")
    print("=" * 50)

    # 处理 --reset 参数：清除已保存的配置
    if "--reset" in sys.argv or "-r" in sys.argv:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
            print(f"[重置] 已清除 {CONFIG_FILE}")
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print(f"[重置] 已清除 {STATE_FILE}")
        print("[重置] 重新启动即可重新输入账号密码")
        return

    # 处理 --list-tasks 参数：列出未完成作业/考试并退出
    if "--list-tasks" in sys.argv or "-l" in sys.argv:
        # 自定义输出文件：--output xxx.json
        out_path = None
        for i, a in enumerate(sys.argv):
            if a in ("--output", "-o") and i + 1 < len(sys.argv):
                out_path = Path(sys.argv[i + 1])
                break
        run_list_tasks(out_path)
        return

    # 加载配置
    config = load_config()
    if not config.get("phone") or not config.get("password"):
        phone, password = initial_setup()
        config = load_config()  # 重新加载完整配置（含刚保存的）
    else:
        phone = config["phone"]
        password = config["password"]
        print(f"[配置] 已加载账号: {phone}")

    # 初始化客户端
    client = XuexitongClient()

    def do_login() -> bool:
        """执行登录，失败时提示重试"""
        if client.login(phone, password):
            return True
        print("[错误] 登录失败！可能是密码错误或加密方式已变更")
        retry = input("按 R 重新输入账号密码，或按 Enter 退出: ").strip().upper()
        if retry == "R":
            # 清除旧配置，重新开始
            if CONFIG_FILE.exists():
                CONFIG_FILE.unlink()
            return False
        sys.exit(1)

    # 如果有保存的 cookies，优先尝试
    if config.get("cookies"):
        if client.load_cookies(config["cookies"]):
            print("[登录] 尝试使用已保存的会话...")
            test_count = client.get_notice_count()
            if test_count >= 0:
                print("[登录] 会话有效，跳过密码登录")
            else:
                print("[登录] 会话已过期")
                while not do_login():
                    phone, password = initial_setup()
        else:
            while not do_login():
                phone, password = initial_setup()
    else:
        while not do_login():
            phone, password = initial_setup()

    # 保存 cookies 供下次使用
    config["cookies"] = client.get_cookies()
    save_config(config)

    # 显示课程列表
    display_courses(client)

    # 加载状态
    state = load_state()
    print(f"[启动] 轮询间隔: {POLL_INTERVAL}秒 ({POLL_INTERVAL // 60}分钟)")

    # 首轮立即检测
    print("\n[首轮] 正在检查未完成任务...")
    state = check_and_notify(client, state)
    save_state(state)
    print("[完成] 首轮检测完成")

    # 定时轮询
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{now}] 检查中...")
            state = check_and_notify(client, state)
            save_state(state)
            print(f"[{now}] 检测完成")
        except KeyboardInterrupt:
            print("\n[退出] 用户中断")
            save_state(state)
            break
        except Exception as e:
            print(f"[错误] 检测异常: {e}")
            # 异常后等待短时间继续
            time.sleep(60)


if __name__ == "__main__":
    main()
