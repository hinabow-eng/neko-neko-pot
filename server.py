from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timedelta

app = Flask(__name__, static_folder='.')

# 部屋ごとにデータを保持する辞書
# 構造: { "部屋ID": { "info": { "name": ..., "theme": ..., "code": ..., "color": ... }, "cats": [...], "updated_at": datetime } }
rooms_db = {}

# 48時間以上更新のない部屋をクリーンアップする関数
def cleanup_old_rooms():
    now = datetime.now()
    expired_rooms = []
    for room_id, data in rooms_db.items():
        updated_at = data.get('updated_at', now)
        if now - updated_at > timedelta(hours=48):
            expired_rooms.append(room_id)
    
    for room_id in expired_rooms:
        del rooms_db[room_id]

@app.route('/')
def index():
    cleanup_old_rooms()
    return send_from_directory('.', 'index.html')

# 部屋情報の取得API
@app.route('/api/room_info')
def get_room_info():
    cleanup_old_rooms()
    room_id = request.args.get('room', '')
    if room_id in rooms_db:
        info = rooms_db[room_id]['info']
        return jsonify({
            'exists': True,
            'name': info['name'],
            'theme': info['theme'],
            'color': info['color']
        })
    return jsonify({'exists': False})

# 部屋の新規作成API
@app.route('/api/create_room', methods=['POST'])
def create_room():
    cleanup_old_rooms()
    data = request.get_json()
    if not data or not data.get('name') or not data.get('code'):
        return jsonify({'status': 'error', 'message': '入力項目が不足しています'}), 400
    
    room_id = data['name']
    
    rooms_db[room_id] = {
        'info': {
            'name': data['name'],
            'theme': data.get('theme', 'みんなの散歩道'),
            'code': str(data['code']),
            'color': data.get('color', 'yellow')
        },
        'cats': [],
        'updated_at': datetime.now()
    }
    return jsonify({'status': 'ok', 'room_id': room_id})

# 猫（投稿）データの取得API
@app.route('/data.json')
def get_data_json():
    cleanup_old_rooms()
    room_id = request.args.get('room', '')
    if room_id in rooms_db:
        return jsonify(rooms_db[room_id]['cats'])
    return jsonify([])

# 猫（投稿）データの保存API
@app.route('/save', methods=['POST'])
def save_data():
    cleanup_old_rooms()
    room_id = request.args.get('room', '')
    data = request.get_json()
    if room_id in rooms_db and data is not None:
        rooms_db[room_id]['cats'] = data
        rooms_db[room_id]['updated_at'] = datetime.now() # 最終更新時間を更新
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error'}), 400

# 解除コード照合＆アクションAPI（テーマ変更・猫クリア・解散）
@app.route('/api/room_action', methods=['POST'])
def room_action():
    cleanup_old_rooms()
    data = request.get_json()
    room_id = data.get('room')
    action = data.get('action') # 'change_theme', 'clear_cats', 'delete'
    code = str(data.get('code', ''))

    if room_id not in rooms_db:
        return jsonify({'status': 'error', 'message': '部屋が存在しません'}), 404

    # 解除コード判定
    if rooms_db[room_id]['info']['code'] != code:
        return jsonify({'status': 'error', 'message': '解除コードが正しくありません'}), 403

    rooms_db[room_id]['updated_at'] = datetime.now()

    if action == 'delete':
        del rooms_db[room_id]
        return jsonify({'status': 'ok', 'action': 'delete'})

    elif action == 'clear_cats':
        rooms_db[room_id]['cats'] = []
        return jsonify({'status': 'ok', 'action': 'clear_cats'})

    elif action == 'change_theme':
        new_theme = data.get('new_theme', '')
        rooms_db[room_id]['info']['theme'] = new_theme
        return jsonify({'status': 'ok', 'action': 'change_theme', 'new_theme': new_theme})

    return jsonify({'status': 'error', 'message': '無効な操作です'}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)