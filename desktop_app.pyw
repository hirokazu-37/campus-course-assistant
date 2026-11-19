"""桌面程序入口；打包后可直接双击exe。"""
import multiprocessing
from course_assistant.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
