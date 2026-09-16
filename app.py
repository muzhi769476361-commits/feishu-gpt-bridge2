import os
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

# 内存数据库：存储最新的 100 条飞书群消息
messages_db = []

@app.route('/feishu-event', methods=['POST'])
def feishu_event():
    """接收飞书开放平台推送事件的接口"""
    data = request.json or {}
    
    # 1. 处理飞书开放平台的首次 URL 校验请求（Challenge）
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})
    
    # 2. 处理接收到的群消息事件
    header = data.get("header", {})
    event_type = header.get("event_type")
    
    if event_type == "im.message.receive_v1":
        event = data.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        
        # 解析消息内容（飞书传过来的 text 是 JSON 字符串）
        content_raw = message.get("content", "{}")
        try:
            content_json = json.loads(content_raw)
            content_text = content_json.get("text", "")
        except:
            content_text = content_raw

        msg_item = {
            "sender_id": sender.get("sender_id", {}).get("open_id", "未知用户"),
            "content": content_text,
            "msg_type": message.get("message_type"),
            "create_time": message.get("create_time")
        }
        
        # 保存到内存列表
        if content_text:
            messages_db.append(msg_item)
            if len(messages_db) > 100:
                messages_db.pop(0)
                
    return jsonify({"status": "success"}), 200

@app.route('/get-messages', methods=['GET'])
def get_messages():
    """供 ChatGPT 调用的获取接口"""
    limit = request.args.get('limit', default=15, type=int)
    return jsonify({"latest_messages": messages_db[-limit:]})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
