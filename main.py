import os
import json
import requests
from flask import Flask
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from supabase import create_client, Client

# =====================================================
# [1] 系统初始化（Render 生产环境）
# =====================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "render_secret")

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
GOOGLE_SECRET_PATH = "/etc/secrets/youtube_credentials.json"


# =====================================================
# [2] 获取 Google Credentials（核心）
# =====================================================
def get_google_credentials():
    if not os.path.exists(GOOGLE_SECRET_PATH):
        raise RuntimeError("❌ Google Secret 文件不存在，请检查 Render Secret Files")

    return Credentials.from_authorized_user_file(
        GOOGLE_SECRET_PATH,
        scopes=SCOPES
    )


# =====================================================
# [3] YouTube 上传逻辑（生产级稳定版）
# =====================================================
def upload_video_to_youtube(task, credentials):
    temp_file = None
    try:
        print(f"\n🚀 开始处理任务: {task['id']}")

        youtube = build(
            "youtube",
            "v3",
            credentials=credentials,
            cache_discovery=False
        )

        # ---------- 1️⃣ 下载 Supabase 视频 ----------
        video_url = task["video_url"]
        temp_file = f"temp_{task['id']}.mp4"

        print("🌐 下载 Supabase 视频...")
        r = requests.get(video_url, stream=True, timeout=60)
        r.raise_for_status()
        with open(temp_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

        # ---------- 2️⃣ 标记 uploading ----------
        supabase.table("publish_tasks") \
            .update({"status": "uploading"}) \
            .eq("id", task["id"]) \
            .execute()

        # ---------- 3️⃣ 分块上传（Render 无 VPN，极稳） ----------
        media = MediaFileUpload(
            temp_file,
            chunksize=10 * 1024 * 1024,  # Render 可用更大 chunk
            resumable=True
        )

        request_api = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": task.get("title", "AI 自动上传"),
                    "description": task.get("description", "由系统自动发布"),
                    "categoryId": "22"
                },
                "status": {
                    "privacyStatus": "unlisted"
                }
            },
            media_body=media
        )

        print("📤 正在上传到 YouTube...")
        response = None
        while response is None:
            status, response = request_api.next_chunk()
            if status:
                print(f"📶 上传进度: {int(status.progress() * 100)}%")

        video_id = response.get("id")
        print(f"✅ 上传成功！YouTube ID: {video_id}")

        # ---------- 4️⃣ 成功回写 ----------
        supabase.table("publish_tasks") \
            .update({
                "status": "published",
                "youtube_video_id": video_id,
                "error": None
            }) \
            .eq("id", task["id"]) \
            .execute()

    except Exception as e:
        error_msg = str(e)
        print(f"❌ 上传失败: {error_msg}")

        supabase.table("publish_tasks") \
            .update({
                "status": "failed",
                "error": error_msg
            }) \
            .eq("id", task["id"]) \
            .execute()

    finally:
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)
            print("🧹 临时文件已清理")


# =====================================================
# [4] 扫描并执行任务（Render 后端工厂）
# =====================================================
@app.route("/run")
def run_tasks():
    credentials = get_google_credentials()

    print("\n--- 正在巡检数据库任务 ---")
    response = supabase.table("publish_tasks") \
        .select("*") \
        .eq("status", "pending") \
        .execute()

    tasks = response.data or []
    if not tasks:
        return "暂无待发布任务"

    print(f"🎯 发现 {len(tasks)} 个待发布任务")
    for task in tasks:
        upload_video_to_youtube(task, credentials)

    return "🚀 任务执行完成"


# =====================================================
# [5] Render 启动入口
# =====================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
