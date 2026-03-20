#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图标转换工具：将头像图片转换为 ICO 格式
"""

import os
import sys
from PIL import Image

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    import locale
    try:
        locale.setlocale(locale.LC_ALL, 'zh_CN.UTF-8')
    except:
        pass

def convert_to_icon(input_path, output_path, sizes=[256, 128, 64, 48, 32, 16]):
    """
    将图片转换为 ICO 格式

    Args:
        input_path: 输入图片路径
        output_path: 输出 ICO 文件路径
        sizes: 要包含的图标尺寸列表
    """
    try:
        # 打开原始图片
        img = Image.open(input_path)

        # 确保是 PNG 格式（支持透明度）
        if img.format != 'PNG':
            # 转换为 PNG
            png_path = input_path.rsplit('.', 1)[0] + '.png'
            img.save(png_path, 'PNG')
            img = Image.open(png_path)

        # 创建多个尺寸的图标
        icon_images = []
        for size in sizes:
            # 调整大小
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            icon_images.append(resized)

        # 保存为 ICO 文件
        icon_images[0].save(
            output_path,
            format='ICO',
            sizes=[(size, size) for size in sizes]
        )

        print(f"✓ 图标转换成功：{output_path}")
        print(f"  包含尺寸：{', '.join([str(s)+'x'+str(s) for s in sizes])}")
        return True

    except Exception as e:
        print(f"✗ 图标转换失败：{e}")
        return False


def main():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    # 输入和输出路径
    avatar_jpg = os.path.join(BASE_DIR, "avatar.jpg")
    avatar_ico = os.path.join(BASE_DIR, "avatar.ico")

    # 检查输入文件
    if not os.path.exists(avatar_jpg):
        print(f"✗ 未找到头像文件：{avatar_jpg}")
        return

    print("=" * 60)
    print("  图标转换工具")
    print("=" * 60)
    print()

    # 转换图标
    if convert_to_icon(avatar_jpg, avatar_ico):
        print()
        print("=" * 60)
        print("  转换完成！")
        print("=" * 60)
        print()
        print("生成的 ICO 文件：")
        print(f"  {avatar_ico}")
        print()
        print("使用方法：")
        print("  将 avatar.ico 配置到 launcher.spec 文件中")
        print("  修改 icon='avatar.ico'")
        print()
    else:
        print()
        print("转换失败，请检查错误信息")


if __name__ == "__main__":
    main()
