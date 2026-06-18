# 学习通扫描器 (xxnotice)

[![GitHub Release](https://img.shields.io/github/v/release/liusonglei796/xxnotice?style=flat-square)](https://github.com/liusonglei796/xxnotice/releases)
[![Downloads](https://img.shields.io/github/downloads/liusonglei796/xxnotice/total?style=flat-square)](https://github.com/liusonglei796/xxnotice/releases)

超星学习通（泛雅平台）未完成作业/考试自动扫描与桌面通知工具。

## 功能

- **自动扫描** 所有课程中未完成的作业和考试，显示截止时间
- **两阶段防反爬策略**：串行获取 Token → 恢复等待 → 错峰并发查作业，降低被拦截概率
- **多轮重试扫描**：首次扫描最多重试 5 轮，确保被限流的课程在冷却后仍能被扫描到
- **自动冷却重试**：被反爬拦截的课程 60 秒后自动重试
- **桌面通知**：发现新作业/考试时自动弹出系统通知
- **系统托盘运行**：可最小化到托盘，后台持续扫描
- **定时轮询**：默认每 5 分钟自动扫描一次（可配置）
- **断网恢复**：网络断开后自动重连，恢复后继续扫描
- **扫码安全登录**：支持学习通 App 扫码登录，无需输入密码
- **Cookie 自动无感刷新**：Cookie 过期后，若配置了账号密码，系统将在后台自动重新登录并刷新 Cookie，保证长期无人值守运行
- **开机自启动**：内置开关控制是否开机自动运行

## 下载

### 编译版（推荐，无需 Python 环境）

从 [GitHub Releases](https://github.com/liusonglei796/xxnotice/releases) 下载最新版 `xxnotice-scanner.exe`，直接双击运行即可。

**首次使用流程**：
1. 下载 `xxnotice-scanner.exe` 并双击运行
2. 使用**学习通 App 扫码登录**（推荐）或输入**手机号 + 密码**登录
3. 登录成功后自动开始扫描，有新作业时会弹出系统通知

> 编译版已包含所有依赖和 Python 运行时，无需额外安装任何东西。

## 从源码运行

### 1. 安装 Python 3.9+

确保已安装 Python 3.9 或更高版本。

### 2. 克隆项目

```bash
git clone https://github.com/liusonglei796/xxnotice.git
cd xxnotice
```

### 3. 创建虚拟环境 & 安装依赖

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 4. 配置账号

首次启动会弹出登录界面，支持**账号密码登录**或**学习通 App 扫码登录**。登录成功后会自动保存 Cookie，后续启动无需重新登录。

也可手动编辑 `config.json`（首次运行自动生成），常用配置项如下：

```json
{
  "phone": "手机号",
  "password": "密码",
  "poll_interval": 300,
  "only_courses": [],
  "max_workers": 8,
  "rate_limit_delay": 1.0,
  "token_cooldown": 60,
  "hide_no_deadline": true
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `phone` | 学习通登录手机号 | — |
| `password` | 学习通登录密码（可选，配置后可在 Cookie 过期时自动刷新） | — |
| `poll_interval` | 自动扫描间隔（秒） | 300（5分钟） |
| `only_courses` | 限定扫描的课程列表（空=全部） | `[]` |
| `max_workers` | 阶段2并发查作业的最大线程数 | 8 |
| `rate_limit_delay` | 阶段2每个课程的启动间隔（秒） | 1.0 |
| `token_cooldown` | Token被限流后的冷却时间（秒） | 60 |
| `hide_no_deadline` | 是否隐藏无截止时间的任务 | true |

### 5. 登录方式

支持两种登录方式：

- **账号密码登录**：输入学习通手机号和密码（密码加密传输）
- **扫码安全登录**：使用学习通 App 扫描二维码，无需输入密码

登录成功后自动保存 Cookie，后续启动无需重新登录。

### 6. 启动

双击 `start.bat`，或运行：

```bash
.venv\Scripts\python -m xxt_gui
```

## 使用说明

### 主界面

- **刷新按钮**：手动触发一次完整扫描（同时清除所有课程的冷却标记，强制重新请求被拦课程）
- **任务卡片**：显示每门课的未完成作业/考试，含课程名、作业名、截止时间、老师
- **底部统计**：显示未完成作业和考试总数
- **双击卡片**：在浏览器中打开该作业入口
- **作业/考试标签**：切换查看未完成作业或未完成考试
- **开机自启动**：滑动开关控制是否开机自动运行

### 系统托盘

- 关闭窗口会弹窗询问：最小化到托盘或完全退出
- 右键托盘图标可恢复窗口或完全退出
- 后台扫描到新任务时会弹出系统通知

## 项目结构

```
xxnotice/
├── xxt_gui.py          # GUI 主程序（Tkinter）
├── xxt_notifier.py     # 核心扫描引擎（学习通接口、反爬策略）
├── config.json         # 配置文件（已加入 .gitignore，不提交）
├── requirements.txt    # Python 依赖
├── start.bat           # Windows 一键启动脚本
├── test_antispider.py  # 反爬策略测试工具
├── build_exe.py        # PyInstaller 编译脚本
├── dist/               # 编译输出目录（含 .exe）
└── .gitignore
```

## 反爬策略说明

学习通（超星）对 Token 获取和作业列表查询都有频率限制，超出阈值会触发反爬虫验证（URL 含 `antispiderShowVerify.ac`），导致返回空数据。

本工具采用多轮两阶段扫描策略规避限制：

### 单轮扫描流程

1. **阶段 1（串行获取 Token）**：逐门课程依次请求 Token，延迟 0.3 秒/次。首次扫描无缓存时额外延迟 3 秒/次。
2. **阶段间恢复期（15 秒）**：等待服务器 session 请求配额恢复，避免阶段 2 的并发请求立即触发反爬。
3. **阶段 2（错峰并发）**：每门课程间隔 1 秒启动，最多 8 个线程同时查询作业/考试列表。

### 多轮重试机制

首次扫描时，如果某些课程在阶段 1 或阶段 2 被反爬拦截：
- 拦截的课程放入下一轮重试，等待冷却期（60 秒）结束后再次扫描
- 最多重试 5 轮，确保所有课程都能被扫描到

### 手动刷新

点击 GUI 的「刷新」按钮会清除所有课程的冷却标记，强制重新请求所有课程（包括之前被反爬拦截的）。

## 许可证

MIT
