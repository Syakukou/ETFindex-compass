# -*- mode: python ; coding: utf-8 -*-
"""发布版打包配置: 入口 etf_pub.py, 产物 IndexCompass。"""

# 数据接口库有一部分子模块要到运行时才 import, 静态分析扫不到;
# 另有一些接口要读包内的数据文件(如日历), 只收 .py 会漏。
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

data_mods = collect_submodules('akshare')
data_files = collect_data_files('akshare')

a = Analysis(
    ['etf_pub.py'],
    pathex=[],
    binaries=[],
    datas=[('etf_list.json', '.')] + data_files,
    hiddenimports=(['brand', 'etf_flow', 'etf_hold', 'etf_inst', 'etf_meta', 'etf_analytics']
                   + data_mods),
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
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='IndexCompass',
)
