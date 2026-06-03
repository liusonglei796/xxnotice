import tkinter as tk
from tkinter import messagebox
from xxt_notifier import XuexitongClient, load_config, save_config
import sys

THEME = {
    "bg": "#181825",
    "card_bg": "#212130",
    "input_bg": "#313145",
    "fg": "#ffffff",
    "fg_dim": "#bac2de",
    "accent": "#5966f3",
    "accent_hover": "#4852c9",
    "badge_hw": "#89b4fa",
    "badge_exam": "#f38ba8",
    "success": "#a6e3a1",
    "error": "#f38ba8"
}

class XuexitongApp:
    def __init__(self, root):
        self.root = root
        self.root.title("学习通作业/考试扫描器")
        self.root.geometry("850x650")
        self.root.configure(bg=THEME["bg"])
        
        self.client = XuexitongClient()
        self.config = load_config()
        
        # Main container
        self.container = tk.Frame(self.root, bg=THEME["bg"])
        self.container.pack(side="top", fill="both", expand=True)
        
        self.current_frame = None
        self.try_auto_login()

    def show_frame(self, frame_class):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = frame_class(self.container, self)
        self.current_frame.pack(fill="both", expand=True)

    def try_auto_login(self):
        """Try logging in using existing cookies from config"""
        if self.config.get("cookies"):
            # Load cookies into client
            if self.client.load_cookies(self.config["cookies"]):
                # Verify session works by fetching course list (non-blocking verification)
                try:
                    courses = self.client.get_course_list()
                    if courses:
                        print("[GUI] Auto-login successful using cookies")
                        self.show_frame(DashboardFrame)
                        return
                except Exception as e:
                    print(f"[GUI] Auto-login check failed: {e}")
                    
        print("[GUI] Session expired or invalid. Show login frame.")
        self.show_frame(LoginFrame)

class LoginFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=THEME["bg"])
        self.controller = controller
        
        # Center panel card
        card = tk.Frame(self, bg=THEME["card_bg"], padx=40, pady=40)
        card.place(relx=0.5, rely=0.5, anchor="center")
        
        # Title
        title = tk.Label(card, text="学习通账户登录", bg=THEME["card_bg"], fg=THEME["fg"], font=("Microsoft YaHei", 20, "bold"))
        title.pack(pady=(0, 30))
        
        # Phone
        lbl_phone = tk.Label(card, text="手机号", bg=THEME["card_bg"], fg=THEME["fg_dim"], font=("Microsoft YaHei", 10))
        lbl_phone.pack(anchor="w", pady=(0, 5))
        self.ent_phone = tk.Entry(card, bg=THEME["input_bg"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                  font=("Microsoft YaHei", 12), bd=0, relief="flat", width=30)
        self.ent_phone.pack(pady=(0, 20), ipady=8)
        
        # Autofill phone from config
        saved_phone = self.controller.config.get("phone", "")
        if saved_phone:
            self.ent_phone.insert(0, saved_phone)
            
        # Password
        lbl_pwd = tk.Label(card, text="密码", bg=THEME["card_bg"], fg=THEME["fg_dim"], font=("Microsoft YaHei", 10))
        lbl_pwd.pack(anchor="w", pady=(0, 5))
        self.ent_pwd = tk.Entry(card, bg=THEME["input_bg"], fg=THEME["fg"], insertbackground=THEME["fg"],
                                font=("Microsoft YaHei", 12), show="*", bd=0, relief="flat", width=30)
        self.ent_pwd.pack(pady=(0, 10), ipady=8)
        
        saved_pwd = self.controller.config.get("password", "")
        if saved_pwd:
            self.ent_pwd.insert(0, saved_pwd)

        # Status text
        self.lbl_status = tk.Label(card, text="", bg=THEME["card_bg"], fg=THEME["error"], font=("Microsoft YaHei", 10))
        self.lbl_status.pack(pady=(0, 20))
        
        # Login Button
        self.btn_login = tk.Button(card, text="登  录", bg=THEME["accent"], fg=THEME["fg"], 
                                   activebackground=THEME["accent_hover"], activeforeground=THEME["fg"],
                                   font=("Microsoft YaHei", 12, "bold"), bd=0, relief="flat", width=28,
                                   command=self.perform_login)
        self.btn_login.pack(ipady=8)
        
        # Hover effect on button
        self.btn_login.bind("<Enter>", lambda e: self.btn_login.configure(bg=THEME["accent_hover"]))
        self.btn_login.bind("<Leave>", lambda e: self.btn_login.configure(bg=THEME["accent"]))

    def perform_login(self):
        phone = self.ent_phone.get().strip()
        pwd = self.ent_pwd.get().strip()
        
        if not phone or not pwd:
            self.lbl_status.configure(text="手机号或密码不能为空", fg=THEME["error"])
            return
            
        self.lbl_status.configure(text="正在登录...", fg=THEME["fg_dim"])
        self.btn_login.configure(state="disabled")
        self.update_idletasks()
        
        try:
            success = self.controller.client.login(phone, pwd)
            if success:
                # Save credentials and new cookies
                self.controller.config["phone"] = phone
                self.controller.config["password"] = pwd
                self.controller.config["cookies"] = self.controller.client.get_cookies()
                save_config(self.controller.config)
                
                self.lbl_status.configure(text="登录成功！正在跳转...", fg=THEME["success"])
                self.update_idletasks()
                self.after(500, lambda: self.controller.show_frame(DashboardFrame))
            else:
                self.lbl_status.configure(text="登录失败，请检查账号密码", fg=THEME["error"])
                self.btn_login.configure(state="normal")
        except Exception as e:
            self.lbl_status.configure(text=f"异常: {e}", fg=THEME["error"])
            self.btn_login.configure(state="normal")

import webbrowser

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
        
        # Mousewheel binding
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        # Platform-independent scroll
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

class DashboardFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=THEME["bg"])
        self.controller = controller
        
        self.tasks_list = []  # List of all uncompleted tasks
        self.current_tab = "作业"  # '作业' or '考试'
        
        # 1. Header Frame
        header = tk.Frame(self, bg=THEME["card_bg"], height=70, padx=20)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        
        # Profile / Account Status
        user_info = f"账号: {self.controller.config.get('phone', '未知')}"
        lbl_user = tk.Label(header, text=user_info, bg=THEME["card_bg"], fg=THEME["fg"], font=("Microsoft YaHei", 12, "bold"))
        lbl_user.pack(side="left")
        
        # Refresh Button
        self.btn_refresh = tk.Button(header, text="刷 新 🔄", bg=THEME["accent"], fg=THEME["fg"],
                                     activebackground=THEME["accent_hover"], activeforeground=THEME["fg"],
                                     font=("Microsoft YaHei", 10, "bold"), bd=0, relief="flat", padx=15, pady=5,
                                     command=self.fetch_and_render)
        self.btn_refresh.pack(side="right", padx=(10, 0))
        
        # Logout Button
        self.btn_logout = tk.Button(header, text="退出登录 🚪", bg="#e05f65", fg=THEME["fg"],
                                    activebackground="#bd4f54", activeforeground=THEME["fg"],
                                    font=("Microsoft YaHei", 10, "bold"), bd=0, relief="flat", padx=15, pady=5,
                                     command=self.logout)
        self.btn_logout.pack(side="right")
        
        # 2. Navigation / Tabs Bar
        nav_bar = tk.Frame(self, bg=THEME["bg"], pady=15, padx=20)
        nav_bar.pack(side="top", fill="x")
        
        self.btn_tab_hw = tk.Button(nav_bar, text="正在进行中的作业", bg=THEME["accent"], fg=THEME["fg"],
                                    font=("Microsoft YaHei", 11, "bold"), bd=0, relief="flat", width=22,
                                    command=lambda: self.switch_tab("作业"))
        self.btn_tab_hw.pack(side="left", padx=(0, 10), ipady=6)
        
        self.btn_tab_exam = tk.Button(nav_bar, text="未完成的考试", bg=THEME["card_bg"], fg=THEME["fg_dim"],
                                     font=("Microsoft YaHei", 11, "bold"), bd=0, relief="flat", width=22,
                                     command=lambda: self.switch_tab("考试"))
        self.btn_tab_exam.pack(side="left", ipady=6)

        # Loading Indicator Label
        self.lbl_loading = tk.Label(self, text="", bg=THEME["bg"], fg=THEME["fg_dim"], font=("Microsoft YaHei", 11))
        self.lbl_loading.pack(side="top", fill="x", pady=5)
        
        # 3. Main Tasks View Area
        self.scroll_frame = ScrollableFrame(self)
        self.scroll_frame.pack(side="top", fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Mock fetch_and_render and render_current_tasks methods to allow running Task 3 before Task 4
        # (These will be fully implemented in Task 4)
        # self.fetch_and_render()

    def fetch_and_render(self):
        pass

    def render_current_tasks(self):
        pass

    def switch_tab(self, tab_name):
        self.current_tab = tab_name
        if tab_name == "作业":
            self.btn_tab_hw.configure(bg=THEME["accent"], fg=THEME["fg"])
            self.btn_tab_exam.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
        else:
            self.btn_tab_hw.configure(bg=THEME["card_bg"], fg=THEME["fg_dim"])
            self.btn_tab_exam.configure(bg=THEME["accent"], fg=THEME["fg"])
        self.render_current_tasks()

    def logout(self):
        if messagebox.askyesno("提示", "确定要退出登录并清除会话吗？"):
            self.controller.config["cookies"] = []
            save_config(self.controller.config)
            self.controller.client.clear_tokens_cache()
            self.controller.show_frame(LoginFrame)

def main():
    root = tk.Tk()
    app = XuexitongApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
