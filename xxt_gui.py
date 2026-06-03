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
        # Temp placeholder label
        lbl = tk.Label(self, text="登录页面", bg=THEME["bg"], fg=THEME["fg"], font=("Microsoft YaHei", 20))
        lbl.pack(pady=50)

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
