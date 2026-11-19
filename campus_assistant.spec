# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / 'streamlit_app.py'), '.'),
         (str(root / 'settings' / 'parsing.json'), 'settings'),
         (str(root / 'scripts' / 'show_notification.ps1'), 'scripts'),
         (str(root / 'assets' / 'app.ico'), 'assets')]
for package in ('streamlit', 'webview', 'pythonnet', 'clr_loader'):
    datas += collect_data_files(package)
for package in ('streamlit', 'pywebview', 'pythonnet'):
    datas += copy_metadata(package)
hidden = collect_submodules('course_assistant') + collect_submodules('streamlit')
hidden += ['webview.platforms.winforms', 'webview.platforms.edgechromium', 'clr_loader', 'clr']

a = Analysis([str(root / 'desktop_app.pyw')], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'torch', 'tensorflow', 'matplotlib', 'scipy', 'IPython'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='校园课程管家',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'app.ico'), uac_admin=False)
