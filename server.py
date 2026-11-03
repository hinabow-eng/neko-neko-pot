import json
import os
import secrets
import string
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_from_directory

try:
    import fcntl  # Linux / Mac（Renderはここ）。Windowsには無いので、その場合は下のthreading.Lockだけ使う
except ImportError:
    fcntl = None

# static_folder=None：このフォルダのファイルが勝手に公開されないようにする
app = Flask(__name__, static_folder=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------
# 部屋データの保存先（ファイル）
# gunicornのワーカーが何個あっても、同じファイルを見るので部屋が消えない。
# 公開フォルダの外（一時フォルダ）に置くので、URLから直接読まれることもない。
# 場所を変えたいときは、環境変数 NEKO_DATA_FILE にパスを入れる。
# ------------------------------------------------------------
DATA_FILE = os.environ.get('NEKO_DATA_FILE', os.path.join(tempfile.gettempdir(), 'neko_rooms.json'))
LOCK_FILE = DATA_FILE + '.lock'
_thread_lock = threading.Lock()

# 部屋IDに使う文字（英大小文字＋数字）
ROOM_ID_CHARS = string.ascii_letters + string.digits


def _now():
    return datetime.now()


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return _now()


def load_db():
    """ファイルから全部屋を読み込む。48時間以上更新のない部屋は取り除く。"""
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            db = json.load(f)
    except (FileNotFoundError, ValueError):
        db = {}

    now = _now()
    for room_id in list(db.keys()):
        updated_at = _parse_time(db[room_id].get('updated_at', ''))
        if now - updated_at > timedelta(hours=48):
            del db[room_id]
    return db


def save_db(db):
    """途中で壊れないように、一時ファイルに書いてから入れ替える。"""
    tmp_path = DATA_FILE + '.tmp.' + secrets.token_hex(4)
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp_path, DATA_FILE)


@contextmanager
def locked_db():
    """読む → 変更する → 書く、を他のワーカーと同時にやらないようにする。"""
    with _thread_lock:
        lock = open(LOCK_FILE, 'a+')
        try:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            db = load_db()
            yield db
            save_db(db)
        finally:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


# 推測されにくいランダムな部屋ID（20〜30文字）を作る
def generate_room_id(db):
    while True:
        length = secrets.choice(range(20, 31))
        room_id = ''.join(secrets.choice(ROOM_ID_CHARS) for _ in range(length))
        if room_id not in db:
            return room_id


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


# 部屋情報の取得API
@app.route('/api/room_info')
def get_room_info():
    room_id = request.args.get('room', '')
    db = load_db()
    if room_id in db:
        info = db[room_id]['info']
        return jsonify({
            'exists': True,
            'name': info['name'],
            'theme': info['theme'],
            'color': info['color'],
            'fish_limit': info.get('fish_limit', 3)
        })
    return jsonify({'exists': False})


# 部屋の新規作成API
@app.route('/api/create_room', methods=['POST'])
def create_room():
    data = request.get_json(silent=True)
    if not data or not data.get('name') or not data.get('code'):
        return jsonify({'status': 'error', 'message': '入力項目が不足しています'}), 400

    # 1日のおさかな上限（TOPページで選んだ数。999は無限）。想定外の値は標準の3にする
    try:
        fish_limit = int(data.get('fish_limit', 3))
    except (TypeError, ValueError):
        fish_limit = 3
    if fish_limit not in (1, 3, 5, 7, 10, 999):
        fish_limit = 3

    with locked_db() as db:
        room_id = generate_room_id(db)
        db[room_id] = {
            'info': {
                'name': str(data['name'])[:20],
                'theme': data.get('theme', 'みんなの散歩道'),
                'code': str(data['code']),
                'color': data.get('color', 'yellow'),
                'fish_limit': fish_limit
            },
            'cats': [],
            'updated_at': _now().isoformat()
        }
    return jsonify({'status': 'ok', 'room_id': room_id})


# 猫（投稿）データの取得API
@app.route('/data.json')
def get_data_json():
    room_id = request.args.get('room', '')
    db = load_db()
    if room_id in db:
        return jsonify(db[room_id]['cats'])
    return jsonify([])


# 猫（投稿）データの保存API
@app.route('/save', methods=['POST'])
def save_data():
    room_id = request.args.get('room', '')
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'status': 'error'}), 400

    with locked_db() as db:
        if room_id in db:
            db[room_id]['cats'] = data
            db[room_id]['updated_at'] = _now().isoformat()
            return jsonify({'status': 'ok'})
    return jsonify({'status': 'error'}), 400


# 解除コード照合＆アクションAPI（テーマ変更・猫クリア・解散）
@app.route('/api/room_action', methods=['POST'])
def room_action():
    data = request.get_json(silent=True) or {}
    room_id = data.get('room')
    action = data.get('action')  # 'change_theme', 'clear_cats', 'delete'
    code = str(data.get('code', ''))

    with locked_db() as db:
        if room_id not in db:
            return jsonify({'status': 'error', 'message': '部屋が存在しません'}), 404

        # 解除コード判定
        if db[room_id]['info']['code'] != code:
            return jsonify({'status': 'error', 'message': '解除コードが正しくありません'}), 403

        db[room_id]['updated_at'] = _now().isoformat()

        if action == 'delete':
            del db[room_id]
            return jsonify({'status': 'ok', 'action': 'delete'})

        elif action == 'clear_cats':
            db[room_id]['cats'] = []
            return jsonify({'status': 'ok', 'action': 'clear_cats'})

        elif action == 'change_theme':
            new_theme = data.get('new_theme', '')
            db[room_id]['info']['theme'] = new_theme
            return jsonify({'status': 'ok', 'action': 'change_theme', 'new_theme': new_theme})

    return jsonify({'status': 'error', 'message': '無効な操作です'}), 400


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
