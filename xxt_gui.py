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

class DashboardFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=THEME["bg"])
        self.controller = controller
        # Temp placeholder label
        lbl = tk.Label(self, text="仪表盘页面", bg=THEME["bg"], fg=THEME["fg"], font=("Microsoft YaHei", 20))
        lbl.pack(pady=50)

def main():
    root = tk.Tk()
    app = XuexitongApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
