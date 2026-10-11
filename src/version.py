# -*- coding: utf-8 -*-
"""版本与联系方式：界面、托盘、打包脚本共用此处的唯一定义。

系统侧条目（Windows 服务 / 计划任务 / 启动快捷方式 / 单实例互斥体）统一带
QVTUAutoLogin 标识，方便在服务管理器、任务计划程序等位置查找与清理；
程序本体仍叫 AutoLogin。

GITEE_URL 为仓库主页，供用户查看说明 / 手动下载新版 exe（程序本身不做更新检查）。
"""

APP_NAME = "AutoLogin"                 # 程序名（exe / 文件属性）
APP_TITLE = "AutoLogin 校园网自动登录"   # 界面与文件属性里展示的完整标题
APP_VERSION = "26.10.6"
CONTACT_WECHAT = "Reeve_1110"
GITEE_URL = "https://gitee.com/reeve1110/qvtu-auto-login"

SYSTEM_TAG = "QVTUAutoLogin"                        # 系统条目统一命名前缀
MUTEX_NAME = "Local\\QVTUAutoLogin_SingleInstance"  # 单实例互斥体（每登录会话一个）
SERVICE_NAME = SYSTEM_TAG + "Service"               # Windows 服务名：QVTUAutoLoginService
SERVICE_DISPLAY = "QVTU AutoLogin 抢先启动服务"
SERVICE_DESC = ("开机抢先启动 AutoLogin 校园网自动登录：服务先于用户登录就绪，"
                "用户一进入桌面立即把托盘程序拉起（无需等待启动项）。")
