import asyncio
import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse
import time
import os
import logging
import io
from PIL import Image
import ffmpeg

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from telethon import TelegramClient
from telethon.errors import ChatIdInvalidError

# -------------------- 配置 --------------------
api_id = 22762141
api_hash = '7e8be8f925116c3f401c6f272410ad15'

# 移除了全局的 TARGET_CHAT_ID，改为在每个接口中作为参数传入

client = TelegramClient(
    'uploader',
    api_id,
    api_hash,
)

app = FastAPI(title="TG 大文件上传服务", version="2025.11 (Enhanced with Chat ID Param)")


# -------------------- 缩略图生成函数 (无变化) --------------------
def generate_video_thumbnail(video_path: str) -> io.BytesIO | None:
    try:
        probe = ffmpeg.probe(video_path)
        video_info = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        width = int(video_info['width'])
        height = int(video_info['height'])

        if width > height:
            new_width = 320
            new_height = int(height * (320 / width))
        else:
            new_height = 320
            new_width = int(width * (320 / height))

        out, _ = (
            ffmpeg
            .input(video_path, ss=0.1)
            .filter('scale', new_width, new_height)
            .output('pipe:', format='image2', vframes=1)
            .run(capture_stdout=True, quiet=True)
        )

        img = Image.open(io.BytesIO(out))
        byte_arr = io.BytesIO()
        img.save(byte_arr, format='JPEG', quality=85)
        byte_arr.seek(0)

        return byte_arr

    except ffmpeg.Error as e:
        logging.error(f"FFmpeg 处理视频 {video_path} 时出错: {e.stderr.decode()}")
        return None
    except Exception as e:
        logging.error(f"生成缩略图时发生未知错误: {e}")
        return None


# -------------------- 上传进度回调 (无变化) --------------------
def progress_callback(current, total):
    if not hasattr(progress_callback, 'start_time'):
        progress_callback.start_time = time.time()
    elapsed = time.time() - progress_callback.start_time
    if elapsed == 0:
        elapsed = 1
    speed = current / 1024 / 1024 / elapsed
    percent = (current / total) * 100
    remaining = total - current
    eta = remaining / (current / elapsed) if current > 0 else 0
    eta_min = int(eta // 60)
    eta_sec = int(eta % 60)
    print(f"\r上传进度: {percent:.1f}% | {current / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB | "
          f"速度: {speed:.1f} MB/s | 剩余: {eta_min:02d}:{eta_sec:02d} ", end="", flush=True)


# -------------------- 修改后的上传接口 (UploadFile) --------------------
@app.post("/upload")
async def upload_video(
        file: UploadFile = File(...),
        caption: str = Form(""),
        chat_id: str = Form(...)  # <--- 新增 chat_id 参数
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="请选择文件")

    temp_path = f"/tmp/{file.filename}"

    try:
        with open(temp_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        total_size = os.path.getsize(temp_path)
        print(f"\n文件 {file.filename} ({total_size / 1024 / 1024:.1f} MB) 已保存到临时目录。")

        # 调用通用上传函数，并传入 chat_id
        return await _upload_and_respond(temp_path, file.filename, caption, chat_id)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理上传文件失败: {str(e)}")

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# -------------------- 修改后的上传接口 (by_path) --------------------
@app.post("/upload_by_path")
async def upload_by_path(
        file_path: str = Form(...),
        caption: str = Form(""),
        chat_id: str = Form(...)  # <--- 新增 chat_id 参数
):
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"文件路径不存在: {file_path}")

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=400, detail=f"路径不是一个文件: {file_path}")

    file_name = os.path.basename(file_path)
    print(f"\n收到通过路径上传的请求: {file_path} 到 {chat_id}")

    # 调用通用上传函数，并传入 chat_id
    return await _upload_and_respond(file_path, file_name, caption, chat_id)


@app.get("/get/me")
async def get_me():
    me = await client.get_me()
    return me.stringify()


@app.get("/get_group")
async def get_dialogs():
    """
    获取当前账号的所有对话（聊天、频道、群组）列表。
    返回每个对话的名称和ID。
    """
    dialogs_list = []
    try:
        # client.iter_dialogs() 是一个异步生成器，我们需要用 async for 来遍历它
        async for dialog in client.iter_dialogs():
            dialogs_list.append({
                "name": dialog.name,
                "id": dialog.id
            })

        return JSONResponse({
            "success": True,
            "count": len(dialogs_list),
            "dialogs": dialogs_list
        })

    except Exception as e:
        logging.error(f"获取对话列表失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取对话列表失败: {str(e)}")

# -------------------- 修改后的通用上传函数 --------------------
async def _upload_and_respond(file_path: str, file_name: str, caption: str, chat_id: str):
    """
    处理文件上传到 Telegram 的核心逻辑，chat_id 现在是参数。
    """
    try:

        # --- 核心逻辑：处理 chat_id ---
        final_chat_id = None
        if isinstance(chat_id, str) and chat_id.lower() == "me":
            # 如果是 "me"，就使用 "me" 字符串
            final_chat_id = "me"
        else:
            # 否则，全部强制转换为 int
            final_chat_id = int(chat_id)
        print(f"准备上传到: {final_chat_id} (类型: {type(final_chat_id).__name__})")

        total_size = os.path.getsize(file_path)
        print(f"开始上传: {file_name} ({total_size / 1024 / 1024:.1f} MB) 到 {chat_id}")

        progress_callback.start_time = time.time()
        start_time = time.time()

        thumbnail_io = None
        if file_name.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')):
            print("检测到视频文件，正在生成缩略图...")
            thumbnail_io = generate_video_thumbnail(file_path)
            if thumbnail_io:
                print("缩略图生成成功。")
            else:
                print("缩略图生成失败，将不发送缩略图。")

        # 发送文件
        async with client:
            with open(file_path, "rb") as fobj:
                message = await client.send_file(
                    entity=final_chat_id,
                    file=fobj,
                    caption=caption or file_name,
                    progress_callback=progress_callback,
                    supports_streaming=True,
                    thumb=thumbnail_io
                )

        total_time = time.time() - start_time
        eta_min = int(total_time // 60)
        eta_sec = int(total_time % 60)
        print(f"\n上传完成！总耗时: {eta_min:02d}:{eta_sec:02d}")

        doc = message.media.document
        return JSONResponse({
            "success": True,
            "message": f"文件已成功上传到 {chat_id}",
            "uploaded_to_chat_id": str(chat_id),  # 返回实际上传到的 chat_id
            "file_id": str(doc.id),
            "access_hash": str(doc.access_hash),
            "file_name": file_name,
            "size_mb": round(doc.size / 1024 / 1024, 2),
            "message_id": message.id,
        })

    except ValueError as e:
        # 当 get_input_entity 找不到 chat_id 时会抛出 ValueError
        raise HTTPException(status_code=400, detail=f"无效的 Chat ID '{chat_id}': {str(e)}")
    except Exception as e:
        logging.error(f"上传到 Telegram 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")


# -------------------- 启动服务 (无变化) --------------------
async def main():
    print("正在连接 Telegram（首次输入手机号+验证码）...")
    await client.start()
    print("登录成功！session 已保存，以后无需再次输入")

    print("\n服务启动！地址 → http://0.0.0.0:8100")
    print("API 文档 (Swagger UI) → http://你的IP:8100/docs\n")

    config = uvicorn.Config(app, host="0.0.0.0", port=8100, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == '__main__':
    client.loop.run_until_complete(main())
##pip install telethon fastapi uvicorn[standard] python-dotenv loguru python-multipart  ffmpeg-python cryptg pillow aiohttp hachoir
