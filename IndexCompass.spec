# -*- mode: python ; coding: utf-8 -*-
"""发布版打包配置: 入口 etf_pub.py, 产物 IndexCompass。

Windows: 输出 IndexCompass/ 目录(内含 exe)
macOS:   先出同样的目录, 再包成 IndexCompass.app

两边共用一份分析配置, 只在图标格式和最终封装上分叉, 避免两份 spec 各自漂移。
"""
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

IS_MAC = sys.platform == 'darwin'

# 版本号与 brand.py 保持单一来源, 免得 app 上显示的版本和 exe 对不上
_src = (Path(SPECPATH) / 'brand.py').read_text(encoding='utf-8')
_m = re.search(r'VERSION\s*=\s*[\'"](v?[0-9][0-9.]*)[\'"]', _src)
SHORT_VER = _m.group(1).lstrip('v') if _m else '0.0.0'

# 数据接口库有一部分子模块要到运行时才 import, 静态分析扫不到;
# 另有一些接口要读包内的数据文件(如日历), 只收 .py 会漏。
data_mods = collect_submodules('akshare')
data_files = collect_data_files('akshare')

# 两个配色的图标都带上: 窗口图标在 macOS 上用 png, 在 Windows 上用 ico
datas = [('etf_list.json', '.')]
for _nm in ('icon_a.ico', 'icon_a.png', 'icon_self.ico', 'icon_self.png'):
    _p = Path(SPECPATH) / 'icons' / _nm
    if _p.exists():
        datas.append((str(_p), 'icons'))

ICON = str(Path(SPECPATH) / 'icons' / ('icon_a.icns' if IS_MAC else 'icon_a.ico'))

a = Analysis(
    ['etf_pub.py'],
    pathex=[],
    binaries=[],
    datas=datas + data_files,
    hiddenimports=(['brand', 'app_env', 'etf_flow', 'etf_hold', 'etf_inst', 'etf_meta',
                    'etf_analytics'] + data_mods),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='IndexCompass',
    icon=ICON,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 会破坏 Mach-O 的签名结构, macOS 上必须关掉
    upx=(not IS_MAC),
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=(not IS_MAC),
    upx_exclude=[],
    name='IndexCompass',
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name='IndexCompass.app',
        icon=ICON,
        bundle_identifier='com.syakukou.indexcompass',
        info_plist={
            'CFBundleName': 'IndexCompass',
            'CFBundleDisplayName': '指数罗盘',
            'CFBundleShortVersionString': SHORT_VER,
            'CFBundleVersion': SHORT_VER,
            'CFBundleDevelopmentRegion': 'zh_CN',
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,
        },
    )
