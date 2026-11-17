# 行程助手 TripAssistant

粘贴一段中文会议通知，自动解析出时间、地点、会议名，确认后一键写入 macOS 日历。

## 下载安装

1. 打开 [Latest Release](https://github.com/nannanpa/macos-apps/releases/latest)，下载 `TripAssistant-x.x.x-arm64.dmg`
2. 打开 DMG，把 **行程助手** 拖入 Applications 文件夹
3. 首次打开：安装包未做公证（ad-hoc 签名），若系统提示"无法验证开发者"，请 **右键点击应用 → 打开 → 打开**，之后不再提示
4. 首次写入日历时会弹出日历权限申请，点 **允许**

## 功能

- **一句话解析**：`明天上午10点开会`、`从2026年4月7日18:30到8日18:30值班`、`明天（11月17日）上午10:00，在勤德楼A625，召开XX领导小组会……` 这类通知原文直接粘贴
- **会议名提炼**：长通知自动提取会议名作标题（"召开学校宣传思想和意识形态工作领导小组会"→ 标题就是它），寒暄套话、背景交代、材料发放等后勤说明自动剔除
- **议题进备注**：主持、议题等实质信息自动归入备注（"哈校长主持会议，研究审议XX方案"→"哈校长主持，研究审议XX方案"）
- **括注日期优先**：`明天（11月17日）`以括号里的日期为准，不受"今天哪天"影响
- **干扰时间识别**：截止时间（"10:00前提交"）、被修正的旧时间（"原定10点，推迟到3点"）、被否定的时间（"不是后天"）不会污染起止时间
- **多事件**：分号/句号分隔的多个行程一次解析成多条
- **发车提醒**：行程带完全地址时，可按当前位置估算出发时间并提前提醒
- **界面**：浅色卡片风格；解析结果逐项可改；所有输入框支持右键粘贴

## 系统要求

- Apple Silicon（arm64）Mac，macOS 12+

## 从源码构建

```bash
python3 -m venv .venv
.venv/bin/pip install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-EventKit \
    pyobjc-framework-CoreLocation pyobjc-framework-MapKit PyInstaller
.venv/bin/python -m PyInstaller --noconfirm 行程助手.spec
```

产物在 `dist/行程助手.app`（arm64，ad-hoc 签名）。

## 已知限制

- 安装包为 ad-hoc 自签名、未公证：从网络下载后首次打开可能被 Gatekeeper 拦截，按上面第 3 步右键打开即可；若希望直接双击可用，需要 Apple Developer ID 签名并公证
- 日历分类读取需要日历权限；未授权时可在界面手动填写日历名

## 作者与许可

MA Bin (nannanpa) · [MIT License](../LICENSE)
