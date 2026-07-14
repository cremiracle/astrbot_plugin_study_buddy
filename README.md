# 学习陪伴助手 (Study Buddy)

AstrBot 插件，提供番茄钟与正计时功能，帮助用户专注学习，并支持在 WebUI 查看学习统计数据。

## 功能

- **番茄钟 (pomodoro)**：支持多轮循环的倒计时模式，可设置学习时长、休息时长和循环次数。
- **正计时 (stopwatch)**：自由计时模式，每经过 1 小时 AI 主动提醒用户休息。
- **学习统计 WebUI**：在 AstrBot Dashboard 中查看总学习时间、近七天学习时间和今日学习时间，以及近七天趋势柱状图。
- **LLM 工具**：AI 可以通过自然语言为用户创建、停止番茄钟，查询学习状态。
- **指令支持**：同时提供 `/study` 指令集用于手动控制。

## 使用方法

### 通过 AI 自然语言触发

直接告诉 AI：
- "帮我开一个 4 轮番茄钟，每轮学习 25 分钟，休息 5 分钟"
- "开始正计时"
- "停止计时"
- "我今天学了多久？"

### 通过指令触发

- `/study start stopwatch` — 开始正计时
- `/study start pomodoro [学习时长] [休息时长] [循环次数]` — 开始番茄钟（默认25分钟学习、5分钟休息、1次循环）
- `/study stop` — 停止计时
- `/study stats` — 查看统计

### 示例

```
/study start pomodoro 25 5 4    # 4轮番茄钟，每轮25分钟学习，5分钟休息
/study start pomodoro 45 10 2   # 2轮番茄钟，每轮45分钟学习，10分钟休息
/study start pomodoro            # 1轮番茄钟，默认25分钟学习，5分钟休息
```

### WebUI

在 AstrBot Dashboard 的插件页面中，点击"学习统计"即可查看可视化学习数据。

## 数据存储

学习记录存储在插件数据目录下的 SQLite 数据库中，路径：
`data/plugin_data/astrbot_plugin_study_buddy/study_records.db`
