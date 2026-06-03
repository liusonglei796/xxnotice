import tkinter as tk
from tkinter import messagebox
from xxt_notifier import (
    XuexitongClient, load_config, save_config, setup_logging, logger,
    is_autostart_enabled, set_autostart,
    find_new_tasks, save_task_state, show_notification,
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
        self._state = not self._state
        self._draw()
        if self._command:
            self._command(self._state)

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
        self.root.title("学习通作业/考试扫描器")
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
        self.current_tab = "作业"  # '作业' or '考试'
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
                                    font=("Microsoft YaHei", 11, "bold"), bd=0, relief="flat", width=18,
                                    command=lambda: self.switch_tab("作业"))
        self.btn_tab_hw.pack(side="left", padx=(0, 10), ipady=6)
        
        self.btn_tab_exam = tk.Button(nav_bar, text="📋 未完成考试", bg=THEME["card_bg"], fg=THEME["fg_dim"],
                                     font=("Microsoft YaHei", 11, "bold"), bd=0, relief="flat", width=18,
                                     command=lambda: self.switch_tab("考试"))
        self.btn_tab_exam.pack(side="left", ipady=6)
        
        # Task count badges
        self.lbl_hw_count = tk.Label(nav_bar, text="", bg=THEME["bg"], fg=THEME["badge_hw"], font=("Microsoft YaHei", 10, "bold"))
        self.lbl_hw_count.pack(side="left", padx=(0, 0))
        
        self.lbl_exam_count = tk.Label(nav_bar, text="", bg=THEME["bg"], fg=THEME["badge_exam"], font=("Microsoft YaHei", 10, "bold"))
        self.lbl_exam_count.pack(side="left", padx=(5, 0))

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
            # Schedule UI update on the main thread
            # 注意：不再在扫面前清空卡片，而是扫完后有数据才替换，
            # 避免扫描过程中出现白屏，也避免扫描返回空时清掉上次结果
            self.after(0, lambda: self._on_fetch_complete(tasks, manual=manual))
        
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def _on_fetch_complete(self, tasks, manual=False):
        """Called on the main thread after background fetch completes.
        参数:
            tasks: 扫描到的任务列表
            manual: True=手动刷新，完成后弹通知；False=自动轮询，有新任务才弹。
        """
        self._is_fetching = False
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
            # 恢复刷新按钮状态，不更新显示
            return

        self.tasks_list = tasks
        
        hw_count = sum(1 for t in tasks if t.get("type") == "作业")
        exam_count = sum(1 for t in tasks if t.get("type") == "考试")
        
        self.lbl_hw_count.configure(text=f"({hw_count})")
        self.lbl_exam_count.configure(text=f"({exam_count})")
        
        total = hw_count + exam_count
        if total == 0:
            self.lbl_loading.configure(text="🎉 没有未完成的作业或考试！", fg=THEME["success"])
        else:
            self.lbl_loading.configure(text=f"共扫描到 {total} 个未完成任务（作业 {hw_count} | 考试 {exam_count}）", fg=THEME["fg_dim"])
        
        self.render_current_tasks()

        # ===== 检测新任务 → 弹窗通知 =====
        from xxt_notifier import load_task_state
        state = load_task_state()
        is_first_run = not state.get("seen_keys")
        root = self.controller.root

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
            elif manual:
                # 手动刷新且没有新任务：弹完成通知，避免点了刷新没反应
                show_notification(
                    "✅ 扫描完成",
                    "没有新发布的作业或考试",
                    tk_root=root
                )
            # 自动轮询且无新任务 → 不弹窗，静默更新
        # 保存当前任务状态（用于下次启动时对比）
        save_task_state(self.tasks_list)
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

    def switch_tab(self, tab_name):
        self.current_tab = tab_name
        if tab_name == "作业":
            self.btn_tab_hw.configure(bg=THEME["accent"], fg=THEME["fg"])
            self.btn_tab_exam.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
        else:
            self.btn_tab_hw.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
            self.btn_tab_exam.configure(bg=THEME["accent"], fg=THEME["fg"])
        self.render_current_tasks()

    def _on_autostart_toggle(self, state):
        """滑动开关回调 — 设置开机自启状态"""
        ok = set_autostart(state)
        if ok:
            self._autostart_on = state
            logger.info(f"[GUI] 开机自启已{'开启' if state else '关闭'}")
        else:
            self.switch_autostart.set(not state)  # 回滚开关状态
            logger.error("[GUI] 设置开机自启失败")

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
    root.mainloop()

if __name__ == "__main__":
    main()
