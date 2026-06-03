# 学习通扫描器 (xxnotice)

超星学习通（泛雅平台）未完成作业/考试自动扫描与桌面通知工具。

## 功能

- **自动扫描** 所有课程中未完成的作业和考试，显示截止时间
- **两阶段防反爬策略**：串行获取 Token → 恢复等待 → 错峰并发查作业，降低被拦截概率
- **自动冷却重试**：被反爬拦截的课程 60 秒后自动重试
- **桌面通知**：发现新作业/考试时自动弹出系统通知
- **系统托盘运行**：可最小化到托盘，后台持续扫描
- **定时轮询**：默认每 15 分钟自动扫描一次
- **断网恢复**：网络断开后自动重连，恢复后继续扫描

## 截图

![GUI 主界面](screenshot.png)

## 安装

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

复制 `config.json`（从仓库下载示例配置），填写您的学习通账号和密码：

```json
{
  "phone": "手机号",
  "password": "密码",
  "poll_interval": 900,
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
| `password` | 学习通登录密码 | — |
| `poll_interval` | 自动扫描间隔（秒） | 900（15分钟） |
| `only_courses` | 限定扫描的课程列表（空=全部） | `[]` |
| `max_workers` | 阶段2并发查作业的最大线程数 | 8 |
| `rate_limit_delay` | 阶段2每个课程的启动间隔（秒） | 1.0 |
| `token_cooldown` | Token被限流后的冷却时间（秒） | 60 |
| `hide_no_deadline` | 是否隐藏无截止时间的任务 | true |

> 首次运行会自动登录并保存 Cookie，后续启动无需重新登录。

### 5. 启动

双击 `start.bat`，或运行：

```bash
.venv\Scripts\python -m xxt_gui
```

## 使用说明

### 主界面

- **刷新按钮**：手动触发一次完整扫描
- **任务卡片**：显示每门课的未完成作业/考试，含课程名、作业名、截止时间、老师
- **底部统计**：显示未完成作业和考试总数
- **双击卡片**：在浏览器中打开该作业入口

### 系统托盘

- 关闭窗口会自动最小化到托盘
- 右键托盘图标可退出程序
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
└── .gitignore
```

## 反爬策略说明

学习通（超星）对 Token 获取和作业列表查询都有频率限制，超出阈值会返回空数据。

本工具采用两阶段扫描策略规避限制：

1. **阶段 1（串行获取 Token）**：逐门课程依次请求 Token，内部 0.3 秒间隔。一门课程被限流后等 60 秒重试。
2. **恢复期（30 秒）**：等待服务器 session 配额恢复。
3. **阶段 2（错峰并发）**：每门课程间隔 1 秒启动，最多 8 个线程同时查询作业列表。

## 许可证

MIT
