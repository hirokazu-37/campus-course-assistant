"""在Windows打包可双击的单文件exe；仅收集程序资源，不收集台账/日志/密钥。"""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def create_icon():
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 248, 248), radius=54, fill='#16786F')
    draw.rounded_rectangle((51, 47, 205, 207), radius=16, fill='white')
    draw.rectangle((51, 62, 75, 190), fill='#CDE8DD')
    draw.line((92, 86, 169, 86), fill='#16786F', width=10)
    draw.line((92, 113, 151, 113), fill='#16786F', width=10)
    draw.line((111, 160, 131, 180, 176, 139), fill='#16786F', width=13, joint='curve')
    folder = ROOT / 'assets'; folder.mkdir(exist_ok=True)
    image.save(folder / 'app.ico', sizes=[(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)])


def main():
    parser = argparse.ArgumentParser(description='构建校园课程管家')
    parser.add_argument('--stage-only', action='store_true', help='只生成dist产物，暂不替换正在使用的程序')
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise SystemExit('请在Windows上构建Windows应用')
    create_icon()
    build_log = ROOT / 'build' / 'desktop-build.log'; build_log.parent.mkdir(exist_ok=True)
    with build_log.open('w', encoding='utf-8') as output:
        result = subprocess.run([sys.executable, '-m', 'PyInstaller', str(ROOT / 'campus_assistant.spec'),
                                 '--noconfirm', '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build' / 'desktop')],
                                cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        print('打包失败，日志：' + str(build_log))
        return result.returncode
    source = ROOT / 'dist' / '校园课程管家.exe'
    destination = ROOT / source.name
    metadata = {'artifact':source.name, 'size_bytes':source.stat().st_size,
                'version':tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']['version'],
                'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                'personal_data_bundled':False,
                'resources':['streamlit_app.py','settings/parsing.json','scripts/show_notification.ps1','assets/app.ico'],
                'data_location':'应用程序旁的data文件夹；本项目根目录exe复用现有data'}
    (ROOT / 'dist' / 'build_manifest.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
    if not args.stage_only:
        shutil.copy2(source, destination)
    print('已生成：' + str(source if args.stage_only else destination))
    print('文件大小：' + str(round(source.stat().st_size / 1024**2, 1)) + ' MB')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
