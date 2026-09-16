import os
from flask import Flask, request, jsonify

app = Flask(__name__)

# 内存数据库：默认放一条初始测试消息
messages_db = [
    {
        "sender": "系统测试",
        "content": "飞书消息同步服务已就绪，等待实时消息接入...",
        "time": "12:00:00"
    }
]

@app.route('/upload', methods=['POST'])
def upload():
    """供本地脚本调用的消息上传接口"""
    data = request.json or {}
    content = data.get('content', '').strip()
    sender = data.get('sender', '群成员')
    time_str = data.get('time', '')

    if content:
        # 防重复消息逻辑
        if not any(m['content'] == content and m['time'] == time_str for m in messages_db):
            messages_db.append({
                "sender": sender,
                "content": content,
                "time": time_str
            })
            # 仅保留最新的100条消息
            if len(messages_db) > 100:
                messages_db.pop(0)
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "ignored"}), 400

@app.route('/get-messages', methods=['GET'])
def get_messages():
    """供 ChatGPT Actions 调用的获取消息接口"""
    limit = request.args.get('limit', default=15, type=int)
    return jsonify({"latest_messages": messages_db[-limit:]})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
