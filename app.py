import os
from flask import Flask, request, jsonify

app = Flask(__name__)

# 内存数据库：存储最新的 100 条消息
messages_db = [
    {"sender": "系统提示", "content": "抓取服务已就绪，请保持飞书网页版挂机", "time": "00:00:00"}
]

@app.route('/upload', methods=['POST'])
def upload():
    """接收浏览器油猴脚本上传的飞书消息"""
    data = request.json or {}
    content = data.get('content', '').strip()
    sender = data.get('sender', '群成员')
    time_str = data.get('time', '')

    if content:
        # 去重并写入内存列表
        if not any(m['content'] == content and m['time'] == time_str for m in messages_db):
            messages_db.append({
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
    """供 ChatGPT 调用的获取消息接口"""
    limit = request.args.get('limit', default=15, type=int)
    return jsonify({"latest_messages": messages_db[-limit:]})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
