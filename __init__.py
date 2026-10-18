"""
AnyRouter 签到插件 (MoviePilot)
用于 AnyRouter (NewAPI / OneAPI 架构) 站点的自动每日访问保号与签到。
通过 CloakBrowser 拟真无头 Chromium 环境自动突破阿里云 ESA / WAF 挑战，
注入登录会话与用户本地状态，自动触发签到并抓取最新余额。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.browser import PlaywrightHelper
import cloakbrowser
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


class AnyRouterSign(_PluginBase):
    plugin_name = "AnyRouter 签到"
    plugin_desc = "拟真浏览器突破阿里云 WAF，自动完成 AnyRouter 每日访问签到、监控余额并推送通知。"
    plugin_icon = "https://anyrouter.top/logo.png"
    plugin_version = "1.1.0"
    plugin_author = "momo"
    author_url = "https://anyrouter.top"
    plugin_config_prefix = "anyroutersign_"
    plugin_order = 15
    auth_level = 1

    # 配置项属性
    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _site_url: str = "https://anyrouter.top"
    _cookie: str = ""
    _user_id: str = "184129"
    _cron: str = "0 8 * * *"
    _use_browser: bool = True
    _max_retries: int = 3
    _retry_interval: int = 30
    _history_days: int = 30

    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if not config:
            return

        self._enabled = config.get("enabled", False)
        self._notify = config.get("notify", True)
        self._onlyonce = config.get("onlyonce", False)
        self._site_url = str(config.get("site_url") or "https://anyrouter.top").rstrip("/")
        self._cookie = str(config.get("cookie") or "").strip()
        self._user_id = str(config.get("user_id") or "184129").strip()
        self._cron = str(config.get("cron") or "0 8 * * *").strip()
        self._use_browser = config.get("use_browser", True)
        self._max_retries = int(config.get("max_retries") or 3)
        self._retry_interval = int(config.get("retry_interval") or 30)
        self._history_days = int(config.get("history_days") or 30)

        if not self._enabled:
            logger.info("AnyRouterSign 插件未启用")
            return

        if self._onlyonce:
            logger.info("AnyRouterSign 触发立即运行一次")
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config)
            from threading import Thread
            Thread(target=self.run_sign_with_retry, daemon=True, name="AnyRouterSign-once").start()

        # 启动定时任务
        if self._cron:
            try:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.run_sign_with_retry,
                    trigger=CronTrigger.from_crontab(self._cron),
                    id="anyroutersign_task",
                    name="AnyRouter每日签到",
                    replace_existing=True,
                )
                self._scheduler.start()
                logger.info(f"AnyRouterSign 定时服务启动成功，周期: {self._cron}")
            except Exception as e:
                logger.error(f"AnyRouterSign 定时任务启动失败: {e}")

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None

    def get_state(self) -> bool:
        return bool(self._enabled)

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def _parse_cookie_dict(self) -> dict[str, str]:
        cookies = {}
        if not self._cookie:
            return cookies
        for item in self._cookie.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                cookies[k.strip()] = v.strip()
        return cookies

    def _execute_sign_with_browser(self) -> Tuple[bool, str, dict]:
        """
        核心方案：使用 MoviePilot 内置 CloakBrowser 进行全拟真 Chromium 访问。
        能够自动解决阿里云 ESA / WAF 的 acw_sc__v2 挑战，并触发前端 React 自动签到。
        """
        cookie_dict = self._parse_cookie_dict()
        session_val = cookie_dict.get("session")
        if not session_val:
            return False, "Cookie 中未找到必需的 session 字段", {}

        parsed_url = urlparse(self._site_url)
        domain = parsed_url.hostname or "anyrouter.top"
        console_url = urljoin(self._site_url + "/", "console")

        details = {
            "console_ok": False,
            "api_ok": False,
            "username": "已登录用户",
            "message": "",
            "balance_usd": "-",
        }

        # 构造待注入的 Cookie 列表 (仅注入 session，让浏览器自动生成新鲜 WAF 票据)
        cookie_list = [{
            "name": "session",
            "value": session_val,
            "domain": domain,
            "path": "/"
        }]

        uid_num = int(self._user_id) if self._user_id.isdigit() else 184129

        # 前端 React 在挂载时检测 localStorage.getItem("user")
        # 只要存在合法的 user 对象，前端 useEffect 会自动调用 POST /api/user/sign_in
        init_script = f"""
        try {{
            localStorage.setItem('user', JSON.stringify({{
                id: {uid_num},
                username: 'momov587',
                display_name: 'Momov587',
                role: 1,
                status: 1
            }}));
        }} catch(e) {{}}
        """

        context = None
        try:
            logger.info("AnyRouterSign 启动 CloakBrowser 仿真浏览器突破 WAF...")
            context = cloakbrowser.launch_context(
                headless=True,
                user_agent=DEFAULT_UA
            )
            context.add_cookies(cookie_list)

            page = context.new_page()
            page.add_init_script(init_script)

            api_sign_response = {}
            api_user_response = {}

            def handle_response(res):
                nonlocal api_sign_response, api_user_response
                url = res.url
                if "/api/user/sign_in" in url:
                    try:
                        api_sign_response["status"] = res.status
                        api_sign_response["text"] = res.text()
                    except Exception:
                        pass
                elif "/api/user/self" in url:
                    try:
                        api_user_response["status"] = res.status
                        api_user_response["json"] = res.json()
                    except Exception:
                        pass

            page.on("response", handle_response)

            logger.info(f"AnyRouterSign 浏览器正在加载控制台: {console_url}")
            page.goto(console_url, wait_until="networkidle", timeout=35000)
            time.sleep(3)

            current_url = page.url
            page_title = page.title()
            page_content = page.content()

            # 严格检查是否被 WAF 阻断
            if "Denied by" in page_content or "403 Forbidden" in page_content or "acw_sc__v2" in page_content:
                denied_m = re.search(r"Denied by ([a-zA-Z0-9_]+)", page_content)
                reason = denied_m.group(0) if denied_m else "WAF 防火墙拦截"
                logger.warn(f"AnyRouterSign 页面被防火墙拦截: {reason}")
                return False, f"站点访问受阻 ({reason})，请稍后重试", details

            if "/login" in current_url:
                logger.warn("AnyRouterSign 页面跳转至登录页，Cookie 可能已过期")
                return False, "登录会话已失效，请更新 Cookie 中的 session", details

            details["console_ok"] = True
            logger.info(f"AnyRouterSign 控制台加载成功，当前页面: {current_url}")

            # 检查接口调用结果
            if api_sign_response:
                st = api_sign_response.get("status")
                txt = api_sign_response.get("text", "")
                if st == 200 and "{" in txt:
                    try:
                        s_data = json.loads(txt)
                        if s_data.get("success"):
                            details["api_ok"] = True
                            msg = s_data.get("message") or "签到成功 (接口响应正常)"
                            details["message"] = msg
                            logger.info(f"AnyRouterSign 签到接口响应: {msg}")
                    except Exception:
                        pass

            # 尝试从页面文本或接口提取余额与用户信息
            if api_user_response.get("json"):
                u_obj = api_user_response["json"].get("data") or {}
                uname = u_obj.get("display_name") or u_obj.get("username")
                if uname:
                    details["username"] = uname
                quota = u_obj.get("quota")
                if quota is not None:
                    details["balance_usd"] = f"${round(quota / 500000, 2):.2f}"
            else:
                # 从页面文本中解析余额
                body_text = page.inner_text("body")
                balance_match = re.search(r"Current balance\s*\n\s*\$?([0-9\.]+)", body_text, re.I)
                if not balance_match:
                    balance_match = re.search(r"当前余额\s*\n\s*\$?([0-9\.]+)", body_text)
                if balance_match:
                    details["balance_usd"] = f"${float(balance_match.group(1)):.2f}"

            # 判定结果
            if details["api_ok"]:
                msg = details["message"] or "签到接口调用成功"
                return True, msg, details
            elif details["console_ok"]:
                # 如果今日已签到过，页面仍会正常访问并显示余额
                msg = details["message"] or "已成功访问控制台并同步状态"
                return True, msg, details

            return False, "未能在页面中完成签到流程", details

        except Exception as e:
            logger.error(f"CloakBrowser 执行签到异常: {e}")
            return False, f"浏览器仿真执行异常: {str(e)[:100]}", details
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass

    def execute_sign(self) -> Tuple[bool, str, dict]:
        """
        执行签到任务入口
        """
        if not self._cookie:
            return False, "未配置站点 Cookie", {}

        return self._execute_sign_with_browser()

    def run_sign_with_retry(self):
        """
        带重试机制的签到主流程
        """
        logger.info(f"开始执行 AnyRouter ({self._site_url}) 签到任务...")
        success = False
        last_msg = ""
        details = {}

        for attempt in range(1, self._max_retries + 1):
            try:
                success, last_msg, details = self.execute_sign()
                if success:
                    logger.info(f"AnyRouter 签到成功 (第 {attempt} 次尝试): {last_msg}")
                    break
                else:
                    logger.warn(f"AnyRouter 签到未成功 (第 {attempt} 次): {last_msg}")
            except Exception as e:
                last_msg = str(e)
                logger.error(f"AnyRouter 签到异常 (第 {attempt} 次): {e}")

            if attempt < self._max_retries:
                time.sleep(self._retry_interval)

        status_text = "签到成功" if success else "签到失败"
        tz = pytz.timezone(settings.TZ)
        now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

        history_item = {
            "date": now_str,
            "status": status_text,
            "username": details.get("username", "已登录用户"),
            "balance_usd": details.get("balance_usd", "-"),
            "message": details.get("message", last_msg),
        }
        self._record_history(history_item)

        if self._notify:
            text = (
                f"站点: {self._site_url}\n"
                f"用户: {details.get('username', '已登录用户')}\n"
                f"当前余额: {details.get('balance_usd', '-')}\n"
                f"状态: {status_text}\n"
                f"结果: {details.get('message', last_msg)}\n"
                f"时间: {now_str}"
            )
            title = f"AnyRouter 签到: {status_text}"
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=text,
            )
            logger.info(f"已发送 AnyRouter 签到通知: {title}")

    def _record_history(self, item: dict):
        history: List[dict] = self.get_data("sign_history") or []
        history.insert(0, item)
        cutoff = datetime.now(pytz.timezone(settings.TZ)) - timedelta(days=self._history_days)
        valid = []
        for h in history:
            try:
                dt = datetime.strptime(h.get("date", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone(settings.TZ))
                if dt >= cutoff:
                    valid.append(h)
            except Exception:
                valid.append(h)
        self.save_data("sign_history", valid[:100])

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "开启通知"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_url",
                                            "label": "站点地址",
                                            "placeholder": "https://anyrouter.top",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {"model": "cron", "label": "签到周期 (默认每天 08:00)"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cookie",
                                            "label": "站点 Cookie",
                                            "rows": 4,
                                            "placeholder": "从浏览器复制完整 Cookie 或 session=... 字段粘贴到此处",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "user_id",
                                            "label": "用户数字 ID (New-API-User)",
                                            "placeholder": "例如 184129",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "use_browser",
                                            "label": "启用无头浏览器拟真过盾 (强烈推荐开启)",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "site_url": self._site_url,
            "cookie": self._cookie,
            "user_id": self._user_id,
            "cron": self._cron,
            "use_browser": self._use_browser,
            "max_retries": self._max_retries,
            "retry_interval": self._retry_interval,
            "history_days": self._history_days,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("sign_history") or []
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无签到记录。请在上方表单中配置站点 Cookie，保存并勾选【立即运行一次】进行测试。",
                        "class": "mb-2",
                    },
                }
            ]

        rows = []
        for h in history:
            st = h.get("status", "未知")
            color = "success" if "成功" in st else "error"
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": h.get("date", "-")},
                        {"component": "td", "text": h.get("username", "-")},
                        {"component": "td", "text": h.get("balance_usd", "-")},
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "VChip",
                                    "props": {"color": color, "size": "small"},
                                    "text": st,
                                }
                            ],
                        },
                        {"component": "td", "text": h.get("message", "-")},
                    ],
                }
            )

        return [
            {
                "component": "VTable",
                "props": {"density": "compact", "hover": True},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "th", "text": "时间"},
                                    {"component": "th", "text": "用户"},
                                    {"component": "th", "text": "当前余额"},
                                    {"component": "th", "text": "状态"},
                                    {"component": "th", "text": "返回结果"},
                                ],
                            }
                        ],
                    },
                    {"component": "tbody", "content": rows},
                ],
            }
        ]
