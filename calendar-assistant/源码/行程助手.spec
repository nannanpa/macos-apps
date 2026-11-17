# -*- mode: python ; coding: utf-8 -*-
# 行程助手 v2 打包配置（PyInstaller 6.22.3）
# 目标：arm64 原生 .app（A4），GUI 可见可输入（A3），Info.plist 带齐 TCC 声明键（A5/D4）

APP_NAME = "行程助手"
BUNDLE_ID = "cn.mabin.tripassistant"

a = Analysis(
    ["calendar_assistant.py"],
    pathex=[],
    binaries=[],
    datas=[("帮助.html", ".")],
    hiddenimports=["calkit", "fuzzy_parser", "geo_helper"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序；CLI 验收输出走 --out 文件（D1 教训）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,       # 保持 universal2 bootloader（含 arm64，本机原生运行；A4 判据接受 universal2）
    codesign_identity=None, # ad-hoc；分发提示见交付函
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon="../图标/AppIcon.icns",
    bundle_identifier=BUNDLE_ID,
    info_plist={
        "CFBundleDisplayName": APP_NAME,
        "CFBundleName": APP_NAME,
        "CFBundleShortVersionString": "1.0.1",
        "CFBundleVersion": "1.0.1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        # 「关于」面板版权行：作者一行 + 官方页面一行（\n 换行，避免长 URL 挤成一行截断）
        "NSHumanReadableCopyright": "© 2026 MA Bin (nannanpa)\n官方页面 github.com/nannanpa/macos-apps/tree/main/calendar-assistant",
        # ---- TCC 声明键（D4 根治；首次运行触发系统授权弹窗，A5）----
        "NSCalendarsUsageDescription": "行程助手需要读取您的日历分类列表，并把您确认的行程写入对应日历。",
        "NSCalendarsFullAccessUsageDescription": "行程助手需要读写日历事件：写入您确认的行程，并支持按标题查找/删除（含验收测试数据清理）。",
        "NSCalendarsWriteOnlyAccessUsageDescription": "行程助手需要把您确认的行程写入日历。",
        "NSLocationWhenInUseUsageDescription": "当行程包含完全地址并勾选「按出发时间设提醒」时，行程助手需要获取当前位置以估算出发时间。",
        "NSAppleEventsUsageDescription": "行程助手可能需要与日历应用通信以刷新数据。",
    },
)
