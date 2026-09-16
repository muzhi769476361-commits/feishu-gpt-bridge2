import os
from flask import Flask, request, jsonify

app = Flask(__name__)

# 存储最新的 100 条消息
messages_db = []

@app.route('/upload', methods=['POST'])
def upload():
    """接收浏览器油猴脚本上传的飞书消息"""
    data = request.json or {}
    content = data.get('content', '').strip()
    sender = data.get('sender', '群成员')
    time_str = data.get('time', '')
    group_name = data.get('group_name', 'A独角兽综合群')

    if content:
        # 去重保存
        if not any(m['content'] == content and m['time'] == time_str for m in messages_db):
            messages_db.append({
                "group_name": group_name,
                "sender": sender,
                "content": content,
                "time": time_str
            })
            if len(messages_db) > 100:
                messages_db.pop(0)
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "ignored"}), 400

@app.route('/get-messages', methods=['GET'])
def get_messages():
    """供 ChatGPT 调用的获取接口"""
    limit = request.args.get('limit', default=15, type=int)
    return jsonify({
        "group": "A独角兽综合群",
        "latest_messages": messages_db[-limit:]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
