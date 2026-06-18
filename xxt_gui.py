import tkinter as tk
from tkinter import messagebox
from xxt_notifier import (
    XuexitongClient, load_config, save_config, setup_logging, logger,
    is_autostart_enabled, set_autostart,
    load_task_state, save_task_state,
    find_new_tasks, show_notification,
    mark_notice_read, extract_notices,
    check_for_update, CURRENT_VERSION,
)
import threading
import webbrowser
import sys
import io
from PIL import Image, ImageTk

try:
    import pystray
    from PIL import ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    logger.warning("pystray/Pillow 未安装，系统托盘功能不可用")

THEME = {
    "bg": "#181825",
    "card_bg": "#212130",
    "card_hover": "#2a2a3e",
    "input_bg": "#313145",
    "fg": "#ffffff",
    "fg_dim": "#bac2de",
    "fg_muted": "#6c7086",
    "accent": "#5966f3",
    "accent_hover": "#4852c9",
    "badge_hw": "#89b4fa",
    "badge_exam": "#f38ba8",
    "success": "#a6e3a1",
    "error": "#f38ba8",
    "border": "#313145",
    "deadline_warn": "#fab387",
}

class Switch(tk.Canvas):
    """滑动开关组件 — 用 Canvas 绘制的自定义 toggle switch

    设计：圆角矩形容器 + 圆形滑块，点击切换开/关状态
    """

    def __init__(self, master, initial_state=False, on_color="#a6e3a1",
                 off_color="#f38ba8", knob_color="#ffffff",
                 width=44, height=22, knob_margin=2, command=None,
                 label_text="", **kwargs):
        bg = kwargs.pop("bg", master.cget("bg"))
        super().__init__(master, width=width, height=height,
                         bd=0, highlightthickness=0, bg=bg, **kwargs)
        self._on_color = on_color
        self._off_color = off_color
        self._knob_color = knob_color
        self._sw_width = width
        self._sw_height = height
        self._knob_margin = knob_margin
        self._knob_radius = height // 2 - knob_margin
        self._knob_x_left = knob_margin + self._knob_radius
        self._knob_x_right = width - knob_margin - self._knob_radius
        self._state = initial_state
        self._command = command

        # 可选文字（显示在开关左侧）
        self._label = label_text

        # 背景圆角矩形（pill）
        self._bg_radius = height // 2
        self._bg_item = None
        # 滑块圆形
        self._knob_item = None

        # 如果有文字，在左侧创建一个 Label
        if label_text:
            self._lbl = tk.Label(self, text=label_text, bg=bg, fg=THEME["fg"],
                                 font=("Microsoft YaHei", 10, "bold"))
            # 手动布局：调整 Canvas 宽度以容纳文字
            self._lbl.place(x=0, y=0)
            self.configure(width=width + 80)
            # 记录 Canvas 内容的偏移
            self._text_offset = 80
            self._bg_center_x = width / 2 + self._text_offset
            self._knob_x_left += self._text_offset
            self._knob_x_right += self._text_offset
        else:
            self._text_offset = 0
            self._bg_center_x = width / 2

        self._draw()
        self.bind("<Button-1>", self._on_click)

    def _draw(self):
        """绘制开关背景 + 滑块"""
        self.delete("all")
        if self._label:
            self._lbl.lift()
        bg_color = self._on_color if self._state else self._off_color
        # 背景 pill 形状
        self._bg_item = self.create_rounded_rect(
            self._text_offset + self._knob_margin, self._knob_margin,
            self._text_offset + self._sw_width - self._knob_margin, self._sw_height - self._knob_margin,
            r=self._bg_radius, fill=bg_color, outline=""
        )
        # 滑块圆形
        cx = self._knob_x_right if self._state else self._knob_x_left
        cy = self._sw_height / 2
        r = self._knob_radius
        self._knob_item = self.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=self._knob_color, outline=""
        )

    def create_rounded_rect(self, x1, y1, x2, y2, r=10, **kwargs):
        """绘制圆角矩形（两点之间）"""
        pts = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        return self.create_polygon(pts, smooth=True, **kwargs)

    def _on_click(self, event):
        """点击切换状态"""
        new_state = not self._state
        if self._command:
            # 先执行回调，只有成功时才翻转视觉状态，避免失败时的闪烁回滚
            result = self._command(new_state)
            if result is False:
                return  # 回调明确返回 False，保持原状态不动
        # 回调成功（或没有回调），更新状态并重绘
        self._state = new_state
        self._draw()

    def set(self, state):
        """外部设置状态"""
        self._state = state
        self._draw()

    def get(self):
        """获取当前状态"""
        return self._state


class XuexitongApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"学习通作业/考试扫描器 v{CURRENT_VERSION}")
        self.root.geometry("850x650")
        self.root.configure(bg=THEME["bg"])
        
        self.client = XuexitongClient()
        self.config = load_config()
        self._tray_icon = None
        
        # 关闭按钮 → 弹窗询问是最小化到系统托盘还是完全退出程序
        self.root.protocol("WM_DELETE_WINDOW", self.confirm_close)
        
        # 初始化系统托盘图标
        self._setup_tray()
        
        # Main container
        self.container = tk.Frame(self.root, bg=THEME["bg"])
        self.container.pack(side="top", fill="both", expand=True)
        
        self.current_frame = None
        self.try_auto_login()

    # --------- 系统托盘 ---------

    def _create_tray_icon_image(self):
        """生成托盘图标图片（紫色圆角方块 + 白色 ✓）"""
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # 紫色圆角背景
        draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill='#5966f3')
        # 白色对勾
        draw.line([(16, 32), (28, 46), (48, 18)], fill='white', width=5)
        return img

    def _setup_tray(self):
        """设置系统托盘图标（后台线程运行）"""
        if not HAS_TRAY:
            return
        try:
            icon_image = self._create_tray_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem('打开主界面', self._tray_restore, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('完全退出', self._tray_quit),
            )
            self._tray_icon = pystray.Icon(
                'xxt_notifier', icon_image, '学习通扫描器', menu
            )
            # pystray 需要在独立线程运行
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as e:
            logger.warning(f"系统托盘初始化失败: {e}")

    def minimize_to_tray(self):
        """最小化到系统托盘（关闭窗口时调用）"""
        self.root.withdraw()  # 隐藏窗口，但 mainloop 和定时器继续运行
        if HAS_TRAY and self._tray_icon:
            show_notification(
                "学习通扫描器",
                "已最小化到系统托盘\n后台继续监控新任务",
                tk_root=self.root
            )
        logger.info("[GUI] 最小化到系统托盘，后台继续监控")

    def confirm_close(self):
        """点击窗口 X 按钮时的询问弹窗"""
        # 如果系统托盘不可用，则直接完全退出
        if not HAS_TRAY:
            self.quit_app()
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("关闭确认")
        dialog.configure(bg=THEME["card_bg"])
        dialog.resizable(False, False)

        # 设为模态，防止操作主窗口
        dialog.transient(self.root)
        dialog.grab_set()

        # 居中定位在主窗口上层
        self.root.update_idletasks()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()

        dw = 420
        dh = 180
        dx = root_x + (root_w - dw) // 2
        dy = root_y + (root_h - dh) // 2
        dialog.geometry(f"{dw}x{dh}+{dx}+{dy}")

        # 在 Windows 上隐藏最小化最大化按钮
        try:
            dialog.attributes("-toolwindow", 1)
        except Exception:
            pass

        # 提示内容
        lbl_title = tk.Label(
            dialog,
            text="关闭选项",
            font=("Microsoft YaHei", 12, "bold"),
            bg=THEME["card_bg"],
            fg=THEME["fg"]
        )
        lbl_title.pack(pady=(20, 10))

        lbl_desc = tk.Label(
            dialog,
            text="您希望将程序最小化到系统托盘，还是完全退出程序？",
            font=("Microsoft YaHei", 10),
            bg=THEME["card_bg"],
            fg=THEME["fg_dim"],
            wraplength=380,
            justify="center"
        )
        lbl_desc.pack(pady=(0, 20))

        # 按钮容器
        btn_frame = tk.Frame(dialog, bg=THEME["card_bg"])
        btn_frame.pack(fill="x", padx=30)

        # 点击动作定义
        def on_minimize():
            dialog.destroy()
            self.minimize_to_tray()

        def on_exit():
            dialog.destroy()
            self.quit_app()

        def on_cancel():
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        # 最小化到托盘按钮 (Accent 颜色)
        btn_min = tk.Button(
            btn_frame,
            text="最小化到托盘",
            font=("Microsoft YaHei", 10, "bold"),
            bg=THEME["accent"],
            fg=THEME["fg"],
            activebackground=THEME["accent_hover"],
            activeforeground=THEME["fg"],
            bd=0,
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
            command=on_minimize
        )
        btn_min.pack(side="left", expand=True, fill="x", padx=5)
        btn_min.bind("<Enter>", lambda e: btn_min.configure(bg=THEME["accent_hover"]))
        btn_min.bind("<Leave>", lambda e: btn_min.configure(bg=THEME["accent"]))

        # 完全退出按钮 (红色)
        btn_exit = tk.Button(
            btn_frame,
            text="完全退出",
            font=("Microsoft YaHei", 10, "bold"),
            bg="#e05f65",
            fg=THEME["fg"],
            activebackground="#bd4f54",
            activeforeground=THEME["fg"],
            bd=0,
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
            command=on_exit
        )
        btn_exit.pack(side="left", expand=True, fill="x", padx=5)
        btn_exit.bind("<Enter>", lambda e: btn_exit.configure(bg="#bd4f54"))
        btn_exit.bind("<Leave>", lambda e: btn_exit.configure(bg="#e05f65"))

        # 取消按钮 (灰色)
        btn_cancel = tk.Button(
            btn_frame,
            text="取消",
            font=("Microsoft YaHei", 10, "bold"),
            bg=THEME["fg_muted"],
            fg=THEME["fg"],
            activebackground="#585b70",
            activeforeground=THEME["fg"],
            bd=0,
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
            command=on_cancel
        )
        btn_cancel.pack(side="left", expand=True, fill="x", padx=5)
        btn_cancel.bind("<Enter>", lambda e: btn_cancel.configure(bg="#585b70"))
        btn_cancel.bind("<Leave>", lambda e: btn_cancel.configure(bg=THEME["fg_muted"]))

    def _tray_restore(self, icon=None, item=None):
        """从托盘恢复窗口（pystray 回调在非主线程，需要 after 调度）"""
        self.root.after(0, self._do_restore)

    def _do_restore(self):
        """在主线程中恢复窗口"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        logger.info("[GUI] 从系统托盘恢复窗口")

    def _tray_quit(self, icon=None, item=None):
        """完全退出（pystray 回调）"""
        self.root.after(0, self.quit_app)

    def quit_app(self):
        """完全退出应用"""
        logger.info("[GUI] 完全退出")
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def show_frame(self, frame_class):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = frame_class(self.container, self)
        self.current_frame.pack(fill="both", expand=True)

    def try_auto_login(self):
        """Try logging in using existing cookies from config"""
        if self.config.get("cookies"):
            if self.client.load_cookies(self.config["cookies"]):
                try:
                    courses = self.client.get_course_list()
                    if courses:
                        logger.info("[GUI] Auto-login successful using cookies")
                        self.show_frame(DashboardFrame)
                        return
                except Exception as e:
                    logger.warning(f"[GUI] Auto-login check failed: {e}")
                    
        logger.info("[GUI] Session expired or invalid, show login frame")
        self.show_frame(LoginFrame)

class LoginFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=THEME["bg"])
        self.controller = controller
        self.current_tab = "password"  # "password" or "qrcode"
        
        self.qr_uuid = None
        self.qr_enc = None
        self.qr_polling = False
        
        # Center panel card
        self.card = tk.Frame(self, bg=THEME["card_bg"], padx=40, pady=30)
        self.card.place(relx=0.5, rely=0.5, anchor="center")
        
        # Tab Header Buttons
        self.tab_frame = tk.Frame(self.card, bg=THEME["card_bg"])
        self.tab_frame.pack(fill="x", pady=(0, 20))
        
        self.btn_tab_pwd = tk.Button(
            self.tab_frame, text="账号密码登录", font=("Microsoft YaHei", 11, "bold"),
            bg=THEME["accent"], fg=THEME["fg"], activebackground=THEME["accent_hover"],
            activeforeground=THEME["fg"], bd=0, relief="flat", padx=15, pady=8,
            cursor="hand2", command=lambda: self.switch_tab("password")
        )
        self.btn_tab_pwd.pack(side="left", expand=True, fill="x", padx=(0, 2))
        
        self.btn_tab_qr = tk.Button(
            self.tab_frame, text="扫码安全登录", font=("Microsoft YaHei", 11, "bold"),
            bg=THEME["input_bg"], fg=THEME["fg_dim"], activebackground=THEME["card_hover"],
            activeforeground=THEME["fg"], bd=0, relief="flat", padx=15, pady=8,
            cursor="hand2", command=lambda: self.switch_tab("qrcode")
        )
        self.btn_tab_qr.pack(side="left", expand=True, fill="x", padx=(2, 0))
        
        # --- Frame 1: Password Frame ---
        self.frame_password = tk.Frame(self.card, bg=THEME["card_bg"])
        
        # Phone
        lbl_phone = tk.Label(self.frame_password, text="手机号", bg=THEME["card_bg"], fg=THEME["fg_dim"], font=("Microsoft YaHei", 10))
        lbl_phone.pack(anchor="w", pady=(0, 5))
        self.ent_phone = tk.Entry(self.frame_password, bg=THEME["input_bg"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                  font=("Microsoft YaHei", 12), bd=0, relief="flat", width=30)
        self.ent_phone.pack(pady=(0, 15), ipady=8)
        
        saved_phone = self.controller.config.get("phone", "")
        if saved_phone:
            self.ent_phone.insert(0, saved_phone)
            
        # Password
        lbl_pwd = tk.Label(self.frame_password, text="密码", bg=THEME["card_bg"], fg=THEME["fg_dim"], font=("Microsoft YaHei", 10))
        lbl_pwd.pack(anchor="w", pady=(0, 5))
        self.ent_pwd = tk.Entry(self.frame_password, bg=THEME["input_bg"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                font=("Microsoft YaHei", 12), show="*", bd=0, relief="flat", width=30)
        self.ent_pwd.pack(pady=(0, 10), ipady=8)
        
        saved_pwd = self.controller.config.get("password", "")
        if saved_pwd:
            self.ent_pwd.insert(0, saved_pwd)

        # Status text for password login
        self.lbl_status_pwd = tk.Label(self.frame_password, text="", bg=THEME["card_bg"], fg=THEME["error"], font=("Microsoft YaHei", 10))
        self.lbl_status_pwd.pack(pady=(5, 10))
        
        # Login Button
        self.btn_login = tk.Button(self.frame_password, text="登  录", bg=THEME["accent"], fg=THEME["fg"], 
                                   activebackground=THEME["accent_hover"], activeforeground=THEME["fg"],
                                   font=("Microsoft YaHei", 12, "bold"), bd=0, relief="flat", width=28,
                                   command=self.perform_login)
        self.btn_login.pack(ipady=8)
        self.btn_login.bind("<Enter>", lambda e: self.btn_login.configure(bg=THEME["accent_hover"]))
        self.btn_login.bind("<Leave>", lambda e: self.btn_login.configure(bg=THEME["accent"]))
        
        # Show Password Frame by default
        self.frame_password.pack(fill="both", expand=True)

        # --- Frame 2: QR Code Frame ---
        self.frame_qrcode = tk.Frame(self.card, bg=THEME["card_bg"])
        
        # QR Image label
        self.lbl_qr_image = tk.Label(self.frame_qrcode, bg=THEME["input_bg"], width=200, height=200)
        self.lbl_qr_image.pack(pady=(10, 15))
        
        # Status text for QR login
        self.lbl_status_qr = tk.Label(self.frame_qrcode, text="正在加载二维码...", bg=THEME["card_bg"], fg=THEME["fg_dim"], font=("Microsoft YaHei", 10), wraplength=280)
        self.lbl_status_qr.pack(pady=(0, 15))
        
        # Refresh QR Button
        self.btn_refresh_qr = tk.Button(
            self.frame_qrcode, text="刷新二维码", bg=THEME["accent"], fg=THEME["fg"],
            activebackground=THEME["accent_hover"], activeforeground=THEME["fg"],
            font=("Microsoft YaHei", 10, "bold"), bd=0, relief="flat", padx=15, pady=6,
            cursor="hand2", command=self.refresh_qr_code
        )
        self.btn_refresh_qr.bind("<Enter>", lambda e: self.btn_refresh_qr.configure(bg=THEME["accent_hover"]))
        self.btn_refresh_qr.bind("<Leave>", lambda e: self.btn_refresh_qr.configure(bg=THEME["accent"]))

    def switch_tab(self, tab):
        if self.current_tab == tab:
            return
        
        self.current_tab = tab
        if tab == "password":
            self.qr_polling = False
            self.btn_tab_pwd.configure(bg=THEME["accent"], fg=THEME["fg"])
            self.btn_tab_qr.configure(bg=THEME["input_bg"], fg=THEME["fg_dim"])
            self.frame_qrcode.pack_forget()
            self.frame_password.pack(fill="both", expand=True)
        else:
            self.btn_tab_pwd.configure(bg=THEME["input_bg"], fg=THEME["fg_dim"])
            self.btn_tab_qr.configure(bg=THEME["accent"], fg=THEME["fg"])
            self.frame_password.pack_forget()
            self.frame_qrcode.pack(fill="both", expand=True)
            self.refresh_qr_code()

    def refresh_qr_code(self):
        self.qr_uuid = None
        self.qr_enc = None
        self.qr_polling = False
        self.lbl_status_qr.configure(text="正在从学习通获取二维码...", fg=THEME["fg_dim"])
        self.btn_refresh_qr.pack_forget()
        self.lbl_qr_image.configure(image="", text="⌛", font=("Microsoft YaHei", 24), fg=THEME["fg_dim"])
        
        def bg_fetch():
            uuid, enc, qr_bytes = self.controller.client.get_qr_code_params()
            self.after(0, lambda: self.on_qr_fetched(uuid, enc, qr_bytes))
            
        threading.Thread(target=bg_fetch, daemon=True).start()

    def on_qr_fetched(self, uuid, enc, qr_bytes):
        if self.current_tab != "qrcode":
            return
            
        if not uuid or not qr_bytes:
            self.lbl_status_qr.configure(text="获取二维码失败，请重试", fg=THEME["error"])
            self.lbl_qr_image.configure(text="❌", font=("Microsoft YaHei", 24), fg=THEME["error"])
            self.btn_refresh_qr.pack(pady=(0, 10))
            return
            
        try:
            img_data = io.BytesIO(qr_bytes)
            pil_img = Image.open(img_data)
            
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                try:
                    resample = Image.LANCZOS
                except AttributeError:
                    resample = Image.ANTIALIAS
                    
            pil_img = pil_img.resize((200, 200), resample)
            self.qr_photo = ImageTk.PhotoImage(pil_img)
            self.lbl_qr_image.configure(image=self.qr_photo, text="")
            
            self.qr_uuid = uuid
            self.qr_enc = enc
            self.qr_polling = True
            
            self.lbl_status_qr.configure(text="请使用学习通 App 扫码登录", fg=THEME["fg_dim"])
            self.after(2000, self.poll_qr_status)
        except Exception as e:
            logger.error(f"渲染二维码失败: {e}")
            self.lbl_status_qr.configure(text=f"图片解析失败: {e}", fg=THEME["error"])
            self.lbl_qr_image.configure(text="❌", font=("Microsoft YaHei", 24), fg=THEME["error"])
            self.btn_refresh_qr.pack(pady=(0, 10))

    def poll_qr_status(self):
        if not self.qr_polling or self.current_tab != "qrcode" or not self.qr_uuid:
            return
            
        def bg_poll():
            res = self.controller.client.check_qr_login_status(self.qr_uuid, self.qr_enc)
            self.after(0, lambda: self.on_poll_result(res))
            
        threading.Thread(target=bg_poll, daemon=True).start()

    def on_poll_result(self, res):
        if not self.qr_polling or self.current_tab != "qrcode":
            return
            
        status = res.get("status")
        type_code = res.get("type")
        mes = res.get("mes", "未知状态")
        
        if status == True:
            self.qr_polling = False
            self.controller.config["cookies"] = self.controller.client.get_cookies()
            save_config(self.controller.config)
            
            self.lbl_status_qr.configure(text="登录成功！正在跳转...", fg=THEME["success"])
            self.after(500, lambda: self.controller.show_frame(DashboardFrame))
        else:
            if type_code == "3":
                self.lbl_status_qr.configure(text="请使用学习通 App 扫描二维码", fg=THEME["fg_dim"])
                self.after(2000, self.poll_qr_status)
            elif type_code == "4":
                self.lbl_status_qr.configure(text="已扫描，请在手机端点击【确认登录】", fg=THEME["badge_hw"])
                self.after(2000, self.poll_qr_status)
            elif type_code == "2":
                self.qr_polling = False
                self.lbl_status_qr.configure(text="二维码已失效，请点击刷新", fg=THEME["error"])
                self.btn_refresh_qr.pack(pady=(0, 10))
            else:
                self.lbl_status_qr.configure(text=f"{mes}，请刷新重试", fg=THEME["error"])
                self.btn_refresh_qr.pack(pady=(0, 10))

    def perform_login(self):
        phone = self.ent_phone.get().strip()
        pwd = self.ent_pwd.get().strip()
        
        if not phone or not pwd:
            self.lbl_status_pwd.configure(text="手机号或密码不能为空", fg=THEME["error"])
            return
            
        self.lbl_status_pwd.configure(text="正在登录...", fg=THEME["fg_dim"])
        self.btn_login.configure(state="disabled")
        self.update_idletasks()
        
        try:
            success = self.controller.client.login(phone, pwd)
            if success:
                self.controller.config["phone"] = phone
                self.controller.config["password"] = pwd
                self.controller.config["cookies"] = self.controller.client.get_cookies()
                save_config(self.controller.config)
                
                self.lbl_status_pwd.configure(text="登录成功！正在跳转...", fg=THEME["success"])
                self.update_idletasks()
                self.after(500, lambda: self.controller.show_frame(DashboardFrame))
            else:
                self.lbl_status_pwd.configure(text="登录失败，请检查账号密码", fg=THEME["error"])
                self.btn_login.configure(state="normal")
        except Exception as e:
            self.lbl_status_pwd.configure(text=f"异常: {e}", fg=THEME["error"])
            self.btn_login.configure(state="normal")

    def destroy(self):
        self.qr_polling = False
        super().destroy()


class ScrollableFrame(tk.Frame):
    def __init__(self, container, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, bg=THEME["bg"])
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=THEME["bg"])

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            )
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        
        self.canvas.bind('<Configure>', self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        
        # Mousewheel binding (bound when entering canvas, unbound when leaving)
        self.canvas.bind("<Enter>", lambda _: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda _: self.canvas.unbind_all("<MouseWheel>"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class DashboardFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=THEME["bg"])
        self.controller = controller
        
        self.tasks_list = []  # List of all uncompleted task dicts
        self.notices_list = []  # List of all inbox notices
        self.current_tab = "作业"  # '作业' or '考试' or '通知'
        self._is_fetching = False
        self._poll_job_id = None  # 后台轮询定时器 ID，用于取消/防堆叠
        self.is_first_scan = True
        
        # 1. Header Frame
        header = tk.Frame(self, bg=THEME["card_bg"], height=70, padx=20)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        
        user_info = f"账号: {self.controller.config.get('phone', '未知')}"
        lbl_user = tk.Label(header, text=user_info, bg=THEME["card_bg"], fg=THEME["fg"], font=("Microsoft YaHei", 12, "bold"))
        lbl_user.pack(side="left")
        
        # Refresh Button
        self.btn_refresh = tk.Button(header, text="刷 新 🔄", bg=THEME["accent"], fg=THEME["fg"],
                                     activebackground=THEME["accent_hover"], activeforeground=THEME["fg"],
                                     font=("Microsoft YaHei", 10, "bold"), bd=0, relief="flat", padx=15, pady=5,
                                     command=lambda: self.fetch_and_render(force=True))
        self.btn_refresh.pack(side="right", padx=(10, 0))
        self.btn_refresh.bind("<Enter>", lambda e: self.btn_refresh.configure(bg=THEME["accent_hover"]))
        self.btn_refresh.bind("<Leave>", lambda e: self.btn_refresh.configure(bg=THEME["accent"]))
        
        # 开机自启动 — 滑动开关
        self._autostart_on = is_autostart_enabled()
        self.switch_autostart = Switch(
            header, initial_state=self._autostart_on,
            on_color=THEME["success"], off_color=THEME["error"],
            label_text="开机自启动",
            command=self._on_autostart_toggle
        )
        self.switch_autostart.pack(side="right", padx=(10, 0))
        
        # Logout Button
        self.btn_logout = tk.Button(header, text="退出登录", bg=THEME["fg_muted"], fg=THEME["fg"],
                                    activebackground="#585b70", activeforeground=THEME["fg"],
                                    font=("Microsoft YaHei", 10, "bold"), bd=0, relief="flat", padx=12, pady=5,
                                    command=self.logout)
        self.btn_logout.pack(side="right", padx=(0, 5))
        self.btn_logout.bind("<Enter>", lambda e: self.btn_logout.configure(bg="#585b70"))
        self.btn_logout.bind("<Leave>", lambda e: self.btn_logout.configure(bg=THEME["fg_muted"]))
        
        # 2. Navigation / Tabs Bar
        nav_bar = tk.Frame(self, bg=THEME["bg"], padx=20)
        nav_bar.pack(side="top", fill="x", pady=(15, 2))
        
        self.btn_tab_hw = tk.Button(nav_bar, text="📝 未完成作业", bg=THEME["accent"], fg=THEME["fg"],
                                    font=("Microsoft YaHei", 11, "bold"), bd=0, relief="flat", width=14,
                                    command=lambda: self.switch_tab("作业"))
        self.btn_tab_hw.pack(side="left", padx=(0, 10), ipady=6)
        
        self.btn_tab_exam = tk.Button(nav_bar, text="📋 未完成考试", bg=THEME["card_bg"], fg=THEME["fg_dim"],
                                     font=("Microsoft YaHei", 11, "bold"), bd=0, relief="flat", width=14,
                                     command=lambda: self.switch_tab("考试"))
        self.btn_tab_exam.pack(side="left", ipady=6)

        self.btn_tab_notice = tk.Button(nav_bar, text="🔔 消息通知", bg=THEME["card_bg"], fg=THEME["fg_dim"],
                                       font=("Microsoft YaHei", 11, "bold"), bd=0, relief="flat", width=14,
                                       command=lambda: self.switch_tab("通知"))
        self.btn_tab_notice.pack(side="left", padx=(10, 0), ipady=6)
        
        # Task unread count badges (red accent for unread)
        self.lbl_hw_count = tk.Label(nav_bar, text="", bg=THEME["bg"], fg=THEME["error"], font=("Microsoft YaHei", 10, "bold"))
        self.lbl_hw_count.pack(side="left", padx=(0, 0))
        
        self.lbl_exam_count = tk.Label(nav_bar, text="", bg=THEME["bg"], fg=THEME["error"], font=("Microsoft YaHei", 10, "bold"))
        self.lbl_exam_count.pack(side="left", padx=(5, 0))

        self.lbl_notice_count = tk.Label(nav_bar, text="", bg=THEME["bg"], fg=THEME["error"], font=("Microsoft YaHei", 10, "bold"))
        self.lbl_notice_count.pack(side="left", padx=(5, 0))

        # Loading Indicator Label
        self.lbl_loading = tk.Label(self, text="", bg=THEME["bg"], fg=THEME["fg_dim"], font=("Microsoft YaHei", 10))
        self.lbl_loading.pack(side="top", fill="x", pady=(2, 2))
        
        # 3. Main Tasks View Area
        self.scroll_frame = ScrollableFrame(self)
        self.scroll_frame.pack(side="top", fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Auto-fetch on load
        self.after(100, self.fetch_and_render)

    def schedule_poll(self):
        """安排下一次后台轮询（每 poll_interval 秒自动刷新一次）。"""
        poll_ms = self.controller.config.get("poll_interval", 300) * 1000
        self._poll_job_id = self.after(poll_ms, self.fetch_and_render)

    def _update_unread_badges(self):
        """Update the notification tab badge showing unread count from state."""
        state = load_task_state()
        unread_ids = set(state.get("unread_notice_ids", []))
        cutoff_ts = state.get("cutoff_timestamp", 0) or 0
        hidden_ids = set(state.get("hidden_notice_ids", []))
        count = sum(1 for n in getattr(self, 'notices_list', [])
                    if (n.get("idCode") or "") in unread_ids
                    and n.get("insertTime", 0) >= cutoff_ts
                    and n.get("idCode") not in hidden_ids)
        self.lbl_notice_count.configure(text=f"({count})" if count else "")

    def _check_unread_reminder(self, root):
        """Check if there are unacknowledged notices and show a persistent reminder.
        Called after each automatic poll cycle."""
        state = load_task_state()
        unread_ids = set(state.get("unread_notice_ids", []))
        cutoff_ts = state.get("cutoff_timestamp", 0) or 0
        hidden_ids = set(state.get("hidden_notice_ids", []))
        # Count visible notices that are still in unread set
        count = sum(1 for n in getattr(self, 'notices_list', [])
                    if (n.get("idCode") or "") in unread_ids
                    and n.get("insertTime", 0) >= cutoff_ts
                    and n.get("idCode") not in hidden_ids)
        if count > 0:
            show_notification(
                "🔔 你有未读通知",
                f"还有 {count} 条通知未标记已读，请查看",
                tk_root=root
            )

    def fetch_and_render(self, force=False):
        """Fetch unfinished tasks in a background thread, then render on completion.
        参数:
            force: 为 True 时清除课程的冷却标记，强制重新请求（手动刷新时使用）。
        """
        if self._is_fetching:
            return
        # 取消已排队的轮询（防止手动点击+定时器堆叠）
        if self._poll_job_id:
            self.after_cancel(self._poll_job_id)
            self._poll_job_id = None
        self._is_fetching = True

        # 手动刷新时清冷却，强制重新请求被反爬拦截的课程
        if force:
            logger.info("[GUI] 手动刷新：清除所有课程冷却标记，强制重新请求")
            self.controller.client.clear_cooldowns()

        self.btn_refresh.configure(state="disabled", text="扫描中...")
        self.lbl_loading.configure(text="⏳ 正在扫描所有课程的作业和考试，请稍候...")
        
        is_first = self.is_first_scan or force
        self.is_first_scan = False

        cfg = dict(self.controller.config)
        cfg["is_first_scan"] = is_first
        cfg["retry_round_wait"] = 60

        def update_progress_lbl(data):
            msg = data.get("message", "正在扫描...")
            self.after(0, lambda: self.lbl_loading.configure(text=msg, fg=THEME["fg_dim"]))

        def _worker(manual=force):
            """manual=True 表示手动刷新，完成后弹通知；
               manual=False 表示自动轮询，有新任务才弹。
            """
            try:
                tasks = self.controller.client.get_unfinished_tasks(config=cfg, progress_callback=update_progress_lbl)
            except Exception as e:
                logger.error(f"[GUI] Fetch error: {e}")
                tasks = []

            # 获取未读通知数和通知列表数据
            notice_count = -1
            new_notices_data = {}
            try:
                if self.controller.client._uid:
                    notice_count = self.controller.client.get_notice_count()
                    if notice_count >= 0:
                        from datetime import datetime
                        current_year = str(datetime.now().year)
                        new_notices_data = self.controller.client.get_notice_list(year=current_year)
            except Exception as e:
                logger.error(f"[GUI] Fetch notice count or list error: {e}")

            # Schedule UI update on the main thread
            # 注意：不再在扫面前清空卡片，而是扫完后有数据才替换，
            # 避免扫描过程中出现白屏，也避免扫描返回空时清掉上次结果
            self.after(0, lambda: self._on_fetch_complete(tasks, notice_count=notice_count, new_notices_data=new_notices_data, manual=manual))
        
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def _on_fetch_complete(self, tasks, notice_count=-1, new_notices_data=None, manual=False):
        """Called on the main thread after background fetch completes.
        参数:
            tasks: 扫描到的任务列表
            notice_count: 扫描到的未读通知数
            new_notices_data: 扫描到的通知列表 JSON 数据
            manual: True=手动刷新，完成后弹通知；False=自动轮询，有新任务才弹。
        """
        self._is_fetching = False

        # ===== 优先处理新通知弹窗与状态保存 =====
        from xxt_notifier import load_task_state, save_task_state, extract_notices
        state = load_task_state()
        root = self.controller.root
        has_new_notice = False

        fetched_notices = extract_notices(new_notices_data)
        fetched_ids = [n.get("idCode") for n in fetched_notices if n.get("idCode")]

        if notice_count >= 0:
            self.notices_list = fetched_notices
            hidden_ids = set(state.get("hidden_notice_ids", []))
            
            cutoff_timestamp = state.get("cutoff_timestamp")
            if not cutoff_timestamp:
                import datetime
                now = datetime.datetime.now()
                today_start = datetime.datetime(now.year, now.month, now.day)
                cutoff_timestamp = int(today_start.timestamp() * 1000)
                logger.info(f"[GUI] 首次扫描建立截止时间基线: {today_start.strftime('%Y-%m-%d %H:%M:%S')}")
                save_task_state(self.tasks_list, cutoff_timestamp=cutoff_timestamp)

            unread_notice_count = sum(1 for n in fetched_notices if n.get("isread") == 0 and n.get("insertTime", 0) >= cutoff_timestamp and n.get("idCode") not in hidden_ids)
            self.lbl_notice_count.configure(text=f"({unread_notice_count})")

            if "last_notice_count" not in state:
                # 首次获取通知数：静默建立基线
                logger.info(f"[GUI] 首次获取通知数，静默建立基线: {notice_count}")
                save_task_state(self.tasks_list, notice_count=notice_count, seen_notice_ids=fetched_ids, cutoff_timestamp=cutoff_timestamp)
            else:
                last_notice_count = state["last_notice_count"]
                seen_notice_ids = set(state.get("seen_notice_ids", []))
                new_unread_notices = []
                
                for item in fetched_notices:
                    id_code = item.get("idCode")
                    if id_code and id_code not in seen_notice_ids and item.get("isread") == 0 and item.get("insertTime", 0) >= cutoff_timestamp:
                        new_unread_notices.append(item)
                        
                if new_unread_notices:
                    # 弹窗提示具体通知内容
                    for item in new_unread_notices[:3]:
                        creater = item.get("createrName") or "未知发送人"
                        title = item.get("title") or "无标题通知"
                        import time
                        insert_time = item.get("insertTime", 0)
                        time_str = "未知时间"
                        if insert_time:
                            try:
                                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(insert_time / 1000))
                            except Exception:
                                pass
                        
                        # 尝试获取通知正文
                        content_body = ""
                        notice_id = item.get("uuid") or item.get("idCode")
                        if notice_id:
                            send_tag = str(item.get("sendTag", "0"))
                            try:
                                detail_res = self.controller.client.get_notice_detail(notice_id, send_tag)
                                if detail_res.get("status") == True:
                                    msg_data = detail_res.get("msg", {})
                                    if isinstance(msg_data, dict):
                                        raw_content = msg_data.get("content", "")
                                        if raw_content:
                                            import html
                                            import re
                                            unescaped = html.unescape(raw_content)
                                            content_body = re.sub(r'\s+', ' ', unescaped).strip()
                                            if len(content_body) > 100:
                                                content_body = content_body[:97] + "..."
                            except Exception as e:
                                logger.warning(f"获取通知详情正文失败: {e}")
                        
                        msg_text = f"发件人：{creater}\n标题：{title}\n时间：{time_str}"
                        if content_body:
                            msg_text += f"\n内容：{content_body}"

                        show_notification(
                            "🔔 收件箱新通知",
                            msg_text,
                            tk_root=root
                        )
                    if len(new_unread_notices) > 3:
                        show_notification("🔔 更多新通知", f"还有 {len(new_unread_notices)-3} 个新通知未查看", tk_root=root)
                    has_new_notice = True
                    new_unread_ids = [n.get("idCode") for n in new_unread_notices if n.get("idCode")]
                    save_task_state(self.tasks_list, notice_count=notice_count, seen_notice_ids=fetched_ids, unread_notice_ids=new_unread_ids)
                elif notice_count != last_notice_count:
                    # 如果未读通知数改变但没发现新未读条目（如手机上已读），更新基线
                    save_task_state(self.tasks_list, notice_count=notice_count, seen_notice_ids=fetched_ids)

        # GUI 只显示有截止时间的任务，无截止时间的全部过滤掉
        tasks = [t for t in tasks if t.get("deadline", "") != "无截止时间"]
        self.btn_refresh.configure(state="normal", text="刷 新 🔄")

        has_previous = bool(self.tasks_list)

        if not tasks and has_previous:
            # 扫描返回空但之前有数据：保留旧数据，仅提示扫描被限流
            logger.info("[GUI] 本次扫描未获取到任务（可能课程全部在冷却中），保留上次显示结果")
            self.lbl_loading.configure(
                text="⚠️ 本次扫描被反爬限流，未获取到新数据（显示上次结果）",
                fg=THEME["deadline_warn"]
            )
            # 恢复刷新按钮状态，不更新显示并重新调度轮询
            self.schedule_poll()
            return

        self.tasks_list = tasks
        
        # Update task count badges and unread notice badge
        hw_total = sum(1 for t in tasks if t.get("type") == "作业")
        exam_total = sum(1 for t in tasks if t.get("type") == "考试")
        self.lbl_hw_count.configure(text=f"({hw_total})" if hw_total else "")
        self.lbl_exam_count.configure(text=f"({exam_total})" if exam_total else "")
        self._update_unread_badges()
        
        total = hw_total + exam_total
        if total == 0:
            self.lbl_loading.configure(text="🎉 没有未完成的作业或考试！", fg=THEME["success"])
        else:
            self.lbl_loading.configure(text=f"共扫描到 {total} 个未完成任务（作业 {hw_total} | 考试 {exam_total}）", fg=THEME["fg_dim"])
        
        self.render_current_tasks()

        # ===== 检测新任务 → 弹窗通知 =====
        is_first_run = not state.get("seen_keys")

        if is_first_run:
            # 首次运行：静默建立基线，不弹新任务通知
            # manual 时仍弹一个"完成"提示
            logger.info("[GUI] 首次运行（无 seen_keys），静默建立任务基线")
            if manual:
                show_notification(
                    "📋 首次扫描完成",
                    "已建立任务基线，下次发布新作业时会弹窗提醒你",
                    tk_root=root
                )
        else:
            new_tasks = find_new_tasks(self.tasks_list)
            if new_tasks:
                for nt in new_tasks[:3]:
                    teacher = nt.get("teacher", "") or nt.get("course", "")
                    type_label = nt.get("type", "任务")
                    show_notification(
                        f"📚 新{type_label}：{nt.get('course', '')}",
                        f"老师：{teacher}\n作业：{nt.get('name', '')}\n截止：{nt.get('deadline', '未设置')}",
                        tk_root=root
                    )
                if len(new_tasks) > 3:
                    show_notification("📚 更多新任务", f"还有 {len(new_tasks)-3} 个新任务未查看", tk_root=root)
            elif manual and not has_new_notice:
                # 手动刷新且没有新任务也无新通知：弹完成通知，避免点了刷新没反应
                show_notification(
                    "✅ 扫描完成",
                    "没有新发布的作业或考试",
                    tk_root=root
                )
            # 自动轮询且无新任务 → 不弹窗，静默更新

        # ===== 持久未读提醒：自动轮询时检查是否有未确认的通知 =====
        if not manual:
            self._check_unread_reminder(root)

        # 保存当前任务状态（用于下次启动时对比）
        save_task_state(self.tasks_list, notice_count=notice_count if notice_count >= 0 else None, seen_notice_ids=fetched_ids if notice_count >= 0 else None)
        # 重新调度后台轮询
        self.schedule_poll()

    def _clear_task_cards(self):
        """Remove all widgets from the scrollable area."""
        for widget in self.scroll_frame.scrollable_frame.winfo_children():
            widget.destroy()

    def render_current_tasks(self):
        """Render task cards filtered by the current tab."""
        self._clear_task_cards()
        parent = self.scroll_frame.scrollable_frame
        
        if self.current_tab == "通知":
            from xxt_notifier import load_task_state
            state = load_task_state()
            hidden_ids = set(state.get("hidden_notice_ids", []))
            cutoff_timestamp = state.get("cutoff_timestamp")
            if not cutoff_timestamp:
                import datetime
                now = datetime.datetime.now()
                today_start = datetime.datetime(now.year, now.month, now.day)
                cutoff_timestamp = int(today_start.timestamp() * 1000)
            visible_notices = [n for n in self.notices_list if n.get("isread") == 0 and n.get("insertTime", 0) >= cutoff_timestamp and n.get("idCode") not in hidden_ids]
            if not visible_notices:
                lbl = tk.Label(parent, text="📭 暂无通知消息", bg=THEME["bg"], fg=THEME["fg_dim"],
                               font=("Microsoft YaHei", 14), pady=60)
                lbl.pack(fill="x")
                return
            unread_ids = set(state.get("unread_notice_ids", []))
            for notice in visible_notices:
                nid = notice.get("idCode") or notice.get("uuid", "")
                self._create_notice_card(parent, notice, is_notice_unread=nid in unread_ids if nid else True)
            return

        filtered = [t for t in self.tasks_list if t.get("type") == self.current_tab]
        
        if not filtered:
            type_label = "作业" if self.current_tab == "作业" else "考试"
            emoji = "✅" if self.tasks_list else "📭"
            lbl = tk.Label(parent, text=f"{emoji} 没有未完成的{type_label}", bg=THEME["bg"], fg=THEME["fg_dim"],
                           font=("Microsoft YaHei", 14), pady=60)
            lbl.pack(fill="x")
            return
        
        # Group tasks by course
        course_groups = {}
        for task in filtered:
            course_name = task.get("course", "未知课程")
            course_groups.setdefault(course_name, []).append(task)
        
        for i, (course_name, tasks) in enumerate(course_groups.items()):
            # Course header
            course_header = tk.Frame(parent, bg=THEME["bg"], pady=2)
            top_pad = 2 if i == 0 else 12
            course_header.pack(fill="x", padx=5, pady=(top_pad, 2))
            
            tk.Label(course_header, text=f"📚 {course_name}", bg=THEME["bg"], fg=THEME["fg"],
                     font=("Microsoft YaHei", 12, "bold")).pack(side="left")
            tk.Label(course_header, text=f"{len(tasks)}项", bg=THEME["bg"], fg=THEME["fg_muted"],
                     font=("Microsoft YaHei", 10)).pack(side="right")
            
            # Separator line
            sep = tk.Frame(parent, bg=THEME["border"], height=1)
            sep.pack(fill="x", padx=5, pady=(0, 4))
            
            # Task cards
            for task in tasks:
                self._create_task_card(parent, task)

    def _create_task_card(self, parent, task):
        """Create a single styled task card widget."""
        card = tk.Frame(parent, bg=THEME["card_bg"], padx=16, pady=12, cursor="hand2")
        card.pack(fill="x", padx=5, pady=4)
        
        # Top row: task name + status badge
        top_row = tk.Frame(card, bg=THEME["card_bg"])
        top_row.pack(fill="x")
        
        task_name = task.get("name", "未命名")
        lbl_name = tk.Label(top_row, text=task_name, bg=THEME["card_bg"], fg=THEME["fg"],
                            font=("Microsoft YaHei", 11, "bold"), anchor="w")
        lbl_name.pack(side="left", fill="x", expand=True)
        
        # Status badge
        status = task.get("status", "未知")
        badge_color = THEME["badge_exam"] if status == "待做" else THEME["deadline_warn"] if status == "进行中" else THEME["badge_hw"]
        lbl_status = tk.Label(top_row, text=f" {status} ", bg=badge_color, fg="#1e1e2e",
                              font=("Microsoft YaHei", 9, "bold"), padx=6, pady=1)
        lbl_status.pack(side="right")
        
        # Bottom row: deadline + click hint
        bottom_row = tk.Frame(card, bg=THEME["card_bg"])
        bottom_row.pack(fill="x", pady=(6, 0))
        
        deadline = task.get("deadline", "无截止时间")
        deadline_color = THEME["deadline_warn"] if "剩余" in deadline and "小时" in deadline else THEME["fg_muted"]
        # Highlight urgent deadlines (less than ~48h)
        if "剩余" in deadline:
            try:
                hours_str = deadline.split("剩余")[1].split("小时")[0]
                hours = int(hours_str)
                if hours <= 48:
                    deadline_color = THEME["error"]
                elif hours <= 168:
                    deadline_color = THEME["deadline_warn"]
            except (ValueError, IndexError):
                pass
        
        lbl_deadline = tk.Label(bottom_row, text=f"⏰ {deadline}", bg=THEME["card_bg"], fg=deadline_color,
                                font=("Microsoft YaHei", 9), anchor="w")
        lbl_deadline.pack(side="left")
        
        lbl_hint = tk.Label(bottom_row, text="点击打开 →", bg=THEME["card_bg"], fg=THEME["fg_muted"],
                            font=("Microsoft YaHei", 9))
        lbl_hint.pack(side="right")
        
        # Hover effect & click handler
        url = task.get("url", "")
        all_widgets = [card, top_row, bottom_row, lbl_name, lbl_status, lbl_deadline, lbl_hint]
        
        def on_enter(e):
            for w in [card, top_row, bottom_row]:
                w.configure(bg=THEME["card_hover"])
            for w in [lbl_name, lbl_deadline, lbl_hint]:
                w.configure(bg=THEME["card_hover"])
        
        def on_leave(e):
            for w in [card, top_row, bottom_row]:
                w.configure(bg=THEME["card_bg"])
            for w in [lbl_name, lbl_deadline, lbl_hint]:
                w.configure(bg=THEME["card_bg"])
        
        def on_click(e):
            if url and url != "未知URL":
                webbrowser.open(url)
        
        for w in all_widgets:
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", on_click)
            if hasattr(w, 'configure') and 'cursor' in w.keys():
                w.configure(cursor="hand2")

    def _create_notice_card(self, parent, notice, is_notice_unread=True):
        """Create a single styled notice card widget."""
        card = tk.Frame(parent, bg=THEME["card_bg"], padx=16, pady=12, cursor="hand2")
        card.pack(fill="x", padx=5, pady=4)
        
        # Top row: title + status badge
        top_row = tk.Frame(card, bg=THEME["card_bg"])
        top_row.pack(fill="x")
        
        title = notice.get("title", "无标题通知")
        lbl_title = tk.Label(top_row, text=title, bg=THEME["card_bg"], fg=THEME["fg"],
                             font=("Microsoft YaHei", 11, "bold"), anchor="w")
        lbl_title.pack(side="left", fill="x", expand=True)
        
        # Status badge (Unread vs Read)
        is_read = notice.get("isread", 0)
        badge_text = " 未读 " if is_read == 0 else " 已读 "
        badge_color = THEME["error"] if is_read == 0 else THEME["fg_muted"]
        badge_fg = "#1e1e2e" if is_read == 0 else THEME["fg"]
        
        lbl_status = tk.Label(top_row, text=badge_text, bg=badge_color, fg=badge_fg,
                              font=("Microsoft YaHei", 9, "bold"), padx=6, pady=1)
        lbl_status.pack(side="right")
        
        # Bottom row: sender + time + click hint
        bottom_row = tk.Frame(card, bg=THEME["card_bg"])
        bottom_row.pack(fill="x", pady=(6, 0))
        
        creater = notice.get("createrName") or "未知发送人"
        import time
        insert_time = notice.get("insertTime", 0)
        time_str = "未知时间"
        if insert_time:
            try:
                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(insert_time / 1000))
            except Exception:
                pass
                
        info_text = f"👤 {creater}  |  时间: {time_str}"
        lbl_info = tk.Label(bottom_row, text=info_text, bg=THEME["card_bg"], fg=THEME["fg_muted"],
                            font=("Microsoft YaHei", 9), anchor="w")
        lbl_info.pack(side="left")
        
        lbl_hint = tk.Label(bottom_row, text="点击查看正文 →", bg=THEME["card_bg"], fg=THEME["accent"],
                            font=("Microsoft YaHei", 9))
        lbl_hint.pack(side="right")
        
        lbl_web = tk.Label(bottom_row, text="🌐 网页查看", bg=THEME["card_bg"], fg=THEME["badge_hw"],
                           font=("Microsoft YaHei", 9, "underline"))
        lbl_web.pack(side="right", padx=(0, 15))

        # Mark as read button
        notice_id = notice.get("idCode") or notice.get("uuid", "")
        is_notice_acked = not is_notice_unread
        lbl_mark_read = tk.Label(
            bottom_row,
            text="✓ 已读" if is_notice_acked else "标为已读",
            bg=THEME["card_bg"],
            fg=THEME["fg_muted"] if is_notice_acked else THEME["badge_hw"],
            font=("Microsoft YaHei", 9),
            cursor="hand2",
        )
        lbl_mark_read.pack(side="right", padx=(0, 15))
        
        lbl_delete = tk.Label(bottom_row, text="🗑️ 删除", bg=THEME["card_bg"], fg=THEME["error"],
                             font=("Microsoft YaHei", 9, "underline"))
        lbl_delete.pack(side="right", padx=(0, 15))
        
        # Hover effect & click handler
        all_widgets = [card, top_row, bottom_row, lbl_title, lbl_status, lbl_info, lbl_hint, lbl_web, lbl_delete]
        
        def on_enter(e):
            for w in [card, top_row, bottom_row]:
                w.configure(bg=THEME["card_hover"])
            for w in [lbl_title, lbl_info, lbl_hint, lbl_web, lbl_delete, lbl_mark_read]:
                w.configure(bg=THEME["card_hover"])
                
        def on_leave(e):
            for w in [card, top_row, bottom_row]:
                w.configure(bg=THEME["card_bg"])
            for w in [lbl_title, lbl_info, lbl_hint, lbl_web, lbl_delete, lbl_mark_read]:
                w.configure(bg=THEME["card_bg"])
                
        def on_click(e):
            self.show_notice_detail_dialog(notice)
            card.destroy()

        def on_notice_mark_read(e):
            if notice_id:
                mark_notice_read(notice_id)
                # Update badge on card
                lbl_status.configure(text=" 已读 ", bg=THEME["fg_muted"], fg=THEME["fg"])
                lbl_mark_read.configure(text="✓ 已读", fg=THEME["fg_muted"])
                lbl_mark_read.unbind("<Button-1>")
            return "break"
            
        for w in all_widgets:
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", on_click)
            if hasattr(w, 'configure') and 'cursor' in w.keys():
                w.configure(cursor="hand2")
                
        def open_web(e):
            notice_id = notice.get("uuid") or notice.get("idCode")
            send_tag = str(notice.get("sendTag", "0"))
            if notice_id:
                web_url = f"https://notice.chaoxing.com/pc/notice/{notice_id}/detail?sendTag={send_tag}"
                import webbrowser
                webbrowser.open(web_url)
                
                # Also mark as read locally and update UI
                if notice.get("isread") == 0:
                    notice["isread"] = 1
                    nid = notice.get("idCode") or notice.get("uuid", "")
                    if nid:
                        mark_notice_read(nid)
                    from xxt_notifier import load_task_state, save_task_state
                    state = load_task_state()
                    hidden_ids = set(state.get("hidden_notice_ids", []))
                    cutoff_timestamp = state.get("cutoff_timestamp")
                    if not cutoff_timestamp:
                        import datetime
                        now = datetime.datetime.now()
                        today_start = datetime.datetime(now.year, now.month, now.day)
                        cutoff_timestamp = int(today_start.timestamp() * 1000)
                    unread_count = sum(1 for n in self.notices_list if n.get("isread") == 0 and n.get("insertTime", 0) >= cutoff_timestamp and n.get("idCode") not in hidden_ids)
                    self.lbl_notice_count.configure(text=f"({unread_count})")
                    card.destroy()
                    
                    fetched_ids = [n.get("idCode") for n in self.notices_list if n.get("idCode")]
                    save_task_state(self.tasks_list, notice_count=len(self.notices_list), seen_notice_ids=fetched_ids)
            return "break"
            
        lbl_web.bind("<Button-1>", open_web)

        # Bind mark-read click (only if notice is still unacknowledged)
        if is_notice_unread and notice_id:
            lbl_mark_read.bind("<Button-1>", on_notice_mark_read)
        
        def open_delete(e):
            notice_id = notice.get("idCode")
            if notice_id:
                from tkinter import messagebox
                if messagebox.askyesno("确认隐藏", "确定要在本地隐藏这条通知吗？（学习通服务器端仍会保留）"):
                    from xxt_notifier import load_task_state, save_task_state
                    state = load_task_state()
                    hidden_notice_ids = state.get("hidden_notice_ids", [])
                    if notice_id not in hidden_notice_ids:
                        hidden_notice_ids.append(notice_id)
                    
                    hidden_ids = set(hidden_notice_ids)
                    cutoff_timestamp = state.get("cutoff_timestamp")
                    if not cutoff_timestamp:
                        import datetime
                        now = datetime.datetime.now()
                        today_start = datetime.datetime(now.year, now.month, now.day)
                        cutoff_timestamp = int(today_start.timestamp() * 1000)
                    unread_count = sum(1 for n in self.notices_list if n.get("isread") == 0 and n.get("insertTime", 0) >= cutoff_timestamp and n.get("idCode") not in hidden_ids)
                    self.lbl_notice_count.configure(text=f"({unread_count})")
                    card.destroy()
                    
                    fetched_ids = [n.get("idCode") for n in self.notices_list if n.get("idCode")]
                    save_task_state(self.tasks_list, notice_count=len(self.notices_list), seen_notice_ids=fetched_ids, hidden_notice_ids=hidden_notice_ids)
            return "break"
            
        lbl_delete.bind("<Button-1>", open_delete)

    def show_notice_detail_dialog(self, notice):
        """Open a custom popup dialog displaying the full body of a notice."""
        # Create dialog
        dialog = tk.Toplevel(self.controller.root)
        dialog.title("通知详情")
        dialog.configure(bg=THEME["bg"])
        
        # Center the dialog
        W, H = 520, 420
        screen_w = dialog.winfo_screenwidth()
        screen_h = dialog.winfo_screenheight()
        x = (screen_w - W) // 2
        y = (screen_h - H) // 2
        dialog.geometry(f"{W}x{H}+{x}+{y}")
        dialog.transient(self.controller.root)
        dialog.grab_set()
        
        # Frame
        main_frame = tk.Frame(dialog, bg=THEME["bg"], padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)
        
        # Title
        title = notice.get("title", "无标题通知")
        lbl_title = tk.Label(main_frame, text=title, bg=THEME["bg"], fg=THEME["fg"],
                             font=("Microsoft YaHei", 12, "bold"), wraplength=480, justify="left", anchor="w")
        lbl_title.pack(fill="x", pady=(0, 6))
        
        # Sender + Time
        creater = notice.get("createrName") or "未知发送人"
        import time
        insert_time = notice.get("insertTime", 0)
        time_str = "未知时间"
        if insert_time:
            try:
                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(insert_time / 1000))
            except Exception:
                pass
        
        lbl_info = tk.Label(main_frame, text=f"发件人：{creater}    时间：{time_str}", bg=THEME["bg"], fg=THEME["fg_muted"],
                            font=("Microsoft YaHei", 9), anchor="w")
        lbl_info.pack(fill="x", pady=(0, 10))
        
        # Separator
        sep = tk.Frame(main_frame, bg=THEME["border"], height=1)
        sep.pack(fill="x", pady=(0, 15))
        
        # Content body (with loading indication first)
        lbl_loading = tk.Label(main_frame, text="⏳ 正在加载通知正文...", bg=THEME["bg"], fg=THEME["fg_dim"],
                               font=("Microsoft YaHei", 11))
        lbl_loading.pack(fill="both", expand=True)
        
        # Fetch in thread
        def load_body():
            notice_id = notice.get("uuid") or notice.get("idCode")
            send_tag = str(notice.get("sendTag", "0"))
            content_text = "获取正文失败，请前往学习通网页或App查看。"
            if notice_id:
                try:
                    res = self.controller.client.get_notice_detail(notice_id, send_tag)
                    if res.get("status") == True:
                        msg_data = res.get("msg", {})
                        if isinstance(msg_data, dict):
                            content_text = msg_data.get("content", "").strip()
                            if not content_text:
                                content_text = "（此通知无文字内容，可能是富媒体/图片/附件，请前往网页或App查看详情）"
                except Exception as e:
                    content_text = f"加载失败: {e}"
            
            # Update UI on main thread
            import html
            final_text = html.unescape(content_text)
            
            def update_ui():
                try:
                    lbl_loading.destroy()
                    
                    txt_frame = tk.Frame(main_frame, bg=THEME["bg"])
                    txt_frame.pack(fill="both", expand=True)
                    
                    scrollbar = tk.Scrollbar(txt_frame)
                    scrollbar.pack(side="right", fill="y")
                    
                    txt_area = tk.Text(txt_frame, bg=THEME["card_bg"], fg=THEME["fg_dim"], bd=0, highlightthickness=0,
                                       font=("Microsoft YaHei", 10), wrap="word", yscrollcommand=scrollbar.set, padx=12, pady=12)
                    txt_area.insert("1.0", final_text)
                    txt_area.configure(state="disabled") # Read-only
                    txt_area.pack(side="left", fill="both", expand=True)
                    scrollbar.config(command=txt_area.yview)
                    
                    # Mark as read locally and save state so count updates
                    notice["isread"] = 1
                    from xxt_notifier import load_task_state, save_task_state, mark_notice_read
                    nid = notice.get("idCode") or notice.get("uuid", "")
                    if nid:
                        mark_notice_read(nid)
                    state = load_task_state()
                    hidden_ids = set(state.get("hidden_notice_ids", []))
                    cutoff_timestamp = state.get("cutoff_timestamp")
                    if not cutoff_timestamp:
                        import datetime
                        now = datetime.datetime.now()
                        today_start = datetime.datetime(now.year, now.month, now.day)
                        cutoff_timestamp = int(today_start.timestamp() * 1000)
                    unread_count = sum(1 for n in self.notices_list if n.get("isread") == 0 and n.get("insertTime", 0) >= cutoff_timestamp and n.get("idCode") not in hidden_ids)
                    self.lbl_notice_count.configure(text=f"({unread_count})")
                    
                    fetched_ids = [n.get("idCode") for n in self.notices_list if n.get("idCode")]
                    save_task_state(self.tasks_list, notice_count=len(self.notices_list), seen_notice_ids=fetched_ids)
                except Exception:
                    pass
                
            self.after(0, update_ui)
            
        import threading
        threading.Thread(target=load_body, daemon=True).start()

    def switch_tab(self, tab_name):
        self.current_tab = tab_name
        if tab_name == "作业":
            self.btn_tab_hw.configure(bg=THEME["accent"], fg=THEME["fg"])
            self.btn_tab_exam.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
            self.btn_tab_notice.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
        elif tab_name == "考试":
            self.btn_tab_hw.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
            self.btn_tab_exam.configure(bg=THEME["accent"], fg=THEME["fg"])
            self.btn_tab_notice.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
        else:  # "通知"
            self.btn_tab_hw.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
            self.btn_tab_exam.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
            self.btn_tab_notice.configure(bg=THEME["accent"], fg=THEME["fg"])
        self.render_current_tasks()

    def _on_autostart_toggle(self, state):
        """滑动开关回调 — 设置开机自启状态，返回是否成功"""
        ok = set_autostart(state)
        if ok:
            self._autostart_on = state
            logger.info(f"[GUI] 开机自启已{'开启' if state else '关闭'}")
        else:
            logger.error("[GUI] 设置开机自启失败")
        return ok

    def quit_app(self):
        """完全退出应用"""
        self.controller.quit_app()

    def logout(self):
        if messagebox.askyesno("提示", "确定要退出登录并清除会话吗？"):
            self.controller.config["cookies"] = []
            save_config(self.controller.config)
            self.controller.client.clear_tokens_cache()
            self.controller.show_frame(LoginFrame)


_instance_lock_socket = None

def check_single_instance() -> bool:
    """利用本地端口绑定实现单例运行检测，防止多开实例导致反爬拦截"""
    global _instance_lock_socket
    import socket
    try:
        _instance_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _instance_lock_socket.bind(('127.0.0.1', 47281))
        return True
    except socket.error:
        return False


def main():
    setup_logging()
    if not check_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("启动提示", "学习通扫描器已在后台（或系统托盘）运行中，请勿重复启动！")
        root.destroy()
        sys.exit(0)

    root = tk.Tk()
    app = XuexitongApp(root)

    # 启动后延迟 3 秒，在后台线程检查 GitHub 是否有新版本
    def _check_update_bg():
        check_for_update(silent_fail=True, tk_root=root)

    root.after(3000, lambda: threading.Thread(target=_check_update_bg, daemon=True).start())

    # --minimized: 开机自启时直接最小化到系统托盘，不显示主窗口
    if "--minimized" in sys.argv:
        root.withdraw()
        logger.info("[GUI] 以 --minimized 启动，窗口已隐藏到系统托盘")

    root.mainloop()

if __name__ == "__main__":
    main()
