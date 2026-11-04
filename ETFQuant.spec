# -*- mode: python ; coding: utf-8 -*-

# akshare 里不少函数是在运行时才 import 自己的子模块, 静态分析扫不到。
# 之前只写了 'akshare', 打包后 futures_main_sina 就抛 ModuleNotFoundError,
# 表现为"指数进出页空白"。这里把子模块全收进来。
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ak_mods = collect_submodules('akshare')
# 光收模块还不够: akshare 有些接口要读包内的数据文件(如 data/calendar.json),
# 打包后路径在 _internal/akshare/data 下, 缺了就抛 FileNotFoundError
ak_data = collect_data_files('akshare')

a = Analysis(
    ['etf_pro.py'],
    pathex=[],
    binaries=[],
    datas=[('etf_list.json', '.')] + ak_data,
    hiddenimports=(['etf_flow', 'etf_hold', 'etf_inst', 'etf_meta', 'etf_analytics']
                   + ak_mods),
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
    name='ETFQuant',
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
    name='ETFQuant',
)
